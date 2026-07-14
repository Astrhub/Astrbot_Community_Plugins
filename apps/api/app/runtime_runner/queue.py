from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from ..artifacts.runner_contract import (
    RuntimeDispatchRequest,
    RuntimeDispatchResult,
    runtime_result_error_code,
    runtime_result_object_key,
    runtime_result_passed,
    validate_runtime_result_identity,
)

_RUNNER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class RunnerTerminalStatus(StrEnum):
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class RuntimeRunnerQueueError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeDispatchWorkItem:
    dispatch_id: str
    run_id: str
    attempt: int
    request_sha256: str
    request: RuntimeDispatchRequest


@runtime_checkable
class RuntimeRunnerRepository(Protocol):
    async def claim_runtime_dispatches(
        self,
        runner_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]: ...

    async def renew_runtime_dispatch_lease(
        self,
        dispatch_id: str,
        runner_id: str,
        lease_seconds: int,
    ) -> bool: ...

    async def complete_runtime_dispatch(
        self,
        dispatch_id: str,
        runner_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None: ...


class RuntimeRunnerQueue:
    """Runner-facing queue with no create, collect, artifact, or review mutation methods."""

    def __init__(self, repository: RuntimeRunnerRepository, *, runner_id: str) -> None:
        if not _RUNNER_ID.fullmatch(runner_id):
            raise ValueError("invalid_runtime_runner_id")
        self.repository = repository
        self.runner_id = runner_id

    async def claim(self, *, limit: int, lease_seconds: int) -> tuple[RuntimeDispatchWorkItem, ...]:
        if limit < 1 or limit > 32 or lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("invalid_runtime_claim_limits")
        claimed = await self.repository.claim_runtime_dispatches(
            self.runner_id,
            limit,
            lease_seconds,
        )
        work: list[RuntimeDispatchWorkItem] = []
        for dispatch in claimed:
            try:
                request = RuntimeDispatchRequest.model_validate(dispatch.get("request") or {})
                request_sha256 = request.canonical_sha256()
                if (
                    request.dispatch_id != str(dispatch["id"])
                    or request.artifact_id != str(dispatch["artifact_id"])
                    or request_sha256 != str(dispatch["request_sha256"])
                ):
                    raise ValueError("runtime request identity mismatch")
            except (KeyError, ValidationError, ValueError):
                await self.repository.complete_runtime_dispatch(
                    str(dispatch["id"]),
                    self.runner_id,
                    {
                        "status": RunnerTerminalStatus.FAILED.value,
                        "error_code": "runtime_request_invalid",
                        "error_message": "Runtime dispatch request failed contract validation",
                    },
                )
                continue
            work.append(
                RuntimeDispatchWorkItem(
                    dispatch_id=str(dispatch["id"]),
                    run_id=str(dispatch["run_id"]),
                    attempt=int(dispatch["attempts"]),
                    request_sha256=request_sha256,
                    request=request,
                )
            )
        return tuple(work)

    async def renew(self, work: RuntimeDispatchWorkItem, *, lease_seconds: int) -> bool:
        if lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("invalid_runtime_lease_seconds")
        return await self.repository.renew_runtime_dispatch_lease(
            work.dispatch_id,
            self.runner_id,
            lease_seconds,
        )

    async def complete_result(
        self,
        work: RuntimeDispatchWorkItem,
        result: RuntimeDispatchResult | Mapping[str, Any],
    ) -> dict[str, Any]:
        parsed = (
            result
            if isinstance(result, RuntimeDispatchResult)
            else RuntimeDispatchResult.model_validate(dict(result))
        )
        validate_runtime_result_identity(work.request, parsed)
        passed = runtime_result_passed(parsed)
        error_code = "" if passed else runtime_result_error_code(parsed)
        result_key = runtime_result_object_key(
            work.request,
            work.attempt,
            parsed.result_sha256,
        )
        completed = await self.repository.complete_runtime_dispatch(
            work.dispatch_id,
            self.runner_id,
            {
                "status": "succeeded" if passed else RunnerTerminalStatus.FAILED.value,
                "result_key": result_key,
                "result_sha256": parsed.result_sha256,
                "image_digest": parsed.target.image_digest,
                "error_code": error_code,
                "error_message": "" if passed else "Runtime validation reported a failed gate",
            },
        )
        if completed is None:
            raise RuntimeRunnerQueueError(
                "runtime_dispatch_lease_lost",
                "Runtime dispatch lease is no longer owned by this runner",
            )
        return completed

    async def complete_failure(
        self,
        work: RuntimeDispatchWorkItem,
        *,
        status: RunnerTerminalStatus,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        code, message = _bounded_error(error_code, error_message)
        completed = await self.repository.complete_runtime_dispatch(
            work.dispatch_id,
            self.runner_id,
            {
                "status": status.value,
                "error_code": code,
                "error_message": message,
            },
        )
        if completed is None:
            raise RuntimeRunnerQueueError(
                "runtime_dispatch_lease_lost",
                "Runtime dispatch lease is no longer owned by this runner",
            )
        return completed


def _bounded_error(error_code: str, error_message: str) -> tuple[str, str]:
    code = str(error_code or "").strip()
    message = " ".join(str(error_message or "").split())[:500]
    if not _ERROR_CODE.fullmatch(code):
        raise ValueError("invalid_runtime_error_code")
    return code, message
