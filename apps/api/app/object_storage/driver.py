"""Unified object storage driver protocol."""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping
from pathlib import Path
from typing import Protocol

from .models import (
    ObjectListingEntry,
    ObjectRange,
    StorageBackend,
    StorageCapabilities,
    StoredObject,
)


class ObjectStorageDriver(Protocol):
    profile_id: str
    backend: StorageBackend

    def capabilities(self) -> StorageCapabilities: ...

    async def put_object(
        self,
        key: str,
        data: bytes | AsyncIterable[bytes],
        *,
        namespace: str = "",
        content_type: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
        if_absent: bool = True,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject: ...

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None: ...

    async def read_object(
        self,
        key: str,
        *,
        namespace: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> bytes: ...

    async def read_range(
        self,
        key: str,
        *,
        namespace: str = "",
        start_byte: int,
        max_bytes: int,
        max_object_bytes: int | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> ObjectRange: ...

    async def download_to_path(
        self,
        key: str,
        destination: Path,
        *,
        namespace: str = "",
    ) -> StoredObject: ...

    async def copy_object(
        self,
        source_key: str,
        target_key: str,
        *,
        source_namespace: str = "",
        target_namespace: str = "",
        expected_sha256: str = "",
        content_type: str = "",
        if_absent: bool = True,
    ) -> StoredObject: ...

    async def delete_object(self, key: str, *, namespace: str = "") -> None: ...

    def public_url(self, key: str, *, namespace: str = "") -> str: ...

    async def list_objects(
        self,
        *,
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]: ...
