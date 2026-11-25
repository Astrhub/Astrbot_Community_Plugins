"""Core-admin storage management routes and same-origin public object delivery endpoints."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .manager import StorageManager
from .models import ObjectStorageError, StorageObjectRecord

PUBLIC_OBJECT_CACHE_CONTROL = "public, max-age=3600"
PRIVATE_NO_STORE = "private, no-store"
_RANGE_HEADER = re.compile(r"^bytes=(\d*)-(\d*)$")


class StorageProfileCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    backend: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    secret: dict[str, Any] = Field(default_factory=dict)


class StorageProfileUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict[str, Any] | None = None
    secret: dict[str, Any] | None = None


class StorageBindingUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=64)
    run_immediately: bool = False


class StorageMigrationUnknownImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[dict[str, str]] | None = None
    run_immediately: bool = False


def _http_error_for_storage(exc: ObjectStorageError) -> HTTPException:
    if exc.code in {
        "storage_profile_not_found",
        "storage_migration_not_found",
        "object_missing",
    }:
        return HTTPException(404, {"code": exc.code, "error": str(exc)})
    if exc.code in {
        "storage_profile_exists",
        "storage_profile_still_bound",
        "storage_profile_has_unmigrated_objects",
        "storage_profile_has_unfinished_migrations",
        "storage_profile_has_unknown_objects",
        "storage_profile_not_active",
        "storage_profile_immutable",
        "storage_migration_already_finished",
        "storage_migration_not_paused",
        "storage_migration_no_failed_items",
        "storage_migration_stale",
        "storage_migration_no_unknown_objects",
        "object_key_conflict",
        "webdav_capability_check_failed",
        "imgbed_plugin_gate_failed",
        "imgbed_capability_check_failed",
        "imgbed_scope_missing",
    }:
        return HTTPException(409, {"code": exc.code, "error": str(exc)})
    if exc.code in {
        "storage_credential_key_unconfigured",
        "storage_credential_decrypt_failed",
        "pg_pool_unavailable",
        "storage_backend_not_yet_implemented",
    }:
        return HTTPException(503, {"code": exc.code, "error": str(exc)})
    return HTTPException(400, {"code": exc.code, "error": str(exc)})


def _storage_manager(request: Request) -> StorageManager:
    manager = getattr(request.app.state, "storage_manager", None)
    if manager is None:
        raise HTTPException(
            503, {"code": "storage_manager_unavailable", "error": "存储管理服务不可用"}
        )
    return manager


def _parse_single_byte_range(header_value: str, size_bytes: int) -> tuple[int, int] | None:
    cleaned = header_value.strip()
    match = _RANGE_HEADER.fullmatch(cleaned)
    if not match or size_bytes <= 0:
        return None
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        suffix_len = int(end_raw)
        if suffix_len <= 0:
            return None
        start = max(0, size_bytes - suffix_len)
        end = size_bytes - 1
        return start, end
    start = int(start_raw)
    if start >= size_bytes:
        return None
    end = int(end_raw) if end_raw else size_bytes - 1
    if end < start:
        return None
    return start, min(end, size_bytes - 1)


async def _serve_public_object(
    request: Request,
    manager: StorageManager,
    record: StorageObjectRecord,
) -> Response:
    driver = manager.service.driver_for_profile(record.profile_id)
    size_bytes = record.size_bytes
    content_type = record.content_type or "application/octet-stream"
    etag = f'"{record.sha256}"' if record.sha256 else ""
    base_headers = {
        "Cache-Control": PUBLIC_OBJECT_CACHE_CONTROL,
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
    }
    if etag:
        base_headers["ETag"] = etag

    range_header = request.headers.get("range", "").strip()
    if range_header:
        parsed = _parse_single_byte_range(range_header, size_bytes)
        if parsed is None:
            return Response(
                status_code=416,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes */{size_bytes}",
                },
            )
        start, end_inclusive = parsed
        length = end_inclusive - start + 1
        range_headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end_inclusive}/{size_bytes}",
            "Content-Length": str(length),
        }
        if request.method == "HEAD":
            return Response(status_code=206, media_type=content_type, headers=range_headers)
        ranged = await driver.read_range(
            record.key,
            namespace=record.namespace,
            start_byte=start,
            max_bytes=length,
            expected_size_bytes=size_bytes,
            expected_sha256=record.sha256,
        )
        return Response(
            content=ranged.content,
            status_code=206,
            media_type=content_type,
            headers=range_headers,
        )

    full_headers = {
        **base_headers,
        "Content-Length": str(size_bytes),
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=content_type, headers=full_headers)
    content = await driver.read_object(
        record.key,
        namespace=record.namespace,
        expected_sha256=record.sha256,
    )
    return Response(
        content=content,
        status_code=200,
        media_type=content_type,
        headers=full_headers,
    )


def build_object_storage_router(
    *,
    require_core_admin: Callable[[Request], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/v1/admin/storage/profiles",
        tags=["core-admin"],
        summary="获取存储 Profile 列表",
        description="返回所有存储 Profile、绑定用途、健康状态与对象统计。仅核心管理员可用。",
    )
    async def list_storage_profiles(request: Request, response: Response) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        return {"items": await manager.list_profiles(include_removed=True)}

    @router.post(
        "/v1/admin/storage/profiles",
        status_code=201,
        tags=["core-admin"],
        summary="创建存储 Profile",
        description="创建独立对象存储 Profile，凭据使用 OAUTH_CREDENTIAL_KEY 派生密钥加密保存。",
    )
    async def create_storage_profile(
        request: Request,
        response: Response,
        payload: StorageProfileCreatePayload,
    ) -> dict[str, Any]:
        actor = await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.create_profile(
                profile_id=payload.id,
                name=payload.name,
                backend=payload.backend,
                config=payload.config,
                secret=payload.secret,
                actor_id=str(actor.get("id") or ""),
            )
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.patch(
        "/v1/admin/storage/profiles/{profile_id}",
        tags=["core-admin"],
        summary="编辑存储 Profile",
        description="更新存储 Profile 配置；敏感字段留空表示保留原凭据。",
    )
    async def update_storage_profile(
        request: Request,
        response: Response,
        profile_id: str,
        payload: StorageProfileUpdatePayload,
    ) -> dict[str, Any]:
        actor = await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.update_profile(
                profile_id,
                name=payload.name,
                config=payload.config,
                secret=payload.secret,
                actor_id=str(actor.get("id") or ""),
            )
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.delete(
        "/v1/admin/storage/profiles/{profile_id}",
        tags=["core-admin"],
        summary="移除存储 Profile",
        description="仅当 Profile 无活动绑定、未迁移对象与在途任务时允许移除并保留审计记录。",
    )
    async def delete_storage_profile(
        request: Request,
        response: Response,
        profile_id: str,
    ) -> dict[str, Any]:
        actor = await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.delete_profile(
                profile_id,
                actor_id=str(actor.get("id") or ""),
            )
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.post(
        "/v1/admin/storage/profiles/{profile_id}/test",
        tags=["core-admin"],
        summary="测试存储 Profile 连接",
        description="使用专用测试对象校验写入、读取、Range、公开 URL 与清理能力。",
    )
    async def test_storage_profile(
        request: Request,
        response: Response,
        profile_id: str,
    ) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.test_profile(profile_id)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.post(
        "/v1/admin/storage/profiles/{profile_id}/retire",
        tags=["core-admin"],
        summary="退役存储 Profile",
        description="满足下线门槛后停止市场访问并撤销凭据，不自动删除远端历史对象。",
    )
    async def retire_storage_profile(
        request: Request,
        response: Response,
        profile_id: str,
    ) -> dict[str, Any]:
        actor = await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.retire_profile(
                profile_id,
                actor_id=str(actor.get("id") or ""),
            )
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.get(
        "/v1/admin/storage/bindings",
        tags=["core-admin"],
        summary="获取存储用途绑定",
        description="返回隔离区、插件下载与头像三个用途的当前绑定 Profile。",
    )
    async def list_storage_bindings(request: Request, response: Response) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        return {"items": await manager.list_bindings()}

    @router.put(
        "/v1/admin/storage/bindings/{purpose}",
        tags=["core-admin"],
        summary="绑定或切换存储用途",
        description="将指定用途绑定到活动 Profile；改绑时自动为源 Profile 历史对象创建迁移任务。",
    )
    async def update_storage_binding(
        request: Request,
        response: Response,
        purpose: str,
        payload: StorageBindingUpdatePayload,
    ) -> dict[str, Any]:
        actor = await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.bind_purpose(
                purpose,
                payload.profile_id,
                actor_id=str(actor.get("id") or ""),
                run_immediately=payload.run_immediately,
            )
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.get(
        "/v1/admin/storage/migrations",
        tags=["core-admin"],
        summary="获取存储迁移任务列表",
        description="返回历史与在途存储迁移任务摘要。",
    )
    async def list_storage_migrations(request: Request, response: Response) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        return {"items": await manager.list_migrations()}

    @router.get(
        "/v1/admin/storage/migrations/{migration_id}",
        tags=["core-admin"],
        summary="获取存储迁移进度详情",
        description="返回指定迁移任务的进度统计、未知对象清单与逐对象迁移条目。",
    )
    async def get_storage_migration(
        request: Request,
        response: Response,
        migration_id: str,
    ) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.get_migration_detail(migration_id)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.post(
        "/v1/admin/storage/migrations/{migration_id}/retry-failed",
        tags=["core-admin"],
        summary="重试迁移失败项",
        description="仅将失败条目重新入队，不重跑已完成对象。",
    )
    async def retry_storage_migration_failed(
        request: Request,
        response: Response,
        migration_id: str,
    ) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.retry_failed_migration(migration_id)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.post(
        "/v1/admin/storage/migrations/{migration_id}/pause",
        tags=["core-admin"],
        summary="暂停存储迁移",
        description="暂停在途存储迁移任务，保留已迁移与未迁移对象现状。",
    )
    async def pause_storage_migration(
        request: Request,
        response: Response,
        migration_id: str,
    ) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.pause_migration(migration_id)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.post(
        "/v1/admin/storage/migrations/{migration_id}/resume",
        tags=["core-admin"],
        summary="继续存储迁移",
        description="恢复已暂停的存储迁移任务。",
    )
    async def resume_storage_migration(
        request: Request,
        response: Response,
        migration_id: str,
    ) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        try:
            return await manager.resume_migration(migration_id)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.post(
        "/v1/admin/storage/migrations/{migration_id}/import-unknown",
        tags=["core-admin"],
        summary="人工确认导入未登记对象并纳入迁移",
        description="将源存储对账发现的未知对象登记入库并加入迁移队列，供后台复制校验。",
    )
    async def import_storage_migration_unknown(
        request: Request,
        response: Response,
        migration_id: str,
        payload: StorageMigrationUnknownImportPayload | None = None,
    ) -> dict[str, Any]:
        await require_core_admin(request)
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        manager = _storage_manager(request)
        body = payload or StorageMigrationUnknownImportPayload()
        try:
            return await manager.import_unknown_migration_objects(
                migration_id,
                keys=body.keys,
                run_immediately=body.run_immediately,
            )
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.get(
        "/v1/storage/public/{object_id}",
        tags=["plugins"],
        summary="同源公开对象读取",
        description="读取已发布的公开插件包或头像对象，支持 Range 请求，禁止访问隔离区。",
    )
    async def serve_same_origin_public_object(request: Request, object_id: str) -> Response:
        manager = _storage_manager(request)
        record = await manager.resolve_public_object_by_id(object_id)
        if record is None:
            raise HTTPException(
                404, {"code": "object_not_found", "error": "Public object not found"}
            )
        try:
            return await _serve_public_object(request, manager, record)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    @router.head(
        "/v1/storage/public/{object_id}",
        include_in_schema=False,
    )
    async def head_same_origin_public_object(request: Request, object_id: str) -> Response:
        return await serve_same_origin_public_object(request, object_id)

    @router.api_route(
        "/packages/{alias_key:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def serve_legacy_packages_alias(request: Request, alias_key: str) -> Response:
        if ".." in alias_key or alias_key.startswith("/") or "\\" in alias_key:
            raise HTTPException(404, {"code": "object_not_found", "error": "Package not found"})
        manager = _storage_manager(request)
        record = await manager.resolve_public_alias(f"/packages/{alias_key}")
        if record is None:
            raise HTTPException(404, {"code": "object_not_found", "error": "Package not found"})
        try:
            return await _serve_public_object(request, manager, record)
        except ObjectStorageError as exc:
            raise _http_error_for_storage(exc) from exc

    return router
