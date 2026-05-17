from __future__ import annotations

from pathlib import Path
from typing import Mapping


def read_env_file(path: str) -> dict[str, str]:
    file_path = Path(path)
    if not file_path.exists():
        return {}

    values: dict[str, str] = {}
    for line in file_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = _unquote(value.strip())
    return values


def write_env_file(path: str, values: Mapping[str, str]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    merged = read_env_file(path)
    for key, value in values.items():
        merged[key] = value

    lines = [f"{key}={_quote(value)}" for key, value in sorted(merged.items())]
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _quote(value: str) -> str:
    if value == "":
        return ""
    if any(char.isspace() for char in value) or any(char in value for char in "#\"'"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value
