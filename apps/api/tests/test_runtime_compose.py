from __future__ import annotations

from pathlib import Path

import yaml

from app.runtime_runner.network_policy import required_network_labels

ROOT = Path(__file__).parents[3]


def test_runtime_compose_profile_keeps_socket_out_of_trusted_control_plane() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    for name in ("app", "artifact-worker"):
        volumes = " ".join(str(item) for item in services[name].get("volumes", []))
        assert "docker.sock" not in volumes
    app_volumes = " ".join(str(item) for item in services["app"].get("volumes", []))
    worker_volumes = " ".join(str(item) for item in services["artifact-worker"].get("volumes", []))
    assert "runtime-results" not in app_volumes
    assert "runtime-results:/var/lib/astrbot-runtime-results:ro" in worker_volumes
    runner = services["runtime-runner"]
    runner_volumes = " ".join(str(item) for item in runner["volumes"])
    assert runner["profiles"] == ["runtime-runner"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in runner_volumes
    assert "runtime-results:/var/lib/astrbot-runtime-results" in runner_volumes
    assert runner["environment"]["RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT"] == "true"
    assert runner["environment"]["RUNTIME_RUNNER_EXECUTOR_BACKEND"] == "rootless-docker"
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
