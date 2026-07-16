from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

from app.artifacts.archive import ArchivePrechecker
from app.artifacts.diff import DIFF_TOOL_VERSION
from app.artifacts.jobs import ArtifactJobRunner
from app.artifacts.models import JobType
from app.artifacts.orchestration import (
    ReviewOrchestrator,
    StageState,
    StageToolSnapshot,
)
from app.artifacts.policy import ReviewPolicyStage, review_policy_sha256
from app.artifacts.repository import ArtifactRepository, InMemoryArtifactRepository
from app.artifacts.stages import DiffGraphStage, StageContext, StageOutcome
from app.artifacts.static_scan import StaticScanner
from app.artifacts.storage import ArtifactStorage


def _policy_payload(
    stages: Iterable[ReviewPolicyStage],
    *,
    runtime_targets: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    required = [stage.value for stage in stages]
    return {
        "schema_version": "1",
        "required_stages": required,
        "runtime_targets": runtime_targets or [],
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {"clamav": ReviewPolicyStage.CLAMAV.value in required},
        "dependency": {"enabled": ReviewPolicyStage.DEPENDENCY.value in required},
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


async def _review_fixture(
    policy_payload: dict[str, Any],
) -> tuple[InMemoryArtifactRepository, dict[str, Any], dict[str, Any]]:
    repository = InMemoryArtifactRepository()
    policy = await repository.create_review_policy(
        {
            "version": "orchestration-policy-v1",
            "schema_version": "1",
            "status": "active",
            "is_default": True,
            "policy": policy_payload,
            "policy_sha256": review_policy_sha256(policy_payload),
            "validation_summary": {"valid": True},
            "validated_at": datetime.now(UTC).isoformat(),
            "activated_at": datetime.now(UTC).isoformat(),
        }
    )
    artifact = await repository.create_artifact(
        {
            "plugin_id": "astrbot_plugin_demo",
            "source_type": "upload",
            "source_repo": "https://github.com/alice/astrbot_plugin_demo",
            "source_ref": "main",
            "archive_sha256": "a" * 64,
            "tree_sha256": "b" * 64,
            "size_bytes": 123,
            "quarantine_key": "quarantine/artifact.zip",
            "submitted_by": "owner-1",
            "policy_version_id": policy["id"],
        }
    )
    await repository.transition_review_status(artifact["id"], "prechecking")
    artifact = await repository.transition_review_status(artifact["id"], "scanning")
    assert artifact is not None
    await repository.create_review_run(
        {
            "artifact_id": artifact["id"],
            "type": "static",
            "status": "succeeded",
            "attempt": 1,
            "ruleset_version": "p1.1",
            "tool_name": "static",
            "tool_version": "p1.1",
            "policy_version_id": policy["id"],
            "coverage": {"outcome": "completed", "stage_name": "static"},
        }
    )
    return repository, artifact, policy


async def _complete_stage_job(
    repository: InMemoryArtifactRepository,
    *,
    stage_name: str,
    outcome: str = "completed",
) -> dict[str, Any]:
    claimed = await repository.claim_jobs(f"worker-{stage_name}", 10, 60)
    job = next(item for item in claimed if item["stage_name"] == stage_name)
    payload = job["payload"]
    target = payload.get("target") or {}
    run = await repository.create_review_run(
        {
            "artifact_id": job["artifact_id"],
            "type": payload["stage"],
            "status": "succeeded",
            "attempt": job["attempts"],
            "tool_name": payload["stage"],
            "tool_version": payload["tool_version"],
            "policy_version_id": job["policy_version_id"],
            "input_sha256": payload["input_sha256"],
            "astrbot_version": target.get("astrbot", ""),
            "python_version": target.get("python", ""),
            "coverage": {"outcome": outcome, "stage_name": stage_name},
        }
    )
    assert await repository.complete_job(job["id"], f"worker-{stage_name}") is True
    return run


def test_static_only_policy_enqueues_exactly_one_route_job() -> None:
    async def scenario() -> tuple[Any, Any, list[dict[str, Any]]]:
        repository, artifact, _ = await _review_fixture(_policy_payload([ReviewPolicyStage.STATIC]))
        orchestrator = ReviewOrchestrator(repository, tool_snapshots={})
        first = await orchestrator.reconcile(artifact["id"])
        second = await orchestrator.reconcile(artifact["id"])
        return first, second, await repository.list_artifact_jobs(artifact["id"])

    first, second, jobs = asyncio.run(scenario())

    assert first.stage_states == {"static": StageState.COMPLETED}
    assert first.route_job_id is not None
    assert len(first.enqueued_job_ids) == 1
    assert second.route_job_id == first.route_job_id
    assert second.enqueued_job_ids == ()
    assert [job["type"] for job in jobs] == [JobType.ROUTE_REVIEW.value]


def test_default_runner_configures_diff_without_claiming_import_graph_support() -> None:
    repository = InMemoryArtifactRepository()
    runner = ArtifactJobRunner(
        repository=repository,
        storage=cast(ArtifactStorage, object()),
        prechecker=cast(ArchivePrechecker, object()),
        scanner=cast(StaticScanner, object()),
        worker_id="diff-tool-worker",
        lease_seconds=60,
        poll_seconds=1,
        advanced_review_enabled=True,
    )

    assert runner.review_orchestrator.tool_snapshots[ReviewPolicyStage.DIFF].version == (
        DIFF_TOOL_VERSION
    )
    assert ReviewPolicyStage.IMPORT_GRAPH not in runner.review_orchestrator.tool_snapshots
    assert isinstance(runner._review_stages[JobType.DIFF_GRAPH.value], DiffGraphStage)


def test_concurrent_reconcile_still_creates_one_route_job() -> None:
    async def scenario() -> tuple[list[Any], list[dict[str, Any]]]:
        repository, artifact, _ = await _review_fixture(_policy_payload([ReviewPolicyStage.STATIC]))
        orchestrator = ReviewOrchestrator(repository, tool_snapshots={})
        results = await asyncio.gather(*(orchestrator.reconcile(artifact["id"]) for _ in range(12)))
        return results, await repository.list_artifact_jobs(artifact["id"])

    results, jobs = asyncio.run(scenario())

    route_ids = {result.route_job_id for result in results}
    assert len(route_ids) == 1
    assert None not in route_ids
    assert sum(job["type"] == JobType.ROUTE_REVIEW.value for job in jobs) == 1


def test_dag_advances_one_ready_phase_and_preserves_runtime_target() -> None:
    async def scenario() -> tuple[list[str], list[dict[str, Any]], Any]:
        policy = _policy_payload(
            [
                ReviewPolicyStage.STATIC,
                ReviewPolicyStage.DIFF,
                ReviewPolicyStage.IMPORT_GRAPH,
                ReviewPolicyStage.RUNTIME,
                ReviewPolicyStage.DEPENDENCY,
            ],
            runtime_targets=[{"astrbot": "4.26.5", "python": "3.11"}],
        )
        repository, artifact, _ = await _review_fixture(policy)
        snapshots = {
            ReviewPolicyStage.DIFF: StageToolSnapshot("diff-v1"),
            ReviewPolicyStage.IMPORT_GRAPH: StageToolSnapshot("import-graph-v1"),
            ReviewPolicyStage.RUNTIME: StageToolSnapshot("runtime-image-sha256-v1"),
            ReviewPolicyStage.DEPENDENCY: StageToolSnapshot("dependency-db-v1"),
        }
        orchestrator = ReviewOrchestrator(repository, tool_snapshots=snapshots)
        waves: list[str] = []
        for stage_name in (
            "diff",
            "import_graph",
            "runtime:4.26.5:python-3.11",
            "dependency",
        ):
            result = await orchestrator.reconcile(artifact["id"])
            jobs = await repository.list_artifact_jobs(artifact["id"])
            new_job = next(job for job in jobs if job["id"] in result.enqueued_job_ids)
            assert new_job["stage_name"] == stage_name
            waves.append(stage_name)
            await _complete_stage_job(repository, stage_name=stage_name)
        routed = await orchestrator.reconcile(artifact["id"])
        return waves, await repository.list_artifact_jobs(artifact["id"]), routed

    waves, jobs, routed = asyncio.run(scenario())

    assert waves == [
        "diff",
        "import_graph",
        "runtime:4.26.5:python-3.11",
        "dependency",
    ]
    runtime_job = next(job for job in jobs if job["stage_name"].startswith("runtime:"))
    assert runtime_job["payload"]["target"] == {"astrbot": "4.26.5", "python": "3.11"}
    assert jobs[-1]["type"] == JobType.ROUTE_REVIEW.value
    assert routed.route_job_id == jobs[-1]["id"]
    assert set(routed.stage_states.values()) == {StageState.COMPLETED}


def test_hard_block_records_skipped_coverage_before_routing() -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        policy = _policy_payload(
            [
                ReviewPolicyStage.STATIC,
                ReviewPolicyStage.CLAMAV,
                ReviewPolicyStage.RUNTIME,
                ReviewPolicyStage.DEPENDENCY,
            ],
            runtime_targets=[{"astrbot": "4.26.5", "python": "3.11"}],
        )
        repository, artifact, _ = await _review_fixture(policy)
        orchestrator = ReviewOrchestrator(
            repository,
            tool_snapshots={
                ReviewPolicyStage.CLAMAV: StageToolSnapshot("clamav-db-v1"),
                ReviewPolicyStage.RUNTIME: StageToolSnapshot("runtime-image-v1"),
                ReviewPolicyStage.DEPENDENCY: StageToolSnapshot("dependency-db-v1"),
            },
        )
        await orchestrator.reconcile(artifact["id"])
        await _complete_stage_job(repository, stage_name="clamav", outcome="blocked")
        result = await orchestrator.reconcile(artifact["id"])
        return (
            result,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    result, runs, jobs = asyncio.run(scenario())

    assert result.stage_states["clamav"] == StageState.BLOCKED
    assert result.stage_states["runtime"] == StageState.SKIPPED
    assert result.stage_states["dependency"] == StageState.SKIPPED
    skipped = [run for run in runs if (run.get("coverage") or {}).get("synthetic")]
    assert {run["type"] for run in skipped} == {"runtime", "dependency"}
    assert all(run["status"] == "cancelled" for run in skipped)
    assert all(run["coverage"]["reason"] == "upstream_blocked" for run in skipped)
    assert not any(job["type"] in {"runtime_dispatch", "dependency_scan"} for job in jobs)
    assert sum(job["type"] == "route_review" for job in jobs) == 1


def test_unavailable_tool_is_degraded_without_fake_success() -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]]]:
        policy = _policy_payload(
            [ReviewPolicyStage.STATIC, ReviewPolicyStage.RUNTIME],
            runtime_targets=[{"astrbot": "4.26.5", "python": "3.11"}],
        )
        repository, artifact, _ = await _review_fixture(policy)
        orchestrator = ReviewOrchestrator(
            repository,
            tool_snapshots={
                ReviewPolicyStage.RUNTIME: StageToolSnapshot(
                    "runtime-image-v1",
                    ready=False,
                    reason="runner health is unknown",
                )
            },
        )
        result = await orchestrator.reconcile(artifact["id"])
        return result, await repository.list_review_runs(artifact["id"])

    result, runs = asyncio.run(scenario())

    runtime = next(run for run in runs if run["type"] == "runtime")
    assert result.stage_states["runtime"] == StageState.DEGRADED
    assert runtime["status"] == "cancelled"
    assert runtime["completed_at"] is not None
    assert runtime["coverage"]["outcome"] == "degraded"
    assert runtime["coverage"]["tool_reason"] == "runner health is unknown"
    assert result.route_job_id is not None


def test_succeeded_job_without_terminal_run_is_failed_not_completed() -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        policy = _policy_payload(
            [ReviewPolicyStage.STATIC, ReviewPolicyStage.DIFF, ReviewPolicyStage.RUNTIME],
            runtime_targets=[{"astrbot": "4.26.5", "python": "3.11"}],
        )
        repository, artifact, _ = await _review_fixture(policy)
        orchestrator = ReviewOrchestrator(
            repository,
            tool_snapshots={
                ReviewPolicyStage.DIFF: StageToolSnapshot("diff-v1"),
                ReviewPolicyStage.RUNTIME: StageToolSnapshot("runtime-image-v1"),
            },
        )
        await orchestrator.reconcile(artifact["id"])
        claimed = await repository.claim_jobs("lost-result-worker", 1, 60)
        assert claimed[0]["stage_name"] == "diff"
        assert await repository.complete_job(claimed[0]["id"], "lost-result-worker") is True
        result = await orchestrator.reconcile(artifact["id"])
        return (
            result,
            await repository.list_review_runs(artifact["id"]),
            await repository.list_artifact_jobs(artifact["id"]),
        )

    result, runs, jobs = asyncio.run(scenario())

    assert result.stage_states["diff"] == StageState.FAILED
    assert result.stage_states["runtime"] == StageState.SKIPPED
    runtime = next(run for run in runs if run["type"] == "runtime")
    assert runtime["status"] == "cancelled"
    assert runtime["coverage"]["reason"] == "upstream_failed"
    assert sum(job["type"] == JobType.ROUTE_REVIEW.value for job in jobs) == 1


def test_routing_ack_loss_is_idempotently_recovered() -> None:
    async def scenario() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        repository, artifact, _ = await _review_fixture(_policy_payload([ReviewPolicyStage.STATIC]))
        await ReviewOrchestrator(repository, tool_snapshots={}).reconcile(artifact["id"])
        claimed = await repository.claim_jobs("lost-ack-worker", 1, 60)
        assert claimed[0]["type"] == JobType.ROUTE_REVIEW.value
        runner = ArtifactJobRunner(
            repository=repository,
            storage=cast(ArtifactStorage, object()),
            prechecker=cast(ArchivePrechecker, object()),
            scanner=cast(StaticScanner, object()),
            worker_id="routing-recovery-worker",
            lease_seconds=60,
            poll_seconds=1,
            advanced_review_enabled=True,
        )
        await runner._run_review_stage(claimed[0])
        repository.jobs[claimed[0]["id"]]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        assert await runner.run_once() == 1
        current = await repository.get_artifact(artifact["id"])
        assert current is not None
        return (
            current,
            await repository.list_review_runs(artifact["id"]),
            list(repository.outbox.values()),
        )

    artifact, runs, outbox = asyncio.run(scenario())

    routing_runs = [run for run in runs if run["type"] == "routing"]
    assert artifact["review_status"] == "pending_review"
    assert artifact["automated_review_completed_at"] is not None
    assert len(routing_runs) == 1
    assert routing_runs[0]["status"] == "succeeded"
    assert sum(event["event_type"] == "artifact_pending_review" for event in outbox) == 1


class _AttemptStage:
    job_type = JobType.DIFF_GRAPH.value

    async def execute(self, context: StageContext) -> StageOutcome:
        payload = context.job["payload"]
        run = await context.repository.create_review_run(
            {
                "artifact_id": context.artifact["id"],
                "type": "diff",
                "status": "running",
                "attempt": context.attempt,
                "tool_name": "diff",
                "tool_version": payload["tool_version"],
                "policy_version_id": context.artifact["policy_version_id"],
                "coverage": {"stage_name": "diff"},
            }
        )
        if context.attempt == 1:
            return StageOutcome.retryable_failure("temporary_failure", "retry the stage")
        await context.repository.complete_review_run(
            run["id"],
            {
                "status": "succeeded",
                "summary": "recovered",
                "coverage": {"outcome": "completed", "stage_name": "diff"},
            },
        )
        return StageOutcome.completed("recovered")


def test_retry_and_expired_lease_create_new_run_attempts() -> None:
    async def run_explicit_retry() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        repository, artifact, policy = await _review_fixture(
            _policy_payload([ReviewPolicyStage.STATIC, ReviewPolicyStage.DIFF])
        )
        job = await repository.enqueue_job(
            {
                "artifact_id": artifact["id"],
                "type": JobType.DIFF_GRAPH.value,
                "payload": {"stage": "diff", "tool_version": "diff-v1"},
                "max_attempts": 3,
                "idempotency_key": "explicit-retry-stage",
                "policy_version_id": policy["id"],
                "stage_name": "diff",
            }
        )
        runner = _attempt_runner(repository)
        await runner.run_once()
        repository.jobs[job["id"]]["available_at"] = datetime.now(UTC).isoformat()
        await runner.run_once()
        return await repository.list_review_runs(artifact["id"]), repository.jobs[job["id"]]

    async def run_lease_recovery() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        repository, artifact, policy = await _review_fixture(
            _policy_payload([ReviewPolicyStage.STATIC, ReviewPolicyStage.DIFF])
        )
        job = await repository.enqueue_job(
            {
                "artifact_id": artifact["id"],
                "type": JobType.DIFF_GRAPH.value,
                "payload": {"stage": "diff", "tool_version": "diff-v1"},
                "max_attempts": 3,
                "idempotency_key": "lease-recovery-stage",
                "policy_version_id": policy["id"],
                "stage_name": "diff",
            }
        )
        claimed = await repository.claim_jobs("crashed-worker", 1, 60)
        assert claimed[0]["attempts"] == 1
        await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "diff",
                "status": "running",
                "attempt": 1,
                "tool_name": "diff",
                "tool_version": "diff-v1",
                "policy_version_id": policy["id"],
                "coverage": {"stage_name": "diff"},
            }
        )
        repository.jobs[job["id"]]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        await _attempt_runner(repository).run_once()
        return await repository.list_review_runs(artifact["id"]), repository.jobs[job["id"]]

    retry_runs, retry_job = asyncio.run(run_explicit_retry())
    lease_runs, lease_job = asyncio.run(run_lease_recovery())

    assert [(run["attempt"], run["status"]) for run in retry_runs if run["type"] == "diff"] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert retry_job["status"] == "succeeded"
    assert [(run["attempt"], run["status"]) for run in lease_runs if run["type"] == "diff"] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert lease_job["status"] == "succeeded"


def _attempt_runner(repository: InMemoryArtifactRepository) -> ArtifactJobRunner:
    stage = _AttemptStage()
    return ArtifactJobRunner(
        repository=cast(ArtifactRepository, repository),
        storage=cast(ArtifactStorage, object()),
        prechecker=cast(ArchivePrechecker, object()),
        scanner=cast(StaticScanner, object()),
        worker_id="recovery-worker",
        lease_seconds=60,
        poll_seconds=1,
        review_stages={stage.job_type: stage},
    )
