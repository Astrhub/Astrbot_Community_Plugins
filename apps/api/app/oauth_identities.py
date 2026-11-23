"""5-tier user hierarchy and generic multi-OAuth identity binding helpers."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from .auth import is_admin, is_core_admin

GITHUB_PROVIDER = "github"
GITHUB_INSTANCE_URL = "https://github.com"

PUBLIC_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,37}[A-Za-z0-9]$")
RESERVED_USERNAMES = frozenset(
    {
        "admin",
        "administrator",
        "api",
        "assets",
        "auth",
        "author",
        "authors",
        "docs",
        "health",
        "login",
        "logout",
        "me",
        "notifications",
        "plugin",
        "plugins",
        "register",
        "root",
        "settings",
        "setup",
        "submit",
        "system",
        "v1",
    }
)


class AccountTier(StrEnum):
    GUEST = "guest"
    USER = "user"
    DEVELOPER = "developer"
    ADMIN = "admin"
    CORE_ADMIN = "core_admin"


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def clean_email(value: str | None) -> str:
    return str(value or "").strip()


def validate_public_username(value: str | None, *, allow_reserved: bool = False) -> str:
    username = str(value or "").strip()
    if len(username) < 3 or len(username) > 39:
        raise ValueError("用户名长度需在 3 到 39 个字符之间")
    if "@" in username or "/" in username or ".." in username:
        raise ValueError("用户名不能包含 @、/ 或连续句点")
    if not PUBLIC_USERNAME_PATTERN.fullmatch(username):
        raise ValueError("用户名只能包含字母、数字、点、下划线和短横线，且首尾须为字母或数字")
    if not allow_reserved and username.lower() in RESERVED_USERNAMES:
        raise ValueError("该用户名为系统保留名称，请更换其他用户名")
    return username


def provider_profile_url(identity: Mapping[str, Any]) -> str:
    provider = str(identity.get("provider") or "").strip().lower()
    login = str(identity.get("provider_login") or "").strip()
    instance = str(identity.get("instance_url") or "").strip().rstrip("/")
    if not login:
        return ""
    if provider == GITHUB_PROVIDER:
        return f"https://github.com/{quote(login, safe='')}"
    if instance.startswith(("https://", "http://")):
        return f"{instance}/{quote(login, safe='')}"
    return ""


def public_oauth_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(identity.get("id") or ""),
        "provider": str(identity.get("provider") or ""),
        "instance_url": str(identity.get("instance_url") or ""),
        "provider_user_id": str(identity.get("provider_user_id") or ""),
        "provider_login": str(identity.get("provider_login") or ""),
        "provider_email": str(identity.get("provider_email") or ""),
        "avatar_url": str(identity.get("avatar_url") or ""),
        "profile_url": provider_profile_url(identity),
        "created_at": identity.get("created_at"),
        "updated_at": identity.get("updated_at"),
    }


def author_oauth_profiles(identities: list[dict[str, Any]]) -> list[dict[str, str]]:
    profiles: list[dict[str, str]] = []
    for identity in identities:
        url = provider_profile_url(identity)
        if not url:
            continue
        profiles.append(
            {
                "provider": str(identity.get("provider") or ""),
                "login": str(identity.get("provider_login") or ""),
                "url": url,
            }
        )
    return profiles


async def _call_store(store: Any, method_name: str, *args: Any) -> Any:
    method = getattr(store, method_name, None)
    if not method:
        return None
    result = method(*args)
    return await result if inspect.isawaitable(result) else result


async def list_active_oauth_identities(store: Any, user_id: str) -> list[dict[str, Any]]:
    if not user_id:
        return []
    identities = await _call_store(store, "list_oauth_identities", str(user_id))
    if isinstance(identities, list):
        return [item for item in identities if not item.get("revoked_at")]
    return []


async def has_bound_oauth_identity(store: Any, user: Mapping[str, Any] | None) -> bool:
    if not user or not user.get("id"):
        return False
    if hasattr(store, "list_oauth_identities"):
        identities = await list_active_oauth_identities(store, str(user["id"]))
        return bool(identities)
    return bool(user.get("github_id") or user.get("github_login"))


async def has_active_plugins(store: Any, user_id: str) -> bool:
    if not user_id:
        return False
    if hasattr(store, "has_active_plugins"):
        return bool(await _call_store(store, "has_active_plugins", str(user_id)))
    plugins = await _call_store(store, "list_user_plugins", str(user_id)) or []
    return any(plugin.get("status") in {"listed", "pending"} for plugin in plugins)


async def resolve_account_tier(store: Any, user: Mapping[str, Any] | None) -> AccountTier:
    if not user:
        return AccountTier.GUEST
    if is_core_admin(user):
        return AccountTier.CORE_ADMIN
    if is_admin(user):
        return AccountTier.ADMIN
    if await has_bound_oauth_identity(store, user) or await has_active_plugins(
        store, str(user.get("id") or "")
    ):
        return AccountTier.DEVELOPER
    return AccountTier.USER


def is_developer_tier(tier: AccountTier | str | None) -> bool:
    return str(tier or "") in {
        AccountTier.DEVELOPER.value,
        AccountTier.ADMIN.value,
        AccountTier.CORE_ADMIN.value,
    }


def can_unbind_oauth_identity(
    user: Mapping[str, Any],
    active_identities: list[dict[str, Any]],
    target_id: str,
) -> tuple[bool, str]:
    matched = next((item for item in active_identities if str(item.get("id")) == target_id), None)
    if not matched:
        return (False, "not_found")
    if len(active_identities) <= 1 and not str(user.get("password_hash") or "").strip():
        return (False, "last_auth_method")
    return (True, "")


async def user_tier_context(store: Any, user: Mapping[str, Any]) -> dict[str, Any]:
    user_id = str(user.get("id") or "")
    identities = await list_active_oauth_identities(store, user_id)
    has_oauth = (
        bool(identities)
        if hasattr(store, "list_oauth_identities")
        else bool(user.get("github_id") or user.get("github_login"))
    )
    active_plugins = await has_active_plugins(store, user_id)
    if is_core_admin(user):
        tier = AccountTier.CORE_ADMIN
    elif is_admin(user):
        tier = AccountTier.ADMIN
    elif has_oauth or active_plugins:
        tier = AccountTier.DEVELOPER
    else:
        tier = AccountTier.USER
    return {
        "account_tier": tier.value,
        "is_developer": is_developer_tier(tier),
        "has_oauth_binding": has_oauth,
        "has_active_plugins": active_plugins,
        "has_password": bool(str(user.get("password_hash") or "").strip()),
        "oauth_identities": [public_oauth_identity(item) for item in identities],
        "oauth_providers": sorted(
            {str(item.get("provider") or "") for item in identities if item.get("provider")}
        ),
    }
