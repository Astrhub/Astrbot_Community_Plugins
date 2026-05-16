from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .runtime_config import read_runtime_config

DEFAULT_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime.env"
DEFAULT_SITE_ICON_URL = "/logo.webp"
DEFAULT_SITE_NAME = "AstrBot Community Plugins"
DEFAULT_SITE_SUBTITLE = "全新社区插件市场"
DEFAULT_SITE_DESCRIPTION = "发现、评价和提交 AstrBot 插件。"
DEFAULT_SITE_DOCS_URL = "https://docs.astrbot.app/dev/star/plugin.html"
DEFAULT_LOGIN_AGREEMENT_TEXT = ""
DEFAULT_SERVICE_TERMS_TEXT = ""
DEFAULT_EMAIL_PROVIDER = "disabled"


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
    site_name: str
    site_icon_url: str
    site_subtitle: str
    site_description: str
    site_contact_email: str
    site_docs_url: str
    github_login_enabled: bool
    public_login_enabled: bool
    login_agreement_enabled: bool
    login_agreement_text: str
    service_terms_enabled: bool
    service_terms_text: str
    market_submissions_enabled: bool
    market_comments_enabled: bool
    market_likes_enabled: bool
    plugin_auto_approve_enabled: bool
    max_plugin_tags: int
    email_provider: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_ssl: bool
    cloudflare_email_account_id: str
    cloudflare_email_api_token: str
    cloudflare_email_from: str
    email_daily_limit: int
    email_verification_daily_limit_per_user: int
    core_admin_username: str
    core_admin_password_hash: str
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

    def with_updates(self, **changes: object) -> "Settings":
        return replace(self, **changes)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = _normalize_env(os.environ if env is None else env)
    runtime_config_path = source.get("RUNTIME_CONFIG_FILE", str(DEFAULT_RUNTIME_CONFIG_PATH))
    runtime_overrides = _normalize_env(read_runtime_config(runtime_config_path))
    merged = {**runtime_overrides, **source}
    return Settings(
        host=merged.get("HOST", "127.0.0.1"),
        port=_int(merged.get("PORT"), 8787),
        cors_origins=_list(
            merged.get("CORS_ORIGIN", "http://127.0.0.1:3000,http://localhost:3000")
        ),
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
        site_name=merged.get("SITE_NAME", DEFAULT_SITE_NAME),
        site_icon_url=merged.get("SITE_ICON_URL", DEFAULT_SITE_ICON_URL),
        site_subtitle=merged.get("SITE_SUBTITLE", DEFAULT_SITE_SUBTITLE),
        site_description=merged.get("SITE_DESCRIPTION", DEFAULT_SITE_DESCRIPTION),
        site_contact_email=merged.get("SITE_CONTACT_EMAIL", ""),
        site_docs_url=merged.get("SITE_DOCS_URL", DEFAULT_SITE_DOCS_URL),
        github_login_enabled=_bool(merged.get("GITHUB_LOGIN_ENABLED")),
        public_login_enabled=_bool(merged.get("PUBLIC_LOGIN_ENABLED"), default=True),
        login_agreement_enabled=_bool(merged.get("LOGIN_AGREEMENT_ENABLED")),
        login_agreement_text=merged.get("LOGIN_AGREEMENT_TEXT", DEFAULT_LOGIN_AGREEMENT_TEXT),
        service_terms_enabled=_bool(merged.get("SERVICE_TERMS_ENABLED")),
        service_terms_text=merged.get("SERVICE_TERMS_TEXT", DEFAULT_SERVICE_TERMS_TEXT),
        market_submissions_enabled=_bool(merged.get("MARKET_SUBMISSIONS_ENABLED"), default=True),
        market_comments_enabled=_bool(merged.get("MARKET_COMMENTS_ENABLED"), default=True),
        market_likes_enabled=_bool(merged.get("MARKET_LIKES_ENABLED"), default=True),
        plugin_auto_approve_enabled=_bool(merged.get("PLUGIN_AUTO_APPROVE_ENABLED")),
        max_plugin_tags=max(0, _int(merged.get("MAX_PLUGIN_TAGS"), 8)),
        email_provider=_email_provider(merged.get("EMAIL_PROVIDER", DEFAULT_EMAIL_PROVIDER)),
        smtp_host=merged.get("SMTP_HOST", ""),
        smtp_port=_int(merged.get("SMTP_PORT"), 587),
        smtp_username=merged.get("SMTP_USERNAME", ""),
        smtp_password=merged.get("SMTP_PASSWORD", ""),
        smtp_from=merged.get("SMTP_FROM", ""),
        smtp_ssl=_bool(merged.get("SMTP_SSL")),
        cloudflare_email_account_id=merged.get("CLOUDFLARE_EMAIL_ACCOUNT_ID", ""),
        cloudflare_email_api_token=merged.get("CLOUDFLARE_EMAIL_API_TOKEN", ""),
        cloudflare_email_from=merged.get("CLOUDFLARE_EMAIL_FROM", ""),
        email_daily_limit=max(0, _int(merged.get("EMAIL_DAILY_LIMIT"), 0)),
        email_verification_daily_limit_per_user=max(
            0, _int(merged.get("EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER"), 5)
        ),
        core_admin_username=merged.get("CORE_ADMIN_USERNAME", ""),
        core_admin_password_hash=merged.get("CORE_ADMIN_PASSWORD_HASH", ""),
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


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def _email_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in {"disabled", "smtp", "cloudflare"} else DEFAULT_EMAIL_PROVIDER
