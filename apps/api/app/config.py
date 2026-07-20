from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .env_file import read_env_file

DEFAULT_ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_SITE_ICON_URL = "/logo.webp"
DEFAULT_SITE_NAME = "Astrhub 插件市场"
DEFAULT_SITE_SUBTITLE = "全新社区插件市场"
DEFAULT_SITE_DESCRIPTION = "发现、评价和提交 AstrBot 插件。"
DEFAULT_SITE_DOCS_URL = "https://docs.astrbot.app/dev/star/plugin-new.html"
DEFAULT_LOGIN_AGREEMENT_TEXT = ""
DEFAULT_SERVICE_TERMS_TEXT = ""
DEFAULT_EMAIL_PROVIDER = "disabled"
DEFAULT_EMAIL_FROM_NAME = "Astrhub Plugins Market"
DEFAULT_SMTP_AUTH_METHOD = "auto"
DEFAULT_SMTP_ENCRYPTION = "auto"
DEFAULT_GITHUB_METADATA_SYNC_INTERVAL_SECONDS = 60 * 60
MIN_GITHUB_METADATA_SYNC_INTERVAL_SECONDS = 5 * 60
MAX_GITHUB_METADATA_SYNC_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_ARTIFACT_LOCAL_ROOT = "/var/lib/astrbot-market/artifacts"
DEFAULT_ARTIFACT_MAX_UPLOAD_BYTES = 32 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_UNPACKED_BYTES = 128 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ApiKey:
    name: str
    key: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactReviewSettings:
    enabled: bool
    auto_approve_enabled: bool
    runtime_enabled: bool
    runtime_container_image: str
    runtime_result_root: str
    llm_enabled: bool
    llm_config_ref: str
    llm_provider: str
    llm_model: str
    llm_endpoint_url: str
    llm_api_key: str
    clamav_enabled: bool
    clamav_config_ref: str
    clamav_host: str
    clamav_port: int
    yara_enabled: bool
    yara_ruleset_version: str
    yara_ruleset_path: str
    yara_ruleset_source: str
    yara_ruleset_activated_at: str
    dependency_enabled: bool
    dependency_config_ref: str
    dependency_advisory_url: str
    dependency_advisory_path: str
    dependency_api_token: str

    def component_configuration(self) -> dict[str, dict[str, object]]:
        llm = _review_component_configuration(
            self.llm_enabled,
            {
                "llm_config_ref_missing": self.llm_config_ref,
                "llm_provider_missing": self.llm_provider,
                "llm_model_missing": self.llm_model,
                "llm_endpoint_url_missing": self.llm_endpoint_url,
                "llm_api_key_missing": self.llm_api_key,
            },
        )
        if self.llm_enabled and self.llm_provider not in {"openai", "openai-compatible"}:
            llm = _degraded_review_component(llm, "llm_provider_unsupported")
        if (
            self.llm_enabled
            and self.llm_endpoint_url
            and not _valid_llm_endpoint(self.llm_endpoint_url)
        ):
            llm = _degraded_review_component(llm, "llm_endpoint_url_invalid")
        yara = _review_component_configuration(
            self.yara_enabled,
            {
                "yara_ruleset_version_missing": self.yara_ruleset_version,
                "yara_ruleset_path_missing": self.yara_ruleset_path,
                "yara_ruleset_source_missing": self.yara_ruleset_source,
                "yara_ruleset_activation_missing": self.yara_ruleset_activated_at,
            },
        )
        if (
            self.yara_enabled
            and self.yara_ruleset_path
            and not Path(self.yara_ruleset_path).is_absolute()
        ):
            yara = _degraded_review_component(yara, "yara_ruleset_path_invalid")
        if (
            self.yara_enabled
            and self.yara_ruleset_source
            and not _valid_public_identifier(self.yara_ruleset_source)
        ):
            yara = _degraded_review_component(yara, "yara_ruleset_source_invalid")
        if (
            self.yara_enabled
            and self.yara_ruleset_activated_at
            and not _valid_timestamp(self.yara_ruleset_activated_at)
        ):
            yara = _degraded_review_component(yara, "yara_ruleset_activation_invalid")
        runtime = _review_component_configuration(
            self.runtime_enabled,
            {
                "runtime_container_image_missing": self.runtime_container_image,
                "runtime_result_root_missing": self.runtime_result_root,
            },
        )
        if self.runtime_enabled and not _valid_runtime_image_reference(
            self.runtime_container_image
        ):
            runtime = _degraded_review_component(runtime, "runtime_container_image_invalid")
        if self.runtime_enabled and not Path(self.runtime_result_root).is_absolute():
            runtime = _degraded_review_component(runtime, "runtime_result_root_invalid")
        dependency = _review_component_configuration(
            self.dependency_enabled,
            {"dependency_config_ref_missing": self.dependency_config_ref},
        )
        if self.dependency_enabled and not (
            self.dependency_advisory_path or self.dependency_advisory_url
        ):
            dependency = _degraded_review_component(
                dependency,
                "dependency_advisory_source_missing",
            )
        if self.dependency_advisory_path and self.dependency_advisory_url:
            dependency = _degraded_review_component(
                dependency,
                "dependency_advisory_source_ambiguous",
            )
        if self.dependency_advisory_path and not Path(self.dependency_advisory_path).is_absolute():
            dependency = _degraded_review_component(
                dependency,
                "dependency_advisory_path_invalid",
            )
        if self.dependency_advisory_url and not _valid_dependency_endpoint(
            self.dependency_advisory_url
        ):
            dependency = _degraded_review_component(
                dependency,
                "dependency_advisory_url_invalid",
            )
        return {
            "runtime": runtime,
            "llm": llm,
            "clamav": _review_component_configuration(
                self.clamav_enabled,
                {
                    "clamav_config_ref_missing": self.clamav_config_ref,
                    "clamav_host_missing": self.clamav_host,
                },
            ),
            "yara": yara,
            "dependency": dependency,
        }

    def public_status(self) -> dict[str, object]:
        components = self.component_configuration()
        components["policy"] = _review_component_status(
            enabled=self.enabled,
            configured=False,
            ready=False,
            reasons=["active_policy_unknown"] if self.enabled else [],
        )
        return {
            "enabled": self.enabled,
            "configured": False,
            "ready": False,
            "degraded": self.enabled,
            "auto_approve_enabled": self.auto_approve_enabled,
            "components": components,
        }


@dataclass(frozen=True)
class ArtifactSettings:
    enabled: bool
    storage_backend: str
    local_root: str
    cdn_base_url: str
    s3_endpoint_url: str
    s3_region: str
    s3_access_key_id: str
    s3_secret_access_key: str
    quarantine_bucket: str
    published_bucket: str
    max_upload_bytes: int
    max_unpacked_bytes: int
    max_file_bytes: int
    max_files: int
    max_compression_ratio: int
    max_path_depth: int
    submission_rpm: int
    job_lease_seconds: int
    worker_poll_seconds: int
    quarantine_retention_days: int
    review: ArtifactReviewSettings

    def validation_errors(self, database_url: str) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        errors: list[str] = []
        if not database_url:
            errors.append("database_url_missing")
        if not self.cdn_base_url:
            errors.append("cdn_base_url_missing")
        if self.storage_backend == "local":
            if not self.local_root:
                errors.append("local_root_missing")
        elif self.storage_backend == "s3":
            required = {
                "s3_endpoint_url_missing": self.s3_endpoint_url,
                "s3_access_key_id_missing": self.s3_access_key_id,
                "s3_secret_access_key_missing": self.s3_secret_access_key,
                "quarantine_bucket_missing": self.quarantine_bucket,
                "published_bucket_missing": self.published_bucket,
            }
            errors.extend(code for code, value in required.items() if not value)
        else:
            errors.append("storage_backend_invalid")
        return tuple(errors)

    def public_status(self, database_url: str) -> dict[str, object]:
        errors = self.validation_errors(database_url)
        return {
            "enabled": self.enabled,
            "ready": self.enabled and not errors,
            "storage_backend": self.storage_backend,
            "cdn_configured": bool(self.cdn_base_url),
            "database_configured": bool(database_url),
            "configuration_errors": list(errors),
            "limits": {
                "max_upload_bytes": self.max_upload_bytes,
                "max_unpacked_bytes": self.max_unpacked_bytes,
                "max_file_bytes": self.max_file_bytes,
                "max_files": self.max_files,
            },
            "review": self.review.public_status(),
        }


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    cors_origins: tuple[str, ...]
    env_file_path: str
    web_url: str
    github_client_id: str
    github_client_secret: str
    github_callback_url: str
    github_scope: str
    github_admin_org: str
    github_api_token: str
    github_metadata_sync_enabled: bool
    github_metadata_sync_interval_seconds: int
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
    smtp_from_name: str
    smtp_ssl: bool
    smtp_encryption: str
    smtp_auth_method: str
    smtp_validate_certs: bool
    cloudflare_email_account_id: str
    cloudflare_email_api_token: str
    cloudflare_email_from: str
    cloudflare_email_from_name: str
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
    artifacts: ArtifactSettings

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
    env_file_path = source.get("APP_ENV_FILE", str(DEFAULT_ENV_FILE_PATH))
    file_values = _normalize_env(read_env_file(env_file_path))
    merged = {**file_values, **source}
    return Settings(
        host=merged.get("HOST", "127.0.0.1"),
        port=_int(merged.get("PORT"), 8787),
        cors_origins=_list(
            merged.get("CORS_ORIGIN", "http://127.0.0.1:3000,http://localhost:3000")
        ),
        env_file_path=env_file_path,
        web_url=merged.get("WEB_URL", "http://127.0.0.1:8787"),
        github_client_id=merged.get("GITHUB_CLIENT_ID", ""),
        github_client_secret=merged.get("GITHUB_CLIENT_SECRET", ""),
        github_callback_url=merged.get(
            "GITHUB_CALLBACK_URL",
            "http://127.0.0.1:8787/v1/auth/github/callback",
        ),
        github_scope=merged.get("GITHUB_SCOPE", "read:user user:email read:org"),
        github_admin_org=merged.get("GITHUB_ADMIN_ORG", ""),
        github_api_token=merged.get("GITHUB_API_TOKEN", ""),
        github_metadata_sync_enabled=_bool(
            merged.get("GITHUB_METADATA_SYNC_ENABLED"),
            default=True,
        ),
        github_metadata_sync_interval_seconds=_bounded_int(
            merged.get("GITHUB_METADATA_SYNC_INTERVAL_SECONDS"),
            DEFAULT_GITHUB_METADATA_SYNC_INTERVAL_SECONDS,
            MIN_GITHUB_METADATA_SYNC_INTERVAL_SECONDS,
            MAX_GITHUB_METADATA_SYNC_INTERVAL_SECONDS,
        ),
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
        smtp_from_name=merged.get("SMTP_FROM_NAME", DEFAULT_EMAIL_FROM_NAME).strip()
        or DEFAULT_EMAIL_FROM_NAME,
        smtp_ssl=normalize_smtp_encryption(merged.get("SMTP_ENCRYPTION")) == "ssl_tls",
        smtp_encryption=normalize_smtp_encryption(merged.get("SMTP_ENCRYPTION")),
        smtp_auth_method=normalize_smtp_auth_method(
            merged.get("SMTP_AUTH_METHOD", DEFAULT_SMTP_AUTH_METHOD)
        ),
        smtp_validate_certs=_bool(merged.get("SMTP_VALIDATE_CERTS"), default=True),
        cloudflare_email_account_id=merged.get("CLOUDFLARE_EMAIL_ACCOUNT_ID", ""),
        cloudflare_email_api_token=merged.get("CLOUDFLARE_EMAIL_API_TOKEN", ""),
        cloudflare_email_from=merged.get("CLOUDFLARE_EMAIL_FROM", ""),
        cloudflare_email_from_name=merged.get(
            "CLOUDFLARE_EMAIL_FROM_NAME", DEFAULT_EMAIL_FROM_NAME
        ).strip()
        or DEFAULT_EMAIL_FROM_NAME,
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
        artifacts=ArtifactSettings(
            enabled=_bool(merged.get("ARTIFACTS_ENABLED")),
            storage_backend=_choice(
                merged.get("ARTIFACT_STORAGE_BACKEND"),
                {"local", "s3"},
                "local",
            ),
            local_root=merged.get("ARTIFACT_LOCAL_ROOT", DEFAULT_ARTIFACT_LOCAL_ROOT),
            cdn_base_url=merged.get("ARTIFACT_CDN_BASE_URL", "").rstrip("/"),
            s3_endpoint_url=merged.get("ARTIFACT_S3_ENDPOINT_URL", ""),
            s3_region=merged.get("ARTIFACT_S3_REGION", "auto"),
            s3_access_key_id=merged.get("ARTIFACT_S3_ACCESS_KEY_ID", ""),
            s3_secret_access_key=merged.get("ARTIFACT_S3_SECRET_ACCESS_KEY", ""),
            quarantine_bucket=merged.get("ARTIFACT_QUARANTINE_BUCKET", ""),
            published_bucket=merged.get("ARTIFACT_PUBLISHED_BUCKET", ""),
            max_upload_bytes=max(
                1,
                _int(merged.get("ARTIFACT_MAX_UPLOAD_BYTES"), DEFAULT_ARTIFACT_MAX_UPLOAD_BYTES),
            ),
            max_unpacked_bytes=max(
                1,
                _int(
                    merged.get("ARTIFACT_MAX_UNPACKED_BYTES"),
                    DEFAULT_ARTIFACT_MAX_UNPACKED_BYTES,
                ),
            ),
            max_file_bytes=max(
                1,
                _int(merged.get("ARTIFACT_MAX_FILE_BYTES"), DEFAULT_ARTIFACT_MAX_FILE_BYTES),
            ),
            max_files=max(1, _int(merged.get("ARTIFACT_MAX_FILES"), 2000)),
            max_compression_ratio=max(1, _int(merged.get("ARTIFACT_MAX_COMPRESSION_RATIO"), 100)),
            max_path_depth=max(1, _int(merged.get("ARTIFACT_MAX_PATH_DEPTH"), 16)),
            submission_rpm=max(0, _int(merged.get("ARTIFACT_SUBMISSION_RPM"), 6)),
            job_lease_seconds=max(30, _int(merged.get("ARTIFACT_JOB_LEASE_SECONDS"), 300)),
            worker_poll_seconds=max(1, _int(merged.get("ARTIFACT_WORKER_POLL_SECONDS"), 2)),
            quarantine_retention_days=max(
                1, _int(merged.get("ARTIFACT_QUARANTINE_RETENTION_DAYS"), 30)
            ),
            review=ArtifactReviewSettings(
                enabled=_bool(merged.get("ARTIFACT_ADVANCED_REVIEW_ENABLED")),
                auto_approve_enabled=_bool(
                    merged.get("ARTIFACT_AUTO_APPROVE_ENABLED"),
                    default=False,
                ),
                runtime_enabled=_bool(merged.get("ARTIFACT_RUNTIME_REVIEW_ENABLED")),
                runtime_container_image=merged.get("ARTIFACT_RUNTIME_CONTAINER_IMAGE", ""),
                runtime_result_root=merged.get(
                    "ARTIFACT_RUNTIME_RESULT_ROOT",
                    "/var/lib/astrbot-runtime-results",
                ),
                llm_enabled=_bool(merged.get("ARTIFACT_LLM_REVIEW_ENABLED")),
                llm_config_ref=merged.get("ARTIFACT_LLM_CONFIG_REF", "config:llm-default"),
                llm_provider=merged.get("ARTIFACT_LLM_PROVIDER", ""),
                llm_model=merged.get("ARTIFACT_LLM_MODEL", ""),
                llm_endpoint_url=merged.get("ARTIFACT_LLM_ENDPOINT_URL", ""),
                llm_api_key=merged.get("ARTIFACT_LLM_API_KEY", ""),
                clamav_enabled=_bool(merged.get("ARTIFACT_CLAMAV_ENABLED")),
                clamav_config_ref=merged.get(
                    "ARTIFACT_CLAMAV_CONFIG_REF",
                    "config:clamav-default",
                ),
                clamav_host=merged.get("ARTIFACT_CLAMAV_HOST", ""),
                clamav_port=max(1, min(65535, _int(merged.get("ARTIFACT_CLAMAV_PORT"), 3310))),
                yara_enabled=_bool(merged.get("ARTIFACT_YARA_ENABLED")),
                yara_ruleset_version=merged.get("ARTIFACT_YARA_RULESET_VERSION", ""),
                yara_ruleset_path=merged.get("ARTIFACT_YARA_RULESET_PATH", ""),
                yara_ruleset_source=merged.get("ARTIFACT_YARA_RULESET_SOURCE", "deployment"),
                yara_ruleset_activated_at=merged.get(
                    "ARTIFACT_YARA_RULESET_ACTIVATED_AT",
                    "",
                ),
                dependency_enabled=_bool(merged.get("ARTIFACT_DEPENDENCY_REVIEW_ENABLED")),
                dependency_config_ref=merged.get(
                    "ARTIFACT_DEPENDENCY_CONFIG_REF",
                    "config:dependency-default",
                ),
                dependency_advisory_url=merged.get("ARTIFACT_DEPENDENCY_ADVISORY_URL", ""),
                dependency_advisory_path=merged.get(
                    "ARTIFACT_DEPENDENCY_ADVISORY_PATH",
                    "",
                ),
                dependency_api_token=merged.get("ARTIFACT_DEPENDENCY_API_TOKEN", ""),
            ),
        ),
    )


def _review_component_configuration(
    enabled: bool,
    required: Mapping[str, str],
) -> dict[str, object]:
    missing = [code for code, value in required.items() if not str(value or "").strip()]
    return _review_component_status(
        enabled=enabled,
        configured=enabled and not missing,
        ready=False,
        reasons=(missing or ["health_unknown"]) if enabled else [],
    )


def _review_component_status(
    *,
    enabled: bool,
    configured: bool,
    ready: bool,
    reasons: list[str],
) -> dict[str, object]:
    degraded = enabled and not ready
    status = "disabled"
    if enabled:
        status = "ready" if ready else "degraded"
    return {
        "enabled": enabled,
        "configured": configured,
        "ready": ready,
        "degraded": degraded,
        "status": status,
        "reasons": reasons,
    }


def _degraded_review_component(
    component: dict[str, object],
    reason: str,
) -> dict[str, object]:
    reasons = [str(item) for item in component.get("reasons") or []]
    if reason not in reasons:
        reasons.append(reason)
    return {
        **component,
        "configured": False,
        "ready": False,
        "degraded": True,
        "status": "degraded",
        "reasons": reasons,
    }


def _valid_llm_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError:
        return False
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    return bool(
        parsed.scheme in ({"https", "http"} if loopback else {"https"})
        and parsed.hostname
        and parsed.path
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _valid_runtime_image_reference(value: str) -> bool:
    normalized = value.strip()
    if normalized.startswith("sha256:"):
        return bool(re.fullmatch(r"sha256:[a-f0-9]{64}", normalized))
    if normalized.count("@") != 1 or "://" in normalized or "?" in normalized or "#" in normalized:
        return False
    repository, digest = normalized.rsplit("@", 1)
    return bool(
        repository
        and len(repository) <= 255
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", repository)
        and ".." not in repository
        and "//" not in repository
        and re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
    )


def runtime_image_digest(value: str) -> str:
    normalized = value.strip()
    if not _valid_runtime_image_reference(normalized):
        return ""
    return normalized.rsplit("@", 1)[-1]


def _valid_dependency_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError:
        return False
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    ):
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return True
    return address.is_global


def _valid_public_identifier(value: str) -> bool:
    normalized = str(value or "").strip()
    return bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", normalized) and "://" not in normalized
    )


def _valid_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


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


def _choice(value: str | None, choices: set[str], default: str) -> str:
    normalized = str(value or default).strip().lower()
    return normalized if normalized in choices else normalized


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    return min(max(_int(value, default), minimum), maximum)


def _email_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in {"disabled", "smtp", "cloudflare"} else DEFAULT_EMAIL_PROVIDER


def normalize_smtp_encryption(value: str | None) -> str:
    encryption = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "auto": "auto",
        "none": "none",
        "plain": "none",
        "starttls": "starttls",
        "start_tls": "starttls",
        "ssl": "ssl_tls",
        "tls": "ssl_tls",
        "ssl_tls": "ssl_tls",
    }
    if encryption:
        return aliases.get(encryption, DEFAULT_SMTP_ENCRYPTION)
    return DEFAULT_SMTP_ENCRYPTION


def normalize_smtp_auth_method(value: str | None) -> str:
    method = str(value or "").strip().lower().replace("-", "_")
    return method if method in {"auto", "login", "plain", "none"} else DEFAULT_SMTP_AUTH_METHOD
