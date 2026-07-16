from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import JobType, ReviewStatus
from ..orchestration import ROUTING_STAGE_NAME
from ..policy import parse_review_policy
from ..routing_evaluator import RouteKind, RoutingEvaluation, RoutingEvaluator
from .base import StageContext, StageOutcome


class RoutingStage:
    job_type = JobType.ROUTE_REVIEW.value

    def __init__(self, evaluator: RoutingEvaluator | None = None) -> None:
        self.evaluator = evaluator or RoutingEvaluator()

    async def execute(self, context: StageContext) -> StageOutcome:
        invalid = _validate_snapshot(context)
        if invalid is not None:
            return invalid
        recovered = await _recovered_outcome(context, self.evaluator)
        if recovered is not None:
            return recovered
        if context.artifact["review_status"] != ReviewStatus.SCANNING.value:
            return StageOutcome.terminal_failure(
                "artifact_not_scanning",
                "Artifact is not ready for automated routing",
            )
        assert context.policy is not None
        policy = parse_review_policy(context.policy.get("policy") or {})
        artifact_id = str(context.artifact["id"])
        runs = await context.repository.list_review_runs(artifact_id)
        findings = await context.repository.list_findings(artifact_id)
        evaluation = self.evaluator.evaluate(
            artifact=context.artifact,
            policy=policy,
            runs=runs,
            findings=findings,
        )
        run = await context.repository.create_review_run(
            {
                "artifact_id": artifact_id,
                "type": ROUTING_STAGE_NAME,
                "status": "running",
                "attempt": context.attempt,
                "tool_name": ROUTING_STAGE_NAME,
                "tool_version": str(
                    (context.job.get("payload") or {}).get("tool_version") or "routing-v1"
                ),
                "policy_version_id": context.artifact.get("policy_version_id"),
                "input_sha256": evaluation.coverage_sha256,
                "idempotency_key": (
                    f"routing-run:{context.job['id']}:attempt-{context.attempt}"
                ),
                "coverage": {"stage_name": ROUTING_STAGE_NAME},
            }
        )
        existing = _terminal_run_outcome(run)
        if existing is not None:
            return existing

        try:
            routed = await self._apply_route(context, evaluation)
        except (ValueError, TypeError) as exc:
            await context.repository.complete_review_run(
                str(run["id"]),
                {
                    "status": "failed",
                    "summary": "自动路由状态已变化，未应用新的决定",
                    "coverage": {
                        "outcome": "failed",
                        "complete": False,
                        "stage_name": ROUTING_STAGE_NAME,
                        "route": evaluation.kind.value,
                        "target_status": evaluation.target_status,
                        "error_code": "artifact_route_conflict",
                    },
                    "error_code": "artifact_route_conflict",
                    "raw_result": {"private_error_type": type(exc).__name__},
                },
            )
            return StageOutcome.terminal_failure(
                "artifact_route_conflict",
                "Artifact routing state changed concurrently",
            )
        if routed is None:
            return StageOutcome.terminal_failure(
                "artifact_not_found",
                "Artifact disappeared during automated routing",
            )

        coverage = _completed_coverage(evaluation)
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "succeeded",
                "summary": evaluation.summary,
                "coverage": coverage,
                "input_sha256": evaluation.coverage_sha256,
                "output_sha256": evaluation.coverage_sha256,
                "raw_result": {"normalized_evaluation": dict(evaluation.coverage)},
            },
        )
        await context.repository.update_artifact_review_coverage(
            artifact_id,
            {
                "policy_version_id": context.artifact["policy_version_id"],
                "routing": dict(evaluation.coverage),
            },
            automated_review_completed=True,
        )
        await _emit_route_status(context, routed, evaluation)
        return StageOutcome.completed(evaluation.summary, coverage=coverage)

    async def _apply_route(
        self,
        context: StageContext,
        evaluation: RoutingEvaluation,
    ) -> dict[str, Any] | None:
        artifact_id = str(context.artifact["id"])
        policy_id = str(context.artifact["policy_version_id"])
        decision_key = (
            f"routing:{artifact_id}:{policy_id}:{evaluation.kind.value}:"
            f"{evaluation.coverage_sha256}"
        )
        metadata = {"routing": dict(evaluation.coverage)}
        if evaluation.kind is RouteKind.AUTO_REJECT:
            return await context.repository.decide_artifact(
                artifact_id,
                action="auto_reject",
                target_status=ReviewStatus.REJECTED.value,
                reason=evaluation.summary,
                reviewer=None,
                idempotency_key=decision_key,
                policy_version_id=policy_id,
                source="policy",
                input_run_ids=evaluation.input_run_ids,
                input_fingerprints=evaluation.input_fingerprints,
                coverage_sha256=evaluation.coverage_sha256,
                metadata=metadata,
                risk_level=evaluation.risk_level,
                rejection_code=evaluation.reason_codes[0],
            )
        if evaluation.kind is RouteKind.AUTO_APPROVE:
            return await context.repository.auto_approve_artifact(
                artifact_id,
                reason=evaluation.summary,
                expected_repo_version=str(context.artifact.get("repo_version") or ""),
                expected_normalized_version=str(
                    context.artifact.get("normalized_version") or ""
                ),
                expected_version=str(context.artifact.get("version") or ""),
                idempotency_key=decision_key,
                policy_version_id=policy_id,
                input_run_ids=evaluation.input_run_ids,
                input_fingerprints=evaluation.input_fingerprints,
                coverage_sha256=evaluation.coverage_sha256,
                metadata=metadata,
                risk_level=evaluation.risk_level,
            )
        return await context.repository.transition_review_status(
            artifact_id,
            ReviewStatus.PENDING_REVIEW.value,
            risk_level=evaluation.risk_level,
        )


def _validate_snapshot(context: StageContext) -> StageOutcome | None:
    if context.policy is None or not context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "review_policy_unavailable",
            "Artifact has no available fixed review policy snapshot",
        )
    if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "artifact_policy_snapshot_conflict",
            "Routing job policy does not match the artifact snapshot",
        )
    return None


async def _recovered_outcome(
    context: StageContext,
    evaluator: RoutingEvaluator,
) -> StageOutcome | None:
    status = str(context.artifact.get("review_status") or "")
    if status == ReviewStatus.SCANNING.value:
        return None
    if status not in {
        ReviewStatus.PENDING_REVIEW.value,
        ReviewStatus.APPROVED.value,
        ReviewStatus.REJECTED.value,
    }:
        return None
    policy_id = str(context.artifact.get("policy_version_id") or "")
    runs = await context.repository.list_review_runs(str(context.artifact["id"]))
    for run in reversed(runs):
        coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
        if (
            run.get("type") == ROUTING_STAGE_NAME
            and run.get("status") == "succeeded"
            and str(run.get("policy_version_id") or "") == policy_id
            and str(coverage.get("target_status") or "") == status
        ):
            return StageOutcome.completed(
                "Automated routing side effects were already applied",
                coverage={**dict(coverage), "recovered": True},
            )
    assert context.policy is not None
    policy = parse_review_policy(context.policy.get("policy") or {})
    findings = await context.repository.list_findings(str(context.artifact["id"]))
    evaluation = evaluator.evaluate(
        artifact=context.artifact,
        policy=policy,
        runs=runs,
        findings=findings,
    )
    job_run_prefix = f"routing-run:{context.job['id']}:"
    interrupted_run = next(
        (
            run
            for run in reversed(runs)
            if run.get("type") == ROUTING_STAGE_NAME
            and run.get("status") == "running"
            and str(run.get("policy_version_id") or "") == policy_id
            and str(run.get("input_sha256") or "") == evaluation.coverage_sha256
            and str(run.get("idempotency_key") or "").startswith(job_run_prefix)
        ),
        None,
    )
    if interrupted_run is None:
        return None
    if evaluation.target_status != status or not await _route_side_effect_matches(
        context, evaluation
    ):
        conflict_coverage = {
            "outcome": "failed",
            "complete": False,
            "stage_name": ROUTING_STAGE_NAME,
            "route": evaluation.kind.value,
            "target_status": evaluation.target_status,
            "observed_status": status,
            "error_code": "artifact_route_conflict",
        }
        await context.repository.complete_review_run(
            str(interrupted_run["id"]),
            {
                "status": "failed",
                "summary": "自动路由被并发人工决定终止",
                "coverage": conflict_coverage,
                "error_code": "artifact_route_conflict",
                "input_sha256": evaluation.coverage_sha256,
            },
        )
        return StageOutcome.terminal_failure(
            "artifact_route_conflict",
            "Artifact was decided by another reviewer while routing was in progress",
            details={"coverage": conflict_coverage},
        )

    coverage = _completed_coverage(evaluation)
    await context.repository.complete_review_run(
        str(interrupted_run["id"]),
        {
            "status": "succeeded",
            "summary": evaluation.summary,
            "coverage": coverage,
            "input_sha256": evaluation.coverage_sha256,
            "output_sha256": evaluation.coverage_sha256,
            "raw_result": {"normalized_evaluation": dict(evaluation.coverage)},
        },
    )
    await context.repository.update_artifact_review_coverage(
        str(context.artifact["id"]),
        {
            "policy_version_id": policy_id,
            "routing": dict(evaluation.coverage),
        },
        automated_review_completed=True,
    )
    await _emit_route_status(context, context.artifact, evaluation)
    return StageOutcome.completed(
        "Automated routing recovered an interrupted completion",
        coverage={**coverage, "recovered": True},
    )


async def _route_side_effect_matches(
    context: StageContext,
    evaluation: RoutingEvaluation,
) -> bool:
    if evaluation.kind is RouteKind.MANUAL_REVIEW:
        return True
    expected_action = {
        RouteKind.AUTO_APPROVE: "auto_approve",
        RouteKind.AUTO_REJECT: "auto_reject",
    }[evaluation.kind]
    decisions = await context.repository.list_review_decisions(str(context.artifact["id"]))
    return any(
        decision.get("action") == expected_action
        and decision.get("source") == "policy"
        and str(decision.get("policy_version_id") or "")
        == str(context.artifact.get("policy_version_id") or "")
        and str(decision.get("coverage_sha256") or "") == evaluation.coverage_sha256
        and str(decision.get("to_status") or "") == evaluation.target_status
        for decision in decisions
    )


def _completed_coverage(evaluation: RoutingEvaluation) -> dict[str, Any]:
    return {
        **dict(evaluation.coverage),
        "outcome": "completed",
        "stage_name": ROUTING_STAGE_NAME,
        "route": evaluation.kind.value,
        "target_status": evaluation.target_status,
        "auto_approved": evaluation.kind is RouteKind.AUTO_APPROVE,
        "auto_rejected": evaluation.kind is RouteKind.AUTO_REJECT,
        "manual_review_required": evaluation.kind is RouteKind.MANUAL_REVIEW,
    }


def _terminal_run_outcome(run: Mapping[str, Any]) -> StageOutcome | None:
    coverage = dict(run.get("coverage") or {})
    if run.get("status") == "succeeded":
        return StageOutcome.completed(
            "Automated routing run was already completed",
            coverage={**coverage, "recovered": True},
        )
    if run.get("status") in {"failed", "timed_out", "cancelled"}:
        return StageOutcome.terminal_failure(
            str(run.get("error_code") or "artifact_route_conflict"),
            "Automated routing run previously failed",
            details={"coverage": {**coverage, "recovered": True}},
        )
    return None


async def _emit_route_status(
    context: StageContext,
    artifact: Mapping[str, Any],
    evaluation: RoutingEvaluation,
) -> None:
    event_type, suffix = {
        RouteKind.AUTO_REJECT: ("artifact_rejected", "auto-rejected"),
        RouteKind.MANUAL_REVIEW: ("artifact_pending_review", "pending-review"),
        RouteKind.AUTO_APPROVE: ("artifact_approved", "auto-approved"),
    }[evaluation.kind]
    await context.with_snapshots(artifact=artifact).emit_status(
        event_type,
        suffix,
        {"reason_codes": list(evaluation.reason_codes)},
    )
