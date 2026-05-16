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


class RoleUpdatePayload(BaseModel):
    role: str = "user"


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


class SetupConfig(BaseModel):
    database_url: str
    redis_url: str

    @field_validator("database_url", "redis_url")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()
