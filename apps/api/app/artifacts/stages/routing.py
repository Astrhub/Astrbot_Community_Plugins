from __future__ import annotations

from ..models import JobType, ReviewStatus
from ..orchestration import ROUTING_STAGE_NAME
from .base import StageContext, StageOutcome


class RoutingStage:
    job_type = JobType.ROUTE_REVIEW.value

    async def execute(self, context: StageContext) -> StageOutcome:
        if context.artifact["review_status"] == ReviewStatus.PENDING_REVIEW.value:
            runs = await context.repository.list_review_runs(str(context.artifact["id"]))
            if any(
                run["type"] == ROUTING_STAGE_NAME
                and run["status"] == "succeeded"
                and run.get("policy_version_id") == context.artifact.get("policy_version_id")
                for run in runs
            ):
                return StageOutcome.completed(
                    "Artifact is already pending manual review",
                    coverage={"route": ReviewStatus.PENDING_REVIEW.value, "recovered": True},
                )
        if context.artifact["review_status"] != ReviewStatus.SCANNING.value:
            return StageOutcome.terminal_failure(
                "artifact_not_scanning",
                "Artifact is not ready for automated routing",
            )
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

        payload = context.job.get("payload") or {}
        stage_states = dict(payload.get("stage_states") or {})
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": ROUTING_STAGE_NAME,
                "status": "running",
                "attempt": context.attempt,
                "tool_name": ROUTING_STAGE_NAME,
                "tool_version": str(payload.get("tool_version") or "routing-v1"),
                "policy_version_id": context.artifact.get("policy_version_id"),
                "coverage": {"stage_name": ROUTING_STAGE_NAME},
            }
        )
        coverage = {
            "outcome": "completed",
            "stage_name": ROUTING_STAGE_NAME,
            "route": ReviewStatus.PENDING_REVIEW.value,
            "stage_states": stage_states,
            "auto_approved": False,
        }
        await context.repository.complete_review_run(
            run["id"],
            {
                "status": "succeeded",
                "summary": "自动阶段完成，进入人工复核",
                "coverage": coverage,
                "raw_result": {
                    "route": ReviewStatus.PENDING_REVIEW.value,
                    "stage_states": stage_states,
                },
            },
        )
        await context.repository.update_artifact_review_coverage(
            str(context.artifact["id"]),
            {
                "policy_version_id": context.artifact["policy_version_id"],
                "stages": stage_states,
                "routing": ReviewStatus.PENDING_REVIEW.value,
            },
            automated_review_completed=True,
        )
        pending = await context.repository.transition_review_status(
            str(context.artifact["id"]),
            ReviewStatus.PENDING_REVIEW.value,
        )
        if pending is None:
            return StageOutcome.terminal_failure(
                "artifact_state_changed",
                "Artifact left scanning state during routing",
            )
        await context.with_snapshots(artifact=pending).emit_status(
            "artifact_pending_review",
            "pending-review",
        )
        return StageOutcome.completed(
            "自动阶段完成，进入人工复核",
            coverage=coverage,
        )
