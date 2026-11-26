"""Strictly scoped, cached read-only proxy for overseas GitLab/Gitea images and avatars."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from .auth import can_edit_plugin
from .git_sources.models import GitSourceError, parse_repository_ref
from .git_sources.registry import get_provider_adapter

OVERSEAS_PROXY_PROVIDERS = frozenset({"gitlab", "gitea"})
OVERSEAS_AVATAR_HOSTS = frozenset(
    {
        "gitlab.com",
        "www.gitlab.com",
        "gitea.com",
        "www.gitea.com",
        "secure.gravatar.com",
        "gravatar.com",
        "www.gravatar.com",
        "avatars.githubusercontent.com",
    }
)
ALLOWED_ASSET_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"})
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_README_ASSET_BYTES = 5 * 1024 * 1024
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MEDIA_CACHE_TTL_SECONDS = 3600
MAX_MEMORY_CACHE_ITEMS = 128

_MEMORY_CACHE: OrderedDict[str, dict[str, str]] = OrderedDict()


async def _store_call(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(store, method, None)
    if fn is None:
        return None
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def detect_safe_image_mime(data: bytes) -> str | None:
    """Sniff raster image magic bytes; rejects SVG, HTML, scripts, and unknown binaries."""
    if not data or len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        major_brand = data[8:12]
        compatible_brands = {data[offset : offset + 4] for offset in range(16, 64, 4)}
        if major_brand in {b"avif", b"avis"}:
            return "image/avif"
        if major_brand == b"mif1" and compatible_brands & {b"avif", b"avis"}:
            return "image/avif"
    return None


def is_overseas_avatar_url(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
        return False
    host = (parsed.hostname or "").lower()
    return host in OVERSEAS_AVATAR_HOSTS


def normalize_asset_path(value: str) -> str:
    path = unquote(str(value or "")).strip().lstrip("/")
    if not path or len(path) > 512 or "\\" in path or any(ord(ch) < 32 for ch in path):
        raise HTTPException(400, {"error": "Invalid asset path"})
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(400, {"error": "Invalid asset path"})
    lower = parts[-1].lower()
    if not any(lower.endswith(ext) for ext in ALLOWED_ASSET_EXTENSIONS):
        raise HTTPException(400, {"error": "Unsupported image asset type"})
    return "/".join(parts)


def _cache_key(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((namespace, *parts)).encode("utf-8")).hexdigest()
    return f"market:media-proxy:v1:{digest}"


async def _load_cached_media(store: Any, key: str) -> tuple[bytes, str, str] | None:
    redis_client = getattr(store, "redis", None)
    if redis_client is not None:
        try:
            raw = await redis_client.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw:
                payload = json.loads(raw)
                return (
                    base64.b64decode(payload["data"]),
                    str(payload["content_type"]),
                    str(payload["etag"]),
                )
        except Exception:
            pass
    entry = _MEMORY_CACHE.get(key)
    if entry is not None:
        _MEMORY_CACHE.move_to_end(key)
        return (
            base64.b64decode(entry["data"]),
            entry["content_type"],
            entry["etag"],
        )
    return None


async def _save_cached_media(
    store: Any, key: str, content: bytes, content_type: str, etag: str
) -> None:
    encoded = {
        "data": base64.b64encode(content).decode("ascii"),
        "content_type": content_type,
        "etag": etag,
    }
    redis_client = getattr(store, "redis", None)
    if redis_client is not None:
        try:
            await redis_client.set(key, json.dumps(encoded), ex=MEDIA_CACHE_TTL_SECONDS)
            return
        except Exception:
            pass
    _MEMORY_CACHE[key] = encoded
    _MEMORY_CACHE.move_to_end(key)
    while len(_MEMORY_CACHE) > MAX_MEMORY_CACHE_ITEMS:
        _MEMORY_CACHE.popitem(last=False)


def _image_response(
    request: Request,
    content: bytes,
    content_type: str,
    etag: str,
    *,
    cache_control: str,
) -> Response:
    headers = {
        "Cache-Control": cache_control,
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type=content_type, headers=headers)


async def _fetch_allowlisted_avatar(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bytes, str]:
    current = url
    async with httpx.AsyncClient(
        transport=transport,
        timeout=15.0,
        follow_redirects=False,
        headers={"User-Agent": "Astrhub-Media-Proxy/1.0"},
    ) as client:
        for _ in range(4):
            if not is_overseas_avatar_url(current):
                raise HTTPException(404, {"error": "Avatar unavailable"})
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = str(response.headers.get("location") or "").strip()
                        if not location:
                            raise HTTPException(404, {"error": "Avatar unavailable"})
                        current = str(response.url.join(location))
                        continue
                    if response.status_code != 200:
                        raise HTTPException(404, {"error": "Avatar unavailable"})
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_AVATAR_BYTES:
                            raise HTTPException(413, {"error": "Avatar too large"})
                    raw = bytes(buf)
                    mime = detect_safe_image_mime(raw)
                    if not mime:
                        raise HTTPException(415, {"error": "Unsupported avatar media type"})
                    return raw, mime
            except HTTPException:
                raise
            except httpx.HTTPError as exc:
                raise HTTPException(502, {"error": "Upstream avatar fetch failed"}) from exc
    raise HTTPException(404, {"error": "Avatar unavailable"})


def _adapter_for(app: Any, settings: Any, provider: str) -> Any:
    factory = getattr(app.state, "git_provider_adapter_factory", None)
    if factory is not None:
        return factory(settings, provider)
    return get_provider_adapter(settings, provider)


def build_media_proxy_router(
    *,
    current_user_fn: Callable[[Request], Awaitable[dict[str, Any] | None]],
    runtime_settings_fn: Callable[[Any], Awaitable[Any]],
) -> APIRouter:
    router = APIRouter()

    async def _visible_overseas_plugin(
        request: Request, plugin_id: str
    ) -> tuple[dict[str, Any], Any]:
        store = request.app.state.store
        plugin = await _store_call(store, "get_plugin", plugin_id)
        if not plugin:
            raise HTTPException(404, {"error": "Plugin not found"})
        if plugin.get("status") != "listed":
            user = await current_user_fn(request)
            if not user or not can_edit_plugin(user, plugin):
                raise HTTPException(404, {"error": "Plugin not found"})
        try:
            ref = parse_repository_ref(str(plugin.get("repo") or ""))
        except GitSourceError as exc:
            raise HTTPException(404, {"error": "Plugin repository unavailable"}) from exc
        if ref.provider not in OVERSEAS_PROXY_PROVIDERS:
            raise HTTPException(
                404, {"error": "Media proxy only serves overseas GitLab/Gitea repos"}
            )
        return plugin, ref

    @router.get(
        "/v1/plugins/{plugin_id}/logo",
        tags=["plugins"],
        summary="代理获取海外平台插件图标",
        responses={
            200: {"description": "图片字节流"},
            304: {"description": "未变更"},
            404: {"description": "插件或图标不存在"},
        },
    )
    async def plugin_logo_proxy(request: Request, plugin_id: str) -> Response:
        plugin, ref = await _visible_overseas_plugin(request, plugin_id)
        cache_control = (
            "public, max-age=3600" if plugin.get("status") == "listed" else "private, no-store"
        )
        key = _cache_key(
            "logo", str(plugin["id"]), ref.normalized_repo, str(plugin.get("logo") or "")
        )
        cached = await _load_cached_media(request.app.state.store, key)
        if cached is not None:
            content, content_type, etag = cached
            return _image_response(
                request, content, content_type, etag, cache_control=cache_control
            )

        settings = await runtime_settings_fn(request.app)
        adapter = _adapter_for(request.app, settings, ref.provider)
        try:
            raw = await adapter.fetch_raw_file(ref, "logo.png")
        except GitSourceError as exc:
            raise HTTPException(502, {"error": exc.message}) from exc
        if not raw:
            raise HTTPException(404, {"error": "Plugin logo not found"})
        if len(raw) > MAX_LOGO_BYTES:
            raise HTTPException(413, {"error": "Plugin logo too large"})
        mime = detect_safe_image_mime(raw)
        if not mime:
            raise HTTPException(415, {"error": "Unsupported plugin logo media type"})
        etag = f'W/"{hashlib.sha256(raw).hexdigest()[:24]}"'
        await _save_cached_media(request.app.state.store, key, raw, mime, etag)
        return _image_response(request, raw, mime, etag, cache_control=cache_control)

    @router.get(
        "/v1/plugins/{plugin_id}/readme-asset",
        tags=["plugins"],
        summary="代理获取海外平台插件 README 仓库内图片",
        responses={
            200: {"description": "图片字节流"},
            304: {"description": "未变更"},
            400: {"description": "资源路径非法"},
            404: {"description": "插件或资源不存在"},
        },
    )
    async def plugin_readme_asset_proxy(
        request: Request,
        plugin_id: str,
        path: str = Query(..., min_length=1, max_length=512),
    ) -> Response:
        plugin, ref = await _visible_overseas_plugin(request, plugin_id)
        normalized_path = normalize_asset_path(path)
        cache_control = (
            "public, max-age=3600" if plugin.get("status") == "listed" else "private, no-store"
        )
        key = _cache_key("readme-asset", str(plugin["id"]), ref.normalized_repo, normalized_path)
        cached = await _load_cached_media(request.app.state.store, key)
        if cached is not None:
            content, content_type, etag = cached
            return _image_response(
                request, content, content_type, etag, cache_control=cache_control
            )

        settings = await runtime_settings_fn(request.app)
        adapter = _adapter_for(request.app, settings, ref.provider)
        try:
            raw = await adapter.fetch_raw_file(ref, normalized_path)
        except GitSourceError as exc:
            raise HTTPException(502, {"error": exc.message}) from exc
        if not raw:
            raise HTTPException(404, {"error": "Plugin README asset not found"})
        if len(raw) > MAX_README_ASSET_BYTES:
            raise HTTPException(413, {"error": "Plugin README asset too large"})
        mime = detect_safe_image_mime(raw)
        if not mime:
            raise HTTPException(415, {"error": "Unsupported README asset media type"})
        etag = f'W/"{hashlib.sha256(raw).hexdigest()[:24]}"'
        await _save_cached_media(request.app.state.store, key, raw, mime, etag)
        return _image_response(request, raw, mime, etag, cache_control=cache_control)

    @router.get(
        "/v1/authors/{username}/avatar",
        tags=["plugins"],
        summary="代理获取作者海外平台头像",
        responses={
            200: {"description": "头像图片字节流"},
            304: {"description": "未变更"},
            404: {"description": "作者或头像不存在"},
        },
    )
    async def author_avatar_proxy(request: Request, username: str) -> Response:
        store = request.app.state.store
        author = await _store_call(store, "get_user_by_public_username", username)
        if not author:
            author = await _store_call(store, "get_user_by_github_login", username)
        if not author:
            raise HTTPException(404, {"error": "Author not found"})
        avatar_url = str(author.get("avatar_url") or author.get("avatar") or "").strip()
        if not is_overseas_avatar_url(avatar_url):
            raise HTTPException(404, {"error": "Avatar not served by proxy"})
        key = _cache_key("avatar", str(author["id"]), avatar_url)
        cached = await _load_cached_media(store, key)
        if cached is not None:
            content, content_type, etag = cached
            return _image_response(
                request, content, content_type, etag, cache_control="public, max-age=3600"
            )
        transport = getattr(request.app.state, "media_proxy_transport", None)
        raw, mime = await _fetch_allowlisted_avatar(avatar_url, transport=transport)
        etag = f'W/"{hashlib.sha256(raw).hexdigest()[:24]}"'
        await _save_cached_media(store, key, raw, mime, etag)
        return _image_response(request, raw, mime, etag, cache_control="public, max-age=3600")

    @router.get(
        "/v1/me/avatar",
        tags=["account"],
        summary="代理获取当前登录用户的海外平台头像",
        responses={
            200: {"description": "头像图片字节流"},
            401: {"description": "未登录"},
            404: {"description": "头像不存在"},
        },
    )
    async def my_avatar_proxy(request: Request) -> Response:
        user = await current_user_fn(request)
        if not user:
            raise HTTPException(401, {"error": "Not authenticated"})
        avatar_url = str(user.get("avatar_url") or user.get("avatar") or "").strip()
        if not is_overseas_avatar_url(avatar_url):
            raise HTTPException(404, {"error": "Avatar not served by proxy"})
        store = request.app.state.store
        key = _cache_key("avatar", str(user["id"]), avatar_url)
        cached = await _load_cached_media(store, key)
        if cached is not None:
            content, content_type, etag = cached
            return _image_response(
                request, content, content_type, etag, cache_control="private, no-store"
            )
        transport = getattr(request.app.state, "media_proxy_transport", None)
        raw, mime = await _fetch_allowlisted_avatar(avatar_url, transport=transport)
        etag = f'W/"{hashlib.sha256(raw).hexdigest()[:24]}"'
        await _save_cached_media(store, key, raw, mime, etag)
        return _image_response(request, raw, mime, etag, cache_control="private, no-store")

    return router
