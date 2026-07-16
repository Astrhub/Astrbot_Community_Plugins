from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..models import JobType, ReviewStatus, risk_rank
from ..package_review import (
    LlmBudgetExceeded,
    LlmError,
    LlmOutputInvalid,
    LlmProviderTimeout,
    PackageInputBuilder,
    PackageReviewService,
)
from ..policy import parse_review_policy
from ..storage import ArtifactStorageError
from .base import StageContext, StageOutcome


class LlmPackageStage:
    job_type = JobType.LLM_PACKAGE.value

    def __init__(
        self,
        service: PackageReviewService,
        *,
        provider_config_ref: str,
    ) -> None:
        self.service = service
        self.provider_config_ref = provider_config_ref

    @property
    def tool_version(self) -> str:
        return self.service.provider.version

    async def execute(self, context: StageContext) -> StageOutcome:
        if context.artifact.get("review_status") != ReviewStatus.SCANNING.value:
            return StageOutcome.terminal_failure(
                "artifact_not_scanning",
                "Artifact is not ready for package-level LLM review",
            )
        if context.policy is None:
            return StageOutcome.terminal_failure(
                "review_policy_unavailable",
                "Artifact package review policy is unavailable",
            )
        if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
            return StageOutcome.terminal_failure(
                "artifact_policy_snapshot_conflict",
                "Package review job does not match the artifact policy snapshot",
            )
        policy = parse_review_policy(context.policy.get("policy") or {})
        llm_policy = policy.llm
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "llm_package",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": self.service.provider.name,
                "tool_version": self.tool_version,
                "model": llm_policy.model,
                "prompt_version": llm_policy.prompt_version,
                "result_schema_version": "1",
                "policy_version_id": context.artifact.get("policy_version_id"),
                "input_sha256": str(
                    (context.job.get("payload") or {}).get("input_sha256") or ""
                ),
                "idempotency_key": (
                    f"llm-package-run:{context.job['id']}:attempt-{context.attempt}"
                ),
                "coverage": {"stage_name": "llm_package"},
            }
        )
        recovered = _recovered_outcome(run)
        if recovered is not None:
            return recovered
        if not llm_policy.enabled:
            return await self._record_skipped(context, run)
        if llm_policy.provider_config_ref != self.provider_config_ref:
            return await self._record_degraded(
                context,
                run,
                LlmError(
                    "llm_provider_config_mismatch",
                    "LLM provider configuration does not match the fixed review policy",
                ),
            )

        input_sha256 = ""
        prepared = None
        try:
            prepared = await PackageInputBuilder(
                context.repository,
                context.storage,
            ).build(
                context.artifact,
                llm_policy,
                required_stages=policy.required_stages,
                routing=policy.routing.model_dump(mode="json"),
                allowed_categories=policy.category.allowed_categories,
            )
            input_sha256 = prepared.input_sha256
            manifest = await context.repository.list_artifact_files(
                str(context.artifact["id"])
            )
            evaluation = await self.service.evaluate(
                prepared,
                manifest=manifest,
                policy=llm_policy,
            )
        except ArtifactStorageError:
            return await self._record_degraded(
                context,
                run,
                LlmError(
                    "llm_package_input_unavailable",
                    "Package review input content is unavailable",
                ),
                input_sha256=input_sha256,
                prepared=prepared,
            )
        except (ValidationError, ValueError):
            return await self._record_degraded(
                context,
                run,
                LlmError(
                    "llm_package_input_invalid",
                    "Package review input failed server-side validation",
                ),
                input_sha256=input_sha256,
                prepared=prepared,
            )
        except LlmError as exc:
            return await self._record_degraded(
                context,
                run,
                exc,
                input_sha256=input_sha256,
                prepared=prepared,
            )

        input_complete = bool(prepared.input.coverage.complete)
        deterministic_risk = _highest_deterministic_risk(prepared)
        effective_risk = _higher_risk(evaluation.result.risk_level.value, deterministic_risk)
        manual_threshold = policy.routing.manual_review_at.value
        manual_review_required = bool(
            evaluation.result.needs_manual_review or not input_complete
            or risk_rank(effective_risk) >= risk_rank(manual_threshold)
        )
        coverage = {
            "outcome": "completed",
            "complete": input_complete,
            "stage_name": "llm_package",
            "manual_review_required": manual_review_required,
            "risk_level": effective_risk,
            "model_risk_level": evaluation.result.risk_level.value,
            "deterministic_risk_floor": deterministic_risk,
            "suggested_file_count": len(evaluation.result.suggested_files),
            "suggested_category": (
                evaluation.result.suggested_category.value
                if evaluation.result.suggested_category is not None
                else None
            ),
            "confidence": evaluation.result.confidence,
            "model": llm_policy.model,
            "prompt_version": llm_policy.prompt_version,
            "result_schema_version": evaluation.result.schema_version,
            "input_sha256": input_sha256,
            "input_chars": len(prepared.input.canonical_json()),
            "input_token_estimate": prepared.input_token_estimate,
            "prompt_token_estimate": prepared.prompt_token_estimate,
            "max_output_tokens": prepared.max_output_tokens,
            "token_budget": llm_policy.max_tokens,
            "cost_budget_microusd": llm_policy.max_cost_microusd,
            "estimated_max_cost_microusd": prepared.estimated_max_cost_microusd,
            "provider_attempts": evaluation.attempts,
            "usage": dict(evaluation.usage),
            "input_coverage": prepared.input.coverage.model_dump(mode="json"),
        }
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "succeeded",
                "summary": "包级自动审查建议已生成",
                "coverage": coverage,
                "input_sha256": input_sha256,
                "output_sha256": evaluation.output_sha256,
                "raw_result": {
                    "normalized_result": evaluation.result.model_dump(mode="json"),
                    "provider_response": evaluation.raw_response,
                    "usage": dict(evaluation.usage),
                },
            },
        )
        return StageOutcome.completed("包级自动审查建议已生成", coverage=coverage)

    async def _record_skipped(
        self,
        context: StageContext,
        run: dict[str, Any],
    ) -> StageOutcome:
        coverage = {
            "outcome": "skipped",
            "complete": False,
            "stage_name": "llm_package",
            "reason": "llm_policy_disabled",
            "manual_review_required": True,
        }
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "cancelled",
                "summary": "包级自动审查已由固定策略关闭",
                "coverage": coverage,
                "raw_result": {"error_code": "llm_policy_disabled"},
                "error_code": "llm_policy_disabled",
            },
        )
        return StageOutcome.completed("包级自动审查已由固定策略关闭", coverage=coverage)

    async def _record_degraded(
        self,
        context: StageContext,
        run: dict[str, Any],
        error: LlmError,
        *,
        input_sha256: str = "",
        prepared: Any = None,
    ) -> StageOutcome:
        status = "timed_out" if isinstance(error, LlmProviderTimeout) else "failed"
        coverage: dict[str, Any] = {
            "outcome": "degraded",
            "complete": False,
            "stage_name": "llm_package",
            "error_code": error.code,
            "manual_review_required": True,
            "provider_attempts": error.attempts,
        }
        if input_sha256:
            coverage["input_sha256"] = input_sha256
        if prepared is not None:
            coverage.update(
                {
                    "input_token_estimate": prepared.input_token_estimate,
                    "prompt_token_estimate": prepared.prompt_token_estimate,
                    "max_output_tokens": prepared.max_output_tokens,
                    "estimated_max_cost_microusd": prepared.estimated_max_cost_microusd,
                    "input_coverage": prepared.input.coverage.model_dump(mode="json"),
                }
            )
        raw_result: dict[str, Any] = {"error_code": error.code}
        if isinstance(error, LlmOutputInvalid) and error.raw_response is not None:
            raw_result["provider_response"] = error.raw_response
        if isinstance(error, LlmBudgetExceeded):
            raw_result["budget_exceeded"] = True
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": status,
                "summary": "包级自动审查不可用，必须进入人工复核",
                "coverage": coverage,
                "raw_result": raw_result,
                "error_code": error.code,
                "input_sha256": input_sha256,
            },
        )
        return StageOutcome.degraded(
            error.code,
            "包级自动审查不可用，必须进入人工复核",
            coverage=coverage,
        )


def _recovered_outcome(run: dict[str, Any]) -> StageOutcome | None:
    status = str(run.get("status") or "")
    coverage = dict(run.get("coverage") or {})
    if status == "succeeded":
        return StageOutcome.completed(
            "包级自动审查副作用已完成",
            coverage={**coverage, "recovered": True},
        )
    if coverage.get("outcome") == "skipped":
        return StageOutcome.completed(
            "包级自动审查先前已跳过",
            coverage={**coverage, "recovered": True},
        )
    if status in {"failed", "timed_out", "cancelled"}:
        code = str(run.get("error_code") or "llm_provider_unavailable")
        return StageOutcome.degraded(
            code,
            "包级自动审查先前已降级",
            coverage={**coverage, "recovered": True},
        )
    return None


def _highest_deterministic_risk(prepared: Any) -> str:
    severities = [item.severity.value for item in prepared.input.deterministic_findings]
    return max(severities, key=risk_rank, default="none")


def _higher_risk(left: str, right: str) -> str:
    return left if risk_rank(left) >= risk_rank(right) else right
