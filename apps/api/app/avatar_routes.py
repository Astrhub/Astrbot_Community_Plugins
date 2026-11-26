"""User avatar upload and removal endpoints backed by the avatar storage purpose."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile

from .avatar_upload import (
    MAX_AVATAR_SOURCE_BYTES,
    AvatarValidationError,
    NormalizedAvatar,
    normalize_avatar,
)
from .config import Settings
from .object_storage.models import ObjectStorageError, StoragePurpose

LOGGER = logging.getLogger("astrbot_market.avatar")

AVATAR_UPLOADS_PER_HOUR = 10
AVATAR_RATE_LIMIT_SECONDS = 3600
PRIVATE_NO_STORE = "private, no-store"
AVATAR_OWNER_TYPE = "user_avatar"


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "error": message, **extra})


def _storage_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "storage_manager", None)
    if manager is None:
        raise _error(503, "storage_manager_unavailable", "存储管理服务不可用")
    return manager


async def _reserve_upload_slot(
    call_store: Callable[..., Awaitable[Any]], request: Request, user: dict[str, Any]
) -> None:
    key = f"avatar_upload:{user['id']}:{int(time.time()) // AVATAR_RATE_LIMIT_SECONDS}"
    reserved = await call_store(
        request,
        "reserve_rate_limit",
        key,
        AVATAR_UPLOADS_PER_HOUR,
        AVATAR_RATE_LIMIT_SECONDS,
    )
    if not reserved:
        raise _error(
            429,
            "avatar_upload_rate_limited",
            f"头像上传过于频繁，每小时最多 {AVATAR_UPLOADS_PER_HOUR} 次",
            retry_after=AVATAR_RATE_LIMIT_SECONDS,
        )


async def _read_upload(request: Request, upload: UploadFile) -> bytes:
    # Declared length only short-circuits obviously oversized bodies; the real limit is enforced
    # on the bytes actually read below, so a lying Content-Length cannot smuggle a larger file.
    declared = request.headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > MAX_AVATAR_SOURCE_BYTES + 64 * 1024:
        raise _error(413, "avatar_too_large", "头像不能超过 5 MB")
    try:
        content = await upload.read(MAX_AVATAR_SOURCE_BYTES + 1)
    finally:
        await upload.close()
    return content


async def _discard_failed_object(manager: Any, namespace: str, key: str) -> None:
    try:
        await manager.service.discard_failed_object(
            purpose=StoragePurpose.AVATAR,
            namespace=namespace,
            key=key,
        )
    except Exception:
        LOGGER.exception("Failed to clean up avatar object %s/%s", namespace, key)


async def _delete_object_safely(manager: Any, namespace: str, key: str) -> None:
    try:
        await manager.service.delete_object(
            purpose=StoragePurpose.AVATAR,
            namespace=namespace,
            key=key,
        )
    except Exception:
        LOGGER.exception("Failed to remove orphan avatar object %s/%s", namespace, key)


async def _store_avatar(
    manager: Any, *, user: dict[str, Any], avatar: NormalizedAvatar
) -> tuple[Any, str]:
    """Write the normalized avatar under users/<id> and return (record, public_url)."""
    namespace = f"users/{user['id']}"
    digest = avatar.sha256
    key = f"{digest}.webp"
    if await manager.repository.get_binding(StoragePurpose.AVATAR) is None:
        raise _error(
            503,
            "avatar_storage_unbound",
            "站点尚未配置头像存储，请联系管理员在基础设施页绑定 avatar 用途",
        )

    try:
        await manager.service.put_object(
            purpose=StoragePurpose.AVATAR,
            namespace=namespace,
            key=key,
            data=avatar.content,
            content_type=avatar.content_type,
            max_bytes=MAX_AVATAR_SOURCE_BYTES,
            expected_sha256=digest,
            owner_type=AVATAR_OWNER_TYPE,
            owner_id=str(user["id"]),
            if_absent=True,
        )
    except ObjectStorageError as exc:
        LOGGER.warning("Avatar storage write failed for user %s: %s", user["id"], exc.code)
        await _discard_failed_object(manager, namespace, key)
        raise _error(503, exc.code, "头像写入存储失败") from exc

    record = manager.service.find_active_record_by_purpose_and_key(
        purpose=StoragePurpose.AVATAR,
        namespace=namespace,
        key=key,
    )
    if record is None:
        raise _error(503, "avatar_record_missing", "头像存储记录未生成")
    if (
        record.size_bytes != avatar.size_bytes
        or record.sha256 != avatar.sha256
        or record.content_type != avatar.content_type
    ):
        raise _error(503, "avatar_verification_failed", "头像写入校验失败")

    try:
        public_url = manager.service.public_url(
            purpose=StoragePurpose.AVATAR,
            namespace=namespace,
            key=key,
        )
    except ObjectStorageError as exc:
        LOGGER.warning("Avatar public URL unavailable for user %s: %s", user["id"], exc.code)
        public_url = ""
    # The stored URL carries a short content digest so a replaced avatar is never served from a
    # stale browser or CDN cache, even on backends whose object URL is not content-addressed.
    stored_url = public_url or str(record.public_url or f"/v1/storage/public/{record.id}")
    separator = "&" if "?" in stored_url else "?"
    return record, f"{stored_url}{separator}v={avatar.sha256[:12]}"


def build_avatar_router(
    *,
    runtime_settings_for_app: Callable[[Any], Awaitable[Settings]],
    call_store: Callable[..., Awaitable[Any]],
    require_user: Callable[[Request], Awaitable[dict[str, Any]]],
    enrich_private_user: Callable[[Request, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/me/avatar",
        status_code=201,
        tags=["user"],
        summary="上传头像",
        description=(
            "上传 PNG、JPEG 或 WebP 图片作为当前用户头像。服务端会校正方向、居中裁剪为 1:1、"
            "缩放到不超过 512x512 并转码为 WebP；原文件不超过 5 MB。"
        ),
        responses={
            401: {"description": "未登录"},
            403: {"description": "头像上传已关闭"},
            409: {"description": "并发上传冲突"},
            413: {"description": "图片超过 5 MB 或像素总量超限"},
            415: {"description": "图片格式不支持或解码失败"},
            429: {"description": "上传过于频繁"},
            503: {"description": "头像存储未配置或写入失败"},
        },
    )
    async def upload_my_avatar(
        request: Request, response: Response, file: UploadFile
    ) -> dict[str, Any]:
        # Authenticate and gate before touching the multipart body so anonymous or oversized
        # uploads never reach the decoder.
        user = await require_user(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        settings = await runtime_settings_for_app(request.app)
        if not settings.avatar_upload_enabled:
            raise _error(403, "avatar_upload_disabled", "站点已关闭头像上传")
        await _reserve_upload_slot(call_store, request, user)
        manager = _storage_manager(request)
        await manager.initialize()

        content = await _read_upload(request, file)
        try:
            avatar = normalize_avatar(content)
        except AvatarValidationError as exc:
            raise _error(exc.status_code, exc.code, exc.message) from exc

        current = await call_store(request, "get_user_by_id", user["id"])
        if current is None:
            raise _error(404, "user_not_found", "用户不存在")
        expected_object_id = str(current.get("uploaded_avatar_object_id") or "")

        record, public_url = await _store_avatar(manager, user=user, avatar=avatar)
        try:
            updated = await call_store(
                request,
                "set_user_uploaded_avatar",
                user["id"],
                url=public_url,
                profile_id=record.profile_id,
                object_id=record.id,
                sha256=avatar.sha256,
                expected_object_id=expected_object_id,
            )
        except Exception as exc:
            # The object exists remotely but the user row never adopted it, so remove it to
            # keep the previous avatar intact and avoid an unreachable orphan.
            LOGGER.exception("Avatar database update failed for user %s", user["id"])
            await _delete_object_safely(manager, record.namespace, record.key)
            raise _error(503, "avatar_persist_failed", "头像保存失败") from exc
        if updated is None:
            # Another upload landed between the read and the write; drop this object and report
            # the conflict instead of silently pointing the user at an older image.
            await _delete_object_safely(manager, record.namespace, record.key)
            raise _error(409, "avatar_upload_conflict", "头像正在被另一请求更新，请重试")

        return {
            "user": await enrich_private_user(request, updated),
            "avatar": {
                "url": public_url,
                "content_type": avatar.content_type,
                "size_bytes": avatar.size_bytes,
                "sha256": avatar.sha256,
                "width": avatar.width,
                "height": avatar.height,
            },
        }

    @router.delete(
        "/v1/me/avatar",
        tags=["user"],
        summary="清除已上传头像",
        description=(
            "删除当前用户上传的头像引用并回退到 OAuth 提供的头像；远端历史对象不立即删除。"
        ),
        responses={401: {"description": "未登录"}, 404: {"description": "没有已上传的头像"}},
    )
    async def delete_my_avatar(request: Request, response: Response) -> dict[str, Any]:
        user = await require_user(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        current = await call_store(request, "get_user_by_id", user["id"])
        expected_object_id = str((current or {}).get("uploaded_avatar_object_id") or "")
        if not current or not expected_object_id:
            raise _error(404, "avatar_not_uploaded", "当前没有已上传的头像")
        updated = await call_store(
            request,
            "clear_user_uploaded_avatar",
            user["id"],
            expected_object_id=expected_object_id,
        )
        if updated is None:
            raise _error(409, "avatar_upload_conflict", "头像正在被另一请求更新，请刷新后重试")
        return {"user": await enrich_private_user(request, updated)}

    return router
