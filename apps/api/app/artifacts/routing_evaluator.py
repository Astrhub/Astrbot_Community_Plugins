from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .archive import PrecheckError, normalize_version
from .models import FindingSeverity, ReviewStatus, risk_rank
from .policy import ReviewPolicyStage, ReviewPolicyV1, ToolFailureAction


_DETERMINISTIC_GATE_TYPES = {
    ReviewPolicyStage.STATIC.value,
    ReviewPolicyStage.DIFF.value,
    ReviewPolicyStage.IMPORT_GRAPH.value,
    ReviewPolicyStage.RUNTIME.value,
    ReviewPolicyStage.CLAMAV.value,
    ReviewPolicyStage.YARA.value,
    ReviewPolicyStage.DEPENDENCY.value,
}


class RouteKind(StrEnum):
    AUTO_REJECT = "auto_reject"
    MANUAL_REVIEW = "manual_review"
    AUTO_APPROVE = "auto_approve"


@dataclass(frozen=True, slots=True)
class RoutingUnitResult:
    name: str
    run_type: str
    run_id: str
    status: str
    outcome: str
    complete: bool
    tool_name: str
    tool_version: str
    error_code: str

    def audit_value(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "run_type": self.run_type,
            "run_id": self.run_id,
            "status": self.status,
            "outcome": self.outcome,
            "complete": self.complete,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class RoutingEvaluation:
    kind: RouteKind
    target_status: str
    reason_codes: tuple[str, ...]
    summary: str
    risk_level: str
    complete: bool
    version_match: bool
    input_run_ids: tuple[str, ...]
    input_fingerprints: tuple[str, ...]
    coverage_sha256: str
    coverage: Mapping[str, Any]


class RoutingEvaluator:
    def evaluate(
        self,
        *,
        artifact: Mapping[str, Any],
        policy: ReviewPolicyV1,
        runs: Sequence[Mapping[str, Any]],
        findings: Sequence[Mapping[str, Any]],
    ) -> RoutingEvaluation:
        policy_id = str(artifact.get("policy_version_id") or "")
        if not policy_id:
            raise ValueError("review_policy_unavailable")
        current_runs = [
            item for item in runs if str(item.get("policy_version_id") or "") == policy_id
        ]
        current_run_ids = {str(item.get("id") or "") for item in current_runs}
        units, missing = _required_unit_results(policy, current_runs)
        open_findings = [
            item
            for item in findings
            if str(item.get("status") or "open") == "open"
            and str(item.get("run_id") or "") in current_run_ids
        ]
        open_findings.sort(
            key=lambda item: (
                -risk_rank(str(item.get("severity") or FindingSeverity.INFO.value)),
                str(item.get("fingerprint") or ""),
            )
        )
        fingerprints = tuple(
            sorted(
                {
                    str(item.get("fingerprint") or "")
                    for item in open_findings
                    if str(item.get("fingerprint") or "")
                }
            )
        )
        input_run_ids = tuple(sorted({item.run_id for item in units if item.run_id}))
        version_match = _version_match(artifact)
        degraded = [
            item
            for item in units
            if item.status != "succeeded" or item.outcome in {"degraded", "failed", "skipped"}
        ]
        blocked = [item for item in units if item.outcome == "blocked"]
        deterministic_degraded = [
            item for item in degraded if item.run_type in _DETERMINISTIC_GATE_TYPES
        ]
        advisory_degraded = [
            item for item in degraded if item.run_type not in _DETERMINISTIC_GATE_TYPES
        ]
        deterministic_blocked = [
            item for item in blocked if item.run_type in _DETERMINISTIC_GATE_TYPES
        ]
        advisory_blocked = [
            item for item in blocked if item.run_type not in _DETERMINISTIC_GATE_TYPES
        ]
        incomplete = [
            item
            for item in units
            if not item.complete and item not in degraded and item not in blocked
        ]
        deterministic_blockers = [
            item
            for item in open_findings
            if bool(item.get("deterministic"))
            and risk_rank(str(item.get("severity") or "info"))
            >= risk_rank(policy.routing.deterministic_reject_at.value)
        ]
        manual_findings = [
            item
            for item in open_findings
            if risk_rank(str(item.get("severity") or "info"))
            >= risk_rank(policy.routing.manual_review_at.value)
        ]
        risk_level = _highest_finding_risk(open_findings)
        complete = not missing and not degraded and not blocked and not incomplete

        if deterministic_blockers or deterministic_blocked:
            kind = RouteKind.AUTO_REJECT
            reasons = _reasons(
                "deterministic_finding_reject"
                if deterministic_blockers
                else "required_stage_blocked"
            )
        elif (
            deterministic_degraded
            and policy.routing.degraded_action is ToolFailureAction.FAIL_CLOSED
        ):
            kind = RouteKind.AUTO_REJECT
            reasons = _reasons("required_stage_degraded", "policy_fail_closed")
        elif (
            missing
            or degraded
            or advisory_blocked
            or incomplete
            or not version_match
            or manual_findings
        ):
            kind = RouteKind.MANUAL_REVIEW
            reasons = _reasons(
                "required_stage_missing" if missing else "",
                "required_stage_degraded" if degraded else "",
                "advisory_stage_degraded" if advisory_degraded else "",
                "advisory_stage_blocked" if advisory_blocked else "",
                "required_stage_incomplete" if incomplete else "",
                "artifact_repo_version_mismatch" if not version_match else "",
                "finding_requires_manual_review" if manual_findings else "",
            )
        elif not policy.routing.auto_approve:
            kind = RouteKind.MANUAL_REVIEW
            reasons = ("auto_approve_disabled",)
        else:
            kind = RouteKind.AUTO_APPROVE
            reasons = ("all_auto_approve_gates_passed",)

        target_status = {
            RouteKind.AUTO_REJECT: ReviewStatus.REJECTED.value,
            RouteKind.MANUAL_REVIEW: ReviewStatus.PENDING_REVIEW.value,
            RouteKind.AUTO_APPROVE: ReviewStatus.APPROVED.value,
        }[kind]
        audit = {
            "schema_version": "1",
            "policy_version_id": policy_id,
            "route": kind.value,
            "target_status": target_status,
            "reason_codes": list(reasons),
            "risk_level": risk_level,
            "complete": complete,
            "version_match": version_match,
            "required_units": [item.audit_value() for item in units],
            "missing_units": list(missing),
            "input_run_ids": list(input_run_ids),
            "input_fingerprints": list(fingerprints),
            "finding_summary": {
                "open_count": len(open_findings),
                "manual_count": len(manual_findings),
                "deterministic_blocker_count": len(deterministic_blockers),
                "advisory_degraded_count": len(advisory_degraded),
                "advisory_blocked_count": len(advisory_blocked),
            },
        }
        canonical = json.dumps(
            audit,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        coverage_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
        coverage = {**audit, "coverage_sha256": coverage_sha256}
        return RoutingEvaluation(
            kind=kind,
            target_status=target_status,
            reason_codes=reasons,
            summary=_summary(kind, reasons),
            risk_level=risk_level,
            complete=complete,
            version_match=version_match,
            input_run_ids=input_run_ids,
            input_fingerprints=fingerprints,
            coverage_sha256=coverage_sha256,
            coverage=coverage,
        )


def _required_unit_results(
    policy: ReviewPolicyV1,
    runs: Sequence[Mapping[str, Any]],
) -> tuple[tuple[RoutingUnitResult, ...], tuple[str, ...]]:
    expected: list[tuple[str, str, str, str]] = []
    for stage in policy.required_stages:
        if stage is ReviewPolicyStage.RUNTIME:
            expected.extend(
                (
                    f"runtime:{target.astrbot}:python-{target.python}",
                    ReviewPolicyStage.RUNTIME.value,
                    target.astrbot,
                    target.python,
                )
                for target in policy.runtime_targets
            )
        else:
            expected.append((stage.value, stage.value, "", ""))

    values: list[RoutingUnitResult] = []
    missing: list[str] = []
    for stage_name, run_type, astrbot_version, python_version in expected:
        run = _latest_unit_run(
            runs,
            stage_name,
            run_type,
            astrbot_version=astrbot_version,
            python_version=python_version,
        )
        if run is None:
            missing.append(stage_name)
            continue
        coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
        outcome = str(coverage.get("outcome") or "")
        status = str(run.get("status") or "")
        complete = bool(
            status == "succeeded"
            and outcome == "completed"
            and coverage.get("complete", True) is not False
        )
        values.append(
            RoutingUnitResult(
                name=stage_name,
                run_type=run_type,
                run_id=str(run.get("id") or ""),
                status=status,
                outcome=outcome or ("completed" if status == "succeeded" else "failed"),
                complete=complete,
                tool_name=str(run.get("tool_name") or ""),
                tool_version=str(run.get("tool_version") or ""),
                error_code=str(run.get("error_code") or coverage.get("error_code") or ""),
            )
        )
    values.sort(key=lambda item: item.name)
    return tuple(values), tuple(sorted(missing))


def _latest_unit_run(
    runs: Sequence[Mapping[str, Any]],
    stage_name: str,
    run_type: str,
    *,
    astrbot_version: str = "",
    python_version: str = "",
) -> Mapping[str, Any] | None:
    for run in reversed(runs):
        if str(run.get("type") or "") != run_type:
            continue
        if run_type == ReviewPolicyStage.RUNTIME.value:
            if (
                str(run.get("astrbot_version") or "") != astrbot_version
                or str(run.get("python_version") or "") != python_version
            ):
                continue
            return run
        coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
        if str(coverage.get("stage_name") or run.get("type") or "") == stage_name:
            return run
    return None


def _version_match(artifact: Mapping[str, Any]) -> bool:
    version = str(artifact.get("version") or "").strip()
    repo_version = str(artifact.get("repo_version") or "").strip()
    normalized = str(artifact.get("normalized_version") or "").strip()
    if not version or not repo_version or not normalized or version != repo_version:
        return False
    try:
        return normalize_version(version) == normalized == normalize_version(repo_version)
    except PrecheckError:
        return False


def _highest_finding_risk(findings: Sequence[Mapping[str, Any]]) -> str:
    severity = max(
        (str(item.get("severity") or FindingSeverity.INFO.value) for item in findings),
        key=risk_rank,
        default=FindingSeverity.INFO.value,
    )
    return "none" if severity == FindingSeverity.INFO.value else severity


def _reasons(*values: str) -> tuple[str, ...]:
    return tuple(value for value in values if value)


def _summary(kind: RouteKind, reasons: Sequence[str]) -> str:
    label = {
        RouteKind.AUTO_REJECT: "确定性门禁拒绝候选版本",
        RouteKind.MANUAL_REVIEW: "候选版本需要人工复核",
        RouteKind.AUTO_APPROVE: "候选版本满足自动通过门禁",
    }[kind]
    return f"{label}：{', '.join(reasons)}"


__all__ = [
    "RouteKind",
    "RoutingEvaluation",
    "RoutingEvaluator",
    "RoutingUnitResult",
]
