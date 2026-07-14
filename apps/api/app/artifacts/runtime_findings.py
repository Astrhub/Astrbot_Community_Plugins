from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .runner_contract import (
    AttestationStatus,
    ProbeStatus,
    RuntimeDispatchRequest,
    RuntimeDispatchResult,
    RuntimeViolation,
)
from .runtime_targets import RuntimeTargetResolution

_MACHINE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
)

_SEVERITY_BY_CODE: dict[str, Literal["info", "low", "medium", "high", "critical"]] = {
    "astrbot_version_incompatible": "critical",
    "astrbot_core_dependency_conflict": "critical",
    "dependency_conflict": "high",
    "dependency_install_failed": "high",
    "plugin_metadata_mismatch": "high",
    "plugin_import_failed": "high",
    "plugin_instance_failed": "high",
    "plugin_initialize_failed": "high",
    "plugin_startup_failed": "high",
    "handler_registration_failed": "high",
    "llm_tool_registration_failed": "high",
    "plugin_terminate_failed": "high",
    "runtime_network_unverified": "high",
    "runtime_cleanup_failed": "high",
    "runtime_command_timed_out": "medium",
    "dependency_check_timed_out": "medium",
    "astrbot_lifecycle_failed": "medium",
}


class NormalizedRuntimeFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    fingerprint: str = Field(min_length=64, max_length=64)
    rule_id: str = Field(min_length=1, max_length=96)
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str = Field(min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(default="", max_length=500)
    evidence_excerpt: str = Field(default="", max_length=500)
    source: Literal["runtime"] = "runtime"
    deterministic: bool
    metadata: dict[str, Any]

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not re.fullmatch(r"^[a-f0-9]{64}$", value):
            raise ValueError("runtime finding fingerprint must be a sha256 digest")
        return value

    @field_validator("rule_id", "category")
    @classmethod
    def validate_machine_name(cls, value: str) -> str:
        if not _MACHINE_NAME.fullmatch(value):
            raise ValueError("runtime finding identifiers must use lowercase machine syntax")
        return value

    @field_validator("message", "suggestion", "evidence_excerpt")
    @classmethod
    def validate_public_text(cls, value: str) -> str:
        normalized = " ".join(value.replace("\x00", "").split())
        if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
            raise ValueError("runtime finding cannot contain credential material")
        return normalized

    @model_validator(mode="after")
    def validate_metadata_boundary(self) -> NormalizedRuntimeFinding:
        canonical = json.dumps(
            self.metadata,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(canonical.encode()) > 8192:
            raise ValueError("runtime finding metadata exceeds 8192 bytes")
        if any(pattern.search(canonical) for pattern in _SECRET_PATTERNS):
            raise ValueError("runtime finding metadata cannot contain credential material")
        return self

    def as_repository_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def normalize_runtime_findings(
    result: RuntimeDispatchResult,
    *,
    tool_name: str,
    tool_version: str,
) -> tuple[NormalizedRuntimeFinding, ...]:
    base_metadata = _base_metadata(result, tool_name, tool_version)
    findings: list[NormalizedRuntimeFinding] = []

    if result.install.status != ProbeStatus.PASSED:
        code = result.install.error_code or "dependency_install_failed"
        findings.append(
            _finding(
                result,
                code,
                phase="install",
                category="dependency",
                evidence=result.install.message or result.install.status.value,
                metadata=base_metadata,
                deterministic=_is_deterministic(code, result.install.status),
            )
        )
    for conflict in result.install.conflicts:
        code = (
            "astrbot_core_dependency_conflict"
            if conflict.required_by.casefold() == "astrbot"
            else "dependency_conflict"
        )
        evidence = (
            f"{conflict.package} {conflict.installed_version or 'missing'}; "
            f"requires {conflict.requirement}; required by {conflict.required_by or 'unknown'}"
        )
        findings.append(
            _finding(
                result,
                code,
                phase="install",
                category="dependency",
                evidence=evidence,
                metadata={**base_metadata, "package": conflict.package},
                subject=conflict.package,
                deterministic=True,
            )
        )

    smoke_probes = {
        "metadata": result.smoke.metadata,
        "import": result.smoke.import_probe,
        "instance": result.smoke.instance,
        "initialize": result.smoke.initialize,
        "startup": result.smoke.startup,
        "handlers": result.smoke.handlers,
        "hooks": result.smoke.hooks,
        "llm_tools": result.smoke.llm_tools,
        "termination": result.smoke.termination,
    }
    for phase, probe in smoke_probes.items():
        if probe.status in {ProbeStatus.PASSED, ProbeStatus.SKIPPED}:
            continue
        code = probe.error_code or result.smoke.error_code or "plugin_startup_failed"
        findings.append(
            _finding(
                result,
                code,
                phase=phase,
                category=_smoke_category(phase),
                evidence=probe.message or probe.status.value,
                metadata=base_metadata,
                deterministic=_is_deterministic(code, probe.status),
            )
        )

    if result.network_attestation.status != AttestationStatus.PASSED:
        code = result.network_attestation.error_code or "runtime_network_unverified"
        findings.append(
            _finding(
                result,
                code,
                phase="network",
                category="sandbox_network",
                evidence=(
                    "install_egress_enforced="
                    f"{result.network_attestation.install_egress_enforced}; "
                    f"private_network_blocked={result.network_attestation.private_network_blocked}; "
                    "metadata_endpoint_blocked="
                    f"{result.network_attestation.metadata_endpoint_blocked}; "
                    f"smoke_network_disabled={result.network_attestation.smoke_network_disabled}"
                ),
                metadata=base_metadata,
                deterministic=False,
            )
        )

    if result.cleanup.status != ProbeStatus.PASSED:
        code = result.cleanup.error_code or "runtime_cleanup_failed"
        findings.append(
            _finding(
                result,
                code,
                phase="cleanup",
                category="sandbox_cleanup",
                evidence=(
                    result.cleanup.message
                    or f"leaked_resources={','.join(result.cleanup.leaked_resources[:10])}"
                ),
                metadata=base_metadata,
                deterministic=False,
            )
        )

    for violation in _runtime_violations(result):
        severity = (
            "critical"
            if any(
                marker in violation.category
                for marker in (
                    "credential",
                    "metadata_endpoint",
                    "private_network",
                    "docker_socket",
                    "smoke_network",
                    "escape",
                )
            )
            else "high"
        )
        findings.append(
            _finding(
                result,
                violation.category,
                phase=violation.phase,
                category="runtime_violation",
                evidence=violation.message,
                metadata={**base_metadata, "count": violation.count},
                subject=violation.category,
                severity=severity,
                deterministic=True,
            )
        )
    deduplicated = {finding.fingerprint: finding for finding in findings}
    return tuple(sorted(deduplicated.values(), key=lambda item: item.fingerprint))


def normalize_target_resolution_finding(
    resolution: RuntimeTargetResolution,
    *,
    policy_version_id: str,
    tool_version: str,
) -> NormalizedRuntimeFinding | None:
    source = resolution.finding
    if source is None:
        return None
    metadata = {
        "schema_version": "1",
        "phase": "target_resolution",
        "policy_version_id": policy_version_id,
        "plugin_version": resolution.plugin_version,
        "plugin_normalized_version": resolution.plugin_normalized_version,
        "metadata_requirement": resolution.metadata_requirement,
        "tool_name": "runtime-target-resolver",
        "tool_version": tool_version,
    }
    rule_id = source.rule_id
    fingerprint = _fingerprint(
        rule_id,
        "target_resolution",
        resolution.metadata_requirement,
        policy_version_id,
    )
    return NormalizedRuntimeFinding(
        fingerprint=fingerprint,
        rule_id=rule_id,
        severity=source.severity,
        category=source.category,
        message=source.message,
        evidence_excerpt=source.evidence_excerpt,
        deterministic=source.deterministic,
        metadata=metadata,
    )


def normalize_runtime_dispatch_failure(
    request: RuntimeDispatchRequest,
    *,
    dispatch_status: str,
    error_code: str,
    attempts: int,
    tool_version: str,
) -> NormalizedRuntimeFinding:
    code = error_code if _MACHINE_NAME.fullmatch(error_code) else "runtime_validation_failed"
    severity: Literal["info", "low", "medium", "high", "critical"]
    if code == "runtime_result_invalid":
        severity = "critical"
    elif "timeout" in code:
        severity = "medium"
    else:
        severity = "high"
    metadata = {
        "schema_version": "1",
        "phase": "dispatch",
        "dispatch_id": request.dispatch_id,
        "dispatch_status": dispatch_status,
        "attempts": attempts,
        "tool_name": "runtime-runner",
        "tool_version": tool_version[:128],
        "target": request.target.model_dump(mode="json"),
    }
    return NormalizedRuntimeFinding(
        fingerprint=_fingerprint(
            code,
            "dispatch",
            request.target.astrbot_version,
            request.target.python_version,
        ),
        rule_id=code,
        severity=severity,
        category="runtime_integrity" if code == "runtime_result_invalid" else "runtime_execution",
        message=_message_for_code(code),
        suggestion=_suggestion_for_code(code),
        evidence_excerpt=f"status={dispatch_status}; attempts={attempts}",
        deterministic=False,
        metadata=metadata,
    )


def _finding(
    result: RuntimeDispatchResult,
    code: str,
    *,
    phase: str,
    category: str,
    evidence: str,
    metadata: Mapping[str, Any],
    subject: str = "",
    severity: Literal["info", "low", "medium", "high", "critical"] | None = None,
    deterministic: bool,
) -> NormalizedRuntimeFinding:
    normalized_code = code if _MACHINE_NAME.fullmatch(code) else "runtime_validation_failed"
    return NormalizedRuntimeFinding(
        fingerprint=_fingerprint(
            normalized_code,
            phase,
            subject,
            result.target.astrbot_version,
            result.target.python_version,
        ),
        rule_id=normalized_code,
        severity=severity or _SEVERITY_BY_CODE.get(normalized_code, "high"),
        category=category,
        message=_message_for_code(normalized_code),
        suggestion=_suggestion_for_code(normalized_code),
        evidence_excerpt=evidence[:500],
        deterministic=deterministic,
        metadata={**metadata, "phase": phase, "probe_error_code": normalized_code},
    )


def _base_metadata(
    result: RuntimeDispatchResult,
    tool_name: str,
    tool_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "dispatch_id": result.dispatch_id,
        "result_sha256": result.result_sha256,
        "tool_name": _machine_value(tool_name, "runtime-runner"),
        "tool_version": str(tool_version)[:128],
        "target": result.target.model_dump(mode="json"),
        "dependency_snapshot": {
            "before_sha256": result.install.core_before_sha256,
            "after_sha256": result.install.core_after_sha256,
        },
        "private_logs_available": bool(result.logs_key and result.logs_sha256),
    }


def _runtime_violations(result: RuntimeDispatchResult) -> Iterable[RuntimeViolation]:
    yield from result.smoke.violations
    yield from result.network_attestation.violations


def _is_deterministic(code: str, status: ProbeStatus) -> bool:
    return status != ProbeStatus.TIMED_OUT and not any(
        marker in code for marker in ("timeout", "unavailable", "network", "cleanup")
    )


def _smoke_category(phase: str) -> str:
    if phase in {"handlers", "hooks", "llm_tools"}:
        return "registration"
    if phase == "metadata":
        return "compatibility"
    return "plugin_lifecycle"


def _message_for_code(code: str) -> str:
    messages = {
        "astrbot_core_dependency_conflict": "Plugin requirements changed an AstrBot core dependency destructively",
        "dependency_conflict": "Plugin dependencies do not form a consistent environment",
        "dependency_install_failed": "Plugin dependencies could not be installed in the runtime sandbox",
        "plugin_metadata_mismatch": "Loaded plugin metadata does not match the submitted artifact",
        "plugin_import_failed": "AstrBot could not import the submitted plugin",
        "plugin_instance_failed": "AstrBot could not instantiate the submitted plugin",
        "plugin_initialize_failed": "The plugin initialize lifecycle failed",
        "plugin_startup_failed": "The plugin startup lifecycle failed",
        "handler_registration_failed": "Plugin handler registration could not be verified",
        "llm_tool_registration_failed": "Plugin LLM tool registration could not be verified",
        "plugin_terminate_failed": "The plugin did not terminate cleanly",
        "runtime_network_unverified": "Runtime network isolation could not be verified",
        "runtime_cleanup_failed": "Runtime resources were not fully cleaned up",
        "runtime_command_timed_out": "A runtime validation command timed out",
        "dependency_check_timed_out": "Dependency validation timed out",
        "astrbot_lifecycle_failed": "AstrBot lifecycle initialization failed in the sandbox",
        "runtime_dispatch_timeout": "Runtime validation exhausted its permitted lease attempts",
        "runtime_result_invalid": "Runtime result failed integrity or schema validation",
        "runtime_validation_failed": "Runtime validation did not produce a valid passing result",
    }
    return messages.get(code, "Runtime validation reported a security or compatibility issue")


def _suggestion_for_code(code: str) -> str:
    if code in {"dependency_conflict", "astrbot_core_dependency_conflict"}:
        return "Adjust requirements.txt so AstrBot and plugin dependencies remain compatible"
    if code.startswith("plugin_") or code.endswith("registration_failed"):
        return "Review the referenced lifecycle phase and submit a corrected plugin version"
    return "Review the runtime report and correct the reported condition before resubmitting"


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x00".join(str(part) for part in parts).encode()).hexdigest()


def _machine_value(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value).strip().lower()).strip("_")
    return normalized[:96] if _MACHINE_NAME.fullmatch(normalized[:96]) else fallback
