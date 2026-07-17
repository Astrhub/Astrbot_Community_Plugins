from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ...artifacts.runner_contract import (
    FailedPluginRecord,
    MetadataProbeResult,
    ProbeResult,
    ProbeStatus,
    RegistrationProbeResult,
    RuntimeDispatchRequest,
    SmokeResult,
    StartupProbeResult,
)
from .command import redact_probe_text

ASTRBOT_4266_VERSION = "4.26.6"
ASTRBOT_4266_SOURCE_COMMIT = "5d10e0d428b41308cc63215db00359c61ee17195"

_PLUGIN_DIR = re.compile(r"^astrbot_plugin_[a-z0-9][a-z0-9_]{0,95}$")
_HOOK_EVENT_NAMES = {
    "OnAstrBotLoadedEvent",
    "OnPlatformLoadedEvent",
    "OnWaitingLLMRequestEvent",
    "OnLLMRequestEvent",
    "OnLLMResponseEvent",
    "OnAgentBeginEvent",
    "OnAgentDoneEvent",
    "OnDecoratingResultEvent",
    "OnCallingFuncToolEvent",
    "OnUsingLLMToolEvent",
    "OnLLMToolRespondEvent",
    "OnAfterMessageSentEvent",
    "OnPluginErrorEvent",
    "OnPluginLoadedEvent",
    "OnPluginUnloadedEvent",
}


@dataclass(frozen=True, slots=True)
class AstrBotPluginObservation:
    name: str = ""
    version: str = ""
    author: str = ""
    module_loaded: bool = False
    instance_created: bool = False
    initialized: bool = False
    handler_names: tuple[str, ...] = ()
    hook_names: tuple[str, ...] = ()
    llm_tool_names: tuple[str, ...] = ()
    failed_error: str = ""
    failed_traceback: str = ""


@runtime_checkable
class AstrBotLifecycleSession(Protocol):
    async def initialize(self) -> AstrBotPluginObservation: ...

    async def startup(self) -> None: ...

    async def terminate_plugin(self) -> None: ...

    async def close(self) -> None: ...


SessionFactory = Callable[
    [RuntimeDispatchRequest, Path, str],
    AstrBotLifecycleSession,
]


class AstrBotSmokeProbe:
    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self.session_factory = session_factory or build_astrbot_lifecycle_session

    async def execute(
        self,
        request: RuntimeDispatchRequest,
        *,
        runtime_root: Path,
        plugin_dir_name: str,
    ) -> SmokeResult:
        started = time.monotonic()
        session: AstrBotLifecycleSession | None = None
        observation = AstrBotPluginObservation()
        metadata = _skipped_metadata()
        import_probe = _skipped_probe()
        instance = _skipped_probe()
        initialize = _skipped_probe()
        startup = _skipped_startup()
        handlers = _skipped_registration()
        hooks = _skipped_registration()
        llm_tools = _skipped_registration()
        termination = _skipped_probe()
        failed_plugin = FailedPluginRecord(present=False)
        smoke_error = ""
        smoke_message = ""
        try:
            session = self.session_factory(request, runtime_root, plugin_dir_name)
            phase_started = time.monotonic()
            observation = await session.initialize()
            phase_duration = _duration_ms(phase_started)
            if observation.failed_error:
                failure_code = _failed_plugin_error_code(observation.failed_traceback)
                failed_plugin = FailedPluginRecord(
                    present=True,
                    error_code=failure_code,
                    message=_safe_message(observation.failed_error),
                )
                (
                    import_probe,
                    instance,
                    initialize,
                    handlers,
                    hooks,
                    llm_tools,
                ) = _failed_observation_probes(failure_code, phase_duration)
                smoke_error = failure_code
                smoke_message = "AstrBot reported the submitted plugin as failed"
            else:
                metadata = _metadata_result(request, observation, phase_duration)
                import_probe = _boolean_probe(
                    observation.module_loaded,
                    "plugin_import_failed",
                    phase_duration,
                )
                instance = _boolean_probe(
                    observation.instance_created,
                    "plugin_instance_failed",
                    phase_duration,
                )
                initialize = _boolean_probe(
                    observation.initialized,
                    "plugin_initialize_failed",
                    phase_duration,
                )
                handlers = _registration_result(observation.handler_names, phase_duration)
                hooks = _registration_result(observation.hook_names, phase_duration)
                llm_tools = _registration_result(observation.llm_tool_names, phase_duration)
                failed_code = _first_failed_code(
                    metadata,
                    import_probe,
                    instance,
                    initialize,
                    handlers,
                    hooks,
                    llm_tools,
                )
                if failed_code:
                    smoke_error = failed_code
                    smoke_message = "Plugin lifecycle initialization did not pass"

            if not smoke_error:
                startup_started = time.monotonic()
                try:
                    await session.startup()
                except Exception:
                    startup = StartupProbeResult(
                        status=ProbeStatus.FAILED,
                        duration_ms=_duration_ms(startup_started),
                        error_code="plugin_startup_failed",
                        message="Plugin startup hook failed",
                    )
                    smoke_error = startup.error_code
                    smoke_message = "Plugin startup hook did not pass"
                else:
                    startup_duration = _duration_ms(startup_started)
                    startup = StartupProbeResult(
                        status=ProbeStatus.PASSED,
                        duration_ms=startup_duration,
                        ready_ms=startup_duration,
                    )

            termination_started = time.monotonic()
            try:
                await session.terminate_plugin()
            except Exception:
                termination = ProbeResult(
                    status=ProbeStatus.FAILED,
                    duration_ms=_duration_ms(termination_started),
                    error_code="plugin_terminate_failed",
                    message="Plugin termination did not complete",
                )
                smoke_error = smoke_error or termination.error_code
                smoke_message = smoke_message or "Plugin termination did not pass"
            else:
                termination = ProbeResult(
                    status=ProbeStatus.PASSED,
                    duration_ms=_duration_ms(termination_started),
                )
        except Exception:
            import_probe = ProbeResult(
                status=ProbeStatus.FAILED,
                duration_ms=_duration_ms(started),
                error_code="astrbot_lifecycle_failed",
                message="AstrBot lifecycle could not initialize the smoke environment",
            )
            smoke_error = "astrbot_lifecycle_failed"
            smoke_message = "AstrBot lifecycle initialization failed"
        finally:
            if session is not None:
                with suppress(Exception):
                    await session.close()

        status = ProbeStatus.PASSED if not smoke_error else ProbeStatus.FAILED
        return SmokeResult(
            status=status,
            duration_ms=_duration_ms(started),
            metadata=metadata,
            import_probe=import_probe,
            instance=instance,
            initialize=initialize,
            startup=startup,
            handlers=handlers,
            hooks=hooks,
            llm_tools=llm_tools,
            failed_plugin=failed_plugin,
            termination=termination,
            violations=(),
            error_code=smoke_error,
            message=smoke_message,
        )


def build_astrbot_lifecycle_session(
    request: RuntimeDispatchRequest,
    runtime_root: Path,
    plugin_dir_name: str,
) -> AstrBotLifecycleSession:
    if request.target.astrbot_version != ASTRBOT_4266_VERSION:
        raise ValueError("unsupported_astrbot_smoke_adapter")
    if (
        request.target.astrbot_commit
        and request.target.astrbot_commit != ASTRBOT_4266_SOURCE_COMMIT
    ):
        raise ValueError("astrbot_smoke_adapter_commit_mismatch")
    return AstrBot4266LifecycleSession(runtime_root, plugin_dir_name)


class AstrBot4266LifecycleSession:
    def __init__(self, runtime_root: Path, plugin_dir_name: str) -> None:
        self.runtime_root = runtime_root.resolve(strict=True)
        if not _PLUGIN_DIR.fullmatch(plugin_dir_name):
            raise ValueError("invalid_runtime_plugin_directory")
        self.plugin_dir_name = plugin_dir_name
        self.lifecycle: Any | None = None
        self.plugin_metadata: Any | None = None
        self._initialized = False
        self._started = False
        self._terminated = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._previous_environment: dict[str, str | None] = {}
        self._path_inserted = False
        _validate_runtime_root(self.runtime_root, plugin_dir_name)

    async def initialize(self) -> AstrBotPluginObservation:
        if self._initialized:
            raise RuntimeError("astrbot_lifecycle_already_initialized")
        runtime_environment = {
            "ASTRBOT_ROOT": str(self.runtime_root),
            "ASTRBOT_RELOAD": "0",
            "TESTING": "1",
        }
        self._previous_environment = {name: os.environ.get(name) for name in runtime_environment}
        os.environ.update(runtime_environment)
        root_path = str(self.runtime_root)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
            self._path_inserted = True
        before_tasks = set(asyncio.all_tasks())
        installed_version = importlib.metadata.version("AstrBot")
        if installed_version != ASTRBOT_4266_VERSION:
            raise RuntimeError("installed_astrbot_version_mismatch")
        importlib.invalidate_caches()
        from astrbot.core import LogBroker, db_helper
        from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
        from astrbot.core.star.star_handler import star_handlers_registry

        self.lifecycle = AstrBotCoreLifecycle(LogBroker(), db_helper)
        await self.lifecycle.initialize()
        self._initialized = True
        self._background_tasks = set(asyncio.all_tasks()) - before_tasks
        plugin_manager = self.lifecycle.plugin_manager
        failed = plugin_manager.failed_plugin_dict.get(self.plugin_dir_name) or {}
        candidates = [
            item
            for item in plugin_manager.context.get_all_stars()
            if getattr(item, "root_dir_name", "") == self.plugin_dir_name
            and not getattr(item, "reserved", False)
        ]
        metadata = candidates[0] if len(candidates) == 1 else None
        self.plugin_metadata = metadata
        if failed:
            return AstrBotPluginObservation(
                name=str(failed.get("name") or ""),
                version=str(failed.get("version") or ""),
                author=str(failed.get("author") or ""),
                failed_error=_safe_message(str(failed.get("error") or "plugin load failed")),
                failed_traceback=redact_probe_text(
                    str(failed.get("traceback") or ""),
                    maximum=8192,
                ),
            )
        if metadata is None:
            return AstrBotPluginObservation(
                failed_error="AstrBot did not register exactly one submitted plugin",
                failed_traceback="plugin registration missing",
            )
        module_path = str(metadata.module_path or "")
        registered = star_handlers_registry.get_handlers_by_module_name(module_path)
        handlers = tuple(
            sorted(
                handler.handler_full_name
                for handler in registered
                if getattr(handler.event_type, "name", "") == "AdapterMessageEvent"
            )
        )
        hooks = tuple(
            sorted(
                handler.handler_full_name
                for handler in registered
                if getattr(handler.event_type, "name", "") in _HOOK_EVENT_NAMES
            )
        )
        tools = tuple(
            sorted(tool.name for tool in plugin_manager._iter_plugin_llm_tools(module_path))
        )
        return AstrBotPluginObservation(
            name=str(metadata.name or ""),
            version=str(metadata.version or ""),
            author=str(metadata.author or ""),
            module_loaded=metadata.module is not None,
            instance_created=metadata.star_cls is not None,
            initialized=metadata.star_cls is not None,
            handler_names=handlers,
            hook_names=hooks,
            llm_tool_names=tools,
        )

    async def startup(self) -> None:
        if not self._initialized or self.lifecycle is None:
            raise RuntimeError("astrbot_lifecycle_not_initialized")
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        before_tasks = set(asyncio.all_tasks())
        self.lifecycle._load()
        self._started = True
        handlers = star_handlers_registry.get_handlers_by_event_type(EventType.OnAstrBotLoadedEvent)
        for handler in handlers:
            await handler.handler()
        self._background_tasks.update(set(asyncio.all_tasks()) - before_tasks)

    async def terminate_plugin(self) -> None:
        if self.plugin_metadata is None:
            self._terminated = True
            return
        plugin_class = getattr(self.plugin_metadata, "star_cls_type", None)
        if plugin_class is not None and "__del__" in plugin_class.__dict__:
            raise RuntimeError("deprecated_plugin_del_termination_unverifiable")
        await self.lifecycle.plugin_manager._terminate_plugin(self.plugin_metadata)
        self.plugin_metadata.activated = False
        self._terminated = True

    async def close(self) -> None:
        if self.lifecycle is not None and self._initialized:
            if not self._terminated:
                with suppress(Exception):
                    await self.terminate_plugin()
            with suppress(Exception):
                await self.lifecycle.stop()
        for task in self._background_tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._path_inserted:
            with suppress(ValueError):
                sys.path.remove(str(self.runtime_root))
            self._path_inserted = False
        for name, previous in self._previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        self._previous_environment.clear()


def _validate_runtime_root(root: Path, plugin_dir_name: str) -> None:
    plugins_root = root / "data" / "plugins"
    target = plugins_root / plugin_dir_name
    if not target.is_dir() or target.is_symlink():
        raise ValueError("runtime_plugin_directory_missing")
    entries = {
        item.name
        for item in plugins_root.iterdir()
        if item.name not in {"__init__.py", "__pycache__"}
    }
    if entries != {plugin_dir_name}:
        raise ValueError("runtime_plugin_directory_not_isolated")
    (root / "data" / "__init__.py").touch(mode=0o600, exist_ok=True)
    (plugins_root / "__init__.py").touch(mode=0o600, exist_ok=True)


def _metadata_result(
    request: RuntimeDispatchRequest,
    observation: AstrBotPluginObservation,
    duration_ms: int,
) -> MetadataProbeResult:
    if (
        observation.name != request.expected_plugin.name
        or observation.version != request.expected_plugin.version
        or not observation.author
    ):
        return MetadataProbeResult(
            status=ProbeStatus.FAILED,
            duration_ms=duration_ms,
            error_code="plugin_metadata_mismatch",
            message="Loaded plugin metadata differs from the submitted artifact",
            name=_safe_name(observation.name),
            version=_safe_name(observation.version),
            author=_safe_name(observation.author),
        )
    return MetadataProbeResult(
        status=ProbeStatus.PASSED,
        duration_ms=duration_ms,
        name=observation.name,
        version=observation.version,
        author=observation.author,
    )


def _failed_observation_probes(
    error_code: str,
    duration_ms: int,
) -> tuple[
    ProbeResult,
    ProbeResult,
    ProbeResult,
    RegistrationProbeResult,
    RegistrationProbeResult,
    RegistrationProbeResult,
]:
    import_passed = error_code not in {
        "plugin_import_failed",
        "handler_registration_failed",
        "llm_tool_registration_failed",
    }
    instance_passed = import_passed and error_code != "plugin_instance_failed"
    initialize_passed = instance_passed and error_code != "plugin_initialize_failed"
    return (
        _boolean_probe(import_passed, error_code, duration_ms),
        _boolean_probe(instance_passed, error_code, duration_ms),
        _boolean_probe(initialize_passed, error_code, duration_ms),
        _failed_registration(error_code, duration_ms),
        _failed_registration(error_code, duration_ms),
        _failed_registration(error_code, duration_ms),
    )


def _failed_plugin_error_code(traceback_text: str) -> str:
    normalized = traceback_text.lower()
    if "register_llm_tool" in normalized or "llm_tool" in normalized:
        return "llm_tool_registration_failed"
    if (
        "register_command" in normalized
        or "get_handler_or_create" in normalized
        or "star_handlers_registry" in normalized
    ):
        return "handler_registration_failed"
    if "initialize" in normalized:
        return "plugin_initialize_failed"
    if "star_cls_type" in normalized or "plugin_cls(" in normalized:
        return "plugin_instance_failed"
    return "plugin_import_failed"


def _boolean_probe(passed: bool, error_code: str, duration_ms: int) -> ProbeResult:
    if passed:
        return ProbeResult(status=ProbeStatus.PASSED, duration_ms=duration_ms)
    return ProbeResult(
        status=ProbeStatus.FAILED,
        duration_ms=duration_ms,
        error_code=error_code,
        message="Plugin lifecycle phase did not pass",
    )


def _registration_result(names: tuple[str, ...], duration_ms: int) -> RegistrationProbeResult:
    normalized = tuple(sorted(dict.fromkeys(_safe_name(name) for name in names)))
    return RegistrationProbeResult(
        status=ProbeStatus.PASSED,
        duration_ms=duration_ms,
        count=len(normalized),
        names=normalized,
    )


def _failed_registration(error_code: str, duration_ms: int) -> RegistrationProbeResult:
    return RegistrationProbeResult(
        status=ProbeStatus.FAILED,
        duration_ms=duration_ms,
        error_code=error_code,
        message="Plugin registration could not be verified",
        count=0,
        names=(),
    )


def _first_failed_code(*probes: Any) -> str:
    for probe in probes:
        if probe.status != ProbeStatus.PASSED:
            return str(probe.error_code or "plugin_startup_failed")
    return ""


def _skipped_probe() -> ProbeResult:
    return ProbeResult(
        status=ProbeStatus.SKIPPED,
        error_code="probe_not_reached",
        message="Probe phase was not reached",
    )


def _skipped_metadata() -> MetadataProbeResult:
    return MetadataProbeResult(
        status=ProbeStatus.SKIPPED,
        error_code="probe_not_reached",
        message="Metadata probe was not reached",
    )


def _skipped_startup() -> StartupProbeResult:
    return StartupProbeResult(
        status=ProbeStatus.SKIPPED,
        error_code="probe_not_reached",
        message="Startup probe was not reached",
    )


def _skipped_registration() -> RegistrationProbeResult:
    return RegistrationProbeResult(
        status=ProbeStatus.SKIPPED,
        error_code="probe_not_reached",
        message="Registration probe was not reached",
        count=0,
        names=(),
    )


def _safe_name(value: str) -> str:
    return redact_probe_text(str(value or ""), maximum=160)


def _safe_message(value: str) -> str:
    return redact_probe_text(str(value or ""), maximum=500)


def _duration_ms(started: float) -> int:
    return min(int((time.monotonic() - started) * 1000), 3_600_000)
