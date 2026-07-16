from __future__ import annotations

import asyncio

import pytest

from app.artifacts.runtime_findings import normalize_runtime_findings
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
        output = await ContainerExecutionPipeline(fake).execute(work_item())
        return output, fake

    output, fake = asyncio.run(scenario())
    result = output.result

    assert result.dispatch_id == "dispatch_01"
    assert result.target.resolved_python_version == "3.12.0"
    assert result.install.astrbot_version == "4.26.5"
    assert result.smoke.metadata.name == "astrbot_plugin_demo"
    assert result.cleanup.status.value == "passed"
    assert output.private_objects[0].key == result.install.sbom_key
    assert [name for name, _ in fake.calls] == [
        "prepare",
        "install",
        "smoke",
        "attest",
        "cleanup",
    ]
    assert not fake.resources


@pytest.mark.parametrize(
    ("mode", "install_status", "smoke_code", "finding_code"),
    [
        (FakeFailureMode.NONE, "passed", "", ""),
        (
            FakeFailureMode.DEPENDENCY_CONFLICT,
            "failed",
            "",
            "astrbot_core_dependency_conflict",
        ),
        (FakeFailureMode.IMPORT_FAILURE, "passed", "plugin_import_failed", "plugin_import_failed"),
        (
            FakeFailureMode.INITIALIZE_FAILURE,
            "passed",
            "plugin_initialize_failed",
            "plugin_initialize_failed",
        ),
        (
            FakeFailureMode.HANDLER_FAILURE,
            "passed",
            "handler_registration_failed",
            "handler_registration_failed",
        ),
        (
            FakeFailureMode.TOOL_FAILURE,
            "passed",
            "llm_tool_registration_failed",
            "llm_tool_registration_failed",
        ),
        (
            FakeFailureMode.TERMINATION_FAILURE,
            "passed",
            "plugin_terminate_failed",
            "plugin_terminate_failed",
        ),
    ],
)
def test_fake_executor_plugin_fixture_matrix_produces_valid_structured_findings(
    mode: FakeFailureMode,
    install_status: str,
    smoke_code: str,
    finding_code: str,
) -> None:
    output = asyncio.run(
        ContainerExecutionPipeline(
            DeterministicFakeContainerExecutor(default_failure=mode)
        ).execute(work_item())
    )
    result = output.result
    findings = normalize_runtime_findings(
        result,
        tool_name="deterministic-fake",
        tool_version="1",
    )

    assert result.install.status.value == install_status
    assert result.smoke.error_code == smoke_code
    assert result.cleanup.status.value == "passed"
    if finding_code:
        assert finding_code in {finding.rule_id for finding in findings}
    else:
        assert findings == ()


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
    async def scenario() -> tuple[BaseException, set[str], list[tuple[str, str]]]:
        fake = DeterministicFakeContainerExecutor(default_failure=mode)
        pipeline = ContainerExecutionPipeline(fake)
        with pytest.raises(expected_exception) as raised:
            await pipeline.execute(work_item())
        await pipeline.cleanup_dispatch(work_item())
        return raised.value, fake.resources, fake.calls

    error, resources, calls = asyncio.run(scenario())
    assert getattr(error, "code", "") == expected_code
    assert not resources
    assert ("cleanup", "dispatch_01") in calls


def test_cleanup_failure_remains_a_failed_gate_until_orphan_cleanup() -> None:
    async def scenario() -> tuple[object, int, set[str]]:
        fake = DeterministicFakeContainerExecutor(default_failure=FakeFailureMode.CLEANUP_FAILURE)
        pipeline = ContainerExecutionPipeline(fake)
        output = await pipeline.execute(work_item())
        resources_before = set(fake.resources)
        removed = await pipeline.cleanup_orphans()
        return output.result, removed, resources_before

    result, removed, resources_before = asyncio.run(scenario())

    assert result.cleanup.status.value == "failed"
    assert result.cleanup.error_code == "runtime_cleanup_failed"
    assert resources_before == {"fake-runtime-dispatch_01"}
    assert removed == 1


def test_pipeline_cleanup_is_idempotent_after_crash() -> None:
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

    assert calls.count(("cleanup", "dispatch_01")) == 1
    assert ("abort", "dispatch_01") not in calls
    assert not resources


def test_pipeline_cancellation_still_runs_cleanup() -> None:
    class BlockingSmokeExecutor(DeterministicFakeContainerExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def smoke(self, prepared, work):
            self._record("smoke", work)
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled smoke should not return")

    async def scenario() -> tuple[list[tuple[str, str]], set[str]]:
        fake = BlockingSmokeExecutor()
        pipeline = ContainerExecutionPipeline(fake)
        running = asyncio.create_task(pipeline.execute(work_item()))
        await asyncio.wait_for(fake.started.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return fake.calls, fake.resources

    calls, resources = asyncio.run(scenario())
    assert ("cleanup", "dispatch_01") in calls
    assert not resources
