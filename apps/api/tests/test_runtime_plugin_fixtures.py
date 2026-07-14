from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

FIXTURE_ROOT = Path(__file__).parent / "fixtures/runtime_plugins"
EXPECTED_SCENARIOS = {
    "pass",
    "dependency_conflict",
    "import_failure",
    "initialize_failure",
    "handler_failure",
    "tool_failure",
    "termination_failure",
}
FORBIDDEN_IMPORTS = {
    "aiohttp",
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {"__import__", "compile", "eval", "exec"}


def test_runtime_fixture_matrix_is_complete_and_local() -> None:
    scenarios = {path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir()}
    assert scenarios == EXPECTED_SCENARIOS


@pytest.mark.parametrize("scenario", sorted(EXPECTED_SCENARIOS))
def test_runtime_fixture_is_minimal_safe_and_metadata_compatible(scenario: str) -> None:
    fixture = FIXTURE_ROOT / scenario
    metadata = yaml.safe_load((fixture / "metadata.yaml").read_text(encoding="utf-8"))
    source = (fixture / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(fixture / "main.py"))

    assert metadata["name"] == "astrbot_plugin_demo"
    assert metadata["version"] == "v1.2.3"
    assert metadata["astrbot_version"] == ">=4.26.5,<4.27.0"
    assert metadata["repo"] == "https://github.com/example/astrbot_plugin_demo"
    assert len(source.encode()) < 4096
    assert not (_import_roots(tree) & FORBIDDEN_IMPORTS)
    assert not (_direct_calls(tree) & FORBIDDEN_CALLS)

    requirements = tuple(fixture.glob("requirements.txt"))
    assert bool(requirements) is (scenario == "dependency_conflict")
    if requirements:
        assert requirements[0].read_text(encoding="utf-8") == "pydantic==1.10.22\n"


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def _direct_calls(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
