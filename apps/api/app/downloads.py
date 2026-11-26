from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_package_download_url(plugin_id: Any, artifact_id: Any) -> str:
    """Build an opaque, backend-independent package locator."""
    plugin = str(plugin_id or "").strip()
    artifact = str(artifact_id or "").strip()
    if not plugin or not artifact:
        return ""
    return f"/packages/{plugin}/{artifact}"


def record_download_success(
    plugins: list[dict[str, Any]],
    plugin_id: str,
) -> None:
    """Apply a committed PostgreSQL download increment to the in-memory store."""
    for plugin in plugins:
        if str(plugin.get("id") or "") == str(plugin_id):
            plugin["download_count"] = int(plugin.get("download_count") or 0) + 1
            return


def normalize_publication_download_url(
    publication: Mapping[str, Any],
    web_url: str,
) -> str:
    """Resolve the current publication without trusting legacy backend URLs."""
    artifact_id = str(publication.get("id") or "").strip()
    plugin_id = str(publication.get("plugin_id") or "").strip()
    if not artifact_id:
        return ""
    locator = build_package_download_url(plugin_id, artifact_id)
    if not locator:
        return ""
    return web_url.rstrip("/") + locator
