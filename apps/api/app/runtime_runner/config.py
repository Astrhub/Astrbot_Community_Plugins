from __future__ import annotations

import os
import re
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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

    def public_summary(self) -> dict[str, object]:
        return {
            "configured": True,
            "runner_id": self.runner_id,
            "result_storage_backend": self.result_storage_backend,
            "executor_backend": self.executor_backend,
            "claim_limit": self.claim_limit,
            "lease_seconds": self.lease_seconds,
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
