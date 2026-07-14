from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from app.runtime_runner.probe.command import CommandResult, SubprocessCommandRunner
from app.runtime_runner.probe.install import InstallSandbox
from tests.runtime_runner_helpers import runtime_request


class FakeCommandRunner:
    def __init__(self, results: Sequence[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        return self.results.pop(0)


def command(
    output: str = "",
    *,
    returncode: int = 0,
    timed_out: bool = False,
    truncated: bool = False,
) -> CommandResult:
    return CommandResult(
        returncode=returncode,
        output=output,
        duration_ms=1,
        timed_out=timed_out,
        truncated=truncated,
    )


def package_list(*pairs: tuple[str, str]) -> str:
    return json.dumps([{"name": name, "version": version} for name, version in pairs])


def prepare_paths(
    tmp_path: Path, requirements: str | None = "demo-lib==1.0.0\n"
) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    plugin = tmp_path / "plugin"
    workspace.mkdir()
    plugin.mkdir()
    if requirements is not None:
        (plugin / "requirements.txt").write_text(requirements, encoding="utf-8")
    return workspace, plugin


def test_install_probe_uses_exact_argv_and_emits_dependency_snapshots_and_sbom(
    tmp_path: Path,
) -> None:
    before = package_list(("AstrBot", "4.26.5"), ("fastapi", "1.0.0"))
    after = package_list(
        ("AstrBot", "4.26.5"),
        ("demo-lib", "1.0.0"),
        ("fastapi", "1.1.0"),
    )
    runner = FakeCommandRunner(
        [command(), command(), command(before), command(), command(), command(after)]
    )
    workspace, plugin = prepare_paths(tmp_path)

    output = asyncio.run(
        InstallSandbox(runner, runtime_python_version="3.12.10").execute(
            runtime_request(),
            workspace=workspace,
            plugin_root=plugin,
        )
    )

    assert output.result.status.value == "passed"
    assert output.result.astrbot_version == "4.26.5"
    assert output.result.core_before_sha256 != output.result.core_after_sha256
    assert output.requirements_sha256
    assert output.sbom_path and output.sbom_path.is_file()
    assert output.sbom_sha256
    sbom = json.loads(output.sbom_path.read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert runner.calls[1][-1] == "AstrBot==4.26.5"
    assert runner.calls[3][-2:] == ("--requirement", str(plugin / "requirements.txt"))
    assert all("shell" not in call for argv in runner.calls for call in argv)


@pytest.mark.parametrize(
    "requirements",
    [
        "demo @ https://user:secret@example.test/demo.whl\n",
        "git+https://github.com/example/demo.git\n",
        "--extra-index-url https://packages.example.test/simple\n",
        "-e ../demo\n",
    ],
)
def test_install_probe_rejects_untrusted_requirement_sources_before_commands(
    tmp_path: Path,
    requirements: str,
) -> None:
    runner = FakeCommandRunner([])
    workspace, plugin = prepare_paths(tmp_path, requirements)

    output = asyncio.run(
        InstallSandbox(runner, runtime_python_version="3.12.1").execute(
            runtime_request(),
            workspace=workspace,
            plugin_root=plugin,
        )
    )

    assert output.result.status.value == "failed"
    assert output.result.error_code == "requirements_invalid"
    assert runner.calls == []
    assert "secret" not in output.result.message


def test_install_probe_detects_core_dependency_downgrade(tmp_path: Path) -> None:
    before = package_list(("AstrBot", "4.26.5"), ("fastapi", "1.0.0"))
    after = package_list(("AstrBot", "4.26.5"), ("fastapi", "0.9.0"))
    runner = FakeCommandRunner(
        [command(), command(), command(before), command(), command(), command(after)]
    )
    workspace, plugin = prepare_paths(tmp_path)

    output = asyncio.run(
        InstallSandbox(runner, runtime_python_version="3.12.0").execute(
            runtime_request(),
            workspace=workspace,
            plugin_root=plugin,
        )
    )

    assert output.result.status.value == "failed"
    assert output.result.error_code == "astrbot_core_dependency_conflict"
    assert output.result.conflicts[0].package == "fastapi"
    assert output.result.conflicts[0].installed_version == "0.9.0"


def test_install_probe_maps_pip_check_failure_and_redacts_logs(tmp_path: Path) -> None:
    packages = package_list(("AstrBot", "4.26.5"), ("fastapi", "1.0.0"))
    conflict = "demo 1.0 has requirement fastapi>=2, but you have fastapi 1.0.0."
    secret_log = "https://user:password@example.test/simple?token=abcdef123456"
    runner = FakeCommandRunner(
        [
            command(),
            command(),
            command(packages),
            command(secret_log),
            command(conflict, returncode=1),
            command(packages),
        ]
    )
    workspace, plugin = prepare_paths(tmp_path)

    output = asyncio.run(
        InstallSandbox(runner, runtime_python_version="3.12.2").execute(
            runtime_request(),
            workspace=workspace,
            plugin_root=plugin,
        )
    )

    assert output.result.status.value == "failed"
    assert output.result.error_code == "dependency_conflict"
    assert output.result.pip_check.status.value == "failed"
    assert output.result.conflicts[0].required_by == "demo"
    assert "password" not in output.logs
    assert "abcdef123456" not in output.logs
    assert "[REDACTED]" in output.logs


def test_install_probe_maps_command_timeout_without_running_later_steps(tmp_path: Path) -> None:
    runner = FakeCommandRunner([command(returncode=-9, timed_out=True)])
    workspace, plugin = prepare_paths(tmp_path, None)

    output = asyncio.run(
        InstallSandbox(runner, runtime_python_version="3.12.0").execute(
            runtime_request(),
            workspace=workspace,
            plugin_root=plugin,
        )
    )

    assert output.result.status.value == "timed_out"
    assert output.result.error_code == "runtime_command_timed_out"
    assert len(runner.calls) == 1


def test_install_probe_rejects_container_python_mismatch_before_commands(tmp_path: Path) -> None:
    runner = FakeCommandRunner([])
    workspace, plugin = prepare_paths(tmp_path, None)

    output = asyncio.run(
        InstallSandbox(runner, runtime_python_version="3.11.9").execute(
            runtime_request(),
            workspace=workspace,
            plugin_root=plugin,
        )
    )

    assert output.result.error_code == "runtime_python_version_mismatch"
    assert runner.calls == []


def test_subprocess_runner_uses_minimal_environment_and_enforces_timeout(tmp_path: Path) -> None:
    async def scenario() -> tuple[CommandResult, CommandResult]:
        runner = SubprocessCommandRunner()
        os.environ["RUNTIME_PROBE_SHOULD_NOT_LEAK"] = "secret"
        visible = await runner.run(
            [
                sys.executable,
                "-c",
                "import os; print(os.getenv('RUNTIME_PROBE_SHOULD_NOT_LEAK', 'absent'))",
            ],
            cwd=tmp_path,
            env=None,
            timeout_seconds=1,
            max_output_bytes=1024,
        )
        timed_out = await runner.run(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cwd=tmp_path,
            env=None,
            timeout_seconds=0.01,
            max_output_bytes=1024,
        )
        return visible, timed_out

    visible, timed_out = asyncio.run(scenario())
    assert visible.output.strip() == "absent"
    assert timed_out.timed_out
    assert not timed_out.succeeded
