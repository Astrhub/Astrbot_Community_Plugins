from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..diff import (
    DIFF_TOOL_NAME,
    DIFF_TOOL_VERSION,
    ArtifactDiffService,
    DiffBuildError,
    diff_output_sha256,
    validate_hunk_payload,
)
from ..models import JobType, ReviewStatus
from ..policy import parse_review_policy
from ..storage import ArtifactStorageError
from .base import StageContext, StageOutcome


class DiffGraphStage:
    job_type = JobType.DIFF_GRAPH.value

    def __init__(self, service: ArtifactDiffService | None = None) -> None:
        self.service = service or ArtifactDiffService()

    async def execute(self, context: StageContext) -> StageOutcome:
        if context.artifact.get("review_status") != ReviewStatus.SCANNING.value:
            return StageOutcome.terminal_failure(
                "artifact_not_scanning",
                "Artifact is not ready for diff generation",
            )
        if context.policy is None:
            return StageOutcome.terminal_failure(
                "review_policy_unavailable",
                "Artifact diff policy is unavailable",
            )
        if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
            return StageOutcome.terminal_failure(
                "artifact_policy_snapshot_conflict",
                "Diff job policy does not match the artifact snapshot",
            )
        payload = context.job.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if str(payload.get("stage") or "") != "diff":
            return StageOutcome.terminal_failure(
                "diff_graph_stage_unsupported",
                "The diff worker cannot execute this diff_graph stage",
            )
        if str(payload.get("tool_version") or "") != DIFF_TOOL_VERSION:
            return StageOutcome.terminal_failure(
                "diff_tool_version_conflict",
                "Diff job tool version does not match the worker",
            )

        recovered = await self._recover_completed(context)
        if recovered is not None:
            return recovered
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "diff",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": DIFF_TOOL_NAME,
                "tool_version": DIFF_TOOL_VERSION,
                "policy_version_id": context.artifact.get("policy_version_id"),
                "input_sha256": str(payload.get("input_sha256") or ""),
                "idempotency_key": f"diff-run:{context.job['id']}:attempt-{context.attempt}",
                "coverage": {"stage_name": "diff", "outcome": "running"},
            }
        )
        existing = _outcome_from_run(run)
        if existing is not None:
            return existing

        policy = parse_review_policy(context.policy.get("policy") or {})
        forced_paths = set(policy.llm.required_files)
        try:
            result = await self.service.build(
                artifact=context.artifact,
                repository=context.repository,
                storage=context.storage,
                forced_paths=forced_paths,
            )
        except DiffBuildError as exc:
            if exc.retryable:
                return StageOutcome.retryable_failure(exc.code, str(exc))
            return StageOutcome.terminal_failure(exc.code, str(exc))

        summary = (
            "文件差异已生成，审查范围已退化为全量" if result.degraded_code else "文件差异已生成"
        )
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "succeeded",
                "summary": summary,
                "input_sha256": result.input_sha256,
                "output_sha256": result.output_sha256,
                "coverage": dict(result.coverage),
                "raw_result": {
                    "counts": dict(result.coverage.get("counts") or {}),
                    "complete": bool(result.coverage.get("complete")),
                    "full_review_required": bool(result.coverage.get("full_review_required")),
                    "reason": result.degraded_code,
                },
            },
        )
        if result.blocking_code:
            return StageOutcome.degraded(
                result.blocking_code,
                summary,
                coverage=result.coverage,
            )
        return StageOutcome.completed(summary, coverage=result.coverage)

    async def _recover_completed(self, context: StageContext) -> StageOutcome | None:
        runs = await context.repository.list_review_runs(str(context.artifact["id"]))
        for run in reversed(runs):
            if (
                run.get("type") != "diff"
                or run.get("status") != "succeeded"
                or run.get("policy_version_id") != context.artifact.get("policy_version_id")
                or run.get("tool_version") != DIFF_TOOL_VERSION
            ):
                continue
            coverage = run.get("coverage")
            coverage = coverage if isinstance(coverage, Mapping) else {}
            if (
                coverage.get("stage_name") != "diff"
                or coverage.get("current_tree_sha256") != context.artifact.get("tree_sha256")
                or coverage.get("requested_base_artifact_id")
                != context.artifact.get("base_artifact_id")
            ):
                continue
            compared_base_id = str(coverage.get("compared_base_artifact_id") or "") or None
            base_tree_sha256 = str(coverage.get("base_tree_sha256") or "") or None
            if compared_base_id:
                base = await context.repository.get_artifact(compared_base_id)
                if (
                    base is None
                    or base.get("plugin_id") != context.artifact.get("plugin_id")
                    or base.get("tree_sha256") != base_tree_sha256
                ):
                    continue
            diffs = await context.repository.list_artifact_diffs(str(context.artifact["id"]))
            if int(coverage.get("file_count") or 0) != len(diffs):
                continue
            expected_output_sha256 = str(coverage.get("output_sha256") or "")
            if (
                not expected_output_sha256
                or run.get("output_sha256") != expected_output_sha256
                or diff_output_sha256(diffs) != expected_output_sha256
                or run.get("input_sha256") != coverage.get("input_sha256")
            ):
                continue
            if any(
                item.get("current_tree_sha256") != context.artifact.get("tree_sha256")
                or item.get("base_tree_sha256") != base_tree_sha256
                or (str(item.get("base_artifact_id") or "") or None) != compared_base_id
                for item in diffs
            ):
                continue
            try:
                await self._validate_stored_hunks(
                    context,
                    diffs,
                    current_tree_sha256=str(context.artifact.get("tree_sha256") or ""),
                    base_tree_sha256=base_tree_sha256,
                )
            except (ArtifactStorageError, DiffBuildError):
                continue
            recovered_coverage = {**dict(coverage), "recovered": True}
            reason = str(coverage.get("reason") or "")
            if coverage.get("outcome") == "degraded" and reason:
                return StageOutcome.degraded(
                    reason,
                    "Existing artifact diff was validated and recovered",
                    coverage=recovered_coverage,
                )
            return StageOutcome.completed(
                "Existing artifact diff was validated and recovered",
                coverage=recovered_coverage,
            )
        return None

    async def _validate_stored_hunks(
        self,
        context: StageContext,
        diffs: list[dict[str, Any]],
        *,
        current_tree_sha256: str,
        base_tree_sha256: str | None,
    ) -> None:
        for diff in diffs:
            key = str(diff.get("hunks_key") or "")
            if not key:
                continue
            stats = diff.get("stats")
            stats = stats if isinstance(stats, Mapping) else {}
            expected_sha256 = str(stats.get("hunks_sha256") or "")
            if not expected_sha256:
                raise DiffBuildError("diff_hunk_invalid", "Diff hunk hash is missing")
            payload = await context.storage.read_text_content(
                key,
                self.service.limits.max_hunk_bytes,
                expected_sha256,
            )
            validate_hunk_payload(
                payload,
                diff=diff,
                artifact_id=str(context.artifact["id"]),
                current_tree_sha256=current_tree_sha256,
                base_tree_sha256=base_tree_sha256,
            )


def _outcome_from_run(run: Mapping[str, Any]) -> StageOutcome | None:
    status = str(run.get("status") or "")
    coverage = run.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    if status == "succeeded":
        return StageOutcome.retryable_failure(
            "diff_recovery_invalid",
            "Completed diff run could not be validated from persisted side effects",
        )
    return None
