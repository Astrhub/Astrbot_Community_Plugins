from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from ..models import JobType, ReviewStatus
from ..policy import parse_review_policy
from ..runner_contract import (
    MAX_RUNTIME_RESULT_BYTES,
    RUNTIME_CONTRACT_SCHEMA_VERSION,
    RuntimeDispatchRequest,
    RuntimeLimits,
    runtime_dispatch_id,
)
from ..runtime_dispatch import (
    CollectionState,
    RuntimeDispatchController,
    RuntimeDispatchServiceError,
)
from ..runtime_targets import RuntimeImage, RuntimeTargetResolver
from .base import StageContext, StageOutcome

RUNTIME_STAGE_TOOL_VERSION = f"runtime-contract-v{RUNTIME_CONTRACT_SCHEMA_VERSION}"
DEFAULT_RUNTIME_COLLECT_POLLS = 720
DEFAULT_RUNTIME_COLLECT_DELAY_SECONDS = 5

_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "timed_out", "cancelled"}


class RuntimeDispatchStage:
    job_type = JobType.RUNTIME_DISPATCH.value

    def __init__(
        self,
        controller: RuntimeDispatchController | None,
        *,
        image_digest: str,
        platform: str = "linux/amd64",
        astrbot_commits: Mapping[str, str] | None = None,
    ) -> None:
        self.controller = controller
        self.image_digest = image_digest
        self.platform = platform
        self.astrbot_commits = dict(astrbot_commits or {})
        self.version = (
            f"{RUNTIME_STAGE_TOOL_VERSION}:{image_digest[7:23]}"
            if image_digest.startswith("sha256:")
            else RUNTIME_STAGE_TOOL_VERSION
        )

    async def execute(self, context: StageContext) -> StageOutcome:
        invalid = _validate_context(context)
        if invalid is not None:
            return invalid
        assert context.policy is not None
        policy = parse_review_policy(context.policy.get("policy") or {})
        target_data = _target_payload(context.job)
        target_policy = next(
            (
                target
                for target in policy.runtime_targets
                if target.astrbot == target_data["astrbot"]
                and target.python == target_data["python"]
            ),
            None,
        )
        if target_policy is None:
            return StageOutcome.terminal_failure(
                "runtime_target_snapshot_invalid",
                "Runtime job target is not present in the fixed policy snapshot",
            )

        stage_name = str((context.job.get("payload") or {}).get("stage_name") or "runtime")
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "runtime",
                "status": "running",
                "attempt": 1,
                "tool_name": "runtime-runner",
                "tool_version": self.version,
                "policy_version_id": context.artifact["policy_version_id"],
                "input_sha256": str((context.job.get("payload") or {}).get("input_sha256") or ""),
                "coverage": {"outcome": "running", "stage_name": stage_name},
                "container_image_digest": self.image_digest,
                "astrbot_version": target_policy.astrbot,
                "python_version": target_policy.python,
                "platform": self.platform,
                "idempotency_key": _runtime_run_key(
                    str(context.artifact["id"]),
                    str(context.artifact["policy_version_id"]),
                    target_policy.astrbot,
                    target_policy.python,
                    self.image_digest,
                ),
            }
        )
        if str(run.get("status") or "") in _TERMINAL_RUN_STATUSES:
            return _terminal_run_outcome(run)
        if self.controller is None:
            return await _fail_run(
                context,
                run,
                "runtime_runner_unavailable",
                "Runtime dispatch is unavailable in this worker",
                stage_name=stage_name,
                degraded=True,
            )

        try:
            metadata = await _precheck_metadata(context)
        except ValueError as exc:
            return await _fail_run(
                context,
                run,
                "runtime_precheck_metadata_missing",
                "Runtime validation requires the fixed precheck metadata snapshot",
                stage_name=stage_name,
                private_error=type(exc).__name__,
            )
        try:
            resolution = RuntimeTargetResolver(
                (
                    RuntimeImage(
                        astrbot_version=target_policy.astrbot,
                        python_version=target_policy.python,
                        image_digest=self.image_digest,
                        platform=self.platform,
                        astrbot_commit=self.astrbot_commits.get(target_policy.astrbot, ""),
                    ),
                )
            ).resolve(
                policy.model_copy(update={"runtime_targets": (target_policy,)}),
                metadata,
                plugin_version=str(context.artifact.get("version") or ""),
                plugin_normalized_version=str(context.artifact.get("normalized_version") or ""),
            )
        except (ValidationError, ValueError) as exc:
            return await _fail_run(
                context,
                run,
                "runtime_target_snapshot_invalid",
                "Runtime target snapshot could not be resolved",
                stage_name=stage_name,
                private_error=type(exc).__name__,
            )
        if resolution.blocked or len(resolution.targets) != 1:
            finding = resolution.finding
            if finding is not None:
                payload = finding.as_repository_payload()
                payload.update(
                    {
                        "fingerprint": _fingerprint(
                            str(context.artifact["id"]),
                            finding.rule_id,
                            target_policy.astrbot,
                            target_policy.python,
                        ),
                        "correlation": {
                            "runtime": {
                                "astrbot_version": target_policy.astrbot,
                                "python_version": target_policy.python,
                            }
                        },
                    }
                )
                await context.repository.replace_findings(
                    str(context.artifact["id"]),
                    str(run["id"]),
                    (payload,),
                )
            return await _fail_run(
                context,
                run,
                resolution.error_code or "runtime_target_unavailable",
                "Plugin metadata is incompatible with the fixed runtime target",
                stage_name=stage_name,
            )

        request = _dispatch_request(
            context,
            run_id=str(run["id"]),
            target=resolution.targets[0].model_dump(mode="json"),
            metadata=metadata,
            policy=policy,
        )
        try:
            dispatch = await self.controller.create(
                request,
                run_id=str(run["id"]),
                max_attempts=3,
            )
        except (RuntimeDispatchServiceError, ValidationError, ValueError) as exc:
            code = getattr(exc, "code", "runtime_dispatch_invalid")
            return await _fail_run(
                context,
                run,
                str(code),
                "Runtime dispatch could not be created",
                stage_name=stage_name,
                private_error=type(exc).__name__,
            )

        await _enqueue_collect(
            context,
            dispatch_id=str(dispatch["id"]),
            run_id=str(run["id"]),
            stage_name=stage_name,
            tool_version=self.version,
            poll=0,
            delay_seconds=0,
        )
        return StageOutcome.completed(
            "Runtime dispatch was queued for the isolated runner",
            coverage={
                "outcome": "running",
                "stage_name": stage_name,
                "dispatch_id": dispatch["id"],
                "target": target_data,
            },
        )


class RuntimeCollectStage:
    job_type = JobType.RUNTIME_COLLECT.value

    def __init__(
        self,
        controller: RuntimeDispatchController | None,
        *,
        max_polls: int = DEFAULT_RUNTIME_COLLECT_POLLS,
        poll_delay_seconds: int = DEFAULT_RUNTIME_COLLECT_DELAY_SECONDS,
    ) -> None:
        if max_polls < 1 or max_polls > 10_000:
            raise ValueError("runtime_collect_poll_limit_invalid")
        if poll_delay_seconds < 0 or poll_delay_seconds > 300:
            raise ValueError("runtime_collect_poll_delay_invalid")
        self.controller = controller
        self.max_polls = max_polls
        self.poll_delay_seconds = poll_delay_seconds

    async def execute(self, context: StageContext) -> StageOutcome:
        invalid = _validate_context(context)
        if invalid is not None:
            return invalid
        payload = (
            context.job.get("payload") if isinstance(context.job.get("payload"), Mapping) else {}
        )
        dispatch_id = str(payload.get("dispatch_id") or "")
        run_id = str(payload.get("run_id") or context.job.get("run_id") or "")
        stage_name = str(payload.get("stage_name") or "runtime")
        tool_version = str(payload.get("tool_version") or RUNTIME_STAGE_TOOL_VERSION)
        try:
            poll = int(payload.get("poll") or 0)
        except (TypeError, ValueError):
            poll = -1
        if not dispatch_id or not run_id or poll < 0 or poll > self.max_polls:
            return StageOutcome.terminal_failure(
                "runtime_collect_snapshot_invalid",
                "Runtime collect job has an invalid server snapshot",
            )
        run = await _review_run(context, run_id)
        if run is None:
            return StageOutcome.terminal_failure(
                "runtime_collect_run_missing",
                "Runtime collect job cannot find its review run",
            )
        if str(run.get("status") or "") in _TERMINAL_RUN_STATUSES:
            return _terminal_run_outcome(run)
        if self.controller is None:
            return await _fail_run(
                context,
                run,
                "runtime_runner_unavailable",
                "Runtime collection is unavailable in this worker",
                stage_name=stage_name,
                degraded=True,
            )

        dispatch = await context.repository.get_runtime_dispatch(dispatch_id)
        if dispatch is None:
            return await _fail_run(
                context,
                run,
                "runtime_dispatch_not_found",
                "Runtime dispatch disappeared before collection",
                stage_name=stage_name,
            )
        try:
            request = RuntimeDispatchRequest.model_validate(dispatch.get("request") or {})
        except ValidationError:
            request = None
        if request is None or not _collect_binding_matches(
            context,
            run,
            dispatch,
            request,
            dispatch_id=dispatch_id,
            run_id=run_id,
            stage_name=stage_name,
        ):
            return await _fail_run(
                context,
                run,
                "runtime_collect_snapshot_invalid",
                "Runtime collect job does not match its dispatch and review run",
                stage_name=stage_name,
            )

        if poll >= self.max_polls:
            await context.repository.cancel_runtime_dispatch(
                dispatch_id,
                error_code="runtime_collect_timeout",
                error_message="Runtime result did not become terminal within the collect window",
            )
        try:
            collected = await self.controller.collect(dispatch_id)
        except RuntimeDispatchServiceError as exc:
            if exc.retryable and poll < self.max_polls:
                return await self._wait(
                    context,
                    dispatch_id,
                    run_id,
                    stage_name,
                    tool_version,
                    poll,
                )
            return await _fail_run(
                context,
                run,
                exc.code,
                "Runtime result collection failed",
                stage_name=stage_name,
            )

        if collected.state is CollectionState.WAITING:
            return await self._wait(
                context,
                dispatch_id,
                run_id,
                stage_name,
                tool_version,
                poll,
            )
        if collected.state is CollectionState.NOT_FOUND:
            return await _fail_run(
                context,
                run,
                "runtime_dispatch_not_found",
                "Runtime dispatch disappeared before collection",
                stage_name=stage_name,
            )
        refreshed = await _review_run(context, run_id)
        if refreshed is None:
            return StageOutcome.terminal_failure(
                "runtime_collect_run_missing",
                "Runtime run disappeared after collection",
            )
        return _terminal_run_outcome(refreshed)

    async def _wait(
        self,
        context: StageContext,
        dispatch_id: str,
        run_id: str,
        stage_name: str,
        tool_version: str,
        poll: int,
    ) -> StageOutcome:
        if poll >= self.max_polls:
            return StageOutcome.terminal_failure(
                "runtime_collect_timeout",
                "Runtime collection exhausted its bounded poll window",
            )
        await _enqueue_collect(
            context,
            dispatch_id=dispatch_id,
            run_id=run_id,
            stage_name=stage_name,
            tool_version=tool_version,
            poll=poll + 1,
            delay_seconds=self.poll_delay_seconds,
        )
        return StageOutcome.completed(
            "Runtime result is still pending and another bounded poll was queued",
            coverage={
                "outcome": "running",
                "stage_name": stage_name,
                "dispatch_id": dispatch_id,
                "poll": poll,
                "waiting": True,
            },
        )


def _validate_context(context: StageContext) -> StageOutcome | None:
    if context.policy is None or not context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "review_policy_unavailable",
            "Runtime stage has no fixed review policy snapshot",
        )
    if context.job.get("policy_version_id") != context.artifact.get("policy_version_id"):
        return StageOutcome.terminal_failure(
            "artifact_policy_snapshot_conflict",
            "Runtime job policy does not match the artifact snapshot",
        )
    if context.artifact.get("review_status") != ReviewStatus.SCANNING.value:
        return StageOutcome.terminal_failure(
            "artifact_not_scanning",
            "Artifact is not available for runtime validation",
        )
    return None


def _target_payload(job: Mapping[str, Any]) -> dict[str, str]:
    payload = job.get("payload") if isinstance(job.get("payload"), Mapping) else {}
    target = payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
    return {
        "astrbot": str(target.get("astrbot") or ""),
        "python": str(target.get("python") or ""),
    }


async def _precheck_metadata(context: StageContext) -> Mapping[str, Any]:
    runs = await context.repository.list_review_runs(str(context.artifact["id"]))
    for run in reversed(runs):
        raw_result = run.get("raw_result") if isinstance(run.get("raw_result"), Mapping) else {}
        metadata = (
            raw_result.get("metadata") if isinstance(raw_result.get("metadata"), Mapping) else None
        )
        if (
            run.get("type") == "precheck"
            and run.get("status") == "succeeded"
            and run.get("policy_version_id") == context.artifact.get("policy_version_id")
            and metadata is not None
        ):
            return metadata
    raise ValueError("runtime_precheck_metadata_missing")


def _dispatch_request(
    context: StageContext,
    *,
    run_id: str,
    target: Mapping[str, Any],
    metadata: Mapping[str, Any],
    policy: Any,
) -> RuntimeDispatchRequest:
    artifact_id = str(context.artifact["id"])
    dispatch_id = runtime_dispatch_id(
        run_id,
        artifact_id,
        str(target.get("astrbot_version") or ""),
        str(target.get("python_version") or ""),
        str(target.get("image_digest") or ""),
    )
    limits = RuntimeLimits(
        **policy.limits.model_dump(mode="python"),
        max_result_bytes=MAX_RUNTIME_RESULT_BYTES,
    )
    return RuntimeDispatchRequest.model_validate(
        {
            "schema_version": RUNTIME_CONTRACT_SCHEMA_VERSION,
            "dispatch_id": dispatch_id,
            "artifact_id": artifact_id,
            "artifact_sha256": context.artifact["archive_sha256"],
            "artifact_size_bytes": context.artifact["size_bytes"],
            "quarantine_key": context.artifact["quarantine_key"],
            "policy_version_id": context.artifact["policy_version_id"],
            "expected_plugin": {
                "name": str(
                    context.artifact.get("plugin_name")
                    or context.artifact.get("plugin_id")
                    or metadata.get("name")
                    or ""
                ),
                "version": context.artifact["version"],
                "source_repo": context.artifact["source_repo"],
                "source_commit_sha": context.artifact.get("source_commit_sha") or "",
            },
            "target": dict(target),
            "limits": limits.model_dump(mode="json"),
            "install_network_profile": policy.network_profiles.install,
            "smoke_network_profile": policy.network_profiles.smoke,
            "result_key": f"runtime/results/{hashlib.sha256(artifact_id.encode()).hexdigest()[:32]}/{dispatch_id}",
        }
    )


async def _enqueue_collect(
    context: StageContext,
    *,
    dispatch_id: str,
    run_id: str,
    stage_name: str,
    tool_version: str,
    poll: int,
    delay_seconds: int,
) -> None:
    available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    await context.repository.enqueue_job(
        {
            "artifact_id": context.artifact["id"],
            "type": JobType.RUNTIME_COLLECT.value,
            "payload": {
                "stage": "runtime",
                "stage_name": stage_name,
                "tool_version": tool_version,
                "dispatch_id": dispatch_id,
                "run_id": run_id,
                "poll": poll,
            },
            "max_attempts": 3,
            "available_at": available_at.isoformat(),
            "idempotency_key": f"runtime-collect:{dispatch_id}:poll-{poll}",
            "policy_version_id": context.artifact["policy_version_id"],
            "run_id": run_id,
            "stage_name": stage_name,
        }
    )


async def _review_run(context: StageContext, run_id: str) -> Mapping[str, Any] | None:
    runs = await context.repository.list_review_runs(str(context.artifact["id"]))
    return next((run for run in runs if str(run.get("id") or "") == run_id), None)


def _collect_binding_matches(
    context: StageContext,
    run: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    request: RuntimeDispatchRequest,
    *,
    dispatch_id: str,
    run_id: str,
    stage_name: str,
) -> bool:
    coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
    return bool(
        str(dispatch.get("id") or "") == dispatch_id
        and str(dispatch.get("artifact_id") or "") == str(context.artifact["id"])
        and str(dispatch.get("run_id") or "") == run_id
        and request.dispatch_id == dispatch_id
        and request.artifact_id == str(context.artifact["id"])
        and request.policy_version_id == str(context.artifact["policy_version_id"])
        and request.canonical_sha256() == str(dispatch.get("request_sha256") or "")
        and str(run.get("policy_version_id") or "") == request.policy_version_id
        and str(run.get("astrbot_version") or "") == request.target.astrbot_version
        and str(run.get("python_version") or "") == request.target.python_version
        and str(run.get("container_image_digest") or "") == request.target.image_digest
        and str(coverage.get("stage_name") or "") == stage_name
    )


async def _fail_run(
    context: StageContext,
    run: Mapping[str, Any],
    error_code: str,
    summary: str,
    *,
    stage_name: str,
    degraded: bool = False,
    private_error: str = "",
) -> StageOutcome:
    outcome = "degraded" if degraded else "blocked"
    coverage = {
        "outcome": outcome,
        "complete": False,
        "stage_name": stage_name,
        "error_code": error_code,
    }
    await context.repository.complete_review_run(
        str(run["id"]),
        {
            "status": "failed",
            "summary": summary,
            "coverage": coverage,
            "error_code": error_code,
            "raw_result": ({"private_error_type": private_error} if private_error else {}),
        },
    )
    if degraded:
        return StageOutcome.degraded(error_code, summary, coverage=coverage)
    return StageOutcome.blocked(error_code, summary, coverage=coverage)


def _terminal_run_outcome(run: Mapping[str, Any]) -> StageOutcome:
    coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
    if run.get("status") == "succeeded":
        return StageOutcome.completed(
            str(run.get("summary") or "Runtime validation passed"),
            coverage=coverage,
        )
    error_code = str(run.get("error_code") or "runtime_validation_failed")
    return StageOutcome.blocked(
        error_code,
        str(run.get("summary") or "Runtime validation did not pass"),
        coverage=coverage,
    )


def _runtime_run_key(
    artifact_id: str,
    policy_id: str,
    astrbot_version: str,
    python_version: str,
    image_digest: str,
) -> str:
    digest = hashlib.sha256(
        "\x00".join(
            (artifact_id, policy_id, astrbot_version, python_version, image_digest)
        ).encode()
    ).hexdigest()
    return f"runtime-run:{digest}"


def _fingerprint(
    artifact_id: str,
    rule_id: str,
    astrbot_version: str,
    python_version: str,
) -> str:
    return hashlib.sha256(
        "\x00".join((artifact_id, rule_id, astrbot_version, python_version)).encode()
    ).hexdigest()


__all__ = [
    "DEFAULT_RUNTIME_COLLECT_DELAY_SECONDS",
    "DEFAULT_RUNTIME_COLLECT_POLLS",
    "RUNTIME_STAGE_TOOL_VERSION",
    "RuntimeCollectStage",
    "RuntimeDispatchStage",
]
