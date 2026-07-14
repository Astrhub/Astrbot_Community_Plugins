from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.runtime_runner.probe.smoke import (
    ASTRBOT_4265_SOURCE_COMMIT,
    AstrBot4265LifecycleSession,
    AstrBotLifecycleSession,
    AstrBotPluginObservation,
    AstrBotSmokeProbe,
    build_astrbot_lifecycle_session,
)
from tests.runtime_runner_helpers import runtime_request


class FakeSession:
    def __init__(
        self,
        observation: AstrBotPluginObservation,
        *,
        startup_error: bool = False,
        termination_error: bool = False,
    ) -> None:
        self.observation = observation
        self.startup_error = startup_error
        self.termination_error = termination_error
        self.calls: list[str] = []

    async def initialize(self) -> AstrBotPluginObservation:
        self.calls.append("initialize")
        return self.observation

    async def startup(self) -> None:
        self.calls.append("startup")
        if self.startup_error:
            raise RuntimeError("startup failed with secret")

    async def terminate_plugin(self) -> None:
        self.calls.append("terminate")
        if self.termination_error:
            raise RuntimeError("termination failed")

    async def close(self) -> None:
        self.calls.append("close")


def successful_observation(**changes: object) -> AstrBotPluginObservation:
    values = {
        "name": "astrbot_plugin_demo",
        "version": "v1.2.3",
        "author": "Alice",
        "module_loaded": True,
        "instance_created": True,
        "initialized": True,
        "handler_names": ("data.plugins.astrbot_plugin_demo.main_hello",),
        "hook_names": ("data.plugins.astrbot_plugin_demo.main_on_loaded",),
        "llm_tool_names": ("demo_tool",),
    }
    values.update(changes)
    return AstrBotPluginObservation(**values)


def run_probe(session: FakeSession, tmp_path: Path):
    def factory(request, root, name):
        return session

    return asyncio.run(
        AstrBotSmokeProbe(factory).execute(
            runtime_request(),
            runtime_root=tmp_path,
            plugin_dir_name="astrbot_plugin_demo",
        )
    )


def test_smoke_probe_records_real_lifecycle_registration_surfaces(tmp_path: Path) -> None:
    session = FakeSession(successful_observation())
    result = run_probe(session, tmp_path)

    assert result.status.value == "passed"
    assert result.metadata.name == "astrbot_plugin_demo"
    assert result.instance.status.value == "passed"
    assert result.handlers.names == ("data.plugins.astrbot_plugin_demo.main_hello",)
    assert result.hooks.names == ("data.plugins.astrbot_plugin_demo.main_on_loaded",)
    assert result.llm_tools.names == ("demo_tool",)
    assert result.startup.ready_ms is not None
    assert session.calls == ["initialize", "startup", "terminate", "close"]


@pytest.mark.parametrize(
    ("traceback_text", "error_code"),
    [
        ("_import_plugin_with_dependency_recovery ImportError", "plugin_import_failed"),
        ("plugin_cls(context=...)", "plugin_instance_failed"),
        ("await metadata.star_cls.initialize()", "plugin_initialize_failed"),
        ("register_command star_handlers_registry", "handler_registration_failed"),
        ("register_llm_tool", "llm_tool_registration_failed"),
    ],
)
def test_failed_plugin_dict_is_mapped_to_stable_phase_codes(
    tmp_path: Path,
    traceback_text: str,
    error_code: str,
) -> None:
    session = FakeSession(
        AstrBotPluginObservation(
            name="astrbot_plugin_demo",
            version="v1.2.3",
            author="Alice",
            failed_error="load failed Bearer abcdefghijklmnopqrstuvwxyz",
            failed_traceback=traceback_text,
        )
    )
    result = run_probe(session, tmp_path)

    assert result.status.value == "failed"
    assert result.error_code == error_code
    assert result.failed_plugin.present
    assert result.failed_plugin.error_code == error_code
    assert "abcdefghijklmnopqrstuvwxyz" not in result.failed_plugin.message
    assert "startup" not in session.calls
    assert session.calls[-2:] == ["terminate", "close"]


def test_metadata_mismatch_blocks_startup_but_still_terminates(tmp_path: Path) -> None:
    session = FakeSession(successful_observation(version="v9.9.9"))
    result = run_probe(session, tmp_path)

    assert result.error_code == "plugin_metadata_mismatch"
    assert result.metadata.status.value == "failed"
    assert session.calls == ["initialize", "terminate", "close"]


@pytest.mark.parametrize(
    ("startup_error", "termination_error", "error_code"),
    [
        (True, False, "plugin_startup_failed"),
        (False, True, "plugin_terminate_failed"),
    ],
)
def test_startup_and_termination_failures_are_distinct(
    tmp_path: Path,
    startup_error: bool,
    termination_error: bool,
    error_code: str,
) -> None:
    session = FakeSession(
        successful_observation(),
        startup_error=startup_error,
        termination_error=termination_error,
    )
    result = run_probe(session, tmp_path)
    assert result.error_code == error_code
    assert result.termination.status.value == ("failed" if termination_error else "passed")
    assert session.calls[-1] == "close"


def test_source_backed_factory_is_locked_to_version_and_commit(tmp_path: Path) -> None:
    request = runtime_request()
    plugins = tmp_path / "data/plugins/astrbot_plugin_demo"
    plugins.mkdir(parents=True)
    session = build_astrbot_lifecycle_session(
        request,
        tmp_path,
        "astrbot_plugin_demo",
    )
    assert isinstance(session, AstrBot4265LifecycleSession)
    assert isinstance(session, AstrBotLifecycleSession)
    assert ASTRBOT_4265_SOURCE_COMMIT == "adebd2958ed8"

    changed = request.model_copy(
        update={"target": request.target.model_copy(update={"astrbot_version": "4.27.0"})}
    )
    with pytest.raises(ValueError, match="unsupported"):
        build_astrbot_lifecycle_session(changed, tmp_path, "astrbot_plugin_demo")


def test_source_backed_session_requires_exactly_one_non_reserved_plugin(tmp_path: Path) -> None:
    first = tmp_path / "data/plugins/astrbot_plugin_demo"
    second = tmp_path / "data/plugins/astrbot_plugin_other"
    first.mkdir(parents=True)
    second.mkdir()

    with pytest.raises(ValueError, match="not_isolated"):
        AstrBot4265LifecycleSession(tmp_path, "astrbot_plugin_demo")
