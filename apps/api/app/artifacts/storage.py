from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

from botocore.exceptions import ClientError

from ..config import ArtifactSettings

SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_SUFFIX = re.compile(r"^[a-f0-9]{8,12}$")
COPY_CHUNK_SIZE = 1024 * 1024


class ArtifactStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


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
    if value in {"", ".", ".."} or not SAFE_PATH_SEGMENT.fullmatch(value):
        raise ArtifactStorageError("invalid_path_segment", f"Invalid {label}")


class LocalArtifactStorage:
    def __init__(self, root: str | Path, cdn_base_url: str) -> None:
        self.root = Path(root).resolve()
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.quarantine_root = self.root / "quarantine"
        self.content_root = self.root / "content"
        self.published_root = self.root / "published"
        for directory in (self.quarantine_root, self.content_root, self.published_root):
            directory.mkdir(parents=True, exist_ok=True)

    async def put_quarantine(
        self,
        stream: AsyncIterable[bytes],
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> StoredObject:
        target = _safe_path(self.quarantine_root, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_path(prefix="upload-", suffix=".tmp", directory=target.parent)
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as handle:
                async for chunk in stream:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArtifactStorageError(
                            "archive_too_large", "Artifact exceeds size limit"
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            sha256 = digest.hexdigest()
            _validate_expected_sha(expected_sha256, sha256)
            await asyncio.to_thread(_install_file_if_absent, temporary, target, sha256)
            return StoredObject(key=key, size_bytes=size, sha256=sha256)
        finally:
            temporary.unlink(missing_ok=True)

    async def download_quarantine(self, key: str, destination: Path) -> StoredObject:
        source = _safe_path(self.quarantine_root, key)
        if not source.is_file():
            raise ArtifactStorageError("quarantine_object_missing", "Artifact object not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, destination)
        return await asyncio.to_thread(_file_stat, destination, key)

    async def put_text_content(self, key: str, content: bytes) -> StoredObject:
        target = _safe_path(self.content_root, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        temporary = _temporary_path(prefix="content-", suffix=".tmp", directory=target.parent)
        try:
            await asyncio.to_thread(temporary.write_bytes, content)
            await asyncio.to_thread(_install_file_if_absent, temporary, target, digest)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(key=key, size_bytes=len(content), sha256=digest)

    async def read_text_content(self, key: str, max_bytes: int, expected_sha256: str = "") -> bytes:
        source = _safe_path(self.content_root, key)
        if not source.is_file():
            raise ArtifactStorageError("content_object_missing", "Private content object not found")
        if source.stat().st_size > max_bytes:
            raise ArtifactStorageError("content_object_too_large", "Private content exceeds limit")
        content = await asyncio.to_thread(_read_file_limited, source, max_bytes)
        _validate_expected_sha(expected_sha256, hashlib.sha256(content).hexdigest())
        return content

    async def publish_if_absent(
        self, source_key: str, published_key: str, expected_sha256: str
    ) -> StoredObject:
        source = _safe_path(self.quarantine_root, source_key)
        target = _safe_path(self.published_root, published_key)
        if not source.is_file():
            raise ArtifactStorageError("quarantine_object_missing", "Artifact object not found")
        source_stat = await asyncio.to_thread(_file_stat, source, source_key)
        _validate_expected_sha(expected_sha256, source_stat.sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_copy_file_if_absent, source, target, expected_sha256)
        return await asyncio.to_thread(_file_stat, target, published_key)

    async def stat_published(self, key: str) -> StoredObject | None:
        target = _safe_path(self.published_root, key)
        if not target.is_file():
            return None
        return await asyncio.to_thread(_file_stat, target, key)

    async def delete_quarantine(self, key: str) -> None:
        _safe_path(self.quarantine_root, key).unlink(missing_ok=True)

    async def revoke_published(self, key: str) -> None:
        _safe_path(self.published_root, key).unlink(missing_ok=True)

    def public_url(self, key: str) -> str:
        return _public_url(self.cdn_base_url, key)


class S3ArtifactStorage:
    def __init__(self, config: ArtifactSettings, client: Any | None = None) -> None:
        self.config = config
        self.cdn_base_url = config.cdn_base_url.rstrip("/")
        self.quarantine_bucket = config.quarantine_bucket
        self.published_bucket = config.published_bucket
        self.client = client or self._create_client()

    def _create_client(self) -> Any:
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.config.s3_endpoint_url,
            region_name=self.config.s3_region,
            aws_access_key_id=self.config.s3_access_key_id,
            aws_secret_access_key=self.config.s3_secret_access_key,
        )

    async def put_quarantine(
        self,
        stream: AsyncIterable[bytes],
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> StoredObject:
        _validate_object_key(key)
        temporary, stored = await _stream_to_temporary(stream, max_bytes, expected_sha256)
        try:
            await asyncio.to_thread(
                self._put_file_if_absent,
                self.quarantine_bucket,
                key,
                temporary,
                stored,
            )
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(key=key, size_bytes=stored.size_bytes, sha256=stored.sha256)

    async def download_quarantine(self, key: str, destination: Path) -> StoredObject:
        _validate_object_key(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.client.download_file,
            self.quarantine_bucket,
            key,
            str(destination),
        )
        return await asyncio.to_thread(_file_stat, destination, key)

    async def put_text_content(self, key: str, content: bytes) -> StoredObject:
        _validate_object_key(key)
        digest = hashlib.sha256(content).hexdigest()
        stored = StoredObject(key=key, size_bytes=len(content), sha256=digest)
        await asyncio.to_thread(
            self._put_bytes_if_absent,
            self.quarantine_bucket,
            key,
            content,
            stored,
        )
        return stored

    async def read_text_content(self, key: str, max_bytes: int, expected_sha256: str = "") -> bytes:
        _validate_object_key(key)
        stored = await asyncio.to_thread(self._head_object, self.quarantine_bucket, key)
        if stored is None:
            raise ArtifactStorageError("content_object_missing", "Private content object not found")
        if stored.size_bytes > max_bytes:
            raise ArtifactStorageError("content_object_too_large", "Private content exceeds limit")
        if expected_sha256 and stored.sha256:
            _validate_expected_sha(expected_sha256, stored.sha256)
        response = await asyncio.to_thread(
            self.client.get_object,
            Bucket=self.quarantine_bucket,
            Key=key,
        )
        body = response["Body"]
        content = await asyncio.to_thread(body.read, max_bytes + 1)
        close = getattr(body, "close", None)
        if close:
            close()
        if len(content) > max_bytes:
            raise ArtifactStorageError("content_object_too_large", "Private content exceeds limit")
        _validate_expected_sha(expected_sha256, hashlib.sha256(content).hexdigest())
        return content

    async def publish_if_absent(
        self, source_key: str, published_key: str, expected_sha256: str
    ) -> StoredObject:
        _validate_object_key(source_key)
        _validate_object_key(published_key)
        temporary = _temporary_path(prefix="artifact-publish-", suffix=".zip")
        try:
            source = await self.download_quarantine(source_key, temporary)
            _validate_expected_sha(expected_sha256, source.sha256)
            stored = StoredObject(
                key=published_key,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
            )
            await asyncio.to_thread(
                self._put_file_if_absent,
                self.published_bucket,
                published_key,
                temporary,
                stored,
            )
            return stored
        finally:
            temporary.unlink(missing_ok=True)

    async def stat_published(self, key: str) -> StoredObject | None:
        _validate_object_key(key)
        return await asyncio.to_thread(self._head_object, self.published_bucket, key)

    async def delete_quarantine(self, key: str) -> None:
        _validate_object_key(key)
        await asyncio.to_thread(
            self.client.delete_object,
            Bucket=self.quarantine_bucket,
            Key=key,
        )

    async def revoke_published(self, key: str) -> None:
        _validate_object_key(key)
        await asyncio.to_thread(
            self.client.delete_object,
            Bucket=self.published_bucket,
            Key=key,
        )

    def public_url(self, key: str) -> str:
        return _public_url(self.cdn_base_url, key)

    def _put_file_if_absent(self, bucket: str, key: str, path: Path, stored: StoredObject) -> None:
        with path.open("rb") as handle:
            self._put_body_if_absent(bucket, key, handle, stored)

    def _put_bytes_if_absent(
        self, bucket: str, key: str, content: bytes, stored: StoredObject
    ) -> None:
        self._put_body_if_absent(bucket, key, content, stored)

    def _put_body_if_absent(self, bucket: str, key: str, body: Any, stored: StoredObject) -> None:
        try:
            self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/zip" if key.endswith(".zip") else "text/plain",
                Metadata={"sha256": stored.sha256},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status not in {409, 412} and code not in {
                "ConditionalRequestConflict",
                "PreconditionFailed",
            }:
                raise
            existing = self._head_object(bucket, key)
            if not existing or existing.sha256 != stored.sha256:
                raise ArtifactStorageError(
                    "published_key_conflict", "Object already exists with different content"
                ) from exc

    def _head_object(self, bucket: str, key: str) -> StoredObject | None:
        try:
            result = self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return StoredObject(
            key=key,
            size_bytes=int(result.get("ContentLength") or 0),
            sha256=str((result.get("Metadata") or {}).get("sha256") or ""),
        )


def create_artifact_storage(config: ArtifactSettings) -> ArtifactStorage:
    if config.storage_backend == "local":
        return LocalArtifactStorage(config.local_root, config.cdn_base_url)
    if config.storage_backend == "s3":
        return S3ArtifactStorage(config)
    raise ArtifactStorageError("storage_backend_invalid", "Unsupported artifact storage backend")


async def _stream_to_temporary(
    stream: AsyncIterable[bytes], max_bytes: int, expected_sha256: str
) -> tuple[Path, StoredObject]:
    temporary = _temporary_path(prefix="artifact-upload-", suffix=".tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in stream:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ArtifactStorageError("archive_too_large", "Artifact exceeds size limit")
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
        sha256 = digest.hexdigest()
        _validate_expected_sha(expected_sha256, sha256)
        return temporary, StoredObject(key="", size_bytes=size, sha256=sha256)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _safe_path(root: Path, key: str) -> Path:
    _validate_object_key(key)
    target = (root / PurePosixPath(key)).resolve()
    if target == root or root not in target.parents:
        raise ArtifactStorageError("invalid_object_key", "Object key escapes storage root")
    return target


def _temporary_path(*, prefix: str, suffix: str, directory: Path | None = None) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(descriptor)
    return Path(name)


def _validate_object_key(key: str) -> None:
    path = PurePosixPath(key)
    if not key or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactStorageError("invalid_object_key", "Invalid object key")
    for part in path.parts:
        validate_path_segment(part, "object_key")


def _public_url(base_url: str, key: str) -> str:
    if not base_url:
        raise ArtifactStorageError("cdn_base_url_missing", "CDN base URL is not configured")
    _validate_object_key(key)
    encoded = "/".join(quote(part, safe="._-") for part in PurePosixPath(key).parts)
    return f"{base_url}/{encoded}"


def _install_file_if_absent(temporary: Path, target: Path, expected_sha256: str) -> None:
    try:
        os.link(temporary, target)
    except FileExistsError:
        existing = _file_stat(target, str(target))
        if existing.sha256 != expected_sha256:
            raise ArtifactStorageError(
                "object_key_conflict", "Object already exists with different content"
            ) from None


def _copy_file_if_absent(source: Path, target: Path, expected_sha256: str) -> None:
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:
        existing = _file_stat(target, str(target))
        if existing.sha256 != expected_sha256:
            raise ArtifactStorageError(
                "published_key_conflict", "Object already exists with different content"
            ) from None
        return
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, COPY_CHUNK_SIZE)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _file_stat(path: Path, key: str) -> StoredObject:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return StoredObject(key=key, size_bytes=size, sha256=digest.hexdigest())


def _read_file_limited(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ArtifactStorageError("content_object_too_large", "Private content exceeds limit")
    return content


def _validate_expected_sha(expected: str, actual: str) -> None:
    if expected and expected != actual:
        raise ArtifactStorageError("sha256_mismatch", "Artifact SHA-256 does not match")
