from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..artifacts.runner_contract import (
    CleanupResult,
    InstallResult,
    NetworkAttestation,
    RuntimeDispatchResult,
    SmokeResult,
    build_runtime_dispatch_result,
)
from .execution import RuntimeExecutionError, RuntimeExecutionService
from .queue import RuntimeDispatchWorkItem


@dataclass(frozen=True, slots=True)
class PreparedRuntime:
    dispatch_id: str
    resource_id: str
    resolved_python_version: str


@runtime_checkable
class ContainerExecutor(Protocol):
    async def prepare(self, work: RuntimeDispatchWorkItem) -> PreparedRuntime: ...

    async def install(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> InstallResult: ...

    async def smoke(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> SmokeResult: ...

    async def attest(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> NetworkAttestation: ...

    async def cleanup(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> CleanupResult: ...

    async def abort(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> None: ...

    async def cleanup_orphans(self) -> int: ...

    async def close(self) -> None: ...


class ContainerExecutionPipeline(RuntimeExecutionService):
    """把分阶段容器操作收敛为 runner 使用的单次执行接口。"""

    def __init__(self, executor: ContainerExecutor) -> None:
        self.executor = executor
        self._prepared: dict[str, PreparedRuntime] = {}

    async def execute(self, work: RuntimeDispatchWorkItem) -> RuntimeDispatchResult:
        prepared = await self.executor.prepare(work)
        if prepared.dispatch_id != work.dispatch_id:
            raise RuntimeExecutionError(
                "runtime_prepare_identity_mismatch",
                "Prepared runtime does not match the claimed dispatch",
            )
        self._prepared[work.dispatch_id] = prepared
        install = await self.executor.install(prepared, work)
        smoke = await self.executor.smoke(prepared, work)
        attestation = await self.executor.attest(prepared, work)
        cleanup = await self.executor.cleanup(prepared, work)
        if cleanup.status.value == "passed":
            self._prepared.pop(work.dispatch_id, None)
        return build_runtime_dispatch_result(
            {
                "schema_version": work.request.schema_version,
                "dispatch_id": work.dispatch_id,
                "artifact_sha256": work.request.artifact_sha256,
                "target": {
                    **work.request.target.model_dump(mode="json"),
                    "resolved_python_version": prepared.resolved_python_version,
                },
                "install": install.model_dump(mode="json"),
                "smoke": smoke.model_dump(mode="json"),
                "network_attestation": attestation.model_dump(mode="json"),
                "cleanup": cleanup.model_dump(mode="json"),
            }
        )

    async def abort(self, work: RuntimeDispatchWorkItem) -> None:
        prepared = self._prepared.get(work.dispatch_id)
        if prepared is not None:
            await self.executor.abort(prepared, work)

    async def cleanup_dispatch(self, work: RuntimeDispatchWorkItem) -> None:
        prepared = self._prepared.get(work.dispatch_id)
        if prepared is None:
            return
        cleanup = await self.executor.cleanup(prepared, work)
        if cleanup.status.value == "passed":
            self._prepared.pop(work.dispatch_id, None)

    async def cleanup_orphans(self) -> int:
        return await self.executor.cleanup_orphans()

    async def close(self) -> None:
        await self.executor.close()


class FakeFailureMode(StrEnum):
    NONE = "none"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    IMPORT_FAILURE = "import_failure"
    INITIALIZE_FAILURE = "initialize_failure"
    HANDLER_FAILURE = "handler_failure"
    TOOL_FAILURE = "tool_failure"
    TERMINATION_FAILURE = "termination_failure"
    TIMEOUT = "timeout"
    OOM = "oom"
    CRASH = "crash"
    CLEANUP_FAILURE = "cleanup_failure"


class DeterministicFakeContainerExecutor:
    """仅供 contract 和 worker 测试使用，不接生产构建器。"""

    def __init__(
        self,
        *,
        default_failure: FakeFailureMode = FakeFailureMode.NONE,
        failures: Mapping[str, FakeFailureMode] | None = None,
    ) -> None:
        self.default_failure = default_failure
        self.failures = dict(failures or {})
        self.resources: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def prepare(self, work: RuntimeDispatchWorkItem) -> PreparedRuntime:
        self._record("prepare", work)
        resource_id = f"fake-runtime-{work.dispatch_id}"
        self.resources.add(resource_id)
        python_version = work.request.target.python_version
        if python_version.count(".") == 1:
            python_version = f"{python_version}.0"
        return PreparedRuntime(work.dispatch_id, resource_id, python_version)

    async def install(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> InstallResult:
        self._record("install", work)
        mode = self._mode(work)
        if mode == FakeFailureMode.TIMEOUT:
            raise TimeoutError
        if mode == FakeFailureMode.OOM:
            raise RuntimeExecutionError(
                "runtime_container_oom",
                "Runtime install container exceeded its memory limit",
            )
        snapshot = hashlib.sha256(
            f"{work.request.target.astrbot_version}:{prepared.resolved_python_version}".encode()
        ).hexdigest()
        if mode == FakeFailureMode.DEPENDENCY_CONFLICT:
            changed_snapshot = hashlib.sha256(f"{snapshot}:pydantic:1.10.22".encode()).hexdigest()
            return InstallResult.model_validate(
                {
                    "status": "failed",
                    "duration_ms": 20,
                    "error_code": "dependency_conflict",
                    "message": "Plugin requirements conflict with AstrBot dependencies",
                    "astrbot_version": work.request.target.astrbot_version,
                    "pip_check": {
                        "status": "failed",
                        "duration_ms": 1,
                        "error_code": "dependency_conflict",
                        "message": "AstrBot requires pydantic>=2.7",
                    },
                    "packages": [
                        {
                            "name": "astrbot",
                            "version": work.request.target.astrbot_version,
                            "source": "index",
                        },
                        {"name": "pydantic", "version": "1.10.22", "source": "index"},
                    ],
                    "conflicts": [
                        {
                            "package": "pydantic",
                            "installed_version": "1.10.22",
                            "requirement": ">=2.7",
                            "required_by": "AstrBot",
                        }
                    ],
                    "core_before_sha256": snapshot,
                    "core_after_sha256": changed_snapshot,
                }
            )
        return InstallResult.model_validate(
            {
                "status": "passed",
                "duration_ms": 20,
                "astrbot_version": work.request.target.astrbot_version,
                "pip_check": {"status": "passed", "duration_ms": 1},
                "packages": [
                    {
                        "name": "astrbot",
                        "version": work.request.target.astrbot_version,
                        "source": "index",
                    }
                ],
                "conflicts": [],
                "core_before_sha256": snapshot,
                "core_after_sha256": snapshot,
            }
        )

    async def smoke(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> SmokeResult:
        self._record("smoke", work)
        mode = self._mode(work)
        if mode == FakeFailureMode.CRASH:
            raise RuntimeExecutionError(
                "runtime_container_crashed",
                "Runtime smoke container exited unexpectedly",
            )
        return _fake_smoke_result(work, mode)

    async def attest(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> NetworkAttestation:
        self._record("attest", work)
        return NetworkAttestation.model_validate(
            {
                "status": "passed",
                "backend": "fake-container-v1",
                "install_profile": work.request.install_network_profile,
                "smoke_profile": work.request.smoke_network_profile,
                "install_egress_enforced": True,
                "private_network_blocked": True,
                "metadata_endpoint_blocked": True,
                "smoke_network_disabled": True,
                "violations": [],
            }
        )

    async def cleanup(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> CleanupResult:
        self._record("cleanup", work)
        if self._mode(work) == FakeFailureMode.CLEANUP_FAILURE:
            return CleanupResult.model_validate(
                {
                    "status": "failed",
                    "duration_ms": 1,
                    "error_code": "runtime_cleanup_failed",
                    "message": "Deterministic cleanup failure",
                    "leaked_resources": [prepared.resource_id],
                }
            )
        removed = int(prepared.resource_id in self.resources)
        self.resources.discard(prepared.resource_id)
        return CleanupResult.model_validate(
            {
                "status": "passed",
                "duration_ms": 1,
                "removed_containers": removed,
                "removed_volumes": 0,
                "removed_networks": 0,
                "removed_temp_roots": 0,
                "leaked_resources": [],
            }
        )

    async def abort(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> None:
        self._record("abort", work)

    async def cleanup_orphans(self) -> int:
        removed = len(self.resources)
        self.resources.clear()
        self.calls.append(("cleanup_orphans", ""))
        return removed

    async def close(self) -> None:
        self.closed = True

    def _mode(self, work: RuntimeDispatchWorkItem) -> FakeFailureMode:
        return self.failures.get(work.dispatch_id, self.default_failure)

    def _record(self, action: str, work: RuntimeDispatchWorkItem) -> None:
        self.calls.append((action, work.dispatch_id))


def _fake_smoke_result(
    work: RuntimeDispatchWorkItem,
    mode: FakeFailureMode,
) -> SmokeResult:
    passed = {"status": "passed", "duration_ms": 1}
    skipped = {
        "status": "skipped",
        "duration_ms": 0,
        "error_code": "probe_not_reached",
        "message": "Probe phase was not reached",
    }
    registration_passed = {**passed, "count": 1}
    plugin_name = work.request.expected_plugin.name
    payload = {
        "status": "passed",
        "duration_ms": 10,
        "metadata": {
            **passed,
            "name": plugin_name,
            "version": work.request.expected_plugin.version,
            "author": "Runtime fixture",
        },
        "import_probe": passed,
        "instance": passed,
        "initialize": passed,
        "startup": {**passed, "ready_ms": 1},
        "handlers": {
            **registration_passed,
            "names": [f"data.plugins.{plugin_name}.main_runtime_fixture"],
        },
        "hooks": {
            **registration_passed,
            "names": [f"data.plugins.{plugin_name}.main_on_loaded"],
        },
        "llm_tools": {**registration_passed, "names": ["runtime_fixture_tool"]},
        "failed_plugin": {"present": False},
        "termination": passed,
        "violations": [],
    }
    error_codes = {
        FakeFailureMode.IMPORT_FAILURE: "plugin_import_failed",
        FakeFailureMode.INITIALIZE_FAILURE: "plugin_initialize_failed",
        FakeFailureMode.HANDLER_FAILURE: "handler_registration_failed",
        FakeFailureMode.TOOL_FAILURE: "llm_tool_registration_failed",
        FakeFailureMode.TERMINATION_FAILURE: "plugin_terminate_failed",
    }
    error_code = error_codes.get(mode)
    if error_code is None:
        return SmokeResult.model_validate(payload)

    failed = {
        "status": "failed",
        "duration_ms": 1,
        "error_code": error_code,
        "message": "Deterministic plugin fixture failure",
    }
    failed_registration = {**failed, "count": 0, "names": []}
    skipped_registration = {**skipped, "count": 0, "names": []}
    payload.update(
        {
            "status": "failed",
            "error_code": error_code,
            "message": "Deterministic plugin fixture did not pass",
        }
    )
    if mode == FakeFailureMode.TERMINATION_FAILURE:
        payload["termination"] = failed
        return SmokeResult.model_validate(payload)

    payload.update(
        {
            "metadata": skipped,
            "startup": skipped,
            "hooks": skipped_registration,
            "failed_plugin": {
                "present": True,
                "error_code": error_code,
                "message": "AstrBot reported the deterministic fixture as failed",
            },
            "termination": passed,
        }
    )
    if mode == FakeFailureMode.IMPORT_FAILURE:
        payload.update(
            {
                "import_probe": failed,
                "instance": skipped,
                "initialize": skipped,
                "handlers": skipped_registration,
                "llm_tools": skipped_registration,
            }
        )
    elif mode == FakeFailureMode.INITIALIZE_FAILURE:
        payload.update(
            {
                "initialize": failed,
                "handlers": skipped_registration,
                "llm_tools": skipped_registration,
            }
        )
    elif mode == FakeFailureMode.HANDLER_FAILURE:
        payload.update(
            {
                "import_probe": failed,
                "instance": skipped,
                "initialize": skipped,
                "handlers": failed_registration,
                "llm_tools": skipped_registration,
            }
        )
    else:
        payload.update(
            {
                "import_probe": failed,
                "instance": skipped,
                "initialize": skipped,
                "handlers": skipped_registration,
                "llm_tools": failed_registration,
            }
        )
    return SmokeResult.model_validate(payload)
