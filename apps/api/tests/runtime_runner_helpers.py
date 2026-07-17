from __future__ import annotations

from typing import Any

from app.artifacts.runner_contract import RuntimeDispatchRequest
from app.runtime_runner.queue import RuntimeDispatchWorkItem


def runtime_request(
    *,
    dispatch_id: str = "dispatch_01",
    timeout_seconds: int = 10,
) -> RuntimeDispatchRequest:
    return RuntimeDispatchRequest.model_validate(
        {
            "schema_version": "1",
            "dispatch_id": dispatch_id,
            "artifact_id": "artifact_01",
            "artifact_sha256": "a" * 64,
            "artifact_size_bytes": 4096,
            "quarantine_key": "artifacts/artifact_01/source.zip",
            "policy_version_id": "policy_01",
            "expected_plugin": {
                "name": "astrbot_plugin_demo",
                "version": "v1.2.3",
                "source_repo": "https://github.com/alice/astrbot_plugin_demo",
                "source_commit_sha": "b" * 40,
            },
            "target": {
                "astrbot_version": "4.26.6",
                "python_version": "3.12",
                "image_digest": f"sha256:{'c' * 64}",
                "platform": "linux/amd64",
                "astrbot_commit": "5d10e0d428b41308cc63215db00359c61ee17195",
            },
            "limits": {
                "cpu": 1,
                "memory_mb": 768,
                "pids": 128,
                "timeout_seconds": timeout_seconds,
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


def work_item(*, dispatch_id: str = "dispatch_01") -> RuntimeDispatchWorkItem:
    request = runtime_request(dispatch_id=dispatch_id)
    return RuntimeDispatchWorkItem(
        dispatch_id=dispatch_id,
        run_id="run_01",
        attempt=1,
        request_sha256=request.canonical_sha256(),
        request=request,
    )


class FakeRunnerRepository:
    def __init__(self, *, dispatch_id: str = "dispatch_01", renew_result: bool = True) -> None:
        request = runtime_request(dispatch_id=dispatch_id)
        self.dispatch: dict[str, Any] = {
            "id": dispatch_id,
            "artifact_id": request.artifact_id,
            "run_id": "run_01",
            "attempts": 0,
            "request": request.model_dump(mode="json"),
            "request_sha256": request.canonical_sha256(),
            "status": "queued",
        }
        self.renew_result = renew_result
        self.claim_count = 0
        self.renew_count = 0
        self.completions: list[dict[str, Any]] = []

    async def claim_runtime_dispatches(
        self,
        runner_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        self.claim_count += 1
        if self.dispatch["status"] != "queued" or limit < 1:
            return []
        self.dispatch.update(
            {
                "status": "running",
                "attempts": 1,
                "lease_owner": runner_id,
            }
        )
        return [dict(self.dispatch)]

    async def renew_runtime_dispatch_lease(
        self,
        dispatch_id: str,
        runner_id: str,
        lease_seconds: int,
    ) -> bool:
        self.renew_count += 1
        return self.renew_result and self.dispatch.get("lease_owner") == runner_id

    async def complete_runtime_dispatch(
        self,
        dispatch_id: str,
        runner_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.dispatch.get("lease_owner") != runner_id:
            return None
        saved = {**self.dispatch, **payload, "lease_owner": None}
        self.dispatch = saved
        self.completions.append(dict(payload))
        return dict(saved)
