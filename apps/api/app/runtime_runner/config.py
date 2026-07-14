from __future__ import annotations

import os
import re
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_RUNNER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RuntimeRunnerConfigurationError(ValueError):
    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__(", ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class RuntimeRunnerSettings:
    database_url: str = field(repr=False)
    runner_id: str
    result_storage_backend: str
    result_root: Path
    executor_backend: str
    claim_limit: int
    lease_seconds: int
    poll_seconds: float
    orphan_cleanup_seconds: float
    shutdown_grace_seconds: float
    docker_binary: str = "docker"
    docker_host: str = ""
    docker_image_repository: str = "astrbot-runtime-probe"
    docker_artifact_root: str = "/var/lib/astrbot-market/artifacts"
    docker_install_network: str = "astrbot-runtime-install"
    docker_package_index_url: str = "https://pypi.org/simple"
    docker_install_proxy_url: str = "http://astrbot-runtime-package-proxy:3128"
    docker_install_proxy_container: str = "astrbot-runtime-package-proxy"
    docker_orphan_ttl_seconds: int = 7200
    docker_allow_rootful_development: bool = False
    docker_seccomp_profile: str = "builtin"
    docker_apparmor_profile: str = ""

    def public_summary(self) -> dict[str, object]:
        return {
            "configured": True,
            "runner_id": self.runner_id,
            "result_storage_backend": self.result_storage_backend,
            "executor_backend": self.executor_backend,
            "claim_limit": self.claim_limit,
            "lease_seconds": self.lease_seconds,
            "container_isolation": (
                "rootful-development"
                if self.docker_allow_rootful_development
                else "rootless-required"
            ),
            "image_pinning": "digest",
            "network_profiles": {"install": "proxy-only", "smoke": "none"},
        }


def load_runtime_runner_settings(
    env: Mapping[str, str] | None = None,
) -> RuntimeRunnerSettings:
    source = os.environ if env is None else env
    errors: list[str] = []
    database_url = str(source.get("RUNTIME_RUNNER_DATABASE_URL", "")).strip()
    if not database_url:
        errors.append("runtime_runner_database_url_missing")

    runner_id = str(source.get("RUNTIME_RUNNER_ID", "")).strip() or _default_runner_id()
    if not _RUNNER_ID.fullmatch(runner_id):
        errors.append("runtime_runner_id_invalid")

    result_storage_backend = (
        str(source.get("RUNTIME_RUNNER_RESULT_STORAGE_BACKEND", "local")).strip().lower()
    )
    if result_storage_backend != "local":
        errors.append("runtime_runner_result_storage_backend_unsupported")

    result_root = Path(
        str(source.get("RUNTIME_RUNNER_RESULT_ROOT", "/var/lib/astrbot-runtime-results"))
    ).expanduser()
    if not result_root.is_absolute():
        errors.append("runtime_runner_result_root_not_absolute")

    executor_backend = str(source.get("RUNTIME_RUNNER_EXECUTOR_BACKEND", "unconfigured")).strip()
    if not re.fullmatch(r"^[a-z][a-z0-9_-]{0,31}$", executor_backend):
        errors.append("runtime_runner_executor_backend_invalid")

    docker_binary = str(source.get("RUNTIME_RUNNER_DOCKER_BINARY", "docker")).strip()
    if not _valid_docker_binary(docker_binary):
        errors.append("runtime_runner_docker_binary_invalid")
    docker_host = str(source.get("RUNTIME_RUNNER_DOCKER_HOST", "")).strip()
    if docker_host and not re.fullmatch(r"unix:///[A-Za-z0-9_./-]{1,240}", docker_host):
        errors.append("runtime_runner_docker_host_invalid")
    docker_image_repository = str(
        source.get("RUNTIME_RUNNER_DOCKER_IMAGE_REPOSITORY", "astrbot-runtime-probe")
    ).strip()
    if not _valid_image_repository(docker_image_repository):
        errors.append("runtime_runner_docker_image_repository_invalid")
    docker_artifact_root = str(
        source.get(
            "RUNTIME_RUNNER_ARTIFACT_ROOT",
            "/var/lib/astrbot-market/artifacts",
        )
    ).strip()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", docker_artifact_root):
        errors.append("runtime_runner_artifact_root_invalid")
    docker_install_network = str(
        source.get("RUNTIME_RUNNER_INSTALL_NETWORK", "astrbot-runtime-install")
    ).strip()
    if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", docker_install_network):
        errors.append("runtime_runner_install_network_invalid")
    docker_package_index_url = str(
        source.get("RUNTIME_RUNNER_PACKAGE_INDEX_URL", "https://pypi.org/simple")
    ).strip()
    if not _valid_package_index_url(docker_package_index_url):
        errors.append("runtime_runner_package_index_url_invalid")
    docker_install_proxy_url = str(
        source.get(
            "RUNTIME_RUNNER_INSTALL_PROXY_URL",
            "http://astrbot-runtime-package-proxy:3128",
        )
    ).strip()
    if not _valid_install_proxy_url(docker_install_proxy_url):
        errors.append("runtime_runner_install_proxy_url_invalid")
    docker_install_proxy_container = str(
        source.get(
            "RUNTIME_RUNNER_INSTALL_PROXY_CONTAINER",
            "astrbot-runtime-package-proxy",
        )
    ).strip()
    if not re.fullmatch(
        r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        docker_install_proxy_container,
    ):
        errors.append("runtime_runner_install_proxy_container_invalid")
    elif _url_hostname(docker_install_proxy_url) != docker_install_proxy_container:
        errors.append("runtime_runner_install_proxy_container_mismatch")
    docker_orphan_ttl_seconds = _bounded_int(
        source,
        "RUNTIME_RUNNER_ORPHAN_TTL_SECONDS",
        default=7200,
        minimum=3600,
        maximum=604800,
        errors=errors,
    )
    docker_allow_rootful_development = _boolean(
        source,
        "RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT",
        default=False,
        errors=errors,
    )
    docker_seccomp_profile = str(
        source.get("RUNTIME_RUNNER_SECCOMP_PROFILE", "builtin")
    ).strip()
    if docker_seccomp_profile != "builtin":
        errors.append("runtime_runner_seccomp_profile_invalid")
    docker_apparmor_profile = str(
        source.get("RUNTIME_RUNNER_APPARMOR_PROFILE", "")
    ).strip()
    if docker_apparmor_profile and not re.fullmatch(
        r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
        docker_apparmor_profile,
    ):
        errors.append("runtime_runner_apparmor_profile_invalid")

    claim_limit = _bounded_int(
        source,
        "RUNTIME_RUNNER_CLAIM_LIMIT",
        default=4,
        minimum=1,
        maximum=32,
        errors=errors,
    )
    lease_seconds = _bounded_int(
        source,
        "RUNTIME_RUNNER_LEASE_SECONDS",
        default=60,
        minimum=10,
        maximum=3600,
        errors=errors,
    )
    poll_seconds = _bounded_float(
        source,
        "RUNTIME_RUNNER_POLL_SECONDS",
        default=1.0,
        minimum=0.05,
        maximum=60.0,
        errors=errors,
    )
    orphan_cleanup_seconds = _bounded_float(
        source,
        "RUNTIME_RUNNER_ORPHAN_CLEANUP_SECONDS",
        default=300.0,
        minimum=1.0,
        maximum=86400.0,
        errors=errors,
    )
    shutdown_grace_seconds = _bounded_float(
        source,
        "RUNTIME_RUNNER_SHUTDOWN_GRACE_SECONDS",
        default=30.0,
        minimum=0.0,
        maximum=3600.0,
        errors=errors,
    )

    if errors:
        raise RuntimeRunnerConfigurationError(tuple(dict.fromkeys(errors)))
    return RuntimeRunnerSettings(
        database_url=database_url,
        runner_id=runner_id,
        result_storage_backend=result_storage_backend,
        result_root=result_root,
        executor_backend=executor_backend,
        claim_limit=claim_limit,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
        orphan_cleanup_seconds=orphan_cleanup_seconds,
        shutdown_grace_seconds=shutdown_grace_seconds,
        docker_binary=docker_binary,
        docker_host=docker_host,
        docker_image_repository=docker_image_repository,
        docker_artifact_root=docker_artifact_root,
        docker_install_network=docker_install_network,
        docker_package_index_url=docker_package_index_url,
        docker_install_proxy_url=docker_install_proxy_url,
        docker_install_proxy_container=docker_install_proxy_container,
        docker_orphan_ttl_seconds=docker_orphan_ttl_seconds,
        docker_allow_rootful_development=docker_allow_rootful_development,
        docker_seccomp_profile=docker_seccomp_profile,
        docker_apparmor_profile=docker_apparmor_profile,
    )


def _default_runner_id() -> str:
    hostname = re.sub(r"[^A-Za-z0-9._:-]", "-", socket.gethostname()).strip("-")
    prefix = (hostname or "runtime-runner")[:110]
    return f"{prefix}-{secrets.token_hex(4)}"


def _bounded_int(
    source: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    raw = str(source.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name.lower()}_invalid")
        return default
    if value < minimum or value > maximum:
        errors.append(f"{name.lower()}_out_of_range")
        return default
    return value


def _bounded_float(
    source: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    raw = str(source.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name.lower()}_invalid")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name.lower()}_out_of_range")
        return default
    return value


def _boolean(
    source: Mapping[str, str],
    name: str,
    *,
    default: bool,
    errors: list[str],
) -> bool:
    raw = str(source.get(name, "true" if default else "false")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    errors.append(f"{name.lower()}_invalid")
    return default


def _valid_docker_binary(value: str) -> bool:
    if value == "docker":
        return True
    return bool(value.startswith("/") and re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", value))


def _valid_image_repository(value: str) -> bool:
    return bool(
        1 <= len(value) <= 255
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value)
        and "@" not in value
        and ".." not in value
        and "//" not in value
    )


def _valid_package_index_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/").endswith("/simple")
    )


def _valid_install_proxy_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "http"
        and parsed.hostname
        and re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", parsed.hostname)
        and port is not None
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _url_hostname(value: str) -> str:
    try:
        return str(urlsplit(value).hostname or "")
    except ValueError:
        return ""
