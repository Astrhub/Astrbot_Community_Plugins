from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolate_runtime_config(tmp_path, monkeypatch) -> None:
    import app.config as config_module

    monkeypatch.setattr(config_module, "DEFAULT_RUNTIME_CONFIG_PATH", tmp_path / "runtime.env")
