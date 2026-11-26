"""Storage profile lifecycle, connection tests, purpose bindings, and migration execution."""

from __future__ import annotations

from dataclasses import replace
import asyncio
import hashlib
import logging
import os
import secrets
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from .credentials import decrypt_storage_secret, encrypt_storage_secret
from .driver import ObjectStorageDriver
from .imgbed_driver import (
    ImgBedObjectStorageDriver,
    validate_imgbed_base_url,
    validate_imgbed_folder,
    validate_imgbed_scopes,
)
from .local_fs import LocalFilesystemDriver, validate_local_filesystem_root
from .models import (
    PURPOSE_ALLOWED_BACKENDS,
    ObjectStorageError,
    StorageBackend,
    StorageBinding,
    StorageMigration,
    StorageMigrationItem,
    StorageMigrationItemStatus,
    StorageMigrationStatus,
    StorageObjectRecord,
    StorageObjectStatus,
    StorageProfile,
    StorageProfileStatus,
    StoragePurpose,
    StorageUrlAlias,
    utc_now_iso,
    validate_object_key,
    validate_profile_id,
)
from .pg_driver import PostgresObjectStorageDriver, validate_pg_schema
from .repository import ObjectStorageRepository, create_object_storage_repository
from .s3 import S3ObjectStorageDriver
from .service import (
    LEGACY_PUBLISHED_PROFILE_ID,
    LEGACY_QUARANTINE_PROFILE_ID,
    ObjectStorageService,
)
from .sqlite_driver import SqliteObjectStorageDriver, validate_sqlite_db_path
from .webdav_driver import (
    WebDavObjectStorageDriver,
    validate_webdav_auth_type,
    validate_webdav_base_url,
    validate_webdav_root_path,
)

LOGGER = logging.getLogger("astrbot_market.object_storage")
HEALTHCHECK_PREFIX = "__healthcheck__/"
PUBLIC_PURPOSES = frozenset({StoragePurpose.ARTIFACT_PUBLISHED, StoragePurpose.AVATAR})
REPO_ROOT = Path(__file__).resolve().parents[4]
PENDING_OBJECT_TTL_SECONDS = 300
_STALE_MIGRATION_SENTINEL = "__migration_stale__"
_UNKNOWN_OBJECTS_HOLD_ERROR = "存在未登记对象，需人工确认导入或清理后方可完成迁移并下线源配置"
ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        StorageMigrationStatus.QUEUED,
        StorageMigrationStatus.RUNNING,
        StorageMigrationStatus.PAUSED,
        StorageMigrationStatus.COMPLETED_WITH_ERRORS,
        StorageMigrationStatus.FAILED,
    }
)


class StorageManager:
    """Manages persistent storage profiles, bindings, health tests, and migrations."""

    def __init__(
        self,
        *,
        store: Any,
        settings: Settings,
        repository: ObjectStorageRepository | None = None,
        service: ObjectStorageService | None = None,
        s3_client_factory: Any | None = None,
        webdav_client_factory: Any | None = None,
        imgbed_client_factory: Any | None = None,
        pg_pool_override: Any | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.repository = repository or create_object_storage_repository(store)
        self.service = service or ObjectStorageService(repository=self.repository)
        self.service.repository = self.repository
        self._s3_client_factory = s3_client_factory
        self._webdav_client_factory = webdav_client_factory
        self._imgbed_client_factory = imgbed_client_factory
        self._pg_pool_override = pg_pool_override
        self._initialized = False
        self._migration_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def rebind_store(self, store: Any) -> None:
        self.store = store
        self.repository = create_object_storage_repository(store)
        self.service.reset()
        self.service.repository = self.repository
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._refresh_lock:
            if self._initialized:
                return
            await self._sync_service_from_repository()
            await self._ensure_legacy_artifact_profiles()
            await self._sync_service_from_repository()
            await self.recover_stale_pending_objects()
            await self._recover_running_migrations()
            await self._sync_service_from_repository()
            self._initialized = True

    async def refresh(self) -> None:
        if not self._initialized:
            await self.initialize()
            return
        async with self._refresh_lock:
            await self._sync_service_from_repository()

    async def _sync_service_from_repository(self) -> None:
        profiles = await self.repository.list_profiles(include_removed=True)
        active_profile_ids: set[str] = set()
        for profile in profiles:
            if profile.status in {StorageProfileStatus.RETIRED, StorageProfileStatus.REMOVED}:
                if self.service.get_profile(profile.id) is not None:
                    await self._close_and_unregister_driver(profile.id)
                continue
            active_profile_ids.add(profile.id)
            existing = self.service.get_profile(profile.id)
            try:
                current_driver = self.service.driver_for_profile(profile.id)
            except ObjectStorageError:
                current_driver = None
            if existing == profile and current_driver is not None:
                continue
            if current_driver is not None:
                await self._close_and_unregister_driver(profile.id)
            try:
                driver = await self._build_driver(profile)
            except Exception as exc:
                LOGGER.warning("Skipping driver activation for profile %s: %s", profile.id, exc)
                continue
            self.service.register_profile(profile, driver)

        for profile in list(self.service.list_profiles()):
            if profile.id not in active_profile_ids:
                await self._close_and_unregister_driver(profile.id)

        self.service.replace_bindings(await self.repository.list_bindings())
        self.service.replace_object_records(await self.repository.list_objects())

    async def _recover_running_migrations(self) -> None:
        migrations = await self.repository.list_migrations(
            status=StorageMigrationStatus.RUNNING,
            limit=500,
        )
        for migration in migrations:
            now = utc_now_iso()
            running_items = await self.repository.list_migration_items(
                migration.id,
                status=StorageMigrationItemStatus.RUNNING,
            )
            for item in running_items:
                await self.repository.update_migration_item(
                    replace(item, status=StorageMigrationItemStatus.PENDING, updated_at=now)
                )
            await self.repository.update_migration(
                replace(
                    migration,
                    status=StorageMigrationStatus.QUEUED,
                    last_error="服务重启后恢复迁移",
                    updated_at=now,
                    completed_at="",
                )
            )

    async def _reconcile_unknown_objects(
        self,
        migration: StorageMigration,
    ) -> StorageMigration:
        if not migration.unknown_objects:
            return migration
        try:
            source_driver = self.service.driver_for_profile(migration.source_profile_id)
        except ObjectStorageError:
            return migration
        remaining: list[Mapping[str, Any]] = []
        changed = False
        for entry in migration.unknown_objects:
            namespace = str(entry.get("namespace") or "")
            key = str(entry.get("key") or "")
            try:
                stat = await source_driver.stat_object(key, namespace=namespace)
            except Exception as exc:
                LOGGER.warning(
                    "Unknown object reconciliation failed for %s (%s/%s): %s",
                    migration.id,
                    namespace,
                    key,
                    exc,
                )
                remaining.append(dict(entry))
                continue
            if stat is None:
                changed = True
                continue
            refreshed = dict(entry)
            refreshed["size_bytes"] = stat.size_bytes
            remaining.append(refreshed)
            if refreshed != dict(entry):
                changed = True
        if not changed:
            return migration
        updated = replace(
            migration,
            unknown_objects=tuple(remaining),
            last_error="" if not remaining else migration.last_error,
            updated_at=utc_now_iso(),
        )
        return await self.repository.update_migration(updated)

    async def close(self) -> None:
        for profile in list(self.service.list_profiles()):
            try:
                driver = self.service.driver_for_profile(profile.id)
            except ObjectStorageError:
                continue
            close_fn = getattr(driver, "close", None)
            if callable(close_fn):
                result = close_fn()
                if asyncio.iscoroutine(result):
                    await result

    async def list_profiles(self, *, include_removed: bool = True) -> list[dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        profiles = await self.repository.list_profiles(include_removed=include_removed)
        bindings = await self.repository.list_bindings()
        migrations = await self.repository.list_migrations(limit=200)
        bound_purposes_by_profile: dict[str, list[str]] = {}
        for binding in bindings:
            bound_purposes_by_profile.setdefault(binding.profile_id, []).append(
                binding.purpose.value
            )
        items: list[dict[str, Any]] = []
        for profile in profiles:
            active_count = await self.repository.count_objects(
                profile_id=profile.id,
                status=StorageObjectStatus.ACTIVE,
            )
            pending_count = await self.repository.count_objects(
                profile_id=profile.id,
                status=StorageObjectStatus.PENDING,
            )
            in_flight = any(
                (m.source_profile_id == profile.id or m.target_profile_id == profile.id)
                and m.status in ACTIVE_MIGRATION_STATUSES
                for m in migrations
            )
            capabilities: dict[str, Any] = {}
            if profile.status not in {StorageProfileStatus.RETIRED, StorageProfileStatus.REMOVED}:
                try:
                    caps = self.service.capabilities_for_profile(profile.id)
                    capabilities = {
                        "conditional_create": caps.conditional_create,
                        "range_read": caps.range_read,
                        "public_read": caps.public_read,
                        "image_variants": caps.image_variants,
                        "list_objects": caps.list_objects,
                    }
                except ObjectStorageError:
                    capabilities = {}
            data = profile.to_public_dict()
            data["bound_purposes"] = sorted(bound_purposes_by_profile.get(profile.id, []))
            data["active_object_count"] = active_count
            data["pending_object_count"] = pending_count
            data["has_active_migrations"] = in_flight
            data["capabilities"] = capabilities
            items.append(data)
        return items

    async def create_profile(
        self,
        *,
        profile_id: str,
        name: str,
        backend: str,
        config: Mapping[str, Any],
        secret: Mapping[str, Any] | None = None,
        actor_id: str = "",
    ) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        normalized_id = validate_profile_id(profile_id)
        if await self.repository.get_profile(normalized_id) is not None:
            raise ObjectStorageError(
                "storage_profile_exists", f"Storage profile {normalized_id} already exists"
            )
        cleaned_name = str(name or "").strip()
        if not cleaned_name:
            raise ObjectStorageError("storage_profile_name_missing", "Profile name is required")
        try:
            backend_enum = StorageBackend(str(backend or "").strip().lower())
        except ValueError as exc:
            raise ObjectStorageError(
                "storage_backend_invalid", "Unsupported storage backend"
            ) from exc

        sanitized_config, cleaned_secret = self._validate_profile_inputs(
            backend=backend_enum,
            config=config,
            secret=secret or {},
            existing_secret_ref="",
        )
        secret_ref = ""
        if cleaned_secret:
            secret_ref = f"storage_secret_{secrets.token_hex(16)}"
            ciphertext = encrypt_storage_secret(
                self.settings.oauth_credential_key,
                profile_id=normalized_id,
                secret_payload=cleaned_secret,
            )
            await self.repository.save_secret(
                secret_ref,
                profile_id=normalized_id,
                ciphertext=ciphertext,
            )
        now = utc_now_iso()
        profile = StorageProfile(
            id=normalized_id,
            name=cleaned_name,
            backend=backend_enum,
            status=StorageProfileStatus.ACTIVE,
            config=sanitized_config,
            secret_ref=secret_ref,
            legacy=False,
            created_by=actor_id,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        try:
            driver = await self._build_driver(profile)
        except Exception:
            if secret_ref:
                await self.repository.delete_secret(secret_ref)
            raise
        saved = await self.repository.upsert_profile(profile)
        self.service.register_profile(saved, driver)
        return (await self._profile_view(saved.id)) or saved.to_public_dict()

    async def update_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        config: Mapping[str, Any] | None = None,
        secret: Mapping[str, Any] | None = None,
        actor_id: str = "",
    ) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        existing = await self.repository.get_profile(validate_profile_id(profile_id))
        if existing is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        if existing.status in {StorageProfileStatus.RETIRED, StorageProfileStatus.REMOVED}:
            raise ObjectStorageError(
                "storage_profile_immutable",
                "Retired or removed storage profiles cannot be edited",
            )
        merged_config = {**dict(existing.config), **dict(config or {})}
        sanitized_config, cleaned_secret = self._validate_profile_inputs(
            backend=existing.backend,
            config=merged_config,
            secret=secret or {},
            existing_secret_ref=existing.secret_ref,
        )
        if existing.backend == StorageBackend.IMGBED:
            bindings = await self.repository.list_bindings()
            published_bound = any(
                binding.purpose == StoragePurpose.ARTIFACT_PUBLISHED
                and binding.profile_id == existing.id
                for binding in bindings
            )
            if published_bound:
                scopes = set(sanitized_config.get("scopes") or ())
                if not {"list", "delete"}.issubset(scopes):
                    raise ObjectStorageError(
                        "imgbed_scope_missing",
                        "ImgBed Profile bound to published artifacts must retain list and delete scopes",
                    )

        secret_ref = existing.secret_ref
        merged_secret: dict[str, Any] | None = None
        previous_ciphertext: tuple[str, str] | None = None
        if cleaned_secret:
            previous_secret: dict[str, Any] = {}
            if secret_ref:
                previous_secret = await self._load_secret(existing)
                previous_ciphertext = await self.repository.get_secret(secret_ref)
            merged_secret = {**previous_secret, **cleaned_secret}
            secret_ref = secret_ref or f"storage_secret_{secrets.token_hex(16)}"

        config_changed = sanitized_config != dict(existing.config) or bool(cleaned_secret)
        updated = StorageProfile(
            id=existing.id,
            name=str(name).strip() if name is not None and str(name).strip() else existing.name,
            backend=existing.backend,
            status=existing.status,
            config=sanitized_config,
            secret_ref=secret_ref,
            legacy=existing.legacy,
            health_status="unknown" if config_changed else existing.health_status,
            health_message="" if config_changed else existing.health_message,
            last_checked_at="" if config_changed else existing.last_checked_at,
            created_by=existing.created_by,
            updated_by=actor_id,
            created_at=existing.created_at,
            updated_at=utc_now_iso(),
        )
        driver = await self._build_driver(updated, secret_override=merged_secret)
        try:
            if cleaned_secret:
                ciphertext = encrypt_storage_secret(
                    self.settings.oauth_credential_key,
                    profile_id=existing.id,
                    secret_payload=merged_secret or {},
                )
                await self.repository.save_secret(
                    secret_ref,
                    profile_id=existing.id,
                    ciphertext=ciphertext,
                )
            saved = await self.repository.upsert_profile(updated)
        except Exception:
            close_fn = getattr(driver, "close", None)
            if callable(close_fn):
                close_result = close_fn()
                if asyncio.iscoroutine(close_result):
                    await close_result
            if cleaned_secret:
                try:
                    if previous_ciphertext is not None and existing.secret_ref:
                        await self.repository.save_secret(
                            existing.secret_ref,
                            profile_id=previous_ciphertext[0],
                            ciphertext=previous_ciphertext[1],
                        )
                        if secret_ref != existing.secret_ref:
                            await self.repository.delete_secret(secret_ref)
                    elif secret_ref:
                        await self.repository.delete_secret(secret_ref)
                except Exception as rollback_exc:
                    LOGGER.error(
                        "Failed to restore storage secret for profile %s after update failure: %s",
                        existing.id,
                        rollback_exc,
                    )
            raise

        try:
            old_driver = self.service.driver_for_profile(existing.id)
        except ObjectStorageError:
            old_driver = None
        self.service.register_profile(saved, driver)
        if old_driver is not None and old_driver is not driver:
            close_fn = getattr(old_driver, "close", None)
            if callable(close_fn):
                try:
                    close_result = close_fn()
                    if asyncio.iscoroutine(close_result):
                        await close_result
                except Exception as exc:
                    LOGGER.warning(
                        "Failed to close replaced storage driver %s: %s", existing.id, exc
                    )
        return (await self._profile_view(saved.id)) or saved.to_public_dict()

    async def test_profile(self, profile_id: str) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        profile = await self.repository.get_profile(validate_profile_id(profile_id))
        if profile is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        if profile.status in {StorageProfileStatus.RETIRED, StorageProfileStatus.REMOVED}:
            raise ObjectStorageError(
                "storage_profile_not_active", "Retired or removed profile cannot be tested"
            )
        driver = self.service.driver_for_profile(profile.id)
        updated_config = dict(profile.config)
        if isinstance(driver, ImgBedObjectStorageDriver):
            probe = await driver.probe_capabilities(probe_prefix=HEALTHCHECK_PREFIX)
            checks = {
                "reachable": probe.reachable,
                "write": probe.upload,
                "read": probe.read,
                "image_variants": probe.image_variants,
                "delete": probe.delete,
                "write_probe_skipped_no_delete_scope": probe.write_probe_skipped_no_delete_scope,
                "range": probe.range_read,
                "plugin_zip_ready": probe.plugin_zip_ready,
                "public_url": True,
            }
            base_ok = (
                probe.reachable and probe.image_variants
                if probe.write_probe_skipped_no_delete_scope
                else (probe.upload and probe.read and probe.image_variants and probe.delete)
            )
            health_status = "healthy" if base_ok else "unhealthy"
            health_message = probe.message
            updated_config["range_read_supported"] = probe.range_read
            updated_config["plugin_zip_verified"] = probe.plugin_zip_ready
        elif isinstance(driver, WebDavObjectStorageDriver):
            probe = await driver.probe_capabilities(probe_prefix=HEALTHCHECK_PREFIX)
            checks = {
                "write": probe.write,
                "read": probe.read,
                "range": probe.range_read,
                "conditional_create": probe.conditional_create,
                "overwrite_protection": probe.overwrite_protection,
                "delete": probe.delete,
                "public_url": probe.public_url,
            }
            all_required_ok = all(
                checks[key]
                for key in (
                    "write",
                    "read",
                    "range",
                    "overwrite_protection",
                    "delete",
                    "public_url",
                )
            )
            health_status = "healthy" if all_required_ok else "unhealthy"
            health_message = probe.message
            updated_config["conditional_create_supported"] = probe.conditional_create
            updated_config["range_read_supported"] = probe.range_read
        else:
            probe_key = f"{HEALTHCHECK_PREFIX}probe-{secrets.token_hex(8)}.txt"
            probe_bytes = b"astrhub-storage-health-check-0123456789"
            probe_sha = hashlib.sha256(probe_bytes).hexdigest()
            checks = {
                "write": False,
                "read": False,
                "range": False,
                "delete": False,
                "public_url": False,
            }
            health_status = "healthy"
            health_message = "连接与读写校验通过"
            try:
                stored = await driver.put_object(
                    probe_key,
                    probe_bytes,
                    content_type="text/plain; charset=utf-8",
                    expected_sha256=probe_sha,
                    if_absent=True,
                )
                checks["write"] = stored.sha256 == probe_sha and stored.size_bytes == len(
                    probe_bytes
                )
                read_back = await driver.read_object(probe_key, expected_sha256=probe_sha)
                checks["read"] = read_back == probe_bytes
                ranged = await driver.read_range(
                    probe_key,
                    start_byte=8,
                    max_bytes=7,
                    max_object_bytes=len(probe_bytes),
                    expected_size_bytes=len(probe_bytes),
                    expected_sha256=probe_sha,
                )
                checks["range"] = ranged.content == b"storage" and ranged.truncated is True
                if driver.capabilities().public_read:
                    url = driver.public_url(probe_key)
                    checks["public_url"] = bool(url.startswith(("http://", "https://", "/")))
                else:
                    checks["public_url"] = True
            except Exception as exc:
                health_status = "unhealthy"
                health_message = str(exc) or exc.__class__.__name__
            finally:
                try:
                    await driver.delete_object(probe_key)
                    checks["delete"] = await driver.stat_object(probe_key) is None
                except Exception as cleanup_exc:
                    checks["delete"] = False
                    if health_status == "healthy":
                        health_status = "unhealthy"
                        health_message = f"测试对象清理失败：{cleanup_exc}"

            if not all(checks.values()):
                health_status = "unhealthy"
                if health_message == "连接与读写校验通过":
                    health_message = "部分存储能力校验未通过"

        checked_at = utc_now_iso()
        updated = StorageProfile(
            id=profile.id,
            name=profile.name,
            backend=profile.backend,
            status=profile.status,
            config=updated_config,
            secret_ref=profile.secret_ref,
            legacy=profile.legacy,
            health_status=health_status,
            health_message=health_message,
            last_checked_at=checked_at,
            created_by=profile.created_by,
            updated_by=profile.updated_by,
            created_at=profile.created_at,
            updated_at=checked_at,
        )
        await self.repository.upsert_profile(updated)
        self.service.register_profile(updated, driver)
        return {
            "ok": health_status == "healthy",
            "profile_id": profile.id,
            "health_status": health_status,
            "health_message": health_message,
            "checked_at": checked_at,
            "checks": checks,
        }

    async def list_bindings(self) -> list[dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        bindings = {b.purpose: b for b in await self.repository.list_bindings()}
        result: list[dict[str, Any]] = []
        for purpose in StoragePurpose:
            binding = bindings.get(purpose)
            result.append(
                {
                    "purpose": purpose.value,
                    "profile_id": binding.profile_id if binding else "",
                    "generation": binding.generation if binding else 0,
                    "updated_by": binding.updated_by if binding else "",
                    "updated_at": binding.updated_at if binding else "",
                }
            )
        return result

    async def bind_purpose(
        self,
        purpose_raw: str,
        profile_id: str,
        *,
        actor_id: str = "",
        run_immediately: bool = False,
    ) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        try:
            purpose = StoragePurpose(str(purpose_raw or "").strip())
        except ValueError as exc:
            raise ObjectStorageError(
                "invalid_storage_purpose", f"Invalid purpose: {purpose_raw}"
            ) from exc

        target = await self.repository.get_profile(validate_profile_id(profile_id))
        if target is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        if target.status != StorageProfileStatus.ACTIVE:
            raise ObjectStorageError(
                "storage_profile_not_active",
                f"Storage profile {target.id} is not active",
            )
        if target.backend not in PURPOSE_ALLOWED_BACKENDS.get(purpose, frozenset()):
            raise ObjectStorageError(
                "storage_backend_disallowed_for_purpose",
                f"Backend {target.backend.value} cannot be bound to {purpose.value}",
            )
        if target.backend == StorageBackend.WEBDAV and target.health_status != "healthy":
            probe = await self.test_profile(target.id)
            if not probe.get("ok"):
                raise ObjectStorageError(
                    "webdav_capability_check_failed",
                    str(probe.get("health_message") or "WebDAV capability probe failed"),
                )
        if target.backend == StorageBackend.IMGBED:
            if purpose == StoragePurpose.ARTIFACT_PUBLISHED:
                driver = self.service.driver_for_profile(target.id)
                assert isinstance(driver, ImgBedObjectStorageDriver)
                if not (
                    target.health_status == "healthy"
                    and bool(target.config.get("plugin_zip_verified", False))
                ):
                    zip_probe = await driver.probe_capabilities(
                        probe_prefix=HEALTHCHECK_PREFIX,
                        test_plugin_zip=True,
                    )
                    checked_at = utc_now_iso()
                    updated_cfg = {
                        **dict(target.config),
                        "range_read_supported": zip_probe.range_read,
                        "plugin_zip_verified": zip_probe.plugin_zip_ready,
                    }
                    base_ok = (
                        zip_probe.reachable and zip_probe.image_variants
                        if zip_probe.write_probe_skipped_no_delete_scope
                        else (
                            zip_probe.upload
                            and zip_probe.read
                            and zip_probe.image_variants
                            and zip_probe.delete
                        )
                    )
                    updated_target = StorageProfile(
                        id=target.id,
                        name=target.name,
                        backend=target.backend,
                        status=target.status,
                        config=updated_cfg,
                        secret_ref=target.secret_ref,
                        legacy=target.legacy,
                        health_status="healthy" if base_ok else "unhealthy",
                        health_message=zip_probe.message,
                        last_checked_at=checked_at,
                        created_by=target.created_by,
                        updated_by=target.updated_by,
                        created_at=target.created_at,
                        updated_at=checked_at,
                    )
                    await self.repository.upsert_profile(updated_target)
                    self.service.register_profile(updated_target, driver)
                    if not zip_probe.plugin_zip_ready:
                        raise ObjectStorageError(
                            "imgbed_plugin_gate_failed",
                            zip_probe.message,
                        )
            elif purpose == StoragePurpose.AVATAR and target.health_status != "healthy":
                probe = await self.test_profile(target.id)
                if not probe.get("ok"):
                    raise ObjectStorageError(
                        "imgbed_capability_check_failed",
                        str(probe.get("health_message") or "ImgBed capability probe failed"),
                    )

        previous = await self.repository.get_binding(purpose)
        generation = (previous.generation + 1) if previous is not None else 1
        pending_binding = StorageBinding(
            purpose=purpose,
            profile_id=target.id,
            generation=generation,
            updated_by=actor_id,
            updated_at=utc_now_iso(),
        )

        draining_profile: StorageProfile | None = None
        planned_migration: StorageMigration | None = None
        planned_items: list[StorageMigrationItem] = []
        if previous is not None and previous.profile_id != target.id:
            (
                draining_profile,
                planned_migration,
                planned_items,
            ) = await self._prepare_migration_plan(
                purpose=purpose,
                source_profile_id=previous.profile_id,
                target_profile_id=target.id,
                binding_generation=pending_binding.generation,
                actor_id=actor_id,
            )

        binding, migration = await self.repository.commit_rebind(
            binding=pending_binding,
            draining_profile=draining_profile,
            migration=planned_migration,
            items=planned_items,
        )
        self.service.set_binding(binding)
        if draining_profile is not None:
            try:
                src_driver = self.service.driver_for_profile(draining_profile.id)
                self.service.register_profile(draining_profile, src_driver)
            except ObjectStorageError:
                pass
        if migration is not None and run_immediately:
            migration = await self.run_migration(migration.id)

        return {
            "binding": {
                "purpose": binding.purpose.value,
                "profile_id": binding.profile_id,
                "generation": binding.generation,
                "updated_by": binding.updated_by,
                "updated_at": binding.updated_at,
            },
            "migration": migration.to_dict() if migration is not None else None,
        }

    async def list_migrations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        migrations = await self.repository.list_migrations(limit=limit)
        return [item.to_dict() for item in migrations]

    async def get_migration_detail(self, migration_id: str) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        migration = await self.repository.get_migration(migration_id)
        if migration is None:
            raise ObjectStorageError(
                "storage_migration_not_found", f"Unknown migration: {migration_id}"
            )
        items = await self.repository.list_migration_items(migration_id)
        return {
            **migration.to_dict(),
            "items": [item.to_dict() for item in items],
        }

    async def pause_migration(self, migration_id: str) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        migration = await self.repository.get_migration(migration_id)
        if migration is None:
            raise ObjectStorageError(
                "storage_migration_not_found", f"Unknown migration: {migration_id}"
            )
        if migration.status in {StorageMigrationStatus.COMPLETED, StorageMigrationStatus.CANCELLED}:
            raise ObjectStorageError(
                "storage_migration_already_finished", "Completed migration cannot be paused"
            )
        updated = StorageMigration(
            id=migration.id,
            purpose=migration.purpose,
            source_profile_id=migration.source_profile_id,
            target_profile_id=migration.target_profile_id,
            binding_generation=migration.binding_generation,
            status=StorageMigrationStatus.PAUSED,
            total_count=migration.total_count,
            completed_count=migration.completed_count,
            failed_count=migration.failed_count,
            skipped_count=migration.skipped_count,
            unknown_objects=migration.unknown_objects,
            last_error=migration.last_error,
            created_by=migration.created_by,
            created_at=migration.created_at,
            updated_at=utc_now_iso(),
            completed_at="",
        )
        saved = await self.repository.update_migration(updated)
        return saved.to_dict()

    async def resume_migration(
        self,
        migration_id: str,
        *,
        run_immediately: bool = False,
    ) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        migration = await self.repository.get_migration(migration_id)
        if migration is None:
            raise ObjectStorageError(
                "storage_migration_not_found", f"Unknown migration: {migration_id}"
            )
        if migration.status != StorageMigrationStatus.PAUSED:
            raise ObjectStorageError(
                "storage_migration_not_paused", "Only paused migrations can be resumed"
            )
        updated = StorageMigration(
            id=migration.id,
            purpose=migration.purpose,
            source_profile_id=migration.source_profile_id,
            target_profile_id=migration.target_profile_id,
            binding_generation=migration.binding_generation,
            status=StorageMigrationStatus.QUEUED,
            total_count=migration.total_count,
            completed_count=migration.completed_count,
            failed_count=migration.failed_count,
            skipped_count=migration.skipped_count,
            unknown_objects=migration.unknown_objects,
            last_error="",
            created_by=migration.created_by,
            created_at=migration.created_at,
            updated_at=utc_now_iso(),
            completed_at="",
        )
        saved = await self.repository.update_migration(updated)
        if run_immediately:
            saved = await self.run_migration(saved.id)
        return saved.to_dict()

    async def retry_failed_migration(
        self,
        migration_id: str,
        *,
        run_immediately: bool = False,
    ) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        migration = await self.repository.get_migration(migration_id)
        if migration is None:
            raise ObjectStorageError(
                "storage_migration_not_found", f"Unknown migration: {migration_id}"
            )
        reset_count = await self.repository.reset_failed_migration_items(migration_id)
        if reset_count == 0:
            raise ObjectStorageError(
                "storage_migration_no_failed_items", "No failed migration items to retry"
            )
        all_items = await self.repository.list_migration_items(migration_id)
        updated = _recompute_migration_counts(
            migration,
            all_items,
            status=StorageMigrationStatus.QUEUED,
            last_error="",
        )
        saved = await self.repository.update_migration(updated)
        if run_immediately:
            saved = await self.run_migration(saved.id)
        return saved.to_dict()

    async def process_queued_migrations(self, *, batch_size: int = 20) -> int:
        if not self._initialized:
            await self.initialize()
        queued = await self.repository.list_migrations(
            status=StorageMigrationStatus.QUEUED,
            limit=10,
        )
        processed = 0
        for migration in queued:
            await self.run_migration(migration.id, batch_size=batch_size)
            processed += 1
        return processed

    async def run_migration(
        self,
        migration_id: str,
        *,
        batch_size: int = 100,
    ) -> StorageMigration:
        if not self._initialized:
            await self.initialize()
        async with self._migration_lock:
            migration = await self.repository.get_migration(migration_id)
            if migration is None:
                raise ObjectStorageError(
                    "storage_migration_not_found", f"Unknown migration: {migration_id}"
                )
            if migration.status in {
                StorageMigrationStatus.PAUSED,
                StorageMigrationStatus.COMPLETED,
                StorageMigrationStatus.CANCELLED,
            }:
                return migration

            if not await self._is_migration_binding_current(migration):
                return await self._cancel_stale_migration(
                    migration,
                    "用途绑定已变更，旧代次迁移任务已自动取消",
                )

            running = StorageMigration(
                id=migration.id,
                purpose=migration.purpose,
                source_profile_id=migration.source_profile_id,
                target_profile_id=migration.target_profile_id,
                binding_generation=migration.binding_generation,
                status=StorageMigrationStatus.RUNNING,
                total_count=migration.total_count,
                completed_count=migration.completed_count,
                failed_count=migration.failed_count,
                skipped_count=migration.skipped_count,
                unknown_objects=migration.unknown_objects,
                last_error="",
                created_by=migration.created_by,
                created_at=migration.created_at,
                updated_at=utc_now_iso(),
                completed_at="",
            )
            migration = await self.repository.update_migration(running)

            pending_items = await self.repository.list_migration_items(
                migration_id,
                status=StorageMigrationItemStatus.PENDING,
            )
            processed_in_batch = 0
            last_error = ""
            for item in pending_items:
                latest = await self.repository.get_migration(migration_id)
                if latest is None or latest.status == StorageMigrationStatus.PAUSED:
                    return latest or migration
                if not await self._is_migration_binding_current(latest):
                    return await self._cancel_stale_migration(
                        latest,
                        "用途绑定已变更，旧代次迁移任务已自动取消",
                    )
                if processed_in_batch >= batch_size:
                    break
                processed_in_batch += 1
                await self.repository.update_migration_item(
                    replace(
                        item,
                        status=StorageMigrationItemStatus.RUNNING,
                        updated_at=utc_now_iso(),
                    )
                )
                item_error = await self._migrate_single_item(migration, item)
                if item_error == _STALE_MIGRATION_SENTINEL:
                    return await self._cancel_stale_migration(
                        migration,
                        "用途绑定在对象提交前已变更，旧代次迁移任务已自动取消",
                    )
                if item_error:
                    last_error = item_error

            latest_before_finish = await self.repository.get_migration(migration_id) or migration
            latest_before_finish = await self._reconcile_unknown_objects(latest_before_finish)
            if not await self._is_migration_binding_current(latest_before_finish):
                return await self._cancel_stale_migration(
                    latest_before_finish,
                    "用途绑定已变更，旧代次迁移任务已自动取消",
                )

            all_items = await self.repository.list_migration_items(migration_id)
            remaining_pending = sum(
                1 for entry in all_items if entry.status == StorageMigrationItemStatus.PENDING
            )
            failed_total = sum(
                1 for entry in all_items if entry.status == StorageMigrationItemStatus.FAILED
            )
            if remaining_pending > 0:
                final_status = StorageMigrationStatus.QUEUED
            elif failed_total > 0:
                final_status = StorageMigrationStatus.COMPLETED_WITH_ERRORS
            elif latest_before_finish.unknown_objects:
                final_status = StorageMigrationStatus.COMPLETED_WITH_ERRORS
                last_error = last_error or _UNKNOWN_OBJECTS_HOLD_ERROR
            else:
                final_status = StorageMigrationStatus.COMPLETED

            updated = _recompute_migration_counts(
                latest_before_finish,
                all_items,
                status=final_status,
                last_error=last_error,
            )
            saved = await self.repository.update_migration(updated)
            if final_status == StorageMigrationStatus.COMPLETED:
                await self._restore_draining_profile_if_ready(saved)
            return saved

    async def import_unknown_migration_objects(
        self,
        migration_id: str,
        *,
        keys: list[dict[str, str]] | None = None,
        run_immediately: bool = False,
    ) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        migration = await self.repository.get_migration(migration_id)
        if migration is None:
            raise ObjectStorageError(
                "storage_migration_not_found", f"Unknown migration: {migration_id}"
            )
        if migration.status == StorageMigrationStatus.CANCELLED or not (
            await self._is_migration_binding_current(migration)
        ):
            raise ObjectStorageError(
                "storage_migration_stale",
                "Stale or cancelled migration cannot import unknown objects",
            )
        migration = await self._reconcile_unknown_objects(migration)
        if not migration.unknown_objects:
            all_items = await self.repository.list_migration_items(migration.id)
            if not any(
                item.status
                in {StorageMigrationItemStatus.PENDING, StorageMigrationItemStatus.FAILED}
                for item in all_items
            ):
                completed = _recompute_migration_counts(
                    migration,
                    all_items,
                    status=StorageMigrationStatus.COMPLETED,
                    last_error="",
                )
                saved = await self.repository.update_migration(completed)
                await self._restore_draining_profile_if_ready(saved)
                return saved.to_dict()
            raise ObjectStorageError(
                "storage_migration_no_unknown_objects",
                "Migration has no pending unknown objects to import",
            )

        requested_set: set[tuple[str, str]] | None = None
        if keys is not None:
            requested_set = {
                (str(entry.get("namespace") or "").strip("/"), str(entry.get("key") or "").strip())
                for entry in keys
                if str(entry.get("key") or "").strip()
            }

        source_driver = self.service.driver_for_profile(migration.source_profile_id)
        source_profile = await self.repository.get_profile(migration.source_profile_id)
        source_backend = (
            source_profile.backend.value
            if source_profile is not None
            else source_driver.backend.value
        )

        selected: list[dict[str, Any]] = []
        remaining_unknown: list[dict[str, Any]] = []
        for entry in migration.unknown_objects:
            locator = (str(entry.get("namespace") or ""), str(entry.get("key") or ""))
            if requested_set is None or locator in requested_set:
                selected.append(dict(entry))
            else:
                remaining_unknown.append(dict(entry))

        if not selected:
            raise ObjectStorageError(
                "storage_migration_no_unknown_objects",
                "No matching unknown objects found for import",
            )

        now = utc_now_iso()
        new_items: list[StorageMigrationItem] = []
        for entry in selected:
            ns = str(entry.get("namespace") or "")
            key = str(entry.get("key") or "")
            existing = await self.repository.find_active_object(
                profile_id=migration.source_profile_id,
                namespace=ns,
                key=key,
            )
            if existing is None or existing.status != StorageObjectStatus.ACTIVE:
                size_bytes, sha256, content_type, public_url = await self._inspect_object_metadata(
                    driver=source_driver,
                    purpose=migration.purpose,
                    namespace=ns,
                    key=key,
                )
                obj_id = existing.id if existing is not None else f"stobj_{secrets.token_hex(12)}"
                if not public_url and migration.purpose in PUBLIC_PURPOSES:
                    public_url = f"/v1/storage/public/{obj_id}"
                existing = await self.repository.upsert_object(
                    StorageObjectRecord(
                        id=obj_id,
                        purpose=migration.purpose,
                        profile_id=migration.source_profile_id,
                        backend=source_backend,
                        namespace=ns,
                        key=key,
                        size_bytes=size_bytes,
                        sha256=sha256,
                        content_type=content_type,
                        status=StorageObjectStatus.ACTIVE,
                        public_url=public_url,
                        owner_type="unknown_import",
                        created_at=existing.created_at if existing is not None else now,
                        updated_at=now,
                    )
                )
            new_items.append(
                StorageMigrationItem(
                    id=f"stmitem_{secrets.token_hex(12)}",
                    migration_id=migration.id,
                    object_id=existing.id,
                    source_profile_id=existing.profile_id,
                    source_namespace=existing.namespace,
                    source_key=existing.key,
                    source_public_url=existing.public_url,
                    target_profile_id=migration.target_profile_id,
                    target_namespace=existing.namespace,
                    target_key=existing.key,
                    target_public_url="",
                    expected_sha256=existing.sha256,
                    status=StorageMigrationItemStatus.PENDING,
                    attempts=0,
                    last_error="",
                    created_at=now,
                    updated_at=now,
                )
            )

        existing_items = await self.repository.list_migration_items(migration.id)
        all_items = [*existing_items, *new_items]
        updated = StorageMigration(
            id=migration.id,
            purpose=migration.purpose,
            source_profile_id=migration.source_profile_id,
            target_profile_id=migration.target_profile_id,
            binding_generation=migration.binding_generation,
            status=StorageMigrationStatus.QUEUED,
            total_count=len(all_items),
            completed_count=sum(
                1 for item in all_items if item.status == StorageMigrationItemStatus.COMPLETED
            ),
            failed_count=sum(
                1 for item in all_items if item.status == StorageMigrationItemStatus.FAILED
            ),
            skipped_count=sum(
                1 for item in all_items if item.status == StorageMigrationItemStatus.SKIPPED
            ),
            unknown_objects=tuple(remaining_unknown),
            last_error="",
            created_by=migration.created_by,
            created_at=migration.created_at,
            updated_at=now,
            completed_at="",
        )
        saved = await self.repository.add_migration_items(updated, new_items)
        if run_immediately:
            saved = await self.run_migration(saved.id)
        return saved.to_dict()

    async def recover_stale_pending_objects(
        self,
        *,
        max_age_seconds: int = PENDING_OBJECT_TTL_SECONDS,
    ) -> dict[str, int]:
        """Recover or prune orphaned pending object records left behind by a mid-write crash."""
        pending_records = await self.repository.list_objects(status=StorageObjectStatus.PENDING)
        now_dt = datetime.now(UTC)
        activated = 0
        deleted = 0
        for record in pending_records:
            if max_age_seconds > 0 and record.updated_at:
                try:
                    updated_dt = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
                    if (now_dt - updated_dt).total_seconds() < max_age_seconds:
                        continue
                except ValueError:
                    pass
            try:
                driver = self.service.driver_for_profile(record.profile_id)
            except ObjectStorageError:
                await self.repository.delete_object_record(record.id)
                deleted += 1
                continue
            try:
                stat = await driver.stat_object(record.key, namespace=record.namespace)
            except Exception as exc:
                LOGGER.warning(
                    "Pending recovery stat failed for %s (%s): %s", record.id, record.key, exc
                )
                continue
            if stat is None:
                await self.repository.delete_object_record(record.id)
                deleted += 1
                continue
            size_bytes, sha256, content_type, public_url = await self._inspect_object_metadata(
                driver=driver,
                purpose=record.purpose,
                namespace=record.namespace,
                key=record.key,
                precomputed_stat=stat,
            )
            if not public_url and record.purpose in PUBLIC_PURPOSES:
                public_url = f"/v1/storage/public/{record.id}"
            await self.repository.upsert_object(
                StorageObjectRecord(
                    id=record.id,
                    purpose=record.purpose,
                    profile_id=record.profile_id,
                    backend=record.backend,
                    namespace=record.namespace,
                    key=record.key,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    content_type=content_type or record.content_type,
                    status=StorageObjectStatus.ACTIVE,
                    public_url=public_url,
                    owner_type=record.owner_type,
                    owner_id=record.owner_id,
                    created_at=record.created_at,
                    updated_at=utc_now_iso(),
                )
            )
            activated += 1
        return {"activated": activated, "deleted": deleted}

    async def retire_profile(self, profile_id: str, *, actor_id: str = "") -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        profile = await self.repository.get_profile(validate_profile_id(profile_id))
        if profile is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        if profile.status == StorageProfileStatus.REMOVED:
            raise ObjectStorageError(
                "storage_profile_removed", "Storage profile is already removed"
            )
        await self._enforce_decommission_gates(profile.id)

        retired = StorageProfile(
            id=profile.id,
            name=profile.name,
            backend=profile.backend,
            status=StorageProfileStatus.RETIRED,
            config=profile.config,
            secret_ref="",
            legacy=profile.legacy,
            health_status="retired",
            health_message="已退役并停止市场访问",
            last_checked_at=profile.last_checked_at,
            created_by=profile.created_by,
            updated_by=actor_id,
            created_at=profile.created_at,
            updated_at=utc_now_iso(),
        )
        await self.repository.decommission_profile(
            retired,
            secret_ref_to_delete=profile.secret_ref,
        )
        await self._close_and_unregister_driver(profile.id)
        return (await self._profile_view(retired.id)) or retired.to_public_dict()

    async def delete_profile(self, profile_id: str, *, actor_id: str = "") -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        profile = await self.repository.get_profile(validate_profile_id(profile_id))
        if profile is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        if profile.status == StorageProfileStatus.REMOVED:
            return (await self._profile_view(profile.id)) or profile.to_public_dict()
        await self._enforce_decommission_gates(profile.id)

        removed = StorageProfile(
            id=profile.id,
            name=profile.name,
            backend=profile.backend,
            status=StorageProfileStatus.REMOVED,
            config={},
            secret_ref="",
            legacy=profile.legacy,
            health_status="removed",
            health_message="配置与凭据已移除（保留审计记录）",
            last_checked_at=profile.last_checked_at,
            created_by=profile.created_by,
            updated_by=actor_id,
            created_at=profile.created_at,
            updated_at=utc_now_iso(),
        )
        await self.repository.decommission_profile(
            removed,
            secret_ref_to_delete=profile.secret_ref,
        )
        await self._close_and_unregister_driver(profile.id)
        return (await self._profile_view(removed.id)) or removed.to_public_dict()

    async def resolve_public_object_by_id(self, object_id: str) -> StorageObjectRecord | None:
        if not self._initialized:
            await self.initialize()
        record = await self.repository.get_object(object_id)
        if (
            record is not None
            and record.status == StorageObjectStatus.ACTIVE
            and record.purpose in PUBLIC_PURPOSES
        ):
            return record
        alias = await self.repository.get_url_alias(f"/v1/storage/public/{object_id}")
        if alias is None or alias.purpose not in PUBLIC_PURPOSES:
            return None
        target = await self.repository.get_object(alias.object_id)
        if (
            target is not None
            and target.status == StorageObjectStatus.ACTIVE
            and target.purpose in PUBLIC_PURPOSES
        ):
            return target
        return None

    async def resolve_public_alias(self, alias_path: str) -> StorageObjectRecord | None:
        if not self._initialized:
            await self.initialize()
        normalized = "/" + str(alias_path or "").lstrip("/")
        alias = await self.repository.get_url_alias(normalized)
        if alias is not None and alias.purpose in PUBLIC_PURPOSES:
            record = await self.repository.get_object(alias.object_id)
            if (
                record is not None
                and record.status == StorageObjectStatus.ACTIVE
                and record.purpose in PUBLIC_PURPOSES
            ):
                return record
        if normalized.startswith("/packages/"):
            key = normalized.removeprefix("/packages/")
            try:
                validate_object_key(key)
            except ObjectStorageError:
                return None
            return await self.repository.find_active_object_by_purpose_and_key(
                purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                key=key,
            )
        return None

    async def _restore_draining_profile_if_ready(
        self,
        migration: StorageMigration,
    ) -> None:
        profile = await self.repository.get_profile(migration.source_profile_id)
        if profile is None or profile.status != StorageProfileStatus.DRAINING:
            return
        try:
            driver = self.service.driver_for_profile(profile.id)
        except ObjectStorageError:
            return
        active_objects = await self.repository.list_objects(
            profile_id=profile.id,
            status=StorageObjectStatus.ACTIVE,
        )
        pending_objects = await self.repository.list_objects(
            profile_id=profile.id,
            status=StorageObjectStatus.PENDING,
        )
        if active_objects or pending_objects:
            return
        migrations = await self.repository.list_migrations(limit=500)
        for other in migrations:
            if other.id == migration.id or other.source_profile_id != profile.id:
                continue
            if other.status in ACTIVE_MIGRATION_STATUSES or other.unknown_objects:
                return
        if any(
            binding.profile_id == profile.id for binding in await self.repository.list_bindings()
        ):
            return
        restored = replace(
            profile,
            status=StorageProfileStatus.ACTIVE,
            updated_by="system",
            updated_at=utc_now_iso(),
        )
        await self.repository.upsert_profile(restored)
        self.service.register_profile(restored, driver)

    async def _enforce_decommission_gates(self, profile_id: str) -> None:
        bindings = await self.repository.list_bindings()
        if any(binding.profile_id == profile_id for binding in bindings):
            raise ObjectStorageError(
                "storage_profile_still_bound",
                "Storage profile still has an active purpose binding",
            )
        active_count = await self.repository.count_objects(
            profile_id=profile_id,
            status=StorageObjectStatus.ACTIVE,
        )
        pending_count = await self.repository.count_objects(
            profile_id=profile_id,
            status=StorageObjectStatus.PENDING,
        )
        if active_count or pending_count:
            raise ObjectStorageError(
                "storage_profile_has_unmigrated_objects",
                "Storage profile still owns unmigrated or pending objects",
            )
        migrations = await self.repository.list_migrations(limit=500)
        for migration in migrations:
            is_source = migration.source_profile_id == profile_id
            is_target = migration.target_profile_id == profile_id
            if not is_source and not is_target:
                continue
            if migration.status in ACTIVE_MIGRATION_STATUSES:
                raise ObjectStorageError(
                    "storage_profile_has_unfinished_migrations",
                    "Storage profile still has unfinished or failed migrations",
                )
            if is_source and migration.unknown_objects:
                raise ObjectStorageError(
                    "storage_profile_has_unknown_objects",
                    "Storage profile still has unreconciled unknown objects in migration reports",
                )

    async def _close_and_unregister_driver(self, profile_id: str) -> None:
        try:
            driver = self.service.driver_for_profile(profile_id)
        except ObjectStorageError:
            driver = None
        if driver is not None:
            close_fn = getattr(driver, "close", None)
            if callable(close_fn):
                result = close_fn()
                if asyncio.iscoroutine(result):
                    await result
        self.service.unregister_profile(profile_id)

    async def _is_migration_binding_current(self, migration: StorageMigration) -> bool:
        current_binding = await self.repository.get_binding(migration.purpose)
        if current_binding is None:
            return False
        return (
            current_binding.profile_id == migration.target_profile_id
            and current_binding.generation == migration.binding_generation
        )

    async def _cancel_stale_migration(
        self,
        migration: StorageMigration,
        reason: str,
    ) -> StorageMigration:
        all_items = await self.repository.list_migration_items(migration.id)
        updated = _recompute_migration_counts(
            migration,
            all_items,
            status=StorageMigrationStatus.CANCELLED,
            last_error=reason,
        )
        return await self.repository.update_migration(updated)

    async def _prepare_migration_plan(
        self,
        *,
        purpose: StoragePurpose,
        source_profile_id: str,
        target_profile_id: str,
        binding_generation: int,
        actor_id: str,
    ) -> tuple[StorageProfile | None, StorageMigration | None, list[StorageMigrationItem]]:
        all_purpose_active = await self.repository.list_objects(
            purpose=purpose,
            status=StorageObjectStatus.ACTIVE,
        )
        active_objects = [obj for obj in all_purpose_active if obj.profile_id != target_profile_id]
        all_profile_active = await self.repository.list_objects(
            profile_id=source_profile_id,
            status=StorageObjectStatus.ACTIVE,
        )
        known_locators = {(obj.namespace, obj.key) for obj in all_profile_active}
        known_full_keys = {
            f"{obj.namespace}/{obj.key}" if obj.namespace else obj.key for obj in all_profile_active
        }
        source_profile = await self.repository.get_profile(source_profile_id)
        unknown_objects: list[dict[str, Any]] = []
        seen_unknown_full_keys: set[str] = set()
        try:
            source_driver = self.service.driver_for_profile(source_profile_id)
            if (
                source_profile is not None
                and source_profile.legacy
                and source_profile.backend == StorageBackend.LOCAL
                and source_profile.id == LEGACY_QUARANTINE_PROFILE_ID
            ):
                namespaces = sorted(
                    {
                        obj.namespace
                        for obj in all_profile_active
                        if obj.purpose == purpose and obj.namespace
                    }
                    | {"quarantine", "content"}
                )
            else:
                namespaces = sorted(
                    {obj.namespace for obj in all_profile_active if obj.purpose == purpose} | {""}
                )
            for ns in namespaces:
                listed = await source_driver.list_objects(namespace=ns)
                for entry in listed:
                    if entry.key.startswith(HEALTHCHECK_PREFIX):
                        continue
                    full_key = f"{entry.namespace}/{entry.key}" if entry.namespace else entry.key
                    if (
                        entry.namespace,
                        entry.key,
                    ) in known_locators or full_key in known_full_keys:
                        continue
                    if full_key in seen_unknown_full_keys:
                        continue
                    seen_unknown_full_keys.add(full_key)
                    unknown_objects.append(
                        {
                            "namespace": entry.namespace,
                            "key": entry.key,
                            "size_bytes": entry.size_bytes,
                        }
                    )
        except Exception as exc:
            LOGGER.warning(
                "Reconciliation listing failed for profile %s: %s", source_profile_id, exc
            )

        if not active_objects and not unknown_objects:
            return None, None, []

        draining_profile: StorageProfile | None = None
        existing_bindings = await self.repository.list_bindings()
        still_bound_elsewhere = any(
            b.profile_id == source_profile_id and b.purpose != purpose for b in existing_bindings
        )
        if (
            source_profile is not None
            and source_profile.status == StorageProfileStatus.ACTIVE
            and not still_bound_elsewhere
        ):
            draining_profile = StorageProfile(
                id=source_profile.id,
                name=source_profile.name,
                backend=source_profile.backend,
                status=StorageProfileStatus.DRAINING,
                config=source_profile.config,
                secret_ref=source_profile.secret_ref,
                legacy=source_profile.legacy,
                health_status=source_profile.health_status,
                health_message=source_profile.health_message,
                last_checked_at=source_profile.last_checked_at,
                created_by=source_profile.created_by,
                updated_by=actor_id,
                created_at=source_profile.created_at,
                updated_at=utc_now_iso(),
            )

        now = utc_now_iso()
        migration_id = f"stmig_{secrets.token_hex(12)}"
        items = [
            StorageMigrationItem(
                id=f"stmitem_{secrets.token_hex(12)}",
                migration_id=migration_id,
                object_id=obj.id,
                source_profile_id=obj.profile_id,
                source_namespace=obj.namespace,
                source_key=obj.key,
                source_public_url=obj.public_url,
                target_profile_id=target_profile_id,
                target_namespace=obj.namespace,
                target_key=obj.key,
                target_public_url="",
                expected_sha256=obj.sha256,
                status=StorageMigrationItemStatus.PENDING,
                attempts=0,
                last_error="",
                created_at=now,
                updated_at=now,
            )
            for obj in active_objects
        ]
        if items:
            initial_status = StorageMigrationStatus.QUEUED
            initial_error = ""
            completed_at = ""
        else:
            initial_status = StorageMigrationStatus.COMPLETED_WITH_ERRORS
            initial_error = _UNKNOWN_OBJECTS_HOLD_ERROR
            completed_at = now
        migration = StorageMigration(
            id=migration_id,
            purpose=purpose,
            source_profile_id=source_profile_id,
            target_profile_id=target_profile_id,
            binding_generation=binding_generation,
            status=initial_status,
            total_count=len(items),
            completed_count=0,
            failed_count=0,
            skipped_count=0,
            unknown_objects=tuple(unknown_objects),
            last_error=initial_error,
            created_by=actor_id,
            created_at=now,
            updated_at=now,
            completed_at=completed_at,
        )
        return draining_profile, migration, items

    async def _skip_for_existing_target(
        self,
        *,
        migration: StorageMigration,
        item: StorageMigrationItem,
        current: StorageObjectRecord,
        target: StorageObjectRecord,
        attempts: int,
        now: str,
    ) -> str:
        deleted = replace(current, status=StorageObjectStatus.DELETED, updated_at=now)
        await self.repository.upsert_object(deleted)
        self.service.cache_object_record(deleted)
        if migration.purpose in PUBLIC_PURPOSES:
            alias_paths = set(
                _extract_public_alias_paths(
                    purpose=migration.purpose,
                    source_public_url=item.source_public_url,
                    source_key=item.source_key,
                )
            )
            alias_paths.add(f"/v1/storage/public/{current.id}")
            for alias_path in alias_paths:
                await self.repository.upsert_url_alias(
                    StorageUrlAlias(
                        id=f"stalias_{secrets.token_hex(12)}",
                        alias_path=alias_path,
                        object_id=target.id,
                        purpose=migration.purpose,
                        created_at=now,
                    )
                )
        await self.repository.update_migration_item(
            replace(
                item,
                target_profile_id=target.profile_id,
                target_namespace=target.namespace,
                target_key=target.key,
                target_public_url=target.public_url,
                expected_sha256=target.sha256,
                status=StorageMigrationItemStatus.SKIPPED,
                attempts=attempts,
                last_error="目标 Profile 已存在当前对象，已保留目标版本",
                updated_at=now,
            )
        )
        return ""

    async def _migrate_single_item(
        self,
        migration: StorageMigration,
        item: StorageMigrationItem,
    ) -> str:
        now = utc_now_iso()
        attempts = item.attempts + 1
        current = await self.repository.get_object(item.object_id)
        if (
            current is None
            or current.status != StorageObjectStatus.ACTIVE
            or current.profile_id != item.source_profile_id
            or current.namespace != item.source_namespace
            or current.key != item.source_key
            or (item.expected_sha256 and current.sha256 != item.expected_sha256)
        ):
            await self.repository.update_migration_item(
                StorageMigrationItem(
                    id=item.id,
                    migration_id=item.migration_id,
                    object_id=item.object_id,
                    source_profile_id=item.source_profile_id,
                    source_namespace=item.source_namespace,
                    source_key=item.source_key,
                    source_public_url=item.source_public_url,
                    target_profile_id=item.target_profile_id,
                    target_namespace=item.target_namespace,
                    target_key=item.target_key,
                    target_public_url="",
                    expected_sha256=item.expected_sha256,
                    status=StorageMigrationItemStatus.SKIPPED,
                    attempts=attempts,
                    last_error="对象在迁移前已被替换或删除，已跳过旧版本",
                    created_at=item.created_at,
                    updated_at=now,
                )
            )
            return ""

        target_existing = await self.repository.find_active_object(
            profile_id=item.target_profile_id,
            namespace=item.target_namespace,
            key=item.target_key,
        )
        if target_existing is not None and target_existing.id != current.id:
            return await self._skip_for_existing_target(
                migration=migration,
                item=item,
                current=current,
                target=target_existing,
                attempts=attempts,
                now=now,
            )

        try:
            stored = await self.service.copy_object(
                source_profile_id=item.source_profile_id,
                source_namespace=item.source_namespace,
                source_key=item.source_key,
                target_profile_id=item.target_profile_id,
                target_namespace=item.target_namespace,
                target_key=item.target_key,
                expected_sha256=item.expected_sha256,
                content_type=current.content_type,
                if_absent=True,
                register=False,
            )
            target_driver = self.service.driver_for_profile(item.target_profile_id)
            if stored.size_bytes > 0:
                probe_len = min(16, stored.size_bytes)
                ranged = await target_driver.read_range(
                    stored.key,
                    namespace=stored.namespace,
                    start_byte=0,
                    max_bytes=probe_len,
                    expected_size_bytes=stored.size_bytes,
                    expected_sha256=stored.sha256,
                )
                if len(ranged.content) != probe_len:
                    raise ObjectStorageError(
                        "migration_range_verify_failed",
                        "Migrated target object range check returned unexpected length",
                    )

            if not await self._is_migration_binding_current(migration):
                target_existing = await self.repository.find_active_object(
                    profile_id=item.target_profile_id,
                    namespace=item.target_namespace,
                    key=item.target_key,
                )
                if target_existing is None:
                    await self.service.delete_object(
                        profile_id=item.target_profile_id,
                        namespace=stored.namespace,
                        key=stored.key,
                    )
                return _STALE_MIGRATION_SENTINEL

            target_public_url = stored.public_url
            if not target_public_url and migration.purpose in PUBLIC_PURPOSES:
                target_public_url = f"/v1/storage/public/{current.id}"

            updated_object = StorageObjectRecord(
                id=current.id,
                purpose=current.purpose,
                profile_id=stored.profile_id,
                backend=stored.backend,
                namespace=stored.namespace,
                key=stored.key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                content_type=stored.content_type,
                status=StorageObjectStatus.ACTIVE,
                public_url=target_public_url,
                owner_type=current.owner_type,
                owner_id=current.owner_id,
                created_at=current.created_at,
                updated_at=now,
            )
            await self.repository.upsert_object(updated_object)
            self.service.cache_object_record(updated_object)

            if migration.purpose in PUBLIC_PURPOSES:
                for alias_path in _extract_public_alias_paths(
                    purpose=migration.purpose,
                    source_public_url=item.source_public_url,
                    source_key=item.source_key,
                ):
                    await self.repository.upsert_url_alias(
                        StorageUrlAlias(
                            id=f"stalias_{secrets.token_hex(12)}",
                            alias_path=alias_path,
                            object_id=current.id,
                            purpose=migration.purpose,
                            created_at=now,
                        )
                    )

            await self.repository.update_migration_item(
                StorageMigrationItem(
                    id=item.id,
                    migration_id=item.migration_id,
                    object_id=item.object_id,
                    source_profile_id=item.source_profile_id,
                    source_namespace=item.source_namespace,
                    source_key=item.source_key,
                    source_public_url=item.source_public_url,
                    target_profile_id=stored.profile_id,
                    target_namespace=stored.namespace,
                    target_key=stored.key,
                    target_public_url=target_public_url,
                    expected_sha256=stored.sha256,
                    status=StorageMigrationItemStatus.COMPLETED,
                    attempts=attempts,
                    last_error="",
                    created_at=item.created_at,
                    updated_at=now,
                )
            )
            return ""
        except Exception as exc:
            if isinstance(exc, ObjectStorageError) and exc.code == "object_key_conflict":
                target_existing = await self.repository.find_active_object(
                    profile_id=item.target_profile_id,
                    namespace=item.target_namespace,
                    key=item.target_key,
                )
                if target_existing is not None and target_existing.id != current.id:
                    return await self._skip_for_existing_target(
                        migration=migration,
                        item=item,
                        current=current,
                        target=target_existing,
                        attempts=attempts,
                        now=now,
                    )
            err_msg = str(exc) or exc.__class__.__name__
            await self.repository.update_migration_item(
                StorageMigrationItem(
                    id=item.id,
                    migration_id=item.migration_id,
                    object_id=item.object_id,
                    source_profile_id=item.source_profile_id,
                    source_namespace=item.source_namespace,
                    source_key=item.source_key,
                    source_public_url=item.source_public_url,
                    target_profile_id=item.target_profile_id,
                    target_namespace=item.target_namespace,
                    target_key=item.target_key,
                    target_public_url="",
                    expected_sha256=item.expected_sha256,
                    status=StorageMigrationItemStatus.FAILED,
                    attempts=attempts,
                    last_error=err_msg,
                    created_at=item.created_at,
                    updated_at=now,
                )
            )
            return err_msg

    async def _ensure_default_avatar_binding(self) -> None:
        if await self.repository.get_binding(StoragePurpose.AVATAR) is not None:
            return
        published = await self.repository.get_binding(StoragePurpose.ARTIFACT_PUBLISHED)
        if published is None:
            return
        profile = await self.repository.get_profile(published.profile_id)
        if profile is None or profile.status != StorageProfileStatus.ACTIVE:
            return
        await self.repository.upsert_binding(
            StorageBinding(
                purpose=StoragePurpose.AVATAR,
                profile_id=published.profile_id,
                generation=1,
                updated_by="system",
                updated_at=utc_now_iso(),
            )
        )

    async def _ensure_legacy_artifact_profiles(self) -> None:
        await self._ensure_default_avatar_binding()
        artifact_cfg = self.settings.artifacts
        if not artifact_cfg.enabled:
            return
        if artifact_cfg.storage_backend not in {"local", "s3"}:
            return
        q_binding = await self.repository.get_binding(StoragePurpose.ARTIFACT_QUARANTINE)
        p_binding = await self.repository.get_binding(StoragePurpose.ARTIFACT_PUBLISHED)
        if q_binding is None or p_binding is None:
            now = utc_now_iso()
            q_profile: StorageProfile | None = None
            p_profile: StorageProfile | None = None
            if artifact_cfg.storage_backend == "local":
                if artifact_cfg.local_root:
                    root = Path(artifact_cfg.local_root).resolve()
                    try:
                        for directory in (
                            root / "quarantine",
                            root / "content",
                            root / "published",
                        ):
                            directory.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        LOGGER.warning(
                            "Skipping legacy local artifact profile bootstrap for %s: %s",
                            root,
                            exc,
                        )
                    else:
                        q_profile = StorageProfile(
                            id=LEGACY_QUARANTINE_PROFILE_ID,
                            name="Legacy Local Quarantine",
                            backend=StorageBackend.LOCAL,
                            config={"mode": "filesystem", "root": str(root)},
                            legacy=True,
                            created_at=now,
                            updated_at=now,
                        )
                        p_profile = StorageProfile(
                            id=LEGACY_PUBLISHED_PROFILE_ID,
                            name="Legacy Local Published",
                            backend=StorageBackend.LOCAL,
                            config={
                                "mode": "filesystem",
                                "root": str(root / "published"),
                                "public_base_url": artifact_cfg.cdn_base_url.rstrip("/"),
                            },
                            legacy=True,
                            created_at=now,
                            updated_at=now,
                        )
            elif all(
                (
                    artifact_cfg.s3_endpoint_url,
                    artifact_cfg.s3_access_key_id,
                    artifact_cfg.s3_secret_access_key,
                    artifact_cfg.quarantine_bucket,
                    artifact_cfg.published_bucket,
                )
            ):
                q_profile = StorageProfile(
                    id=LEGACY_QUARANTINE_PROFILE_ID,
                    name="Legacy S3 Quarantine",
                    backend=StorageBackend.S3,
                    config={
                        "endpoint_url": artifact_cfg.s3_endpoint_url,
                        "region": artifact_cfg.s3_region,
                        "bucket": artifact_cfg.quarantine_bucket,
                    },
                    legacy=True,
                    created_at=now,
                    updated_at=now,
                )
                p_profile = StorageProfile(
                    id=LEGACY_PUBLISHED_PROFILE_ID,
                    name="Legacy S3 Published",
                    backend=StorageBackend.S3,
                    config={
                        "endpoint_url": artifact_cfg.s3_endpoint_url,
                        "region": artifact_cfg.s3_region,
                        "bucket": artifact_cfg.published_bucket,
                        "public_base_url": artifact_cfg.cdn_base_url.rstrip("/"),
                    },
                    legacy=True,
                    created_at=now,
                    updated_at=now,
                )

            if (
                q_profile is not None
                and q_binding is None
                and await self.repository.get_profile(q_profile.id) is None
            ):
                try:
                    driver_q = await self._build_driver(q_profile)
                    saved_q = await self.repository.upsert_profile(q_profile)
                    self.service.register_profile(saved_q, driver_q)
                    saved_qb = await self.repository.upsert_binding(
                        StorageBinding(
                            purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                            profile_id=saved_q.id,
                            generation=1,
                            updated_by="system",
                            updated_at=now,
                        )
                    )
                    self.service.set_binding(saved_qb)
                    q_binding = saved_qb
                except Exception as exc:
                    LOGGER.warning("Skipping legacy quarantine profile bootstrap: %s", exc)
            if (
                p_profile is not None
                and p_binding is None
                and await self.repository.get_profile(p_profile.id) is None
            ):
                try:
                    driver_p = await self._build_driver(p_profile)
                    saved_p = await self.repository.upsert_profile(p_profile)
                    self.service.register_profile(saved_p, driver_p)
                    saved_pb = await self.repository.upsert_binding(
                        StorageBinding(
                            purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                            profile_id=saved_p.id,
                            generation=1,
                            updated_by="system",
                            updated_at=now,
                        )
                    )
                    self.service.set_binding(saved_pb)
                    p_binding = saved_pb
                except Exception as exc:
                    LOGGER.warning("Skipping legacy published profile bootstrap: %s", exc)

        await self._ensure_default_avatar_binding()

        # Idempotently backfill pre-existing files on legacy profiles into market_storage_objects.
        if q_binding is not None and q_binding.profile_id == LEGACY_QUARANTINE_PROFILE_ID:
            legacy_q = await self.repository.get_profile(LEGACY_QUARANTINE_PROFILE_ID)
            if legacy_q is not None and legacy_q.status == StorageProfileStatus.ACTIVE:
                q_namespaces = (
                    ("quarantine", "content") if legacy_q.backend == StorageBackend.LOCAL else ("",)
                )
                await self._backfill_legacy_profile_objects(
                    profile=legacy_q,
                    purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                    namespaces=q_namespaces,
                )
        if p_binding is not None and p_binding.profile_id == LEGACY_PUBLISHED_PROFILE_ID:
            legacy_p = await self.repository.get_profile(LEGACY_PUBLISHED_PROFILE_ID)
            if legacy_p is not None and legacy_p.status == StorageProfileStatus.ACTIVE:
                await self._backfill_legacy_profile_objects(
                    profile=legacy_p,
                    purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                    namespaces=("",),
                )

    async def _backfill_legacy_profile_objects(
        self,
        *,
        profile: StorageProfile,
        purpose: StoragePurpose,
        namespaces: tuple[str, ...],
    ) -> int:
        try:
            driver = self.service.driver_for_profile(profile.id)
        except ObjectStorageError:
            return 0
        existing_objs = await self.repository.list_objects(profile_id=profile.id)
        existing_locators = {
            (obj.namespace, obj.key)
            for obj in existing_objs
            if obj.status != StorageObjectStatus.DELETED
        }
        now = utc_now_iso()
        added = 0
        for ns in namespaces:
            try:
                listed = await driver.list_objects(namespace=ns)
            except Exception as exc:
                LOGGER.warning(
                    "Legacy backfill listing failed for profile %s namespace %r: %s",
                    profile.id,
                    ns,
                    exc,
                )
                continue
            for entry in listed:
                if entry.key.startswith(HEALTHCHECK_PREFIX):
                    continue
                if (entry.namespace, entry.key) in existing_locators:
                    continue
                try:
                    (
                        size_bytes,
                        sha256,
                        content_type,
                        public_url,
                    ) = await self._inspect_object_metadata(
                        driver=driver,
                        purpose=purpose,
                        namespace=entry.namespace,
                        key=entry.key,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "Legacy backfill inspection failed for %s (%s/%s): %s",
                        profile.id,
                        entry.namespace,
                        entry.key,
                        exc,
                    )
                    continue
                object_id = f"stobj_{secrets.token_hex(12)}"
                if not public_url and purpose in PUBLIC_PURPOSES:
                    public_url = f"/v1/storage/public/{object_id}"
                await self.repository.upsert_object(
                    StorageObjectRecord(
                        id=object_id,
                        purpose=purpose,
                        profile_id=profile.id,
                        backend=profile.backend.value,
                        namespace=entry.namespace,
                        key=entry.key,
                        size_bytes=size_bytes,
                        sha256=sha256,
                        content_type=content_type,
                        status=StorageObjectStatus.ACTIVE,
                        public_url=public_url,
                        owner_type="legacy_backfill",
                        created_at=now,
                        updated_at=now,
                    )
                )
                if purpose == StoragePurpose.ARTIFACT_PUBLISHED:
                    for alias_path in _extract_public_alias_paths(
                        purpose=purpose,
                        source_public_url=public_url,
                        source_key=entry.key,
                    ):
                        if await self.repository.get_url_alias(alias_path) is None:
                            await self.repository.upsert_url_alias(
                                StorageUrlAlias(
                                    id=f"stalias_{secrets.token_hex(12)}",
                                    alias_path=alias_path,
                                    object_id=object_id,
                                    purpose=purpose,
                                    created_at=now,
                                )
                            )
                existing_locators.add((entry.namespace, entry.key))
                added += 1
        return added

    async def _inspect_object_metadata(
        self,
        *,
        driver: ObjectStorageDriver,
        purpose: StoragePurpose,
        namespace: str,
        key: str,
        precomputed_stat: Any | None = None,
    ) -> tuple[int, str, str, str]:
        stat = precomputed_stat or await driver.stat_object(key, namespace=namespace)
        if stat is None:
            raise ObjectStorageError("object_missing", f"Object {key} not found on backend")
        size_bytes = stat.size_bytes
        sha256 = stat.sha256
        content_type = stat.content_type
        public_url = stat.public_url
        if not sha256:
            descriptor, temp_name = tempfile.mkstemp(prefix="obj-inspect-", suffix=".tmp")
            os.close(descriptor)
            temporary = Path(temp_name)
            try:
                downloaded = await driver.download_to_path(key, temporary, namespace=namespace)
                size_bytes = downloaded.size_bytes
                sha256 = downloaded.sha256
                content_type = downloaded.content_type or content_type
                public_url = downloaded.public_url or public_url
            finally:
                temporary.unlink(missing_ok=True)
        if not public_url and purpose in PUBLIC_PURPOSES and driver.capabilities().public_read:
            try:
                public_url = driver.public_url(key, namespace=namespace)
            except ObjectStorageError:
                public_url = ""
        return size_bytes, sha256, content_type, public_url

    async def _build_driver(
        self,
        profile: StorageProfile,
        *,
        secret_override: Mapping[str, Any] | None = None,
    ) -> ObjectStorageDriver:
        cfg = dict(profile.config)
        if profile.backend == StorageBackend.LOCAL:
            mode = str(cfg.get("mode") or "filesystem").strip().lower()
            public_base_url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
            if mode == "filesystem":
                root = validate_local_filesystem_root(
                    str(cfg.get("root") or ""),
                    forbidden_roots=(REPO_ROOT,),
                    forbidden_files=(
                        (Path(self.settings.local_data_path),)
                        if self.settings.local_data_path
                        else ()
                    ),
                )
                return LocalFilesystemDriver(
                    profile_id=profile.id,
                    root=root,
                    public_base_url=public_base_url,
                )
            if mode == "sqlite":
                db_path = str(cfg.get("db_path") or "").strip()
                forbidden_files = (
                    (Path(self.settings.local_data_path),) if self.settings.local_data_path else ()
                )
                validated = validate_sqlite_db_path(
                    db_path,
                    forbidden_roots=(REPO_ROOT,),
                    forbidden_files=forbidden_files,
                )
                return SqliteObjectStorageDriver(
                    profile_id=profile.id,
                    db_path=validated,
                    public_base_url=public_base_url,
                )
            if mode == "postgresql":
                schema = validate_pg_schema(str(cfg.get("schema") or "public"))
                use_market_db = bool(cfg.get("use_market_db", True))
                if use_market_db:
                    pool = self._pg_pool_override
                    if pool is None and hasattr(self.store, "_pool"):
                        pool = self.store._pool()
                    if pool is None:
                        raise ObjectStorageError(
                            "pg_pool_unavailable",
                            "Market PostgreSQL pool is not available in current storage mode",
                        )
                    return PostgresObjectStorageDriver(
                        profile_id=profile.id,
                        pool=pool,
                        schema=schema,
                        public_base_url=public_base_url,
                    )
                secret = (
                    dict(secret_override)
                    if secret_override is not None
                    else await self._load_secret(profile)
                )
                dsn = str(secret.get("dsn") or "").strip()
                return PostgresObjectStorageDriver(
                    profile_id=profile.id,
                    dsn=dsn,
                    schema=schema,
                    public_base_url=public_base_url,
                )
            raise ObjectStorageError("local_mode_invalid", f"Unsupported local mode: {mode}")

        if profile.backend == StorageBackend.S3:
            bucket = str(cfg.get("bucket") or "").strip()
            endpoint_url = str(cfg.get("endpoint_url") or "").strip()
            region = str(cfg.get("region") or "auto").strip() or "auto"
            public_base_url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
            if profile.legacy and not profile.secret_ref:
                access_key_id = self.settings.artifacts.s3_access_key_id
                secret_access_key = self.settings.artifacts.s3_secret_access_key
            else:
                secret = (
                    dict(secret_override)
                    if secret_override is not None
                    else await self._load_secret(profile)
                )
                access_key_id = str(secret.get("access_key_id") or "")
                secret_access_key = str(secret.get("secret_access_key") or "")
            client = (
                self._s3_client_factory(profile, cfg) if callable(self._s3_client_factory) else None
            )
            return S3ObjectStorageDriver(
                profile_id=profile.id,
                bucket=bucket,
                endpoint_url=endpoint_url,
                region=region,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                public_base_url=public_base_url,
                client=client,
            )

        if profile.backend == StorageBackend.WEBDAV:
            allow_private = bool(cfg.get("allow_private_network", False))
            base_url = validate_webdav_base_url(
                cfg.get("base_url"),
                allow_private_network=allow_private,
            )
            root_path = validate_webdav_root_path(cfg.get("root_path"))
            auth_type = validate_webdav_auth_type(cfg.get("auth_type"))
            public_base_url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
            verify_tls = bool(cfg.get("verify_tls", True))
            conditional_create = bool(cfg.get("conditional_create_supported", True))
            range_read = bool(cfg.get("range_read_supported", True))
            secret = (
                dict(secret_override)
                if secret_override is not None
                else await self._load_secret(profile)
            )
            client = (
                self._webdav_client_factory(profile, cfg)
                if callable(self._webdav_client_factory)
                else None
            )
            return WebDavObjectStorageDriver(
                profile_id=profile.id,
                base_url=base_url,
                root_path=root_path,
                auth_type=auth_type,
                username=str(secret.get("username") or ""),
                password=str(secret.get("password") or ""),
                token=str(secret.get("token") or ""),
                public_base_url=public_base_url,
                allow_private_network=allow_private,
                verify_tls=verify_tls,
                conditional_create_supported=conditional_create,
                range_read_supported=range_read,
                client=client,
            )

        if profile.backend == StorageBackend.IMGBED:
            allow_private = bool(cfg.get("allow_private_network", False))
            base_url = validate_imgbed_base_url(
                cfg.get("base_url"),
                allow_private_network=allow_private,
            )
            upload_folder = validate_imgbed_folder(cfg.get("upload_folder"))
            scopes = validate_imgbed_scopes(cfg.get("scopes"))
            image_variants = bool(cfg.get("image_variants_enabled", True))
            range_read = bool(cfg.get("range_read_supported", False))
            plugin_zip_verified = bool(cfg.get("plugin_zip_verified", False))
            verify_tls = bool(cfg.get("verify_tls", True))
            secret = (
                dict(secret_override)
                if secret_override is not None
                else await self._load_secret(profile)
            )
            token = str(secret.get("token") or "").strip()
            client = (
                self._imgbed_client_factory(profile, cfg)
                if callable(self._imgbed_client_factory)
                else None
            )
            return ImgBedObjectStorageDriver(
                profile_id=profile.id,
                base_url=base_url,
                token=token,
                upload_folder=upload_folder,
                scopes=scopes,
                image_variants_enabled=image_variants,
                range_read_supported=range_read,
                plugin_zip_verified=plugin_zip_verified,
                allow_private_network=allow_private,
                verify_tls=verify_tls,
                client=client,
            )

        raise ObjectStorageError(
            "storage_backend_not_yet_implemented",
            f"Storage backend {profile.backend.value} will be enabled in a later stage",
        )

    async def _load_secret(self, profile: StorageProfile) -> dict[str, Any]:
        if not profile.secret_ref:
            return {}
        stored = await self.repository.get_secret(profile.secret_ref)
        if stored is None:
            raise ObjectStorageError("storage_credential_missing", "Stored secret not found")
        stored_profile_id, ciphertext = stored
        if stored_profile_id != profile.id:
            raise ObjectStorageError(
                "storage_credential_profile_mismatch", "Storage credential does not match profile"
            )
        return decrypt_storage_secret(
            self.settings.oauth_credential_key,
            profile_id=profile.id,
            ciphertext=ciphertext,
        )

    def _validate_profile_inputs(
        self,
        *,
        backend: StorageBackend,
        config: Mapping[str, Any],
        secret: Mapping[str, Any],
        existing_secret_ref: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        public_base_url = _validate_optional_public_base_url(config.get("public_base_url"))
        if backend == StorageBackend.LOCAL:
            mode = str(config.get("mode") or "filesystem").strip().lower()
            if mode == "filesystem":
                root = str(config.get("root") or "").strip()
                if not root:
                    raise ObjectStorageError(
                        "local_root_missing", "Local filesystem root is required"
                    )
                return (
                    {"mode": "filesystem", "root": root, "public_base_url": public_base_url},
                    {},
                )
            if mode == "sqlite":
                db_path = str(config.get("db_path") or "").strip()
                forbidden_files = (
                    (Path(self.settings.local_data_path),) if self.settings.local_data_path else ()
                )
                validated = validate_sqlite_db_path(
                    db_path,
                    forbidden_roots=(REPO_ROOT,),
                    forbidden_files=forbidden_files,
                )
                return (
                    {
                        "mode": "sqlite",
                        "db_path": str(validated),
                        "public_base_url": public_base_url,
                    },
                    {},
                )
            if mode == "postgresql":
                schema = validate_pg_schema(str(config.get("schema") or "public"))
                use_market_db = bool(config.get("use_market_db", True))
                cleaned_secret: dict[str, Any] = {}
                if not use_market_db:
                    dsn = str(secret.get("dsn") or "").strip()
                    if dsn:
                        cleaned_secret["dsn"] = dsn
                    elif not existing_secret_ref:
                        raise ObjectStorageError(
                            "pg_dsn_missing",
                            "External PostgreSQL storage profile requires a DSN",
                        )
                return (
                    {
                        "mode": "postgresql",
                        "use_market_db": use_market_db,
                        "schema": schema,
                        "public_base_url": public_base_url,
                    },
                    cleaned_secret,
                )
            raise ObjectStorageError("local_mode_invalid", f"Unsupported local mode: {mode}")

        if backend == StorageBackend.S3:
            bucket = str(config.get("bucket") or "").strip()
            endpoint_url = str(config.get("endpoint_url") or "").strip()
            region = str(config.get("region") or "auto").strip() or "auto"
            if not bucket:
                raise ObjectStorageError("s3_bucket_missing", "S3 bucket is required")
            if not endpoint_url:
                raise ObjectStorageError("s3_endpoint_url_missing", "S3 endpoint URL is required")
            cleaned_secret = {}
            ak = str(secret.get("access_key_id") or "").strip()
            sk = str(secret.get("secret_access_key") or "").strip()
            if ak:
                cleaned_secret["access_key_id"] = ak
            if sk:
                cleaned_secret["secret_access_key"] = sk
            if not existing_secret_ref and (not ak or not sk):
                raise ObjectStorageError(
                    "s3_credentials_missing",
                    "S3 access_key_id and secret_access_key are required",
                )
            return (
                {
                    "endpoint_url": endpoint_url,
                    "region": region,
                    "bucket": bucket,
                    "public_base_url": public_base_url,
                },
                cleaned_secret,
            )

        if backend == StorageBackend.WEBDAV:
            allow_private = bool(config.get("allow_private_network", False))
            base_url = validate_webdav_base_url(
                config.get("base_url"),
                allow_private_network=allow_private,
            )
            root_path = validate_webdav_root_path(config.get("root_path"))
            auth_type = validate_webdav_auth_type(config.get("auth_type"))
            verify_tls = bool(config.get("verify_tls", True))
            cleaned_secret = {}
            if auth_type == "basic":
                username = str(secret.get("username") or "").strip()
                password = str(secret.get("password") or "")
                if username:
                    cleaned_secret["username"] = username
                if password:
                    cleaned_secret["password"] = password
                if not existing_secret_ref and (not username or not password):
                    raise ObjectStorageError(
                        "webdav_credentials_missing",
                        "WebDAV basic auth requires username and password",
                    )
            elif auth_type == "bearer":
                token = str(secret.get("token") or "").strip()
                if token:
                    cleaned_secret["token"] = token
                if not existing_secret_ref and not token:
                    raise ObjectStorageError(
                        "webdav_credentials_missing",
                        "WebDAV bearer auth requires a token",
                    )
            sanitized_webdav: dict[str, Any] = {
                "base_url": base_url,
                "root_path": root_path,
                "auth_type": auth_type,
                "allow_private_network": allow_private,
                "verify_tls": verify_tls,
                "public_base_url": public_base_url,
            }
            return sanitized_webdav, cleaned_secret

        if backend == StorageBackend.IMGBED:
            allow_private = bool(config.get("allow_private_network", False))
            base_url = validate_imgbed_base_url(
                config.get("base_url"),
                allow_private_network=allow_private,
            )
            upload_folder = validate_imgbed_folder(config.get("upload_folder"))
            scopes = validate_imgbed_scopes(config.get("scopes"))
            image_variants = bool(config.get("image_variants_enabled", True))
            verify_tls = bool(config.get("verify_tls", True))
            cleaned_secret = {}
            token = str(secret.get("token") or "").strip()
            if token:
                cleaned_secret["token"] = token
            if not existing_secret_ref and not token:
                raise ObjectStorageError(
                    "imgbed_token_missing",
                    "ImgBed Bearer token is required",
                )
            sanitized_imgbed: dict[str, Any] = {
                "base_url": base_url,
                "upload_folder": upload_folder,
                "scopes": list(scopes),
                "image_variants_enabled": image_variants,
                "allow_private_network": allow_private,
                "verify_tls": verify_tls,
            }
            return sanitized_imgbed, cleaned_secret

        raise ObjectStorageError(
            "storage_backend_not_yet_implemented",
            f"Storage backend {backend.value} will be enabled in a later stage",
        )

    async def _profile_view(self, profile_id: str) -> dict[str, Any] | None:
        profiles = await self.list_profiles(include_removed=True)
        return next((item for item in profiles if item["id"] == profile_id), None)


async def storage_migration_worker(
    manager: StorageManager,
    *,
    poll_seconds: float = 5.0,
) -> None:
    """Background worker executing queued storage migrations independently of artifact-worker."""
    while True:
        try:
            await manager.process_queued_migrations()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Storage migration worker iteration failed: %s", exc)
        await asyncio.sleep(poll_seconds)


def _validate_optional_public_base_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ObjectStorageError("public_base_url_invalid", "Invalid public_base_url") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ObjectStorageError(
            "public_base_url_invalid",
            "public_base_url must be an absolute http(s) URL",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ObjectStorageError(
            "public_base_url_invalid",
            "public_base_url cannot contain credentials, query, or fragment",
        )
    return raw


def _extract_public_alias_paths(
    *,
    purpose: StoragePurpose,
    source_public_url: str,
    source_key: str,
) -> list[str]:
    paths: list[str] = []
    if source_public_url:
        try:
            parsed = urlsplit(source_public_url)
            if parsed.path and parsed.path.startswith("/"):
                paths.append(parsed.path)
        except ValueError:
            pass
    if purpose == StoragePurpose.ARTIFACT_PUBLISHED and source_key:
        pkg_alias = f"/packages/{source_key.lstrip('/')}"
        if pkg_alias not in paths:
            paths.append(pkg_alias)
    return paths


def _recompute_migration_counts(
    migration: StorageMigration,
    items: list[StorageMigrationItem],
    *,
    status: StorageMigrationStatus,
    last_error: str,
) -> StorageMigration:
    completed = sum(1 for i in items if i.status == StorageMigrationItemStatus.COMPLETED)
    failed = sum(1 for i in items if i.status == StorageMigrationItemStatus.FAILED)
    skipped = sum(1 for i in items if i.status == StorageMigrationItemStatus.SKIPPED)
    now = utc_now_iso()
    done_at = (
        now
        if status
        in {
            StorageMigrationStatus.COMPLETED,
            StorageMigrationStatus.COMPLETED_WITH_ERRORS,
            StorageMigrationStatus.CANCELLED,
        }
        else ""
    )
    return StorageMigration(
        id=migration.id,
        purpose=migration.purpose,
        source_profile_id=migration.source_profile_id,
        target_profile_id=migration.target_profile_id,
        binding_generation=migration.binding_generation,
        status=status,
        total_count=len(items),
        completed_count=completed,
        failed_count=failed,
        skipped_count=skipped,
        unknown_objects=migration.unknown_objects,
        last_error=last_error,
        created_by=migration.created_by,
        created_at=migration.created_at,
        updated_at=now,
        completed_at=done_at,
    )
