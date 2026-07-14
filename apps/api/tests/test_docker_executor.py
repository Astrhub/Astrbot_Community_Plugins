from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.artifacts.runner_contract import InstallResult, SmokeResult
from app.runtime_runner.container_executor import ContainerExecutor
from app.runtime_runner.docker_cli import DockerCli, DockerCommandResult
from app.runtime_runner.docker_executor import (
    DockerContainerExecutor,
    DockerExecutorConfiguration,
)
from app.runtime_runner.execution import RuntimeExecutionError
from app.runtime_runner.network_policy import (
    package_index_sha256,
    required_network_labels,
)
from app.runtime_runner.queue import RuntimeDispatchWorkItem
from tests.runtime_runner_helpers import runtime_request


class FakeDockerClient:
    def __init__(
        self,
        *,
        rootless: bool = True,
        network_valid: bool = True,
        unmanaged_peer: bool = False,
    ) -> None:
        self.rootless = rootless
        self.network_valid = network_valid
        self.unmanaged_peer = unmanaged_peer
        self.calls: list[tuple[tuple[str, ...], bytes]] = []
        self.closed = False

    async def execute(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> DockerCommandResult:
        arguments = tuple(argv)
        self.calls.append((arguments, stdin))
        stdout = ""
        if arguments[:2] == ("info", "--format"):
            options = '["name=rootless","name=seccomp,profile=builtin"]'
            stdout = options if self.rootless else '["name=seccomp,profile=builtin"]'
        elif arguments[:2] == ("image", "inspect"):
            stdout = (
                '{"Id":"sha256:'
                + "c" * 64
                + '","RepoDigests":[],"Os":"linux","Architecture":"amd64"}'
            )
        elif arguments[:2] == ("network", "inspect"):
            containers = {
                "proxy": {"Name": "astrbot-runtime-package-proxy"},
            }
            if self.unmanaged_peer:
                containers["foreign"] = {"Name": "foreign-service"}
            stdout = json.dumps(
                {
                    "Name": "astrbot-runtime-install",
                    "Driver": "bridge",
                    "Scope": "local",
                    "Internal": self.network_valid,
                    "Options": {
                        "com.docker.network.bridge.enable_ip_masquerade": "false"
                    },
                    "Labels": required_network_labels(
                        "pypi-only-v1",
                        "https://pypi.org/simple",
                    ),
                    "Containers": containers,
                }
            )
        elif arguments[:3] == (
            "container",
            "inspect",
            "astrbot-runtime-package-proxy",
        ):
            stdout = json.dumps(
                {
                    "astrbot.runtime.package-proxy": "true",
                    "astrbot.runtime.package-index-sha256": package_index_sha256(
                        "https://pypi.org/simple"
                    ),
                }
            )
        elif arguments[:3] == ("container", "inspect", "foreign-service"):
            stdout = "{}"
        elif arguments[0] == "run" and "emit" in arguments:
            phase = arguments[-1]
            stdout = (
                _install_result().model_dump_json()
                if phase == "install"
                else _smoke_result().model_dump_json()
            )
        elif arguments[0] == "run" and "platform.python_version()" in arguments[-1]:
            stdout = "3.12.10\n"
        elif arguments[:3] in {
            ("container", "rm", "--force"),
            ("volume", "rm", "--force"),
        }:
            stdout = f"{arguments[-1]}\n"
        return DockerCommandResult(
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_ms=1,
        )

    async def close(self) -> None:
        self.closed = True


def test_docker_executor_uses_structured_hardened_phase_commands(tmp_path: Path) -> None:
    work = _work_with_artifact(tmp_path)
    client = FakeDockerClient()
    executor = DockerContainerExecutor(
        client,
        DockerExecutorConfiguration(
            image_repository="local-image-id",
            artifact_root=str(tmp_path),
            install_network="astrbot-runtime-install",
            package_index_url="https://pypi.org/simple",
            install_proxy_url="http://astrbot-runtime-package-proxy:3128",
            install_proxy_container="astrbot-runtime-package-proxy",
            allow_rootful_development=True,
        ),
    )

    async def scenario() -> tuple[object, object, object, object, object, object]:
        assert isinstance(executor, ContainerExecutor)
        prepared = await executor.prepare(work)
        install = await executor.install(prepared, work)
        smoke = await executor.smoke(prepared, work)
        attestation = await executor.attest(prepared, work)
        cleanup = await executor.cleanup(prepared, work)
        repeated_cleanup = await executor.cleanup(prepared, work)
        return prepared, install, smoke, attestation, cleanup, repeated_cleanup

    prepared, install, smoke, attestation, cleanup, repeated_cleanup = asyncio.run(scenario())

    assert prepared.resolved_python_version == "3.12.10"
    assert install.status.value == "passed"
    assert smoke.status.value == "passed"
    assert attestation.status.value == "passed"
    assert cleanup.status.value == "passed"
    assert repeated_cleanup.status.value == "passed"
    assert repeated_cleanup.removed_containers == 0
    assert repeated_cleanup.removed_volumes == 0

    commands = [call for call, _ in client.calls]
    copied = next(call for call in commands if call[:2] == ("container", "cp"))
    assert copied[2] == str(tmp_path / "artifacts/artifact_01/source.zip")
    assert copied[3].endswith(":/runtime/input/artifact.zip")
    install_run = next(
        call
        for call in commands
        if call[0] == "run" and call[-1] == "install" and "emit" not in call
    )
    smoke_run = next(
        call
        for call in commands
        if call[0] == "run" and call[-1] == "smoke" and "emit" not in call
    )
    for command in (install_run, smoke_run):
        assert "--interactive" in command
        assert command[command.index("--user") + 1] == "65532:65532"
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert "--read-only" in command
        assert "no-new-privileges=true" in command
        assert "seccomp=builtin" in command
        assert "--pids-limit" in command
        assert "--memory" in command
        assert "--cpus" in command
        assert "--tmpfs" in command
        assert "--privileged" not in command
        assert "docker.sock" not in " ".join(command)
        assert "sh" not in command
        assert "bash" not in command
    assert install_run[install_run.index("--network") + 1] == "astrbot-runtime-install"
    assert smoke_run[smoke_run.index("--network") + 1] == "none"
    assert "PIP_INDEX_URL=https://pypi.org/simple" in install_run
    assert "HTTPS_PROXY=http://astrbot-runtime-package-proxy:3128" in install_run
    assert not any("PROXY=" in argument for argument in smoke_run)
    assert not any("/artifacts" in argument for argument in install_run)
    phase_inputs = [stdin for command, stdin in client.calls if command in {install_run, smoke_run}]
    assert all(0 < len(payload) <= 64 * 1024 for payload in phase_inputs)


def test_docker_executor_fails_closed_on_rootful_engine(tmp_path: Path) -> None:
    work = _work_with_artifact(tmp_path)
    executor = DockerContainerExecutor(
        FakeDockerClient(rootless=False),
        DockerExecutorConfiguration(
            image_repository="registry.example/runtime-probe",
            artifact_root=str(tmp_path),
            install_network="astrbot-runtime-install",
            package_index_url="https://pypi.org/simple",
            install_proxy_url="http://astrbot-runtime-package-proxy:3128",
            install_proxy_container="astrbot-runtime-package-proxy",
        ),
    )

    with pytest.raises(RuntimeExecutionError) as raised:
        asyncio.run(executor.prepare(work))
    assert raised.value.code == "runtime_rootless_required"


@pytest.mark.parametrize(
    "client",
    [
        FakeDockerClient(network_valid=False),
        FakeDockerClient(unmanaged_peer=True),
    ],
)
def test_docker_executor_fails_closed_when_install_network_is_unverified(
    tmp_path: Path,
    client: FakeDockerClient,
) -> None:
    work = _work_with_artifact(tmp_path)
    executor = DockerContainerExecutor(
        client,
        DockerExecutorConfiguration(
            image_repository="registry.example/runtime-probe",
            artifact_root=str(tmp_path),
            install_network="astrbot-runtime-install",
            package_index_url="https://pypi.org/simple",
            install_proxy_url="http://astrbot-runtime-package-proxy:3128",
            install_proxy_container="astrbot-runtime-package-proxy",
        ),
    )

    with pytest.raises(RuntimeExecutionError) as raised:
        asyncio.run(executor.prepare(work))
    assert raised.value.code == "runtime_network_unverified"


def test_docker_cli_uses_bounded_structured_exec() -> None:
    result = asyncio.run(
        DockerCli(binary="/bin/echo").execute(
            ("structured", "argument with spaces"),
            max_output_bytes=1024,
        )
    )
    assert result.stdout == "structured argument with spaces\n"
    assert result.succeeded
    with pytest.raises(ValueError, match="host"):
        DockerCli(host="tcp://127.0.0.1:2375")


class OrphanDockerClient(FakeDockerClient):
    async def execute(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> DockerCommandResult:
        arguments = tuple(argv)
        if arguments[:2] == ("container", "ls"):
            return _docker_result("astrbot-old-container\nastrbot-new-container\n")
        if arguments[:2] == ("volume", "ls"):
            return _docker_result("astrbot-old-volume\nastrbot-new-volume\n")
        if len(arguments) >= 3 and arguments[1] == "inspect" and arguments[0] in {
            "container",
            "volume",
        }:
            created_at = "1000" if "old" in arguments[2] else "9000"
            return _docker_result(
                json.dumps(
                    {
                        "astrbot.runtime.managed": "true",
                        "astrbot.runtime.created-at": created_at,
                    }
                )
            )
        return await super().execute(
            arguments,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


def test_docker_orphan_reconciler_removes_only_expired_managed_resources(
    tmp_path: Path,
) -> None:
    client = OrphanDockerClient()
    executor = DockerContainerExecutor(
        client,
        DockerExecutorConfiguration(
            image_repository="registry.example/runtime-probe",
            artifact_root=str(tmp_path),
            install_network="astrbot-runtime-install",
            package_index_url="https://pypi.org/simple",
            install_proxy_url="http://astrbot-runtime-package-proxy:3128",
            install_proxy_container="astrbot-runtime-package-proxy",
            orphan_ttl_seconds=3600,
        ),
        clock=lambda: 10_000,
    )

    removed = asyncio.run(executor.cleanup_orphans())
    commands = [command for command, _ in client.calls]

    assert removed == 2
    assert ("container", "rm", "--force", "astrbot-old-container") in commands
    assert ("volume", "rm", "--force", "astrbot-old-volume") in commands
    assert not any("astrbot-new" in " ".join(command) and "rm" in command for command in commands)


def _work_with_artifact(root: Path) -> RuntimeDispatchWorkItem:
    path = root / "artifacts/artifact_01/source.zip"
    path.parent.mkdir(parents=True)
    payload = b"safe runtime artifact fixture"
    path.write_bytes(payload)
    request = runtime_request().model_copy(
        update={
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "artifact_size_bytes": len(payload),
        }
    )
    return RuntimeDispatchWorkItem(
        dispatch_id=request.dispatch_id,
        run_id="run_01",
        attempt=1,
        request_sha256=request.canonical_sha256(),
        request=request,
    )


def _install_result() -> InstallResult:
    digest = hashlib.sha256(b"runtime-dependencies").hexdigest()
    return InstallResult.model_validate(
        {
            "status": "passed",
            "astrbot_version": "4.26.5",
            "pip_check": {"status": "passed"},
            "packages": [{"name": "AstrBot", "version": "4.26.5"}],
            "conflicts": [],
            "core_before_sha256": digest,
            "core_after_sha256": digest,
        }
    )


def _smoke_result() -> SmokeResult:
    passed = {"status": "passed"}
    return SmokeResult.model_validate(
        {
            "status": "passed",
            "metadata": {
                **passed,
                "name": "astrbot_plugin_demo",
                "version": "v1.2.3",
                "author": "Runtime Fixture",
            },
            "import_probe": passed,
            "instance": passed,
            "initialize": passed,
            "startup": {**passed, "ready_ms": 1},
            "handlers": {**passed, "count": 1, "names": ["runtime_fixture"]},
            "hooks": {**passed, "count": 1, "names": ["on_loaded"]},
            "llm_tools": {**passed, "count": 1, "names": ["runtime_fixture_tool"]},
            "failed_plugin": {"present": False},
            "termination": passed,
            "violations": [],
        }
    )


def _docker_result(stdout: str = "") -> DockerCommandResult:
    return DockerCommandResult(
        returncode=0,
        stdout=stdout,
        stderr="",
        duration_ms=1,
    )
