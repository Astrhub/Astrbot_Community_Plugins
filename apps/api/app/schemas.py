from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import DEFAULT_EMAIL_FROM_NAME, normalize_smtp_auth_method, normalize_smtp_encryption


class PluginSubmission(BaseModel):
    name: str
    display_name: str | None = None
    desc: str
    author: str
    repo: str
    social_link: str = ""
    category: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator(
        "name",
        "display_name",
        "desc",
        "author",
        "repo",
        "social_link",
        "category",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str]:
        return [str(tag).strip() for tag in value or [] if str(tag).strip()]


class PluginSubmissionMetadataPreviewPayload(BaseModel):
    repo: str

    @field_validator("repo", mode="before")
    @classmethod
    def strip_repo(cls, value: str | None) -> str:
        return str(value or "").strip()


class PluginPatch(BaseModel):
    name: str | None = None
    display_name: str | None = None
    desc: str | None = None
    author: str | None = None
    repo: str | None = None
    social_link: str | None = None
    category: str | None = None
    tags: list[str] | None = None

    @field_validator(
        "name",
        "display_name",
        "desc",
        "author",
        "repo",
        "social_link",
        "category",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip()


class PluginUnlistPayload(BaseModel):
    reason: str = Field(max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class CommentCreate(BaseModel):
    body: str
    parent_id: str | None = None

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        return value.strip()


class MuteUserPayload(BaseModel):
    muted_until: str | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class UserProfileUpdate(BaseModel):
    github_name: str | None = None
    avatar_url: str | None = None
    github_token: str | None = None
    github_refresh_interval_seconds: int | None = Field(default=None, ge=300, le=86400)
    notification_email: str | None = None
    notify_plugin_review: bool | None = None
    notify_comments: bool | None = None
    notify_replies: bool | None = None
    notify_likes: bool | None = None
    notify_unlist: bool | None = None
    email_notify_plugin_review: bool | None = None
    email_notify_pending_review: bool | None = None
    email_notify_comments: bool | None = None
    email_notify_replies: bool | None = None
    email_notify_likes: bool | None = None
    email_notify_unlist: bool | None = None

    @field_validator("github_name", "avatar_url", "github_token", "notification_email")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip()


class NotificationDeletePayload(BaseModel):
    ids: list[str] = Field(default_factory=list)

    @field_validator("ids", mode="before")
    @classmethod
    def clean_ids(cls, value: list[str] | None) -> list[str]:
        return [str(item).strip() for item in value or [] if str(item).strip()]


class RoleUpdatePayload(BaseModel):
    role: str = "user"


class InternalUserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

    @field_validator("username", "password", "role")
    @classmethod
    def strip_internal_user_fields(cls, value: str) -> str:
        return value.strip()


class InternalLoginPayload(BaseModel):
    username: str
    password: str

    @field_validator("username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()


class AnnouncementCreate(BaseModel):
    title: str
    body: str

    @field_validator("title", "body")
    @classmethod
    def strip_required(cls, value: str) -> str:
        return value.strip()


class ApiKeyCreate(BaseModel):
    name: str = "AstrBot WebUI"
    scopes: list[str] = Field(default_factory=lambda: ["market:read", "market:write"])

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip() or "AstrBot WebUI"

    @field_validator("scopes", mode="before")
    @classmethod
    def clean_scopes(cls, value: list[str] | str | None) -> list[str]:
        raw_scopes = value.replace(",", "|").split("|") if isinstance(value, str) else value
        scopes: list[str] = []
        for item in raw_scopes or ["market:read"]:
            scope = str(item or "").strip()
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes or ["market:read"]


class SiteSetupConfig(BaseModel):
    name: str = "AstrBot Community Plugins"
    icon_url: str = "/logo.webp"
    web_url: str = "http://127.0.0.1:8787"
    subtitle: str = "全新社区插件市场"
    description: str = "发现、评价和提交 AstrBot 插件。"
    contact_email: str = ""
    docs_url: str = "https://docs.astrbot.app/dev/star/plugin-new.html"

    @field_validator(
        "name",
        "icon_url",
        "web_url",
        "subtitle",
        "description",
        "contact_email",
        "docs_url",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()


class PostgresSetupConfig(BaseModel):
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str
    username: str
    password: str
    ssl: bool = False

    @field_validator("host", "database", "username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()


class RedisSetupConfig(BaseModel):
    host: str
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    password: str = ""
    ssl: bool = False

    @field_validator("host", "password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class AdminSetupConfig(BaseModel):
    username: str = "admin"
    password: str

    @field_validator("username", "password")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()


class AuthSetupConfig(BaseModel):
    github_login_enabled: bool = False
    public_login_enabled: bool = True
    login_agreement_enabled: bool = False
    login_agreement_text: str = ""
    service_terms_enabled: bool = False
    service_terms_text: str = ""

    @field_validator("login_agreement_text", "service_terms_text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class GithubSetupConfig(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    callback_url: str = ""
    scope: str = "read:user user:email read:org"
    admin_org: str = ""
    api_token: str = ""
    metadata_sync_enabled: bool = True
    metadata_sync_interval_seconds: int = Field(default=3600, ge=300, le=86400)

    @field_validator(
        "client_id", "client_secret", "callback_url", "scope", "admin_org", "api_token"
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class PluginGithubRefreshPayload(BaseModel):
    github_token: str = ""
    save_token: bool = False
    refresh_interval_seconds: int | None = Field(default=None, ge=300, le=86400)

    @field_validator("github_token")
    @classmethod
    def strip_token(cls, value: str) -> str:
        return value.strip()


class MarketSetupConfig(BaseModel):
    submissions_enabled: bool = True
    comments_enabled: bool = True
    likes_enabled: bool = True
    plugin_auto_approve_enabled: bool = False
    max_plugin_tags: int = Field(default=8, ge=0, le=50)
    api_token: str = ""
    api_token_remove_indexes: list[int] = Field(default_factory=list)
    metadata_sync_enabled: bool = True
    metadata_sync_interval_seconds: int = Field(default=3600, ge=300, le=86400)

    @field_validator("api_token")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_token_remove_indexes")
    @classmethod
    def validate_token_remove_indexes(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("token indexes must be non-negative")
        return sorted(set(value))


class SmtpSetupConfig(BaseModel):
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    password: str = ""
    from_address: str = ""
    from_name: str = DEFAULT_EMAIL_FROM_NAME
    ssl: bool = False
    encryption: str = ""
    auth_method: str = "auto"
    validate_certs: bool = True

    @field_validator(
        "host",
        "username",
        "password",
        "from_address",
        "from_name",
        "encryption",
        "auth_method",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def normalize_smtp_options(self) -> "SmtpSetupConfig":
        self.encryption = normalize_smtp_encryption(self.encryption)
        self.auth_method = normalize_smtp_auth_method(self.auth_method)
        self.ssl = self.encryption == "ssl_tls"
        return self


class CloudflareEmailSetupConfig(BaseModel):
    account_id: str = ""
    api_token: str = ""
    from_address: str = ""
    from_name: str = DEFAULT_EMAIL_FROM_NAME

    @field_validator("account_id", "api_token", "from_address", "from_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class EmailSetupConfig(BaseModel):
    provider: str = "disabled"
    smtp: SmtpSetupConfig = Field(default_factory=SmtpSetupConfig)
    cloudflare: CloudflareEmailSetupConfig = Field(default_factory=CloudflareEmailSetupConfig)
    daily_limit: int = Field(default=0, ge=0)
    verification_daily_limit_per_user: int = Field(default=5, ge=0)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        if provider not in {"disabled", "smtp", "cloudflare"}:
            raise ValueError("email provider must be disabled, smtp or cloudflare")
        return provider


class SystemSettingsPayload(BaseModel):
    site: SiteSetupConfig = Field(default_factory=SiteSetupConfig)
    auth: AuthSetupConfig = Field(default_factory=AuthSetupConfig)
    github: GithubSetupConfig = Field(default_factory=GithubSetupConfig)
    market: MarketSetupConfig = Field(default_factory=MarketSetupConfig)
    email: EmailSetupConfig = Field(default_factory=EmailSetupConfig)


class TestEmailPayload(BaseModel):
    to: str
    subject: str = "AstrBot Community Plugins test email"
    body: str = "This is a test email from AstrBot Community Plugins."

    @field_validator("to", "subject", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class SetupConfig(BaseModel):
    postgres: PostgresSetupConfig
    redis: RedisSetupConfig
    site: SiteSetupConfig = Field(default_factory=SiteSetupConfig)
    admin: AdminSetupConfig
    auth: AuthSetupConfig = Field(default_factory=AuthSetupConfig)
    github: GithubSetupConfig = Field(default_factory=GithubSetupConfig)
    market: MarketSetupConfig = Field(default_factory=MarketSetupConfig)
    email: EmailSetupConfig = Field(default_factory=EmailSetupConfig)
