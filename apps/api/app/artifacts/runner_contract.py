from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RUNTIME_CONTRACT_SCHEMA_VERSION = "1"
MAX_RUNTIME_REQUEST_BYTES = 64 * 1024
MAX_RUNTIME_RESULT_BYTES = 1024 * 1024

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_SHA = re.compile(r"^[a-f0-9]{7,40}$")
_ASTRBOT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_PYTHON_VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PLUGIN_NAME = re.compile(r"^astrbot_plugin_[a-z0-9][a-z0-9_]{0,95}$")
_OBJECT_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_GITHUB_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
    re.compile(
        r"[?&](?:access_token|api_key|key|password|secret|signature|token)=[^&#\s]+",
        re.IGNORECASE,
    ),
)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ProbeStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


class AttestationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RuntimeTarget(ContractModel):
    astrbot_version: str = Field(min_length=5, max_length=32)
    python_version: str = Field(min_length=3, max_length=16)
    image_digest: str = Field(min_length=71, max_length=71)
    platform: Literal["linux/amd64", "linux/arm64"] = "linux/amd64"
    astrbot_commit: str = Field(default="", max_length=40)

    @field_validator("astrbot_version")
    @classmethod
    def validate_astrbot_version(cls, value: str) -> str:
        if not _ASTRBOT_VERSION.fullmatch(value):
            raise ValueError("AstrBot target must be an exact major.minor.patch version")
        return value

    @field_validator("python_version")
    @classmethod
    def validate_python_version(cls, value: str) -> str:
        if not _PYTHON_VERSION.fullmatch(value):
            raise ValueError("Python target must be an exact major.minor or patch version")
        return value

    @field_validator("image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        if not value.startswith("sha256:") or not _SHA256.fullmatch(value[7:]):
            raise ValueError("Runtime image must be pinned by a sha256 digest")
        return value

    @field_validator("astrbot_commit")
    @classmethod
    def validate_astrbot_commit(cls, value: str) -> str:
        if value and not _COMMIT_SHA.fullmatch(value):
            raise ValueError("AstrBot commit must be a 7 to 40 character lowercase Git SHA")
        return value


class RuntimeTargetSnapshot(RuntimeTarget):
    resolved_python_version: str = Field(min_length=5, max_length=32)

    @field_validator("resolved_python_version")
    @classmethod
    def validate_resolved_python_version(cls, value: str) -> str:
        if not re.fullmatch(r"^[0-9]+\.[0-9]+\.[0-9]+$", value):
            raise ValueError("Resolved Python version must include major, minor, and patch")
        return value


class RuntimeLimits(ContractModel):
    cpu: float = Field(gt=0, le=16, allow_inf_nan=False)
    memory_mb: int = Field(ge=128, le=32768)
    pids: int = Field(ge=16, le=4096)
    timeout_seconds: int = Field(ge=10, le=3600)
    disk_mb: int = Field(ge=128, le=32768)
    tmpfs_mb: int = Field(ge=16, le=8192)
    max_log_bytes: int = Field(ge=1024, le=16 * 1024 * 1024)
    max_result_bytes: int = Field(ge=4096, le=MAX_RUNTIME_RESULT_BYTES)

    @model_validator(mode="after")
    def validate_related_limits(self) -> Self:
        if self.tmpfs_mb > self.memory_mb:
            raise ValueError("tmpfs_mb cannot exceed memory_mb")
        return self


class ExpectedPluginMetadata(ContractModel):
    name: str = Field(min_length=16, max_length=112)
    version: str = Field(min_length=1, max_length=64)
    source_repo: str = Field(min_length=19, max_length=256)
    source_commit_sha: str = Field(default="", max_length=40)

    @field_validator("name")
    @classmethod
    def validate_plugin_name(cls, value: str) -> str:
        if not _PLUGIN_NAME.fullmatch(value):
            raise ValueError("Plugin name must use the astrbot_plugin_<name> format")
        return value

    @field_validator("version")
    @classmethod
    def validate_plugin_version(cls, value: str) -> str:
        return _bounded_text(value, label="Plugin version", maximum=64)

    @field_validator("source_repo")
    @classmethod
    def validate_source_repo(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"github.com", "www.github.com"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Source repository must be a credential-free GitHub HTTPS URL")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if (
            len(parts) != 2
            or any(part in {".", ".."} for part in parts)
            or not all(_GITHUB_PATH_SEGMENT.fullmatch(part) for part in parts)
        ):
            raise ValueError("Source repository must identify one GitHub owner and repository")
        return value.rstrip("/")

    @field_validator("source_commit_sha")
    @classmethod
    def validate_source_commit(cls, value: str) -> str:
        if value and not _COMMIT_SHA.fullmatch(value):
            raise ValueError("Source commit must be a 7 to 40 character lowercase Git SHA")
        return value


class RuntimeDispatchRequest(ContractModel):
    schema_version: Literal[RUNTIME_CONTRACT_SCHEMA_VERSION]
    dispatch_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    artifact_size_bytes: int = Field(gt=0, le=512 * 1024 * 1024)
    quarantine_key: str = Field(min_length=1, max_length=512)
    policy_version_id: str = Field(min_length=1, max_length=128)
    expected_plugin: ExpectedPluginMetadata
    target: RuntimeTarget
    limits: RuntimeLimits
    install_network_profile: str = Field(min_length=1, max_length=64)
    smoke_network_profile: Literal["none"]
    result_key: str = Field(min_length=1, max_length=420)

    @field_validator("dispatch_id", "artifact_id", "policy_version_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("Contract identifiers contain unsupported characters")
        return value

    @field_validator("artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        return _validate_sha256(value, "artifact_sha256")

    @field_validator("quarantine_key", "result_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        return _validate_object_key(value)

    @field_validator("install_network_profile")
    @classmethod
    def validate_network_profile(cls, value: str) -> str:
        if not _PROFILE.fullmatch(value):
            raise ValueError("Install network profile must be a versioned identifier")
        return value

    @model_validator(mode="after")
    def validate_request_boundary(self) -> Self:
        if self.quarantine_key == self.result_key:
            raise ValueError("Runtime result key must differ from the quarantine input key")
        _validate_contract_boundary(self, MAX_RUNTIME_REQUEST_BYTES)
        return self

    def canonical_sha256(self) -> str:
        return contract_sha256(self)


class ProbeResult(ContractModel):
    status: ProbeStatus
    duration_ms: int = Field(default=0, ge=0, le=3_600_000)
    error_code: str = Field(default="", max_length=96)
    message: str = Field(default="", max_length=500)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str) -> str:
        if value and not _ERROR_CODE.fullmatch(value):
            raise ValueError("Probe error_code must use lowercase machine-readable syntax")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _bounded_text(value, label="Probe message", maximum=500, allow_empty=True)

    @model_validator(mode="after")
    def validate_status_error(self) -> Self:
        if self.status == ProbeStatus.PASSED and (self.error_code or self.message):
            raise ValueError("Passed probes cannot carry error details")
        if self.status != ProbeStatus.PASSED and not self.error_code:
            raise ValueError("Non-passed probes require an error_code")
        return self


class MetadataProbeResult(ProbeResult):
    name: str = Field(default="", max_length=112)
    version: str = Field(default="", max_length=64)
    author: str = Field(default="", max_length=160)

    @field_validator("name", "version", "author")
    @classmethod
    def validate_metadata_text(cls, value: str) -> str:
        return _bounded_text(value, label="Metadata value", maximum=160, allow_empty=True)

    @model_validator(mode="after")
    def validate_passed_metadata(self) -> Self:
        if self.status == ProbeStatus.PASSED:
            if not _PLUGIN_NAME.fullmatch(self.name) or not self.version or not self.author:
                raise ValueError("Passed metadata probes require valid name, version, and author")
        return self


class StartupProbeResult(ProbeResult):
    ready_ms: int | None = Field(default=None, ge=0, le=3_600_000)

    @model_validator(mode="after")
    def validate_ready_time(self) -> Self:
        if self.status == ProbeStatus.PASSED and self.ready_ms is None:
            raise ValueError("Passed startup probes require ready_ms")
        if self.status != ProbeStatus.PASSED and self.ready_ms is not None:
            raise ValueError("Failed startup probes cannot claim readiness")
        return self


class RegistrationProbeResult(ProbeResult):
    count: int = Field(default=0, ge=0, le=5000)
    names: tuple[str, ...] = Field(default=(), max_length=5000)

    @field_validator("names")
    @classmethod
    def validate_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _bounded_text(name, label="Registration name", maximum=160) for name in value
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("Registration names cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_registration_count(self) -> Self:
        if self.count != len(self.names):
            raise ValueError("Registration count must match names")
        return self


class FailedPluginRecord(ContractModel):
    present: bool
    error_code: str = Field(default="", max_length=96)
    message: str = Field(default="", max_length=500)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str) -> str:
        if value and not _ERROR_CODE.fullmatch(value):
            raise ValueError("Failed plugin error_code must use lowercase syntax")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _bounded_text(value, label="Failed plugin message", maximum=500, allow_empty=True)

    @model_validator(mode="after")
    def validate_presence(self) -> Self:
        if self.present and not self.error_code:
            raise ValueError("Present failed-plugin records require an error_code")
        if not self.present and (self.error_code or self.message):
            raise ValueError("Absent failed-plugin records cannot carry error details")
        return self


class RuntimeViolation(ContractModel):
    phase: Literal["install", "smoke", "cleanup"]
    category: str = Field(min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=500)
    count: int = Field(default=1, ge=1, le=100000)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if not _ERROR_CODE.fullmatch(value):
            raise ValueError("Violation category must use lowercase machine-readable syntax")
        return value

    @field_validator("message")
    @classmethod
    def validate_violation_message(cls, value: str) -> str:
        return _bounded_text(value, label="Violation message", maximum=500)


class InstalledPackage(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    source: Literal["index", "direct_url", "vcs", "local", "unknown"] = "index"

    @field_validator("name")
    @classmethod
    def validate_package_name(cls, value: str) -> str:
        if not _PACKAGE_NAME.fullmatch(value):
            raise ValueError("Installed package name is invalid")
        return value

    @field_validator("version")
    @classmethod
    def validate_package_version(cls, value: str) -> str:
        return _bounded_text(value, label="Package version", maximum=128)


class DependencyConflict(ContractModel):
    package: str = Field(min_length=1, max_length=128)
    installed_version: str = Field(default="", max_length=128)
    requirement: str = Field(min_length=1, max_length=256)
    required_by: str = Field(default="", max_length=128)

    @field_validator("package", "required_by")
    @classmethod
    def validate_package_names(cls, value: str) -> str:
        if value and not _PACKAGE_NAME.fullmatch(value):
            raise ValueError("Dependency package name is invalid")
        return value

    @field_validator("installed_version", "requirement")
    @classmethod
    def validate_dependency_text(cls, value: str) -> str:
        return _bounded_text(value, label="Dependency value", maximum=256, allow_empty=True)


class InstallResult(ProbeResult):
    astrbot_version: str = Field(default="", max_length=32)
    pip_check: ProbeResult
    packages: tuple[InstalledPackage, ...] = Field(default=(), max_length=2000)
    conflicts: tuple[DependencyConflict, ...] = Field(default=(), max_length=500)
    core_before_sha256: str = Field(default="", max_length=64)
    core_after_sha256: str = Field(default="", max_length=64)
    sbom_key: str | None = Field(default=None, max_length=512)
    sbom_sha256: str | None = Field(default=None, max_length=64)

    @field_validator("astrbot_version")
    @classmethod
    def validate_installed_astrbot(cls, value: str) -> str:
        if value and not _ASTRBOT_VERSION.fullmatch(value):
            raise ValueError("Installed AstrBot version must be exact")
        return value

    @field_validator("core_before_sha256", "core_after_sha256")
    @classmethod
    def validate_core_hash(cls, value: str) -> str:
        return _validate_sha256(value, "core dependency snapshot", allow_empty=True)

    @field_validator("sbom_key")
    @classmethod
    def validate_sbom_key(cls, value: str | None) -> str | None:
        return _validate_object_key(value) if value is not None else None

    @field_validator("sbom_sha256")
    @classmethod
    def validate_sbom_sha256(cls, value: str | None) -> str | None:
        return _validate_sha256(value, "sbom_sha256") if value is not None else None

    @model_validator(mode="after")
    def validate_install_result(self) -> Self:
        if (self.sbom_key is None) != (self.sbom_sha256 is None):
            raise ValueError("SBOM key and sha256 must be provided together")
        if self.status == ProbeStatus.PASSED:
            if not self.astrbot_version or self.pip_check.status != ProbeStatus.PASSED:
                raise ValueError("Passed installs require AstrBot and a passed pip check")
            if not self.core_before_sha256 or not self.core_after_sha256:
                raise ValueError("Passed installs require both core dependency snapshots")
        return self


class SmokeResult(ContractModel):
    status: ProbeStatus
    duration_ms: int = Field(default=0, ge=0, le=3_600_000)
    metadata: MetadataProbeResult
    import_probe: ProbeResult
    initialize: ProbeResult
    startup: StartupProbeResult
    handlers: RegistrationProbeResult
    llm_tools: RegistrationProbeResult
    failed_plugin: FailedPluginRecord
    termination: ProbeResult
    violations: tuple[RuntimeViolation, ...] = Field(default=(), max_length=1000)
    error_code: str = Field(default="", max_length=96)
    message: str = Field(default="", max_length=500)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str) -> str:
        if value and not _ERROR_CODE.fullmatch(value):
            raise ValueError("Smoke error_code must use lowercase machine-readable syntax")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _bounded_text(value, label="Smoke message", maximum=500, allow_empty=True)

    @model_validator(mode="after")
    def validate_smoke_result(self) -> Self:
        required = (
            self.metadata,
            self.import_probe,
            self.initialize,
            self.startup,
            self.handlers,
            self.llm_tools,
            self.termination,
        )
        if self.status == ProbeStatus.PASSED:
            if any(probe.status != ProbeStatus.PASSED for probe in required):
                raise ValueError("Passed smoke results require every lifecycle probe to pass")
            if self.failed_plugin.present:
                raise ValueError("Passed smoke results cannot contain a failed-plugin record")
            if self.error_code or self.message:
                raise ValueError("Passed smoke results cannot carry error details")
        elif not self.error_code:
            raise ValueError("Non-passed smoke results require an error_code")
        return self


class NetworkAttestation(ContractModel):
    status: AttestationStatus
    backend: str = Field(min_length=1, max_length=96)
    install_profile: str = Field(min_length=1, max_length=64)
    smoke_profile: Literal["none"]
    install_egress_enforced: bool
    private_network_blocked: bool
    metadata_endpoint_blocked: bool
    smoke_network_disabled: bool
    violations: tuple[RuntimeViolation, ...] = Field(default=(), max_length=1000)
    error_code: str = Field(default="", max_length=96)

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("Network backend must be a public identifier")
        return value

    @field_validator("install_profile")
    @classmethod
    def validate_install_profile(cls, value: str) -> str:
        if not _PROFILE.fullmatch(value):
            raise ValueError("Install profile must be a versioned identifier")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str) -> str:
        if value and not _ERROR_CODE.fullmatch(value):
            raise ValueError("Network error_code must use lowercase syntax")
        return value

    @model_validator(mode="after")
    def validate_attestation(self) -> Self:
        controls = (
            self.install_egress_enforced,
            self.private_network_blocked,
            self.metadata_endpoint_blocked,
            self.smoke_network_disabled,
        )
        if self.status == AttestationStatus.PASSED:
            if not all(controls) or self.violations or self.error_code:
                raise ValueError(
                    "Passed network attestations require every control and no violations"
                )
        elif not self.error_code:
            raise ValueError("Non-passed network attestations require an error_code")
        return self


class CleanupResult(ProbeResult):
    removed_containers: int = Field(default=0, ge=0, le=1000)
    removed_volumes: int = Field(default=0, ge=0, le=1000)
    removed_networks: int = Field(default=0, ge=0, le=1000)
    removed_temp_roots: int = Field(default=0, ge=0, le=1000)
    leaked_resources: tuple[str, ...] = Field(default=(), max_length=1000)

    @field_validator("leaked_resources")
    @classmethod
    def validate_leaked_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_bounded_text(item, label="Leaked resource ID", maximum=160) for item in value)

    @model_validator(mode="after")
    def validate_cleanup(self) -> Self:
        if self.status == ProbeStatus.PASSED and self.leaked_resources:
            raise ValueError("Passed cleanup cannot report leaked resources")
        return self


class _RuntimeDispatchResultPayload(ContractModel):
    schema_version: Literal[RUNTIME_CONTRACT_SCHEMA_VERSION]
    dispatch_id: str = Field(min_length=1, max_length=128)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    target: RuntimeTargetSnapshot
    install: InstallResult
    smoke: SmokeResult
    network_attestation: NetworkAttestation
    cleanup: CleanupResult
    logs_key: str | None = Field(default=None, max_length=512)
    logs_sha256: str | None = Field(default=None, max_length=64)

    @field_validator("dispatch_id")
    @classmethod
    def validate_dispatch_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("Dispatch ID contains unsupported characters")
        return value

    @field_validator("artifact_sha256", "logs_sha256")
    @classmethod
    def validate_hashes(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _validate_sha256(value, info.field_name)

    @field_validator("logs_key")
    @classmethod
    def validate_logs_key(cls, value: str | None) -> str | None:
        return _validate_object_key(value) if value is not None else None

    @model_validator(mode="after")
    def validate_result_payload(self) -> Self:
        if (self.logs_key is None) != (self.logs_sha256 is None):
            raise ValueError("Log key and sha256 must be provided together")
        _validate_contract_boundary(self, MAX_RUNTIME_RESULT_BYTES)
        return self


class RuntimeDispatchResult(_RuntimeDispatchResultPayload):
    result_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("result_sha256")
    @classmethod
    def validate_result_sha256(cls, value: str) -> str:
        return _validate_sha256(value, "result_sha256")

    @model_validator(mode="after")
    def validate_result_hash(self) -> Self:
        expected_hash = runtime_result_sha256(self)
        if self.result_sha256 != expected_hash:
            raise ValueError("Runtime result sha256 does not match canonical JSON")
        return self


def canonical_contract_json(
    value: BaseModel | Mapping[str, Any],
    *,
    exclude: Sequence[str] = (),
) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = dict(value)
    for key in exclude:
        payload.pop(key, None)
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def contract_sha256(value: BaseModel | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_contract_json(value).encode("utf-8")).hexdigest()


def runtime_result_sha256(value: BaseModel | Mapping[str, Any]) -> str:
    canonical = canonical_contract_json(value, exclude=("result_sha256",))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_runtime_dispatch_result(payload: Mapping[str, Any]) -> RuntimeDispatchResult:
    normalized = dict(payload)
    normalized.pop("result_sha256", None)
    validated = _RuntimeDispatchResultPayload.model_validate(normalized)
    normalized = validated.model_dump(mode="json")
    normalized["result_sha256"] = runtime_result_sha256(normalized)
    return RuntimeDispatchResult.model_validate(normalized)


def runtime_result_passed(result: RuntimeDispatchResult) -> bool:
    return (
        result.install.status == ProbeStatus.PASSED
        and result.smoke.status == ProbeStatus.PASSED
        and result.network_attestation.status == AttestationStatus.PASSED
        and result.cleanup.status == ProbeStatus.PASSED
    )


def validate_runtime_result_identity(
    request: RuntimeDispatchRequest,
    result: RuntimeDispatchResult,
) -> None:
    target = result.target
    if (
        result.dispatch_id != request.dispatch_id
        or result.artifact_sha256 != request.artifact_sha256
        or target.astrbot_version != request.target.astrbot_version
        or target.python_version != request.target.python_version
        or target.image_digest != request.target.image_digest
        or target.platform != request.target.platform
        or target.astrbot_commit != request.target.astrbot_commit
        or result.install.astrbot_version not in {"", request.target.astrbot_version}
        or result.network_attestation.install_profile != request.install_network_profile
        or result.network_attestation.smoke_profile != request.smoke_network_profile
    ):
        raise ValueError("runtime result identity does not match its request")
    if result.smoke.metadata.status == ProbeStatus.PASSED and (
        result.smoke.metadata.name != request.expected_plugin.name
        or result.smoke.metadata.version != request.expected_plugin.version
    ):
        raise ValueError("runtime metadata result does not match the submitted plugin")


def runtime_result_error_code(result: RuntimeDispatchResult) -> str:
    if result.cleanup.status != ProbeStatus.PASSED:
        return result.cleanup.error_code or "runtime_cleanup_failed"
    if result.network_attestation.status != AttestationStatus.PASSED:
        return result.network_attestation.error_code or "runtime_network_unverified"
    if result.install.status != ProbeStatus.PASSED:
        return result.install.error_code or "dependency_install_failed"
    if result.smoke.status != ProbeStatus.PASSED:
        return result.smoke.error_code or "plugin_startup_failed"
    return "runtime_validation_failed"


def runtime_result_object_key(
    request: RuntimeDispatchRequest,
    attempt: int,
    result_sha256: str,
) -> str:
    if attempt < 1 or attempt > 1000:
        raise ValueError("Runtime result attempt is outside the supported range")
    digest = _validate_sha256(result_sha256, "result_sha256")
    return _validate_object_key(f"{request.result_key}/attempt-{attempt}-{digest}.json")


def _validate_contract_boundary(value: BaseModel, maximum_bytes: int) -> None:
    payload = value.model_dump(mode="json")
    _reject_secret_material(payload)
    size = len(canonical_contract_json(payload).encode("utf-8"))
    if size > maximum_bytes:
        raise ValueError(f"Contract payload exceeds {maximum_bytes} bytes")


def _reject_secret_material(value: Any) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_secret_material(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_material(item)
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("Runtime contract cannot contain credential material")


def _validate_sha256(value: str, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and not value:
        return value
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 digest")
    return value


def _validate_object_key(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or normalized.startswith("/")
        or "\\" in normalized
        or "://" in normalized
        or "?" in normalized
        or "#" in normalized
        or _CONTROL_CHARACTER.search(normalized)
    ):
        raise ValueError("Object key must be a bounded relative storage key")
    segments = normalized.split("/")
    if any(
        segment in {".", ".."} or not _OBJECT_KEY_SEGMENT.fullmatch(segment) for segment in segments
    ):
        raise ValueError("Object key cannot contain empty or traversal segments")
    return normalized


def _bounded_text(
    value: str,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    normalized = " ".join(value.split())
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise ValueError(f"{label} must contain at most {maximum} characters")
    if _CONTROL_CHARACTER.search(normalized):
        raise ValueError(f"{label} contains control characters")
    return normalized
