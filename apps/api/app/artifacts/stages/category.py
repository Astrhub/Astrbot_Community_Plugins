from __future__ import annotations

import hashlib
from typing import Any

from ..category import (
    CategoryError,
    CategoryInputBuilder,
    CategoryProviderTimeout,
    CategoryResultInvalid,
    CategorySuggestionService,
)
from ..models import JobType, ReviewStatus
from ..policy import parse_review_policy
from .base import StageContext, StageOutcome


class CategoryStage:
    job_type = JobType.CATEGORY.value

    def __init__(
        self,
        service: CategorySuggestionService,
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
                "Artifact is not ready for category suggestion",
            )
        if context.policy is None:
            return StageOutcome.terminal_failure(
                "review_policy_unavailable",
                "Artifact category policy is unavailable",
            )
        if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
            return StageOutcome.terminal_failure(
                "artifact_policy_snapshot_conflict",
                "Category job policy does not match the artifact snapshot",
            )
        policy = parse_review_policy(context.policy.get("policy") or {})
        category_policy = policy.category
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "category",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": self.service.provider.name,
                "tool_version": self.tool_version,
                "model": category_policy.model,
                "prompt_version": category_policy.prompt_version,
                "policy_version_id": context.artifact.get("policy_version_id"),
                "input_sha256": str((context.job.get("payload") or {}).get("input_sha256") or ""),
                "idempotency_key": (f"category-run:{context.job['id']}:attempt-{context.attempt}"),
                "coverage": {"stage_name": "category"},
            }
        )
        recovered = _recovered_outcome(run)
        if recovered is not None:
            return recovered
        if not category_policy.enabled:
            return await self._record_skipped(context, run)
        if category_policy.provider_config_ref != self.provider_config_ref:
            return await self._record_degraded(
                context,
                run,
                CategoryError(
                    "category_provider_config_mismatch",
                    "Category provider configuration does not match the fixed policy",
                ),
            )

        input_sha256 = ""
        try:
            input_data = await CategoryInputBuilder(
                context.repository,
                context.storage,
            ).build(context.artifact, category_policy)
            input_sha256 = hashlib.sha256(input_data.canonical_json().encode()).hexdigest()
            evaluation = await self.service.evaluate(
                input_data,
                model=category_policy.model,
                prompt_version=category_policy.prompt_version,
                max_output_tokens=category_policy.max_output_tokens,
            )
            suggestion = evaluation.suggestion
            applied = await context.repository.apply_category_suggestion(
                str(context.artifact["id"]),
                suggested_category=suggestion.suggested_category.value,
                confidence=suggestion.confidence,
                reason=suggestion.reason,
                minimum_confidence=category_policy.minimum_confidence,
            )
            if applied is None:
                return StageOutcome.terminal_failure(
                    "artifact_state_changed",
                    "Artifact left scanning state during category suggestion",
                )
        except CategoryError as exc:
            return await self._record_degraded(
                context,
                run,
                exc,
                input_sha256=input_sha256,
            )

        coverage = {
            "outcome": "completed",
            "stage_name": "category",
            "suggested_category": suggestion.suggested_category.value,
            "confidence": suggestion.confidence,
            "category_applied": bool(applied.get("category_applied")),
            "model": suggestion.model,
            "prompt_version": suggestion.prompt_version,
            "input_chars": len(input_data.canonical_json()),
            "input_sha256": input_sha256,
            "file_count": len(input_data.file_tree),
        }
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "succeeded",
                "summary": "AI 分类建议已生成",
                "coverage": coverage,
                "input_sha256": input_sha256,
                "raw_result": {
                    "normalized_result": suggestion.model_dump(mode="json"),
                    "provider_response": evaluation.raw_response,
                },
            },
        )
        return StageOutcome.completed("AI 分类建议已生成", coverage=coverage)

    async def _record_skipped(
        self,
        context: StageContext,
        run: dict[str, Any],
    ) -> StageOutcome:
        coverage = {
            "outcome": "skipped",
            "stage_name": "category",
            "reason": "category_policy_disabled",
        }
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": "cancelled",
                "summary": "AI 分类建议已由固定策略关闭",
                "coverage": coverage,
                "raw_result": {"error_code": "category_policy_disabled"},
                "error_code": "category_policy_disabled",
            },
        )
        return StageOutcome.completed("AI 分类建议已由固定策略关闭", coverage=coverage)

    async def _record_degraded(
        self,
        context: StageContext,
        run: dict[str, Any],
        error: CategoryError,
        *,
        input_sha256: str = "",
    ) -> StageOutcome:
        status = "timed_out" if isinstance(error, CategoryProviderTimeout) else "failed"
        coverage = {
            "outcome": "degraded",
            "stage_name": "category",
            "error_code": error.code,
        }
        if input_sha256:
            coverage["input_sha256"] = input_sha256
        raw_result: dict[str, Any] = {"error_code": error.code}
        if isinstance(error, CategoryResultInvalid) and error.raw_response is not None:
            raw_result["provider_response"] = error.raw_response
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": status,
                "summary": "AI 分类建议不可用，保留人工分类流程",
                "coverage": coverage,
                "raw_result": raw_result,
                "error_code": error.code,
                "input_sha256": input_sha256,
            },
        )
        return StageOutcome.degraded(
            error.code,
            "AI 分类建议不可用，保留人工分类流程",
            coverage=coverage,
        )


def _recovered_outcome(run: dict[str, Any]) -> StageOutcome | None:
    status = str(run.get("status") or "")
    coverage = dict(run.get("coverage") or {})
    if status == "succeeded":
        return StageOutcome.completed(
            "AI 分类建议副作用已完成",
            coverage={**coverage, "recovered": True},
        )
    if coverage.get("outcome") == "skipped":
        return StageOutcome.completed(
            "AI 分类建议先前已跳过",
            coverage={**coverage, "recovered": True},
        )
    if status in {"failed", "timed_out", "cancelled"}:
        code = str(run.get("error_code") or "category_provider_unavailable")
        return StageOutcome.degraded(
            code,
            "AI 分类建议先前已降级",
            coverage={**coverage, "recovered": True},
        )
    return None
