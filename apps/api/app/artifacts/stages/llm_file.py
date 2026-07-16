from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..file_review import (
    SUMMARY_RESERVE_TOKENS,
    FileCandidate,
    FileCandidateSelector,
    FileInputBuilder,
    FileReviewResultV1,
    FileReviewService,
    artifact_llm_budget,
    latest_package_result,
    verified_file_findings,
)
from ..models import JobType, ReviewStatus, risk_rank
from ..package_review import LlmBudgetExceeded, LlmError, LlmOutputInvalid, LlmProviderTimeout
from ..policy import ReviewPolicyStage, parse_review_policy
from ..storage import ArtifactStorageError
from ..structured_llm import canonical_json, estimate_cost_microusd
from .base import StageContext, StageOutcome


class LlmFileStage:
    job_type = JobType.LLM_FILE.value

    def __init__(
        self,
        service: FileReviewService,
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
        aggregate = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "llm_file",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": self.service.provider.name,
                "tool_version": self.tool_version,
                "model": llm_policy.model,
                "prompt_version": llm_policy.prompt_version,
                "result_schema_version": "1",
                "policy_version_id": context.artifact.get("policy_version_id"),
                "input_sha256": str((context.job.get("payload") or {}).get("input_sha256") or ""),
                "idempotency_key": f"llm-file-run:{context.job['id']}:attempt-{context.attempt}",
                "coverage": {"stage_name": "llm_file"},
            }
        )
        recovered = _recovered_outcome(aggregate, "文件级自动审查")
        if recovered is not None:
            return recovered
        if not llm_policy.enabled:
            return await _record_skipped(context, aggregate, "llm_file", "llm_policy_disabled")
        if llm_policy.provider_config_ref != self.provider_config_ref:
            return await self._record_degraded(
                context,
                aggregate,
                LlmError(
                    "llm_provider_config_mismatch",
                    "LLM provider configuration does not match the fixed review policy",
                ),
            )

        try:
            plan = await FileCandidateSelector(context.repository).build(
                context.artifact,
                llm_policy,
                required_stages=policy.required_stages,
            )
            runs = await context.repository.list_review_runs(str(context.artifact["id"]))
            package_result = latest_package_result(
                runs,
                policy_version_id=str(context.artifact.get("policy_version_id") or ""),
            )
            budget = artifact_llm_budget(
                runs,
                llm_policy,
                str(context.artifact.get("policy_version_id") or ""),
            )
        except (ValidationError, ValueError) as exc:
            return await self._record_degraded(
                context,
                aggregate,
                LlmError("llm_file_selection_invalid", "File review selection is invalid"),
                private_error=exc,
            )

        reserve_tokens, reserve_cost = _summary_reserve(policy.required_stages, llm_policy)
        reviewed: list[dict[str, Any]] = []
        skipped = [{"path": item.path, "reason": item.reason} for item in plan.skipped]
        manual_from_results = False
        stage_error: LlmError | None = None
        for index, candidate in enumerate(plan.candidates):
            if len(reviewed) >= llm_policy.max_files:
                skipped.extend(
                    {"path": item.path, "reason": "file_count_budget"}
                    for item in plan.candidates[index:]
                )
                break
            available_tokens = max(0, budget.remaining_tokens - reserve_tokens)
            available_cost = max(0, budget.remaining_cost_microusd - reserve_cost)
            try:
                content = await context.storage.read_text_content(
                    candidate.content_key,
                    candidate.size_bytes + 1,
                    candidate.sha256,
                )
                prepared = FileInputBuilder().build(
                    candidate,
                    content,
                    package_result=package_result,
                    remaining_tokens=available_tokens,
                    remaining_cost_microusd=available_cost,
                    policy=llm_policy,
                )
            except LlmBudgetExceeded:
                skipped.append({"path": candidate.path, "reason": "artifact_budget_exhausted"})
                continue
            except ArtifactStorageError:
                skipped.append({"path": candidate.path, "reason": "file_content_unavailable"})
                continue
            except LlmError as exc:
                stage_error = exc
                break

            child = await _create_file_run(context, aggregate, candidate, prepared, self)
            recovered_result = _recovered_file_result(child)
            if recovered_result is not None:
                coverage = child.get("coverage") or {}
                usage = coverage.get("usage") if isinstance(coverage, Mapping) else {}
                budget.consume(usage if isinstance(usage, Mapping) else {})
                reviewed_item = _reviewed_file(
                    candidate,
                    child,
                    recovered_result,
                    manual_review_at=policy.routing.manual_review_at.value,
                )
                reviewed.append(reviewed_item)
                manual_from_results = manual_from_results or bool(
                    reviewed_item["manual_review_required"]
                )
                continue
            if str(child.get("status") or "") in {"failed", "timed_out", "cancelled"}:
                stage_error = LlmError(
                    str(child.get("error_code") or "llm_output_invalid"),
                    "File review child run previously failed",
                )
                break
            evaluation = None
            try:
                evaluation = await self.service.evaluate(
                    prepared,
                    policy=llm_policy,
                    remaining_tokens=available_tokens,
                    remaining_cost_microusd=available_cost,
                )
                reread = await context.storage.read_text_content(
                    candidate.content_key,
                    candidate.size_bytes + 1,
                    candidate.sha256,
                )
                existing = await context.repository.list_findings(str(context.artifact["id"]))
                normalized_findings = verified_file_findings(
                    evaluation,
                    prepared,
                    reread,
                    existing,
                )
                if normalized_findings:
                    await context.repository.replace_findings(
                        str(context.artifact["id"]),
                        str(child["id"]),
                        normalized_findings,
                    )
                child_coverage = {
                    "outcome": "completed",
                    "complete": True,
                    "stage_name": f"llm_file:file:{candidate.file_id}",
                    "provider_call": True,
                    "file_id": candidate.file_id,
                    "file_path": candidate.path,
                    "file_sha256": candidate.sha256,
                    "selection_reasons": [item.value for item in candidate.reasons],
                    "input_token_estimate": prepared.input_token_estimate,
                    "prompt_token_estimate": prepared.prompt_token_estimate,
                    "max_output_tokens": prepared.max_output_tokens,
                    "estimated_max_cost_microusd": prepared.estimated_max_cost_microusd,
                    "usage": dict(evaluation.usage),
                    "finding_count": len(normalized_findings),
                    "model_risk_level": evaluation.result.risk_level.value,
                    "risk_level": _effective_file_risk(candidate, evaluation.result),
                    "manual_review_required": bool(
                        evaluation.result.needs_manual_review
                        or risk_rank(_effective_file_risk(candidate, evaluation.result))
                        >= risk_rank(policy.routing.manual_review_at.value)
                    ),
                }
                child = await context.repository.complete_review_run(
                    str(child["id"]),
                    {
                        "status": "succeeded",
                        "summary": "单文件自动审查建议已生成",
                        "coverage": child_coverage,
                        "input_sha256": prepared.input_sha256,
                        "output_sha256": evaluation.output_sha256,
                        "raw_result": {
                            "normalized_result": evaluation.result.model_dump(mode="json"),
                            "provider_response": evaluation.raw_response,
                            "usage": dict(evaluation.usage),
                        },
                    },
                )
                assert child is not None
                budget.consume(evaluation.usage)
                reviewed_item = _reviewed_file(
                    candidate,
                    child,
                    evaluation.result,
                    manual_review_at=policy.routing.manual_review_at.value,
                )
                reviewed.append(reviewed_item)
                manual_from_results = manual_from_results or bool(
                    reviewed_item["manual_review_required"]
                )
            except (LlmError, ArtifactStorageError) as exc:
                error = (
                    exc
                    if isinstance(exc, LlmError)
                    else LlmError(
                        "llm_file_content_unavailable",
                        "File content became unavailable during evidence verification",
                    )
                )
                if evaluation is not None:
                    error.attempts = evaluation.attempts
                    error.usage = dict(evaluation.usage)
                await _complete_failed_file_run(context, child, candidate, prepared, error)
                if error.usage:
                    budget.consume(error.usage)
                stage_error = error
                break

        selection_sha = hashlib.sha256(
            canonical_json(
                {
                    "candidates": [
                        {"path": item.path, "sha256": item.sha256, "reasons": item.reasons}
                        for item in plan.candidates
                    ],
                    "skipped": skipped,
                }
            ).encode()
        ).hexdigest()
        complete = bool(
            stage_error is None
            and plan.complete
            and len(reviewed) == len(plan.candidates)
            and not any(item["reason"].endswith("budget") for item in skipped)
        )
        max_risk = max(
            (str(item["risk_level"]) for item in reviewed),
            key=risk_rank,
            default="none",
        )
        manual_threshold = policy.routing.manual_review_at.value
        manual_review_required = bool(
            stage_error is not None
            or not complete
            or manual_from_results
            or risk_rank(max_risk) >= risk_rank(manual_threshold)
        )
        coverage = {
            "outcome": "degraded" if stage_error else "completed",
            "complete": complete,
            "stage_name": "llm_file",
            "manual_review_required": manual_review_required,
            "candidate_file_count": len(plan.candidates),
            "reviewed_file_count": len(reviewed),
            "skipped_file_count": len(skipped),
            "reviewed_files": reviewed,
            "skipped_files": skipped,
            "graph_complete": plan.graph_complete,
            "package_input_complete": plan.package_input_complete,
            "risk_level": max_risk,
            "budget": {
                "used_tokens": budget.used_tokens,
                "remaining_tokens": budget.remaining_tokens,
                "used_cost_microusd": budget.used_cost_microusd,
                "remaining_cost_microusd": budget.remaining_cost_microusd,
                "summary_reserved_tokens": reserve_tokens,
                "summary_reserved_cost_microusd": reserve_cost,
            },
        }
        if stage_error is not None:
            coverage["error_code"] = stage_error.code
            return await self._record_degraded(
                context,
                aggregate,
                stage_error,
                coverage=coverage,
                input_sha256=selection_sha,
            )
        await context.repository.complete_review_run(
            str(aggregate["id"]),
            {
                "status": "succeeded",
                "summary": "文件级自动审查建议已生成",
                "coverage": coverage,
                "input_sha256": selection_sha,
                "output_sha256": hashlib.sha256(canonical_json(coverage).encode()).hexdigest(),
                "raw_result": {
                    "file_run_ids": [item["run_id"] for item in reviewed],
                    "reviewed_files": reviewed,
                    "skipped_files": skipped,
                },
            },
        )
        return StageOutcome.completed("文件级自动审查建议已生成", coverage=coverage)

    async def _record_degraded(
        self,
        context: StageContext,
        run: Mapping[str, Any],
        error: LlmError,
        *,
        coverage: Mapping[str, Any] | None = None,
        input_sha256: str = "",
        private_error: Exception | None = None,
    ) -> StageOutcome:
        status = "timed_out" if isinstance(error, LlmProviderTimeout) else "failed"
        result_coverage = {
            "outcome": "degraded",
            "complete": False,
            "stage_name": "llm_file",
            "manual_review_required": True,
            "error_code": error.code,
            **dict(coverage or {}),
        }
        raw_result: dict[str, Any] = {"error_code": error.code}
        if isinstance(error, LlmOutputInvalid) and error.raw_response is not None:
            raw_result["provider_response"] = error.raw_response
        if private_error is not None:
            raw_result["private_error_type"] = type(private_error).__name__
        await context.repository.complete_review_run(
            str(run["id"]),
            {
                "status": status,
                "summary": "文件级自动审查不完整，必须进入人工复核",
                "coverage": result_coverage,
                "raw_result": raw_result,
                "error_code": error.code,
                "input_sha256": input_sha256,
            },
        )
        return StageOutcome.degraded(
            error.code,
            "文件级自动审查不完整，必须进入人工复核",
            coverage=result_coverage,
        )


def _validate_context(context: StageContext) -> StageOutcome | None:
    if context.artifact.get("review_status") != ReviewStatus.SCANNING.value:
        return StageOutcome.terminal_failure(
            "artifact_not_scanning",
            "Artifact is not ready for file-level LLM review",
        )
    if context.policy is None:
        return StageOutcome.terminal_failure(
            "review_policy_unavailable",
            "Artifact file review policy is unavailable",
        )
    if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "artifact_policy_snapshot_conflict",
            "File review job does not match the artifact policy snapshot",
        )
    return None


async def _create_file_run(
    context: StageContext,
    aggregate: Mapping[str, Any],
    candidate: FileCandidate,
    prepared: Any,
    stage: LlmFileStage,
) -> dict[str, Any]:
    return await context.repository.create_review_run(
        {
            "artifact_id": context.artifact["id"],
            "type": "llm_file",
            "status": "running",
            "attempt": context.attempt,
            "tool_name": stage.service.provider.name,
            "tool_version": stage.tool_version,
            "model": str(aggregate.get("model") or ""),
            "prompt_version": str(aggregate.get("prompt_version") or ""),
            "result_schema_version": "1",
            "policy_version_id": context.artifact.get("policy_version_id"),
            "input_sha256": prepared.input_sha256,
            "idempotency_key": (
                f"llm-file-item:{context.job['id']}:attempt-{context.attempt}:"
                f"{candidate.file_id}:{candidate.sha256}:{prepared.input_sha256}"
            ),
            "coverage": {
                "stage_name": f"llm_file:file:{candidate.file_id}",
                "file_id": candidate.file_id,
                "file_path": candidate.path,
                "file_sha256": candidate.sha256,
            },
        }
    )


def _recovered_file_result(run: Mapping[str, Any]) -> FileReviewResultV1 | None:
    if run.get("status") != "succeeded":
        return None
    raw = run.get("raw_result") if isinstance(run.get("raw_result"), Mapping) else {}
    try:
        return FileReviewResultV1.model_validate(raw.get("normalized_result"))
    except ValidationError as exc:
        raise LlmOutputInvalid("Recovered file review result is invalid") from exc


def _reviewed_file(
    candidate: FileCandidate,
    run: Mapping[str, Any],
    result: FileReviewResultV1,
    *,
    manual_review_at: str,
) -> dict[str, Any]:
    effective_risk = _effective_file_risk(candidate, result)
    return {
        "file_id": candidate.file_id,
        "path": candidate.path,
        "sha256": candidate.sha256,
        "run_id": str(run["id"]),
        "model_risk_level": result.risk_level.value,
        "risk_level": effective_risk,
        "finding_count": len(result.findings),
        "manual_review_required": bool(
            result.needs_manual_review or risk_rank(effective_risk) >= risk_rank(manual_review_at)
        ),
    }


def _effective_file_risk(candidate: FileCandidate, result: FileReviewResultV1) -> str:
    deterministic = max(
        (str(item.get("severity") or "info") for item in candidate.deterministic_findings),
        key=risk_rank,
        default="none",
    )
    return (
        result.risk_level.value
        if risk_rank(result.risk_level) >= risk_rank(deterministic)
        else deterministic
    )


async def _complete_failed_file_run(
    context: StageContext,
    run: Mapping[str, Any],
    candidate: FileCandidate,
    prepared: Any,
    error: LlmError,
) -> None:
    coverage = {
        "outcome": "degraded",
        "complete": False,
        "stage_name": f"llm_file:file:{candidate.file_id}",
        "provider_call": error.attempts > 0,
        "file_id": candidate.file_id,
        "file_path": candidate.path,
        "file_sha256": candidate.sha256,
        "manual_review_required": True,
        "error_code": error.code,
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
            "summary": "单文件自动审查输出无法验证",
            "coverage": coverage,
            "raw_result": raw,
            "error_code": error.code,
            "input_sha256": prepared.input_sha256,
        },
    )


def _summary_reserve(
    required_stages: tuple[ReviewPolicyStage, ...],
    policy: Any,
) -> tuple[int, int]:
    if ReviewPolicyStage.LLM_SUMMARY not in required_stages:
        return 0, 0
    tokens = min(SUMMARY_RESERVE_TOKENS, max(256, policy.max_tokens // 10))
    cost = estimate_cost_microusd(
        prompt_tokens=tokens // 2,
        completion_tokens=tokens - tokens // 2,
        input_rate=policy.input_cost_microusd_per_million_tokens,
        output_rate=policy.output_cost_microusd_per_million_tokens,
    )
    return tokens, cost


async def _record_skipped(
    context: StageContext,
    run: Mapping[str, Any],
    stage_name: str,
    reason: str,
) -> StageOutcome:
    coverage = {
        "outcome": "skipped",
        "complete": False,
        "stage_name": stage_name,
        "manual_review_required": True,
        "reason": reason,
    }
    await context.repository.complete_review_run(
        str(run["id"]),
        {
            "status": "cancelled",
            "summary": "文件级自动审查已由固定策略关闭",
            "coverage": coverage,
            "raw_result": {"error_code": reason},
            "error_code": reason,
        },
    )
    return StageOutcome.completed("文件级自动审查已跳过", coverage=coverage)


def _recovered_outcome(run: Mapping[str, Any], label: str) -> StageOutcome | None:
    status = str(run.get("status") or "")
    coverage = dict(run.get("coverage") or {})
    if status == "succeeded":
        return StageOutcome.completed(
            f"{label}副作用已完成",
            coverage={**coverage, "recovered": True},
        )
    if coverage.get("outcome") == "skipped":
        return StageOutcome.completed(
            f"{label}先前已跳过",
            coverage={**coverage, "recovered": True},
        )
    if status in {"failed", "timed_out", "cancelled"}:
        return StageOutcome.degraded(
            str(run.get("error_code") or "llm_provider_unavailable"),
            f"{label}先前已降级",
            coverage={**coverage, "recovered": True},
        )
    return None
