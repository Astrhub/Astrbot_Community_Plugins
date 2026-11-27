"""Session token hashing, device metadata parsing, and public session formatting."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .config import Settings

MAX_USER_AGENT_LENGTH = 512
MAX_IP_ADDRESS_LENGTH = 128


def hash_session_token(token: str) -> str:
    """Return the SHA-256 hex digest stored instead of the raw session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_ttl_for(settings: Settings, *, trusted: bool) -> int:
    """Return the fixed session lifetime in seconds for regular or trusted devices."""
    if trusted:
        return max(1, int(settings.trusted_session_max_age_seconds))
    return max(1, int(settings.session_max_age_seconds))


def trusted_request_origins(settings: Settings) -> set[str]:
    """Return normalized scheme+host origins trusted for cookie-authenticated mutations."""
    origins: set[str] = set()
    for candidate in (settings.web_url, *settings.cors_origins):
        parsed = urlsplit(str(candidate or "").strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}".lower())
    return origins


def verify_trusted_mutation_origin(request: Request, settings: Settings) -> None:
    """Reject cross-site browser requests on cookie-authenticated session mutations."""
    allowed = trusted_request_origins(settings)
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.strip().lower() not in allowed:
            raise HTTPException(
                403,
                {"code": "untrusted_origin", "error": "请求来源不受信任"},
            )
        return

    sec_fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_fetch_site == "cross-site":
        raise HTTPException(
            403,
            {"code": "untrusted_origin", "error": "请求来源不受信任"},
        )

    referer = str(request.headers.get("referer") or "").strip()
    if referer:
        parsed = urlsplit(referer)
        referer_origin = (
            f"{parsed.scheme}://{parsed.netloc}".lower()
            if parsed.scheme in {"http", "https"} and parsed.netloc
            else ""
        )
        if referer_origin not in allowed:
            raise HTTPException(
                403,
                {"code": "untrusted_origin", "error": "请求来源不受信任"},
            )


def extract_request_client_ip(request: Request) -> str:
    """Return the direct client host reported by ASGI without trusting spoofable headers."""
    host = request.client.host if request.client and request.client.host else ""
    return str(host).strip()[:MAX_IP_ADDRESS_LENGTH]


def extract_request_user_agent(request: Request) -> str:
    """Return a bounded User-Agent string from the incoming HTTP request."""
    return str(request.headers.get("user-agent") or "").strip()[:MAX_USER_AGENT_LENGTH]


def parse_device_label(user_agent: str | None) -> str:
    """Summarize a User-Agent header into a short readable browser and OS label."""
    raw = str(user_agent or "").strip()
    if not raw:
        return "未知设备"

    lower = raw.lower()
    browser = _detect_browser(lower)
    platform = _detect_platform(lower)

    if browser and platform:
        return f"{browser} · {platform}"
    if browser:
        return browser
    if platform:
        return platform
    return "未知设备"


def _detect_browser(lower_ua: str) -> str:
    if "edg/" in lower_ua or "edga/" in lower_ua or "edgios/" in lower_ua:
        return "Edge"
    if "opr/" in lower_ua or "opera" in lower_ua:
        return "Opera"
    if "firefox/" in lower_ua or "fxios/" in lower_ua:
        return "Firefox"
    if "chrome/" in lower_ua or "crios/" in lower_ua:
        return "Chrome"
    if "safari/" in lower_ua and "chrome/" not in lower_ua and "chromium/" not in lower_ua:
        return "Safari"
    if "curl/" in lower_ua or "httpx" in lower_ua or "python-requests" in lower_ua:
        return "API 客户端"
    return ""


def _detect_platform(lower_ua: str) -> str:
    if "iphone" in lower_ua:
        return "iPhone"
    if "ipad" in lower_ua:
        return "iPad"
    if "android" in lower_ua:
        return "Android"
    if "windows" in lower_ua:
        return "Windows"
    if "mac os x" in lower_ua or "macintosh" in lower_ua:
        return "macOS"
    if "linux" in lower_ua:
        return "Linux"
    return ""


def public_session_info(
    session: Mapping[str, Any],
    *,
    current_token_hash: str = "",
) -> dict[str, Any]:
    """Build the client-safe session representation without exposing secret tokens."""
    token_hash = str(session.get("token_hash") or "")
    device_label = str(session.get("device_label") or "").strip()
    if not device_label:
        device_label = parse_device_label(str(session.get("user_agent") or ""))
    return {
        "id": str(session.get("id") or ""),
        "trusted": bool(session.get("trusted", False)),
        "device_label": device_label,
        "ip_address": str(session.get("ip_address") or ""),
        "created_at": session.get("created_at"),
        "last_seen_at": session.get("last_seen_at"),
        "expires_at": session.get("expires_at"),
        "is_current": bool(current_token_hash and token_hash == current_token_hash),
    }
