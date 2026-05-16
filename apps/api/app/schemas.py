from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class PluginSubmission(BaseModel):
    name: str
    display_name: str | None = None
    desc: str
    author: str
    repo: str
    social_link: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("name", "display_name", "desc", "author", "repo", "social_link", mode="before")
    @classmethod
    def strip_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str]:
        return [str(tag).strip() for tag in value or [] if str(tag).strip()]


class PluginPatch(BaseModel):
    name: str | None = None
    display_name: str | None = None
    desc: str | None = None
    author: str | None = None
    repo: str | None = None
    social_link: str | None = None
    tags: list[str] | None = None

    @field_validator("name", "display_name", "desc", "author", "repo", "social_link", mode="before")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip()


class CommentCreate(BaseModel):
    body: str
    parent_id: str | None = None

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        return value.strip()


class MuteUserPayload(BaseModel):
    muted_until: str | None = None


class UserProfileUpdate(BaseModel):
    github_name: str | None = None
    avatar_url: str | None = None

    @field_validator("github_name", "avatar_url")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip()


class RoleUpdatePayload(BaseModel):
    role: str = "user"


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


class SiteSetupConfig(BaseModel):
    name: str = "AstrBot Community Plugins"
    icon_url: str = "/logo.webp"
    subtitle: str = "全新社区插件市场"
    description: str = "发现、评价和提交 AstrBot 插件。"
    contact_email: str = ""
    docs_url: str = "https://docs.astrbot.app/dev/star/plugin-new.html"

    @field_validator("name", "icon_url", "subtitle", "description", "contact_email", "docs_url")
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

    @field_validator("client_id", "client_secret", "callback_url", "scope", "admin_org")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class MarketSetupConfig(BaseModel):
    submissions_enabled: bool = True
    comments_enabled: bool = True
    likes_enabled: bool = True
    plugin_auto_approve_enabled: bool = False
    max_plugin_tags: int = Field(default=8, ge=0, le=50)


class SmtpSetupConfig(BaseModel):
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    password: str = ""
    from_address: str = ""
    ssl: bool = False

    @field_validator("host", "username", "password", "from_address")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class CloudflareEmailSetupConfig(BaseModel):
    account_id: str = ""
    api_token: str = ""
    from_address: str = ""

    @field_validator("account_id", "api_token", "from_address")
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
