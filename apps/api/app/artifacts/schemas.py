from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class GithubArtifactSubmission(BaseModel):
    source_ref: str = Field(default="", max_length=200)

    @field_validator("source_ref")
    @classmethod
    def clean_source_ref(cls, value: str) -> str:
        return value.strip()


class ArtifactDecisionPayload(BaseModel):
    reason: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(default="", max_length=200)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()


class PluginRegistrationPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    desc: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=120)
    repo: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)
    category: Literal[
        "ai_tools",
        "entertainment",
        "integrations",
        "productivity",
        "utilities",
        "other",
    ] = "other"

    @field_validator("name", "display_name", "desc", "author", "repo")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        tags: list[str] = []
        for item in value:
            tag = str(item or "").strip()[:40]
            if tag and tag not in tags:
                tags.append(tag)
        return tags
