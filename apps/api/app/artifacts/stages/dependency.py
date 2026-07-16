from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import Version
from pydantic import ValidationError

from ...runtime_runner.storage import RuntimeResultStorageError
from ..advisory import (
    AdvisoryQueryResult,
    AdvisoryStatus,
    DependencyAdvisoryProvider,
    DependencyPackage,
)
from ..models import JobType, ReviewStatus
from ..policy import ToolFailureAction, parse_review_policy
from ..requirements_parser import (
    RequirementsParseError,
    RequirementsParseResult,
    parse_requirements,
)
from ..runner_contract import (
    MAX_RUNTIME_RESULT_BYTES,
    RuntimeDispatchRequest,
    RuntimeDispatchResult,
    runtime_result_object_key,
    runtime_result_passed,
    validate_runtime_result_identity,
)
from ..sbom import MAX_SBOM_BYTES, validate_cyclonedx_sbom
from ..storage import ArtifactStorageError
from .base import StageContext, StageOutcome

DEPENDENCY_STAGE_TOOL_VERSION = "dependency-stage-v1"

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True, slots=True)
class _RuntimeEvidence:
    run_id: str
    astrbot_version: str
    python_version: str
    result: RuntimeDispatchResult
    sbom_sha256: str


class DependencyStage:
    job_type = JobType.DEPENDENCY_SCAN.value

    def __init__(
        self,
        provider: DependencyAdvisoryProvider,
        result_storage: Any | None,
    ) -> None:
        self.provider = provider
        self.result_storage = result_storage

    async def execute(self, context: StageContext) -> StageOutcome:
        invalid = _validate_context(context)
        if invalid is not None:
            return invalid
        assert context.policy is not None
        policy = parse_review_policy(context.policy.get("policy") or {})
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "dependency",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": "dependency-advisory",
                "tool_version": self.provider.version,
                "policy_version_id": context.artifact["policy_version_id"],
                "input_sha256": str((context.job.get("payload") or {}).get("input_sha256") or ""),
                "coverage": {"outcome": "running", "stage_name": "dependency"},
                "idempotency_key": _dependency_run_key(
                    str(context.artifact["id"]),
                    str(context.artifact["policy_version_id"]),
                    self.provider.version,
                ),
            }
        )
        if str(run.get("status") or "") in {"succeeded", "failed", "timed_out", "cancelled"}:
            return _terminal_outcome(run)
        if self.result_storage is None or not self.provider.ready:
            return await _unavailable(
                context,
                run,
                policy.dependency.on_unavailable,
                self.provider.unavailable_reason or "dependency_runtime_evidence_unavailable",
                AdvisoryStatus.NOT_QUERIED,
            )
        if self.provider.config_ref != policy.dependency.advisory_config_ref:
            return await _unavailable(
                context,
                run,
                policy.dependency.on_unavailable,
                "dependency_advisory_config_mismatch",
                AdvisoryStatus.NOT_QUERIED,
            )

        try:
            declared = await _declared_requirements(context)
            evidence = await _runtime_evidence(context, policy.runtime_targets, self.result_storage)
        except (
            ArtifactStorageError,
            RuntimeResultStorageError,
            ValidationError,
            ValueError,
        ) as exc:
            error_code = _safe_error_code(exc, "dependency_runtime_evidence_invalid")
            return await _unavailable(
                context,
                run,
                policy.dependency.on_unavailable,
                str(error_code),
                AdvisoryStatus.NOT_QUERIED,
                private_error=type(exc).__name__,
            )

        requirements_hashes = {item.result.install.requirements_sha256 for item in evidence}
        if requirements_hashes != {declared.content_sha256}:
            return await _unavailable(
                context,
                run,
                policy.dependency.on_unavailable,
                "dependency_requirements_snapshot_mismatch",
                AdvisoryStatus.NOT_QUERIED,
            )

        packages = tuple(
            sorted(
                {
                    (canonicalize_name(package.name), package.version): DependencyPackage(
                        package.name,
                        package.version,
                    )
                    for item in evidence
                    for package in item.result.install.packages
                }.values(),
                key=lambda item: (item.name, Version(item.version)),
            )
        )
        if not packages:
            return await _unavailable(
                context,
                run,
                policy.dependency.on_unavailable,
                "dependency_package_snapshot_empty",
                AdvisoryStatus.NOT_QUERIED,
            )
        try:
            advisory = await self.provider.query(
                packages,
                max_age_hours=policy.dependency.max_data_age_hours,
            )
        except Exception as exc:
            return await _unavailable(
                context,
                run,
                policy.dependency.on_unavailable,
                "dependency_advisory_query_failed",
                AdvisoryStatus.ERROR,
                private_error=type(exc).__name__,
            )
        if advisory.status is not AdvisoryStatus.OK:
            return await _complete_unavailable_result(
                context,
                run,
                policy.dependency.on_unavailable,
                advisory,
                evidence=evidence,
                requirements=declared,
                packages=packages,
            )

        findings = [
            *_source_findings(declared, evidence, policy.dependency.private_package_prefixes),
            *_advisory_findings(advisory),
            *_package_policy_findings(advisory, policy.dependency.denied_licenses),
        ]
        await context.repository.replace_findings(
            str(context.artifact["id"]),
            str(run["id"]),
            findings,
        )
        threshold = _SEVERITY_RANK[policy.dependency.max_severity.value]
        blocking = [
            item
            for item in findings
            if bool(item.get("deterministic", True))
            and _SEVERITY_RANK[str(item["severity"])] >= threshold
        ]
        package_sha256 = _package_snapshot_sha256(evidence)
        active_advisories = [item for item in advisory.advisories if not item.withdrawn]
        outcome = "blocked" if blocking else "completed"
        coverage = {
            "outcome": outcome,
            "complete": True,
            "stage_name": "dependency",
            "runtime_targets": len(evidence),
            "package_count": len(packages),
            "requirements_sha256": declared.content_sha256,
            "package_snapshot_sha256": package_sha256,
            "database_version": advisory.database_version,
            "database_source": advisory.source,
            "database_generated_at": advisory.generated_at,
            "database_queried_at": advisory.queried_at,
            "database_snapshot_sha256": advisory.snapshot_sha256,
            "advisory_status": advisory.status.value,
            "advisory_count": len(active_advisories),
            "finding_count": len(findings),
            "no_known_vulnerabilities": not active_advisories,
        }
        summary = (
            "Dependency policy found deterministic blocking risk"
            if blocking
            else "Dependency graph and fresh advisory snapshot passed policy"
        )
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "succeeded",
                "summary": summary,
                "coverage": coverage,
                "raw_result": {
                    "advisory": _advisory_summary(advisory),
                    "runtime_targets": _target_summary(evidence),
                    "requirements_sha256": declared.content_sha256,
                    "package_snapshot_sha256": package_sha256,
                    "package_count": len(packages),
                    "finding_count": len(findings),
                },
                "output_sha256": _findings_sha256(findings),
                "dependency_snapshot_sha256": package_sha256,
                "ruleset_version": advisory.database_version,
            },
        )
        if blocking:
            return StageOutcome.blocked(
                "dependency_policy_blocked",
                summary,
                coverage=coverage,
            )
        return StageOutcome.completed(summary, coverage=coverage)


async def _runtime_evidence(
    context: StageContext,
    targets: Sequence[Any],
    result_storage: Any,
) -> tuple[_RuntimeEvidence, ...]:
    runs = await context.repository.list_review_runs(str(context.artifact["id"]))
    sboms = await context.repository.list_artifact_sboms(str(context.artifact["id"]))
    collected: list[_RuntimeEvidence] = []
    for target in targets:
        matching = [
            run
            for run in runs
            if run.get("type") == "runtime"
            and run.get("policy_version_id") == context.artifact.get("policy_version_id")
            and run.get("astrbot_version") == target.astrbot
            and run.get("python_version") == target.python
        ]
        if len(matching) != 1 or matching[0].get("status") != "succeeded":
            raise ValueError("dependency_runtime_target_incomplete")
        run = matching[0]
        raw_result = run.get("raw_result") if isinstance(run.get("raw_result"), Mapping) else {}
        dispatch_id = str(raw_result.get("dispatch_id") or "")
        dispatch = await context.repository.get_runtime_dispatch(dispatch_id)
        if dispatch is None or not dispatch.get("collected_at"):
            raise ValueError("dependency_runtime_dispatch_incomplete")
        request = RuntimeDispatchRequest.model_validate(dispatch.get("request") or {})
        if (
            request.dispatch_id != dispatch_id
            or request.artifact_id != context.artifact["id"]
            or request.canonical_sha256() != dispatch.get("request_sha256")
        ):
            raise ValueError("dependency_runtime_request_invalid")
        result_key = runtime_result_object_key(
            request,
            int(dispatch.get("attempts") or 0),
            str(dispatch.get("result_sha256") or ""),
        )
        if result_key != dispatch.get("result_key"):
            raise ValueError("dependency_runtime_result_key_invalid")
        result_content = await result_storage.read_text_content(
            result_key,
            min(request.limits.max_result_bytes, MAX_RUNTIME_RESULT_BYTES),
        )
        result = RuntimeDispatchResult.model_validate_json(result_content)
        validate_runtime_result_identity(request, result)
        if result.result_sha256 != dispatch.get("result_sha256") or not runtime_result_passed(
            result
        ):
            raise ValueError("dependency_runtime_result_invalid")
        matching_sboms = [item for item in sboms if item.get("run_id") == run.get("id")]
        if len(matching_sboms) != 1:
            raise ValueError("dependency_runtime_sbom_missing")
        sbom = matching_sboms[0]
        if (
            sbom.get("object_key") != result.install.sbom_key
            or sbom.get("document_sha256") != result.install.sbom_sha256
        ):
            raise ValueError("dependency_runtime_sbom_identity_invalid")
        sbom_content = await result_storage.read_text_content(
            str(sbom["object_key"]),
            MAX_SBOM_BYTES,
            str(sbom["document_sha256"]),
        )
        validated = validate_cyclonedx_sbom(
            sbom_content,
            astrbot_version=result.install.astrbot_version,
            packages=result.install.packages,
            expected_sha256=str(sbom["document_sha256"]),
        )
        if validated.package_count != int(sbom.get("package_count") or 0):
            raise ValueError("dependency_runtime_sbom_package_count_invalid")
        collected.append(
            _RuntimeEvidence(
                run_id=str(run["id"]),
                astrbot_version=target.astrbot,
                python_version=target.python,
                result=result,
                sbom_sha256=validated.document_sha256,
            )
        )
    return tuple(collected)


async def _declared_requirements(context: StageContext) -> RequirementsParseResult:
    files = await context.repository.list_artifact_files(str(context.artifact["id"]))
    candidates = [
        item
        for item in files
        if PurePosixPath(str(item.get("path") or "")).name.casefold() == "requirements.txt"
    ]
    if not candidates:
        return parse_requirements(b"")
    if len(candidates) != 1:
        raise ValueError("dependency_requirements_ambiguous")
    file = candidates[0]
    if not file.get("is_text") or not file.get("content_key"):
        raise ValueError("dependency_requirements_content_unavailable")
    content = await context.storage.read_text_content(
        str(file["content_key"]),
        256 * 1024,
        str(file["sha256"]),
    )
    try:
        return parse_requirements(content)
    except RequirementsParseError as exc:
        raise ValueError(exc.code) from exc


def _source_findings(
    requirements: RequirementsParseResult,
    evidence: Sequence[_RuntimeEvidence],
    private_prefixes: Sequence[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for diagnostic in requirements.diagnostics:
        severity = "medium" if diagnostic.code == "dependency_declaration_conflict" else "high"
        findings.append(
            _finding(
                rule_id=diagnostic.code,
                severity=severity,
                category="dependency_source",
                package=diagnostic.name,
                version="",
                message=diagnostic.message,
                evidence=diagnostic.evidence,
                metadata={"line": diagnostic.line_number, "source": diagnostic.source.value},
            )
        )
    for item in evidence:
        for package in item.result.install.packages:
            if package.source == "index":
                continue
            findings.append(
                _finding(
                    rule_id="dependency_installed_source_untrusted",
                    severity="high",
                    category="dependency_source",
                    package=package.name,
                    version=package.version,
                    message="Installed dependency did not originate from the configured index",
                    evidence=(
                        f"name={canonicalize_name(package.name)}; version={package.version}; "
                        f"source={package.source}; target={item.astrbot_version}/{item.python_version}"
                    ),
                    metadata={"source": package.source},
                )
            )
    for requirement in requirements.requirements:
        if requirement.source.value != "index":
            continue
        if any(requirement.name.startswith(prefix) for prefix in private_prefixes):
            findings.append(
                _finding(
                    rule_id="dependency_confusion_private_name_on_public_index",
                    severity="high",
                    category="dependency_confusion",
                    package=requirement.name,
                    version=requirement.specifier,
                    message="Private package prefix is resolved through the public package index",
                    evidence=(
                        f"name={requirement.name}; line={requirement.line_number}; "
                        f"declaration_sha256={requirement.declaration_sha256}"
                    ),
                    metadata={"line": requirement.line_number},
                )
            )
    return findings


def _advisory_findings(advisory: AdvisoryQueryResult) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in advisory.advisories:
        if item.withdrawn:
            findings.append(
                _finding(
                    rule_id="dependency_advisory_withdrawn",
                    severity="info",
                    category="dependency_advisory",
                    package=item.package,
                    version=item.version,
                    advisory_id=item.advisory_id,
                    message="A matching advisory record is withdrawn and is not an active block",
                    evidence=_advisory_evidence(item, advisory),
                    metadata=_advisory_metadata(item, advisory),
                    deterministic=False,
                )
            )
            continue
        findings.append(
            _finding(
                rule_id="dependency_known_vulnerability",
                severity=item.severity,
                category="dependency_vulnerability",
                package=item.package,
                version=item.version,
                advisory_id=item.advisory_id,
                message="Installed dependency version matches a known security advisory",
                evidence=_advisory_evidence(item, advisory),
                metadata=_advisory_metadata(item, advisory),
            )
        )
    return findings


def _package_policy_findings(
    advisory: AdvisoryQueryResult,
    denied_licenses: Sequence[str],
) -> list[dict[str, Any]]:
    denied = set(denied_licenses)
    findings: list[dict[str, Any]] = []
    for item in advisory.packages:
        if item.withdrawn:
            findings.append(
                _finding(
                    rule_id="dependency_release_withdrawn",
                    severity="high",
                    category="dependency_withdrawn",
                    package=item.package,
                    version=item.version,
                    message="Installed dependency release is withdrawn by the advisory snapshot",
                    evidence=(
                        f"name={item.package}; version={item.version}; "
                        f"database={advisory.database_version}"
                    ),
                    metadata={
                        "database_version": advisory.database_version,
                        "database_source": advisory.source,
                        "database_generated_at": advisory.generated_at,
                        "snapshot_sha256": advisory.snapshot_sha256,
                    },
                )
            )
        if item.license_expression and item.license_expression in denied:
            findings.append(
                _finding(
                    rule_id="dependency_license_denied",
                    severity="high",
                    category="dependency_license",
                    package=item.package,
                    version=item.version,
                    message="Installed dependency license is denied by the fixed review policy",
                    evidence=(
                        f"name={item.package}; version={item.version}; "
                        f"license={item.license_expression}; database={advisory.database_version}"
                    ),
                    metadata={
                        "license": item.license_expression,
                        "database_version": advisory.database_version,
                        "database_source": advisory.source,
                        "database_generated_at": advisory.generated_at,
                        "snapshot_sha256": advisory.snapshot_sha256,
                    },
                )
            )
    return findings


def _finding(
    *,
    rule_id: str,
    severity: str,
    category: str,
    package: str,
    version: str,
    message: str,
    evidence: str,
    metadata: Mapping[str, Any],
    advisory_id: str = "",
    deterministic: bool = True,
) -> dict[str, Any]:
    name = canonicalize_name(package) if package else ""
    fingerprint = hashlib.sha256(
        "\x00".join((rule_id, name, version, advisory_id)).encode()
    ).hexdigest()
    correlation: dict[str, Any] = {}
    if advisory_id and name and version:
        correlation = {
            "dependency": {
                "name": name,
                "version": version,
                "advisory_id": advisory_id,
            }
        }
    return {
        "fingerprint": fingerprint,
        "rule_id": rule_id,
        "severity": severity,
        "category": category,
        "message": message,
        "suggestion": "Update, replace, or remove the affected dependency and resubmit",
        "evidence_excerpt": " ".join(evidence.split())[:500],
        "confidence": 1.0 if deterministic else 0.7,
        "status": "open",
        "source": "dependency",
        "deterministic": deterministic,
        "correlation": correlation,
        "metadata": dict(metadata),
    }


async def _complete_unavailable_result(
    context: StageContext,
    run: Mapping[str, Any],
    action: ToolFailureAction,
    advisory: AdvisoryQueryResult,
    *,
    evidence: Sequence[_RuntimeEvidence],
    requirements: RequirementsParseResult,
    packages: Sequence[DependencyPackage],
) -> StageOutcome:
    return await _unavailable(
        context,
        run,
        action,
        advisory.error_code or f"dependency_advisory_{advisory.status.value}",
        advisory.status,
        raw_result={
            "advisory": _advisory_summary(advisory),
            "runtime_targets": _target_summary(evidence),
            "requirements_sha256": requirements.content_sha256,
            "package_snapshot_sha256": _package_snapshot_sha256(evidence),
            "package_count": len(packages),
        },
    )


async def _unavailable(
    context: StageContext,
    run: Mapping[str, Any],
    action: ToolFailureAction,
    error_code: str,
    status: AdvisoryStatus,
    *,
    private_error: str = "",
    raw_result: Mapping[str, Any] | None = None,
) -> StageOutcome:
    blocked = action is ToolFailureAction.FAIL_CLOSED
    outcome = "blocked" if blocked else "degraded"
    coverage = {
        "outcome": outcome,
        "complete": False,
        "stage_name": "dependency",
        "advisory_status": status.value,
        "no_known_vulnerabilities": False,
        "error_code": error_code,
    }
    private = dict(raw_result or {})
    if private_error:
        private["private_error_type"] = private_error
    await context.repository.complete_review_run(
        str(run["id"]),
        {
            "status": "failed",
            "summary": "Dependency advisory coverage is incomplete",
            "coverage": coverage,
            "raw_result": private,
            "error_code": error_code,
        },
    )
    if blocked:
        return StageOutcome.blocked(
            error_code,
            "Dependency advisory coverage is incomplete",
            coverage=coverage,
        )
    return StageOutcome.degraded(
        error_code,
        "Dependency advisory coverage is incomplete",
        coverage=coverage,
    )


def _validate_context(context: StageContext) -> StageOutcome | None:
    if context.policy is None or not context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "review_policy_unavailable",
            "Dependency stage has no fixed review policy snapshot",
        )
    if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "artifact_policy_snapshot_conflict",
            "Dependency job policy does not match the artifact snapshot",
        )
    if context.artifact.get("review_status") != ReviewStatus.SCANNING.value:
        return StageOutcome.terminal_failure(
            "artifact_not_scanning",
            "Artifact is not available for dependency review",
        )
    return None


def _terminal_outcome(run: Mapping[str, Any]) -> StageOutcome:
    coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
    outcome = str(coverage.get("outcome") or "")
    summary = str(run.get("summary") or "Dependency review completed")
    if outcome == "blocked":
        return StageOutcome.blocked(
            str(run.get("error_code") or "dependency_policy_blocked"),
            summary,
            coverage=coverage,
        )
    if outcome == "degraded" or run.get("status") != "succeeded":
        return StageOutcome.degraded(
            str(run.get("error_code") or "dependency_review_incomplete"),
            summary,
            coverage=coverage,
        )
    return StageOutcome.completed(summary, coverage=coverage)


def _advisory_evidence(item: Any, result: AdvisoryQueryResult) -> str:
    fixed = ",".join(item.fixed_versions) or "none"
    return (
        f"name={item.package}; version={item.version}; advisory={item.advisory_id}; "
        f"affected={item.affected}; fixed={fixed}; database={result.database_version}"
    )


def _advisory_metadata(item: Any, result: AdvisoryQueryResult) -> dict[str, Any]:
    return {
        "advisory_id": item.advisory_id,
        "affected": item.affected,
        "fixed_versions": list(item.fixed_versions),
        "withdrawn": item.withdrawn,
        "database_version": result.database_version,
        "database_source": result.source,
        "database_generated_at": result.generated_at,
        "database_queried_at": result.queried_at,
        "snapshot_sha256": result.snapshot_sha256,
    }


def _advisory_summary(result: AdvisoryQueryResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "database_version": result.database_version,
        "source": result.source,
        "generated_at": result.generated_at,
        "queried_at": result.queried_at,
        "snapshot_sha256": result.snapshot_sha256,
        "advisory_count": len(result.advisories),
        "package_metadata_count": len(result.packages),
        "error_code": result.error_code,
    }


def _target_summary(evidence: Sequence[_RuntimeEvidence]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": item.run_id,
            "astrbot_version": item.astrbot_version,
            "python_version": item.python_version,
            "result_sha256": item.result.result_sha256,
            "sbom_sha256": item.sbom_sha256,
            "package_count": len(item.result.install.packages),
            "pip_check": item.result.install.pip_check.status.value,
            "core_before_sha256": item.result.install.core_before_sha256,
            "core_after_sha256": item.result.install.core_after_sha256,
        }
        for item in evidence
    ]


def _package_snapshot_sha256(evidence: Sequence[_RuntimeEvidence]) -> str:
    payload = [
        {
            "astrbot_version": item.astrbot_version,
            "python_version": item.python_version,
            "packages": [
                package.model_dump(mode="json") for package in item.result.install.packages
            ],
        }
        for item in evidence
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _findings_sha256(findings: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "fingerprint": item["fingerprint"],
            "severity": item["severity"],
            "rule_id": item["rule_id"],
        }
        for item in sorted(findings, key=lambda item: str(item["fingerprint"]))
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _dependency_run_key(artifact_id: str, policy_id: str, provider_version: str) -> str:
    digest = hashlib.sha256(
        "\x00".join((artifact_id, policy_id, provider_version)).encode()
    ).hexdigest()
    return f"dependency-run:{digest}"


def _safe_error_code(error: Exception, fallback: str) -> str:
    candidate = str(getattr(error, "code", "") or str(error)).strip()
    return candidate if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", candidate) else fallback


__all__ = ["DEPENDENCY_STAGE_TOOL_VERSION", "DependencyStage"]
