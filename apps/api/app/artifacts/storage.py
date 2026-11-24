"""Artifact storage compatibility facade backed by the unified object_storage layer."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import ArtifactSettings
from ..object_storage import (
    ObjectStorageError,
    ObjectStorageService,
    StorageBackend,
    StoragePurpose,
    build_legacy_object_storage_service,
    build_local_legacy_object_storage_service,
)
from ..object_storage.models import (
    SAFE_PATH_SEGMENT,
    validate_path_segment as _validate_object_path_segment,
)

SAFE_SUFFIX = re.compile(r"^[a-f0-9]{8,12}$")
COPY_CHUNK_SIZE = 1024 * 1024


class ArtifactStorageError(ObjectStorageError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredObjectRange:
    content: bytes
    size_bytes: int
    sha256: str
    start_byte: int
    end_byte: int
    truncated: bool


class ArtifactStorage(Protocol):
    async def put_quarantine(
        self,
        stream: AsyncIterable[bytes],
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> StoredObject: ...

    async def download_quarantine(self, key: str, destination: Path) -> StoredObject: ...

    async def put_text_content(self, key: str, content: bytes) -> StoredObject: ...

    async def stat_text_content(self, key: str) -> StoredObject | None: ...

    async def read_text_content_range(
        self,
        key: str,
        *,
        start_byte: int,
        max_bytes: int,
        max_object_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> StoredObjectRange: ...

    async def read_text_content(
        self, key: str, max_bytes: int, expected_sha256: str = ""
    ) -> bytes: ...

    async def publish_if_absent(
        self, source_key: str, published_key: str, expected_sha256: str
    ) -> StoredObject: ...

    async def stat_published(self, key: str) -> StoredObject | None: ...

    async def delete_quarantine(self, key: str) -> None: ...

    async def revoke_published(self, key: str) -> None: ...

    def public_url(self, key: str) -> str: ...


def build_published_key(
    *,
    author_id: str,
    repo_name: str,
    version: str,
    plugin_name: str,
    suffix: str,
) -> str:
    segments = {
        "author_id": author_id,
        "repo_name": repo_name,
        "version": version,
        "plugin_name": plugin_name,
    }
    for label, value in segments.items():
        validate_path_segment(value, label)
    if not SAFE_SUFFIX.fullmatch(suffix):
        raise ArtifactStorageError("invalid_path_suffix", "Invalid artifact path suffix")
    filename = f"{plugin_name}-{version}-{suffix}.zip"
    return f"{author_id}/{repo_name}/{version}/{filename}"


def build_quarantine_key(artifact_id: str) -> str:
    validate_path_segment(artifact_id, "artifact_id")
    return f"artifacts/{artifact_id}/source.zip"


def build_content_key(artifact_id: str, file_id: str) -> str:
    validate_path_segment(artifact_id, "artifact_id")
    validate_path_segment(file_id, "file_id")
    return f"artifacts/{artifact_id}/files/{file_id}.txt"


def build_diff_key(artifact_id: str, diff_id: str) -> str:
    validate_path_segment(artifact_id, "artifact_id")
    validate_path_segment(diff_id, "diff_id")
    return f"artifacts/{artifact_id}/diffs/{diff_id}.json"


def validate_path_segment(value: str, label: str) -> None:
    try:
        _validate_object_path_segment(value, label)
    except ObjectStorageError as exc:
        raise ArtifactStorageError(exc.code, str(exc)) from exc


class ObjectArtifactStorage:
    """ArtifactStorage facade delegating to ObjectStorageService."""

    def __init__(
        self,
        object_storage: ObjectStorageService,
        *,
        quarantine_namespace: str = "",
        content_namespace: str = "",
        published_namespace: str = "",
    ) -> None:
        self.object_storage = object_storage
        self._quarantine_namespace = quarantine_namespace
        self._content_namespace = content_namespace
        self._published_namespace = published_namespace

    async def put_quarantine(
        self,
        stream: AsyncIterable[bytes],
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> StoredObject:
        try:
            stored = await self.object_storage.put_object(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                namespace=self._quarantine_namespace,
                key=key,
                data=stream,
                content_type="application/zip",
                max_bytes=max_bytes,
                expected_sha256=expected_sha256,
                if_absent=True,
                owner_type="artifact",
            )
        except ObjectStorageError as exc:
            raise _map_quarantine_error(exc) from exc
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def download_quarantine(self, key: str, destination: Path) -> StoredObject:
        try:
            stored = await self.object_storage.download_to_path(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                namespace=self._quarantine_namespace,
                key=key,
                destination=destination,
            )
        except ObjectStorageError as exc:
            raise _map_quarantine_error(exc) from exc
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def put_text_content(self, key: str, content: bytes) -> StoredObject:
        try:
            stored = await self.object_storage.put_object(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                namespace=self._content_namespace,
                key=key,
                data=content,
                if_absent=True,
                owner_type="artifact_content",
            )
        except ObjectStorageError as exc:
            raise _map_content_error(exc) from exc
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def stat_text_content(self, key: str) -> StoredObject | None:
        try:
            stored = await self.object_storage.stat_object(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                namespace=self._content_namespace,
                key=key,
            )
        except ObjectStorageError as exc:
            raise _map_content_error(exc) from exc
        if stored is None:
            return None
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def read_text_content_range(
        self,
        key: str,
        *,
        start_byte: int,
        max_bytes: int,
        max_object_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> StoredObjectRange:
        try:
            result = await self.object_storage.read_range(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                namespace=self._content_namespace,
                key=key,
                start_byte=start_byte,
                max_bytes=max_bytes,
                max_object_bytes=max_object_bytes,
                expected_size_bytes=expected_size_bytes,
                expected_sha256=expected_sha256,
            )
        except ObjectStorageError as exc:
            raise _map_content_error(exc) from exc
        return StoredObjectRange(
            content=result.content,
            size_bytes=result.size_bytes,
            sha256=result.sha256,
            start_byte=result.start_byte,
            end_byte=result.end_byte,
            truncated=result.truncated,
        )

    async def read_text_content(self, key: str, max_bytes: int, expected_sha256: str = "") -> bytes:
        result = await self.read_text_content_range(
            key,
            start_byte=0,
            max_bytes=max_bytes,
            max_object_bytes=max_bytes,
            expected_sha256=expected_sha256,
        )
        return result.content

    async def publish_if_absent(
        self, source_key: str, published_key: str, expected_sha256: str
    ) -> StoredObject:
        try:
            stored = await self.object_storage.copy_object(
                source_purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                source_namespace=self._quarantine_namespace,
                source_key=source_key,
                target_purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                target_namespace=self._published_namespace,
                target_key=published_key,
                expected_sha256=expected_sha256,
                content_type="application/zip",
                if_absent=True,
                owner_type="artifact_published",
            )
        except ObjectStorageError as exc:
            raise _map_publish_error(exc) from exc
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def stat_published(self, key: str) -> StoredObject | None:
        try:
            stored = await self.object_storage.stat_object(
                purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                namespace=self._published_namespace,
                key=key,
            )
        except ObjectStorageError as exc:
            raise ArtifactStorageError(exc.code, str(exc)) from exc
        if stored is None:
            return None
        return StoredObject(key=stored.key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def delete_quarantine(self, key: str) -> None:
        try:
            await self.object_storage.delete_object(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                namespace=self._quarantine_namespace,
                key=key,
            )
        except ObjectStorageError as exc:
            raise ArtifactStorageError(exc.code, str(exc)) from exc

    async def revoke_published(self, key: str) -> None:
        try:
            await self.object_storage.delete_object(
                purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                namespace=self._published_namespace,
                key=key,
            )
        except ObjectStorageError as exc:
            raise ArtifactStorageError(exc.code, str(exc)) from exc

    def public_url(self, key: str) -> str:
        try:
            return self.object_storage.public_url(
                purpose=StoragePurpose.ARTIFACT_PUBLISHED,
                namespace=self._published_namespace,
                key=key,
            )
        except ObjectStorageError as exc:
            if exc.code == "public_url_unconfigured":
                raise ArtifactStorageError(
                    "cdn_base_url_missing", "CDN base URL is not configured"
                ) from exc
            raise ArtifactStorageError(exc.code, str(exc)) from exc


class LocalArtifactStorage(ObjectArtifactStorage):
    def __init__(
        self,
        root: str | Path,
        cdn_base_url: str,
        *,
        master_key: str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.quarantine_root = self.root / "quarantine"
        self.content_root = self.root / "content"
        self.published_root = self.root / "published"
        for directory in (self.quarantine_root, self.content_root, self.published_root):
            directory.mkdir(parents=True, exist_ok=True)
        service = build_local_legacy_object_storage_service(
            self.root,
            self.cdn_base_url,
            master_key=master_key,
        )
        super().__init__(
            service,
            quarantine_namespace="quarantine",
            content_namespace="content",
            published_namespace="",
        )


class S3ArtifactStorage(ObjectArtifactStorage):
    def __init__(
        self,
        config: ArtifactSettings,
        client: Any | None = None,
        *,
        master_key: str | None = None,
    ) -> None:
        self.config = config
        self.cdn_base_url = config.cdn_base_url.rstrip("/")
        self.quarantine_bucket = config.quarantine_bucket
        self.published_bucket = config.published_bucket
        service = build_legacy_object_storage_service(
            config,
            s3_client=client,
            master_key=master_key,
        )
        quarantine_driver = service.driver_for_purpose(StoragePurpose.ARTIFACT_QUARANTINE)
        self.client = getattr(quarantine_driver, "client", client)
        super().__init__(
            service,
            quarantine_namespace="",
            content_namespace="",
            published_namespace="",
        )


def create_artifact_storage(
    config: ArtifactSettings,
    *,
    object_storage: ObjectStorageService | None = None,
    master_key: str | None = None,
) -> ArtifactStorage:
    if object_storage is not None:
        quarantine_binding = object_storage.get_binding(StoragePurpose.ARTIFACT_QUARANTINE)
        quarantine_profile = (
            object_storage.get_profile(quarantine_binding.profile_id)
            if quarantine_binding is not None
            else None
        )
        use_legacy_local_namespaces = bool(
            quarantine_profile is not None
            and quarantine_profile.legacy
            and quarantine_profile.backend == StorageBackend.LOCAL
        )
        return ObjectArtifactStorage(
            object_storage,
            quarantine_namespace="quarantine" if use_legacy_local_namespaces else "",
            content_namespace="content" if use_legacy_local_namespaces else "",
            published_namespace="",
        )
    if config.storage_backend == "local":
        return LocalArtifactStorage(
            config.local_root,
            config.cdn_base_url,
            master_key=master_key,
        )
    if config.storage_backend == "s3":
        return S3ArtifactStorage(config, master_key=master_key)
    raise ArtifactStorageError("storage_backend_invalid", "Unsupported artifact storage backend")


def _map_quarantine_error(exc: ObjectStorageError) -> ArtifactStorageError:
    if exc.code == "object_too_large":
        return ArtifactStorageError("archive_too_large", "Artifact exceeds size limit")
    if exc.code == "object_missing":
        return ArtifactStorageError("quarantine_object_missing", "Artifact object not found")
    if exc.code == "sha256_mismatch":
        return ArtifactStorageError("sha256_mismatch", "Artifact SHA-256 does not match")
    return ArtifactStorageError(exc.code, str(exc))


def _map_content_error(exc: ObjectStorageError) -> ArtifactStorageError:
    if exc.code == "object_missing":
        return ArtifactStorageError("content_object_missing", "Private content object not found")
    if exc.code == "object_too_large":
        return ArtifactStorageError("content_object_too_large", "Private content exceeds limit")
    if exc.code == "object_range_invalid":
        return ArtifactStorageError("content_range_invalid", "Private content range is invalid")
    if exc.code == "object_size_mismatch":
        return ArtifactStorageError("content_size_mismatch", "Private content size does not match")
    if exc.code == "sha256_mismatch":
        return ArtifactStorageError("sha256_mismatch", "Artifact SHA-256 does not match")
    return ArtifactStorageError(exc.code, str(exc))


def _map_publish_error(exc: ObjectStorageError) -> ArtifactStorageError:
    if exc.code == "object_missing":
        return ArtifactStorageError("quarantine_object_missing", "Artifact object not found")
    if exc.code == "object_key_conflict":
        return ArtifactStorageError(
            "published_key_conflict", "Object already exists with different content"
        )
    if exc.code == "sha256_mismatch":
        return ArtifactStorageError("sha256_mismatch", "Artifact SHA-256 does not match")
    return ArtifactStorageError(exc.code, str(exc))


__all__ = [
    "ArtifactStorage",
    "ArtifactStorageError",
    "COPY_CHUNK_SIZE",
    "LocalArtifactStorage",
    "ObjectArtifactStorage",
    "S3ArtifactStorage",
    "SAFE_PATH_SEGMENT",
    "SAFE_SUFFIX",
    "StoredObject",
    "StoredObjectRange",
    "build_content_key",
    "build_diff_key",
    "build_published_key",
    "build_quarantine_key",
    "create_artifact_storage",
    "validate_path_segment",
]
