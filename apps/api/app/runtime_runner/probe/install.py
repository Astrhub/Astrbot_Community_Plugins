from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ...artifacts.runner_contract import (
    DependencyConflict,
    InstallResult,
    InstalledPackage,
    ProbeResult,
    ProbeStatus,
    RuntimeDispatchRequest,
)
from .command import CommandResult, CommandRunner, redact_probe_text

_REQUIREMENTS_MAX_BYTES = 256 * 1024
_UNSAFE_REQUIREMENT = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|git\+|hg\+|svn\+|bzr\+|--|^-|\s@\s)",
    re.IGNORECASE,
)
_PIP_CONFLICT = re.compile(
    r"^(?P<package>[A-Za-z0-9_.-]+)\s+(?P<installed>[^\s]+)\s+has requirement\s+"
    r"(?P<requirement>.+?),\s+but you have\s+(?P<actual>[A-Za-z0-9_.-]+)\s+"
    r"(?P<actual_version>[^.\s]+(?:\.[^.\s]+)*)\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class InstallProbeOutput:
    result: InstallResult
    requirements_sha256: str
    sbom_path: Path | None
    sbom_sha256: str
    logs: str
    logs_truncated: bool


class InstallSandbox:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        python_executable: str = sys.executable,
        runtime_python_version: str | None = None,
        command_env: Mapping[str, str] | None = None,
    ) -> None:
        self.runner = runner
        self.python_executable = python_executable
        self.runtime_python_version = runtime_python_version or platform.python_version()
        self.command_env = dict(command_env or {})

    async def execute(
        self,
        request: RuntimeDispatchRequest,
        *,
        workspace: Path,
        plugin_root: Path,
    ) -> InstallProbeOutput:
        started = time.monotonic()
        workspace = workspace.resolve(strict=True)
        plugin_root = plugin_root.resolve(strict=True)
        if not workspace.is_dir() or not plugin_root.is_dir():
            raise ValueError("runtime_install_paths_invalid")
        requirements_path = plugin_root / "requirements.txt"
        try:
            requirements_sha256 = _validate_requirements(requirements_path)
        except ValueError as exc:
            return _failure_output(
                started,
                "requirements_invalid",
                str(exc),
                requirements_sha256="",
            )
        try:
            _validate_python_target(request.target.python_version, self.runtime_python_version)
        except ValueError as exc:
            return _failure_output(
                started,
                "runtime_python_version_mismatch",
                str(exc),
                requirements_sha256=requirements_sha256,
            )

        log_parts: list[str] = []
        logs_truncated = False
        venv_root = workspace / "venv"
        venv_python = venv_root / "bin" / "python"
        command_limit = request.limits.max_log_bytes

        async def run(phase: str, argv: Sequence[str]) -> CommandResult:
            nonlocal logs_truncated
            result = await self.runner.run(
                argv,
                cwd=workspace,
                env=self.command_env,
                timeout_seconds=request.limits.timeout_seconds,
                max_output_bytes=command_limit,
            )
            if result.output:
                log_parts.append(f"[{phase}]\n{redact_probe_text(result.output)}")
            logs_truncated = logs_truncated or result.truncated
            return result

        created = await run(
            "venv",
            (self.python_executable, "-m", "venv", "--clear", str(venv_root)),
        )
        if not created.succeeded:
            return _command_failure(
                started,
                created,
                "runtime_venv_failed",
                requirements_sha256,
                log_parts,
                logs_truncated,
                max_log_bytes=command_limit,
            )
        installed_astrbot = await run(
            "astrbot-install",
            (
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                f"AstrBot=={request.target.astrbot_version}",
            ),
        )
        if not installed_astrbot.succeeded:
            return _command_failure(
                started,
                installed_astrbot,
                "dependency_install_failed",
                requirements_sha256,
                log_parts,
                logs_truncated,
                max_log_bytes=command_limit,
            )
        before_command = await run("snapshot-before", _pip_list_argv(venv_python))
        before = _parse_package_snapshot(before_command)
        if before is None:
            return _command_failure(
                started,
                before_command,
                "dependency_snapshot_invalid",
                requirements_sha256,
                log_parts,
                logs_truncated,
                max_log_bytes=command_limit,
            )
        before_sha256 = _snapshot_sha256(before)

        if requirements_sha256:
            installed_plugin = await run(
                "plugin-requirements",
                (
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--requirement",
                    str(requirements_path),
                ),
            )
            if not installed_plugin.succeeded:
                return _command_failure(
                    started,
                    installed_plugin,
                    "dependency_install_failed",
                    requirements_sha256,
                    log_parts,
                    logs_truncated,
                    core_before_sha256=before_sha256,
                    packages=tuple(before.values()),
                    max_log_bytes=command_limit,
                )

        pip_check = await run(
            "pip-check",
            (str(venv_python), "-m", "pip", "check", "--disable-pip-version-check"),
        )
        after_command = await run("snapshot-after", _pip_list_argv(venv_python))
        after = _parse_package_snapshot(after_command)
        if after is None:
            return _command_failure(
                started,
                after_command,
                "dependency_snapshot_invalid",
                requirements_sha256,
                log_parts,
                logs_truncated,
                core_before_sha256=before_sha256,
                max_log_bytes=command_limit,
            )
        after_sha256 = _snapshot_sha256(after)
        conflicts = [*_core_dependency_conflicts(before, after), *_pip_check_conflicts(pip_check)]
        status = ProbeStatus.PASSED
        error_code = ""
        message = ""
        if pip_check.timed_out:
            status = ProbeStatus.TIMED_OUT
            error_code = "dependency_check_timed_out"
            message = "Dependency validation exceeded its configured timeout"
        elif not pip_check.succeeded:
            status = ProbeStatus.FAILED
            error_code = "dependency_conflict"
            message = "Installed plugin requirements do not form a consistent environment"
        elif conflicts:
            status = ProbeStatus.FAILED
            error_code = "astrbot_core_dependency_conflict"
            message = "Plugin requirements destructively changed AstrBot core dependencies"

        sbom_path, sbom_sha256 = _write_sbom(
            workspace,
            request.target.astrbot_version,
            tuple(after.values()),
        )
        result = InstallResult(
            status=status,
            duration_ms=_duration_ms(started),
            error_code=error_code,
            message=message,
            astrbot_version=request.target.astrbot_version,
            pip_check=(
                ProbeResult(status=ProbeStatus.PASSED, duration_ms=pip_check.duration_ms)
                if pip_check.succeeded
                else ProbeResult(
                    status=(ProbeStatus.TIMED_OUT if pip_check.timed_out else ProbeStatus.FAILED),
                    duration_ms=pip_check.duration_ms,
                    error_code=(
                        "dependency_check_timed_out"
                        if pip_check.timed_out
                        else "dependency_conflict"
                    ),
                    message="Dependency validation did not pass",
                )
            ),
            packages=tuple(after.values()),
            conflicts=tuple(conflicts[:500]),
            core_before_sha256=before_sha256,
            core_after_sha256=after_sha256,
        )
        logs, final_truncated = _bounded_logs(log_parts, request.limits.max_log_bytes)
        return InstallProbeOutput(
            result=result,
            requirements_sha256=requirements_sha256,
            sbom_path=sbom_path,
            sbom_sha256=sbom_sha256,
            logs=logs,
            logs_truncated=logs_truncated or final_truncated,
        )


def _validate_requirements(path: Path) -> str:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return ""
    if path.is_symlink() or not path.is_file() or stat.st_size > _REQUIREMENTS_MAX_BYTES:
        raise ValueError("requirements.txt must be a bounded regular file")
    content = path.read_bytes()
    if b"\x00" in content:
        raise ValueError("requirements.txt contains unsupported binary content")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("requirements.txt must use UTF-8") from exc
    for line in _logical_requirement_lines(text):
        if _UNSAFE_REQUIREMENT.search(line):
            raise ValueError("requirements.txt cannot contain URLs, VCS sources, or pip options")
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError("requirements.txt contains an invalid requirement") from exc
        if requirement.url:
            raise ValueError("requirements.txt cannot contain direct URL dependencies")
    return hashlib.sha256(content).hexdigest() if content.strip() else ""


def _logical_requirement_lines(value: str) -> tuple[str, ...]:
    lines: list[str] = []
    pending = ""
    for raw_line in value.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        pending += line
        if pending.endswith("\\"):
            pending = pending[:-1].strip() + " "
            continue
        lines.append(pending.strip())
        pending = ""
    if pending:
        raise ValueError("requirements.txt has an incomplete continuation")
    return tuple(lines)


def _validate_python_target(target: str, actual: str) -> None:
    try:
        target_release = Version(target).release
        actual_release = Version(actual).release
    except InvalidVersion as exc:
        raise ValueError("runtime Python version is invalid") from exc
    if actual_release[: len(target_release)] != target_release:
        raise ValueError("container Python version does not match the runtime target")


def _pip_list_argv(python: Path) -> tuple[str, ...]:
    return (
        str(python),
        "-m",
        "pip",
        "list",
        "--format=json",
        "--disable-pip-version-check",
    )


def _parse_package_snapshot(result: CommandResult) -> dict[str, InstalledPackage] | None:
    if not result.succeeded:
        return None
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) > 2000:
        return None
    packages: dict[str, InstalledPackage] = {}
    try:
        for item in payload:
            if not isinstance(item, Mapping):
                return None
            package = InstalledPackage(
                name=str(item.get("name") or ""),
                version=str(item.get("version") or ""),
                source="unknown",
            )
            key = canonicalize_name(package.name)
            if key in packages:
                return None
            packages[key] = package
    except ValueError:
        return None
    return dict(sorted(packages.items()))


def _snapshot_sha256(packages: Mapping[str, InstalledPackage]) -> str:
    payload = [package.model_dump(mode="json") for package in packages.values()]
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _core_dependency_conflicts(
    before: Mapping[str, InstalledPackage],
    after: Mapping[str, InstalledPackage],
) -> list[DependencyConflict]:
    conflicts: list[DependencyConflict] = []
    for key, original in before.items():
        current = after.get(key)
        if current is None:
            conflicts.append(
                DependencyConflict(
                    package=original.name,
                    installed_version="",
                    requirement=f">={original.version}",
                    required_by="AstrBot",
                )
            )
            continue
        try:
            downgraded = Version(current.version) < Version(original.version)
        except InvalidVersion:
            downgraded = current.version != original.version
        if downgraded:
            conflicts.append(
                DependencyConflict(
                    package=original.name,
                    installed_version=current.version,
                    requirement=f">={original.version}",
                    required_by="AstrBot",
                )
            )
    return conflicts


def _pip_check_conflicts(result: CommandResult) -> list[DependencyConflict]:
    if result.succeeded or result.timed_out:
        return []
    conflicts: list[DependencyConflict] = []
    for line in result.output.splitlines()[:500]:
        match = _PIP_CONFLICT.match(line.strip())
        if not match:
            continue
        conflicts.append(
            DependencyConflict(
                package=match.group("actual"),
                installed_version=match.group("actual_version"),
                requirement=match.group("requirement")[:256],
                required_by=match.group("package"),
            )
        )
    if not conflicts:
        conflicts.append(
            DependencyConflict(
                package="environment",
                installed_version="inconsistent",
                requirement="pip check must pass",
                required_by="AstrBot",
            )
        )
    return conflicts


def _write_sbom(
    workspace: Path,
    astrbot_version: str,
    packages: tuple[InstalledPackage, ...],
) -> tuple[Path, str]:
    components = [
        {
            "type": "library",
            "name": package.name,
            "version": package.version,
            "purl": f"pkg:pypi/{canonicalize_name(package.name)}@{package.version}",
        }
        for package in packages
    ]
    component_hash = hashlib.sha256(
        json.dumps(components, ensure_ascii=True, sort_keys=True).encode()
    ).hexdigest()
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.UUID(component_hash[:32])}",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "AstrBot", "version": astrbot_version}
        },
        "components": components,
    }
    content = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    output = workspace / "output"
    output.mkdir(mode=0o700, exist_ok=True)
    path = output / "sbom.cdx.json"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _command_failure(
    started: float,
    command: CommandResult,
    error_code: str,
    requirements_sha256: str,
    log_parts: list[str],
    logs_truncated: bool,
    *,
    core_before_sha256: str = "",
    packages: tuple[InstalledPackage, ...] = (),
    max_log_bytes: int,
) -> InstallProbeOutput:
    code = "runtime_command_timed_out" if command.timed_out else error_code
    status = ProbeStatus.TIMED_OUT if command.timed_out else ProbeStatus.FAILED
    result = InstallResult(
        status=status,
        duration_ms=_duration_ms(started),
        error_code=code,
        message="Runtime dependency installation did not complete",
        pip_check=ProbeResult(
            status=ProbeStatus.SKIPPED,
            error_code="dependency_check_not_run",
            message="Dependency validation was not reached",
        ),
        packages=packages,
        conflicts=(),
        core_before_sha256=core_before_sha256,
    )
    logs, final_truncated = _bounded_logs(log_parts, max_log_bytes)
    return InstallProbeOutput(
        result=result,
        requirements_sha256=requirements_sha256,
        sbom_path=None,
        sbom_sha256="",
        logs=logs,
        logs_truncated=logs_truncated or final_truncated,
    )


def _failure_output(
    started: float,
    error_code: str,
    message: str,
    *,
    requirements_sha256: str,
) -> InstallProbeOutput:
    return InstallProbeOutput(
        result=InstallResult(
            status=ProbeStatus.FAILED,
            duration_ms=_duration_ms(started),
            error_code=error_code,
            message=redact_probe_text(message, maximum=500),
            pip_check=ProbeResult(
                status=ProbeStatus.SKIPPED,
                error_code="dependency_check_not_run",
                message="Dependency validation was not reached",
            ),
        ),
        requirements_sha256=requirements_sha256,
        sbom_path=None,
        sbom_sha256="",
        logs="",
        logs_truncated=False,
    )


def _bounded_logs(parts: list[str], maximum: int) -> tuple[str, bool]:
    value = redact_probe_text("\n".join(parts), maximum=maximum)
    original_size = len("\n".join(parts).encode("utf-8", errors="replace"))
    return value, original_size > len(value.encode("utf-8"))


def _duration_ms(started: float) -> int:
    return min(int((time.monotonic() - started) * 1000), 3_600_000)
