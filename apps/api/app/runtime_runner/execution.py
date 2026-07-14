from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from ..artifacts.runner_contract import RuntimeDispatchResult
from .queue import RuntimeDispatchWorkItem

_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class RuntimeExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("invalid_runtime_execution_error_code")
        normalized_message = " ".join(str(message or "").split())[:500]
        super().__init__(normalized_message)
        self.code = code


@runtime_checkable
class RuntimeExecutionService(Protocol):
    async def execute(self, work: RuntimeDispatchWorkItem) -> RuntimeDispatchResult: ...

    async def abort(self, work: RuntimeDispatchWorkItem) -> None: ...

    async def cleanup_dispatch(self, work: RuntimeDispatchWorkItem) -> None: ...

    async def cleanup_orphans(self) -> int: ...

    async def close(self) -> None: ...


def build_runtime_execution_service(backend: str) -> RuntimeExecutionService:
    raise RuntimeExecutionError(
        "runtime_executor_unavailable",
        f"Runtime executor backend '{backend}' is not available in this build",
    )
