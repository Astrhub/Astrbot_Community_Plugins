from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

WORKER_KINDS = frozenset({"artifact_worker", "runtime_runner"})
WORKER_COMPONENTS = {
    "artifact_worker": frozenset({"artifact_worker", "llm", "clamav", "yara", "dependency"}),
    "runtime_runner": frozenset({"runtime"}),
}
COMPONENT_FIELDS = frozenset({"ready", "reason", "version", "data_updated_at"})
METRIC_JOB_STATUSES = frozenset({"queued", "running"})
METRIC_RUN_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "timed_out", "cancelled"}
)

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REASON = re.compile(r"^(?:[a-z0-9][a-z0-9_]{0,95})?$")
_VERSION = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:+/-]{0,159})?$")


def normalize_worker_heartbeat(
    *,
    worker_kind: str,
    worker_id: str,
    components: Mapping[str, Any],
    ttl_seconds: int,
    capacity: int,
    active_count: int,
) -> dict[str, Any]:
    normalized_kind = str(worker_kind or "").strip()
    normalized_id = str(worker_id or "").strip()
    if normalized_kind not in WORKER_KINDS or not _WORKER_ID.fullmatch(normalized_id):
        raise ValueError("review_worker_heartbeat_invalid")
    if not 15 <= int(ttl_seconds) <= 3600:
        raise ValueError("review_worker_heartbeat_invalid")
    if not 0 <= int(capacity) <= 1024 or not 0 <= int(active_count) <= int(capacity):
        raise ValueError("review_worker_heartbeat_invalid")
    allowed_components = WORKER_COMPONENTS[normalized_kind]
    if not isinstance(components, Mapping) or not components:
        raise ValueError("review_worker_heartbeat_invalid")
    normalized_components: dict[str, dict[str, Any]] = {}
    for raw_name, raw_component in components.items():
        name = str(raw_name or "").strip()
        if name not in allowed_components or not isinstance(raw_component, Mapping):
            raise ValueError("review_worker_heartbeat_invalid")
        if set(map(str, raw_component)) - COMPONENT_FIELDS:
            raise ValueError("review_worker_heartbeat_invalid")
        ready = raw_component.get("ready")
        if not isinstance(ready, bool):
            raise ValueError("review_worker_heartbeat_invalid")
        reason = str(raw_component.get("reason") or "").strip()
        version = str(raw_component.get("version") or "").strip()
        if not _REASON.fullmatch(reason) or not _VERSION.fullmatch(version):
            raise ValueError("review_worker_heartbeat_invalid")
        normalized_components[name] = {
            "ready": ready,
            "reason": reason,
            "version": version,
            "data_updated_at": _optional_timestamp(raw_component.get("data_updated_at")),
        }
    return {
        "worker_kind": normalized_kind,
        "worker_id": normalized_id,
        "components": normalized_components,
        "ttl_seconds": int(ttl_seconds),
        "capacity": int(capacity),
        "active_count": int(active_count),
    }


def percentile_cont(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * min(1.0, max(0.0, percentile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _optional_timestamp(value: Any) -> str:
    if value in {None, ""}:
        return ""
    parsed = timestamp(value)
    if parsed is None:
        raise ValueError("review_worker_heartbeat_invalid")
    return parsed.isoformat().replace("+00:00", "Z")
