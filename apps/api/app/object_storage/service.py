"""Profile registry, purpose bindings, in-memory object tracking, and legacy config mapping."""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from collections.abc import AsyncIterable, Mapping
from pathlib import Path
from typing import Any

from ..config import ArtifactSettings
from .credentials import InMemoryStorageSecretStore, StorageSecretStore
from .driver import ObjectStorageDriver
from .local_fs import LocalFilesystemDriver
from .models import (
    PURPOSE_ALLOWED_BACKENDS,
    ObjectListingEntry,
    ObjectRange,
    ObjectStorageError,
    StorageBackend,
    StorageBinding,
    StorageCapabilities,
    StorageObjectRecord,
    StorageObjectStatus,
    StorageProfile,
    StorageProfileStatus,
    StoragePurpose,
    StoredObject,
    guess_content_type,
    utc_now_iso,
    validate_expected_sha256,
    validate_namespace,
    validate_object_key,
    validate_profile_id,
)
from .s3 import S3ObjectStorageDriver

LEGACY_QUARANTINE_PROFILE_ID = "legacy-market-quarantine"
LEGACY_PUBLISHED_PROFILE_ID = "legacy-market-published"
STREAM_COPY_CHUNK_SIZE = 1024 * 1024


class ObjectStorageService:
    """Coordinates storage profiles, purpose bindings, drivers, and object registration."""

    def __init__(self, *, secret_store: StorageSecretStore | None = None) -> None:
        self.secret_store = secret_store
        self._profiles: dict[str, StorageProfile] = {}
        self._drivers: dict[str, ObjectStorageDriver] = {}
        self._bindings: dict[StoragePurpose, StorageBinding] = {}
        self._objects: dict[str, StorageObjectRecord] = {}
        self._object_index: dict[tuple[str, str, str], str] = {}

    def register_profile(
        self,
        profile: StorageProfile,
        driver: ObjectStorageDriver,
    ) -> StorageProfile:
        if driver.profile_id != profile.id or driver.backend != profile.backend:
            raise ObjectStorageError(
                "storage_profile_driver_mismatch",
                "Storage driver does not match profile identity or backend",
            )
        self._profiles[profile.id] = profile
        self._drivers[profile.id] = driver
        return profile

    def get_profile(self, profile_id: str) -> StorageProfile | None:
        return self._profiles.get(validate_profile_id(profile_id))

    def list_profiles(self) -> list[StorageProfile]:
        return list(self._profiles.values())

    def bind_purpose(
        self,
        purpose: StoragePurpose,
        profile_id: str,
        *,
        updated_by: str = "",
    ) -> StorageBinding:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        if profile.status != StorageProfileStatus.ACTIVE:
            raise ObjectStorageError(
                "storage_profile_not_active",
                f"Storage profile {profile.id} is not active",
            )
        allowed_backends = PURPOSE_ALLOWED_BACKENDS.get(purpose, frozenset())
        if profile.backend not in allowed_backends:
            raise ObjectStorageError(
                "storage_backend_disallowed_for_purpose",
                f"Backend {profile.backend.value} cannot be bound to {purpose.value}",
            )
        previous = self._bindings.get(purpose)
        generation = (previous.generation + 1) if previous is not None else 1
        binding = StorageBinding(
            purpose=purpose,
            profile_id=profile.id,
            generation=generation,
            updated_by=updated_by,
            updated_at=utc_now_iso(),
        )
        self._bindings[purpose] = binding
        return binding

    def get_binding(self, purpose: StoragePurpose) -> StorageBinding | None:
        return self._bindings.get(purpose)

    def list_bindings(self) -> list[StorageBinding]:
        return list(self._bindings.values())

    def driver_for_profile(self, profile_id: str) -> ObjectStorageDriver:
        driver = self._drivers.get(validate_profile_id(profile_id))
        if driver is None:
            raise ObjectStorageError("storage_profile_not_found", f"Unknown profile: {profile_id}")
        return driver

    def driver_for_purpose(self, purpose: StoragePurpose) -> ObjectStorageDriver:
        binding = self._bindings.get(purpose)
        if binding is None:
            raise ObjectStorageError(
                "storage_purpose_unbound", f"No storage profile bound for {purpose.value}"
            )
        return self.driver_for_profile(binding.profile_id)

    def capabilities_for_profile(self, profile_id: str) -> StorageCapabilities:
        return self.driver_for_profile(profile_id).capabilities()

    def capabilities_for_purpose(self, purpose: StoragePurpose) -> StorageCapabilities:
        return self.driver_for_purpose(purpose).capabilities()

    async def put_object(
        self,
        *,
        key: str,
        data: bytes | AsyncIterable[bytes],
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
        content_type: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
        if_absent: bool = True,
        metadata: Mapping[str, str] | None = None,
        owner_type: str = "",
        owner_id: str = "",
        register: bool = True,
    ) -> StoredObject:
        driver, resolved_purpose = self._resolve_target(purpose=purpose, profile_id=profile_id)
        normalized_ns = validate_namespace(namespace)
        validate_object_key(key)
        resolved_type = guess_content_type(key, content_type)
        record_id = ""
        if register and resolved_purpose is not None:
            record_id = self._register_pending(
                purpose=resolved_purpose,
                profile_id=driver.profile_id,
                backend=driver.backend.value,
                namespace=normalized_ns,
                key=key,
                content_type=resolved_type,
                owner_type=owner_type,
                owner_id=owner_id,
            )
        try:
            stored = await driver.put_object(
                key,
                data,
                namespace=normalized_ns,
                content_type=resolved_type,
                max_bytes=max_bytes,
                expected_sha256=expected_sha256,
                if_absent=if_absent,
                metadata=metadata,
            )
        except Exception:
            if record_id:
                self._discard_pending(record_id)
            raise
        if record_id:
            self._activate_record(record_id, stored)
        return stored

    async def stat_object(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> StoredObject | None:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        return await driver.stat_object(key, namespace=namespace)

    async def read_object(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> bytes:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        return await driver.read_object(
            key,
            namespace=namespace,
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
        )

    async def read_range(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
        start_byte: int,
        max_bytes: int,
        max_object_bytes: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> ObjectRange:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        return await driver.read_range(
            key,
            namespace=namespace,
            start_byte=start_byte,
            max_bytes=max_bytes,
            max_object_bytes=max_object_bytes,
            expected_size_bytes=expected_size_bytes,
            expected_sha256=expected_sha256,
        )

    async def download_to_path(
        self,
        *,
        key: str,
        destination: Path,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> StoredObject:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        return await driver.download_to_path(key, destination, namespace=namespace)

    async def copy_object(
        self,
        *,
        source_key: str,
        target_key: str,
        source_purpose: StoragePurpose | None = None,
        source_profile_id: str = "",
        source_namespace: str = "",
        target_purpose: StoragePurpose | None = None,
        target_profile_id: str = "",
        target_namespace: str = "",
        expected_sha256: str = "",
        content_type: str = "",
        if_absent: bool = True,
        owner_type: str = "",
        owner_id: str = "",
        register: bool = True,
    ) -> StoredObject:
        source_driver, _ = self._resolve_target(
            purpose=source_purpose, profile_id=source_profile_id
        )
        target_driver, resolved_target_purpose = self._resolve_target(
            purpose=target_purpose, profile_id=target_profile_id
        )
        normalized_src_ns = validate_namespace(source_namespace)
        normalized_dst_ns = validate_namespace(target_namespace)
        validate_object_key(source_key)
        validate_object_key(target_key)
        resolved_type = guess_content_type(target_key, content_type)
        record_id = ""
        if register and resolved_target_purpose is not None:
            record_id = self._register_pending(
                purpose=resolved_target_purpose,
                profile_id=target_driver.profile_id,
                backend=target_driver.backend.value,
                namespace=normalized_dst_ns,
                key=target_key,
                content_type=resolved_type,
                owner_type=owner_type,
                owner_id=owner_id,
            )
        try:
            if source_driver.profile_id == target_driver.profile_id:
                stored = await source_driver.copy_object(
                    source_key,
                    target_key,
                    source_namespace=normalized_src_ns,
                    target_namespace=normalized_dst_ns,
                    expected_sha256=expected_sha256,
                    content_type=resolved_type,
                    if_absent=if_absent,
                )
            else:
                stored = await self._stream_copy_across_profiles(
                    source_driver=source_driver,
                    source_key=source_key,
                    source_namespace=normalized_src_ns,
                    target_driver=target_driver,
                    target_key=target_key,
                    target_namespace=normalized_dst_ns,
                    expected_sha256=expected_sha256,
                    content_type=resolved_type,
                    if_absent=if_absent,
                )
        except Exception:
            if record_id:
                self._discard_pending(record_id)
            raise
        if record_id:
            self._activate_record(record_id, stored)
        return stored

    async def delete_object(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> None:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        normalized_ns = validate_namespace(namespace)
        await driver.delete_object(key, namespace=normalized_ns)
        index_key = (driver.profile_id, normalized_ns, key)
        record_id = self._object_index.pop(index_key, None)
        if record_id and record_id in self._objects:
            existing = self._objects[record_id]
            self._objects[record_id] = StorageObjectRecord(
                id=existing.id,
                purpose=existing.purpose,
                profile_id=existing.profile_id,
                backend=existing.backend,
                namespace=existing.namespace,
                key=existing.key,
                size_bytes=existing.size_bytes,
                sha256=existing.sha256,
                content_type=existing.content_type,
                status=StorageObjectStatus.DELETED,
                public_url=existing.public_url,
                owner_type=existing.owner_type,
                owner_id=existing.owner_id,
                created_at=existing.created_at,
                updated_at=utc_now_iso(),
            )

    def public_url(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> str:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        return driver.public_url(key, namespace=namespace)

    async def list_objects(
        self,
        *,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]:
        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        return await driver.list_objects(namespace=namespace, prefix=prefix)

    def get_object_record(self, object_id: str) -> StorageObjectRecord | None:
        return self._objects.get(object_id)

    def find_object_record(
        self,
        *,
        profile_id: str,
        key: str,
        namespace: str = "",
    ) -> StorageObjectRecord | None:
        index_key = (validate_profile_id(profile_id), validate_namespace(namespace), key)
        record_id = self._object_index.get(index_key)
        return self._objects.get(record_id) if record_id else None

    def list_object_records(
        self,
        *,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        status: StorageObjectStatus | None = None,
    ) -> list[StorageObjectRecord]:
        results: list[StorageObjectRecord] = []
        for record in self._objects.values():
            if purpose is not None and record.purpose != purpose:
                continue
            if profile_id and record.profile_id != profile_id:
                continue
            if status is not None and record.status != status:
                continue
            results.append(record)
        return results

    def _resolve_target(
        self,
        *,
        purpose: StoragePurpose | None,
        profile_id: str,
    ) -> tuple[ObjectStorageDriver, StoragePurpose | None]:
        if profile_id:
            return self.driver_for_profile(profile_id), purpose
        if purpose is not None:
            return self.driver_for_purpose(purpose), purpose
        raise ObjectStorageError(
            "storage_target_missing", "Either purpose or profile_id must be specified"
        )

    def _register_pending(
        self,
        *,
        purpose: StoragePurpose,
        profile_id: str,
        backend: str,
        namespace: str,
        key: str,
        content_type: str,
        owner_type: str,
        owner_id: str,
    ) -> str:
        index_key = (profile_id, namespace, key)
        existing_id = self._object_index.get(index_key)
        if existing_id and existing_id in self._objects:
            existing = self._objects[existing_id]
            if existing.status == StorageObjectStatus.ACTIVE:
                return existing_id
        now = utc_now_iso()
        record_id = f"stobj_{secrets.token_hex(12)}"
        self._objects[record_id] = StorageObjectRecord(
            id=record_id,
            purpose=purpose,
            profile_id=profile_id,
            backend=backend,
            namespace=namespace,
            key=key,
            size_bytes=0,
            sha256="",
            content_type=content_type,
            status=StorageObjectStatus.PENDING,
            owner_type=owner_type,
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
        )
        self._object_index[index_key] = record_id
        return record_id

    def _activate_record(self, record_id: str, stored: StoredObject) -> None:
        existing = self._objects.get(record_id)
        if existing is None:
            return
        self._objects[record_id] = StorageObjectRecord(
            id=existing.id,
            purpose=existing.purpose,
            profile_id=stored.profile_id,
            backend=stored.backend,
            namespace=stored.namespace,
            key=stored.key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            content_type=stored.content_type,
            status=StorageObjectStatus.ACTIVE,
            public_url=stored.public_url,
            owner_type=existing.owner_type,
            owner_id=existing.owner_id,
            created_at=existing.created_at,
            updated_at=utc_now_iso(),
        )

    def _discard_pending(self, record_id: str) -> None:
        existing = self._objects.get(record_id)
        if existing is None or existing.status != StorageObjectStatus.PENDING:
            return
        self._objects.pop(record_id, None)
        index_key = (existing.profile_id, existing.namespace, existing.key)
        if self._object_index.get(index_key) == record_id:
            self._object_index.pop(index_key, None)

    async def _stream_copy_across_profiles(
        self,
        *,
        source_driver: ObjectStorageDriver,
        source_key: str,
        source_namespace: str,
        target_driver: ObjectStorageDriver,
        target_key: str,
        target_namespace: str,
        expected_sha256: str,
        content_type: str,
        if_absent: bool,
    ) -> StoredObject:
        descriptor, temp_name = tempfile.mkstemp(prefix="obj-copy-", suffix=".tmp")
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            source = await source_driver.download_to_path(
                source_key,
                temporary,
                namespace=source_namespace,
            )
            validate_expected_sha256(expected_sha256, source.sha256)
            stored = await target_driver.put_object(
                target_key,
                _file_chunk_stream(temporary),
                namespace=target_namespace,
                content_type=content_type,
                expected_sha256=source.sha256,
                if_absent=if_absent,
            )
            if stored.size_bytes != source.size_bytes or stored.sha256 != source.sha256:
                raise ObjectStorageError(
                    "copy_verification_failed",
                    "Copied object digest or size does not match source",
                )
            return stored
        finally:
            temporary.unlink(missing_ok=True)


async def _file_chunk_stream(path: Path) -> AsyncIterable[bytes]:
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, STREAM_COPY_CHUNK_SIZE):
            yield chunk


def build_local_legacy_object_storage_service(
    local_root: str | Path,
    cdn_base_url: str,
    *,
    master_key: str | None = None,
) -> ObjectStorageService:
    """Build legacy local quarantine/published profiles bound to artifact purposes."""
    secret_store = InMemoryStorageSecretStore(master_key) if master_key else None
    service = ObjectStorageService(secret_store=secret_store)
    root = Path(local_root).resolve()
    published_root = root / "published"
    service.register_profile(
        StorageProfile(
            id=LEGACY_QUARANTINE_PROFILE_ID,
            name="Legacy Local Quarantine",
            backend=StorageBackend.LOCAL,
            config={"mode": "filesystem", "root": str(root)},
            legacy=True,
        ),
        LocalFilesystemDriver(
            profile_id=LEGACY_QUARANTINE_PROFILE_ID,
            root=root,
            public_base_url="",
        ),
    )
    service.register_profile(
        StorageProfile(
            id=LEGACY_PUBLISHED_PROFILE_ID,
            name="Legacy Local Published",
            backend=StorageBackend.LOCAL,
            config={
                "mode": "filesystem",
                "root": str(published_root),
                "public_base_url": cdn_base_url.rstrip("/"),
            },
            legacy=True,
        ),
        LocalFilesystemDriver(
            profile_id=LEGACY_PUBLISHED_PROFILE_ID,
            root=published_root,
            public_base_url=cdn_base_url,
        ),
    )
    service.bind_purpose(StoragePurpose.ARTIFACT_QUARANTINE, LEGACY_QUARANTINE_PROFILE_ID)
    service.bind_purpose(StoragePurpose.ARTIFACT_PUBLISHED, LEGACY_PUBLISHED_PROFILE_ID)
    return service


def build_legacy_object_storage_service(
    config: ArtifactSettings,
    *,
    s3_client: Any | None = None,
    master_key: str | None = None,
) -> ObjectStorageService:
    """Map legacy ARTIFACT_STORAGE_BACKEND and ARTIFACT_CDN_BASE_URL into storage profiles.

    Binds only artifact-quarantine and artifact-published; avatar stays unbound.
    """
    if config.storage_backend == "local":
        return build_local_legacy_object_storage_service(
            config.local_root,
            config.cdn_base_url,
            master_key=master_key,
        )
    if config.storage_backend == "s3":
        secret_store = InMemoryStorageSecretStore(master_key) if master_key else None
        service = ObjectStorageService(secret_store=secret_store)
        quarantine_driver: ObjectStorageDriver = S3ObjectStorageDriver(
            profile_id=LEGACY_QUARANTINE_PROFILE_ID,
            bucket=config.quarantine_bucket,
            endpoint_url=config.s3_endpoint_url,
            region=config.s3_region,
            access_key_id=config.s3_access_key_id,
            secret_access_key=config.s3_secret_access_key,
            public_base_url="",
            client=s3_client,
        )
        published_driver: ObjectStorageDriver = S3ObjectStorageDriver(
            profile_id=LEGACY_PUBLISHED_PROFILE_ID,
            bucket=config.published_bucket,
            endpoint_url=config.s3_endpoint_url,
            region=config.s3_region,
            access_key_id=config.s3_access_key_id,
            secret_access_key=config.s3_secret_access_key,
            public_base_url=config.cdn_base_url,
            client=s3_client,
        )
        service.register_profile(
            StorageProfile(
                id=LEGACY_QUARANTINE_PROFILE_ID,
                name="Legacy S3 Quarantine",
                backend=StorageBackend.S3,
                config={
                    "endpoint_url": config.s3_endpoint_url,
                    "region": config.s3_region,
                    "bucket": config.quarantine_bucket,
                },
                legacy=True,
            ),
            quarantine_driver,
        )
        service.register_profile(
            StorageProfile(
                id=LEGACY_PUBLISHED_PROFILE_ID,
                name="Legacy S3 Published",
                backend=StorageBackend.S3,
                config={
                    "endpoint_url": config.s3_endpoint_url,
                    "region": config.s3_region,
                    "bucket": config.published_bucket,
                    "public_base_url": config.cdn_base_url.rstrip("/"),
                },
                legacy=True,
            ),
            published_driver,
        )
        service.bind_purpose(StoragePurpose.ARTIFACT_QUARANTINE, LEGACY_QUARANTINE_PROFILE_ID)
        service.bind_purpose(StoragePurpose.ARTIFACT_PUBLISHED, LEGACY_PUBLISHED_PROFILE_ID)
        return service
    raise ObjectStorageError("storage_backend_invalid", "Unsupported artifact storage backend")
