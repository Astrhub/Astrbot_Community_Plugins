"""Multi-platform OAuth login/binding routes, repository discovery, and admin git-binding controls."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, unquote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..auth import can_moderate_plugins
from ..config import Settings
from ..sessions import (
    extract_request_client_ip,
    extract_request_user_agent,
    session_ttl_for,
)
from .credentials import CredentialStore, require_oauth_credential_key
from .models import (
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    THIRD_PARTY_PROVIDERS,
    GitSourceError,
    OAuthTokenSet,
    normalize_git_repo_key,
)
from .registry import (
    ensure_valid_provider_token,
    get_provider_adapter,
    get_provider_oauth_config,
)

LOGGER = logging.getLogger(__name__)
ALLOWED_RETURN_PATHS = frozenset({"/submit", "/settings/personal", "/plugin-workbench"})


def normalize_oauth_return_path(path: str) -> str | None:
    candidate = str(path or "").strip()
    if candidate in ALLOWED_RETURN_PATHS:
        return candidate
    if not candidate.startswith("/author/"):
        return None
    raw_segment = candidate.removeprefix("/author/")
    if (
        not raw_segment
        or "/" in raw_segment
        or "\\" in raw_segment
        or "?" in raw_segment
        or "#" in raw_segment
    ):
        return None
    segment = unquote(raw_segment).strip()
    if not segment or segment in {".", ".."} or "/" in segment or "\\" in segment:
        return None
    if any(ord(ch) < 32 or ch.isspace() for ch in segment):
        return None
    return f"/author/{quote(segment, safe='')}"


class GitBindingRevokePayload(BaseModel):
    reason: str = Field(default="", max_length=500)


class GitBindingTransferPayload(BaseModel):
    target_user_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=500)


def _state_cookie_name(settings: Settings, provider: str) -> str:
    return f"{settings.oauth_state_cookie_name}_{provider}"


def _return_cookie_name(provider: str) -> str:
    return f"oauth_return_to_{provider}"


def _trust_cookie_name(provider: str) -> str:
    return f"oauth_trust_device_{provider}"


def _safe_normalized_repo_key(repo_url: str) -> str:
    try:
        return normalize_git_repo_key(repo_url)
    except GitSourceError:
        return str(repo_url or "").strip().lower().removesuffix(".git").rstrip("/")


def build_git_provider_router(
    *,
    runtime_settings_for_app: Callable[[Any], Awaitable[Settings]],
    call_store: Callable[..., Awaitable[Any]],
    current_user: Callable[[Request], Awaitable[dict[str, Any] | None]],
    require_user: Callable[[Request], Awaitable[dict[str, Any]]],
    set_cookie: Callable[..., None],
    delete_cookie: Callable[..., None] | None = None,
    enforce_user_rpm_limit: Callable[[Request, str, dict[str, Any], int], Awaitable[None]],
    github_list_repositories: Callable[[Request, dict[str, Any], int], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    def _clear_cookie(response: Response, name: str, settings: Settings) -> None:
        if delete_cookie is not None:
            delete_cookie(response, name, settings)
            return
        response.delete_cookie(
            name,
            path="/",
            httponly=True,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site.lower(),
        )

    def _adapter_for(request: Request, settings: Settings, provider: str) -> Any:
        factory = getattr(request.app.state, "git_provider_adapter_factory", None)
        if factory is not None:
            return factory(settings, provider)
        return get_provider_adapter(settings, provider)

    @router.get(
        "/v1/auth/oauth/{provider}/login",
        tags=["auth"],
        summary="第三方 Git 平台 OAuth 登录或绑定",
        description="重定向到受支持的第三方 Git 平台（GitLab、Gitee、AtomGit、Gitea）OAuth 授权页。",
        responses={
            302: {"description": "重定向到平台授权页"},
            403: {"description": "该平台 OAuth 登录已关闭"},
            404: {"description": "不支持的平台"},
            501: {"description": "该平台 OAuth 尚未配置"},
            503: {"description": "OAUTH_CREDENTIAL_KEY 尚未配置"},
        },
    )
    async def oauth_provider_login(
        request: Request,
        provider: str,
        next: str = "",
        trust_device: bool = False,
    ) -> Response:
        key = provider.strip().lower()
        if key == "github":
            safe_next = normalize_oauth_return_path(next)
            query_params: dict[str, str] = {}
            if safe_next:
                query_params["next"] = safe_next
            if trust_device:
                query_params["trust_device"] = "1"
            query = f"?{urlencode(query_params)}" if query_params else ""
            return RedirectResponse(f"/v1/auth/github/login{query}", status_code=302)
        if key not in THIRD_PARTY_PROVIDERS:
            raise HTTPException(
                404, {"code": "unsupported_git_provider", "error": "不支持的 OAuth 平台"}
            )
        settings = await runtime_settings_for_app(request.app)
        oauth_cfg = get_provider_oauth_config(settings, key)
        label = PROVIDER_LABELS[key]
        if not oauth_cfg.enabled:
            return JSONResponse(status_code=403, content={"error": f"{label} login is disabled"})
        if not oauth_cfg.client_id or not oauth_cfg.client_secret:
            return JSONResponse(
                status_code=501,
                content={
                    "error": f"{label} OAuth is not configured",
                    "next": (
                        f"Set {key.upper()}_CLIENT_ID, {key.upper()}_CLIENT_SECRET and "
                        f"{key.upper()}_CALLBACK_URL"
                    ),
                },
            )
        require_oauth_credential_key(settings.oauth_credential_key)

        adapter = _adapter_for(request, settings, key)
        state = str(uuid.uuid4())
        authorize_url = adapter.build_authorize_url(state=state)
        response = RedirectResponse(authorize_url, status_code=302)
        set_cookie(response, _state_cookie_name(settings, key), state, settings, max_age=600)
        destination = normalize_oauth_return_path(next) or "/"
        set_cookie(response, _return_cookie_name(key), destination, settings, max_age=600)
        set_cookie(
            response,
            _trust_cookie_name(key),
            "1" if trust_device else "0",
            settings,
            max_age=600,
        )
        return response

    @router.get(
        "/v1/auth/oauth/{provider}/callback",
        tags=["auth"],
        summary="第三方 Git 平台 OAuth 回调",
        description="处理第三方 Git 平台 OAuth 回调，绑定身份并持久化加密令牌后签发会话。",
        responses={
            302: {"description": "登录或绑定成功并重定向"},
            400: {"description": "无效的 OAuth 回调"},
            403: {"description": "该平台 OAuth 登录已关闭"},
            404: {"description": "不支持的平台"},
            503: {"description": "OAUTH_CREDENTIAL_KEY 未配置或平台暂不可用"},
        },
    )
    async def oauth_provider_callback(
        request: Request,
        provider: str,
        code: str | None = None,
        state: str | None = None,
    ) -> Response:
        key = provider.strip().lower()
        if key not in THIRD_PARTY_PROVIDERS:
            raise HTTPException(
                404, {"code": "unsupported_git_provider", "error": "不支持的 OAuth 平台"}
            )
        settings = await runtime_settings_for_app(request.app)
        oauth_cfg = get_provider_oauth_config(settings, key)
        if not oauth_cfg.enabled:
            raise HTTPException(
                403,
                {"code": "oauth_provider_disabled", "error": f"{PROVIDER_LABELS[key]} 登录已关闭"},
            )
        require_oauth_credential_key(settings.oauth_credential_key)

        expected_state = request.cookies.get(_state_cookie_name(settings, key))
        if not code or not state or not expected_state or state != expected_state:
            raise HTTPException(
                400, {"code": "invalid_oauth_callback", "error": "Invalid OAuth callback"}
            )

        adapter = _adapter_for(request, settings, key)
        try:
            raw_tokens = await adapter.exchange_code(code)
            profile = await adapter.fetch_user_profile(raw_tokens.access_token)
        except GitSourceError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code, "error": exc.message}) from exc

        token_set = OAuthTokenSet(
            provider=key,
            access_token=raw_tokens.access_token,
            refresh_token=raw_tokens.refresh_token,
            scope=raw_tokens.scope,
            expires_at=raw_tokens.expires_at,
            provider_user_id=profile.provider_user_id,
            provider_login=profile.provider_login,
        )

        logged_in = await current_user(request)
        existing_identity = await call_store(
            request,
            "get_oauth_identity_by_provider_user",
            key,
            profile.instance_url,
            profile.provider_user_id,
        )
        created_new_user = False
        if logged_in:
            if existing_identity and existing_identity["user_id"] != logged_in["id"]:
                raise HTTPException(
                    409,
                    {
                        "code": "oauth_identity_conflict",
                        "error": f"该 {PROVIDER_LABELS[key]} 账号已绑定到其他市场账号",
                    },
                )
            user = logged_in
        elif existing_identity:
            user = await call_store(request, "get_user_by_id", existing_identity["user_id"])
            if not user:
                raise HTTPException(404, {"code": "user_not_found", "error": "关联账号不存在"})
        else:
            user = await call_store(
                request,
                "create_oauth_user",
                provider=key,
                provider_login=profile.provider_login,
                display_name=profile.display_name,
                avatar_url=profile.avatar_url,
            )
            created_new_user = True

        cred_store = CredentialStore(request.app.state.store, settings.oauth_credential_key)
        existing_cred_ref = str((existing_identity or {}).get("credential_ref") or "").strip()
        credential_ref = await cred_store.save_token_set(
            user_id=str(user["id"]),
            token_set=token_set,
            credential_ref=existing_cred_ref,
        )
        try:
            await call_store(
                request,
                "upsert_oauth_identity",
                user["id"],
                provider=key,
                instance_url=profile.instance_url,
                provider_user_id=profile.provider_user_id,
                provider_login=profile.provider_login,
                provider_email=profile.email,
                avatar_url=profile.avatar_url,
                scopes=token_set.scope,
                credential_ref=credential_ref,
                expires_at=token_set.expires_at,
            )
        except Exception:
            if not existing_cred_ref:
                await cred_store.revoke(credential_ref)
            if created_new_user:
                winner = await call_store(
                    request,
                    "get_oauth_identity_by_provider_user",
                    key,
                    profile.instance_url,
                    profile.provider_user_id,
                )
                if winner and winner.get("user_id") and winner["user_id"] != user["id"]:
                    await call_store(request, "delete_user", user["id"], winner["user_id"])
                    winning_user = await call_store(request, "get_user_by_id", winner["user_id"])
                    if winning_user:
                        user = winning_user
                    else:
                        raise
                else:
                    raise
            else:
                raise

        existing_token = request.cookies.get(settings.session_cookie_name) or ""
        existing_session = (
            await call_store(request, "get_session_by_token", existing_token)
            if (logged_in and existing_token)
            else None
        )
        preserve_existing = bool(
            logged_in
            and existing_session
            and str(existing_session.get("user_id") or "") == str(user["id"])
        )
        destination = (
            normalize_oauth_return_path(request.cookies.get(_return_cookie_name(key), "")) or ""
        )
        response = RedirectResponse(settings.web_url.rstrip("/") + destination, status_code=302)
        if not preserve_existing:
            requested_trust = request.cookies.get(_trust_cookie_name(key)) == "1"
            ttl = session_ttl_for(settings, trusted=requested_trust)
            session = await call_store(
                request,
                "create_session",
                user["id"],
                trusted=requested_trust,
                ttl_seconds=ttl,
                user_agent=extract_request_user_agent(request),
                ip_address=extract_request_client_ip(request),
            )
            set_cookie(
                response, settings.session_cookie_name, session["token"], settings, max_age=ttl
            )
        _clear_cookie(response, _state_cookie_name(settings, key), settings)
        _clear_cookie(response, _return_cookie_name(key), settings)
        _clear_cookie(response, _trust_cookie_name(key), settings)
        return response

    @router.get(
        "/v1/me/git/{provider}/repositories",
        tags=["developer"],
        summary="获取指定 Git 平台可管理的公开插件仓库",
        description="返回当前用户在指定已绑定 OAuth 平台下可管理的 astrbot_plugin_* 公开仓库列表。",
    )
    async def list_provider_repositories(
        request: Request,
        provider: str,
        page: int = Query(1, ge=1, le=10000),
    ) -> dict[str, Any]:
        key = provider.strip().lower()
        if key not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                404, {"code": "unsupported_git_provider", "error": "不支持的 Git 平台"}
            )
        user = await require_user(request)
        await enforce_user_rpm_limit(request, f"git_repositories_{key}", user, 60)
        if key == "github":
            return await github_list_repositories(request, user, page)

        settings = await runtime_settings_for_app(request.app)
        adapter = _adapter_for(request, settings, key)
        _, token_set = await ensure_valid_provider_token(
            request.app.state.store,
            settings,
            user,
            key,
            adapter=adapter,
        )
        plugins = await call_store(request, "list_plugins")
        submitted = {
            _safe_normalized_repo_key(
                str(plugin.get("normalized_repo") or plugin.get("repo") or "")
            )
            for plugin in plugins
            if plugin.get("repo")
        }
        try:
            return await adapter.list_manageable_repositories(
                token_set.access_token,
                page=page,
                submitted_normalized_repos=submitted,
            )
        except GitSourceError as exc:
            if exc.status_code == 409 and token_set.refresh_token:
                _, refreshed = await ensure_valid_provider_token(
                    request.app.state.store,
                    settings,
                    user,
                    key,
                    adapter=adapter,
                    force_refresh=True,
                )
                try:
                    return await adapter.list_manageable_repositories(
                        refreshed.access_token,
                        page=page,
                        submitted_normalized_repos=submitted,
                    )
                except GitSourceError as retry_exc:
                    raise HTTPException(
                        retry_exc.status_code,
                        {"code": retry_exc.code, "error": retry_exc.message},
                    ) from retry_exc
            raise HTTPException(exc.status_code, {"code": exc.code, "error": exc.message}) from exc

    @router.get(
        "/v1/admin/git-bindings",
        tags=["admin"],
        summary="查看 Git 仓库绑定列表",
        description="管理员查看全站 Git 仓库归属绑定记录。",
    )
    async def admin_list_git_bindings(
        request: Request,
        include_revoked: bool = False,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise HTTPException(403, {"code": "forbidden", "error": "Forbidden"})
        items = await call_store(
            request, "list_git_repository_bindings", include_revoked=include_revoked
        )
        return {"items": items}

    @router.post(
        "/v1/admin/git-bindings/{binding_id}/revoke",
        tags=["admin"],
        summary="撤销 Git 仓库绑定",
        description="管理员撤销指定 Git 仓库绑定并记录审计原因。",
    )
    async def admin_revoke_git_binding(
        request: Request,
        binding_id: str,
        payload: GitBindingRevokePayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise HTTPException(403, {"code": "forbidden", "error": "Forbidden"})
        revoked = await call_store(
            request,
            "revoke_git_repository_binding",
            binding_id,
            revoked_by=user["id"],
            reason=payload.reason.strip() or "管理员撤销绑定",
        )
        if not revoked:
            raise HTTPException(404, {"code": "binding_not_found", "error": "仓库绑定不存在"})
        LOGGER.info(
            "Admin %s revoked git repository binding %s (%s): %s",
            user["id"],
            binding_id,
            revoked.get("normalized_repo"),
            payload.reason.strip(),
        )
        return {"binding": revoked}

    @router.post(
        "/v1/admin/git-bindings/{binding_id}/transfer",
        tags=["admin"],
        summary="转移 Git 仓库绑定",
        description="管理员将指定 Git 仓库绑定转移至目标用户并递增绑定代次，同时记录审计日志。",
    )
    async def admin_transfer_git_binding(
        request: Request,
        binding_id: str,
        payload: GitBindingTransferPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise HTTPException(403, {"code": "forbidden", "error": "Forbidden"})
        target_user = await call_store(request, "get_user_by_id", payload.target_user_id)
        if not target_user:
            raise HTTPException(404, {"code": "user_not_found", "error": "目标用户不存在"})
        transferred = await call_store(
            request,
            "transfer_git_repository_binding",
            binding_id,
            target_user_id=target_user["id"],
            actor_user_id=user["id"],
            reason=payload.reason.strip() or "管理员转移绑定",
        )
        if not transferred:
            raise HTTPException(404, {"code": "binding_not_found", "error": "仓库绑定不存在"})
        LOGGER.info(
            "Admin %s transferred git repository binding %s (%s) to user %s: %s",
            user["id"],
            binding_id,
            transferred.get("normalized_repo"),
            target_user["id"],
            payload.reason.strip(),
        )
        return {"binding": transferred}

    return router
