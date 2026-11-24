"""Unified object storage models, validation, and content-type helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_CONTENT_TYPE_BY_SUFFIX: dict[str, str] = {
    ".zip": "application/zip",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


class StorageBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"
    WEBDAV = "webdav"
    IMGBED = "imgbed"


class StorageProfileStatus(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    RETIRED = "retired"
    REMOVED = "removed"


class StoragePurpose(StrEnum):
    ARTIFACT_QUARANTINE = "artifact-quarantine"
    ARTIFACT_PUBLISHED = "artifact-published"
    AVATAR = "avatar"


class StorageObjectStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DELETED = "deleted"


PURPOSE_ALLOWED_BACKENDS: dict[StoragePurpose, frozenset[StorageBackend]] = {
    StoragePurpose.ARTIFACT_QUARANTINE: frozenset(
        {StorageBackend.LOCAL, StorageBackend.S3, StorageBackend.WEBDAV}
    ),
    StoragePurpose.ARTIFACT_PUBLISHED: frozenset(
        {StorageBackend.LOCAL, StorageBackend.S3, StorageBackend.WEBDAV, StorageBackend.IMGBED}
    ),
    StoragePurpose.AVATAR: frozenset(
        {StorageBackend.LOCAL, StorageBackend.S3, StorageBackend.WEBDAV, StorageBackend.IMGBED}
    ),
}


class ObjectStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StorageCapabilities:
    conditional_create: bool = False
    range_read: bool = False
    public_read: bool = False
    image_variants: bool = False
    list_objects: bool = False


@dataclass(frozen=True, slots=True)
class StoredObject:
    profile_id: str
    backend: str
    namespace: str
    key: str
    size_bytes: int
    sha256: str
    content_type: str = "application/octet-stream"
    public_url: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObjectRange:
    content: bytes
    size_bytes: int
    sha256: str
    content_type: str
    start_byte: int
    end_byte: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ObjectListingEntry:
    namespace: str
    key: str
    size_bytes: int
    sha256: str = ""
    content_type: str = ""


@dataclass(frozen=True, slots=True)
class StorageProfile:
    id: str
    name: str
    backend: StorageBackend
    status: StorageProfileStatus = StorageProfileStatus.ACTIVE
    config: Mapping[str, Any] = field(default_factory=dict)
    secret_ref: str = ""
    legacy: bool = False

    def __post_init__(self) -> None:
        validate_profile_id(self.id)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "backend": self.backend.value,
            "status": self.status.value,
            "config": dict(self.config),
            "has_secret": bool(self.secret_ref),
            "legacy": self.legacy,
        }


@dataclass(frozen=True, slots=True)
class StorageBinding:
    purpose: StoragePurpose
    profile_id: str
    generation: int = 1
    updated_by: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class StorageObjectRecord:
    id: str
    purpose: StoragePurpose
    profile_id: str
    backend: str
    namespace: str
    key: str
    size_bytes: int
    sha256: str
    content_type: str
    status: StorageObjectStatus
    public_url: str = ""
    owner_type: str = ""
    owner_id: str = ""
    created_at: str = ""
    updated_at: str = ""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_profile_id(profile_id: str) -> str:
    cleaned = str(profile_id or "").strip()
    if not SAFE_PROFILE_ID.fullmatch(cleaned):
        raise ObjectStorageError("invalid_profile_id", f"Invalid storage profile id: {profile_id}")
    return cleaned


def validate_path_segment(value: str, label: str) -> None:
    if value in {"", ".", ".."} or not SAFE_PATH_SEGMENT.fullmatch(value):
        raise ObjectStorageError("invalid_path_segment", f"Invalid {label}")


def validate_object_key(key: str) -> None:
    path = PurePosixPath(key)
    if not key or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ObjectStorageError("invalid_object_key", "Invalid object key")
    for part in path.parts:
        validate_path_segment(part, "object_key")


def validate_namespace(namespace: str) -> str:
    if not namespace:
        return ""
    path = PurePosixPath(namespace)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ObjectStorageError("invalid_namespace", "Invalid storage namespace")
    for part in path.parts:
        validate_path_segment(part, "namespace")
    return "/".join(path.parts)


def guess_content_type(key: str, explicit: str = "") -> str:
    cleaned = str(explicit or "").strip()
    if cleaned:
        return cleaned
    suffix = PurePosixPath(key).suffix.lower()
    return _CONTENT_TYPE_BY_SUFFIX.get(suffix, "application/octet-stream")


def build_public_url(
    base_url: str, key: str, *, missing_code: str = "public_url_unconfigured"
) -> str:
    cleaned_base = str(base_url or "").strip().rstrip("/")
    if not cleaned_base:
        raise ObjectStorageError(missing_code, "Public delivery base URL is not configured")
    validate_object_key(key)
    encoded = "/".join(quote(part, safe="._-") for part in PurePosixPath(key).parts)
    return f"{cleaned_base}/{encoded}"


def validate_range_params(
    start_byte: int,
    max_bytes: int,
    max_object_bytes: int | None = None,
) -> None:
    if (
        not isinstance(start_byte, int)
        or isinstance(start_byte, bool)
        or start_byte < 0
        or not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes <= 0
    ):
        raise ObjectStorageError("object_range_invalid", "Object range is invalid")
    if max_object_bytes is not None and (
        not isinstance(max_object_bytes, int)
        or isinstance(max_object_bytes, bool)
        or max_object_bytes <= 0
        or max_bytes > max_object_bytes
    ):
        raise ObjectStorageError("object_range_invalid", "Object range is invalid")


def validate_expected_size(expected: int | None, actual: int) -> None:
    if expected is None:
        return
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        raise ObjectStorageError("object_size_mismatch", "Object size is invalid")
    if expected != actual:
        raise ObjectStorageError("object_size_mismatch", "Object size does not match")


def validate_expected_sha256(expected: str, actual: str) -> None:
    if expected and expected != actual:
        raise ObjectStorageError("sha256_mismatch", "Object SHA-256 does not match")


def slice_object_range(
    content: bytes,
    *,
    sha256: str,
    content_type: str,
    start_byte: int,
    max_bytes: int,
) -> ObjectRange:
    if start_byte > len(content):
        raise ObjectStorageError("object_range_invalid", "Object range is invalid")
    end_byte = min(len(content), start_byte + max_bytes)
    return ObjectRange(
        content=content[start_byte:end_byte],
        size_bytes=len(content),
        sha256=sha256,
        content_type=content_type,
        start_byte=start_byte,
        end_byte=end_byte,
        truncated=end_byte < len(content),
    )
