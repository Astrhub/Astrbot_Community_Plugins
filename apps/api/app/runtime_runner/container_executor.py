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
        if self._mode(work) == FakeFailureMode.CRASH:
            raise RuntimeExecutionError(
                "runtime_container_crashed",
                "Runtime smoke container exited unexpectedly",
            )
        passed = {"status": "passed", "duration_ms": 1}
        return SmokeResult.model_validate(
            {
                "status": "passed",
                "duration_ms": 10,
                "metadata": {
                    **passed,
                    "name": work.request.expected_plugin.name,
                    "version": work.request.expected_plugin.version,
                    "author": "Runtime fixture",
                },
                "import_probe": passed,
                "initialize": passed,
                "startup": {**passed, "ready_ms": 1},
                "handlers": {**passed, "count": 0, "names": []},
                "llm_tools": {**passed, "count": 0, "names": []},
                "failed_plugin": {"present": False},
                "termination": passed,
                "violations": [],
            }
        )

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
