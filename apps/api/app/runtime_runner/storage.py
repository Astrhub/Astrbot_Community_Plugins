from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

_OBJECT_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RuntimeResultStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeStoredResult:
    key: str
    size_bytes: int
    sha256: str


@runtime_checkable
class RuntimeResultWriter(Protocol):
    async def put_result(
        self,
        key: str,
        content: bytes,
        *,
        max_bytes: int,
    ) -> RuntimeStoredResult: ...


@runtime_checkable
class RuntimeResultReader(Protocol):
    async def read_text_content(
        self,
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> bytes: ...


class LocalRuntimeResultWriter:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    async def put_result(
        self,
        key: str,
        content: bytes,
        *,
        max_bytes: int,
    ) -> RuntimeStoredResult:
        return await asyncio.to_thread(self._put_result, key, content, max_bytes)

    async def read_text_content(
        self,
        key: str,
        max_bytes: int,
        expected_sha256: str = "",
    ) -> bytes:
        return await asyncio.to_thread(
            self._read_text_content,
            key,
            max_bytes,
            expected_sha256,
        )

    def _read_text_content(
        self,
        key: str,
        max_bytes: int,
        expected_sha256: str,
    ) -> bytes:
        if max_bytes < 1:
            raise RuntimeResultStorageError(
                "runtime_result_too_large",
                "Runtime result read limit is invalid",
            )
        destination = self._destination(key)
        try:
            resolved_root = self.root.resolve(strict=True)
            resolved_parent = destination.parent.resolve(strict=True)
            resolved_parent.relative_to(resolved_root)
        except FileNotFoundError as exc:
            raise RuntimeResultStorageError(
                "content_object_missing",
                "Runtime private object is not available",
            ) from exc
        except (OSError, ValueError) as exc:
            raise RuntimeResultStorageError(
                "runtime_result_object_invalid",
                "Runtime private object parent cannot be resolved safely",
            ) from exc
        try:
            descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise RuntimeResultStorageError(
                "content_object_missing",
                "Runtime private object is not available",
            ) from exc
        except OSError as exc:
            raise RuntimeResultStorageError(
                "runtime_result_object_invalid",
                "Runtime private object cannot be opened safely",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise RuntimeResultStorageError(
                    "runtime_result_too_large",
                    "Runtime private object exceeds its configured byte limit",
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(max_bytes + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) > max_bytes:
            raise RuntimeResultStorageError(
                "runtime_result_too_large",
                "Runtime private object exceeds its configured byte limit",
            )
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise RuntimeResultStorageError(
                "runtime_result_sha256_mismatch",
                "Runtime private object digest does not match its signed reference",
            )
        return content

    def _put_result(self, key: str, content: bytes, max_bytes: int) -> RuntimeStoredResult:
        if max_bytes < 1 or len(content) > max_bytes:
            raise RuntimeResultStorageError(
                "runtime_result_too_large",
                "Runtime result exceeds its configured byte limit",
            )
        destination = self._destination(key)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _is_within(destination.parent.resolve(), self.root):
            raise RuntimeResultStorageError(
                "runtime_result_key_invalid",
                "Runtime result key escapes the configured root",
            )
        digest = hashlib.sha256(content).hexdigest()
        try:
            existing = destination.read_bytes()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != digest or existing != content:
                raise RuntimeResultStorageError(
                    "runtime_result_conflict",
                    "Runtime result key already contains different content",
                )
            return RuntimeStoredResult(key=key, size_bytes=len(existing), sha256=digest)

        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing = destination.read_bytes()
                if hashlib.sha256(existing).hexdigest() != digest or existing != content:
                    raise RuntimeResultStorageError(
                        "runtime_result_conflict",
                        "Runtime result key was concurrently written with different content",
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
        return RuntimeStoredResult(key=key, size_bytes=len(content), sha256=digest)

    def _destination(self, key: str) -> Path:
        if not key or len(key) > 512 or key.startswith("/") or "\\" in key:
            raise RuntimeResultStorageError(
                "runtime_result_key_invalid",
                "Runtime result key is not a valid relative object key",
            )
        parts = key.split("/")
        if any(
            part in {"", ".", ".."} or not _OBJECT_KEY_SEGMENT.fullmatch(part) for part in parts
        ):
            raise RuntimeResultStorageError(
                "runtime_result_key_invalid",
                "Runtime result key contains an unsupported path segment",
            )
        destination = self.root.joinpath(*parts)
        if not _is_within(destination, self.root):
            raise RuntimeResultStorageError(
                "runtime_result_key_invalid",
                "Runtime result key escapes the configured root",
            )
        return destination


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
