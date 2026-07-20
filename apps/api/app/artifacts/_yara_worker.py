from __future__ import annotations

import json
import os
import resource
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(1_048_577))
        if not isinstance(request, dict):
            raise ValueError("request_invalid")
        _apply_limits(request)
        result = _scan(request)
    except Exception as exc:
        result = {
            "status": "error",
            "engine_version": "",
            "matches": [],
            "error_code": _error_code(exc),
            "incomplete_reasons": [],
            "output_truncated": False,
        }
    sys.stdout.write(
        json.dumps(
            result,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    sys.stdout.flush()
    return 2 if result["status"] == "error" else 0


def _scan(request: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import yara
    except ImportError as exc:
        raise RuntimeError("yara_engine_unavailable") from exc

    root = _absolute_path(request.get("root"), None)
    rules_path = _absolute_path(request.get("rules_path"), root)
    source = rules_path.read_text(encoding="utf-8", errors="strict")
    if len(source.encode()) > 2 * 1024 * 1024:
        raise ValueError("yara_rules_too_large")
    targets = request.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 5000:
        raise ValueError("yara_targets_invalid")
    per_file_timeout = _bounded_int(request, "per_file_timeout_seconds", 1, 600)
    max_matches = _bounded_int(request, "max_matches", 1, 5000)
    max_offsets = _bounded_int(request, "max_offsets_per_match", 1, 256)

    with _discard_process_output():
        try:
            rules = yara.compile(source=source, includes=False, error_on_warning=True)
        except Exception as exc:
            raise ValueError("yara_rules_invalid") from exc

        matches: list[dict[str, Any]] = []
        output_truncated = False
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"token", "path"}:
                raise ValueError("yara_target_invalid")
            token = str(target["token"])
            if not token.startswith("f") or not token[1:].isdigit() or len(token) != 7:
                raise ValueError("yara_target_token_invalid")
            path = _absolute_path(target["path"], root)
            try:
                values = rules.match(
                    str(path),
                    timeout=per_file_timeout,
                    console_callback=lambda _message: None,
                )
            except yara.TimeoutError:
                return {
                    "status": "timeout",
                    "engine_version": str(yara.__version__),
                    "matches": matches,
                    "error_code": "yara_scan_timeout",
                    "incomplete_reasons": ["per_file_timeout"],
                    "output_truncated": output_truncated,
                }
            for match in values:
                if len(matches) >= max_matches:
                    output_truncated = True
                    break
                matches.append(
                    {
                        "token": token,
                        "namespace": str(match.namespace),
                        "rule": str(match.rule),
                        "tags": sorted(str(tag) for tag in match.tags)[:32],
                        "offsets": _match_offsets(match, max_offsets),
                    }
                )
            if output_truncated:
                break
    return {
        "status": "matched" if matches else "no_match",
        "engine_version": str(yara.__version__),
        "matches": matches,
        "error_code": "",
        "incomplete_reasons": [],
        "output_truncated": output_truncated,
    }


def _match_offsets(match: Any, limit: int) -> list[int]:
    offsets: set[int] = set()
    for value in match.strings:
        instances = getattr(value, "instances", None)
        if instances is not None:
            for instance in instances:
                offsets.add(int(instance.offset))
                if len(offsets) >= limit:
                    return sorted(offsets)
            continue
        if isinstance(value, tuple) and value:
            offsets.add(int(value[0]))
            if len(offsets) >= limit:
                return sorted(offsets)
    return sorted(offsets)


def _apply_limits(request: Mapping[str, Any]) -> None:
    memory = _bounded_int(request, "memory_bytes", 64 * 1024 * 1024, 4 * 1024**3)
    cpu = _bounded_int(request, "cpu_seconds", 1, 3600)
    output = _bounded_int(request, "max_output_bytes", 256, 16 * 1024 * 1024)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, min(cpu + 1, 3600)))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output * 2, output * 2))


def _bounded_int(request: Mapping[str, Any], name: str, minimum: int, maximum: int) -> int:
    value = request.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name}_invalid")
    return value


def _absolute_path(value: Any, root: Path | None) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise ValueError("path_invalid")
    resolved = path.resolve(strict=True)
    if root is not None:
        resolved_root = root.resolve(strict=True)
        if resolved == resolved_root or resolved_root not in resolved.parents:
            raise ValueError("path_outside_root")
    if not resolved.is_file() and root is not None:
        raise ValueError("path_not_file")
    return resolved


@contextmanager
def _discard_process_output():
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(null)
        os.close(saved_stdout)
        os.close(saved_stderr)


def _error_code(exc: Exception) -> str:
    code = str(exc).strip().lower()
    if code in {
        "yara_engine_unavailable",
        "yara_rules_invalid",
        "yara_rules_too_large",
    }:
        return code
    return "yara_scan_failed"


if __name__ == "__main__":
    raise SystemExit(main())
