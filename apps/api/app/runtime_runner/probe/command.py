from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_REDACTIONS = (
    re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
    re.compile(
        r"([?&](?:access_token|api_key|key|password|secret|signature|token)=)[^&#\s]+",
        re.IGNORECASE,
    ),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@runtime_checkable
class CommandRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        command = _validate_argv(argv)
        working_directory = cwd.resolve(strict=True)
        if not working_directory.is_dir():
            raise ValueError("probe_command_cwd_not_directory")
        if timeout_seconds <= 0 or max_output_bytes < 1024 or max_output_bytes > 16_777_216:
            raise ValueError("probe_command_limits_invalid")
        process_env = _minimal_environment(env)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=working_directory,
            env=process_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        output_task = asyncio.create_task(_read_bounded(process.stdout, max_output_bytes))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        output, truncated = await output_task
        duration_ms = min(int((time.monotonic() - started) * 1000), 3_600_000)
        return CommandResult(
            returncode=process.returncode if process.returncode is not None else 255,
            output=redact_probe_text(output),
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=truncated,
        )


async def _read_bounded(
    stream: asyncio.StreamReader,
    maximum: int,
) -> tuple[str, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while chunk := await stream.read(65_536):
        remaining = maximum - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def redact_probe_text(value: str, *, maximum: int = 16_777_216) -> str:
    normalized = value.replace("\x00", "")
    for pattern in _REDACTIONS:
        normalized = pattern.sub(r"\1[REDACTED]", normalized)
    return normalized[:maximum]


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    command = tuple(str(item) for item in argv)
    if not command or len(command) > 128:
        raise ValueError("probe_command_argv_invalid")
    if any(not item or "\x00" in item or len(item) > 4096 for item in command):
        raise ValueError("probe_command_argv_invalid")
    return command


def _minimal_environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    environment = {
        "HOME": "/tmp/runtime-probe-home",
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for name, value in (extra or {}).items():
        if not _ENV_NAME.fullmatch(name) or "\x00" in value:
            raise ValueError("probe_command_environment_invalid")
        environment[name] = value
    return environment
