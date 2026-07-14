from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .models import JobStatus, JobType, ReviewRunStatus, ReviewStatus
from .policy import ReviewPolicyStage, ReviewPolicyV1, parse_review_policy
from .repository import ArtifactRepository

ROUTING_STAGE_NAME = "routing"
ROUTING_TOOL_VERSION = "routing-v1"


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_STAGE_STATES = frozenset(
    {
        StageState.COMPLETED,
        StageState.BLOCKED,
        StageState.DEGRADED,
        StageState.FAILED,
        StageState.SKIPPED,
    }
)


@dataclass(frozen=True, slots=True)
class StageToolSnapshot:
    version: str
    ready: bool = True
    reason: str = ""
    max_attempts: int = 3

    def __post_init__(self) -> None:
        version = " ".join(str(self.version or "").split())
        reason = " ".join(str(self.reason or "").split())[:240]
        if not version or len(version) > 160:
            raise ValueError("Stage tool version must contain 1 to 160 characters")
        if self.ready and reason:
            raise ValueError("Ready stage tools cannot have an unavailable reason")
        if not self.ready and not reason:
            raise ValueError("Unavailable stage tools require a reason")
        if self.max_attempts < 1 or self.max_attempts > 20:
            raise ValueError("Stage max_attempts must be between 1 and 20")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    artifact_id: str
    policy_version_id: str
    stage_states: Mapping[str, StageState]
    enqueued_job_ids: tuple[str, ...]
    recorded_run_ids: tuple[str, ...]
    route_job_id: str | None
    waiting_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StageUnit:
    stage: ReviewPolicyStage
    name: str
    job_type: JobType
    run_type: str
    target: Mapping[str, str]


_STAGE_PHASES: tuple[tuple[ReviewPolicyStage, ...], ...] = (
    (ReviewPolicyStage.STATIC,),
    (ReviewPolicyStage.DIFF,),
    (ReviewPolicyStage.IMPORT_GRAPH,),
    (ReviewPolicyStage.CLAMAV, ReviewPolicyStage.YARA),
    (ReviewPolicyStage.RUNTIME,),
    (ReviewPolicyStage.DEPENDENCY,),
    (ReviewPolicyStage.CATEGORY,),
    (ReviewPolicyStage.LLM_PACKAGE,),
    (ReviewPolicyStage.LLM_FILE,),
    (ReviewPolicyStage.LLM_SUMMARY,),
)
_STAGE_PHASE = {
    stage: phase_index for phase_index, stages in enumerate(_STAGE_PHASES) for stage in stages
}
_STAGE_JOB_TYPES = {
    ReviewPolicyStage.STATIC: JobType.STATIC_SCAN,
    ReviewPolicyStage.DIFF: JobType.DIFF_GRAPH,
    ReviewPolicyStage.IMPORT_GRAPH: JobType.DIFF_GRAPH,
    ReviewPolicyStage.CLAMAV: JobType.CLAMAV_SCAN,
    ReviewPolicyStage.YARA: JobType.YARA_SCAN,
    ReviewPolicyStage.RUNTIME: JobType.RUNTIME_DISPATCH,
    ReviewPolicyStage.DEPENDENCY: JobType.DEPENDENCY_SCAN,
    ReviewPolicyStage.CATEGORY: JobType.CATEGORY,
    ReviewPolicyStage.LLM_PACKAGE: JobType.LLM_PACKAGE,
    ReviewPolicyStage.LLM_FILE: JobType.LLM_FILE,
    ReviewPolicyStage.LLM_SUMMARY: JobType.LLM_SUMMARY,
}
_ARTIFACT_TERMINAL_STATUSES = {
    ReviewStatus.APPROVED.value,
    ReviewStatus.REJECTED.value,
    ReviewStatus.WITHDRAWN.value,
    ReviewStatus.CHANGES_REQUESTED.value,
    ReviewStatus.PROCESSING_FAILED.value,
}


class ReviewOrchestrator:
    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        tool_snapshots: Mapping[ReviewPolicyStage | str, StageToolSnapshot],
    ) -> None:
        self.repository = repository
        self.tool_snapshots = {
            ReviewPolicyStage(str(stage)): snapshot for stage, snapshot in tool_snapshots.items()
        }

    async def reconcile(self, artifact_id: str) -> OrchestrationResult:
        artifact = await self.repository.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError("artifact_not_found")
        policy_version_id = str(artifact.get("policy_version_id") or "")
        if not policy_version_id:
            raise ValueError("review_policy_unavailable")
        policy_record = await self.repository.get_review_policy(policy_version_id)
        if policy_record is None:
            raise ValueError("review_policy_unavailable")
        policy = parse_review_policy(policy_record.get("policy") or {})
        runs = await self.repository.list_review_runs(artifact_id)
        jobs = await self.repository.list_artifact_jobs(artifact_id)
        units = _stage_units(policy)
        unit_states = {
            unit.name: self._unit_state(
                artifact_id,
                unit,
                policy_version_id,
                runs,
                jobs,
            )
            for unit in units
        }
        enqueued_job_ids: list[str] = []
        recorded_run_ids: list[str] = []
        waiting_on: set[str] = set()

        if str(artifact.get("review_status") or "") not in _ARTIFACT_TERMINAL_STATUSES:
            required = set(policy.required_stages)
            for phase_index, phase in enumerate(_STAGE_PHASES):
                phase_stages = [stage for stage in phase if stage in required]
                for stage in phase_stages:
                    dependency_states = {
                        dependency.value: _aggregate_stage_state(
                            dependency,
                            units,
                            unit_states,
                        )
                        for dependency in required
                        if _STAGE_PHASE[dependency] < phase_index
                    }
                    incomplete_dependencies = {
                        name: state
                        for name, state in dependency_states.items()
                        if state not in TERMINAL_STAGE_STATES
                    }
                    if incomplete_dependencies:
                        waiting_on.update(incomplete_dependencies)
                        continue
                    blocking_dependencies = {
                        name: state
                        for name, state in dependency_states.items()
                        if state != StageState.COMPLETED
                    }
                    for unit in [item for item in units if item.stage == stage]:
                        if unit_states[unit.name] != StageState.PENDING:
                            continue
                        tool = self._tool_snapshot(stage)
                        if blocking_dependencies:
                            reason = _upstream_reason(blocking_dependencies)
                            run = await self._record_synthetic_outcome(
                                artifact,
                                policy_version_id,
                                unit,
                                tool,
                                state=StageState.SKIPPED,
                                reason=reason,
                                upstream=blocking_dependencies,
                            )
                            unit_states[unit.name] = StageState.SKIPPED
                            recorded_run_ids.append(str(run["id"]))
                            runs.append(run)
                            continue
                        if not tool.ready:
                            run = await self._record_synthetic_outcome(
                                artifact,
                                policy_version_id,
                                unit,
                                tool,
                                state=StageState.DEGRADED,
                                reason="stage_tool_unavailable",
                                upstream={},
                            )
                            unit_states[unit.name] = StageState.DEGRADED
                            recorded_run_ids.append(str(run["id"]))
                            runs.append(run)
                            continue
                        job = await self._enqueue_stage_job(
                            artifact,
                            policy_version_id,
                            unit,
                            tool,
                        )
                        if str(job["id"]) not in {str(item["id"]) for item in jobs}:
                            enqueued_job_ids.append(str(job["id"]))
                            jobs.append(job)
                        unit_states[unit.name] = StageState.RUNNING

        stage_states = {
            stage.value: _aggregate_stage_state(stage, units, unit_states)
            for stage in policy.required_stages
        }
        route_job = _find_route_job(jobs, artifact_id, policy_version_id)
        all_terminal = all(state in TERMINAL_STAGE_STATES for state in unit_states.values())
        if (
            str(artifact.get("review_status") or "") not in _ARTIFACT_TERMINAL_STATUSES
            and all_terminal
        ):
            route_job = await self._enqueue_route_job(artifact, policy_version_id, unit_states)
            if str(route_job["id"]) not in {str(item["id"]) for item in jobs}:
                enqueued_job_ids.append(str(route_job["id"]))
                jobs.append(route_job)
        elif not all_terminal:
            waiting_on.update(
                name for name, state in unit_states.items() if state not in TERMINAL_STAGE_STATES
            )

        return OrchestrationResult(
            artifact_id=artifact_id,
            policy_version_id=policy_version_id,
            stage_states=MappingProxyType(stage_states),
            enqueued_job_ids=tuple(enqueued_job_ids),
            recorded_run_ids=tuple(recorded_run_ids),
            route_job_id=str(route_job["id"]) if route_job else None,
            waiting_on=tuple(sorted(waiting_on)),
        )

    def _unit_state(
        self,
        artifact_id: str,
        unit: _StageUnit,
        policy_version_id: str,
        runs: Sequence[Mapping[str, Any]],
        jobs: Sequence[Mapping[str, Any]],
    ) -> StageState:
        tool = self._tool_snapshot(unit.stage)
        latest_run = _latest_unit_run(unit, policy_version_id, tool.version, runs)
        job_key = _stage_key(
            "job",
            artifact_id=artifact_id,
            policy_version_id=policy_version_id,
            stage_name=unit.name,
            tool_version=tool.version,
        )
        matching_job = next(
            (job for job in reversed(jobs) if job.get("idempotency_key") == job_key),
            None,
        )
        if matching_job is None and unit.stage == ReviewPolicyStage.STATIC:
            matching_job = next(
                (
                    job
                    for job in reversed(jobs)
                    if job.get("type") == JobType.STATIC_SCAN.value
                    and str(job.get("policy_version_id") or "") == policy_version_id
                ),
                None,
            )
        active_job = matching_job and matching_job.get("status") in {
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
        }
        if latest_run is not None:
            run_state = _run_state(latest_run)
            synthetic = bool((latest_run.get("coverage") or {}).get("synthetic"))
            if run_state in TERMINAL_STAGE_STATES and not (synthetic and active_job):
                return run_state
        if active_job:
            return StageState.RUNNING
        if matching_job is not None:
            return StageState.FAILED
        if latest_run is not None:
            return StageState.FAILED
        return StageState.PENDING

    def _tool_snapshot(self, stage: ReviewPolicyStage) -> StageToolSnapshot:
        if stage == ReviewPolicyStage.STATIC:
            return self.tool_snapshots.get(stage) or StageToolSnapshot("p1.1")
        return self.tool_snapshots.get(stage) or StageToolSnapshot(
            "unconfigured",
            ready=False,
            reason=f"{stage.value}_tool_not_configured",
        )

    async def _enqueue_stage_job(
        self,
        artifact: Mapping[str, Any],
        policy_version_id: str,
        unit: _StageUnit,
        tool: StageToolSnapshot,
    ) -> dict[str, Any]:
        input_sha256 = _stage_input_sha256(artifact, policy_version_id, unit, tool.version)
        return await self.repository.enqueue_job(
            {
                "artifact_id": artifact["id"],
                "type": unit.job_type.value,
                "payload": {
                    "stage": unit.stage.value,
                    "stage_name": unit.name,
                    "tool_version": tool.version,
                    "input_sha256": input_sha256,
                    "artifact_sha256": artifact.get("archive_sha256") or "",
                    "tree_sha256": artifact.get("tree_sha256") or "",
                    "target": dict(unit.target),
                },
                "max_attempts": tool.max_attempts,
                "idempotency_key": _stage_key(
                    "job",
                    artifact_id=str(artifact["id"]),
                    policy_version_id=policy_version_id,
                    stage_name=unit.name,
                    tool_version=tool.version,
                ),
                "policy_version_id": policy_version_id,
                "stage_name": unit.name,
            }
        )

    async def _record_synthetic_outcome(
        self,
        artifact: Mapping[str, Any],
        policy_version_id: str,
        unit: _StageUnit,
        tool: StageToolSnapshot,
        *,
        state: StageState,
        reason: str,
        upstream: Mapping[str, StageState],
    ) -> dict[str, Any]:
        input_sha256 = _stage_input_sha256(artifact, policy_version_id, unit, tool.version)
        coverage = {
            "outcome": state.value,
            "complete": False,
            "synthetic": True,
            "reason": reason,
            "stage": unit.stage.value,
            "stage_name": unit.name,
            "upstream": {name: value.value for name, value in sorted(upstream.items())},
        }
        if not tool.ready:
            coverage["tool_reason"] = tool.reason
        return await self.repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": unit.run_type,
                "status": ReviewRunStatus.CANCELLED.value,
                "attempt": 1,
                "tool_name": unit.stage.value,
                "tool_version": tool.version,
                "policy_version_id": policy_version_id,
                "input_sha256": input_sha256,
                "coverage": coverage,
                "summary": f"Stage {unit.name} was not executed: {reason}",
                "error_code": reason,
                "astrbot_version": unit.target.get("astrbot", ""),
                "python_version": unit.target.get("python", ""),
                "idempotency_key": _stage_key(
                    f"synthetic:{state.value}:{reason}",
                    artifact_id=str(artifact["id"]),
                    policy_version_id=policy_version_id,
                    stage_name=unit.name,
                    tool_version=tool.version,
                ),
            }
        )

    async def _enqueue_route_job(
        self,
        artifact: Mapping[str, Any],
        policy_version_id: str,
        unit_states: Mapping[str, StageState],
    ) -> dict[str, Any]:
        return await self.repository.enqueue_job(
            {
                "artifact_id": artifact["id"],
                "type": JobType.ROUTE_REVIEW.value,
                "payload": {
                    "stage": ROUTING_STAGE_NAME,
                    "tool_version": ROUTING_TOOL_VERSION,
                    "stage_states": {
                        name: state.value for name, state in sorted(unit_states.items())
                    },
                },
                "max_attempts": 3,
                "idempotency_key": _stage_key(
                    "job",
                    artifact_id=str(artifact["id"]),
                    policy_version_id=policy_version_id,
                    stage_name=ROUTING_STAGE_NAME,
                    tool_version=ROUTING_TOOL_VERSION,
                ),
                "policy_version_id": policy_version_id,
                "stage_name": ROUTING_STAGE_NAME,
            }
        )


def review_run_type_for_job(job: Mapping[str, Any]) -> str | None:
    payload = job.get("payload") if isinstance(job.get("payload"), Mapping) else {}
    stage_name = str(payload.get("stage") or job.get("stage_name") or "")
    if stage_name == ROUTING_STAGE_NAME:
        return ROUTING_STAGE_NAME
    try:
        return ReviewPolicyStage(stage_name).value
    except ValueError:
        pass
    job_type = str(job.get("type") or "")
    return {
        JobType.PRECHECK.value: "precheck",
        JobType.STATIC_SCAN.value: ReviewPolicyStage.STATIC.value,
        JobType.DIFF_GRAPH.value: ReviewPolicyStage.DIFF.value,
        JobType.CLAMAV_SCAN.value: ReviewPolicyStage.CLAMAV.value,
        JobType.YARA_SCAN.value: ReviewPolicyStage.YARA.value,
        JobType.RUNTIME_DISPATCH.value: ReviewPolicyStage.RUNTIME.value,
        JobType.RUNTIME_COLLECT.value: ReviewPolicyStage.RUNTIME.value,
        JobType.DEPENDENCY_SCAN.value: ReviewPolicyStage.DEPENDENCY.value,
        JobType.CATEGORY.value: ReviewPolicyStage.CATEGORY.value,
        JobType.LLM_PACKAGE.value: ReviewPolicyStage.LLM_PACKAGE.value,
        JobType.LLM_FILE.value: ReviewPolicyStage.LLM_FILE.value,
        JobType.LLM_SUMMARY.value: ReviewPolicyStage.LLM_SUMMARY.value,
        JobType.ROUTE_REVIEW.value: ROUTING_STAGE_NAME,
    }.get(job_type)


def _stage_units(policy: ReviewPolicyV1) -> tuple[_StageUnit, ...]:
    units: list[_StageUnit] = []
    for phase in _STAGE_PHASES:
        for stage in phase:
            if stage not in policy.required_stages:
                continue
            if stage == ReviewPolicyStage.RUNTIME:
                for target in policy.runtime_targets:
                    units.append(
                        _StageUnit(
                            stage=stage,
                            name=f"runtime:{target.astrbot}:python-{target.python}",
                            job_type=_STAGE_JOB_TYPES[stage],
                            run_type=stage.value,
                            target=MappingProxyType(
                                {"astrbot": target.astrbot, "python": target.python}
                            ),
                        )
                    )
                continue
            units.append(
                _StageUnit(
                    stage=stage,
                    name=stage.value,
                    job_type=_STAGE_JOB_TYPES[stage],
                    run_type=stage.value,
                    target=MappingProxyType({}),
                )
            )
    return tuple(units)


def _latest_unit_run(
    unit: _StageUnit,
    policy_version_id: str,
    tool_version: str,
    runs: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for run in reversed(runs):
        if (
            str(run.get("type") or "") != unit.run_type
            or str(run.get("policy_version_id") or "") != policy_version_id
        ):
            continue
        coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
        run_stage_name = str(coverage.get("stage_name") or run.get("type") or "")
        if unit.stage == ReviewPolicyStage.RUNTIME:
            if str(run.get("astrbot_version") or "") != unit.target.get("astrbot") or str(
                run.get("python_version") or ""
            ) != unit.target.get("python"):
                continue
        elif run_stage_name != unit.name:
            continue
        if (
            unit.stage != ReviewPolicyStage.STATIC
            and str(run.get("tool_version") or "") != tool_version
        ):
            continue
        return run
    return None


def _run_state(run: Mapping[str, Any]) -> StageState:
    coverage = run.get("coverage") if isinstance(run.get("coverage"), Mapping) else {}
    outcome = str(coverage.get("outcome") or "")
    if outcome in {state.value for state in TERMINAL_STAGE_STATES}:
        return StageState(outcome)
    status = str(run.get("status") or "")
    if status == ReviewRunStatus.SUCCEEDED.value:
        return StageState.COMPLETED
    if status in {ReviewRunStatus.QUEUED.value, ReviewRunStatus.RUNNING.value}:
        return StageState.RUNNING
    return StageState.FAILED


def _aggregate_stage_state(
    stage: ReviewPolicyStage,
    units: Sequence[_StageUnit],
    unit_states: Mapping[str, StageState],
) -> StageState:
    states = [unit_states[unit.name] for unit in units if unit.stage == stage]
    if not states:
        return StageState.PENDING
    if any(state == StageState.PENDING for state in states):
        return StageState.PENDING
    if any(state == StageState.RUNNING for state in states):
        return StageState.RUNNING
    for state in (
        StageState.BLOCKED,
        StageState.DEGRADED,
        StageState.FAILED,
        StageState.SKIPPED,
    ):
        if state in states:
            return state
    return StageState.COMPLETED


def _upstream_reason(states: Mapping[str, StageState]) -> str:
    for state, reason in (
        (StageState.BLOCKED, "upstream_blocked"),
        (StageState.DEGRADED, "upstream_degraded"),
        (StageState.FAILED, "upstream_failed"),
        (StageState.SKIPPED, "upstream_incomplete"),
    ):
        if state in states.values():
            return reason
    return "upstream_incomplete"


def _stage_input_sha256(
    artifact: Mapping[str, Any],
    policy_version_id: str,
    unit: _StageUnit,
    tool_version: str,
) -> str:
    payload = {
        "artifact_id": artifact["id"],
        "archive_sha256": artifact.get("archive_sha256") or "",
        "tree_sha256": artifact.get("tree_sha256") or "",
        "policy_version_id": policy_version_id,
        "stage_name": unit.name,
        "target": dict(unit.target),
        "tool_version": tool_version,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _stage_key(
    kind: str,
    *,
    artifact_id: str,
    policy_version_id: str,
    stage_name: str,
    tool_version: str,
) -> str:
    digest = hashlib.sha256(
        "\x00".join((artifact_id, policy_version_id, stage_name, tool_version, kind)).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"review-stage:{stage_name}:{digest}"


def _find_route_job(
    jobs: Sequence[Mapping[str, Any]],
    artifact_id: str,
    policy_version_id: str,
) -> Mapping[str, Any] | None:
    key = _stage_key(
        "job",
        artifact_id=artifact_id,
        policy_version_id=policy_version_id,
        stage_name=ROUTING_STAGE_NAME,
        tool_version=ROUTING_TOOL_VERSION,
    )
    return next((job for job in reversed(jobs) if job.get("idempotency_key") == key), None)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
