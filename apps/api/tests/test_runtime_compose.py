from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.runtime_runner.network_policy import required_network_labels

ROOT = Path(__file__).parents[3]


def test_runtime_compose_profile_keeps_socket_out_of_trusted_control_plane() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    for name in ("app", "artifact-worker"):
        volumes = " ".join(str(item) for item in services[name].get("volumes", []))
        assert "docker.sock" not in volumes
        assert services[name]["user"] == "${" + (
            "API_UID_GID:-10001:10001}"
            if name == "app"
            else "ARTIFACT_WORKER_UID_GID:-10001:10001}"
        )
    app_volumes = " ".join(str(item) for item in services["app"].get("volumes", []))
    worker_volumes = " ".join(str(item) for item in services["artifact-worker"].get("volumes", []))
    assert "${API_ENV_FILE:-./apps/api/.env}:/app/apps/api/.env:rw" in app_volumes
    assert (
        "${ARTIFACT_WORKER_ENV_FILE:-./deploy/compose/artifact-worker.env.example}:"
        "/app/apps/api/.env:ro"
    ) in worker_volumes
    assert "runtime-results" not in app_volumes
    assert "runtime-results:/var/lib/astrbot-runtime-results:ro" in worker_volumes
    runner = services["runtime-runner"]
    runner_volumes = " ".join(str(item) for item in runner["volumes"])
    assert runner["profiles"] == ["runtime-runner"]
    assert (
        "${RUNTIME_RUNNER_DOCKER_SOCKET:-/run/user/10001/docker.sock}:/var/run/docker.sock"
    ) in runner_volumes
    assert "runtime-results:/var/lib/astrbot-runtime-results" in runner_volumes
    assert runner["environment"]["RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT"] == (
        "${RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT:-false}"
    )
    assert runner["environment"]["RUNTIME_RUNNER_EXECUTOR_BACKEND"] == "rootless-docker"
    assert runner["user"] == "${RUNTIME_RUNNER_UID_GID:-10001:10001}"
    assert runner["read_only"] is True
    assert runner["cap_drop"] == ["ALL"]
    assert "no-new-privileges=true" in runner["security_opt"]


def test_runtime_compose_network_matches_fail_closed_policy_contract() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    network = compose["networks"]["runtime-install"]
    proxy = compose["services"]["runtime-package-proxy"]

    assert network["name"] == "astrbot-runtime-install"
    assert network["internal"] is True
    assert network["driver"] == "bridge"
    assert network["driver_opts"]["com.docker.network.bridge.enable_ip_masquerade"] == "false"
    assert network["labels"] == required_network_labels(
        "pypi-only-v1",
        "https://pypi.org/simple",
    )
    assert proxy["container_name"] == "astrbot-runtime-package-proxy"
    assert proxy["user"] == "13:13"
    assert set(proxy["networks"]) == {"runtime-install", "runtime-proxy-egress"}
    assert proxy["labels"]["astrbot.runtime.package-proxy"] == "true"
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]


def test_runtime_probe_and_runner_images_are_separate_targets() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    probe = compose["services"]["runtime-probe-image"]
    runner = compose["services"]["runtime-runner"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    probe_dockerfile = (ROOT / "Dockerfile.runtime-probe").read_text(encoding="utf-8")

    assert probe["build"]["dockerfile"] == "Dockerfile.runtime-probe"
    assert probe["network_mode"] == "none"
    assert runner["build"]["target"] == "runtime-runner"
    assert "FROM api-base AS runtime-runner" in dockerfile
    assert "COPY --from=docker-cli /usr/local/bin/docker" in dockerfile
    assert "USER 65532:65532" in probe_dockerfile
    assert "PYTHONPATH=/opt/runtime-probe" in probe_dockerfile
    assert compose["services"]["app"]["build"]["target"] == "api"
    assert "USER 10001:10001" in dockerfile


def test_runtime_runner_configuration_excludes_control_plane_and_review_secrets() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    runner = compose["services"]["runtime-runner"]
    serialized = " ".join(
        [
            *(str(item) for item in runner.get("environment", {}).keys()),
            *(str(item) for item in runner.get("volumes", [])),
        ]
    )

    for forbidden in (
        "REDIS_URL",
        "ARTIFACT_LLM_API_KEY",
        "ARTIFACT_S3_SECRET_ACCESS_KEY",
        "SMTP_PASSWORD",
        "GITHUB_CLIENT_SECRET",
    ):
        assert forbidden not in serialized
    assert "artifact-data:/var/lib/astrbot-market/artifacts:ro" in serialized


def test_systemd_units_use_separate_users_and_environment_files() -> None:
    units = {
        "api": (ROOT / "deploy/systemd/astrbot-community-plugins.service").read_text(
            encoding="utf-8"
        ),
        "worker": (ROOT / "deploy/systemd/astrbot-artifact-worker.service").read_text(
            encoding="utf-8"
        ),
        "runner": (ROOT / "deploy/systemd/astrbot-runtime-runner.service").read_text(
            encoding="utf-8"
        ),
    }

    assert "User=astrbot-market-api" in units["api"]
    assert "EnvironmentFile=/etc/astrbot-community-plugins/api.env" in units["api"]
    assert "User=astrbot-market-worker" in units["worker"]
    assert "EnvironmentFile=/etc/astrbot-community-plugins/artifact-worker.env" in units["worker"]
    assert "User=astrbot-runtime" in units["runner"]
    assert "SupplementaryGroups=astrbot-market" in units["runner"]
    assert "EnvironmentFile=/etc/astrbot-community-plugins/runtime-runner.env" in units["runner"]
    assert "ReadOnlyPaths=/var/lib/astrbot-market/artifacts" in units["runner"]
    assert "ReadWritePaths=/var/lib/astrbot-runtime-results" in units["runner"]
    assert "InaccessiblePaths=/var/lib/astrbot-runtime-results" in units["api"]
    assert "ProtectHome=read-only" in units["runner"]
    assert "ProtectHome=true" not in units["runner"]
    assert all("UMask=0007" in unit for unit in units.values())
    assert "docker.sock" not in units["api"] + units["worker"]


def test_deployment_examples_keep_advanced_review_features_fail_closed() -> None:
    api_example = (ROOT / "deploy/compose/api.env.example").read_text(encoding="utf-8")
    worker_example = (ROOT / "deploy/compose/artifact-worker.env.example").read_text(
        encoding="utf-8"
    )
    runner_example = (ROOT / "deploy/systemd/astrbot-runtime-runner.env.example").read_text(
        encoding="utf-8"
    )

    assert "ARTIFACT_LLM_API_KEY" not in api_example
    assert "ARTIFACT_CLAMAV_HOST" not in api_example
    assert "ARTIFACTS_ENABLED=false" in worker_example
    assert "ARTIFACT_ADVANCED_REVIEW_ENABLED=false" in worker_example
    assert "ARTIFACT_AUTO_APPROVE_ENABLED=false" in worker_example
    assert "ARTIFACT_RUNTIME_REVIEW_ENABLED=false" in worker_example
    assert "ARTIFACT_LLM_REVIEW_ENABLED=false" in worker_example
    assert "ARTIFACT_CLAMAV_ENABLED=false" in worker_example
    assert "ARTIFACT_YARA_ENABLED=false" in worker_example
    assert "ARTIFACT_DEPENDENCY_REVIEW_ENABLED=false" in worker_example
    assert "RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false" in runner_example
    assert "REDIS_URL" not in runner_example
    assert "ARTIFACT_LLM" not in runner_example


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable")
def test_all_compose_profiles_render_with_safe_defaults() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "artifacts",
            "--profile",
            "runtime-runner",
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
