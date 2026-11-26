"""User avatar upload and removal endpoints backed by the avatar storage purpose."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException

from .avatar_upload import (
    MAX_AVATAR_SOURCE_BYTES,
    AvatarValidationError,
    NormalizedAvatar,
    normalize_avatar,
)
from .config import Settings
from .object_storage.models import ObjectStorageError, StorageObjectStatus, StoragePurpose

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


async def _read_upload(request: Request) -> bytes:
    # Check Content-Length before parsing multipart data so oversized payloads are rejected
    # without spooling; the exact byte limit is still enforced on the bytes read below.
    declared = request.headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > MAX_AVATAR_SOURCE_BYTES + 64 * 1024:
        raise _error(413, "avatar_too_large", "头像不能超过 5 MB")
    try:
        async with request.form(max_files=1, max_fields=8, max_part_size=64 * 1024) as form:
            upload = form.get("file")
            if not isinstance(upload, StarletteUploadFile):
                raise _error(422, "avatar_file_required", "请上传头像文件")
            return await upload.read(MAX_AVATAR_SOURCE_BYTES + 1)
    except MultiPartException as exc:
        raise _error(400, "avatar_invalid_multipart", "头像上传表单格式无效") from exc


def _was_adopted(object_id: str, current_user: dict[str, Any] | None) -> bool:
    # A concurrent winner or a committed-but-unreported update can adopt this very object
    # because upload keys are content-addressed; the user row is the source of truth.
    return current_user is not None and str(
        current_user.get("uploaded_avatar_object_id") or ""
    ) == str(object_id)


async def _find_active_avatar_record(
    manager: Any, *, profile_id: str, namespace: str, key: str
) -> Any | None:
    record = manager.service.find_object_record(
        profile_id=profile_id,
        namespace=namespace,
        key=key,
    )
    if record is not None and record.status == StorageObjectStatus.ACTIVE:
        return record
    repository = getattr(manager, "repository", None)
    if repository is not None:
        repo_record = await repository.find_active_object(
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        if repo_record is not None and repo_record.status == StorageObjectStatus.ACTIVE:
            return repo_record
    return None


async def _discard_unreferenced_failed_object(
    manager: Any,
    *,
    profile_id: str,
    namespace: str,
    key: str,
    existed_before: bool,
) -> None:
    # Never delete a live shared object if one already existed or another request activated it.
    if existed_before:
        return
    active = await _find_active_avatar_record(
        manager,
        profile_id=profile_id,
        namespace=namespace,
        key=key,
    )
    if active is not None:
        return
    try:
        await manager.service.discard_failed_object(
            purpose=StoragePurpose.AVATAR,
            namespace=namespace,
            key=key,
        )
    except Exception:
        LOGGER.exception("Failed to discard failed avatar object %s/%s", namespace, key)


async def _delete_unadopted_object(
    manager: Any,
    *,
    object_id: str,
    namespace: str,
    key: str,
    previous_object_id: str,
    created_new: bool,
    call_store: Callable[..., Awaitable[Any]],
    request: Request,
    user_id: str,
) -> None:
    # Content-addressed keys mean an upload may reuse an existing object (if_absent); deleting
    # it would break whoever else references the same bytes. Only drop objects this request
    # actually created and that no user row points at any more.
    if not created_new:
        return
    current = None
    try:
        current = await call_store(request, "get_user_by_id", user_id)
    except Exception:
        LOGGER.exception("Failed to reread user %s before avatar cleanup", user_id)
        current = None
    if _was_adopted(object_id, current):
        return
    record = manager.service.get_object_record(object_id)
    # An upload whose content equals the previous avatar reuses the same content-addressed key
    # (if_absent), so deleting it would drop the live avatar; only remove truly new objects.
    if record is None or str(record.id) == str(previous_object_id):
        return
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
) -> tuple[Any, str, bool]:
    """Write the normalized avatar under users/<id> and return (record, public_url, created_new)."""
    namespace = f"users/{user['id']}"
    digest = avatar.sha256
    key = f"{digest}.webp"
    binding = await manager.repository.get_binding(StoragePurpose.AVATAR)
    if binding is None:
        raise _error(
            503,
            "avatar_storage_unbound",
            "站点尚未配置头像存储，请联系管理员在基础设施页绑定 avatar 用途",
        )
    profile_id = binding.profile_id
    existed_before = (
        await _find_active_avatar_record(
            manager,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        is not None
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
        await _discard_unreferenced_failed_object(
            manager,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
            existed_before=existed_before,
        )
        raise _error(503, exc.code, "头像写入存储失败") from exc

    record = await _find_active_avatar_record(
        manager,
        profile_id=profile_id,
        namespace=namespace,
        key=key,
    )
    if record is None:
        await _discard_unreferenced_failed_object(
            manager,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
            existed_before=existed_before,
        )
        raise _error(503, "avatar_record_missing", "头像存储记录未生成")
    if (
        record.size_bytes != avatar.size_bytes
        or record.sha256 != avatar.sha256
        or record.content_type != avatar.content_type
    ):
        if not existed_before:
            try:
                await manager.service.discard_failed_object(
                    purpose=StoragePurpose.AVATAR,
                    namespace=namespace,
                    key=key,
                )
            except Exception:
                LOGGER.exception("Failed to discard unverified avatar object %s/%s", namespace, key)
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
    return record, f"{stored_url}{separator}v={avatar.sha256[:12]}", not existed_before


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
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {
                                "file": {
                                    "type": "string",
                                    "format": "binary",
                                    "description": "头像图片文件（PNG、JPEG 或 WebP，不超过 5 MB）",
                                }
                            },
                        }
                    }
                },
            }
        },
        responses={
            400: {"description": "上传表单格式无效或图片为空"},
            401: {"description": "未登录"},
            403: {"description": "头像上传已关闭"},
            409: {"description": "并发上传冲突"},
            413: {"description": "图片超过 5 MB 或像素总量超限"},
            415: {"description": "图片格式不支持或解码失败"},
            422: {"description": "缺少头像文件"},
            429: {"description": "上传过于频繁"},
            503: {"description": "头像存储未配置或写入失败"},
        },
    )
    async def upload_my_avatar(request: Request, response: Response) -> dict[str, Any]:
        # Authenticate and gate before parsing the multipart body so anonymous, disabled,
        # rate-limited, or oversized requests never spool upload data.
        user = await require_user(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        settings = await runtime_settings_for_app(request.app)
        if not settings.avatar_upload_enabled:
            raise _error(403, "avatar_upload_disabled", "站点已关闭头像上传")
        await _reserve_upload_slot(call_store, request, user)
        manager = _storage_manager(request)
        await manager.initialize()

        content = await _read_upload(request)
        try:
            avatar = normalize_avatar(content)
        except AvatarValidationError as exc:
            raise _error(exc.status_code, exc.code, exc.message) from exc

        current = await call_store(request, "get_user_by_id", user["id"])
        if current is None:
            raise _error(404, "user_not_found", "用户不存在")
        expected_object_id = str(current.get("uploaded_avatar_object_id") or "")

        record, public_url, created_new = await _store_avatar(manager, user=user, avatar=avatar)
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
            # The write may land while the update's response is lost; the cleanup guard rechecks the
            # user row before touching any remote object.
            LOGGER.exception("Avatar database update failed for user %s", user["id"])
            await _delete_unadopted_object(
                manager,
                object_id=record.id,
                namespace=record.namespace,
                key=record.key,
                previous_object_id=expected_object_id,
                created_new=created_new,
                call_store=call_store,
                request=request,
                user_id=str(user["id"]),
            )
            raise _error(503, "avatar_persist_failed", "头像保存失败") from exc
        if updated is None:
            # Another upload landed between the read and the write; drop this object and report
            # the conflict instead of silently pointing the user at an older image.
            await _delete_unadopted_object(
                manager,
                object_id=record.id,
                namespace=record.namespace,
                key=record.key,
                previous_object_id=expected_object_id,
                created_new=created_new,
                call_store=call_store,
                request=request,
                user_id=str(user["id"]),
            )
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
