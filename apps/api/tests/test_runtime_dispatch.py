from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.runner_contract import (
    RuntimeDispatchRequest,
    RuntimeDispatchResult,
    build_runtime_dispatch_result,
    runtime_result_object_key,
)
from app.artifacts.runtime_dispatch import (
    CollectionState,
    RuntimeDispatchController,
    RuntimeDispatchServiceError,
    RuntimeDispatchWorkItem,
    RuntimeRunnerQueue,
)
from app.artifacts.storage import LocalArtifactStorage


class LeastPrivilegeRunnerRepository:
    def __init__(self, repository: InMemoryArtifactRepository) -> None:
        self._repository = repository

    async def claim_runtime_dispatches(
        self, runner_id: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        return await self._repository.claim_runtime_dispatches(runner_id, limit, lease_seconds)

    async def renew_runtime_dispatch_lease(
        self, dispatch_id: str, runner_id: str, lease_seconds: int
    ) -> bool:
        return await self._repository.renew_runtime_dispatch_lease(
            dispatch_id, runner_id, lease_seconds
        )

    async def complete_runtime_dispatch(
        self,
        dispatch_id: str,
        runner_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._repository.complete_runtime_dispatch(
            dispatch_id,
            runner_id,
            payload,
        )


def runtime_request(
    *,
    dispatch_id: str = "dispatch_01",
    artifact_id: str = "artifact_01",
    policy_id: str = "policy_01",
) -> RuntimeDispatchRequest:
    return RuntimeDispatchRequest.model_validate(
        {
            "schema_version": "1",
            "dispatch_id": dispatch_id,
            "artifact_id": artifact_id,
            "artifact_sha256": "a" * 64,
            "artifact_size_bytes": 4096,
            "quarantine_key": f"artifacts/{artifact_id}/source.zip",
            "policy_version_id": policy_id,
            "expected_plugin": {
                "name": "astrbot_plugin_demo",
                "version": "v1.2.3",
                "source_repo": "https://github.com/alice/astrbot_plugin_demo",
                "source_commit_sha": "b" * 40,
            },
            "target": {
                "astrbot_version": "4.26.5",
                "python_version": "3.12",
                "image_digest": f"sha256:{'c' * 64}",
                "platform": "linux/amd64",
                "astrbot_commit": "adebd2958ed8",
            },
            "limits": {
                "cpu": 1,
                "memory_mb": 768,
                "pids": 128,
                "timeout_seconds": 120,
                "disk_mb": 2048,
                "tmpfs_mb": 256,
                "max_log_bytes": 1_048_576,
                "max_result_bytes": 524_288,
            },
            "install_network_profile": "pypi-only-v1",
            "smoke_network_profile": "none",
            "result_key": f"runtime/results/{dispatch_id}",
        }
    )


def passed_probe(duration_ms: int = 1) -> dict[str, Any]:
    return {"status": "passed", "duration_ms": duration_ms}


def runtime_result(
    *,
    dispatch_id: str = "dispatch_01",
    cleanup_failed: bool = False,
) -> RuntimeDispatchResult:
    cleanup: dict[str, Any]
    if cleanup_failed:
        cleanup = {
            "status": "failed",
            "duration_ms": 100,
            "error_code": "cleanup_failed",
            "message": "one runtime resource remains",
            "leaked_resources": ["container-1"],
        }
    else:
        cleanup = {
            **passed_probe(100),
            "removed_containers": 2,
            "removed_volumes": 1,
            "removed_networks": 1,
            "removed_temp_roots": 1,
            "leaked_resources": [],
        }
    return build_runtime_dispatch_result(
        {
            "schema_version": "1",
            "dispatch_id": dispatch_id,
            "artifact_sha256": "a" * 64,
            "target": {
                "astrbot_version": "4.26.5",
                "python_version": "3.12",
                "resolved_python_version": "3.12.10",
                "image_digest": f"sha256:{'c' * 64}",
                "platform": "linux/amd64",
                "astrbot_commit": "adebd2958ed8",
            },
            "install": {
                **passed_probe(1200),
                "astrbot_version": "4.26.5",
                "pip_check": passed_probe(20),
                "packages": [{"name": "astrbot", "version": "4.26.5"}],
                "conflicts": [],
                "core_before_sha256": "d" * 64,
                "core_after_sha256": "d" * 64,
            },
            "smoke": {
                "status": "passed",
                "duration_ms": 3200,
                "metadata": {
                    **passed_probe(5),
                    "name": "astrbot_plugin_demo",
                    "version": "v1.2.3",
                    "author": "Alice",
                },
                "import_probe": passed_probe(80),
                "instance": passed_probe(10),
                "initialize": passed_probe(40),
                "startup": {**passed_probe(3000), "ready_ms": 2900},
                "handlers": {**passed_probe(2), "count": 1, "names": ["hello"]},
                "hooks": {**passed_probe(2), "count": 0, "names": []},
                "llm_tools": {**passed_probe(2), "count": 0, "names": []},
                "failed_plugin": {"present": False},
                "termination": passed_probe(30),
                "violations": [],
            },
            "network_attestation": {
                "status": "passed",
                "backend": "rootless-docker-v1",
                "install_profile": "pypi-only-v1",
                "smoke_profile": "none",
                "install_egress_enforced": True,
                "private_network_blocked": True,
                "metadata_endpoint_blocked": True,
                "smoke_network_disabled": True,
                "violations": [],
            },
            "cleanup": cleanup,
        }
    )


async def dispatch_fixture(
    root: Path,
    *,
    dispatch_id: str = "dispatch_01",
    max_attempts: int = 3,
) -> tuple[
    InMemoryArtifactRepository,
    LocalArtifactStorage,
    RuntimeDispatchController,
    RuntimeDispatchRequest,
    dict[str, Any],
]:
    repository = InMemoryArtifactRepository()
    artifact = await repository.create_artifact(
        {
            "id": "artifact_01",
            "plugin_id": "astrbot_plugin_demo",
            "version": "v1.2.3",
            "normalized_version": "1.2.3",
            "source_type": "github",
            "source_repo": "https://github.com/alice/astrbot_plugin_demo",
            "source_ref": "main",
            "source_commit_sha": "b" * 40,
            "archive_sha256": "a" * 64,
            "size_bytes": 4096,
            "quarantine_key": "artifacts/artifact_01/source.zip",
            "policy_version_id": "policy_01",
        }
    )
    run = await repository.create_review_run(
        {
            "artifact_id": artifact["id"],
            "type": "runtime",
            "status": "queued",
            "policy_version_id": "policy_01",
            "astrbot_version": "4.26.5",
            "python_version": "3.12",
            "container_image_digest": f"sha256:{'c' * 64}",
        }
    )
    storage = LocalArtifactStorage(root, "https://cdn.example.test")
    controller = RuntimeDispatchController(repository, storage)
    request = runtime_request(dispatch_id=dispatch_id)
    dispatch = await controller.create(request, run_id=run["id"], max_attempts=max_attempts)
    return repository, storage, controller, request, dispatch


def test_successful_dispatch_is_collected_once_and_completes_run(tmp_path: Path) -> None:
    async def scenario() -> tuple[
        Any,
        Any,
        dict[str, Any],
        LeastPrivilegeRunnerRepository,
        list[dict[str, Any]],
    ]:
        repository, storage, controller, request, dispatch = await dispatch_fixture(tmp_path)
        runner_repository = LeastPrivilegeRunnerRepository(repository)
        queue = RuntimeRunnerQueue(runner_repository, runner_id="runner-a")
        work = (await queue.claim(limit=1, lease_seconds=60))[0]
        result = runtime_result()
        result_key = runtime_result_object_key(request, work.attempt, result.result_sha256)
        await storage.put_text_content(result_key, result.model_dump_json().encode())
        completed = await queue.complete_result(work, result)
        first, second = await asyncio.gather(
            controller.collect(dispatch["id"]),
            controller.collect(dispatch["id"]),
        )
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        findings = await repository.list_findings(request.artifact_id)
        return completed, (first, second), run, runner_repository, findings

    completed, collections, run, runner_repository, findings = asyncio.run(scenario())

    assert completed["status"] == "succeeded"
    assert {item.state for item in collections} == {
        CollectionState.COLLECTED,
        CollectionState.ALREADY_COLLECTED,
    }
    assert run["status"] == "succeeded"
    assert run["coverage"]["statuses"] == {
        "install": "passed",
        "smoke": "passed",
        "network": "passed",
        "cleanup": "passed",
    }
    assert not hasattr(runner_repository, "create_runtime_dispatch")
    assert not hasattr(runner_repository, "collect_runtime_dispatch")
    assert findings == []


def test_failed_gate_produces_failed_dispatch_and_run(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, Any], Any, dict[str, Any], list[dict[str, Any]]]:
        repository, storage, controller, request, _ = await dispatch_fixture(tmp_path)
        queue = RuntimeRunnerQueue(
            LeastPrivilegeRunnerRepository(repository),
            runner_id="runner-a",
        )
        work = (await queue.claim(limit=1, lease_seconds=60))[0]
        result = runtime_result(cleanup_failed=True)
        result_key = runtime_result_object_key(request, work.attempt, result.result_sha256)
        await storage.put_text_content(result_key, result.model_dump_json().encode())
        completed = await queue.complete_result(work, result)
        collected = await controller.collect(work.dispatch_id)
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        findings = await repository.list_findings(request.artifact_id)
        return completed, collected, run, findings

    completed, collected, run, findings = asyncio.run(scenario())

    assert completed["status"] == "failed"
    assert completed["error_code"] == "cleanup_failed"
    assert collected.run_status == "failed"
    assert run["status"] == "failed"
    assert [(item["rule_id"], item["source"]) for item in findings] == [
        ("cleanup_failed", "runtime")
    ]


def test_result_identity_mismatch_does_not_complete_dispatch(tmp_path: Path) -> None:
    async def scenario() -> tuple[RuntimeDispatchWorkItem, dict[str, Any]]:
        repository, _, _, _, dispatch = await dispatch_fixture(tmp_path)
        queue = RuntimeRunnerQueue(
            LeastPrivilegeRunnerRepository(repository),
            runner_id="runner-a",
        )
        work = (await queue.claim(limit=1, lease_seconds=60))[0]
        with pytest.raises(ValueError, match="identity"):
            await queue.complete_result(work, runtime_result(dispatch_id="dispatch_other"))
        current = await repository.get_runtime_dispatch(dispatch["id"])
        assert current is not None
        return work, current

    _, dispatch = asyncio.run(scenario())

    assert dispatch["status"] == "running"
    assert dispatch["result_sha256"] is None


def test_invalid_claimed_request_is_failed_without_reaching_executor(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        repository, _, controller, _, dispatch = await dispatch_fixture(tmp_path)
        repository.dispatches[dispatch["id"]]["request"]["target"]["astrbot_version"] = "latest"
        queue = RuntimeRunnerQueue(
            LeastPrivilegeRunnerRepository(repository),
            runner_id="runner-a",
        )
        assert await queue.claim(limit=1, lease_seconds=60) == ()
        collected = await controller.collect(dispatch["id"])
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        return collected, run

    collected, run = asyncio.run(scenario())

    assert collected.state == CollectionState.COLLECTED
    assert collected.error_code == "runtime_request_invalid"
    assert run["status"] == "failed"


def test_missing_result_is_retryable_and_not_marked_collected(tmp_path: Path) -> None:
    async def scenario() -> tuple[RuntimeDispatchServiceError, dict[str, Any]]:
        repository, _, controller, request, dispatch = await dispatch_fixture(tmp_path)
        queue = RuntimeRunnerQueue(
            LeastPrivilegeRunnerRepository(repository),
            runner_id="runner-a",
        )
        work = (await queue.claim(limit=1, lease_seconds=60))[0]
        result = runtime_result()
        await queue.complete_result(work, result)
        with pytest.raises(RuntimeDispatchServiceError) as caught:
            await controller.collect(dispatch["id"])
        current = await repository.get_runtime_dispatch(dispatch["id"])
        assert current is not None
        assert current["result_key"] == runtime_result_object_key(
            request,
            work.attempt,
            result.result_sha256,
        )
        return caught.value, current

    error, dispatch = asyncio.run(scenario())

    assert error.code == "runtime_result_unavailable"
    assert error.retryable is True
    assert dispatch["collected_at"] is None


def test_invalid_result_object_is_collected_as_failure(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
        repository, storage, controller, request, dispatch = await dispatch_fixture(tmp_path)
        invalid = b'{"schema_version":"1","tampered":true}'
        invalid_result_key = runtime_result_object_key(request, 1, "f" * 64)
        stored = await storage.put_text_content(invalid_result_key, invalid)
        claimed = await repository.claim_runtime_dispatches("runner-a", 1, 60)
        await repository.complete_runtime_dispatch(
            dispatch["id"],
            "runner-a",
            {
                "status": "succeeded",
                "result_key": invalid_result_key,
                "result_sha256": stored.sha256,
            },
        )
        assert claimed
        collected = await controller.collect(dispatch["id"])
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        findings = await repository.list_findings(request.artifact_id)
        return collected, run, findings

    collected, run, findings = asyncio.run(scenario())

    assert collected.state == CollectionState.COLLECTED
    assert collected.error_code == "runtime_result_invalid"
    assert run["status"] == "failed"
    assert findings[0]["rule_id"] == "runtime_result_invalid"
    assert findings[0]["severity"] == "critical"


def test_expired_final_lease_is_timed_out_and_collected(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        repository, _, controller, _, dispatch = await dispatch_fixture(
            tmp_path,
            max_attempts=1,
        )
        queue = RuntimeRunnerQueue(
            LeastPrivilegeRunnerRepository(repository),
            runner_id="runner-a",
        )
        assert await queue.claim(limit=1, lease_seconds=60)
        repository.dispatches[dispatch["id"]]["lease_expires_at"] = datetime(
            2000, 1, 1, tzinfo=UTC
        ).isoformat()
        reconciled = await controller.reconcile_expired(limit=10)
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        return reconciled[0], run

    collected, run = asyncio.run(scenario())

    assert collected.state == CollectionState.COLLECTED
    assert collected.run_status == "timed_out"
    assert run["status"] == "timed_out"
    assert run["error_code"] == "runtime_dispatch_timeout"


def test_dispatch_creation_is_idempotent_and_conflict_visible(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any], RuntimeDispatchServiceError]:
        repository, storage, controller, request, dispatch = await dispatch_fixture(tmp_path)
        run = next(item for item in repository.runs.values() if item["type"] == "runtime")
        repeated = await controller.create(request, run_id=run["id"])
        conflicting = runtime_request(dispatch_id="dispatch_other")
        conflicting_controller = RuntimeDispatchController(repository, storage)
        with pytest.raises(RuntimeDispatchServiceError) as caught:
            await conflicting_controller.create(conflicting, run_id=run["id"])
        return dispatch, repeated, caught.value

    dispatch, repeated, error = asyncio.run(scenario())

    assert repeated["id"] == dispatch["id"]
    assert error.code == "runtime_dispatch_conflict"
