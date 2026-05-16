from __future__ import annotations

from enum import StrEnum
from typing import Mapping

from .config import ApiKey


class Role(StrEnum):
    CORE_ADMIN = "core_admin"
    ADMIN = "admin"
    USER = "user"


def normalize_role(role: str | None) -> Role:
    if role == Role.CORE_ADMIN:
        return Role.CORE_ADMIN
    if role == Role.ADMIN:
        return Role.ADMIN
    return Role.USER


def is_core_admin(user: Mapping | None) -> bool:
    return normalize_role(user.get("role") if user else None) == Role.CORE_ADMIN


def is_admin(user: Mapping | None) -> bool:
    return normalize_role(user.get("role") if user else None) in {Role.CORE_ADMIN, Role.ADMIN}


def can_manage_admins(user: Mapping | None) -> bool:
    return is_core_admin(user)


def can_publish_announcement(user: Mapping | None) -> bool:
    return is_core_admin(user)


def can_moderate_plugins(user: Mapping | None) -> bool:
    return is_admin(user)


def can_moderate_community(user: Mapping | None) -> bool:
    return is_admin(user)


def can_edit_plugin(user: Mapping | None, plugin: Mapping | None) -> bool:
    if not user or not plugin:
        return False
    if is_admin(user):
        return True
    return (
        plugin.get("owner_user_id") == user.get("id")
        or plugin.get("owner_github_login") == user.get("github_login")
    )


def can_manage_plugin_submission(user: Mapping | None, plugin: Mapping | None) -> bool:
    if not user or not plugin:
        return False
    return is_admin(user) or plugin.get("owner_user_id") == user.get("id")


def authenticate_api_key(auth_header: str | None, api_keys: list[ApiKey | Mapping]) -> ApiKey | Mapping | None:
    token = _bearer_token(auth_header)
    if not token:
        return None
    return next((key for key in api_keys if _key_value(key) == token), None)


def require_api_key(
    auth_header: str | None,
    api_keys: list[ApiKey | Mapping],
    scope: str,
) -> tuple[bool, int, str]:
    key = authenticate_api_key(auth_header, api_keys)
    if not key:
        return (False, 401, "Missing or invalid API key")
    if scope and scope not in _key_scopes(key):
        return (False, 403, f"API key lacks scope: {scope}")
    return (True, 200, "")


def _bearer_token(auth_header: str | None) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        return ""
    return auth_header.removeprefix("Bearer ").strip()


def _key_value(key: ApiKey | Mapping) -> str:
    return key.key if isinstance(key, ApiKey) else str(key.get("key", ""))


def _key_scopes(key: ApiKey | Mapping) -> tuple[str, ...]:
    return key.scopes if isinstance(key, ApiKey) else tuple(key.get("scopes", ()))
