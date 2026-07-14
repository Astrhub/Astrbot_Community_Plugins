from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .probe.command import redact_probe_text

_UNIX_DOCKER_HOST = re.compile(r"^unix:///[A-Za-z0-9_./-]{1,240}$")


@dataclass(frozen=True, slots=True)
class DockerCommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@runtime_checkable
class DockerCommandClient(Protocol):
    async def execute(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> DockerCommandResult: ...

    async def close(self) -> None: ...


class DockerCli:
    def __init__(self, *, binary: str = "docker", host: str = "") -> None:
        if not _valid_binary(binary):
            raise ValueError("runtime_docker_binary_invalid")
        if host and not _UNIX_DOCKER_HOST.fullmatch(host):
            raise ValueError("runtime_docker_host_invalid")
        self.binary = binary
        self.host = host

    async def execute(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> DockerCommandResult:
        arguments = _validate_argv(argv)
        if len(stdin) > 64 * 1024 or timeout_seconds <= 0:
            raise ValueError("runtime_docker_command_limits_invalid")
        if max_output_bytes < 1024 or max_output_bytes > 16 * 1024 * 1024:
            raise ValueError("runtime_docker_command_limits_invalid")
        command = [self.binary]
        if self.host:
            command.extend(("--host", self.host))
        command.extend(arguments)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_docker_environment(),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, max_output_bytes))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, max_output_bytes))
        if stdin:
            assert process.stdin is not None
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
            stdout_task,
            stderr_task,
        )
        return DockerCommandResult(
            returncode=process.returncode if process.returncode is not None else 255,
            stdout=redact_probe_text(stdout, maximum=max_output_bytes),
            stderr=redact_probe_text(stderr, maximum=max_output_bytes),
            duration_ms=min(int((time.monotonic() - started) * 1000), 3_600_000),
            timed_out=timed_out,
            truncated=stdout_truncated or stderr_truncated,
        )

    async def close(self) -> None:
        return None


async def _read_bounded(stream: asyncio.StreamReader, maximum: int) -> tuple[str, bool]:
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


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    arguments = tuple(str(item) for item in argv)
    if not arguments or len(arguments) > 256:
        raise ValueError("runtime_docker_argv_invalid")
    if any(not item or "\x00" in item or len(item) > 4096 for item in arguments):
        raise ValueError("runtime_docker_argv_invalid")
    return arguments


def _valid_binary(value: str) -> bool:
    if value == "docker":
        return True
    return bool(value.startswith("/") and re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", value))


def _docker_environment() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
