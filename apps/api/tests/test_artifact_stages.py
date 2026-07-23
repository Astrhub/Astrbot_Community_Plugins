from __future__ import annotations

import asyncio
import logging
import operator
from collections.abc import Mapping
from typing import Any, cast

import pytest

from app.artifacts.jobs import ArtifactJobRunner, JobExecutionError
from app.artifacts.repository import ArtifactRepository
from app.artifacts.stages import ReviewStage, StageContext, StageOutcome, StageOutcomeKind
from app.artifacts.storage import ArtifactStorage


class _RecordingRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return {
            "id": artifact_id,
            "plugin_id": "astrbot_plugin_demo",
            "submitted_by": "owner-1",
            "policy_version_id": None,
        }

    async def get_review_policy(self, policy_id: str) -> dict[str, Any] | None:
        return None

    async def enqueue_outbox(self, event: Mapping[str, Any]) -> dict[str, Any]:
        saved = dict(event)
        self.events.append(saved)
        return saved


class _OutcomeStage:
    job_type = "contract_stage"

    def __init__(self, outcome: StageOutcome, *, job_type: str = "contract_stage") -> None:
        self.outcome = outcome
        self.job_type = job_type

    async def execute(self, context: StageContext) -> StageOutcome:
        return self.outcome


def test_stage_outcome_has_explicit_job_semantics_and_immutable_evidence() -> None:
    completed = StageOutcome.completed(
        "done",
        coverage={"files": ["main.py"]},
        details={"nested": {"value": 1}},
    )
    blocked = StageOutcome.blocked("malware_found", "blocked")
    degraded = StageOutcome.degraded("scanner_unavailable", "degraded")
    retryable = StageOutcome.retryable_failure("storage_unavailable", "retry later")
    terminal = StageOutcome.terminal_failure("schema_invalid", "stop")

    assert completed.kind == StageOutcomeKind.COMPLETED
    assert completed.completes_job is True
    assert blocked.completes_job is True
    assert degraded.completes_job is True
    assert retryable.completes_job is False
    assert retryable.retryable is True
    assert terminal.completes_job is False
    assert terminal.retryable is False
    assert completed.coverage["files"] == ("main.py",)
    with pytest.raises(TypeError):
        operator.setitem(completed.details["nested"], "value", 2)
    with pytest.raises(ValueError, match="summary"):
        StageOutcome.completed("  ")
    with pytest.raises(ValueError, match="error code"):
        StageOutcome(StageOutcomeKind.BLOCKED, "blocked")


def test_stage_context_is_a_deep_snapshot_and_emits_bounded_identity_event() -> None:
    async def scenario() -> tuple[StageContext, _RecordingRepository]:
        repository = _RecordingRepository()
        job = {"id": "job-1", "type": "precheck", "attempts": 2, "payload": {"ref": "a"}}
        artifact = {
            "id": "artifact-1",
            "plugin_id": "astrbot_plugin_demo",
            "submitted_by": "owner-1",
            "metadata": {"tags": ["tool"]},
        }
        policy = {"id": "policy-1", "policy": {"required_stages": ["static"]}}
        tool = object()
        context = StageContext.create(
            job=job,
            artifact=artifact,
            policy=policy,
            repository=cast(ArtifactRepository, repository),
            storage=cast(ArtifactStorage, object()),
            tools={"probe": tool},
            logger=logging.getLogger("test-stage"),
        )

        job["payload"]["ref"] = "changed"
        artifact["metadata"]["tags"].append("changed")
        policy["policy"]["required_stages"].append("runtime")

        assert context.job["payload"]["ref"] == "a"
        assert context.artifact["metadata"]["tags"] == ("tool",)
        assert context.policy is not None
        assert context.policy["policy"]["required_stages"] == ("static",)
        assert context.require_tool("probe", object) is tool
        assert context.attempt == 2
        with pytest.raises(TypeError):
            operator.setitem(context.artifact["metadata"], "name", "changed")
        with pytest.raises(RuntimeError, match="missing"):
            context.require_tool("missing", object)

        await context.emit_status("artifact_stage_completed", "stage-completed", {"code": "ok"})
        return context, repository

    context, repository = asyncio.run(scenario())

    assert context.log_context == {
        "artifact_id": "artifact-1",
        "job_id": "job-1",
        "job_type": "precheck",
    }
    assert repository.events == [
        {
            "event_type": "artifact_stage_completed",
            "aggregate_type": "artifact",
            "aggregate_id": "artifact-1",
            "recipient_user_id": "owner-1",
            "payload": {
                "artifact_id": "artifact-1",
                "plugin_id": "astrbot_plugin_demo",
                "code": "ok",
            },
            "dedupe_key": "artifact:artifact-1:stage-completed",
        }
    ]


@pytest.mark.parametrize(
    ("outcome", "expected_retryable"),
    [
        (StageOutcome.retryable_failure("temporary", "retry"), True),
        (StageOutcome.terminal_failure("invalid", "stop"), False),
    ],
)
def test_runner_translates_failed_stage_outcomes(
    outcome: StageOutcome,
    expected_retryable: bool,
) -> None:
    async def scenario() -> JobExecutionError:
        repository = _RecordingRepository()
        stage = _OutcomeStage(outcome)
        assert isinstance(stage, ReviewStage)
        runner = ArtifactJobRunner(
            repository=cast(ArtifactRepository, repository),
            storage=cast(ArtifactStorage, object()),
            prechecker=cast(Any, object()),
            scanner=cast(Any, object()),
            worker_id="stage-contract-worker",
            lease_seconds=60,
            poll_seconds=1,
            review_stages={stage.job_type: stage},
        )
        with pytest.raises(JobExecutionError) as caught:
            await runner._run_review_stage(
                {"id": "job-1", "artifact_id": "artifact-1", "type": stage.job_type}
            )
        return caught.value

    error = asyncio.run(scenario())

    assert error.code == outcome.error_code
    assert error.retryable is expected_retryable


@pytest.mark.parametrize(
    "outcome",
    [
        StageOutcome.completed("done"),
        StageOutcome.blocked("risk", "blocked"),
        StageOutcome.degraded("tool_unknown", "manual review required"),
    ],
)
def test_runner_completes_handled_stage_outcomes(outcome: StageOutcome) -> None:
    async def scenario() -> None:
        repository = _RecordingRepository()
        stage = _OutcomeStage(outcome)
        runner = ArtifactJobRunner(
            repository=cast(ArtifactRepository, repository),
            storage=cast(ArtifactStorage, object()),
            prechecker=cast(Any, object()),
            scanner=cast(Any, object()),
            worker_id="stage-contract-worker",
            lease_seconds=60,
            poll_seconds=1,
            review_stages={stage.job_type: stage},
        )
        await runner._run_review_stage(
            {"id": "job-1", "artifact_id": "artifact-1", "type": stage.job_type}
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("job_type", "outcome", "event_type", "recipient_user_id"),
    [
        (
            "runtime_dispatch",
            StageOutcome.blocked("runtime_probe_failed", "private runtime failure"),
            "artifact_runtime_failed",
            "owner-1",
        ),
        (
            "runtime_collect",
            StageOutcome.degraded("runtime_result_unavailable", "private runtime detail"),
            "artifact_runtime_failed",
            "owner-1",
        ),
        (
            "dependency_scan",
            StageOutcome.blocked("dependency_vulnerability", "private dependency detail"),
            "artifact_dependency_failed",
            "owner-1",
        ),
        (
            "llm_package",
            StageOutcome.degraded("llm_invalid_response", "private model response"),
            "artifact_review_tool_degraded",
            None,
        ),
    ],
)
def test_runner_emits_bounded_stage_alerts(
    job_type: str,
    outcome: StageOutcome,
    event_type: str,
    recipient_user_id: str | None,
) -> None:
    async def scenario() -> list[dict[str, Any]]:
        repository = _RecordingRepository()
        stage = _OutcomeStage(outcome, job_type=job_type)
        runner = ArtifactJobRunner(
            repository=cast(ArtifactRepository, repository),
            storage=cast(ArtifactStorage, object()),
            prechecker=cast(Any, object()),
            scanner=cast(Any, object()),
            worker_id="stage-contract-worker",
            lease_seconds=60,
            poll_seconds=1,
            review_stages={stage.job_type: stage},
        )
        await runner._run_review_stage(
            {"id": "job-alert-1", "artifact_id": "artifact-1", "type": stage.job_type}
        )
        return repository.events

    events = asyncio.run(scenario())

    assert len(events) == 1
    assert events[0]["event_type"] == event_type
    assert events[0]["recipient_user_id"] == recipient_user_id
    assert events[0]["payload"] == {
        "artifact_id": "artifact-1",
        "plugin_id": "astrbot_plugin_demo",
        "stage": job_type,
        "outcome": outcome.kind.value,
        "code": outcome.error_code,
    }
    assert "private" not in str(events[0])
