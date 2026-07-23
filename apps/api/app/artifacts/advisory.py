from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol, Self, runtime_checkable
from urllib.parse import urlsplit

import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ADVISORY_SCHEMA_VERSION = "1"
ADVISORY_ADAPTER_VERSION = "dependency-advisory-v1"
MAX_ADVISORY_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_ADVISORY_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ADVISORY_QUERY_PACKAGES = 5000
MAX_ADVISORY_FUTURE_SKEW = timedelta(minutes=5)

_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_SEVERITIES = {"info", "low", "medium", "high", "critical"}


class AdvisoryStatus(StrEnum):
    OK = "ok"
    STALE = "stale"
    NOT_QUERIED = "not_queried"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DependencyPackage:
    name: str
    version: str

    def __post_init__(self) -> None:
        name = canonicalize_name(self.name)
        try:
            Version(self.version)
        except InvalidVersion as exc:
            raise ValueError("dependency_package_version_invalid") from exc
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class DependencyAdvisory:
    advisory_id: str
    package: str
    version: str
    affected: str
    fixed_versions: tuple[str, ...]
    severity: str
    withdrawn: bool = False


@dataclass(frozen=True, slots=True)
class DependencyPackageMetadata:
    package: str
    version: str
    license_expression: str = ""
    withdrawn: bool = False


@dataclass(frozen=True, slots=True)
class AdvisoryQueryResult:
    status: AdvisoryStatus
    database_version: str = ""
    source: str = ""
    generated_at: str = ""
    queried_at: str = ""
    snapshot_sha256: str = ""
    advisories: tuple[DependencyAdvisory, ...] = ()
    packages: tuple[DependencyPackageMetadata, ...] = ()
    error_code: str = ""

    def __post_init__(self) -> None:
        if self.status in {AdvisoryStatus.OK, AdvisoryStatus.STALE}:
            if (
                not _PUBLIC_ID.fullmatch(self.database_version)
                or not _PUBLIC_ID.fullmatch(self.source)
                or not _is_timestamp(self.generated_at)
                or not _is_timestamp(self.queried_at)
                or not re.fullmatch(r"[a-f0-9]{64}", self.snapshot_sha256)
                or self.error_code
            ):
                raise ValueError("dependency_advisory_snapshot_invalid")
        elif not _ERROR_CODE.fullmatch(self.error_code):
            raise ValueError("dependency_advisory_error_code_invalid")


@runtime_checkable
class DependencyAdvisoryProvider(Protocol):
    version: str
    config_ref: str
    ready: bool
    unavailable_reason: str

    async def query(
        self,
        packages: Sequence[DependencyPackage],
        *,
        max_age_hours: int,
    ) -> AdvisoryQueryResult: ...

    async def close(self) -> None: ...


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _SnapshotAdvisory(_SnapshotModel):
    id: str = Field(min_length=1, max_length=160)
    package: str = Field(min_length=1, max_length=128)
    affected: str = Field(min_length=1, max_length=256)
    fixed_versions: tuple[str, ...] = Field(default=(), max_length=100)
    severity: str = Field(min_length=3, max_length=8)
    withdrawn: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PUBLIC_ID.fullmatch(value):
            raise ValueError("advisory id must be a public identifier")
        return value

    @field_validator("package")
    @classmethod
    def normalize_package(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("affected")
    @classmethod
    def validate_affected(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError("advisory affected range is invalid") from exc
        return value

    @field_validator("fixed_versions")
    @classmethod
    def validate_fixed_versions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            Version(item)
        return tuple(sorted(set(value), key=Version))

    @field_validator("severity")
    @classmethod
    def normalize_severity(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in _SEVERITIES:
            raise ValueError("advisory severity is invalid")
        return normalized


class _SnapshotPackage(_SnapshotModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    license: str = Field(default="", max_length=160)
    withdrawn: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        Version(value)
        return value

    @field_validator("license")
    @classmethod
    def validate_license(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if normalized and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+()\- ]*", normalized):
            raise ValueError("package license expression is invalid")
        return normalized


class _SnapshotEnvelope(_SnapshotModel):
    schema_version: str
    database_version: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=160)
    generated_at: str = Field(min_length=20, max_length=40)
    advisories: tuple[_SnapshotAdvisory, ...] = Field(default=(), max_length=100_000)
    packages: tuple[_SnapshotPackage, ...] = Field(default=(), max_length=100_000)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != ADVISORY_SCHEMA_VERSION:
            raise ValueError("advisory schema version is unsupported")
        return value

    @field_validator("database_version", "source")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not _PUBLIC_ID.fullmatch(value):
            raise ValueError("advisory snapshot identifier is invalid")
        return value

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: str) -> str:
        return _timestamp(value)

    @model_validator(mode="after")
    def validate_unique_records(self) -> Self:
        advisory_keys = [(item.id, item.package) for item in self.advisories]
        package_keys = [(item.name, item.version) for item in self.packages]
        if len(advisory_keys) != len(set(advisory_keys)):
            raise ValueError("advisory snapshot contains duplicate advisories")
        if len(package_keys) != len(set(package_keys)):
            raise ValueError("advisory snapshot contains duplicate package metadata")
        return self


class UnavailableDependencyAdvisoryProvider:
    version = ADVISORY_ADAPTER_VERSION
    ready = False

    def __init__(
        self,
        reason: str = "dependency_advisory_unavailable",
        *,
        config_ref: str = "config:dependency-unavailable",
    ) -> None:
        if not _ERROR_CODE.fullmatch(reason):
            raise ValueError("dependency_advisory_unavailable_reason_invalid")
        self.config_ref = config_ref
        self.unavailable_reason = reason

    async def query(
        self,
        packages: Sequence[DependencyPackage],
        *,
        max_age_hours: int,
    ) -> AdvisoryQueryResult:
        return AdvisoryQueryResult(
            AdvisoryStatus.NOT_QUERIED,
            queried_at=_now(),
            error_code=self.unavailable_reason,
        )

    async def close(self) -> None:
        return None


class LocalDependencyAdvisoryProvider:
    def __init__(
        self,
        content: bytes,
        *,
        config_ref: str = "config:dependency-default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not content or len(content) > MAX_ADVISORY_SNAPSHOT_BYTES:
            raise ValueError("dependency_advisory_snapshot_size_invalid")
        self.snapshot_sha256 = hashlib.sha256(content).hexdigest()
        self.snapshot = _SnapshotEnvelope.model_validate_json(content)
        self.config_ref = config_ref
        self.version = f"{ADVISORY_ADAPTER_VERSION}:{self.snapshot_sha256[:16]}"
        self.ready = True
        self.unavailable_reason = ""
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        config_ref: str = "config:dependency-default",
        clock: Callable[[], datetime] | None = None,
    ) -> LocalDependencyAdvisoryProvider:
        source = Path(path)
        if not source.is_absolute():
            raise ValueError("dependency_advisory_snapshot_path_invalid")
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ValueError("dependency_advisory_snapshot_path_invalid") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size < 1
                or metadata.st_size > MAX_ADVISORY_SNAPSHOT_BYTES
            ):
                raise ValueError("dependency_advisory_snapshot_size_invalid")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(MAX_ADVISORY_SNAPSHOT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return cls(content, config_ref=config_ref, clock=clock)

    async def query(
        self,
        packages: Sequence[DependencyPackage],
        *,
        max_age_hours: int,
    ) -> AdvisoryQueryResult:
        if not packages or len(packages) > MAX_ADVISORY_QUERY_PACKAGES:
            return AdvisoryQueryResult(
                AdvisoryStatus.NOT_QUERIED,
                queried_at=_now(),
                error_code="dependency_package_snapshot_invalid",
            )
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("dependency_advisory_clock_invalid")
        now = now.astimezone(UTC)
        queried_at = now.isoformat().replace("+00:00", "Z")
        package_index = {(item.name, item.version): item for item in packages}
        packages_by_name: dict[str, list[DependencyPackage]] = {}
        for package in packages:
            packages_by_name.setdefault(package.name, []).append(package)
        advisories: list[DependencyAdvisory] = []
        try:
            for record in self.snapshot.advisories:
                for package in packages_by_name.get(record.package, ()):
                    if SpecifierSet(record.affected).contains(
                        Version(package.version),
                        prereleases=True,
                    ):
                        advisories.append(
                            DependencyAdvisory(
                                advisory_id=record.id,
                                package=package.name,
                                version=package.version,
                                affected=record.affected,
                                fixed_versions=record.fixed_versions,
                                severity=record.severity,
                                withdrawn=record.withdrawn,
                            )
                        )
        except (InvalidSpecifier, InvalidVersion):
            return AdvisoryQueryResult(
                AdvisoryStatus.ERROR,
                queried_at=queried_at,
                error_code="dependency_advisory_evaluation_failed",
            )
        metadata = tuple(
            DependencyPackageMetadata(
                package=record.name,
                version=record.version,
                license_expression=record.license,
                withdrawn=record.withdrawn,
            )
            for record in self.snapshot.packages
            if (record.name, record.version) in package_index
        )
        generated = _parse_timestamp(self.snapshot.generated_at)
        if generated > now + MAX_ADVISORY_FUTURE_SKEW:
            return AdvisoryQueryResult(
                AdvisoryStatus.ERROR,
                queried_at=queried_at,
                error_code="dependency_advisory_timestamp_future",
            )
        status = (
            AdvisoryStatus.STALE
            if generated < now - timedelta(hours=max_age_hours)
            else AdvisoryStatus.OK
        )
        return AdvisoryQueryResult(
            status,
            database_version=self.snapshot.database_version,
            source=self.snapshot.source,
            generated_at=self.snapshot.generated_at,
            queried_at=queried_at,
            snapshot_sha256=self.snapshot_sha256,
            advisories=tuple(
                sorted(
                    advisories,
                    key=lambda item: (item.package, Version(item.version), item.advisory_id),
                )
            ),
            packages=tuple(
                sorted(metadata, key=lambda item: (item.package, Version(item.version)))
            ),
        )

    async def close(self) -> None:
        return None


class HttpsDependencyAdvisoryProvider:
    version = ADVISORY_ADAPTER_VERSION

    def __init__(
        self,
        endpoint_url: str,
        *,
        api_token: str = "",
        config_ref: str = "config:dependency-default",
        timeout_seconds: float = 20,
        client: httpx.AsyncClient | None = None,
        resolver: Callable[[str, int], Awaitable[Sequence[str]]] | None = None,
    ) -> None:
        self.endpoint_url = _validate_https_url(endpoint_url)
        parsed = urlsplit(self.endpoint_url)
        assert parsed.hostname is not None
        self.endpoint_host = parsed.hostname
        self.endpoint_port = parsed.port or 443
        self.api_token = api_token
        self.config_ref = config_ref
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self.resolver = resolver or _resolve_host_addresses
        self.ready = True
        self.unavailable_reason = ""

    async def query(
        self,
        packages: Sequence[DependencyPackage],
        *,
        max_age_hours: int,
    ) -> AdvisoryQueryResult:
        if not packages or len(packages) > MAX_ADVISORY_QUERY_PACKAGES:
            return AdvisoryQueryResult(
                AdvisoryStatus.NOT_QUERIED,
                queried_at=_now(),
                error_code="dependency_package_snapshot_invalid",
            )
        try:
            addresses = await self.resolver(self.endpoint_host, self.endpoint_port)
        except (OSError, ValueError):
            return AdvisoryQueryResult(
                AdvisoryStatus.ERROR,
                queried_at=_now(),
                error_code="dependency_advisory_resolution_failed",
            )
        if not addresses or any(not _is_global_address(item) for item in addresses):
            return AdvisoryQueryResult(
                AdvisoryStatus.ERROR,
                queried_at=_now(),
                error_code="dependency_advisory_endpoint_untrusted",
            )
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "packages": [
                {"name": item.name, "version": item.version}
                for item in sorted(packages, key=lambda item: (item.name, Version(item.version)))
            ],
        }
        try:
            async with self.client.stream(
                "POST",
                self.endpoint_url,
                headers=headers,
                json=request,
            ) as response:
                if response.status_code != 200:
                    return AdvisoryQueryResult(
                        AdvisoryStatus.ERROR,
                        queried_at=_now(),
                        error_code="dependency_advisory_http_error",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ADVISORY_RESPONSE_BYTES:
                        return AdvisoryQueryResult(
                            AdvisoryStatus.ERROR,
                            queried_at=_now(),
                            error_code="dependency_advisory_response_too_large",
                        )
                    chunks.append(chunk)
        except (httpx.HTTPError, TimeoutError):
            return AdvisoryQueryResult(
                AdvisoryStatus.ERROR,
                queried_at=_now(),
                error_code="dependency_advisory_request_failed",
            )
        try:
            provider = LocalDependencyAdvisoryProvider(
                b"".join(chunks),
                config_ref=self.config_ref,
            )
        except (ValueError, json.JSONDecodeError):
            return AdvisoryQueryResult(
                AdvisoryStatus.ERROR,
                queried_at=_now(),
                error_code="dependency_advisory_response_invalid",
            )
        return await provider.query(packages, max_age_hours=max_age_hours)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


def _validate_https_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError as exc:
        raise ValueError("dependency_advisory_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("dependency_advisory_url_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("dependency_advisory_url_invalid")
    return value.strip()


async def _resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        0,
        socket.SOCK_STREAM,
    )
    if len(records) > 64:
        raise ValueError("dependency_advisory_resolution_too_large")
    return tuple(sorted({str(record[4][0]) for record in records}))


def _is_global_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _timestamp(value: str) -> str:
    parsed = _parse_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("dependency_advisory_timestamp_invalid")
    return parsed.astimezone(UTC)


def _is_timestamp(value: str) -> bool:
    try:
        _parse_timestamp(value)
    except ValueError:
        return False
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ADVISORY_ADAPTER_VERSION",
    "ADVISORY_SCHEMA_VERSION",
    "AdvisoryQueryResult",
    "AdvisoryStatus",
    "DependencyAdvisory",
    "DependencyAdvisoryProvider",
    "DependencyPackage",
    "DependencyPackageMetadata",
    "HttpsDependencyAdvisoryProvider",
    "LocalDependencyAdvisoryProvider",
    "UnavailableDependencyAdvisoryProvider",
]
