from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any

from fastapi import Request

DOWNLOAD_COUNT_WINDOW_SECONDS = 60
_requester_windows: dict[tuple[str, str], float] = {}
_requester_windows_lock = asyncio.Lock()


def build_package_download_url(plugin_id: Any, artifact_id: Any) -> str:
    """Build an opaque, backend-independent package locator."""
    plugin = str(plugin_id or "").strip()
    artifact = str(artifact_id or "").strip()
    if not plugin or not artifact:
        return ""
    return f"/packages/{plugin}/{artifact}"


def requester_hash(request: Request, plugin_id: str) -> str:
    client_ip = request.client.host if request.client and request.client.host else "unknown"
    if client_ip in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        candidate = (
            forwarded.rsplit(",", 1)[-1].strip()
            if forwarded
            else request.headers.get("x-real-ip", "").strip()
        )
        try:
            client_ip = str(ip_address(candidate))
        except ValueError:
            pass
    return hashlib.sha256(f"{plugin_id}\0{client_ip}".encode()).hexdigest()


async def should_count_in_memory(plugin_id: str, requester: str) -> bool:
    now = time.monotonic()
    async with _requester_windows_lock:
        if len(_requester_windows) > 10000:
            cutoff = now - DOWNLOAD_COUNT_WINDOW_SECONDS
            for key, timestamp in list(_requester_windows.items()):
                if timestamp < cutoff:
                    _requester_windows.pop(key, None)
        key = (plugin_id, requester)
        if now - _requester_windows.get(key, -DOWNLOAD_COUNT_WINDOW_SECONDS) < (
            DOWNLOAD_COUNT_WINDOW_SECONDS
        ):
            return False
        _requester_windows[key] = now
        return True


def record_download_success(
    plugins: list[dict[str, Any]],
    plugin_id: str,
) -> bool:
    """Apply a committed download increment to the in-memory store."""
    for plugin in plugins:
        if str(plugin.get("id") or "") == str(plugin_id):
            plugin["download_count"] = int(plugin.get("download_count") or 0) + 1
            return True
    return False


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
