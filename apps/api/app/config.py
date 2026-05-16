from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .runtime_config import read_runtime_config

DEFAULT_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime.env"


@dataclass(frozen=True)
class ApiKey:
    name: str
    key: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    cors_origins: tuple[str, ...]
    runtime_config_path: str
    web_url: str
    github_client_id: str
    github_client_secret: str
    github_callback_url: str
    github_scope: str
    github_admin_org: str
    database_url: str
    redis_url: str
    session_cookie_name: str
    oauth_state_cookie_name: str
    cookie_same_site: str
    cookie_secure: bool
    enable_dev_auth: bool
    session_max_age_seconds: int
    api_keys: tuple[ApiKey, ...]

    def is_setup_required(self) -> bool:
        return not self.database_url or not self.redis_url

    def missing_setup_fields(self) -> tuple[str, ...]:
        missing = []
        if not self.database_url:
            missing.append("database_url")
        if not self.redis_url:
            missing.append("redis_url")
        return tuple(missing)

    def with_updates(self, **changes: str) -> "Settings":
        return replace(self, **changes)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = _normalize_env(os.environ if env is None else env)
    runtime_config_path = source.get("RUNTIME_CONFIG_FILE", str(DEFAULT_RUNTIME_CONFIG_PATH))
    runtime_overrides = _normalize_env(read_runtime_config(runtime_config_path))
    merged = {**runtime_overrides, **source}
    return Settings(
        host=merged.get("HOST", "127.0.0.1"),
        port=_int(merged.get("PORT"), 8787),
        cors_origins=_list(merged.get("CORS_ORIGIN", "http://127.0.0.1:3000,http://localhost:3000")),
        runtime_config_path=runtime_config_path,
        web_url=merged.get("WEB_URL", "http://127.0.0.1:8787"),
        github_client_id=merged.get("GITHUB_CLIENT_ID", ""),
        github_client_secret=merged.get("GITHUB_CLIENT_SECRET", ""),
        github_callback_url=merged.get(
            "GITHUB_CALLBACK_URL",
            "http://127.0.0.1:8787/v1/auth/github/callback",
        ),
        github_scope=merged.get("GITHUB_SCOPE", "read:user user:email read:org"),
        github_admin_org=merged.get("GITHUB_ADMIN_ORG", ""),
        database_url=merged.get("DATABASE_URL", ""),
        redis_url=merged.get("REDIS_URL", ""),
        session_cookie_name=merged.get("SESSION_COOKIE_NAME", "astrbot_market_session"),
        oauth_state_cookie_name=merged.get("OAUTH_STATE_COOKIE_NAME", "astrbot_market_oauth_state"),
        cookie_same_site=merged.get("COOKIE_SAME_SITE", "Lax"),
        cookie_secure=_bool(merged.get("COOKIE_SECURE")),
        enable_dev_auth=_bool(merged.get("ENABLE_DEV_AUTH")),
        session_max_age_seconds=_int(merged.get("SESSION_MAX_AGE_SECONDS"), 60 * 60 * 24 * 7),
        api_keys=parse_api_keys(merged.get("MARKET_API_KEYS", "")),
    )


def _normalize_env(env: Mapping[str, str]) -> dict[str, str]:
    return {key: str(value) for key, value in env.items() if str(value).strip() != ""}


def parse_api_keys(value: str) -> tuple[ApiKey, ...]:
    keys: list[ApiKey] = []
    for item in value.split(","):
        raw = item.strip()
        if not raw:
            continue
        name, key, scopes = _split_key(raw)
        keys.append(ApiKey(name=name, key=key, scopes=tuple(filter(None, scopes.split("|")))))
    return tuple(keys)


def _split_key(raw: str) -> tuple[str, str, str]:
    parts = raw.split(":", 2)
    if len(parts) == 1:
        return ("default", parts[0], "market:read")
    if len(parts) == 2:
        return (parts[0], parts[1], "market:read")
    return (parts[0], parts[1], parts[2])


def _list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default
