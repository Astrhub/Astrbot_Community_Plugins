from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..artifacts.runner_contract import (
    CleanupResult,
    InstallResult,
    NetworkAttestation,
    ProbeStatus,
    SmokeResult,
)
from ..artifacts.sbom import MAX_SBOM_BYTES
from .config import RuntimeRunnerSettings
from .container_executor import ContainerExecutor, InstallExecutionOutput, PreparedRuntime
from .docker_cli import DockerCommandClient, DockerCommandResult
from .execution import RuntimeExecutionError
from .network_policy import DockerNetworkPolicy, NetworkPolicySnapshot
from .queue import RuntimeDispatchWorkItem

_DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_PROBE_MODULE = "app.runtime_runner.probe.entrypoint"


@dataclass(frozen=True, slots=True)
class DockerExecutorConfiguration:
    image_repository: str
    artifact_root: str
    install_network: str
    package_index_url: str
    install_proxy_url: str
    install_proxy_container: str
    orphan_ttl_seconds: int = 7200
    allow_rootful_development: bool = False
    seccomp_profile: str = "builtin"
    apparmor_profile: str = ""

    def __post_init__(self) -> None:
        if (
            not _IMAGE_REPOSITORY.fullmatch(self.image_repository)
            or "@" in self.image_repository
            or ".." in self.image_repository
            or "//" in self.image_repository
        ):
            raise ValueError("runtime_docker_image_repository_invalid")
        if self.image_repository == "local-image-id" and not self.allow_rootful_development:
            raise ValueError("runtime_local_image_requires_development_mode")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", self.artifact_root):
            raise ValueError("runtime_artifact_root_invalid")
        if not _DOCKER_NAME.fullmatch(self.install_network):
            raise ValueError("runtime_install_network_invalid")
        try:
            index = urlsplit(self.package_index_url)
            proxy = urlsplit(self.install_proxy_url)
            index.port
            proxy_port = proxy.port
        except ValueError as exc:
            raise ValueError("runtime_install_network_url_invalid") from exc
        if (
            index.scheme != "https"
            or not index.hostname
            or index.username
            or index.password
            or index.query
            or index.fragment
            or not index.path.rstrip("/").endswith("/simple")
        ):
            raise ValueError("runtime_package_index_url_invalid")
        if (
            proxy.scheme != "http"
            or proxy.hostname != self.install_proxy_container
            or proxy_port is None
            or proxy.username
            or proxy.password
            or proxy.path not in {"", "/"}
            or proxy.query
            or proxy.fragment
        ):
            raise ValueError("runtime_install_proxy_url_invalid")
        if not _DOCKER_NAME.fullmatch(self.install_proxy_container):
            raise ValueError("runtime_install_proxy_container_invalid")
        if self.seccomp_profile != "builtin":
            raise ValueError("runtime_seccomp_profile_invalid")
        if self.apparmor_profile and not _DOCKER_NAME.fullmatch(self.apparmor_profile):
            raise ValueError("runtime_apparmor_profile_invalid")
        if self.orphan_ttl_seconds < 3600 or self.orphan_ttl_seconds > 604800:
            raise ValueError("runtime_orphan_ttl_invalid")

    @classmethod
    def from_runner_settings(
        cls,
        settings: RuntimeRunnerSettings,
    ) -> DockerExecutorConfiguration:
        return cls(
            image_repository=settings.docker_image_repository,
            artifact_root=settings.docker_artifact_root,
            install_network=settings.docker_install_network,
            package_index_url=settings.docker_package_index_url,
            install_proxy_url=settings.docker_install_proxy_url,
            install_proxy_container=settings.docker_install_proxy_container,
            orphan_ttl_seconds=settings.docker_orphan_ttl_seconds,
            allow_rootful_development=settings.docker_allow_rootful_development,
            seccomp_profile=settings.docker_seccomp_profile,
            apparmor_profile=settings.docker_apparmor_profile,
        )


@dataclass(slots=True)
class _DockerRuntimeState:
    dispatch_id: str
    volume_name: str
    image_ref: str
    rootless_engine: bool
    network_policy: NetworkPolicySnapshot
    created_at: int
    container_names: set[str] = field(default_factory=set)


class DockerContainerExecutor(ContainerExecutor):
    def __init__(
        self,
        client: DockerCommandClient,
        configuration: DockerExecutorConfiguration,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.configuration = configuration
        self.network_policy = DockerNetworkPolicy(
            client,
            network_name=configuration.install_network,
            package_index_url=configuration.package_index_url,
            proxy_url=configuration.install_proxy_url,
            proxy_container=configuration.install_proxy_container,
        )
        self._states: dict[str, _DockerRuntimeState] = {}
        self._rootless_engine: bool | None = None
        self._clock = clock

    async def prepare(self, work: RuntimeDispatchWorkItem) -> PreparedRuntime:
        rootless = await self._ensure_engine()
        artifact_path = self._artifact_path(work)
        network_policy = await self.network_policy.verify(work.request.install_network_profile)
        image_ref = self._image_reference(work.request.target.image_digest)
        await self._ensure_image(image_ref, work)
        resource_id = _resource_name(work)
        state = _DockerRuntimeState(
            dispatch_id=work.dispatch_id,
            volume_name=resource_id,
            image_ref=image_ref,
            rootless_engine=rootless,
            network_policy=network_policy,
            created_at=int(self._clock()),
        )
        self._states[resource_id] = state
        try:
            created = await self.client.execute(
                (
                    "volume",
                    "create",
                    "--label",
                    "astrbot.runtime.managed=true",
                    "--label",
                    f"astrbot.runtime.dispatch={work.dispatch_id}",
                    "--label",
                    f"astrbot.runtime.attempt={work.attempt}",
                    "--label",
                    f"astrbot.runtime.created-at={state.created_at}",
                    resource_id,
                ),
                timeout_seconds=20,
            )
            _require_success(created, "runtime_volume_create_failed")
            await self._stage_artifact(state, work, artifact_path)
            version = await self._probe_python_version(state, work)
        except Exception:
            await self._remove_volume(resource_id)
            self._states.pop(resource_id, None)
            raise
        return PreparedRuntime(
            dispatch_id=work.dispatch_id,
            resource_id=resource_id,
            resolved_python_version=version,
        )

    async def install(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> InstallExecutionOutput:
        state = self._state(prepared, work)
        name = f"{state.volume_name}-install"
        state.container_names.add(name)
        command = self._phase_run_argv(
            state,
            work,
            name=name,
            network=self.configuration.install_network,
            command=("/usr/local/bin/python", "-m", _PROBE_MODULE, "install"),
        )
        executed = await self.client.execute(
            command,
            stdin=work.request.model_dump_json().encode(),
            timeout_seconds=work.request.limits.timeout_seconds + 10,
            max_output_bytes=work.request.limits.max_log_bytes,
        )
        _require_success(executed, "runtime_install_container_failed")
        payload = await self._read_result(state, work, "install")
        try:
            result = InstallResult.model_validate_json(payload)
        except ValidationError as exc:
            raise RuntimeExecutionError(
                "runtime_result_invalid",
                "Install container returned an invalid structured result",
            ) from exc
        sbom = None
        if result.sbom_sha256 is not None:
            sbom = await self._read_private_object(
                state,
                work,
                phase="sbom",
                maximum=MAX_SBOM_BYTES,
            )
        try:
            return InstallExecutionOutput(result=result, sbom=sbom)
        except ValueError as exc:
            raise RuntimeExecutionError(
                "runtime_sbom_invalid",
                "Install container returned an invalid SBOM sidecar",
            ) from exc

    async def smoke(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> SmokeResult:
        state = self._state(prepared, work)
        name = f"{state.volume_name}-smoke"
        state.container_names.add(name)
        command = self._phase_run_argv(
            state,
            work,
            name=name,
            network="none",
            command=("/usr/local/bin/python", "-m", _PROBE_MODULE, "smoke"),
        )
        executed = await self.client.execute(
            command,
            stdin=work.request.model_dump_json().encode(),
            timeout_seconds=work.request.limits.timeout_seconds + 10,
            max_output_bytes=work.request.limits.max_log_bytes,
        )
        _require_success(executed, "runtime_smoke_container_failed")
        payload = await self._read_result(state, work, "smoke")
        try:
            return SmokeResult.model_validate_json(payload)
        except ValidationError as exc:
            raise RuntimeExecutionError(
                "runtime_result_invalid",
                "Smoke container returned an invalid structured result",
            ) from exc

    async def attest(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> NetworkAttestation:
        state = self._state(prepared, work)
        snapshot = state.network_policy
        production_isolation = state.rootless_engine
        return NetworkAttestation.model_validate(
            {
                "status": "passed" if production_isolation else "unknown",
                "backend": snapshot.backend,
                "install_profile": snapshot.profile,
                "smoke_profile": work.request.smoke_network_profile,
                "install_egress_enforced": snapshot.install_egress_enforced,
                "private_network_blocked": snapshot.private_network_blocked,
                "metadata_endpoint_blocked": snapshot.metadata_endpoint_blocked,
                "smoke_network_disabled": snapshot.smoke_network_disabled,
                "violations": [],
                "error_code": "" if production_isolation else "runtime_rootless_unverified",
            }
        )

    async def cleanup(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> CleanupResult:
        state = self._states.get(prepared.resource_id)
        if state is None:
            return CleanupResult(status=ProbeStatus.PASSED)
        if state.dispatch_id != work.dispatch_id:
            raise RuntimeExecutionError(
                "runtime_prepare_identity_mismatch",
                "Prepared Docker runtime does not match the dispatch",
            )
        removed_containers = 0
        leaked: list[str] = []
        for name in sorted(state.container_names):
            success, removed = await self._remove_container(name)
            removed_containers += int(removed)
            if not success:
                leaked.append(name)
        volume_success, removed_volume = await self._remove_volume(state.volume_name)
        if not volume_success:
            leaked.append(state.volume_name)
        if leaked:
            return CleanupResult(
                status=ProbeStatus.FAILED,
                error_code="runtime_cleanup_failed",
                message="Runtime resources could not be confirmed as removed",
                removed_containers=removed_containers,
                removed_volumes=int(removed_volume),
                leaked_resources=tuple(leaked),
            )
        self._states.pop(state.volume_name, None)
        return CleanupResult(
            status=ProbeStatus.PASSED,
            removed_containers=removed_containers,
            removed_volumes=int(removed_volume),
        )

    async def abort(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> None:
        state = self._state(prepared, work)
        for name in sorted(state.container_names):
            await self._remove_container(name)

    async def cleanup_orphans(self) -> int:
        active = {
            name
            for state in self._states.values()
            for name in (state.volume_name, *state.container_names)
        }
        now = int(self._clock())
        removed = 0
        invalid = False
        for kind in ("container", "volume"):
            names = await self._managed_resource_names(kind)
            for name in names:
                if name in active:
                    continue
                labels = await self._resource_labels(kind, name)
                try:
                    created_at = int(labels["astrbot.runtime.created-at"])
                except (KeyError, ValueError):
                    invalid = True
                    continue
                if created_at > now + 60:
                    invalid = True
                    continue
                if now - created_at < self.configuration.orphan_ttl_seconds:
                    continue
                outcome = (
                    await self._remove_container(name)
                    if kind == "container"
                    else await self._remove_volume(name)
                )
                success, was_removed = outcome
                removed += int(was_removed)
                invalid = invalid or not success
        if invalid:
            raise RuntimeExecutionError(
                "runtime_orphan_cleanup_failed",
                "One or more managed runtime resources could not be reconciled",
            )
        return removed

    async def close(self) -> None:
        await self.client.close()

    async def _ensure_engine(self) -> bool:
        if self._rootless_engine is not None:
            return self._rootless_engine
        result = await self.client.execute(
            ("info", "--format", "{{json .SecurityOptions}}"),
            timeout_seconds=15,
        )
        _require_success(result, "runtime_engine_unavailable")
        try:
            options = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeExecutionError(
                "runtime_engine_unverified",
                "Container engine security options could not be verified",
            ) from exc
        if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
            raise RuntimeExecutionError(
                "runtime_engine_unverified",
                "Container engine security options could not be verified",
            )
        rootless = any("rootless" in item.casefold() for item in options)
        seccomp = any("seccomp" in item.casefold() for item in options)
        if not seccomp:
            raise RuntimeExecutionError(
                "runtime_engine_unverified",
                "Container engine does not report seccomp enforcement",
            )
        if not rootless and not self.configuration.allow_rootful_development:
            raise RuntimeExecutionError(
                "runtime_rootless_required",
                "Production runtime execution requires a rootless container engine",
            )
        self._rootless_engine = rootless
        return rootless

    async def _ensure_image(self, image_ref: str, work: RuntimeDispatchWorkItem) -> None:
        result = await self.client.execute(
            ("image", "inspect", image_ref, "--format", "{{json .}}"),
            timeout_seconds=20,
            max_output_bytes=2 * 1024 * 1024,
        )
        _require_success(result, "runtime_image_unavailable")
        try:
            image = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeExecutionError(
                "runtime_image_unverified",
                "Runtime image metadata could not be verified",
            ) from exc
        if not isinstance(image, dict):
            raise RuntimeExecutionError(
                "runtime_image_unverified",
                "Runtime image metadata could not be verified",
            )
        digest = work.request.target.image_digest
        repo_digests = image.get("RepoDigests") if isinstance(image, dict) else None
        identities = {str(image.get("Id") or "")} if isinstance(image, dict) else set()
        if isinstance(repo_digests, list):
            identities.update(str(item) for item in repo_digests)
        if not any(
            identity == digest or identity.endswith(f"@{digest}") for identity in identities
        ):
            raise RuntimeExecutionError(
                "runtime_image_digest_mismatch",
                "Runtime image identity does not match the pinned digest",
            )
        expected_arch = work.request.target.platform.split("/", 1)[1]
        if image.get("Os") != "linux" or image.get("Architecture") != expected_arch:
            raise RuntimeExecutionError(
                "runtime_image_platform_mismatch",
                "Runtime image platform does not match the dispatch target",
            )

    async def _probe_python_version(
        self,
        state: _DockerRuntimeState,
        work: RuntimeDispatchWorkItem,
    ) -> str:
        name = f"{state.volume_name}-version"
        state.container_names.add(name)
        argv = self._base_run_argv(
            state,
            work,
            name=name,
            network="none",
            remove=True,
            writable_volume=False,
        )
        argv.extend(
            (
                state.image_ref,
                "/usr/local/bin/python",
                "-c",
                "import platform; print(platform.python_version())",
            )
        )
        result = await self.client.execute(tuple(argv), timeout_seconds=30)
        _require_success(result, "runtime_python_probe_failed")
        version = result.stdout.strip()
        target = work.request.target.python_version
        if not re.fullmatch(r"^[0-9]+\.[0-9]+\.[0-9]+$", version) or not version.startswith(
            f"{target}." if target.count(".") == 1 else target
        ):
            raise RuntimeExecutionError(
                "runtime_python_version_mismatch",
                "Runtime image Python version does not match the dispatch target",
            )
        state.container_names.discard(name)
        return version

    async def _stage_artifact(
        self,
        state: _DockerRuntimeState,
        work: RuntimeDispatchWorkItem,
        artifact_path: Path,
    ) -> None:
        name = f"{state.volume_name}-stage"
        state.container_names.add(name)
        argv = self._base_run_argv(
            state,
            work,
            name=name,
            network="none",
            remove=False,
            writable_volume=True,
            user="0:0",
        )
        argv[0] = "create"
        argv.extend(
            (
                "--workdir",
                "/runtime",
                "--mount",
                f"type=volume,src={state.volume_name},dst=/runtime",
                state.image_ref,
                "/usr/local/bin/python",
                "-m",
                _PROBE_MODULE,
                "stage",
            )
        )
        created = await self.client.execute(tuple(argv), timeout_seconds=30)
        _require_success(created, "runtime_artifact_stage_failed")
        copied = await self.client.execute(
            ("container", "cp", str(artifact_path), f"{name}:/runtime/input/artifact.zip"),
            timeout_seconds=work.request.limits.timeout_seconds,
            max_output_bytes=work.request.limits.max_log_bytes,
        )
        _require_success(copied, "runtime_artifact_stage_failed")
        started = await self.client.execute(
            ("container", "start", "--attach", name),
            timeout_seconds=30,
            max_output_bytes=work.request.limits.max_log_bytes,
        )
        _require_success(started, "runtime_artifact_stage_failed")

    async def _read_result(
        self,
        state: _DockerRuntimeState,
        work: RuntimeDispatchWorkItem,
        phase: str,
    ) -> str:
        name = f"{state.volume_name}-{phase}-result"
        state.container_names.add(name)
        argv = self._base_run_argv(
            state,
            work,
            name=name,
            network="none",
            remove=True,
            writable_volume=False,
        )
        argv.extend(
            (
                "--mount",
                f"type=volume,src={state.volume_name},dst=/runtime,readonly,volume-nocopy",
                state.image_ref,
                "/usr/local/bin/python",
                "-m",
                _PROBE_MODULE,
                "emit",
                phase,
            )
        )
        result = await self.client.execute(
            tuple(argv),
            timeout_seconds=30,
            max_output_bytes=work.request.limits.max_result_bytes,
        )
        _require_success(result, "runtime_result_unavailable")
        if result.truncated or not result.stdout:
            raise RuntimeExecutionError(
                "runtime_result_invalid",
                "Runtime result exceeded its contract boundary",
            )
        state.container_names.discard(name)
        return result.stdout

    async def _read_private_object(
        self,
        state: _DockerRuntimeState,
        work: RuntimeDispatchWorkItem,
        *,
        phase: str,
        maximum: int,
    ) -> bytes:
        name = f"{state.volume_name}-{phase}-object"
        state.container_names.add(name)
        argv = self._base_run_argv(
            state,
            work,
            name=name,
            network="none",
            remove=True,
            writable_volume=False,
        )
        argv.extend(
            (
                "--mount",
                f"type=volume,src={state.volume_name},dst=/runtime,readonly,volume-nocopy",
                state.image_ref,
                "/usr/local/bin/python",
                "-m",
                _PROBE_MODULE,
                "emit",
                phase,
            )
        )
        emitted = await self.client.execute(
            tuple(argv),
            timeout_seconds=30,
            max_output_bytes=maximum,
        )
        _require_success(emitted, "runtime_private_object_unavailable")
        content = emitted.stdout.encode("utf-8")
        if emitted.truncated or not content or len(content) > maximum:
            raise RuntimeExecutionError(
                "runtime_private_object_invalid",
                "Runtime private object exceeded its bounded output contract",
            )
        state.container_names.discard(name)
        return content

    def _phase_run_argv(
        self,
        state: _DockerRuntimeState,
        work: RuntimeDispatchWorkItem,
        *,
        name: str,
        network: str,
        command: tuple[str, ...],
    ) -> tuple[str, ...]:
        argv = self._base_run_argv(
            state,
            work,
            name=name,
            network=network,
            remove=False,
            writable_volume=True,
        )
        argv.extend(
            (
                "--interactive",
                "--workdir",
                "/runtime",
                "--mount",
                f"type=volume,src={state.volume_name},dst=/runtime",
            )
        )
        if network == self.configuration.install_network:
            argv.extend(
                (
                    "--env",
                    f"PIP_INDEX_URL={self.configuration.package_index_url}",
                    "--env",
                    f"HTTP_PROXY={self.configuration.install_proxy_url}",
                    "--env",
                    f"HTTPS_PROXY={self.configuration.install_proxy_url}",
                    "--env",
                    "NO_PROXY=",
                    "--env",
                    "PIP_NO_CACHE_DIR=1",
                )
            )
        argv.extend((state.image_ref, *command))
        return tuple(argv)

    def _base_run_argv(
        self,
        state: _DockerRuntimeState,
        work: RuntimeDispatchWorkItem,
        *,
        name: str,
        network: str,
        remove: bool,
        writable_volume: bool,
        user: str = "65532:65532",
    ) -> list[str]:
        limits = work.request.limits
        memory = f"{limits.memory_mb}m"
        argv = [
            "run",
            "--name",
            name,
            "--pull",
            "never",
            "--platform",
            work.request.target.platform,
            "--network",
            network,
            "--user",
            user,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            f"seccomp={self.configuration.seccomp_profile}",
            "--pids-limit",
            str(limits.pids),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            format(limits.cpu, "g"),
            "--ipc",
            "none",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            "nofile=1024:1024",
            "--stop-timeout",
            "5",
            "--tmpfs",
            (f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m,uid=65532,gid=65532,mode=700"),
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PYTHONNOUSERSITE=1",
            "--env",
            "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--env",
            "PIP_NO_INPUT=1",
            "--label",
            "astrbot.runtime.managed=true",
            "--label",
            f"astrbot.runtime.dispatch={work.dispatch_id}",
            "--label",
            f"astrbot.runtime.resource={state.volume_name}",
            "--label",
            f"astrbot.runtime.created-at={state.created_at}",
        ]
        if remove:
            argv.append("--rm")
        if writable_volume:
            argv.extend(("--env", "PYTHONPATH=/opt/runtime-probe"))
        if self.configuration.apparmor_profile:
            argv.extend(
                (
                    "--security-opt",
                    f"apparmor={self.configuration.apparmor_profile}",
                )
            )
        return argv

    def _image_reference(self, digest: str) -> str:
        if self.configuration.image_repository == "local-image-id":
            return digest
        return f"{self.configuration.image_repository}@{digest}"

    def _artifact_path(self, work: RuntimeDispatchWorkItem) -> Path:
        root = Path(self.configuration.artifact_root)
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeExecutionError(
                "runtime_artifact_source_unavailable",
                "Runtime artifact source is unavailable",
            ) from exc
        if root.is_symlink() or not resolved_root.is_dir():
            raise RuntimeExecutionError(
                "runtime_artifact_source_unavailable",
                "Runtime artifact source is unavailable",
            )
        candidate = resolved_root.joinpath(*work.request.quarantine_key.split("/"))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise RuntimeExecutionError(
                "runtime_artifact_source_unavailable",
                "Runtime artifact source is unavailable",
            ) from exc
        if candidate != resolved or candidate.is_symlink() or not candidate.is_file():
            raise RuntimeExecutionError(
                "runtime_artifact_source_unavailable",
                "Runtime artifact source is unavailable",
            )
        if candidate.stat().st_size != work.request.artifact_size_bytes:
            raise RuntimeExecutionError(
                "runtime_artifact_size_mismatch",
                "Runtime artifact size does not match the dispatch",
            )
        if _sha256_path(candidate) != work.request.artifact_sha256:
            raise RuntimeExecutionError(
                "runtime_artifact_hash_mismatch",
                "Runtime artifact hash does not match the dispatch",
            )
        return candidate

    def _state(
        self,
        prepared: PreparedRuntime,
        work: RuntimeDispatchWorkItem,
    ) -> _DockerRuntimeState:
        state = self._states.get(prepared.resource_id)
        if state is None or state.dispatch_id != work.dispatch_id:
            raise RuntimeExecutionError(
                "runtime_prepare_identity_mismatch",
                "Prepared Docker runtime does not match the dispatch",
            )
        return state

    async def _managed_resource_names(self, kind: str) -> tuple[str, ...]:
        argv = [kind, "ls"]
        if kind == "container":
            argv.append("--all")
        argv.extend(
            (
                "--filter",
                "label=astrbot.runtime.managed=true",
                "--format",
                "{{.Name}}" if kind == "volume" else "{{.Names}}",
            )
        )
        result = await self.client.execute(
            tuple(argv),
            timeout_seconds=30,
            max_output_bytes=1024 * 1024,
        )
        _require_success(result, "runtime_orphan_cleanup_failed")
        names = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        if len(names) > 10_000 or any(not _DOCKER_NAME.fullmatch(name) for name in names):
            raise RuntimeExecutionError(
                "runtime_orphan_cleanup_failed",
                "Managed runtime resource listing is invalid",
            )
        return names

    async def _resource_labels(self, kind: str, name: str) -> dict[str, str]:
        result = await self.client.execute(
            (kind, "inspect", name, "--format", "{{json .Config.Labels}}")
            if kind == "container"
            else (kind, "inspect", name, "--format", "{{json .Labels}}"),
            timeout_seconds=20,
            max_output_bytes=256 * 1024,
        )
        _require_success(result, "runtime_orphan_cleanup_failed")
        try:
            labels = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeExecutionError(
                "runtime_orphan_cleanup_failed",
                "Managed runtime resource labels are invalid",
            ) from exc
        if (
            not isinstance(labels, dict)
            or labels.get("astrbot.runtime.managed") != "true"
            or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
            )
        ):
            raise RuntimeExecutionError(
                "runtime_orphan_cleanup_failed",
                "Managed runtime resource labels are invalid",
            )
        return labels

    async def _remove_container(self, name: str) -> tuple[bool, bool]:
        result = await self.client.execute(
            ("container", "rm", "--force", name),
            timeout_seconds=30,
        )
        if result.succeeded:
            return True, bool(result.stdout.strip())
        if "No such container" in result.stderr:
            return True, False
        return False, False

    async def _remove_volume(self, name: str) -> tuple[bool, bool]:
        result = await self.client.execute(
            ("volume", "rm", "--force", name),
            timeout_seconds=30,
        )
        if result.succeeded:
            return True, bool(result.stdout.strip())
        if "no such volume" in result.stderr.casefold():
            return True, False
        return False, False


def _resource_name(work: RuntimeDispatchWorkItem) -> str:
    identity = hashlib.sha256(work.dispatch_id.encode()).hexdigest()[:12]
    return f"astrbot-rt-{identity}-a{work.attempt}-{secrets.token_hex(4)}"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_success(result: DockerCommandResult, error_code: str) -> None:
    if result.timed_out:
        raise RuntimeExecutionError(
            "runtime_command_timed_out",
            "Container engine command exceeded its configured timeout",
        )
    if not result.succeeded:
        raise RuntimeExecutionError(
            error_code,
            "Container engine command did not complete successfully",
        )
