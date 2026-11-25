"""Local filesystem driver implementing the unified ObjectStorageDriver contract."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from collections.abc import AsyncIterable, Mapping
from pathlib import Path, PurePosixPath

from .models import (
    ObjectListingEntry,
    ObjectRange,
    ObjectStorageError,
    StorageBackend,
    StorageCapabilities,
    StoredObject,
    build_public_url,
    guess_content_type,
    validate_expected_sha256,
    validate_expected_size,
    validate_namespace,
    validate_object_key,
    validate_profile_id,
    validate_range_params,
)

COPY_CHUNK_SIZE = 1024 * 1024


def validate_local_filesystem_root(
    root: str | Path,
    *,
    forbidden_roots: tuple[Path, ...] = (),
    forbidden_files: tuple[Path, ...] = (),
) -> Path:
    raw = str(root or "").strip()
    if not raw:
        raise ObjectStorageError("local_root_missing", "Local filesystem root is required")
    resolved = Path(raw).expanduser().resolve()
    for forbidden_root in forbidden_roots:
        root_path = forbidden_root.resolve()
        if resolved == root_path or root_path in resolved.parents:
            raise ObjectStorageError(
                "local_root_inside_forbidden",
                "Local filesystem root must be outside the source repository",
            )
    for forbidden_file in forbidden_files:
        file_path = forbidden_file.resolve()
        if resolved == file_path or resolved in file_path.parents:
            raise ObjectStorageError(
                "local_root_conflicts_with_market_state",
                "Local filesystem root must not contain the market state file",
            )
    return resolved


class LocalFilesystemDriver:
    backend = StorageBackend.LOCAL

    def __init__(
        self,
        *,
        profile_id: str,
        root: str | Path,
        public_base_url: str = "",
        file_mode: int = 0o640,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = validate_local_filesystem_root(root)
        self.public_base_url = str(public_base_url or "").strip().rstrip("/")
        self.file_mode = file_mode
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ObjectStorageError(
                "local_root_unavailable",
                f"Local filesystem root is unavailable: {exc}",
            ) from exc

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            conditional_create=True,
            range_read=True,
            public_read=bool(self.public_base_url),
            image_variants=False,
            list_objects=True,
        )

    def resolve_namespace_root(self, namespace: str = "") -> Path:
        normalized = validate_namespace(namespace)
        if not normalized:
            return self.root
        return _safe_path(self.root, normalized)

    def resolve_object_path(self, key: str, *, namespace: str = "") -> Path:
        base = self.resolve_namespace_root(namespace)
        return _safe_path(base, key)

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
    ) -> StoredObject:
        normalized_ns = validate_namespace(namespace)
        target = self.resolve_object_path(key, namespace=normalized_ns)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_type = guess_content_type(key, content_type)
        temporary = _temporary_path(prefix="obj-", suffix=".tmp", directory=target.parent)
        digest = hashlib.sha256()
        size = 0
        try:
            if isinstance(data, (bytes, bytearray, memoryview)):
                payload = bytes(data)
                size = len(payload)
                if max_bytes is not None and size > max_bytes:
                    raise ObjectStorageError("object_too_large", "Object exceeds size limit")
                digest.update(payload)
                sha256 = digest.hexdigest()
                validate_expected_sha256(expected_sha256, sha256)
                await asyncio.to_thread(temporary.write_bytes, payload)
            else:
                with temporary.open("wb") as handle:
                    async for chunk in data:
                        if not chunk:
                            continue
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise ObjectStorageError(
                                "object_too_large", "Object exceeds size limit"
                            )
                        digest.update(chunk)
                        await asyncio.to_thread(handle.write, chunk)
                sha256 = digest.hexdigest()
                validate_expected_sha256(expected_sha256, sha256)
            await asyncio.to_thread(
                _install_file,
                temporary,
                target,
                sha256=sha256,
                file_mode=self.file_mode,
                if_absent=if_absent,
            )
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size,
            sha256=sha256,
            content_type=resolved_type,
            public_url=self._optional_public_url(key, normalized_ns),
            metadata=dict(metadata or {}),
        )

    async def stat_object(self, key: str, *, namespace: str = "") -> StoredObject | None:
        normalized_ns = validate_namespace(namespace)
        target = self.resolve_object_path(key, namespace=normalized_ns)
        if not target.is_file():
            return None
        size, sha256 = await asyncio.to_thread(_file_size_and_sha256, target)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size,
            sha256=sha256,
            content_type=guess_content_type(key),
            public_url=self._optional_public_url(key, normalized_ns),
        )

    async def read_object(
        self,
        key: str,
        *,
        namespace: str = "",
        max_bytes: int | None = None,
        expected_sha256: str = "",
    ) -> bytes:
        normalized_ns = validate_namespace(namespace)
        source = self.resolve_object_path(key, namespace=normalized_ns)
        if not source.is_file():
            raise ObjectStorageError("object_missing", "Object not found")
        if max_bytes is not None and source.stat().st_size > max_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        content = await asyncio.to_thread(_read_file_limited, source, max_bytes)
        sha256 = hashlib.sha256(content).hexdigest()
        validate_expected_sha256(expected_sha256, sha256)
        return content

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
    ) -> ObjectRange:
        validate_range_params(start_byte, max_bytes, max_object_bytes)
        normalized_ns = validate_namespace(namespace)
        source = self.resolve_object_path(key, namespace=normalized_ns)
        if not source.is_file():
            raise ObjectStorageError("object_missing", "Object not found")
        size_bytes = source.stat().st_size
        if max_object_bytes is not None and size_bytes > max_object_bytes:
            raise ObjectStorageError("object_too_large", "Object exceeds limit")
        validate_expected_size(expected_size_bytes, size_bytes)
        if start_byte > size_bytes:
            raise ObjectStorageError("object_range_invalid", "Object range is invalid")
        end_byte = min(size_bytes, start_byte + max_bytes)
        if expected_sha256:
            _, sha256 = await asyncio.to_thread(_file_size_and_sha256, source)
            validate_expected_sha256(expected_sha256, sha256)
        else:
            sha256 = ""
        with source.open("rb") as handle:
            handle.seek(start_byte)
            content = await asyncio.to_thread(handle.read, end_byte - start_byte)
        if len(content) != end_byte - start_byte:
            raise ObjectStorageError("object_range_invalid", "Object range is invalid")
        return ObjectRange(
            content=content,
            size_bytes=size_bytes,
            sha256=sha256 or expected_sha256,
            content_type=guess_content_type(key),
            start_byte=start_byte,
            end_byte=end_byte,
            truncated=end_byte < size_bytes,
        )

    async def download_to_path(
        self,
        key: str,
        destination: Path,
        *,
        namespace: str = "",
    ) -> StoredObject:
        normalized_ns = validate_namespace(namespace)
        source = self.resolve_object_path(key, namespace=normalized_ns)
        if not source.is_file():
            raise ObjectStorageError("object_missing", "Object not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, destination)
        size, sha256 = await asyncio.to_thread(_file_size_and_sha256, destination)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_ns,
            key=key,
            size_bytes=size,
            sha256=sha256,
            content_type=guess_content_type(key),
            public_url=self._optional_public_url(key, normalized_ns),
        )

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
    ) -> StoredObject:
        normalized_src_ns = validate_namespace(source_namespace)
        normalized_dst_ns = validate_namespace(target_namespace)
        source = self.resolve_object_path(source_key, namespace=normalized_src_ns)
        target = self.resolve_object_path(target_key, namespace=normalized_dst_ns)
        if not source.is_file():
            raise ObjectStorageError("object_missing", "Source object not found")
        source_size, source_sha256 = await asyncio.to_thread(_file_size_and_sha256, source)
        validate_expected_sha256(expected_sha256, source_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            _copy_file,
            source,
            target,
            expected_sha256=source_sha256,
            file_mode=self.file_mode,
            if_absent=if_absent,
        )
        size, sha256 = await asyncio.to_thread(_file_size_and_sha256, target)
        return StoredObject(
            profile_id=self.profile_id,
            backend=self.backend.value,
            namespace=normalized_dst_ns,
            key=target_key,
            size_bytes=size,
            sha256=sha256,
            content_type=guess_content_type(target_key, content_type),
            public_url=self._optional_public_url(target_key, normalized_dst_ns),
        )

    async def delete_object(self, key: str, *, namespace: str = "") -> None:
        target = self.resolve_object_path(key, namespace=namespace)
        target.unlink(missing_ok=True)

    def public_url(self, key: str, *, namespace: str = "") -> str:
        normalized_ns = validate_namespace(namespace)
        full_key = f"{normalized_ns}/{key}" if normalized_ns else key
        return build_public_url(self.public_base_url, full_key)

    async def list_objects(
        self,
        *,
        namespace: str = "",
        prefix: str = "",
    ) -> list[ObjectListingEntry]:
        normalized_ns = validate_namespace(namespace)
        base = self.resolve_namespace_root(normalized_ns)
        if not base.is_dir():
            return []
        return await asyncio.to_thread(_walk_directory, base, normalized_ns, prefix)

    def _optional_public_url(self, key: str, namespace: str = "") -> str:
        if not self.public_base_url:
            return ""
        full_key = f"{namespace}/{key}" if namespace else key
        return build_public_url(self.public_base_url, full_key)


def _safe_path(root: Path, key: str) -> Path:
    validate_object_key(key)
    target = (root / PurePosixPath(key)).resolve()
    if target == root or root not in target.parents:
        raise ObjectStorageError("invalid_object_key", "Object key escapes storage root")
    return target


def _temporary_path(*, prefix: str, suffix: str, directory: Path | None = None) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(descriptor)
    return Path(name)


def _install_file(
    temporary: Path,
    target: Path,
    *,
    sha256: str,
    file_mode: int,
    if_absent: bool,
) -> None:
    # Separate API, worker, and runner processes share local files via their service group.
    temporary.chmod(file_mode)
    if not if_absent:
        os.replace(temporary, target)
        return
    try:
        os.link(temporary, target)
    except FileExistsError:
        _, existing_sha = _file_size_and_sha256(target)
        if existing_sha != sha256:
            raise ObjectStorageError(
                "object_key_conflict", "Object already exists with different content"
            ) from None


def _copy_file(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    file_mode: int,
    if_absent: bool,
) -> None:
    if not if_absent:
        temporary = _temporary_path(prefix="copy-", suffix=".tmp", directory=target.parent)
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(file_mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode)
    except FileExistsError:
        _, existing_sha = _file_size_and_sha256(target)
        if existing_sha != expected_sha256:
            raise ObjectStorageError(
                "object_key_conflict", "Object already exists with different content"
            ) from None
        return
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, COPY_CHUNK_SIZE)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _file_size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _read_file_limited(path: Path, max_bytes: int | None) -> bytes:
    with path.open("rb") as handle:
        if max_bytes is None:
            return handle.read()
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ObjectStorageError("object_too_large", "Object exceeds limit")
    return content


def _walk_directory(base: Path, namespace: str, prefix: str) -> list[ObjectListingEntry]:
    entries: list[ObjectListingEntry] = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name.endswith(".tmp"):
            continue
        rel_key = path.relative_to(base).as_posix()
        if prefix and not rel_key.startswith(prefix):
            continue
        size = path.stat().st_size
        sha256 = ""
        entries.append(
            ObjectListingEntry(
                namespace=namespace,
                key=rel_key,
                size_bytes=size,
                sha256=sha256,
                content_type=guess_content_type(rel_key),
            )
        )
    return entries
