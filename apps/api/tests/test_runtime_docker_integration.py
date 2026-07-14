from __future__ import annotations

import asyncio
import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from app.artifacts.runtime_findings import normalize_runtime_findings
from app.runtime_runner.container_executor import ContainerExecutionPipeline
from app.runtime_runner.docker_cli import DockerCli
from app.runtime_runner.docker_executor import (
    DockerContainerExecutor,
    DockerExecutorConfiguration,
)
from app.runtime_runner.queue import RuntimeDispatchWorkItem
from tests.runtime_runner_helpers import runtime_request

RUN_REAL_DOCKER = os.environ.get("ASTRBOT_RUNTIME_DOCKER_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_REAL_DOCKER,
    reason="set ASTRBOT_RUNTIME_DOCKER_INTEGRATION=1 for real Docker smoke",
)
FIXTURES = Path(__file__).parent / "fixtures/runtime_plugins"


@pytest.mark.parametrize(
    ("scenario", "install_status", "smoke_code", "finding_code"),
    [
        ("pass", "passed", "", "runtime_rootless_unverified"),
        (
            "dependency_conflict",
            "failed",
            "astrbot_lifecycle_failed",
            "astrbot_core_dependency_conflict",
        ),
        ("import_failure", "passed", "plugin_import_failed", "plugin_import_failed"),
    ],
)
def test_real_docker_runtime_fixture_matrix(
    tmp_path: Path,
    scenario: str,
    install_status: str,
    smoke_code: str,
    finding_code: str,
) -> None:
    async def run() -> tuple[object, tuple[object, ...]]:
        client = DockerCli()
        image_digest = await _image_digest(client)
        work = _fixture_work(tmp_path, scenario, image_digest)
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
        try:
            result = await ContainerExecutionPipeline(executor).execute(work)
            findings = normalize_runtime_findings(
                result,
                tool_name="docker-runtime-integration",
                tool_version="1",
            )
            return result, findings
        finally:
            await executor.close()

    result, findings = asyncio.run(run())

    assert result.install.status.value == install_status, (
        f"install={result.install.error_code}:{result.install.message}; "
        f"pip={result.install.pip_check.error_code}:{result.install.pip_check.message}"
    )
    assert result.smoke.error_code == smoke_code
    assert result.smoke.violations == ()
    assert result.cleanup.status.value == "passed"
    assert result.network_attestation.status.value == "unknown"
    assert finding_code in {finding.rule_id for finding in findings}
    _assert_no_managed_runtime_resources()


def test_real_install_network_allows_pypi_and_denies_other_egress() -> None:
    async def run() -> tuple[int, int, int]:
        client = DockerCli()
        image_digest = await _image_digest(client)
        base = (
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "astrbot-runtime-install",
            "--user",
            "65532:65532",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            "seccomp=builtin",
            "--pids-limit",
            "32",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m,uid=65532,gid=65532,mode=700",
            "--env",
            "HTTPS_PROXY=http://astrbot-runtime-package-proxy:3128",
            image_digest,
            "/usr/local/bin/python",
            "-c",
        )
        allowed = await client.execute(
            (
                *base,
                "import urllib.request; r=urllib.request.urlopen('https://pypi.org/simple', timeout=15); raise SystemExit(0 if r.status < 400 else 1)",
            ),
            timeout_seconds=30,
        )
        denied = await client.execute(
            (
                *base,
                "import urllib.request;\ntry: urllib.request.urlopen('https://example.com', timeout=10)\nexcept Exception: raise SystemExit(0)\nraise SystemExit(1)",
            ),
            timeout_seconds=30,
        )
        metadata = await client.execute(
            (
                *base,
                "import socket;\ntry: socket.create_connection(('169.254.169.254', 80), timeout=1)\nexcept OSError: raise SystemExit(0)\nraise SystemExit(1)",
            ),
            timeout_seconds=15,
        )
        await client.close()
        return allowed.returncode, denied.returncode, metadata.returncode

    assert asyncio.run(run()) == (0, 0, 0)


async def _image_digest(client: DockerCli) -> str:
    inspected = await client.execute(
        ("image", "inspect", "astrbot-runtime-probe:local", "--format", "{{json .Id}}"),
        timeout_seconds=20,
    )
    assert inspected.succeeded
    digest = json.loads(inspected.stdout)
    assert isinstance(digest, str) and digest.startswith("sha256:")
    return digest


def _fixture_work(root: Path, scenario: str, image_digest: str) -> RuntimeDispatchWorkItem:
    dispatch_id = f"docker_{scenario}"
    archive = root / f"artifacts/{dispatch_id}/source.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted((FIXTURES / scenario).iterdir()):
            bundle.write(path, arcname=path.name)
    payload = archive.read_bytes()
    request = runtime_request(dispatch_id=dispatch_id, timeout_seconds=600).model_copy(
        update={
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "artifact_size_bytes": len(payload),
            "quarantine_key": f"artifacts/{dispatch_id}/source.zip",
            "target": runtime_request().target.model_copy(
                update={"image_digest": image_digest}
            ),
            "limits": runtime_request(timeout_seconds=600).limits.model_copy(
                update={"memory_mb": 2048, "pids": 256, "tmpfs_mb": 1024}
            ),
        }
    )
    return RuntimeDispatchWorkItem(
        dispatch_id=dispatch_id,
        run_id=f"run_{scenario}",
        attempt=1,
        request_sha256=request.canonical_sha256(),
        request=request,
    )


def _assert_no_managed_runtime_resources() -> None:
    import subprocess

    for kind in ("container", "volume"):
        command = [
            "docker",
            kind,
            "ls",
            "--filter",
            "label=astrbot.runtime.managed=true",
            "--format",
            "{{.Names}}" if kind == "container" else "{{.Name}}",
        ]
        if kind == "container":
            command.insert(3, "--all")
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        assert completed.stdout.strip() == ""
