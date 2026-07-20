from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.artifacts.advisory import LocalDependencyAdvisoryProvider
from app.artifacts.jobs import ArtifactJobRunner
from app.artifacts.models import JobType
from app.artifacts.orchestration import ReviewOrchestrator, StageState, StageToolSnapshot
from app.artifacts.policy import ReviewPolicyStage, review_policy_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.runner_contract import (
    RuntimeDispatchRequest,
    build_runtime_dispatch_result,
    runtime_result_object_key,
    runtime_sbom_object_key,
)
from app.artifacts.runtime_dispatch import RuntimeDispatchController
from app.artifacts.sbom import build_cyclonedx_sbom
from app.artifacts.stages import (
    DependencyStage,
    RuntimeCollectStage,
    RuntimeDispatchStage,
    StageContext,
)
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.runtime_runner.queue import RuntimeRunnerQueue

IMAGE_DIGEST = f"sha256:{'c' * 64}"


def policy_payload(*, targets: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "required_stages": ["static", "runtime", "dependency"],
        "runtime_targets": targets or [{"astrbot": "4.26.5", "python": "3.12"}],
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {},
        "dependency": {
            "enabled": True,
            "max_severity": "high",
            "on_unavailable": "manual_review",
            "denied_licenses": ["GPL-3.0-only"],
            "private_package_prefixes": ["private-"],
        },
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


async def fixture(
    root: Path,
    *,
    targets: list[dict[str, str]] | None = None,
) -> tuple[
    InMemoryArtifactRepository,
    LocalArtifactStorage,
    RuntimeDispatchController,
    dict[str, Any],
    dict[str, Any],
]:
    repository = InMemoryArtifactRepository()
    policy_data = policy_payload(targets=targets)
    policy = await repository.create_review_policy(
        {
            "version": "dependency-policy-v1",
            "schema_version": "1",
            "status": "active",
            "is_default": True,
            "policy": policy_data,
            "policy_sha256": review_policy_sha256(policy_data),
            "validation_summary": {"valid": True},
            "validated_at": datetime.now(UTC).isoformat(),
            "activated_at": datetime.now(UTC).isoformat(),
        }
    )
    artifact = await repository.create_artifact(
        {
            "id": "artifact_01",
            "plugin_id": "astrbot_plugin_demo",
            "plugin_name": "astrbot_plugin_demo",
            "version": "v1.2.3",
            "normalized_version": "1.2.3",
            "repo_version": "v1.2.3",
            "source_type": "github",
            "source_repo": "https://github.com/alice/astrbot_plugin_demo",
            "source_ref": "main",
            "source_commit_sha": "b" * 40,
            "archive_sha256": "a" * 64,
            "tree_sha256": "f" * 64,
            "size_bytes": 4096,
            "quarantine_key": "artifacts/artifact_01/source.zip",
            "policy_version_id": policy["id"],
        }
    )
    await repository.transition_review_status(artifact["id"], "prechecking")
    artifact = await repository.transition_review_status(artifact["id"], "scanning")
    assert artifact is not None
    await repository.create_review_run(
        {
            "artifact_id": artifact["id"],
            "type": "precheck",
            "status": "succeeded",
            "policy_version_id": policy["id"],
            "raw_result": {
                "metadata": {
                    "name": "astrbot_plugin_demo",
                    "version": "v1.2.3",
                    "astrbot_version": ">=4.26.5,<4.27.0",
                }
            },
            "coverage": {"outcome": "completed", "stage_name": "precheck"},
        }
    )
    storage = LocalArtifactStorage(root, "https://cdn.example.test")
    return repository, storage, RuntimeDispatchController(repository, storage), artifact, policy


def context(
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
    artifact: dict[str, Any],
    policy: dict[str, Any],
    job: dict[str, Any],
) -> StageContext:
    return StageContext.create(
        job=job,
        artifact=artifact,
        policy=policy,
        repository=repository,
        storage=storage,
        tools={},
        logger=logging.getLogger("test-dependency-stage"),
    )


def runtime_job(
    artifact: dict[str, Any],
    policy: dict[str, Any],
    *,
    astrbot_version: str = "4.26.5",
    python_version: str = "3.12",
) -> dict[str, Any]:
    return {
        "id": f"runtime-job-{astrbot_version}-{python_version}",
        "artifact_id": artifact["id"],
        "type": JobType.RUNTIME_DISPATCH.value,
        "attempts": 1,
        "policy_version_id": policy["id"],
        "payload": {
            "stage": "runtime",
            "stage_name": f"runtime:{astrbot_version}:python-{python_version}",
            "tool_version": "runtime-contract-v1",
            "input_sha256": "1" * 64,
            "target": {"astrbot": astrbot_version, "python": python_version},
        },
    }


def advisory_snapshot(*, stale: bool = False) -> bytes:
    generated = datetime.now(UTC) - (timedelta(days=7) if stale else timedelta())
    return json.dumps(
        {
            "schema_version": "1",
            "database_version": "fixture-db-v1",
            "source": "local-fixture",
            "generated_at": generated.isoformat(),
            "advisories": [
                {
                    "id": "GHSA-demo-1234",
                    "package": "demo-lib",
                    "affected": "==1.2.3",
                    "fixed_versions": ["1.2.4"],
                    "severity": "high",
                }
            ],
            "packages": [
                {
                    "name": "demo-lib",
                    "version": "1.2.3",
                    "license": "GPL-3.0-only",
                    "withdrawn": True,
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def build_result(
    request: RuntimeDispatchRequest,
    requirements: bytes,
    attempt: int = 1,
):
    packages = (
        {
            "name": "AstrBot",
            "version": request.target.astrbot_version,
            "source": "index",
            "requires": ["demo-lib"],
        },
        {"name": "demo-lib", "version": "1.2.3", "source": "index"},
    )
    from app.artifacts.runner_contract import InstalledPackage

    package_models = tuple(InstalledPackage.model_validate(item) for item in packages)
    sbom = build_cyclonedx_sbom(request.target.astrbot_version, package_models)
    sbom_sha256 = hashlib.sha256(sbom).hexdigest()
    passed = {"status": "passed", "duration_ms": 1}
    result = build_runtime_dispatch_result(
        {
            "schema_version": "1",
            "dispatch_id": request.dispatch_id,
            "artifact_sha256": request.artifact_sha256,
            "target": {
                **request.target.model_dump(mode="json"),
                "resolved_python_version": "3.12.10",
            },
            "install": {
                **passed,
                "astrbot_version": request.target.astrbot_version,
                "requirements_sha256": (
                    hashlib.sha256(requirements).hexdigest() if requirements.strip() else ""
                ),
                "pip_check": passed,
                "packages": packages,
                "conflicts": [],
                "core_before_sha256": "d" * 64,
                "core_after_sha256": "d" * 64,
                "sbom_key": runtime_sbom_object_key(request, attempt, sbom_sha256),
                "sbom_sha256": sbom_sha256,
            },
            "smoke": {
                **passed,
                "metadata": {
                    **passed,
                    "name": request.expected_plugin.name,
                    "version": request.expected_plugin.version,
                    "author": "Alice",
                },
                "import_probe": passed,
                "instance": passed,
                "initialize": passed,
                "startup": {**passed, "ready_ms": 1},
                "handlers": {**passed, "count": 0, "names": []},
                "hooks": {**passed, "count": 0, "names": []},
                "llm_tools": {**passed, "count": 0, "names": []},
                "failed_plugin": {"present": False},
                "termination": passed,
                "violations": [],
            },
            "network_attestation": {
                "status": "passed",
                "backend": "rootless-docker-v1",
                "install_profile": request.install_network_profile,
                "smoke_profile": "none",
                "install_egress_enforced": True,
                "private_network_blocked": True,
                "metadata_endpoint_blocked": True,
                "smoke_network_disabled": True,
                "violations": [],
            },
            "cleanup": passed,
        }
    )
    return result, sbom


async def complete_runtime(
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
    controller: RuntimeDispatchController,
    requirements: bytes = b"",
    *,
    collect: bool = True,
) -> None:
    queue = RuntimeRunnerQueue(repository, runner_id="runner-1")
    work = (await queue.claim(limit=1, lease_seconds=60))[0]
    result, sbom = build_result(work.request, requirements, work.attempt)
    assert result.install.sbom_key is not None
    await storage.put_text_content(result.install.sbom_key, sbom)
    result_key = runtime_result_object_key(
        work.request,
        work.attempt,
        result.result_sha256,
    )
    await storage.put_text_content(result_key, result.model_dump_json().encode())
    await queue.complete_result(work, result)
    if collect:
        await controller.collect(work.dispatch_id)


def test_runtime_dispatch_is_idempotent_and_collect_wait_is_requeued(tmp_path: Path) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(tmp_path)
        stage = RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST)
        job = runtime_job(artifact, policy)
        first = await stage.execute(context(repository, storage, artifact, policy, job))
        second = await stage.execute(context(repository, storage, artifact, policy, job))
        jobs = await repository.list_artifact_jobs(artifact["id"])
        collect_job = next(item for item in jobs if item["type"] == "runtime_collect")
        collect = RuntimeCollectStage(controller, max_polls=2, poll_delay_seconds=0)
        waiting = await collect.execute(context(repository, storage, artifact, policy, collect_job))
        return (
            first,
            second,
            waiting,
            repository,
            await repository.list_artifact_jobs(artifact["id"]),
        )

    first, second, waiting, repository, jobs = asyncio.run(scenario())

    assert first.kind.value == "completed"
    assert second.kind.value == "completed"
    assert waiting.coverage["waiting"] is True
    assert len(repository.dispatches) == 1
    assert len([run for run in repository.runs.values() if run["type"] == "runtime"]) == 1
    assert [job["payload"]["poll"] for job in jobs if job["type"] == "runtime_collect"] == [0, 1]


def test_runtime_collect_bounded_timeout_cancels_and_fails_run(tmp_path: Path) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(tmp_path)
        stage = RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST)
        await stage.execute(
            context(repository, storage, artifact, policy, runtime_job(artifact, policy))
        )
        collect_job = next(
            item
            for item in await repository.list_artifact_jobs(artifact["id"])
            if item["type"] == "runtime_collect"
        )
        timed_job = {
            **collect_job,
            "payload": {**collect_job["payload"], "poll": 1},
        }
        outcome = await RuntimeCollectStage(
            controller,
            max_polls=1,
            poll_delay_seconds=0,
        ).execute(context(repository, storage, artifact, policy, timed_job))
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        dispatch = next(iter(repository.dispatches.values()))
        return outcome, run, dispatch

    outcome, run, dispatch = asyncio.run(scenario())

    assert outcome.kind.value == "blocked"
    assert dispatch["status"] == "cancelled"
    assert dispatch["collected_at"]
    assert run["status"] == "failed"
    assert run["error_code"] == "runtime_collect_timeout"


def test_runtime_collect_job_retry_preserves_open_run_until_attempts_are_exhausted(
    tmp_path: Path,
) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(
            tmp_path,
            targets=[
                {"astrbot": "4.26.5", "python": "3.12"},
                {"astrbot": "4.26.5", "python": "3.13"},
            ],
        )
        stage = RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST)
        await stage.execute(
            context(repository, storage, artifact, policy, runtime_job(artifact, policy))
        )
        other_run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "runtime",
                "status": "running",
                "attempt": 1,
                "tool_name": "runtime-runner",
                "tool_version": stage.version,
                "policy_version_id": policy["id"],
                "container_image_digest": IMAGE_DIGEST,
                "astrbot_version": "4.26.5",
                "python_version": "3.13",
                "coverage": {
                    "outcome": "running",
                    "stage_name": "runtime:4.26.5:python-3.13",
                },
            }
        )
        collect_job = next(
            item
            for item in await repository.list_artifact_jobs(artifact["id"])
            if item["type"] == JobType.RUNTIME_COLLECT.value
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=object(),
            scanner=object(),
            worker_id="runtime-lease-recovery",
            lease_seconds=60,
            poll_seconds=1,
        )
        error = RuntimeError("runner lease lost")
        await runner._record_stage_failure(
            collect_job,
            "runtime_collect_retry",
            error,
            will_retry=True,
        )
        running = dict(next(item for item in repository.runs.values() if item["type"] == "runtime"))
        await runner._record_stage_failure(
            collect_job,
            "runtime_collect_failed",
            error,
            will_retry=False,
        )
        failed = next(
            item
            for item in repository.runs.values()
            if item["type"] == "runtime" and item["python_version"] == "3.12"
        )
        unaffected = repository.runs[other_run["id"]]
        return running, failed, unaffected

    running, failed, unaffected = asyncio.run(scenario())

    assert running["status"] == "running"
    assert failed["status"] == "failed"
    assert failed["error_code"] == "runtime_collect_failed"
    assert unaffected["status"] == "running"


def test_runtime_collect_rejects_cross_target_dispatch_binding(tmp_path: Path) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(
            tmp_path,
            targets=[
                {"astrbot": "4.26.5", "python": "3.12"},
                {"astrbot": "4.26.5", "python": "3.13"},
            ],
        )
        stage = RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST)
        for python_version in ("3.12", "3.13"):
            await stage.execute(
                context(
                    repository,
                    storage,
                    artifact,
                    policy,
                    runtime_job(
                        artifact,
                        policy,
                        python_version=python_version,
                    ),
                )
            )
        runs = {
            item["python_version"]: item
            for item in repository.runs.values()
            if item["type"] == "runtime"
        }
        dispatches = {
            item["request"]["target"]["python_version"]: item
            for item in repository.dispatches.values()
        }
        collect_job = next(
            item
            for item in await repository.list_artifact_jobs(artifact["id"])
            if item["type"] == JobType.RUNTIME_COLLECT.value
            and item["run_id"] == runs["3.12"]["id"]
        )
        mismatched = {
            **collect_job,
            "payload": {
                **collect_job["payload"],
                "dispatch_id": dispatches["3.13"]["id"],
            },
        }
        outcome = await RuntimeCollectStage(controller).execute(
            context(repository, storage, artifact, policy, mismatched)
        )
        return outcome, runs, dispatches

    outcome, runs, dispatches = asyncio.run(scenario())

    assert outcome.kind.value == "blocked"
    assert outcome.error_code == "runtime_collect_snapshot_invalid"
    assert runs["3.12"]["status"] == "failed"
    assert runs["3.13"]["status"] == "running"
    assert dispatches["3.13"]["collected_at"] is None


def test_runtime_collect_keeps_dag_running_until_dependency_is_ready(tmp_path: Path) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(tmp_path)
        await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "static",
                "status": "succeeded",
                "attempt": 1,
                "tool_name": "static",
                "tool_version": "p1.1",
                "policy_version_id": policy["id"],
                "coverage": {"outcome": "completed", "stage_name": "static"},
            }
        )
        runtime_stage = RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST)
        provider = LocalDependencyAdvisoryProvider(advisory_snapshot())
        orchestrator = ReviewOrchestrator(
            repository,
            tool_snapshots={
                ReviewPolicyStage.RUNTIME: StageToolSnapshot(runtime_stage.version),
                ReviewPolicyStage.DEPENDENCY: StageToolSnapshot(provider.version),
            },
        )
        initial = await orchestrator.reconcile(artifact["id"])
        dispatch_job = next(
            item
            for item in await repository.claim_jobs("runtime-dispatch-worker", 10, 60)
            if item["type"] == JobType.RUNTIME_DISPATCH.value
        )
        await runtime_stage.execute(context(repository, storage, artifact, policy, dispatch_job))
        assert await repository.complete_job(
            dispatch_job["id"],
            "runtime-dispatch-worker",
        )

        waiting = await orchestrator.reconcile(artifact["id"])
        waiting_runs = await repository.list_review_runs(artifact["id"])
        waiting_jobs = await repository.list_artifact_jobs(artifact["id"])

        await complete_runtime(repository, storage, controller, collect=False)
        collect_job = next(
            item
            for item in await repository.claim_jobs("runtime-collect-worker", 10, 60)
            if item["type"] == JobType.RUNTIME_COLLECT.value
        )
        collected = await RuntimeCollectStage(controller, poll_delay_seconds=0).execute(
            context(repository, storage, artifact, policy, collect_job)
        )
        assert await repository.complete_job(collect_job["id"], "runtime-collect-worker")
        ready = await orchestrator.reconcile(artifact["id"])
        ready_jobs = await repository.list_artifact_jobs(artifact["id"])
        return initial, waiting, waiting_runs, waiting_jobs, collected, ready, ready_jobs

    initial, waiting, waiting_runs, waiting_jobs, collected, ready, ready_jobs = asyncio.run(
        scenario()
    )

    assert initial.stage_states["runtime"] == StageState.RUNNING
    assert waiting.stage_states["runtime"] == StageState.RUNNING
    assert waiting.stage_states["dependency"] == StageState.PENDING
    assert "runtime" in waiting.waiting_on
    assert not any(run["type"] == "dependency" for run in waiting_runs)
    assert not any(job["type"] == JobType.DEPENDENCY_SCAN.value for job in waiting_jobs)
    assert collected.kind.value == "completed"
    assert ready.stage_states["runtime"] == StageState.COMPLETED
    assert ready.stage_states["dependency"] == StageState.RUNNING
    assert any(job["type"] == JobType.DEPENDENCY_SCAN.value for job in ready_jobs)


def test_dependency_stage_consumes_signed_sbom_and_creates_correlatable_findings(
    tmp_path: Path,
) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(tmp_path)
        requirements = b"demo-lib==1.2.3\n"
        file_id = "file_requirements"
        content_key = build_content_key(artifact["id"], file_id)
        await storage.put_text_content(content_key, requirements)
        await repository.replace_artifact_files(
            artifact["id"],
            [
                {
                    "id": file_id,
                    "path": "requirements.txt",
                    "language": "text",
                    "sha256": hashlib.sha256(requirements).hexdigest(),
                    "size_bytes": len(requirements),
                    "line_count": 1,
                    "is_text": True,
                    "content_key": content_key,
                    "flags": {},
                }
            ],
            artifact["tree_sha256"],
        )
        await RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST).execute(
            context(repository, storage, artifact, policy, runtime_job(artifact, policy))
        )
        await complete_runtime(repository, storage, controller, requirements)
        provider = LocalDependencyAdvisoryProvider(advisory_snapshot())
        dependency_job = {
            "id": "dependency-job-1",
            "artifact_id": artifact["id"],
            "type": "dependency_scan",
            "attempts": 1,
            "policy_version_id": policy["id"],
            "payload": {"stage": "dependency", "input_sha256": "2" * 64},
        }
        outcome = await DependencyStage(provider, storage).execute(
            context(repository, storage, artifact, policy, dependency_job)
        )
        findings = await repository.list_findings(artifact["id"])
        run = next(item for item in repository.runs.values() if item["type"] == "dependency")
        return outcome, findings, run

    outcome, findings, run = asyncio.run(scenario())

    assert outcome.kind.value == "blocked"
    assert run["coverage"]["advisory_status"] == "ok"
    assert run["coverage"]["no_known_vulnerabilities"] is False
    assert run["raw_result"]["runtime_targets"][0]["sbom_sha256"]
    vulnerability = next(
        item for item in findings if item["rule_id"] == "dependency_known_vulnerability"
    )
    assert vulnerability["correlation"]["dependency"] == {
        "name": "demo-lib",
        "version": "1.2.3",
        "advisory_id": "GHSA-demo-1234",
    }
    assert {item["rule_id"] for item in findings} >= {
        "dependency_known_vulnerability",
        "dependency_release_withdrawn",
        "dependency_license_denied",
    }


def test_dependency_stage_stale_or_missing_target_never_reports_clean(tmp_path: Path) -> None:
    async def stale_scenario():
        repository, storage, controller, artifact, policy = await fixture(tmp_path / "stale")
        await RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST).execute(
            context(repository, storage, artifact, policy, runtime_job(artifact, policy))
        )
        await complete_runtime(repository, storage, controller)
        outcome = await DependencyStage(
            LocalDependencyAdvisoryProvider(advisory_snapshot(stale=True)),
            storage,
        ).execute(
            context(
                repository,
                storage,
                artifact,
                policy,
                {
                    "id": "dependency-stale",
                    "type": "dependency_scan",
                    "attempts": 1,
                    "policy_version_id": policy["id"],
                    "payload": {"stage": "dependency"},
                },
            )
        )
        run = next(item for item in repository.runs.values() if item["type"] == "dependency")
        return outcome, run

    outcome, run = asyncio.run(stale_scenario())

    assert outcome.kind.value == "degraded"
    assert run["coverage"]["advisory_status"] == "stale"
    assert run["coverage"]["no_known_vulnerabilities"] is False


def test_dependency_stage_requires_every_fixed_runtime_target(tmp_path: Path) -> None:
    async def scenario():
        repository, storage, controller, artifact, policy = await fixture(
            tmp_path,
            targets=[
                {"astrbot": "4.26.5", "python": "3.12"},
                {"astrbot": "4.26.5", "python": "3.13"},
            ],
        )
        await RuntimeDispatchStage(controller, image_digest=IMAGE_DIGEST).execute(
            context(repository, storage, artifact, policy, runtime_job(artifact, policy))
        )
        await complete_runtime(repository, storage, controller)
        outcome = await DependencyStage(
            LocalDependencyAdvisoryProvider(advisory_snapshot()),
            storage,
        ).execute(
            context(
                repository,
                storage,
                artifact,
                policy,
                {
                    "id": "dependency-missing-target",
                    "type": "dependency_scan",
                    "attempts": 1,
                    "policy_version_id": policy["id"],
                    "payload": {"stage": "dependency"},
                },
            )
        )
        run = next(item for item in repository.runs.values() if item["type"] == "dependency")
        return outcome, run

    outcome, run = asyncio.run(scenario())

    assert outcome.kind.value == "degraded"
    assert run["coverage"]["advisory_status"] == "not_queried"
    assert run["coverage"]["no_known_vulnerabilities"] is False
    assert run["error_code"] == "dependency_runtime_target_incomplete"
