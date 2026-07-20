from __future__ import annotations

import ast
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from app.runtime_runner.probe.smoke import (
    ASTRBOT_4266_SOURCE_COMMIT,
    ASTRBOT_4266_VERSION,
)

SOURCE_VALUE = os.environ.get("ASTRBOT_SOURCE_PATH", "")
SOURCE_PATH = Path(SOURCE_VALUE)
SOURCE_AVAILABLE = bool(SOURCE_VALUE) and (SOURCE_PATH / "pyproject.toml").is_file()
pytestmark = pytest.mark.skipif(
    not SOURCE_AVAILABLE,
    reason="ASTRBOT_SOURCE_PATH does not point to an AstrBot source checkout",
)


def test_astrbot_source_snapshot_matches_runtime_adapter() -> None:
    project = tomllib.loads((SOURCE_PATH / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    commit = subprocess.run(
        ["git", "-C", str(SOURCE_PATH), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert project["version"] == ASTRBOT_4266_VERSION
    assert project["requires-python"] == ">=3.12"
    assert commit == ASTRBOT_4266_SOURCE_COMMIT


def test_astrbot_source_keeps_required_lifecycle_contract() -> None:
    lifecycle = _class_contract(
        SOURCE_PATH / "astrbot/core/core_lifecycle.py",
        "AstrBotCoreLifecycle",
    )
    manager = _class_contract(
        SOURCE_PATH / "astrbot/core/star/star_manager.py",
        "PluginManager",
    )

    assert {"initialize", "_load", "stop"} <= lifecycle.methods
    assert {"load", "_terminate_plugin", "_iter_plugin_llm_tools"} <= manager.methods
    assert {"failed_plugin_dict", "star_handler_full_names"} <= manager.attributes


class _ClassContract:
    def __init__(self, methods: set[str], attributes: set[str]) -> None:
        self.methods = methods
        self.attributes = attributes


def _class_contract(path: Path, class_name: str) -> _ClassContract:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    attributes = {node.attr for node in ast.walk(class_node) if isinstance(node, ast.Attribute)}
    return _ClassContract(methods, attributes)
