from __future__ import annotations

import asyncio

import pytest

from app.runtime_runner.container_executor import (
    ContainerExecutionPipeline,
    ContainerExecutor,
    DeterministicFakeContainerExecutor,
    FakeFailureMode,
)
from app.runtime_runner.execution import RuntimeExecutionError
from tests.runtime_runner_helpers import work_item


def test_container_pipeline_runs_all_phases_and_returns_signed_result() -> None:
    async def scenario() -> tuple[object, DeterministicFakeContainerExecutor]:
        fake = DeterministicFakeContainerExecutor()
        assert isinstance(fake, ContainerExecutor)
        result = await ContainerExecutionPipeline(fake).execute(work_item())
        return result, fake

    result, fake = asyncio.run(scenario())

    assert result.dispatch_id == "dispatch_01"
    assert result.target.resolved_python_version == "3.12.0"
    assert result.install.astrbot_version == "4.26.5"
    assert result.smoke.metadata.name == "astrbot_plugin_demo"
    assert result.cleanup.status.value == "passed"
    assert [name for name, _ in fake.calls] == [
        "prepare",
        "install",
        "smoke",
        "attest",
        "cleanup",
    ]
    assert not fake.resources


@pytest.mark.parametrize(
    ("mode", "expected_code", "expected_exception"),
    [
        (FakeFailureMode.TIMEOUT, "", TimeoutError),
        (FakeFailureMode.OOM, "runtime_container_oom", RuntimeExecutionError),
        (FakeFailureMode.CRASH, "runtime_container_crashed", RuntimeExecutionError),
    ],
)
def test_fake_executor_failure_modes_are_deterministic(
    mode: FakeFailureMode,
    expected_code: str,
    expected_exception: type[BaseException],
) -> None:
    async def scenario() -> BaseException:
        pipeline = ContainerExecutionPipeline(
            DeterministicFakeContainerExecutor(default_failure=mode)
        )
        with pytest.raises(expected_exception) as raised:
            await pipeline.execute(work_item())
        await pipeline.cleanup_dispatch(work_item())
        return raised.value

    error = asyncio.run(scenario())
    assert getattr(error, "code", "") == expected_code


def test_cleanup_failure_remains_a_failed_gate_until_orphan_cleanup() -> None:
    async def scenario() -> tuple[object, int, set[str]]:
        fake = DeterministicFakeContainerExecutor(default_failure=FakeFailureMode.CLEANUP_FAILURE)
        pipeline = ContainerExecutionPipeline(fake)
        result = await pipeline.execute(work_item())
        resources_before = set(fake.resources)
        removed = await pipeline.cleanup_orphans()
        return result, removed, resources_before

    result, removed, resources_before = asyncio.run(scenario())

    assert result.cleanup.status.value == "failed"
    assert result.cleanup.error_code == "runtime_cleanup_failed"
    assert resources_before == {"fake-runtime-dispatch_01"}
    assert removed == 1


def test_pipeline_abort_and_cleanup_are_idempotent_after_crash() -> None:
    async def scenario() -> tuple[list[tuple[str, str]], set[str]]:
        fake = DeterministicFakeContainerExecutor(default_failure=FakeFailureMode.CRASH)
        pipeline = ContainerExecutionPipeline(fake)
        work = work_item()
        with pytest.raises(RuntimeExecutionError):
            await pipeline.execute(work)
        await pipeline.abort(work)
        await pipeline.cleanup_dispatch(work)
        await pipeline.cleanup_dispatch(work)
        return fake.calls, fake.resources

    calls, resources = asyncio.run(scenario())

    assert ("abort", "dispatch_01") in calls
    assert not resources
