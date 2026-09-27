from __future__ import annotations

from collections.abc import Mapping

from ..config import Settings, load_settings

MAIL_FIELDS = (
    "email_provider",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_from",
    "smtp_from_name",
    "smtp_encryption",
    "smtp_auth_method",
    "smtp_validate_certs",
    "cloudflare_email_account_id",
    "cloudflare_email_api_token",
    "cloudflare_email_from",
    "cloudflare_email_from_name",
    "web_url",
    "site_name",
)


def notification_settings(settings: Settings, options: Mapping[str, str]) -> Settings:
    """Apply current site mail options without changing worker isolation settings."""
    values = {field.upper(): str(getattr(settings, field)) for field in MAIL_FIELDS}
    values.update({key: value for key, value in options.items() if key in values})
    parsed = load_settings({**values, "APP_ENV_FILE": "/dev/null"})
    return settings.with_updates(**{field: getattr(parsed, field) for field in MAIL_FIELDS})
