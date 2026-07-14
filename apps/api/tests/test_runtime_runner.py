from __future__ import annotations

import asyncio
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.artifacts.runner_contract import RuntimeDispatchResult
from app.runtime_runner.config import (
    RuntimeRunnerConfigurationError,
    RuntimeRunnerSettings,
    load_runtime_runner_settings,
)
from app.runtime_runner.container_executor import (
    ContainerExecutionPipeline,
    DeterministicFakeContainerExecutor,
    FakeFailureMode,
)
from app.runtime_runner.execution import (
    RuntimeExecutionService,
    build_runtime_execution_service,
)
from app.runtime_runner.queue import RuntimeDispatchWorkItem, RuntimeRunnerQueue
from app.runtime_runner.storage import LocalRuntimeResultWriter, RuntimeResultStorageError
from app.runtime_runner.worker import RuntimeRunnerWorker
from tests.runtime_runner_helpers import FakeRunnerRepository


def runner_settings(root: Path, *, shutdown_grace_seconds: float = 0.1) -> RuntimeRunnerSettings:
    return RuntimeRunnerSettings(
        database_url="postgresql://runner@localhost/market",
        runner_id="runner-test",
        result_storage_backend="local",
        result_root=root,
        executor_backend="test-only",
        claim_limit=1,
        lease_seconds=10,
        poll_seconds=0.01,
        orphan_cleanup_seconds=60,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )


def test_runner_config_uses_only_dedicated_environment_and_redacts_database_url(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeRunnerConfigurationError) as raised:
        load_runtime_runner_settings({"DATABASE_URL": "postgresql://market-secret"})
    assert raised.value.errors == ("runtime_runner_database_url_missing",)

    settings = load_runtime_runner_settings(
        {
            "RUNTIME_RUNNER_DATABASE_URL": "postgresql://runner:secret@db/market",
            "RUNTIME_RUNNER_ID": "runner-a",
            "RUNTIME_RUNNER_RESULT_ROOT": str(tmp_path),
            "RUNTIME_RUNNER_EXECUTOR_BACKEND": "rootless-docker",
            "RUNTIME_RUNNER_CLAIM_LIMIT": "2",
        }
    )

    assert settings.runner_id == "runner-a"
    assert settings.claim_limit == 2
    assert "secret" not in repr(settings)
    assert "database_url" not in settings.public_summary()


def test_runner_config_builds_rootless_docker_backend_without_exposing_mounts(
    tmp_path: Path,
) -> None:
    settings = load_runtime_runner_settings(
        {
            "RUNTIME_RUNNER_DATABASE_URL": "postgresql://runner@db/market",
            "RUNTIME_RUNNER_EXECUTOR_BACKEND": "rootless-docker",
            "RUNTIME_RUNNER_DOCKER_HOST": "unix:///run/user/1000/docker.sock",
            "RUNTIME_RUNNER_DOCKER_IMAGE_REPOSITORY": "registry.example/runtime-probe",
            "RUNTIME_RUNNER_ARTIFACT_ROOT": str(tmp_path),
            "RUNTIME_RUNNER_INSTALL_NETWORK": "runtime-install-v1",
        }
    )
    service = build_runtime_execution_service(settings)
    summary = settings.public_summary()

    assert isinstance(service, RuntimeExecutionService)
    assert summary["container_isolation"] == "rootless-required"
    assert summary["image_pinning"] == "digest"
    assert "docker_host" not in summary
    assert "artifact" not in " ".join(summary)
    asyncio.run(service.close())


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("RUNTIME_RUNNER_CLAIM_LIMIT", "0", "runtime_runner_claim_limit_out_of_range"),
        ("RUNTIME_RUNNER_LEASE_SECONDS", "bad", "runtime_runner_lease_seconds_invalid"),
        (
            "RUNTIME_RUNNER_RESULT_STORAGE_BACKEND",
            "s3",
            "runtime_runner_result_storage_backend_unsupported",
        ),
        ("RUNTIME_RUNNER_RESULT_ROOT", "relative", "runtime_runner_result_root_not_absolute"),
        (
            "RUNTIME_RUNNER_DOCKER_HOST",
            "tcp://127.0.0.1:2375",
            "runtime_runner_docker_host_invalid",
        ),
        (
            "RUNTIME_RUNNER_DOCKER_IMAGE_REPOSITORY",
            "registry/repo@latest",
            "runtime_runner_docker_image_repository_invalid",
        ),
        (
            "RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT",
            "sometimes",
            "runtime_runner_allow_rootful_development_invalid",
        ),
        (
            "RUNTIME_RUNNER_PACKAGE_INDEX_URL",
            "https://user:secret@pypi.org/simple",
            "runtime_runner_package_index_url_invalid",
        ),
        (
            "RUNTIME_RUNNER_INSTALL_PROXY_URL",
            "http://user:secret@astrbot-runtime-package-proxy:3128",
            "runtime_runner_install_proxy_url_invalid",
        ),
        (
            "RUNTIME_RUNNER_ORPHAN_TTL_SECONDS",
            "60",
            "runtime_runner_orphan_ttl_seconds_out_of_range",
        ),
    ],
)
def test_runner_config_rejects_invalid_boundaries(name: str, value: str, code: str) -> None:
    with pytest.raises(RuntimeRunnerConfigurationError) as raised:
        load_runtime_runner_settings(
            {
                "RUNTIME_RUNNER_DATABASE_URL": "postgresql://runner@db/market",
                name: value,
            }
        )
    assert code in raised.value.errors


def test_local_result_writer_is_bounded_immutable_and_path_safe(tmp_path: Path) -> None:
    async def scenario() -> tuple[object, object]:
        writer = LocalRuntimeResultWriter(tmp_path)
        first = await writer.put_result("runtime/results/a.json", b"{}", max_bytes=10)
        second = await writer.put_result("runtime/results/a.json", b"{}", max_bytes=10)
        with pytest.raises(RuntimeResultStorageError, match="different content"):
            await writer.put_result("runtime/results/a.json", b"[]", max_bytes=10)
        with pytest.raises(RuntimeResultStorageError) as traversal:
            await writer.put_result("runtime/../secret", b"x", max_bytes=10)
        assert traversal.value.code == "runtime_result_key_invalid"
        with pytest.raises(RuntimeResultStorageError) as oversized:
            await writer.put_result("runtime/results/b.json", b"too-large", max_bytes=2)
        assert oversized.value.code == "runtime_result_too_large"
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second
    assert (tmp_path / "runtime/results/a.json").read_bytes() == b"{}"


def test_worker_uploads_result_before_completing_dispatch(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRunnerRepository, DeterministicFakeContainerExecutor]:
        repository = FakeRunnerRepository()
        queue = RuntimeRunnerQueue(repository, runner_id="runner-test")
        fake = DeterministicFakeContainerExecutor()
        worker = RuntimeRunnerWorker(
            queue=queue,
            result_writer=LocalRuntimeResultWriter(tmp_path),
            executor=ContainerExecutionPipeline(fake),
            settings=runner_settings(tmp_path),
        )
        assert await worker.run_once() == 1
        return repository, fake

    repository, fake = asyncio.run(scenario())
    completion = repository.completions[0]
    result_path = tmp_path / str(completion["result_key"])
    result = RuntimeDispatchResult.model_validate_json(result_path.read_bytes())

    assert completion["status"] == "succeeded"
    assert completion["result_sha256"] == result.result_sha256
    assert ("cleanup_orphans", "") in fake.calls


@pytest.mark.parametrize(
    ("mode", "status", "error_code", "has_result"),
    [
        (FakeFailureMode.TIMEOUT, "timed_out", "runtime_execution_timed_out", False),
        (FakeFailureMode.OOM, "failed", "runtime_container_oom", False),
        (FakeFailureMode.CRASH, "failed", "runtime_container_crashed", False),
        (FakeFailureMode.CLEANUP_FAILURE, "failed", "runtime_cleanup_failed", True),
    ],
)
def test_worker_failure_matrix_is_terminal_and_bounded(
    tmp_path: Path,
    mode: FakeFailureMode,
    status: str,
    error_code: str,
    has_result: bool,
) -> None:
    async def scenario() -> tuple[dict[str, Any], bool]:
        repository = FakeRunnerRepository()
        worker = RuntimeRunnerWorker(
            queue=RuntimeRunnerQueue(repository, runner_id="runner-test"),
            result_writer=LocalRuntimeResultWriter(tmp_path),
            executor=ContainerExecutionPipeline(
                DeterministicFakeContainerExecutor(default_failure=mode)
            ),
            settings=runner_settings(tmp_path),
        )
        await worker.run_once()
        result_files = tuple((tmp_path / "runtime/results/dispatch_01").glob("attempt-*.json"))
        return repository.completions[0], bool(result_files)

    completion, result_exists = asyncio.run(scenario())
    assert completion["status"] == status
    assert completion["error_code"] == error_code
    assert result_exists is has_result


class BlockingExecutionService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.aborted = 0
        self.cleaned = 0
        self.orphan_runs = 0

    async def execute(self, work: RuntimeDispatchWorkItem) -> RuntimeDispatchResult:
        self.started.set()
        await self.release.wait()
        raise AssertionError("blocking executor should be cancelled")

    async def abort(self, work: RuntimeDispatchWorkItem) -> None:
        self.aborted += 1

    async def cleanup_dispatch(self, work: RuntimeDispatchWorkItem) -> None:
        self.cleaned += 1

    async def cleanup_orphans(self) -> int:
        self.orphan_runs += 1
        return 0

    async def close(self) -> None:
        return None


def test_worker_stops_execution_without_completion_after_lease_loss(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRunnerRepository, BlockingExecutionService]:
        repository = FakeRunnerRepository(renew_result=False)
        executor = BlockingExecutionService()
        worker = RuntimeRunnerWorker(
            queue=RuntimeRunnerQueue(repository, runner_id="runner-test"),
            result_writer=LocalRuntimeResultWriter(tmp_path),
            executor=executor,
            settings=runner_settings(tmp_path),
            heartbeat_interval_seconds=0.01,
        )
        await worker.run_once()
        return repository, executor

    repository, executor = asyncio.run(scenario())
    assert repository.renew_count == 1
    assert repository.completions == []
    assert executor.aborted == 1
    assert executor.cleaned == 1


def test_worker_sigterm_path_stops_claiming_and_cancels_after_grace(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRunnerRepository, BlockingExecutionService]:
        repository = FakeRunnerRepository()
        executor = BlockingExecutionService()
        worker = RuntimeRunnerWorker(
            queue=RuntimeRunnerQueue(repository, runner_id="runner-test"),
            result_writer=LocalRuntimeResultWriter(tmp_path),
            executor=executor,
            settings=runner_settings(tmp_path, shutdown_grace_seconds=0),
        )
        running = asyncio.create_task(worker.run_forever())
        await asyncio.wait_for(executor.started.wait(), timeout=1)
        worker.request_stop()
        await asyncio.wait_for(running, timeout=1)
        return repository, executor

    repository, executor = asyncio.run(scenario())
    assert repository.claim_count == 1
    assert repository.completions[0]["status"] == "cancelled"
    assert repository.completions[0]["error_code"] == "runtime_runner_shutdown"
    assert executor.aborted == 1
    assert executor.cleaned == 1


def test_runtime_runner_import_does_not_load_market_api_or_store() -> None:
    script = """
import json
import sys
import app.runtime_runner.worker
blocked = sorted(name for name in sys.modules if name in {'app.main', 'app.store', 'app.config'})
print(json.dumps(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONPATH": "."},
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []


def test_runtime_runner_check_config_command_is_secret_free(tmp_path: Path) -> None:
    database_url = "postgresql://runner:do-not-print@db/market"
    completed = subprocess.run(
        [sys.executable, "-m", "app.runtime_runner", "--check-config"],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "PYTHONPATH": ".",
            "RUNTIME_RUNNER_DATABASE_URL": database_url,
            "RUNTIME_RUNNER_ID": "runner-command-test",
            "RUNTIME_RUNNER_RESULT_ROOT": str(tmp_path),
            "RUNTIME_RUNNER_EXECUTOR_BACKEND": "rootless-docker",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["configured"] is True
    assert payload["runner_id"] == "runner-command-test"
    assert "do-not-print" not in completed.stdout


def test_runner_and_market_process_import_boundaries_are_static() -> None:
    api_root = Path(__file__).parents[1] / "app"
    forbidden_runner_modules = {"app.main", "app.store", "app.config"}
    for path in (api_root / "runtime_runner").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not forbidden_runner_modules.intersection(imported), path
        assert not {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.level > 1
            and node.module in {"main", "store", "config"}
        }, path

    forbidden_market_imports = {
        "runtime_runner.worker",
        "runtime_runner.container_executor",
        "runtime_runner.main",
    }
    for path in (api_root / "main.py", api_root / "artifacts" / "worker.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(module in source for module in forbidden_market_imports), path
        assert "docker.sock" not in source
        assert "DOCKER_HOST" not in source
