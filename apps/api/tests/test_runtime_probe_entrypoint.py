from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from app.runtime_runner.probe.entrypoint import ProbeEntrypointError, _extract_archive


def test_runtime_probe_safely_extracts_minimal_plugin(tmp_path: Path) -> None:
    archive = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("wrapper/metadata.yaml", "name: astrbot_plugin_demo\n")
        bundle.writestr("wrapper/main.py", "PLUGIN = True\n")
    output = tmp_path / "output"
    output.mkdir()

    with zipfile.ZipFile(archive) as bundle:
        _extract_archive(bundle, output)

    assert (output / "wrapper/metadata.yaml").read_text() == "name: astrbot_plugin_demo\n"
    assert stat.S_IMODE((output / "wrapper/main.py").stat().st_mode) == 0o600


@pytest.mark.parametrize("entry", ["../escape.py", "/absolute.py", "safe\\windows.py"])
def test_runtime_probe_rejects_unsafe_archive_paths(tmp_path: Path, entry: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(entry, "unsafe")
    output = tmp_path / "output"
    output.mkdir()

    with zipfile.ZipFile(archive) as bundle:
        with pytest.raises(ProbeEntrypointError, match="unsafe"):
            _extract_archive(bundle, output)


def test_runtime_probe_rejects_zip_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("wrapper/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(link, "../../outside")
    output = tmp_path / "output"
    output.mkdir()

    with zipfile.ZipFile(archive) as bundle:
        with pytest.raises(ProbeEntrypointError, match="symlink"):
            _extract_archive(bundle, output)
