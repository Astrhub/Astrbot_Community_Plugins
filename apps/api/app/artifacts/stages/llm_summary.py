from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..file_review import artifact_llm_budget
from ..models import JobType, ReviewStatus, risk_rank
from ..package_review import LlmError, LlmOutputInvalid, LlmProviderTimeout
from ..policy import parse_review_policy
from ..summary_review import SummaryInputBuilder, SummaryReviewService
from .base import StageContext, StageOutcome


class LlmSummaryStage:
    job_type = JobType.LLM_SUMMARY.value

    def __init__(
        self,
        service: SummaryReviewService,
        *,
        provider_config_ref: str,
    ) -> None:
        self.service = service
        self.provider_config_ref = provider_config_ref

    @property
    def tool_version(self) -> str:
        return self.service.provider.version

    async def execute(self, context: StageContext) -> StageOutcome:
        invalid = _validate_context(context)
        if invalid is not None:
            return invalid
        assert context.policy is not None
        policy = parse_review_policy(context.policy.get("policy") or {})
        llm_policy = policy.llm
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "llm_summary",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": self.service.provider.name,
                "tool_version": self.tool_version,
                "model": llm_policy.model,
                "prompt_version": llm_policy.prompt_version,
                "result_schema_version": "1",
                "policy_version_id": context.artifact.get("policy_version_id"),
                "input_sha256": str((context.job.get("payload") or {}).get("input_sha256") or ""),
                "idempotency_key": (
                    f"llm-summary-run:{context.job['id']}:attempt-{context.attempt}"
                ),
                "coverage": {"stage_name": "llm_summary"},
            }
        )
        recovered = _recovered_outcome(run)
        if recovered is not None:
            return recovered
        if not llm_policy.enabled:
            return await _record_skipped(context, run)
        if llm_policy.provider_config_ref != self.provider_config_ref:
            return await _record_degraded(
                context,
                run,
                LlmError(
                    "llm_provider_config_mismatch",
                    "LLM provider configuration does not match the fixed review policy",
                ),
            )
        try:
            runs = await context.repository.list_review_runs(str(context.artifact["id"]))
            policy_version_id = str(context.artifact.get("policy_version_id") or "")
            policy_runs = [
                item
                for item in runs
                if str(item.get("policy_version_id") or "") == policy_version_id
            ]
            findings = await context.repository.list_findings(str(context.artifact["id"]))
            policy_run_ids = {str(item.get("id") or "") for item in policy_runs}
            policy_findings = [
                item for item in findings if str(item.get("run_id") or "") in policy_run_ids
            ]
            budget = artifact_llm_budget(
                policy_runs,
                llm_policy,
                policy_version_id,
            )
            prepared = SummaryInputBuilder().build(
                policy_runs,
                policy_findings,
                remaining_tokens=budget.remaining_tokens,
                remaining_cost_microusd=budget.remaining_cost_microusd,
                policy=llm_policy,
            )
            evaluation = await self.service.evaluate(
                prepared,
                remaining_tokens=budget.remaining_tokens,
                remaining_cost_microusd=budget.remaining_cost_microusd,
                policy=llm_policy,
            )
        except (LlmError, ValidationError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, LlmError)
                else LlmError("llm_summary_input_invalid", "Summary input is invalid")
            )
            return await _record_degraded(context, run, error)

        deterministic_floor = prepared.risk_floor
        effective_risk = (
            evaluation.result.risk_level.value
            if risk_rank(evaluation.result.risk_level) >= risk_rank(deterministic_floor)
            else deterministic_floor
        )
        input_complete = bool(
            prepared.input.coverage.package_complete
            and prepared.input.coverage.file_complete
            and prepared.input.coverage.omitted_summary_files == 0
            and prepared.input.coverage.omitted_findings == 0
        )
        manual_review_required = bool(
            evaluation.result.needs_manual_review
            or not input_complete
            or risk_rank(effective_risk) >= risk_rank(policy.routing.manual_review_at.value)
        )
        coverage = {
            "outcome": "completed",
            "complete": input_complete,
            "stage_name": "llm_summary",
            "provider_call": True,
            "manual_review_required": manual_review_required,
            "model_risk_level": evaluation.result.risk_level.value,
            "deterministic_risk_floor": deterministic_floor,
            "risk_level": effective_risk,
            "review_priority": evaluation.result.review_priority.value,
            "input_token_estimate": prepared.input_token_estimate,
            "prompt_token_estimate": prepared.prompt_token_estimate,
            "max_output_tokens": prepared.max_output_tokens,
            "estimated_max_cost_microusd": prepared.estimated_max_cost_microusd,
            "usage": dict(evaluation.usage),
            "input_coverage": prepared.input.coverage.model_dump(mode="json"),
        }
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "succeeded",
                "summary": "自动审查汇总建议已生成",
                "coverage": coverage,
                "input_sha256": prepared.input_sha256,
                "output_sha256": evaluation.output_sha256,
                "raw_result": {
                    "normalized_result": evaluation.result.model_dump(mode="json"),
                    "provider_response": evaluation.raw_response,
                    "usage": dict(evaluation.usage),
                },
            },
        )
        return StageOutcome.completed("自动审查汇总建议已生成", coverage=coverage)


def _validate_context(context: StageContext) -> StageOutcome | None:
    if context.artifact.get("review_status") != ReviewStatus.SCANNING.value:
        return StageOutcome.terminal_failure(
            "artifact_not_scanning",
            "Artifact is not ready for LLM review summary",
        )
    if context.policy is None:
        return StageOutcome.terminal_failure(
            "review_policy_unavailable",
            "Artifact summary policy is unavailable",
        )
    if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "artifact_policy_snapshot_conflict",
            "Summary job does not match the artifact policy snapshot",
        )
    return None


async def _record_skipped(
    context: StageContext,
    run: Mapping[str, Any],
) -> StageOutcome:
    coverage = {
        "outcome": "skipped",
        "complete": False,
        "stage_name": "llm_summary",
        "manual_review_required": True,
        "reason": "llm_policy_disabled",
    }
    await context.repository.complete_review_run(
        str(run["id"]),
        {
            "status": "cancelled",
            "summary": "自动审查汇总已由固定策略关闭",
            "coverage": coverage,
            "raw_result": {"error_code": "llm_policy_disabled"},
            "error_code": "llm_policy_disabled",
        },
    )
    return StageOutcome.completed("自动审查汇总已跳过", coverage=coverage)


async def _record_degraded(
    context: StageContext,
    run: Mapping[str, Any],
    error: LlmError,
) -> StageOutcome:
    coverage = {
        "outcome": "degraded",
        "complete": False,
        "stage_name": "llm_summary",
        "manual_review_required": True,
        "error_code": error.code,
        "provider_call": error.attempts > 0,
    }
    if error.usage:
        coverage["usage"] = dict(error.usage)
    raw: dict[str, Any] = {"error_code": error.code}
    if isinstance(error, LlmOutputInvalid) and error.raw_response is not None:
        raw["provider_response"] = error.raw_response
    await context.repository.complete_review_run(
        str(run["id"]),
        {
            "status": "timed_out" if isinstance(error, LlmProviderTimeout) else "failed",
            "summary": "自动审查汇总不可用，必须进入人工复核",
            "coverage": coverage,
            "raw_result": raw,
            "error_code": error.code,
        },
    )
    return StageOutcome.degraded(
        error.code,
        "自动审查汇总不可用，必须进入人工复核",
        coverage=coverage,
    )


def _recovered_outcome(run: Mapping[str, Any]) -> StageOutcome | None:
    status = str(run.get("status") or "")
    coverage = dict(run.get("coverage") or {})
    if status == "succeeded":
        return StageOutcome.completed(
            "自动审查汇总副作用已完成",
            coverage={**coverage, "recovered": True},
        )
    if coverage.get("outcome") == "skipped":
        return StageOutcome.completed(
            "自动审查汇总先前已跳过",
            coverage={**coverage, "recovered": True},
        )
    if status in {"failed", "timed_out", "cancelled"}:
        return StageOutcome.degraded(
            str(run.get("error_code") or "llm_provider_unavailable"),
            "自动审查汇总先前已降级",
            coverage={**coverage, "recovered": True},
        )
    return None
