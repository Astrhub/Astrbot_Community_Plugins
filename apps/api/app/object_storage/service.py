"""Profile registry, purpose bindings, in-memory object tracking, and legacy config mapping."""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from collections.abc import AsyncIterable, Iterable, Mapping
from pathlib import Path
from dataclasses import replace
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

    def __init__(
        self,
        *,
        secret_store: StorageSecretStore | None = None,
        repository: Any | None = None,
    ) -> None:
        self.secret_store = secret_store
        self.repository = repository
        self._profiles: dict[str, StorageProfile] = {}
        self._drivers: dict[str, ObjectStorageDriver] = {}
        self._bindings: dict[StoragePurpose, StorageBinding] = {}
        self._objects: dict[str, StorageObjectRecord] = {}
        self._object_index: dict[tuple[str, str, str], str] = {}

    def unregister_profile(self, profile_id: str) -> None:
        normalized = validate_profile_id(profile_id)
        self._profiles.pop(normalized, None)
        self._drivers.pop(normalized, None)

    def reset(self) -> None:
        self._profiles.clear()
        self._drivers.clear()
        self._bindings.clear()
        self._objects.clear()
        self._object_index.clear()

    def replace_bindings(self, bindings: Iterable[StorageBinding]) -> None:
        self._bindings = {binding.purpose: binding for binding in bindings}

    def replace_object_records(self, records: Iterable[StorageObjectRecord]) -> None:
        objects = {record.id: record for record in records}
        index: dict[tuple[str, str, str], str] = {}
        for record in objects.values():
            if record.status == StorageObjectStatus.DELETED:
                continue
            locator = (record.profile_id, record.namespace, record.key)
            previous_id = index.get(locator)
            if previous_id is None:
                index[locator] = record.id
                continue
            previous = objects[previous_id]
            if (
                record.status == StorageObjectStatus.ACTIVE
                and previous.status != StorageObjectStatus.ACTIVE
            ):
                index[locator] = record.id
            elif record.status == previous.status and (record.updated_at, record.id) > (
                previous.updated_at,
                previous.id,
            ):
                index[locator] = record.id
        self._objects = objects
        self._object_index = index

    def cache_object_record(self, record: StorageObjectRecord) -> None:
        self._objects[record.id] = record
        locator = (record.profile_id, record.namespace, record.key)
        if record.status == StorageObjectStatus.DELETED:
            if self._object_index.get(locator) == record.id:
                self._object_index.pop(locator, None)
            return
        self._object_index[locator] = record.id

    def find_active_record_by_purpose_and_key(
        self,
        *,
        purpose: StoragePurpose,
        key: str,
        namespace: str | None = None,
    ) -> StorageObjectRecord | None:
        candidates = [
            record
            for record in self._objects.values()
            if record.status == StorageObjectStatus.ACTIVE
            and record.purpose == purpose
            and record.key == key
            and (namespace is None or record.namespace == namespace)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return candidates[0]

    def set_binding(self, binding: StorageBinding) -> StorageBinding:
        self._bindings[binding.purpose] = binding
        return binding

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
            record_id = await self._register_pending_async(
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
                await self._discard_pending_async(record_id)
            raise
        if record_id:
            await self._activate_record_async(record_id, stored)
        return stored

    async def stat_object(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> StoredObject | None:
        driver, resolved_ns, resolved_key = await self._resolve_read_target(
            purpose=purpose,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        return await driver.stat_object(resolved_key, namespace=resolved_ns)

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
        driver, resolved_ns, resolved_key = await self._resolve_read_target(
            purpose=purpose,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        return await driver.read_object(
            resolved_key,
            namespace=resolved_ns,
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
        driver, resolved_ns, resolved_key = await self._resolve_read_target(
            purpose=purpose,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        return await driver.read_range(
            resolved_key,
            namespace=resolved_ns,
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
        driver, resolved_ns, resolved_key = await self._resolve_read_target(
            purpose=purpose,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        return await driver.download_to_path(resolved_key, destination, namespace=resolved_ns)

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
        source_driver, normalized_src_ns, resolved_src_key = await self._resolve_read_target(
            purpose=source_purpose,
            profile_id=source_profile_id,
            namespace=source_namespace,
            key=source_key,
        )
        target_driver, resolved_target_purpose = self._resolve_target(
            purpose=target_purpose, profile_id=target_profile_id
        )
        normalized_dst_ns = validate_namespace(target_namespace)
        validate_object_key(resolved_src_key)
        validate_object_key(target_key)
        resolved_type = guess_content_type(target_key, content_type)
        record_id = ""
        if register and resolved_target_purpose is not None:
            record_id = await self._register_pending_async(
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
                    resolved_src_key,
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
                    source_key=resolved_src_key,
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
                await self._discard_pending_async(record_id)
            raise
        if record_id:
            await self._activate_record_async(record_id, stored)
        return stored

    async def delete_object(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> None:
        driver, normalized_ns, resolved_key = await self._resolve_read_target(
            purpose=purpose,
            profile_id=profile_id,
            namespace=namespace,
            key=key,
        )
        await driver.delete_object(resolved_key, namespace=normalized_ns)
        index_key = (driver.profile_id, normalized_ns, resolved_key)
        record_id = self._object_index.pop(index_key, None)
        if record_id and record_id in self._objects:
            existing = self._objects[record_id]
            updated = StorageObjectRecord(
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
            self._objects[record_id] = updated
            if self.repository is not None:
                await self.repository.upsert_object(updated)
        elif self.repository is not None:
            existing = await self.repository.find_active_object(
                profile_id=driver.profile_id,
                namespace=normalized_ns,
                key=resolved_key,
            )
            if existing is not None:
                deleted = StorageObjectRecord(
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
                await self.repository.upsert_object(deleted)

    def public_url(
        self,
        *,
        key: str,
        purpose: StoragePurpose | None = None,
        profile_id: str = "",
        namespace: str = "",
    ) -> str:
        record = None
        if purpose is not None:
            record = self.find_active_record_by_purpose_and_key(
                purpose=purpose,
                key=key,
                namespace=namespace,
            )
            if record is None and not namespace:
                record = self.find_active_record_by_purpose_and_key(
                    purpose=purpose,
                    key=key,
                    namespace=None,
                )
        if record is not None:
            try:
                record_driver = self.driver_for_profile(record.profile_id)
            except ObjectStorageError:
                record_driver = None
            if record_driver is not None:
                public_url_for_sha256 = getattr(record_driver, "public_url_for_sha256", None)
                if record.sha256 and callable(public_url_for_sha256):
                    try:
                        return public_url_for_sha256(
                            record.key,
                            namespace=record.namespace,
                            sha256=record.sha256,
                        )
                    except ObjectStorageError as exc:
                        if exc.code != "public_url_unconfigured":
                            raise
                try:
                    return record_driver.public_url(record.key, namespace=record.namespace)
                except ObjectStorageError as exc:
                    if exc.code != "public_url_unconfigured":
                        raise
            if record.public_url:
                return record.public_url

        driver, _ = self._resolve_target(purpose=purpose, profile_id=profile_id)
        try:
            return driver.public_url(key, namespace=namespace)
        except ObjectStorageError as exc:
            if exc.code == "public_url_unconfigured":
                record = self.find_object_record(
                    profile_id=driver.profile_id,
                    namespace=namespace,
                    key=key,
                )
                if record is not None and record.public_url:
                    return record.public_url
            raise

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

    async def discard_failed_object(
        self,
        *,
        purpose: StoragePurpose,
        namespace: str,
        key: str,
    ) -> None:
        """Remove a failed announcement upload without leaving an active record."""
        driver, resolved_purpose = self._resolve_target(purpose=purpose, profile_id="")
        if resolved_purpose is None:
            raise ObjectStorageError("storage_profile_missing", "Storage profile is not configured")
        normalized_ns = validate_namespace(namespace)
        record = self.find_object_record(
            profile_id=driver.profile_id,
            key=key,
            namespace=normalized_ns,
        )
        try:
            await driver.delete_object(key, namespace=normalized_ns)
        finally:
            if record is not None:
                if record.status == StorageObjectStatus.ACTIVE:
                    record = replace(record, status=StorageObjectStatus.PENDING, public_url="")
                    self._objects[record.id] = record
                await self._discard_pending_async(record.id)

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

    async def _resolve_read_target(
        self,
        *,
        purpose: StoragePurpose | None,
        profile_id: str,
        namespace: str,
        key: str,
    ) -> tuple[ObjectStorageDriver, str, str]:
        normalized_ns = validate_namespace(namespace)
        if profile_id:
            return self.driver_for_profile(profile_id), normalized_ns, key
        if purpose is None:
            raise ObjectStorageError(
                "storage_target_missing", "Either purpose or profile_id must be specified"
            )
        if self.repository is not None:
            record = await self.repository.find_active_object_by_purpose_and_key(
                purpose=purpose,
                key=key,
                namespace=normalized_ns,
            )
            if record is None:
                fallback = await self.repository.find_active_object_by_purpose_and_key(
                    purpose=purpose,
                    key=key,
                    namespace=None,
                )
                if fallback is not None and (not normalized_ns or not fallback.namespace):
                    record = fallback
            if record is not None and record.profile_id in self._drivers:
                return self._drivers[record.profile_id], record.namespace, record.key
        return self.driver_for_purpose(purpose), normalized_ns, key

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

    async def _register_pending_async(
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
        if self.repository is not None:
            existing = await self.repository.find_active_object(
                profile_id=profile_id,
                namespace=namespace,
                key=key,
            )
            if existing is not None and existing.status in {
                StorageObjectStatus.ACTIVE,
                StorageObjectStatus.PENDING,
            }:
                if existing.status == StorageObjectStatus.PENDING:
                    refreshed = StorageObjectRecord(
                        id=existing.id,
                        purpose=purpose,
                        profile_id=existing.profile_id,
                        backend=backend,
                        namespace=existing.namespace,
                        key=existing.key,
                        size_bytes=existing.size_bytes,
                        sha256=existing.sha256,
                        content_type=content_type or existing.content_type,
                        status=StorageObjectStatus.PENDING,
                        public_url=existing.public_url,
                        owner_type=owner_type or existing.owner_type,
                        owner_id=owner_id or existing.owner_id,
                        created_at=existing.created_at,
                        updated_at=utc_now_iso(),
                    )
                    await self.repository.upsert_object(refreshed)
                    existing = refreshed
                self._objects[existing.id] = existing
                self._object_index[(profile_id, namespace, key)] = existing.id
                return existing.id
        record_id = self._register_pending(
            purpose=purpose,
            profile_id=profile_id,
            backend=backend,
            namespace=namespace,
            key=key,
            content_type=content_type,
            owner_type=owner_type,
            owner_id=owner_id,
        )
        if self.repository is not None and record_id in self._objects:
            await self.repository.upsert_object(self._objects[record_id])
        return record_id

    async def _activate_record_async(self, record_id: str, stored: StoredObject) -> None:
        self._activate_record(record_id, stored)
        if self.repository is not None and record_id in self._objects:
            record = self._objects[record_id]
            if not record.public_url and record.purpose in {
                StoragePurpose.ARTIFACT_PUBLISHED,
                StoragePurpose.AVATAR,
            }:
                record = StorageObjectRecord(
                    id=record.id,
                    purpose=record.purpose,
                    profile_id=record.profile_id,
                    backend=record.backend,
                    namespace=record.namespace,
                    key=record.key,
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    content_type=record.content_type,
                    status=record.status,
                    public_url=f"/v1/storage/public/{record.id}",
                    owner_type=record.owner_type,
                    owner_id=record.owner_id,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
                self._objects[record_id] = record
            await self.repository.upsert_object(record)

    async def _discard_pending_async(self, record_id: str) -> None:
        existing = self._objects.get(record_id)
        was_pending = existing is not None and existing.status == StorageObjectStatus.PENDING
        self._discard_pending(record_id)
        if was_pending and self.repository is not None:
            await self.repository.delete_object_record(record_id)

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
