from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from ...artifacts.runner_contract import (
    MAX_RUNTIME_REQUEST_BYTES,
    MAX_RUNTIME_RESULT_BYTES,
    InstallResult,
    ProbeResult,
    ProbeStatus,
    RuntimeDispatchRequest,
    RuntimeViolation,
    SmokeResult,
)
from .command import SubprocessCommandRunner, redact_probe_text
from .install import InstallSandbox

_RUNTIME_ROOT = Path("/runtime")
_ARTIFACT_PATH = _RUNTIME_ROOT / "input/artifact.zip"
_REQUEST_PATH = _RUNTIME_ROOT / "request.json"
_LAYOUT_PATH = _RUNTIME_ROOT / "layout.json"
_OUTPUT_ROOT = _RUNTIME_ROOT / "output"
_PRIVATE_ROOT = _RUNTIME_ROOT / "private"
_RESULT_PATHS = {
    "install": _OUTPUT_ROOT / "install-result.json",
    "smoke": _OUTPUT_ROOT / "smoke-result.json",
}
_RESULT_MARKER = b"ASTRBOT_RUNTIME_RESULT_V1:"
_MAX_ARCHIVE_FILES = 10_000
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000


class ProbeEntrypointError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime-probe")
    parser.add_argument(
        "action",
        choices=("stage", "install", "smoke", "smoke-inner", "emit"),
    )
    parser.add_argument("phase", nargs="?", choices=("install", "smoke"))
    arguments = parser.parse_args(argv)
    if arguments.action == "emit":
        if not arguments.phase:
            return 2
        return _emit(arguments.phase)
    if arguments.action == "stage":
        return _stage_artifact()
    if arguments.action == "smoke-inner":
        return asyncio.run(_run_smoke_inner())
    try:
        request = _read_request(sys.stdin.buffer.read(MAX_RUNTIME_REQUEST_BYTES + 1))
    except ProbeEntrypointError as exc:
        os.write(2, f"{exc.code}\n".encode())
        return 2
    except (ValidationError, ValueError):
        os.write(2, b"runtime_request_invalid\n")
        return 2
    if arguments.action == "install":
        asyncio.run(_run_install(request))
    else:
        asyncio.run(_run_smoke(request))
    return 0


async def _run_install(request: RuntimeDispatchRequest) -> None:
    try:
        plugin_root = _prepare_plugin(request)
        _write_private(_REQUEST_PATH, request.model_dump_json().encode())
        command_environment = {
            name: os.environ[name]
            for name in (
                "PIP_INDEX_URL",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "PIP_NO_CACHE_DIR",
            )
            if name in os.environ
        }
        output = await InstallSandbox(
            SubprocessCommandRunner(),
            command_env=command_environment,
        ).execute(
            request,
            workspace=_RUNTIME_ROOT / "workspace",
            plugin_root=plugin_root,
        )
        result = output.result
        _write_private(
            _PRIVATE_ROOT / "install.log",
            output.logs.encode("utf-8", errors="replace"),
            maximum=request.limits.max_log_bytes,
        )
    except ProbeEntrypointError as exc:
        result = _failed_install(exc.code, str(exc))
    except Exception:
        result = _failed_install(
            "runtime_probe_failed",
            "Runtime install probe failed before producing a result",
        )
    _write_result("install", result.model_dump_json().encode())


async def _run_smoke(request: RuntimeDispatchRequest) -> None:
    result: SmokeResult | None = None
    logs = ""
    try:
        saved_request = _read_request(_REQUEST_PATH.read_bytes())
        if saved_request.canonical_sha256() != request.canonical_sha256():
            raise ProbeEntrypointError(
                "runtime_request_mismatch",
                "Install and smoke request snapshots differ",
            )
        venv_python = _validated_venv_python()
        command = await SubprocessCommandRunner().run(
            (str(venv_python), "-m", "app.runtime_runner.probe.entrypoint", "smoke-inner"),
            cwd=_RUNTIME_ROOT,
            env={
                "PYTHONPATH": "/opt/runtime-probe",
                "RUNTIME_PROBE_REQUEST_PATH": str(_REQUEST_PATH),
            },
            timeout_seconds=request.limits.timeout_seconds,
            max_output_bytes=request.limits.max_log_bytes,
        )
        logs = command.output
        if command.timed_out:
            result = _failed_smoke(
                "runtime_command_timed_out",
                "AstrBot smoke probe exceeded its configured timeout",
                timed_out=True,
            )
        elif command.returncode == 0:
            result = _extract_smoke_result(command.output)
        if result is None:
            raise ProbeEntrypointError(
                "astrbot_lifecycle_failed",
                "AstrBot smoke subprocess did not produce a valid result",
            )
    except ProbeEntrypointError as exc:
        result = _failed_smoke(exc.code, str(exc))
    except Exception:
        result = _failed_smoke(
            "runtime_probe_failed",
            "Runtime smoke probe failed before producing a result",
        )
    _write_private(
        _PRIVATE_ROOT / "smoke.log",
        logs.encode("utf-8", errors="replace"),
        maximum=request.limits.max_log_bytes,
    )
    _write_result("smoke", result.model_dump_json().encode())


async def _run_smoke_inner() -> int:
    request_path = Path(os.environ.get("RUNTIME_PROBE_REQUEST_PATH", ""))
    try:
        if request_path != _REQUEST_PATH:
            raise ProbeEntrypointError("runtime_request_invalid", "Unexpected request path")
        request = _read_request(request_path.read_bytes())
        layout = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
        plugin_dir_name = str(layout["plugin_dir_name"])
        violations = _smoke_network_violations()
        if violations:
            result = _failed_smoke(
                "smoke_network_violation",
                "Smoke environment unexpectedly reached a blocked target",
            ).model_copy(update={"violations": violations})
        else:
            from .smoke import AstrBotSmokeProbe

            result = await AstrBotSmokeProbe().execute(
                request,
                runtime_root=_RUNTIME_ROOT / "astrbot-root",
                plugin_dir_name=plugin_dir_name,
            )
        payload = result.model_dump_json().encode()
    except Exception:
        payload = _failed_smoke(
            "astrbot_lifecycle_failed",
            "AstrBot lifecycle subprocess failed",
        ).model_dump_json().encode()
    framed = _RESULT_MARKER + base64.b64encode(payload) + b"\n"
    os.write(1, framed)
    return 0


def _prepare_plugin(request: RuntimeDispatchRequest) -> Path:
    archive = _ARTIFACT_PATH
    if archive.is_symlink() or not archive.is_file():
        raise ProbeEntrypointError("artifact_archive_invalid", "Artifact archive is unavailable")
    if archive.stat().st_size != request.artifact_size_bytes:
        raise ProbeEntrypointError("artifact_size_mismatch", "Artifact archive size changed")
    if _sha256_file(archive) != request.artifact_sha256:
        raise ProbeEntrypointError("artifact_hash_mismatch", "Artifact archive hash changed")

    extract_root = _RUNTIME_ROOT / "extracted"
    runtime_root = _RUNTIME_ROOT / "astrbot-root"
    workspace = _RUNTIME_ROOT / "workspace"
    for path in (extract_root, runtime_root, workspace, _OUTPUT_ROOT, _PRIVATE_ROOT):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(mode=0o700, parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            _extract_archive(bundle, extract_root)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProbeEntrypointError(
            "artifact_archive_invalid",
            "Artifact is not a valid ZIP archive",
        ) from exc

    metadata = [
        path
        for path in extract_root.rglob("*")
        if path.is_file() and path.name in {"metadata.yaml", "metadata.yml"}
    ]
    if len(metadata) != 1:
        raise ProbeEntrypointError(
            "artifact_layout_invalid",
            "Artifact must contain exactly one metadata file",
        )
    plugin_dir_name = request.expected_plugin.name
    plugin_target = runtime_root / "data/plugins" / plugin_dir_name
    plugin_target.parent.mkdir(mode=0o700, parents=True)
    shutil.copytree(metadata[0].parent, plugin_target)
    _write_private(
        _LAYOUT_PATH,
        json.dumps(
            {"plugin_dir_name": plugin_dir_name},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode(),
    )
    return plugin_target


def _extract_archive(bundle: zipfile.ZipFile, destination: Path) -> None:
    entries = bundle.infolist()
    if len(entries) > _MAX_ARCHIVE_FILES:
        raise ProbeEntrypointError("artifact_archive_invalid", "Artifact has too many files")
    seen: set[str] = set()
    unpacked = 0
    for entry in entries:
        raw_name = entry.filename
        path = PurePosixPath(raw_name)
        if (
            not raw_name
            or "\x00" in raw_name
            or "\\" in raw_name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ProbeEntrypointError("artifact_archive_invalid", "Artifact path is unsafe")
        normalized = path.as_posix().rstrip("/")
        if normalized in seen:
            raise ProbeEntrypointError("artifact_archive_invalid", "Artifact path is duplicated")
        seen.add(normalized)
        mode = entry.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ProbeEntrypointError("artifact_archive_invalid", "Artifact contains a symlink")
        if entry.is_dir():
            (destination / path).mkdir(mode=0o700, parents=True, exist_ok=True)
            continue
        if entry.file_size > _MAX_FILE_BYTES:
            raise ProbeEntrypointError("artifact_archive_invalid", "Artifact file is too large")
        if entry.file_size and (
            entry.compress_size == 0
            or entry.file_size / max(entry.compress_size, 1) > _MAX_COMPRESSION_RATIO
        ):
            raise ProbeEntrypointError(
                "artifact_archive_invalid",
                "Artifact compression ratio is unsafe",
            )
        unpacked += entry.file_size
        if unpacked > _MAX_UNPACKED_BYTES:
            raise ProbeEntrypointError("artifact_archive_invalid", "Artifact is too large unpacked")
        target = destination / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        written = 0
        with bundle.open(entry) as source, target.open("xb") as output:
            while chunk := source.read(64 * 1024):
                written += len(chunk)
                if written > entry.file_size:
                    raise ProbeEntrypointError(
                        "artifact_archive_invalid",
                        "Artifact file size is inconsistent",
                    )
                output.write(chunk)
        if written != entry.file_size:
            raise ProbeEntrypointError(
                "artifact_archive_invalid",
                "Artifact file is truncated",
            )
        target.chmod(0o600)


def _read_request(payload: bytes) -> RuntimeDispatchRequest:
    if not payload:
        raise ProbeEntrypointError("runtime_request_missing", "Runtime request is missing")
    if len(payload) > MAX_RUNTIME_REQUEST_BYTES:
        raise ProbeEntrypointError("runtime_request_too_large", "Runtime request is too large")
    try:
        return RuntimeDispatchRequest.model_validate_json(payload)
    except ValidationError as exc:
        location = "_".join(str(item) for item in exc.errors()[0].get("loc", ())[:3])
        location = re.sub(r"[^a-z0-9_]+", "_", location.casefold()).strip("_")
        code = f"runtime_request_invalid_{location}"[:96].rstrip("_")
        raise ProbeEntrypointError(code, "Runtime request is invalid") from exc


def _validated_venv_python() -> Path:
    path = _RUNTIME_ROOT / "workspace/venv/bin/python"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProbeEntrypointError(
            "runtime_environment_missing",
            "Install probe did not create a reusable runtime environment",
        ) from exc
    if (
        not path.is_file()
        or not resolved.is_file()
        or resolved.parent != Path("/usr/local/bin")
        or not resolved.name.startswith("python")
    ):
        raise ProbeEntrypointError(
            "runtime_environment_missing",
            "Install probe did not create a reusable runtime environment",
        )
    return path


def _extract_smoke_result(output: str) -> SmokeResult | None:
    framed = [line for line in output.splitlines() if line.startswith(_RESULT_MARKER.decode())]
    if not framed:
        return None
    try:
        payload = base64.b64decode(framed[-1].split(":", 1)[1], validate=True)
        if len(payload) > MAX_RUNTIME_RESULT_BYTES:
            return None
        return SmokeResult.model_validate_json(payload)
    except (ValueError, ValidationError):
        return None


def _failed_install(code: str, message: str) -> InstallResult:
    return InstallResult(
        status=ProbeStatus.FAILED,
        error_code=code,
        message=redact_probe_text(message, maximum=500),
        pip_check=ProbeResult(
            status=ProbeStatus.SKIPPED,
            error_code="dependency_check_not_run",
            message="Dependency validation was not reached",
        ),
    )


def _failed_smoke(code: str, message: str, *, timed_out: bool = False) -> SmokeResult:
    failed_status = ProbeStatus.TIMED_OUT if timed_out else ProbeStatus.FAILED
    failed = {
        "status": failed_status.value,
        "error_code": code,
        "message": redact_probe_text(message, maximum=500),
    }
    skipped = {
        "status": "skipped",
        "error_code": "probe_not_reached",
        "message": "Probe phase was not reached",
    }
    skipped_registration = {**skipped, "count": 0, "names": []}
    return SmokeResult.model_validate(
        {
            "status": failed_status.value,
            "metadata": skipped,
            "import_probe": failed,
            "instance": skipped,
            "initialize": skipped,
            "startup": skipped,
            "handlers": skipped_registration,
            "hooks": skipped_registration,
            "llm_tools": skipped_registration,
            "failed_plugin": {"present": False},
            "termination": skipped,
            "violations": [],
            "error_code": code,
            "message": redact_probe_text(message, maximum=500),
        }
    )


def _write_result(phase: str, payload: bytes) -> None:
    if phase not in _RESULT_PATHS or len(payload) > MAX_RUNTIME_RESULT_BYTES:
        raise ProbeEntrypointError("runtime_result_invalid", "Runtime result is invalid")
    _write_private(_RESULT_PATHS[phase], payload, maximum=MAX_RUNTIME_RESULT_BYTES)


def _write_private(path: Path, payload: bytes, *, maximum: int = 64 * 1024) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    bounded = payload[:maximum]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(bounded)
    temporary.chmod(0o600)
    temporary.replace(path)


def _emit(phase: str) -> int:
    path = _RESULT_PATHS[phase]
    try:
        payload = path.read_bytes()
    except OSError:
        return 2
    if not payload or len(payload) > MAX_RUNTIME_RESULT_BYTES:
        return 2
    sys.stdout.buffer.write(payload)
    return 0


def _stage_artifact() -> int:
    try:
        if _ARTIFACT_PATH.is_symlink() or not _ARTIFACT_PATH.is_file():
            return 2
        _ARTIFACT_PATH.chmod(0o444)
        _RUNTIME_ROOT.chmod(0o777)
        _ARTIFACT_PATH.parent.chmod(0o755)
    except OSError:
        return 2
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _smoke_network_violations() -> tuple[RuntimeViolation, ...]:
    targets = (
        ("internet", "1.1.1.1", 443),
        ("cloud_metadata", "169.254.169.254", 80),
        ("host_gateway", "172.17.0.1", 2375),
        ("postgres", "postgres", 5432),
        ("redis", "redis", 6379),
        ("package_proxy", "astrbot-runtime-package-proxy", 3128),
    )
    violations: list[RuntimeViolation] = []
    for label, host, port in targets:
        try:
            connection = socket.create_connection((host, port), timeout=0.2)
        except OSError:
            continue
        connection.close()
        violations.append(
            RuntimeViolation(
                phase="smoke",
                category="smoke_network_access",
                message=f"Smoke environment reached blocked target {label}",
            )
        )
    if Path("/var/run/docker.sock").exists():
        violations.append(
            RuntimeViolation(
                phase="smoke",
                category="docker_socket_exposed",
                message="Smoke environment can see a Docker socket",
            )
        )
    return tuple(violations)


if __name__ == "__main__":
    raise SystemExit(main())
