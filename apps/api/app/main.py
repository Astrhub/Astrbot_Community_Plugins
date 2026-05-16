from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import (
    can_edit_plugin,
    can_manage_admins,
    can_manage_plugin_submission,
    can_moderate_community,
    can_moderate_plugins,
    can_publish_announcement,
    is_admin,
    is_core_admin,
    require_api_key,
)
from .config import ApiKey, Settings, load_settings
from .runtime_config import read_runtime_config, write_runtime_config
from .schemas import (
    AnnouncementCreate,
    ApiKeyCreate,
    CommentCreate,
    MuteUserPayload,
    PluginPatch,
    PluginSubmission,
    RoleUpdatePayload,
    SetupConfig,
)
from .store import InMemoryMarketStore

GITHUB_REPO_PATTERN = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/?$")
PLUGIN_NAME_PATTERN = re.compile(r"^astrbot_plugin_[a-z0-9_-]+$", re.IGNORECASE)


def create_app(
    settings: Settings | None = None,
    store: InMemoryMarketStore | None = None,
) -> FastAPI:
    app = FastAPI(title="AstrBot Community Plugins API", version="0.1.0")
    app.state.settings = settings or load_settings()
    app.state.store = store or InMemoryMarketStore()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app.state.settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["content-type", "authorization", "x-dev-github-login"],
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    register_routes(app)
    return app


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


def register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        settings = get_settings(request)
        return {
            "status": "ok",
            "setup": "required" if settings.is_setup_required() else "complete",
            "database": "configured" if settings.database_url else "missing",
            "redis": "configured" if settings.redis_url else "missing",
        }

    @app.get("/v1/setup/status")
    async def setup_status(request: Request) -> dict[str, Any]:
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        runtime_database_url = runtime_config.get("DATABASE_URL", "")
        runtime_redis_url = runtime_config.get("REDIS_URL", "")
        return {
            "required": settings.is_setup_required(),
            "missing": list(settings.missing_setup_fields()),
            "database_configured": bool(runtime_database_url),
            "redis_configured": bool(runtime_redis_url),
            "saved_database_url": runtime_database_url,
            "saved_redis_url": runtime_redis_url,
            "restart_required": bool(runtime_database_url or runtime_redis_url)
            and (
                runtime_database_url != settings.database_url
                or runtime_redis_url != settings.redis_url
            ),
        }

    @app.post("/v1/setup")
    async def save_setup(request: Request, payload: SetupConfig) -> dict[str, Any]:
        settings = get_settings(request)
        runtime_config = read_runtime_config(settings.runtime_config_path)
        if not can_save_setup_without_auth(settings, runtime_config):
            user = require_user(request)
            if not is_core_admin(user):
                raise error(403, "Only core admin can update infrastructure settings")
        validate_setup_payload(payload)
        write_runtime_config(
            settings.runtime_config_path,
            {
                "DATABASE_URL": payload.database_url,
                "REDIS_URL": payload.redis_url,
            },
        )
        return {
            "saved": True,
            "restart_required": True,
            "message": "Configuration saved. Restart the API process to use PostgreSQL and Redis.",
        }

    @app.get("/v1/me")
    async def me(request: Request) -> dict[str, Any]:
        return require_user(request)

    @app.get("/v1/auth/github/login")
    async def github_login(request: Request) -> Response:
        settings = get_settings(request)
        if not settings.github_client_id:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "GitHub OAuth is not configured",
                    "next": "Set GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET and GITHUB_CALLBACK_URL",
                },
            )

        state = str(uuid.uuid4())
        params = urlencode(
            {
                "client_id": settings.github_client_id,
                "redirect_uri": settings.github_callback_url,
                "scope": settings.github_scope,
                "state": state,
            }
        )
        response = RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")
        set_cookie(response, settings.oauth_state_cookie_name, state, settings, max_age=600)
        return response

    @app.get("/v1/auth/github/callback")
    async def github_callback(request: Request, code: str | None = None, state: str | None = None) -> Response:
        settings = get_settings(request)
        expected_state = request.cookies.get(settings.oauth_state_cookie_name)
        if not code or not state or not expected_state or state != expected_state:
            raise error(400, "Invalid OAuth callback")

        access_token = await exchange_github_code(settings, code)
        profile = await fetch_github_profile(access_token)
        user = get_store(request).upsert_github_user(
            {
                "id": str(profile["id"]),
                "login": profile["login"],
                "name": profile.get("name") or profile["login"],
                "avatar_url": profile.get("avatar_url") or "",
            }
        )
        await promote_org_admin_if_needed(request, user, access_token)
        session = get_store(request).create_session(user["id"])
        response = RedirectResponse(settings.web_url)
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        response.delete_cookie(settings.oauth_state_cookie_name, path="/")
        return response

    @app.post("/v1/auth/logout", status_code=204)
    async def logout(request: Request, response: Response) -> None:
        settings = get_settings(request)
        session_token = request.cookies.get(settings.session_cookie_name)
        if session_token:
            get_store(request).revoke_session(session_token)
        response.delete_cookie(settings.session_cookie_name, path="/")

    @app.get("/v1/auth/debug-login")
    async def debug_login(request: Request, login: str = "") -> Response:
        settings = get_settings(request)
        if not settings.enable_dev_auth:
            raise error(403, "Dev auth is disabled")
        if not login.strip():
            raise error(400, "login is required")
        user = get_store(request).upsert_github_user({"login": login.strip(), "name": login.strip()})
        session = get_store(request).create_session(user["id"])
        response = JSONResponse({"user": user, "session": session})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @app.get("/v1/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        return {"authenticated": True, "user": require_user(request)}

    @app.get("/v1/admin/check")
    async def admin_check(request: Request) -> dict[str, bool]:
        user = require_user(request)
        return {
            "core_admin": is_core_admin(user),
            "admin": is_admin(user),
            "can_moderate_plugins": can_moderate_plugins(user),
            "can_moderate_community": can_moderate_community(user),
            "can_manage_admins": can_manage_admins(user),
        }

    @app.get("/v1/permissions")
    async def permissions(request: Request) -> dict[str, bool]:
        user = require_user(request)
        return {
            "can_edit_any_plugin": is_admin(user),
            "can_moderate_plugins": can_moderate_plugins(user),
            "can_moderate_community": can_moderate_community(user),
            "can_publish_announcement": can_publish_announcement(user),
            "can_manage_admins": can_manage_admins(user),
        }

    @app.get("/v1/plugins")
    async def list_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"items": get_store(request).list_public_plugins()}

    @app.get("/plugins.json")
    async def astrbot_plugin_source(request: Request) -> dict[str, dict[str, Any]]:
        return build_astrbot_plugin_source(get_store(request).list_public_plugins())

    @app.get("/plugins-md5.json")
    async def astrbot_plugin_source_md5(request: Request) -> dict[str, str]:
        feed = build_astrbot_plugin_source(get_store(request).list_public_plugins())
        return {"md5": digest_plugin_source(feed)}

    @app.get("/v1/astrbot/plugins")
    @app.get("/v1/astrbot/plugins.json")
    async def astrbot_plugin_source_v1(request: Request) -> dict[str, dict[str, Any]]:
        return build_astrbot_plugin_source(get_store(request).list_public_plugins())

    @app.get("/v1/astrbot/plugins-md5.json")
    async def astrbot_plugin_source_v1_md5(request: Request) -> dict[str, str]:
        feed = build_astrbot_plugin_source(get_store(request).list_public_plugins())
        return {"md5": digest_plugin_source(feed)}

    @app.get("/v1/plugins/submissions")
    async def list_submissions(request: Request) -> dict[str, list[dict[str, Any]]]:
        user = require_user(request)
        if not is_admin(user):
            raise error(403, "Forbidden")
        return {"items": get_store(request).state["submissions"]}

    @app.post("/v1/plugins/submissions", status_code=201)
    async def submit_plugin(request: Request, payload: PluginSubmission) -> dict[str, Any]:
        user = require_user(request)
        data = payload.model_dump()
        validate_plugin_submission(data)
        validate_repo_owner(data["repo"], user)
        return get_store(request).submit_plugin(user, data)

    @app.get("/v1/plugins/{plugin_id}")
    async def plugin_detail(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = get_plugin_or_404(request, plugin_id)
        return {**plugin, "comments": get_store(request).list_comments(plugin_id)}

    @app.patch("/v1/plugins/{plugin_id}")
    async def update_plugin(request: Request, plugin_id: str, payload: PluginPatch) -> dict[str, Any]:
        user = require_user(request)
        plugin = get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        patch = payload.model_dump(exclude_unset=True)
        if "name" in patch:
            validate_plugin_name(patch["name"])
        if "repo" in patch:
            validate_github_repo(patch["repo"])
            validate_repo_owner(patch["repo"], user)
        updated = get_store(request).update_plugin_metadata(
            plugin_id,
            {**patch, "owner_user_id": plugin["owner_user_id"], "owner_github_login": plugin["owner_github_login"]},
        )
        return updated or {}

    @app.post("/v1/plugins/{plugin_id}/like")
    async def like_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = get_plugin_or_404(request, plugin_id)
        return get_store(request).update_plugin_metadata(plugin_id, {"likes": plugin["likes"] + 1}) or {}

    @app.post("/v1/plugins/{plugin_id}/unlike")
    async def unlike_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = get_plugin_or_404(request, plugin_id)
        return get_store(request).update_plugin_metadata(
            plugin_id,
            {"likes": max(0, plugin["likes"] - 1)},
        ) or {}

    @app.post("/v1/plugins/{plugin_id}/comments", status_code=201)
    async def add_comment(request: Request, plugin_id: str, payload: CommentCreate) -> dict[str, Any]:
        user = require_user(request)
        get_plugin_or_404(request, plugin_id)
        if not payload.body:
            raise error(400, "Comment body is required")
        muted_until = parse_iso_datetime(user.get("muted_until"))
        if muted_until and muted_until > datetime.now(UTC):
            raise error(403, "User is muted")
        return get_store(request).add_comment(plugin_id, user["id"], payload.body, payload.parent_id)

    @app.post("/v1/plugins/{plugin_id}/reindex")
    async def reindex_plugin(request: Request, plugin_id: str) -> dict[str, bool]:
        user = require_user(request)
        plugin = get_plugin_or_404(request, plugin_id)
        if not can_manage_plugin_submission(user, plugin):
            raise error(403, "Forbidden")
        return {"ok": True}

    @app.get("/v1/admin/users")
    async def admin_users(request: Request) -> dict[str, list[dict[str, Any]]]:
        require_admin(request)
        return {"items": get_store(request).state["users"]}

    @app.get("/v1/admin/plugins")
    async def admin_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        require_admin(request)
        return {"items": get_store(request).state["plugins"]}

    @app.get("/v1/admin/summary")
    async def admin_summary(request: Request) -> dict[str, Any]:
        user = require_admin(request)
        state = get_store(request).state
        return {
            "users": len(state["users"]),
            "plugins": len(state["plugins"]),
            "submissions": len(state["submissions"]),
            "announcements": len(state["announcements"]),
            "role": user["role"],
        }

    @app.post("/v1/admin/plugins/{plugin_id}/list")
    async def list_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        user = require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        updated = get_store(request).update_plugin_status(plugin_id, "listed", user["id"])
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.post("/v1/admin/plugins/{plugin_id}/unlist")
    async def unlist_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        user = require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        updated = get_store(request).update_plugin_status(plugin_id, "unlisted", user["id"])
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.delete("/v1/admin/comments/{comment_id}")
    async def delete_comment(request: Request, comment_id: str) -> dict[str, Any]:
        user = require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        deleted = get_store(request).delete_comment(comment_id, user["id"])
        if not deleted:
            raise error(404, "Comment not found")
        return deleted

    @app.post("/v1/admin/users/{user_id}/mute")
    async def mute_user(request: Request, user_id: str, payload: MuteUserPayload) -> dict[str, Any]:
        user = require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        muted_until = payload.muted_until or (datetime.now(UTC) + timedelta(days=1)).isoformat()
        muted = get_store(request).mute_user(user_id, muted_until, user["id"])
        if not muted:
            raise error(404, "User not found")
        return muted

    @app.post("/v1/core/admins/{user_id}")
    async def update_admin(request: Request, user_id: str, payload: RoleUpdatePayload) -> dict[str, Any]:
        user = require_user(request)
        if not can_manage_admins(user):
            raise error(403, "Forbidden")
        target = get_store(request).get_user_by_id(user_id)
        if not target:
            raise error(404, "User not found")
        updated = get_store(request).update_user_role(user_id, "admin" if payload.role == "admin" else "user")
        return updated or {}

    @app.post("/v1/core/announcements", status_code=201)
    async def create_announcement(request: Request, payload: AnnouncementCreate) -> dict[str, Any]:
        user = require_user(request)
        if not can_publish_announcement(user):
            raise error(403, "Forbidden")
        if not payload.title or not payload.body:
            raise error(400, "Announcement title and body are required")
        return get_store(request).publish_announcement(payload.title, payload.body, user["id"])

    @app.get("/v1/announcements")
    async def announcements(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"items": get_store(request).list_announcements()}

    @app.post("/v1/api-keys", status_code=201)
    async def issue_api_key(request: Request, payload: ApiKeyCreate) -> dict[str, Any]:
        user = require_user(request)
        if not is_admin(user):
            raise error(403, "Forbidden")
        return get_store(request).issue_api_key(payload.name, user["id"], payload.scopes)

    @app.get("/v1/api-keys")
    async def api_keys(request: Request) -> dict[str, list[dict[str, Any]]]:
        keys = all_api_keys(request)
        ok, status, message = require_api_key(request.headers.get("authorization"), keys, "market:read")
        if not ok:
            raise error(status, message)
        return {"items": [public_api_key(key) for key in keys]}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> InMemoryMarketStore:
    return request.app.state.store


def current_user(request: Request) -> dict[str, Any] | None:
    settings = get_settings(request)
    store = get_store(request)
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        user = store.get_user_by_session(session_token)
        if user:
            return user

    if not settings.enable_dev_auth:
        return None
    dev_login = request.headers.get("x-dev-github-login", "").strip()
    if not dev_login:
        return None
    return store.upsert_github_user({"login": dev_login, "name": dev_login})


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise error(401, "Not authenticated")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if not is_admin(user):
        raise error(403, "Forbidden")
    return user


def get_plugin_or_404(request: Request, plugin_id: str) -> dict[str, Any]:
    plugin = get_store(request).get_plugin(plugin_id)
    if not plugin:
        raise error(404, "Plugin not found")
    return plugin


def validate_plugin_submission(payload: dict[str, Any]) -> None:
    for field in ("name", "repo", "desc", "author"):
        if not payload.get(field):
            raise error(400, "Missing required plugin fields")
    validate_plugin_name(payload["name"])
    validate_github_repo(payload["repo"])


def validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME_PATTERN.match(name or ""):
        raise error(400, "Plugin name must use astrbot_plugin_ prefix")


def validate_github_repo(repo: str) -> re.Match[str]:
    match = GITHUB_REPO_PATTERN.match(repo or "")
    if not match:
        raise error(400, "Plugin repo must be a GitHub URL")
    return match


def validate_setup_payload(payload: SetupConfig) -> None:
    if not payload.database_url:
        raise error(400, "DATABASE_URL is required")
    if not payload.redis_url:
        raise error(400, "REDIS_URL is required")
    if not payload.database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
        raise error(400, "DATABASE_URL must be a PostgreSQL connection URL")
    if not payload.redis_url.startswith(("redis://", "rediss://")):
        raise error(400, "REDIS_URL must be a Redis connection URL")


def build_astrbot_plugin_source(plugins: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    feed: dict[str, dict[str, Any]] = {}
    for plugin in plugins:
        name = str(plugin.get("name") or plugin.get("id") or "").strip()
        if not name:
            continue
        feed[name] = format_astrbot_plugin(plugin, name)
    return dict(sorted(feed.items()))


def format_astrbot_plugin(plugin: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "name": name,
        "display_name": plugin.get("display_name") or name,
        "desc": plugin.get("desc") or "",
        "short_desc": plugin.get("short_desc") or plugin.get("desc") or "",
        "author": plugin.get("author") or plugin.get("owner_github_login") or "",
        "repo": plugin.get("repo") or "",
        "social_link": plugin.get("social_link") or "",
        "tags": plugin.get("tags") if isinstance(plugin.get("tags"), list) else [],
        "stars": int(plugin.get("stars") or 0),
        "updated_at": plugin.get("updated_at") or "",
        "version": plugin.get("version") or "1.0.0",
        "logo": plugin.get("logo") or "",
        "pinned": bool(plugin.get("pinned")),
        "download_url": plugin.get("download_url") or "",
        "i18n": plugin.get("i18n") if isinstance(plugin.get("i18n"), dict) else {},
        "astrbot_version": plugin.get("astrbot_version") or "",
        "category": plugin.get("category") or "",
        "support_platforms": plugin.get("support_platforms")
        if isinstance(plugin.get("support_platforms"), list)
        else [],
    }


def digest_plugin_source(feed: dict[str, dict[str, Any]]) -> str:
    payload = json.dumps(feed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def can_save_setup_without_auth(settings: Settings, runtime_config: dict[str, str]) -> bool:
    has_saved_config = bool(runtime_config.get("DATABASE_URL") or runtime_config.get("REDIS_URL"))
    return settings.is_setup_required() and not has_saved_config


def validate_repo_owner(repo: str, user: dict[str, Any]) -> None:
    owner = validate_github_repo(repo).group("owner")
    if owner.lower() == user["github_login"].lower():
        return
    raise error(403, "GitHub account must own the repository")


def set_cookie(
    response: Response,
    name: str,
    value: str,
    settings: Settings,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        name,
        value,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_same_site.lower(),
        max_age=max_age or settings.session_max_age_seconds,
        path="/",
    )


async def exchange_github_code(settings: Settings, code: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"accept": "application/json"},
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_callback_url,
            },
        )
    data = response.json()
    if response.status_code >= 400 or not data.get("access_token"):
        raise error(502, data.get("error_description") or data.get("error") or "GitHub OAuth token exchange failed")
    return data["access_token"]


async def fetch_github_profile(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://api.github.com/user",
            headers=github_headers(access_token),
        )
    data = response.json()
    if response.status_code >= 400 or not data.get("login"):
        raise error(502, data.get("message") or "GitHub profile lookup failed")
    return data


async def promote_org_admin_if_needed(
    request: Request,
    user: dict[str, Any],
    access_token: str,
) -> None:
    settings = get_settings(request)
    if not settings.github_admin_org or is_admin(user):
        return
    if await is_github_org_member(settings.github_admin_org, access_token):
        get_store(request).update_user_role(user["id"], "admin")


async def is_github_org_member(org: str, access_token: str) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"https://api.github.com/user/memberships/orgs/{org}",
            headers=github_headers(access_token),
        )
    if response.status_code == 404:
        return False
    data = response.json()
    return response.status_code < 400 and data.get("state") == "active"


def github_headers(access_token: str) -> dict[str, str]:
    return {
        "accept": "application/vnd.github+json",
        "authorization": f"Bearer {access_token}",
        "user-agent": "astrbot-community-plugins",
    }


def all_api_keys(request: Request) -> list[ApiKey | dict[str, Any]]:
    settings = get_settings(request)
    return [*settings.api_keys, *get_store(request).state["apiKeys"]]


def public_api_key(key: ApiKey | dict[str, Any]) -> dict[str, Any]:
    if isinstance(key, ApiKey):
        return {"name": key.name, "scopes": list(key.scopes)}
    return {"id": key.get("id"), "name": key.get("name"), "scopes": key.get("scopes", [])}


def error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


app = create_app()
