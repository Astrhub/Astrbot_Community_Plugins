from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import inspect
import json
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import aiosmtplib
import httpx
import asyncpg
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .auth import (
    Role,
    can_edit_plugin,
    can_manage_admins,
    can_manage_plugin_submission,
    can_moderate_community,
    can_moderate_plugins,
    can_publish_announcement,
    hash_password,
    is_admin,
    is_core_admin,
    normalize_role,
    require_api_key,
    verify_password,
)
from .config import (
    ApiKey,
    DEFAULT_EMAIL_FROM_NAME,
    Settings,
    load_settings,
    normalize_smtp_auth_method,
    normalize_smtp_encryption,
)
from .env_file import write_env_file
from .llms_txt import build_llms_txt
from .openapi_filter import filter_openapi_by_role, role_for_openapi
from .schemas import (
    AnnouncementCreate,
    ApiKeyCreate,
    CommentCreate,
    InternalLoginPayload,
    InternalUserCreate,
    MuteUserPayload,
    NotificationDeletePayload,
    PluginGithubRefreshPayload,
    PluginPatch,
    PluginSubmission,
    PluginSubmissionMetadataPreviewPayload,
    PluginUnlistPayload,
    RoleUpdatePayload,
    SetupConfig,
    SystemSettingsPayload,
    TestEmailPayload,
    UserProfileUpdate,
)
from .store import InMemoryMarketStore
from .store import PgRedisMarketStore

GITHUB_REPO_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
PLUGIN_NAME_PATTERN = re.compile(r"^astrbot_plugin_[a-z0-9_-]+$", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
METADATA_FIELD_PATTERN = re.compile(
    r"^(\s*)(name|display_name|desc|short_desc|author|social_link|tags|version|astrbot_version|category|download_url|support_platforms)\s*:\s*(.*)$"
)
MASKED_SECRET = "********"
CLOUDFLARE_EMAIL_SEND_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send"
)
MARKET_WEB_DIST = Path(__file__).resolve().parents[3] / "apps" / "market-web" / "dist"
DEFAULT_POSTGRES_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "",
    "username": "",
    "password": "",
    "ssl": False,
}
DEFAULT_REDIS_CONFIG = {
    "host": "127.0.0.1",
    "port": 6379,
    "database": 0,
    "password": "",
    "ssl": False,
}
RESERVED_WEB_PATHS = {
    "v1",
    "health",
    "plugins.json",
    "plugins-md5.json",
    "openapi.json",
    "llms.txt",
    "docs",
    "redoc",
}
RESERVED_WEB_PREFIXES = ("v1/", "health/", "plugins.json/", "plugins-md5.json/", "docs/", "redoc/")
POSTGRES_MAINTENANCE_DATABASE = "postgres"
GITHUB_METADATA_SYNC_BATCH_SIZE = 10
GITHUB_METADATA_SYNC_WORKER_SLEEP_SECONDS = 60
GITHUB_RATE_LIMIT_MESSAGE = (
    "GitHub API rate limit reached. Provide a read-only GitHub token and try again."
)
SYSTEM_OPTION_KEYS = {
    "CLOUDFLARE_EMAIL_ACCOUNT_ID",
    "CLOUDFLARE_EMAIL_API_TOKEN",
    "CLOUDFLARE_EMAIL_FROM",
    "CLOUDFLARE_EMAIL_FROM_NAME",
    "EMAIL_DAILY_LIMIT",
    "EMAIL_PROVIDER",
    "EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER",
    "GITHUB_ADMIN_ORG",
    "GITHUB_API_TOKEN",
    "GITHUB_API_TOKEN_STATUS",
    "GITHUB_CALLBACK_URL",
    "GITHUB_CLIENT_ID",
    "GITHUB_LOGIN_ENABLED",
    "GITHUB_METADATA_SYNC_ENABLED",
    "GITHUB_METADATA_SYNC_INTERVAL_SECONDS",
    "GITHUB_SCOPE",
    "LOGIN_AGREEMENT_ENABLED",
    "LOGIN_AGREEMENT_TEXT",
    "MARKET_COMMENTS_ENABLED",
    "MARKET_LIKES_ENABLED",
    "MARKET_SUBMISSIONS_ENABLED",
    "MAX_PLUGIN_TAGS",
    "PLUGIN_AUTO_APPROVE_ENABLED",
    "PUBLIC_LOGIN_ENABLED",
    "SERVICE_TERMS_ENABLED",
    "SERVICE_TERMS_TEXT",
    "SITE_CONTACT_EMAIL",
    "SITE_DESCRIPTION",
    "SITE_DOCS_URL",
    "SITE_ICON_URL",
    "SITE_NAME",
    "SITE_SUBTITLE",
    "SMTP_FROM",
    "SMTP_FROM_NAME",
    "SMTP_AUTH_METHOD",
    "SMTP_ENCRYPTION",
    "SMTP_HOST",
    "SMTP_PASSWORD",
    "SMTP_PORT",
    "SMTP_SSL",
    "SMTP_USERNAME",
    "SMTP_VALIDATE_CERTS",
    "WEB_URL",
}
PRIVATE_USER_FIELDS = {"github_email", "notification_email"}
LOGGER = logging.getLogger(__name__)
SUBMISSION_METADATA_PREVIEW_RPM = 10
RATE_LIMIT_WINDOW_SECONDS = 60
PLUGIN_METADATA_SYNC_FIELDS = (
    "name",
    "display_name",
    "desc",
    "short_desc",
    "author",
    "social_link",
    "tags",
    "version",
    "astrbot_version",
    "category",
    "download_url",
    "support_platforms",
)
OFFICIAL_PLUGIN_CATEGORIES = {
    "ai_tools",
    "entertainment",
    "integrations",
    "productivity",
    "utilities",
}


def create_app(
    settings: Settings | None = None,
    store: InMemoryMarketStore | None = None,
) -> FastAPI:
    app = FastAPI(
        title="AstrBot Community Plugins API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        openapi_tags=[
            {"name": "plugins", "description": "插件浏览与详情"},
            {"name": "submissions", "description": "插件提交与管理"},
            {"name": "comments", "description": "评论与点赞"},
            {"name": "integration", "description": "AstrBot 插件源"},
            {"name": "auth", "description": "认证与会话"},
            {"name": "user", "description": "个人资料与通知"},
            {"name": "announcements", "description": "公告"},
            {"name": "admin", "description": "管理员操作"},
            {"name": "core-admin", "description": "核心管理员操作"},
            {"name": "system", "description": "系统配置与安装"},
        ],
    )
    app.state.settings = settings or load_settings()
    app.state.store = store or create_store(app.state.settings)
    app.state.email_daily_counter = {"date": "", "count": 0}
    app.state.github_api_token_index = 0
    app.state.rate_limit_counters = {}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app.state.settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["content-type", "authorization", "x-dev-github-login"],
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    register_routes(app)
    register_market_web_routes(app)
    return app


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(exc.detail)},
        headers=exc.headers,
    )


def create_store(settings: Settings) -> InMemoryMarketStore | PgRedisMarketStore:
    if settings.database_url and settings.redis_url:
        return PgRedisMarketStore(
            settings.database_url,
            settings.redis_url,
            settings.session_max_age_seconds,
        )
    return InMemoryMarketStore()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await maybe_call_store_lifecycle(app, "connect")
    await bootstrap_internal_core_admin(app)
    sync_task = asyncio.create_task(github_metadata_sync_worker(app))
    try:
        yield None
    finally:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            # Expected during application shutdown after cancelling the sync worker.
            pass
        await maybe_call_store_lifecycle(app, "close")


async def maybe_call_store_lifecycle(app: FastAPI, method_name: str) -> None:
    method = getattr(app.state.store, method_name, None)
    if not method:
        return
    await resolve_optional_awaitable(method())


async def resolve_optional_awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def bootstrap_internal_core_admin(app: FastAPI) -> None:
    settings = app.state.settings
    if not settings.core_admin_username or not settings.core_admin_password_hash:
        return
    await resolve_optional_awaitable(
        app.state.store.create_internal_admin(
            settings.core_admin_username,
            settings.core_admin_password_hash,
        )
    )


def register_routes(app: FastAPI) -> None:
    @app.get("/docs/rest", include_in_schema=False)
    async def rest_api_docs() -> Response:
        return serve_market_web_file("docs/rest")

    @app.get(
        "/health",
        tags=["system"],
        summary="健康检查",
        description="返回服务状态，包括数据库和 Redis 连接情况。",
    )
    async def health(request: Request) -> dict[str, str]:
        settings = get_settings(request)
        return {
            "status": "ok",
            "setup": "required" if settings.is_setup_required() else "complete",
            "database": "configured" if settings.database_url else "missing",
            "redis": "configured" if settings.redis_url else "missing",
        }

    @app.get(
        "/v1/site",
        tags=["system"],
        summary="获取站点配置",
        description="返回站点名称、图标、描述、认证方式、市场功能开关等公开配置。",
    )
    async def site_config(request: Request) -> dict[str, Any]:
        settings = get_settings(request)
        runtime_config = await effective_runtime_config(request)
        return {
            **get_site_config(settings, runtime_config),
            "auth": get_public_auth_config(settings, runtime_config),
            "market": get_public_market_config(settings, runtime_config),
        }

    @app.get(
        "/v1/admin/settings",
        tags=["core-admin"],
        summary="获取系统设置",
        description="获取完整的系统设置，包括数据库、Redis、OAuth、邮件等。仅核心管理员可用。",
        responses={403: {"description": "需要核心管理员权限"}, 401: {"description": "未登录"}},
    )
    async def admin_settings(request: Request) -> dict[str, Any]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can manage system settings")
        settings = get_settings(request)
        runtime_config = await effective_runtime_config(request)
        return build_system_settings(settings, runtime_config, include_secrets=False)

    @app.put(
        "/v1/admin/settings",
        tags=["core-admin"],
        summary="更新系统设置",
        description="更新系统设置。部分修改需要重启服务才能生效。仅核心管理员可用。",
        responses={
            403: {"description": "需要核心管理员权限"},
            400: {"description": "参数校验失败"},
        },
    )
    async def update_admin_settings(
        request: Request,
        payload: SystemSettingsPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can manage system settings")
        settings = get_settings(request)
        runtime_config = await effective_runtime_config(request)
        validate_system_settings_payload(payload, runtime_config, settings)
        await save_system_options(
            request,
            runtime_values_from_system_settings(payload, runtime_config),
        )
        updated_runtime_config = await effective_runtime_config(request)
        updated = build_system_settings(settings, updated_runtime_config, include_secrets=False)
        request.app.state.settings = settings_from_system_settings(
            settings,
            payload,
            runtime_config,
        )
        return {
            "saved": True,
            "restart_required": settings_restart_required(settings, updated_runtime_config),
            "settings": updated,
        }

    @app.post(
        "/v1/admin/settings/email/test",
        tags=["core-admin"],
        summary="测试邮件发送",
        description="用当前邮件配置发送一封测试邮件。仅核心管理员可用。",
        responses={
            403: {"description": "需要核心管理员权限"},
            400: {"description": "收件人邮箱格式无效"},
        },
    )
    async def send_test_email(request: Request, payload: TestEmailPayload) -> dict[str, bool]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can test email settings")
        if not is_valid_email(payload.to):
            raise error(400, "Invalid recipient email")
        settings = await runtime_settings_for_app(request.app)
        await send_email(request.app, settings, payload.to, payload.subject, payload.body)
        return {"sent": True}

    @app.get(
        "/v1/admin/setup/status",
        tags=["core-admin"],
        summary="获取安装状态",
        description="返回数据库、Redis 等基础设施的安装状态。仅核心管理员可用。",
        responses={403: {"description": "需要核心管理员权限"}},
    )
    async def admin_setup_status(request: Request) -> dict[str, Any]:
        user = await require_user(request)
        if not is_core_admin(user):
            raise error(403, "Only core admin can view setup status")
        settings = get_settings(request)
        runtime_config = await effective_runtime_config(request)
        return build_setup_status_response(
            settings,
            runtime_config,
            include_saved_setup=True,
            redact_saved_setup=False,
        )

    @app.get(
        "/v1/setup/status",
        tags=["core-admin"],
        summary="获取安装状态（安装阶段）",
        description="仅在首次安装未完成时可用。返回脱敏后的安装状态。",
        responses={404: {"description": "安装已完成，此接口不可用"}},
    )
    async def setup_status(request: Request) -> dict[str, Any]:
        settings = get_settings(request)
        if not settings.is_setup_required():
            raise error(404, "Setup status is unavailable after initial setup")
        return build_setup_status_response(
            settings,
            settings_config_values(settings),
            include_saved_setup=True,
            redact_saved_setup=True,
        )

    @app.post(
        "/v1/setup",
        tags=["core-admin"],
        summary="完成首次安装",
        description="配置数据库、Redis、核心管理员等基础设施。仅首次安装未完成时可用。",
        responses={
            200: {"description": "安装成功"},
            404: {"description": "安装已完成"},
            400: {"description": "参数校验失败"},
        },
    )
    async def save_setup(
        request: Request,
        payload: SetupConfig,
    ) -> dict[str, Any]:
        settings = get_settings(request)
        if not settings.is_setup_required():
            raise error(404, "Setup is unavailable after initial setup; update .env instead")
        validate_setup_payload(payload)
        database_url = build_postgres_url(payload.postgres.model_dump())
        redis_url = build_redis_url(payload.redis.model_dump())
        core_admin_password_hash = hash_password(payload.admin.password)
        initializer = getattr(
            request.app.state,
            "setup_initializer",
            initialize_setup_infrastructure,
        )
        new_store = await resolve_optional_awaitable(
            initializer(payload, database_url, redis_url, core_admin_password_hash)
        )
        try:
            await save_system_options_to_store(
                new_store,
                {
                    "SITE_ICON_URL": payload.site.icon_url,
                    "SITE_NAME": payload.site.name,
                    "WEB_URL": payload.site.web_url,
                },
            )
            write_env_file(
                settings.env_file_path,
                setup_env_values(
                    payload,
                    database_url,
                    redis_url,
                    core_admin_password_hash,
                ),
            )
        except Exception:
            await close_setup_store(new_store)
            raise
        request.app.state.settings = settings.with_updates(
            core_admin_password_hash=core_admin_password_hash,
            core_admin_username=payload.admin.username,
            database_url=database_url,
            redis_url=redis_url,
            site_icon_url=payload.site.icon_url,
            site_name=payload.site.name,
            web_url=payload.site.web_url,
        )
        await activate_setup_store(request.app, new_store)
        return {
            "saved": True,
            "restart_required": False,
            "activated": True,
            "message": "Configuration saved and PostgreSQL/Redis storage is active.",
        }

    @app.get(
        "/v1/me",
        tags=["user"],
        summary="获取当前用户信息",
        description="返回当前登录用户的公开信息，包括角色、GitHub 信息等。",
        responses={401: {"description": "未登录"}},
    )
    async def me(request: Request) -> dict[str, Any]:
        return private_user(await require_user(request))

    @app.patch(
        "/v1/me/profile",
        tags=["user"],
        summary="更新个人资料",
        description="更新当前用户的资料，包括显示名、头像、GitHub Token、通知偏好等。",
        responses={
            401: {"description": "未登录"},
            400: {"description": "无更新字段或头像 URL 无效"},
            404: {"description": "用户不存在"},
        },
    )
    async def update_my_profile(request: Request, payload: UserProfileUpdate) -> dict[str, Any]:
        user = await require_user(request)
        profile = {key: value for key, value in payload.model_dump().items() if value is not None}
        if not profile:
            raise error(400, "No fields to update")
        if (
            "avatar_url" in profile
            and profile["avatar_url"]
            and not is_valid_public_url(profile["avatar_url"])
        ):
            raise error(400, "Avatar URL must be http(s)")
        if (
            "notification_email" in profile
            and profile["notification_email"]
            and not is_valid_email(profile["notification_email"])
        ):
            raise error(400, "Notification email is invalid")
        updated = await call_store(request, "update_user_profile", user["id"], profile)
        if not updated:
            raise error(404, "User not found")
        return private_user(updated)

    @app.get(
        "/v1/me/notifications",
        tags=["user"],
        summary="获取通知列表",
        description="返回当前用户的通知列表，支持分页。",
        responses={401: {"description": "未登录"}},
    )
    async def my_notifications(
        request: Request,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        user = await require_user(request)
        safe_limit = max(1, min(int(limit or 20), 100))
        safe_offset = max(0, int(offset or 0))
        return {
            "items": await call_store(
                request,
                "list_notifications",
                user["id"],
                safe_limit,
                safe_offset,
            ),
            "total": await call_store(request, "count_notifications", user["id"]),
            "unread_count": await call_store(request, "count_unread_notifications", user["id"]),
            "limit": safe_limit,
            "offset": safe_offset,
        }

    @app.get(
        "/v1/me/notifications/unread-count",
        tags=["user"],
        summary="获取未读通知数",
        description="返回当前用户的未读通知数量。",
        responses={401: {"description": "未登录"}},
    )
    async def my_unread_notification_count(request: Request) -> dict[str, int]:
        user = await require_user(request)
        return {"count": await call_store(request, "count_unread_notifications", user["id"])}

    @app.post(
        "/v1/me/notifications/read",
        tags=["user"],
        summary="标记通知已读",
        description="将当前用户的所有通知标记为已读。",
        responses={401: {"description": "未登录"}},
    )
    async def mark_my_notifications_read(request: Request) -> dict[str, int]:
        user = await require_user(request)
        return {"updated": await call_store(request, "mark_notifications_read", user["id"])}

    @app.delete(
        "/v1/me/notifications",
        tags=["user"],
        summary="清空全部通知",
        description="删除当前用户的所有通知。",
        responses={401: {"description": "未登录"}},
    )
    async def clear_my_notifications(request: Request) -> dict[str, int]:
        user = await require_user(request)
        return {"deleted": await call_store(request, "delete_notifications", user["id"], None)}

    @app.delete(
        "/v1/me/notifications/{notification_id}",
        tags=["user"],
        summary="删除单条通知",
        description="删除当前用户的指定通知。",
        responses={401: {"description": "未登录"}},
    )
    async def delete_my_notification(request: Request, notification_id: str) -> dict[str, int]:
        user = await require_user(request)
        return {
            "deleted": await call_store(
                request,
                "delete_notifications",
                user["id"],
                [notification_id],
            )
        }

    @app.post(
        "/v1/me/notifications/delete",
        tags=["user"],
        summary="批量删除通知",
        description="删除当前用户的多条通知。",
        responses={401: {"description": "未登录"}, 400: {"description": "缺少通知 ID"}},
    )
    async def delete_my_notifications(
        request: Request,
        payload: NotificationDeletePayload,
    ) -> dict[str, int]:
        user = await require_user(request)
        if not payload.ids:
            raise error(400, "Notification ids are required")
        return {
            "deleted": await call_store(
                request,
                "delete_notifications",
                user["id"],
                payload.ids,
            )
        }

    @app.get(
        "/v1/me/plugins",
        tags=["user"],
        summary="获取我提交的插件",
        description="返回当前用户提交的所有插件（包括未上架的）。",
        responses={401: {"description": "未登录"}},
    )
    async def my_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        user = await require_user(request)
        return {
            "items": await call_store(
                request,
                "list_user_plugins",
                user["id"],
                user.get("github_login") or "",
            )
        }

    @app.get(
        "/v1/me/api-keys",
        tags=["user"],
        summary="获取我的 API Key",
        description="返回当前用户创建的 API Key 列表（不含 Key 值）。",
        responses={401: {"description": "未登录"}},
    )
    async def my_api_keys(request: Request) -> dict[str, list[dict[str, Any]]]:
        user = await require_user(request)
        keys = await call_store(request, "list_api_keys_for_user", user["id"])
        return {"items": [public_api_key(key) for key in keys]}

    @app.post(
        "/v1/me/api-keys",
        status_code=201,
        tags=["user"],
        summary="创建 API Key",
        description="为当前用户创建一个新的 API Key，支持指定名称和权限范围。",
        responses={401: {"description": "未登录"}, 201: {"description": "创建成功"}},
    )
    async def issue_my_api_key(request: Request, payload: ApiKeyCreate) -> dict[str, Any]:
        user = await require_user(request)
        api_key = await call_store(
            request, "issue_api_key", payload.name, user["id"], payload.scopes
        )
        return public_api_key(api_key, include_key=True)

    @app.delete(
        "/v1/me/api-keys/{api_key_id}",
        tags=["user"],
        summary="删除 API Key",
        description="删除当前用户的指定 API Key。",
        responses={401: {"description": "未登录"}},
    )
    async def delete_my_api_key(request: Request, api_key_id: str) -> dict[str, int]:
        user = await require_user(request)
        return {"deleted": await call_store(request, "delete_api_key", user["id"], api_key_id)}

    @app.post(
        "/v1/auth/internal/login",
        tags=["auth"],
        summary="用户名密码登录",
        description="使用用户名和密码进行内部登录。如果站点关闭了公开登录，仅核心管理员可登录。",
        responses={
            200: {"description": "登录成功"},
            401: {"description": "用户名或密码错误"},
            403: {"description": "登录功能已关闭"},
        },
    )
    async def internal_login(request: Request, payload: InternalLoginPayload) -> Response:
        settings = await runtime_settings_for_app(request.app)
        user = await call_store(request, "get_user_by_internal_username", payload.username)
        if not user or not verify_password(payload.password, user.get("password_hash", "")):
            raise error(401, "Invalid username or password")
        if not settings.public_login_enabled and not is_core_admin(user):
            raise error(403, "Login is closed")
        session = await call_store(request, "create_session", user["id"])
        response = JSONResponse({"user": private_user(user), "session": session})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @app.get(
        "/v1/auth/github/login",
        tags=["auth"],
        summary="GitHub OAuth 登录",
        description="重定向到 GitHub OAuth 授权页面。需要在站点设置中配置 GitHub OAuth 应用。",
        responses={
            302: {"description": "重定向到 GitHub"},
            403: {"description": "GitHub 登录已关闭"},
            501: {"description": "GitHub OAuth 未配置"},
        },
    )
    async def github_login(request: Request) -> Response:
        settings = await runtime_settings_for_app(request.app)
        if not settings.github_login_enabled:
            return JSONResponse(status_code=403, content={"error": "GitHub login is disabled"})
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

    @app.get(
        "/v1/auth/github/callback",
        tags=["auth"],
        summary="GitHub OAuth 回调",
        description="处理 GitHub OAuth 授权回调，创建或更新用户会话。",
        responses={
            302: {"description": "登录成功，重定向到首页"},
            400: {"description": "无效的 OAuth 回调"},
        },
    )
    async def github_callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> Response:
        settings = await runtime_settings_for_app(request.app)
        expected_state = request.cookies.get(settings.oauth_state_cookie_name)
        if not code or not state or not expected_state or state != expected_state:
            raise error(400, "Invalid OAuth callback")

        access_token = await exchange_github_code(settings, code)
        profile = await fetch_github_profile(access_token)
        current = await current_user(request)
        profile_payload = github_profile_payload(profile)
        if current:
            user = await link_github_profile_to_user(request, current, profile_payload)
        else:
            user = await call_store(request, "upsert_github_user", profile_payload)
        await promote_org_admin_if_needed(request, user, access_token)
        session = await call_store(request, "create_session", user["id"])
        response = RedirectResponse(settings.web_url.rstrip("/"))
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        response.delete_cookie(settings.oauth_state_cookie_name, path="/")
        return response

    @app.post(
        "/v1/auth/logout",
        status_code=204,
        tags=["auth"],
        summary="退出登录",
        description="撤销当前会话并清除登录 Cookie。",
        responses={204: {"description": "退出成功"}},
    )
    async def logout(request: Request, response: Response) -> None:
        settings = await runtime_settings_for_app(request.app)
        session_token = request.cookies.get(settings.session_cookie_name)
        if session_token:
            await call_store(request, "revoke_session", session_token)
        response.delete_cookie(settings.session_cookie_name, path="/")

    @app.get(
        "/v1/auth/debug-login",
        tags=["auth"],
        summary="开发调试登录",
        description="仅供开发环境使用。通过 GitHub 用户名直接创建会话，无需 OAuth。需要启用开发认证模式。",
        responses={
            200: {"description": "登录成功"},
            403: {"description": "开发认证已关闭"},
            400: {"description": "缺少 login 参数"},
        },
    )
    async def debug_login(request: Request, login: str = "") -> Response:
        settings = await runtime_settings_for_app(request.app)
        if not settings.enable_dev_auth:
            raise error(403, "Dev auth is disabled")
        if not login.strip():
            raise error(400, "login is required")
        user = await call_store(
            request, "upsert_github_user", {"login": login.strip(), "name": login.strip()}
        )
        session = await call_store(request, "create_session", user["id"])
        response = JSONResponse({"user": private_user(user), "session": session})
        set_cookie(response, settings.session_cookie_name, session["token"], settings)
        return response

    @app.get(
        "/v1/auth/session",
        tags=["auth"],
        summary="检查当前会话",
        description="返回当前登录状态和用户信息。",
        responses={200: {"description": "已登录"}, 401: {"description": "未登录"}},
    )
    async def auth_session(request: Request) -> dict[str, Any]:
        return {"authenticated": True, "user": private_user(await require_user(request))}

    @app.get(
        "/v1/admin/check",
        tags=["admin"],
        summary="检查管理员权限",
        description="返回当前用户的管理权限详情。",
        responses={401: {"description": "未登录"}},
    )
    async def admin_check(request: Request) -> dict[str, bool]:
        user = await require_user(request)
        return {
            "core_admin": is_core_admin(user),
            "admin": is_admin(user),
            "can_moderate_plugins": can_moderate_plugins(user),
            "can_moderate_community": can_moderate_community(user),
            "can_manage_admins": can_manage_admins(user),
        }

    @app.get(
        "/v1/permissions",
        tags=["user"],
        summary="获取当前权限",
        description="返回当前用户可执行的操作列表。",
        responses={401: {"description": "未登录"}},
    )
    async def permissions(request: Request) -> dict[str, bool]:
        user = await require_user(request)
        return {
            "can_edit_any_plugin": is_admin(user),
            "can_moderate_plugins": can_moderate_plugins(user),
            "can_moderate_community": can_moderate_community(user),
            "can_publish_announcement": can_publish_announcement(user),
            "can_manage_admins": can_manage_admins(user),
        }

    @app.get(
        "/v1/plugins",
        tags=["plugins"],
        summary="获取已上架插件列表",
        description="返回所有已上架的插件。无需登录。",
        responses={200: {"description": "插件列表"}},
    )
    async def list_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"items": await call_store(request, "list_public_plugins")}

    @app.get(
        "/plugins.json",
        tags=["integration"],
        summary="AstrBot 插件源",
        description="返回 AstrBot 插件市场的完整插件源数据（机器可读格式）。无需登录。",
    )
    async def astrbot_plugin_source(request: Request) -> dict[str, dict[str, Any]]:
        return build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))

    @app.get(
        "/plugins-md5.json",
        tags=["integration"],
        summary="AstrBot 插件源 MD5",
        description="返回插件源数据的 MD5 校验值，用于增量更新检测。无需登录。",
    )
    async def astrbot_plugin_source_md5(request: Request) -> dict[str, str]:
        feed = build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))
        return {"md5": digest_plugin_source(feed)}

    @app.get("/v1/astrbot/plugins", tags=["integration"], summary="AstrBot 插件源 v1")
    @app.get("/v1/astrbot/plugins.json", tags=["integration"], summary="AstrBot 插件源 v1")
    async def astrbot_plugin_source_v1(request: Request) -> dict[str, dict[str, Any]]:
        return build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))

    @app.get(
        "/v1/astrbot/plugins-md5.json",
        tags=["integration"],
        summary="AstrBot 插件源 v1 MD5",
        description="返回 v1 插件源数据的 MD5 校验值。",
    )
    async def astrbot_plugin_source_v1_md5(request: Request) -> dict[str, str]:
        feed = build_astrbot_plugin_source(await call_store(request, "list_public_plugins"))
        return {"md5": digest_plugin_source(feed)}

    @app.get(
        "/v1/plugins/submissions",
        tags=["submissions"],
        summary="获取待审核提交列表",
        description="获取所有待审核的插件提交。仅管理员可用。",
        responses={401: {"description": "未登录"}, 403: {"description": "需要管理员权限"}},
    )
    async def list_submissions(request: Request) -> dict[str, list[dict[str, Any]]]:
        user = await require_user(request)
        if not is_admin(user):
            raise error(403, "Forbidden")
        return {"items": await call_store(request, "list_submissions")}

    @app.post(
        "/v1/plugins/submissions/metadata-preview",
        tags=["submissions"],
        summary="预取插件提交元数据",
        description="根据 GitHub 仓库地址预取可用于插件提交表单的元数据。需要登录并验证仓库归属。",
        responses={
            200: {"description": "预取成功"},
            401: {"description": "未登录"},
            403: {"description": "插件提交已关闭或无仓库权限"},
            400: {"description": "参数校验失败"},
            429: {"description": "GitHub API 速率限制"},
        },
    )
    async def preview_submission_metadata(
        request: Request,
        payload: PluginSubmissionMetadataPreviewPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        await enforce_user_rpm_limit(
            request,
            "submission_metadata_preview",
            user,
            SUBMISSION_METADATA_PREVIEW_RPM,
        )
        settings = await runtime_settings_for_app(request.app)
        if not settings.market_submissions_enabled:
            raise error(403, "Plugin submissions are closed")
        validate_repo_owner(payload.repo, user)
        try:
            return await fetch_plugin_submission_metadata_preview(
                payload.repo,
                settings,
                user,
                store=request.app.state.store,
            )
        except GithubMetadataError as exc:
            raise error(exc.status_code, exc.message) from exc

    @app.post(
        "/v1/plugins/submissions",
        status_code=201,
        tags=["submissions"],
        summary="提交新插件",
        description="提交一个新插件到市场。需要验证 GitHub 仓库归属。如果启用自动审核，提交后直接上架。",
        responses={
            201: {"description": "提交成功"},
            401: {"description": "未登录"},
            403: {"description": "插件提交已关闭"},
            400: {"description": "参数校验失败"},
        },
    )
    async def submit_plugin(request: Request, payload: PluginSubmission) -> dict[str, Any]:
        user = await require_user(request)
        settings = await runtime_settings_for_app(request.app)
        if not settings.market_submissions_enabled:
            raise error(403, "Plugin submissions are closed")
        data = payload.model_dump()
        validate_plugin_submission(data, settings)
        validate_repo_owner(data["repo"], user)
        data.update(
            await safe_fetch_plugin_github_metadata(
                data["repo"],
                settings,
                user,
                store=request.app.state.store,
            )
        )
        plugin = await call_store(request, "submit_plugin", user, data)
        if settings.plugin_auto_approve_enabled:
            listed = await call_store(
                request, "update_plugin_status", plugin["id"], "listed", user["id"]
            )
            if listed:
                await notify_plugin_review(request, listed, user, auto_approved=True)
            return listed or plugin
        await notify_pending_plugin_review(request, plugin, user)
        return plugin

    @app.get(
        "/v1/plugins/{plugin_id}",
        tags=["plugins"],
        summary="获取插件详情",
        description="返回指定插件的详细信息，包括描述、作者、版本、交互状态等。无需登录。",
        responses={200: {"description": "插件详情"}, 404: {"description": "插件不存在"}},
    )
    async def plugin_detail(request: Request, plugin_id: str) -> dict[str, Any]:
        plugin = await get_plugin_or_404(request, plugin_id)
        user = await current_user(request)
        return await plugin_with_interaction_state(request, plugin, user)

    @app.patch(
        "/v1/plugins/{plugin_id}",
        tags=["submissions"],
        summary="更新插件信息",
        description="更新插件信息。仅插件作者或管理员可操作。",
        responses={
            200: {"description": "更新成功"},
            401: {"description": "未登录"},
            403: {"description": "无权编辑此插件"},
            404: {"description": "插件不存在"},
        },
    )
    async def update_plugin(
        request: Request, plugin_id: str, payload: PluginPatch
    ) -> dict[str, Any]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        patch = payload.model_dump(exclude_unset=True)
        if "name" in patch:
            validate_plugin_name(patch["name"])
        if "repo" in patch:
            validate_github_repo(patch["repo"])
            validate_repo_owner(patch["repo"], user)
            patch.update(
                await safe_fetch_plugin_github_metadata(
                    patch["repo"],
                    await runtime_settings_for_app(request.app),
                    user,
                )
            )
        if "tags" in patch:
            validate_plugin_tag_count(
                patch.get("tags") or [],
                await runtime_settings_for_app(request.app),
            )
        if "category" in patch:
            patch["category"] = validate_plugin_category(patch.get("category", ""))
        updated = await call_store(
            request,
            "update_plugin_metadata",
            plugin_id,
            {
                **patch,
                "owner_user_id": plugin["owner_user_id"],
                "owner_github_login": plugin["owner_github_login"],
            },
        )
        return updated or {}

    @app.post(
        "/v1/plugins/{plugin_id}/request-list",
        tags=["submissions"],
        summary="申请上架",
        description="插件作者申请将插件上架。如果插件已上架则直接返回。",
        responses={
            200: {"description": "已申请上架或已上架"},
            401: {"description": "未登录"},
            403: {"description": "无权操作"},
            404: {"description": "插件不存在"},
        },
    )
    async def request_plugin_list(request: Request, plugin_id: str) -> dict[str, Any]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        if plugin.get("status") == "listed":
            return plugin
        updated = await call_store(request, "request_plugin_listing", plugin_id, user["id"])
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.post(
        "/v1/plugins/{plugin_id}/unlist",
        tags=["submissions"],
        summary="作者下架插件",
        description="插件作者下架自己的插件。可附下架原因。",
        responses={
            200: {"description": "下架成功"},
            401: {"description": "未登录"},
            403: {"description": "无权操作"},
            404: {"description": "插件不存在"},
        },
    )
    async def unlist_own_plugin(
        request: Request,
        plugin_id: str,
        payload: PluginUnlistPayload | None = None,
    ) -> dict[str, Any]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        reason = (payload.reason if payload else "").strip() or "作者主动下架"
        updated = await call_store(request, "unlist_plugin", plugin_id, user["id"], reason)
        if not updated:
            raise error(404, "Plugin not found")
        return updated

    @app.post(
        "/v1/plugins/{plugin_id}/refresh-github",
        tags=["submissions"],
        summary="刷新插件 GitHub 元数据",
        description="手动触发刷新插件的 GitHub 元数据（stars、描述、README 等）。",
        responses={
            200: {"description": "刷新成功"},
            401: {"description": "未登录"},
            403: {"description": "无权操作"},
        },
    )
    async def refresh_own_plugin_github_metadata(
        request: Request,
        plugin_id: str,
        payload: PluginGithubRefreshPayload | None = None,
    ) -> dict[str, Any]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_edit_plugin(user, plugin):
            raise error(403, "Forbidden")
        refresh_payload = payload or PluginGithubRefreshPayload()
        updated = await refresh_plugin_github_metadata(
            request,
            plugin_id,
            user,
            token=refresh_payload.github_token,
            save_token=refresh_payload.save_token,
            refresh_interval_seconds=refresh_payload.refresh_interval_seconds,
            raise_errors=True,
        )
        return updated or plugin

    @app.post(
        "/v1/plugins/{plugin_id}/like",
        tags=["comments"],
        summary="点赞插件",
        description="为指定插件点赞。需要登录。",
        responses={
            200: {"description": "点赞成功"},
            401: {"description": "未登录"},
            403: {"description": "点赞功能已关闭"},
            404: {"description": "插件不存在"},
        },
    )
    async def like_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        if not (await runtime_settings_for_app(request.app)).market_likes_enabled:
            raise error(403, "Plugin likes are closed")
        user = await require_user(request)
        original_plugin = await get_plugin_or_404(request, plugin_id)
        original_likes = int(original_plugin.get("likes") or 0)
        plugin = await call_store(request, "like_plugin", plugin_id, user["id"])
        if plugin and int(plugin.get("likes") or 0) > original_likes:
            await notify_plugin_like(request, original_plugin, user)
        return await plugin_with_interaction_state(request, plugin, user)

    @app.post(
        "/v1/plugins/{plugin_id}/unlike",
        tags=["comments"],
        summary="取消点赞插件",
        description="取消对指定插件的点赞。需要登录。",
        responses={
            200: {"description": "取消成功"},
            401: {"description": "未登录"},
            403: {"description": "点赞功能已关闭"},
        },
    )
    async def unlike_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        if not (await runtime_settings_for_app(request.app)).market_likes_enabled:
            raise error(403, "Plugin likes are closed")
        user = await require_user(request)
        await get_plugin_or_404(request, plugin_id)
        plugin = await call_store(request, "unlike_plugin", plugin_id, user["id"])
        return await plugin_with_interaction_state(request, plugin, user)

    @app.post(
        "/v1/plugins/{plugin_id}/comments",
        status_code=201,
        tags=["comments"],
        summary="发表评论",
        description="在指定插件下发表评论，支持回复（传 parent_id）。被禁言用户无法评论。",
        responses={
            201: {"description": "评论成功"},
            401: {"description": "未登录"},
            403: {"description": "评论功能已关闭或用户被禁言"},
            400: {"description": "评论内容为空或父评论无效"},
        },
    )
    async def add_comment(
        request: Request, plugin_id: str, payload: CommentCreate
    ) -> dict[str, Any]:
        if not (await runtime_settings_for_app(request.app)).market_comments_enabled:
            raise error(403, "Plugin comments are closed")
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not payload.body:
            raise error(400, "Comment body is required")
        parent_comment = None
        if payload.parent_id:
            parent_comment = await call_store(request, "get_comment", payload.parent_id)
            if (
                not parent_comment
                or parent_comment.get("deleted")
                or parent_comment.get("plugin_id") != plugin_id
            ):
                raise error(400, "Parent comment is invalid")
        muted_until = parse_iso_datetime(user.get("muted_until"))
        if muted_until and muted_until > datetime.now(UTC):
            raise error(403, "User is muted")
        comment = await call_store(
            request, "add_comment", plugin_id, user["id"], payload.body, payload.parent_id
        )
        if parent_comment:
            await notify_comment_reply(request, plugin, parent_comment, comment, user)
        else:
            await notify_plugin_comment(request, plugin, comment, user)
        return comment

    @app.post(
        "/v1/comments/{comment_id}/like",
        tags=["comments"],
        summary="点赞评论",
        description="为指定评论点赞。需要登录。",
        responses={
            200: {"description": "点赞成功"},
            401: {"description": "未登录"},
            403: {"description": "点赞功能已关闭"},
            404: {"description": "评论不存在"},
        },
    )
    async def like_comment(request: Request, comment_id: str) -> dict[str, Any]:
        if not (await runtime_settings_for_app(request.app)).market_likes_enabled:
            raise error(403, "Comment likes are closed")
        user = await require_user(request)
        original_comment = await call_store(request, "get_comment", comment_id)
        if not original_comment:
            raise error(404, "Comment not found")
        original_likes = int(original_comment.get("likes") or 0)
        comment = await call_store(request, "like_comment", comment_id, user["id"])
        if not comment:
            raise error(404, "Comment not found")
        if int(comment.get("likes") or 0) > original_likes:
            plugin = await call_store(request, "get_plugin", comment.get("plugin_id"))
            await notify_comment_like(request, plugin, original_comment, user)
        return with_comment_permissions(comment, user, liked=True)

    @app.post(
        "/v1/comments/{comment_id}/unlike",
        tags=["comments"],
        summary="取消点赞评论",
        description="取消对指定评论的点赞。需要登录。",
        responses={
            200: {"description": "取消成功"},
            401: {"description": "未登录"},
            403: {"description": "点赞功能已关闭"},
            404: {"description": "评论不存在"},
        },
    )
    async def unlike_comment(request: Request, comment_id: str) -> dict[str, Any]:
        if not (await runtime_settings_for_app(request.app)).market_likes_enabled:
            raise error(403, "Comment likes are closed")
        user = await require_user(request)
        comment = await call_store(request, "unlike_comment", comment_id, user["id"])
        if not comment:
            raise error(404, "Comment not found")
        return with_comment_permissions(comment, user, liked=False)

    @app.delete(
        "/v1/comments/{comment_id}",
        tags=["comments"],
        summary="删除评论",
        description="删除指定评论。仅评论作者或管理员可操作。",
        responses={
            200: {"description": "删除成功"},
            401: {"description": "未登录"},
            403: {"description": "无权删除"},
            404: {"description": "评论不存在"},
        },
    )
    async def delete_own_comment(request: Request, comment_id: str) -> dict[str, Any]:
        user = await require_user(request)
        return await delete_comment_by_user(request, comment_id, user)

    @app.post(
        "/v1/plugins/{plugin_id}/reindex",
        tags=["submissions"],
        summary="重新索引插件",
        description="触发重新索引插件搜索数据。仅插件作者或管理员可操作。",
        responses={
            200: {"description": "索引完成"},
            401: {"description": "未登录"},
            403: {"description": "无权操作"},
        },
    )
    async def reindex_plugin(request: Request, plugin_id: str) -> dict[str, bool]:
        user = await require_user(request)
        plugin = await get_plugin_or_404(request, plugin_id)
        if not can_manage_plugin_submission(user, plugin):
            raise error(403, "Forbidden")
        return {"ok": True}

    @app.get(
        "/v1/admin/users",
        tags=["admin"],
        summary="获取所有用户",
        description="返回所有用户的公开信息。仅管理员可用。",
        responses={401: {"description": "未登录"}, 403: {"description": "需要管理员权限"}},
    )
    async def admin_users(request: Request) -> dict[str, list[dict[str, Any]]]:
        await require_admin(request)
        users = await call_store(request, "list_users")
        return {"items": [public_user(user) for user in users]}

    @app.get(
        "/v1/admin/plugins",
        tags=["admin"],
        summary="获取所有插件",
        description="返回所有插件（含未上架）。仅管理员可用。",
        responses={401: {"description": "未登录"}, 403: {"description": "需要管理员权限"}},
    )
    async def admin_plugins(request: Request) -> dict[str, list[dict[str, Any]]]:
        await require_admin(request)
        return {"items": await call_store(request, "list_plugins")}

    @app.get(
        "/v1/admin/summary",
        tags=["admin"],
        summary="获取后台统计摘要",
        description="返回后台统计数据，包括用户数、插件数、评论数等。仅管理员可用。",
        responses={401: {"description": "未登录"}, 403: {"description": "需要管理员权限"}},
    )
    async def admin_summary(request: Request) -> dict[str, Any]:
        user = await require_admin(request)
        summary = await call_store(request, "summary")
        return {**summary, "role": user["role"]}

    @app.post(
        "/v1/admin/plugins/{plugin_id}/list",
        tags=["admin"],
        summary="审核上架插件",
        description="将指定插件的状态改为已上架。会通知插件作者。仅管理员可用。",
        responses={
            200: {"description": "上架成功"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
            404: {"description": "插件不存在"},
        },
    )
    async def list_plugin(
        request: Request,
        plugin_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        plugin = await get_plugin_or_404(request, plugin_id)
        previous_status = plugin.get("status")
        updated = await call_store(request, "update_plugin_status", plugin_id, "listed", user["id"])
        if not updated:
            raise error(404, "Plugin not found")
        queue_plugin_github_metadata_refresh(background_tasks, request.app, updated, user)
        if previous_status != "listed" and updated.get("owner_user_id"):
            await notify_plugin_review(request, updated, user)
        return updated

    @app.post(
        "/v1/admin/plugins/{plugin_id}/refresh-github",
        status_code=202,
        tags=["admin"],
        summary="管理员刷新 GitHub 元数据",
        description="管理员触发刷新指定插件的 GitHub 元数据。异步执行。",
        responses={
            202: {"description": "已接受，异步处理中"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
        },
    )
    async def refresh_admin_plugin_github_metadata(
        request: Request,
        plugin_id: str,
        background_tasks: BackgroundTasks,
        payload: PluginGithubRefreshPayload | None = None,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        plugin = await get_plugin_or_404(request, plugin_id)
        refresh_payload = payload or PluginGithubRefreshPayload()
        user = await update_user_github_sync_preferences(
            request,
            user,
            refresh_payload.github_token,
            refresh_payload.save_token,
            refresh_payload.refresh_interval_seconds,
        )
        queue_plugin_github_metadata_refresh(
            background_tasks,
            request.app,
            plugin,
            user,
            token=refresh_payload.github_token,
        )
        return {"accepted": True, "plugin_id": plugin_id}

    @app.post(
        "/v1/admin/plugins/{plugin_id}/unlist",
        tags=["admin"],
        summary="管理员下架插件",
        description="管理员下架指定插件。必须提供下架原因。会通知插件作者。",
        responses={
            200: {"description": "下架成功"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
            400: {"description": "缺少下架原因"},
            404: {"description": "插件不存在"},
        },
    )
    async def unlist_plugin(
        request: Request,
        plugin_id: str,
        payload: PluginUnlistPayload,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_plugins(user):
            raise error(403, "Forbidden")
        if not payload.reason:
            raise error(400, "Unlist reason is required")
        plugin = await get_plugin_or_404(request, plugin_id)
        updated = await call_store(request, "unlist_plugin", plugin_id, user["id"], payload.reason)
        if not updated:
            raise error(404, "Plugin not found")
        if plugin.get("owner_user_id"):
            await notify_plugin_unlisted(request, updated, user, payload.reason)
        return updated

    @app.delete(
        "/v1/admin/comments/{comment_id}",
        tags=["admin"],
        summary="删除评论（管理员）",
        description="管理员删除任意评论。",
        responses={
            200: {"description": "删除成功"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
            404: {"description": "评论不存在"},
        },
    )
    async def delete_comment(request: Request, comment_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        return await delete_comment_by_user(request, comment_id, user)

    @app.post(
        "/v1/admin/users/{user_id}/mute",
        tags=["admin"],
        summary="禁言用户",
        description="禁言指定用户，可设置禁言到期时间和原因。仅管理员可用。",
        responses={
            200: {"description": "禁言成功"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
            404: {"description": "用户不存在"},
        },
    )
    async def mute_user(request: Request, user_id: str, payload: MuteUserPayload) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        muted_until = payload.muted_until or (datetime.now(UTC) + timedelta(days=1)).isoformat()
        muted = await call_store(
            request,
            "mute_user",
            user_id,
            muted_until,
            user["id"],
            payload.reason or "",
        )
        if not muted:
            raise error(404, "User not found")
        return public_user(muted)

    @app.post(
        "/v1/admin/users/{user_id}/unmute",
        tags=["admin"],
        summary="解除禁言",
        description="解除指定用户的禁言状态。仅管理员可用。",
        responses={
            200: {"description": "解除成功"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
            404: {"description": "用户不存在"},
        },
    )
    async def unmute_user(request: Request, user_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_moderate_community(user):
            raise error(403, "Forbidden")
        unmuted = await call_store(request, "unmute_user", user_id)
        if not unmuted:
            raise error(404, "User not found")
        return public_user(unmuted)

    @app.post(
        "/v1/core/users",
        status_code=201,
        tags=["core-admin"],
        summary="创建内部用户",
        description="创建一个新的内部用户账号（非 GitHub 登录）。仅核心管理员可用。",
        responses={
            201: {"description": "创建成功"},
            401: {"description": "未登录"},
            403: {"description": "需要核心管理员权限"},
            400: {"description": "参数校验失败"},
            409: {"description": "用户名已存在"},
        },
    )
    async def create_internal_user(
        request: Request,
        payload: InternalUserCreate,
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_manage_admins(user):
            raise error(403, "Forbidden")
        username = payload.username.strip()
        password = payload.password
        role = normalize_role(payload.role)
        if role == Role.CORE_ADMIN:
            raise error(400, "Cannot create core admin from user management")
        if len(username) < 3:
            raise error(400, "Username must be at least 3 characters")
        if len(password) < 8:
            raise error(400, "Password must be at least 8 characters")
        if await call_store(request, "get_user_by_internal_username", username):
            raise error(409, "Username already exists")
        created = await call_store(
            request,
            "create_internal_user",
            username,
            hash_password(password),
            role.value,
        )
        return public_user(created)

    @app.delete(
        "/v1/core/users/{user_id}",
        tags=["core-admin"],
        summary="删除用户",
        description="删除指定用户账号。不能删除自己或核心管理员。仅核心管理员可用。",
        responses={
            200: {"description": "删除成功"},
            401: {"description": "未登录"},
            403: {"description": "需要核心管理员权限"},
            400: {"description": "不能删除自己或核心管理员"},
            404: {"description": "用户不存在"},
        },
    )
    async def delete_user(request: Request, user_id: str) -> dict[str, Any]:
        user = await require_user(request)
        if not can_manage_admins(user):
            raise error(403, "Forbidden")
        if user_id == user["id"]:
            raise error(400, "Cannot delete yourself")
        target = await call_store(request, "get_user_by_id", user_id)
        if not target:
            raise error(404, "User not found")
        if normalize_role(target.get("role")) == Role.CORE_ADMIN:
            raise error(400, "Cannot delete core admin")
        deleted = await call_store(request, "delete_user", user_id, user["id"])
        return {"deleted": bool(deleted)}

    @app.post(
        "/v1/core/admins/{user_id}",
        tags=["core-admin"],
        summary="修改管理员角色",
        description="设置或取消用户的管理员角色。不能修改核心管理员角色。仅核心管理员可用。",
        responses={
            200: {"description": "修改成功"},
            401: {"description": "未登录"},
            403: {"description": "需要核心管理员权限"},
            400: {"description": "不能修改核心管理员角色"},
            404: {"description": "用户不存在"},
        },
    )
    async def update_admin(
        request: Request, user_id: str, payload: RoleUpdatePayload
    ) -> dict[str, Any]:
        user = await require_user(request)
        if not can_manage_admins(user):
            raise error(403, "Forbidden")
        target = await call_store(request, "get_user_by_id", user_id)
        if not target:
            raise error(404, "User not found")
        if normalize_role(target.get("role")) == Role.CORE_ADMIN:
            raise error(400, "Cannot change core admin role")
        updated = await call_store(
            request, "update_user_role", user_id, "admin" if payload.role == "admin" else "user"
        )
        return public_user(updated) if updated else {}

    @app.post(
        "/v1/core/announcements",
        status_code=201,
        tags=["core-admin"],
        summary="发布公告",
        description="发布一条全站公告。仅核心管理员可用。",
        responses={
            201: {"description": "发布成功"},
            401: {"description": "未登录"},
            403: {"description": "需要核心管理员权限"},
            400: {"description": "标题和内容不能为空"},
        },
    )
    async def create_announcement(request: Request, payload: AnnouncementCreate) -> dict[str, Any]:
        user = await require_user(request)
        if not can_publish_announcement(user):
            raise error(403, "Forbidden")
        if not payload.title or not payload.body:
            raise error(400, "Announcement title and body are required")
        return await call_store(
            request, "publish_announcement", payload.title, payload.body, user["id"]
        )

    @app.get(
        "/v1/announcements",
        tags=["announcements"],
        summary="获取公告列表",
        description="返回全站公告列表。无需登录。",
    )
    async def announcements(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"items": await call_store(request, "list_announcements")}

    @app.post(
        "/v1/api-keys",
        status_code=201,
        tags=["admin"],
        summary="签发 API Key",
        description="签发一个新的 API Key。仅管理员可用。",
        responses={
            201: {"description": "签发成功"},
            401: {"description": "未登录"},
            403: {"description": "需要管理员权限"},
        },
    )
    async def issue_api_key(request: Request, payload: ApiKeyCreate) -> dict[str, Any]:
        user = await require_user(request)
        if not is_admin(user):
            raise error(403, "Forbidden")
        return await call_store(request, "issue_api_key", payload.name, user["id"], payload.scopes)

    @app.get(
        "/v1/api-keys",
        tags=["admin"],
        summary="获取 API Key 列表",
        description="返回所有 API Key 列表。需要携带有效的 API Key。",
        responses={
            200: {"description": "获取成功"},
            401: {"description": "API Key 无效"},
            403: {"description": "权限不足"},
        },
    )
    async def api_keys(request: Request) -> dict[str, list[dict[str, Any]]]:
        keys = await all_api_keys(request)
        ok, status, message = require_api_key(
            request.headers.get("authorization"), keys, "market:read"
        )
        if not ok:
            raise error(status, message)
        return {"items": [public_api_key(key) for key in keys]}

    @app.get("/openapi.json", include_in_schema=False)
    async def custom_openapi(request: Request) -> JSONResponse:
        user = await current_user(request)
        role = role_for_openapi(user)
        if not hasattr(request.app.state, "_openapi_schema_cache"):
            base = get_openapi(
                title=request.app.title,
                version=request.app.version,
                routes=request.app.routes,
            )
            base["tags"] = request.app.openapi_tags or []
            request.app.state._openapi_schema_cache = base
        base_schema = request.app.state._openapi_schema_cache
        filtered = filter_openapi_by_role(base_schema, role)
        return JSONResponse(filtered)

    @app.get("/llms.txt", include_in_schema=False)
    async def llms_txt(request: Request) -> PlainTextResponse:
        user = await current_user(request)
        role = role_for_openapi(user)
        return PlainTextResponse(build_llms_txt(role), media_type="text/plain; charset=utf-8")


def register_market_web_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def market_web_index() -> Response:
        return serve_market_web_file("")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def market_web_fallback(full_path: str) -> Response:
        if is_reserved_api_path(full_path):
            raise error(404, "Not found")
        return serve_market_web_file(full_path)


def serve_market_web_file(full_path: str) -> FileResponse:
    index_file = MARKET_WEB_DIST / "index.html"
    if not index_file.is_file():
        raise error(404, "Market web build is missing. Run npm run build:web first.")

    requested_file = resolve_market_web_file(full_path)
    return FileResponse(requested_file or index_file)


def resolve_market_web_file(full_path: str) -> Path | None:
    dist_dir = MARKET_WEB_DIST.resolve()
    candidate = (dist_dir / full_path).resolve()
    try:
        candidate.relative_to(dist_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def is_reserved_api_path(full_path: str) -> bool:
    path = full_path.strip("/")
    return path in RESERVED_WEB_PATHS or path.startswith(RESERVED_WEB_PREFIXES)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def load_system_options(store: Any) -> dict[str, str]:
    method = getattr(store, "list_options", None)
    if not method:
        return {}
    values = await resolve_optional_awaitable(method())
    if not isinstance(values, dict):
        return {}
    return {str(key): str(value) for key, value in values.items() if key in SYSTEM_OPTION_KEYS}


async def runtime_settings_for_app(app: FastAPI) -> Settings:
    settings = app.state.settings
    return settings_with_runtime_config(
        settings,
        await load_system_options(app.state.store),
    )


def settings_with_runtime_config(settings: Settings, runtime_config: dict[str, str]) -> Settings:
    if not runtime_config:
        return settings
    return settings.with_updates(
        web_url=runtime_config.get("WEB_URL", settings.web_url),
        site_name=runtime_config.get("SITE_NAME", settings.site_name),
        site_icon_url=runtime_config.get("SITE_ICON_URL", settings.site_icon_url),
        site_subtitle=runtime_config.get("SITE_SUBTITLE", settings.site_subtitle),
        site_description=runtime_config.get("SITE_DESCRIPTION", settings.site_description),
        site_contact_email=runtime_config.get(
            "SITE_CONTACT_EMAIL",
            settings.site_contact_email,
        ),
        site_docs_url=runtime_config.get("SITE_DOCS_URL", settings.site_docs_url),
        github_client_id=runtime_config.get("GITHUB_CLIENT_ID", settings.github_client_id),
        github_client_secret=settings.github_client_secret,
        github_callback_url=runtime_config.get(
            "GITHUB_CALLBACK_URL",
            settings.github_callback_url,
        ),
        github_scope=runtime_config.get("GITHUB_SCOPE", settings.github_scope),
        github_admin_org=runtime_config.get("GITHUB_ADMIN_ORG", settings.github_admin_org),
        github_api_token=runtime_config.get("GITHUB_API_TOKEN", settings.github_api_token),
        github_metadata_sync_enabled=parse_bool(
            runtime_config.get("GITHUB_METADATA_SYNC_ENABLED"),
            settings.github_metadata_sync_enabled,
        ),
        github_metadata_sync_interval_seconds=clamp_sync_interval(
            runtime_config.get(
                "GITHUB_METADATA_SYNC_INTERVAL_SECONDS",
                str(settings.github_metadata_sync_interval_seconds),
            )
        ),
        github_login_enabled=parse_bool(
            runtime_config.get("GITHUB_LOGIN_ENABLED"),
            settings.github_login_enabled,
        ),
        public_login_enabled=parse_bool(
            runtime_config.get("PUBLIC_LOGIN_ENABLED"),
            settings.public_login_enabled,
        ),
        login_agreement_enabled=parse_bool(
            runtime_config.get("LOGIN_AGREEMENT_ENABLED"),
            settings.login_agreement_enabled,
        ),
        login_agreement_text=runtime_config.get(
            "LOGIN_AGREEMENT_TEXT",
            settings.login_agreement_text,
        ),
        service_terms_enabled=parse_bool(
            runtime_config.get("SERVICE_TERMS_ENABLED"),
            settings.service_terms_enabled,
        ),
        service_terms_text=runtime_config.get(
            "SERVICE_TERMS_TEXT",
            settings.service_terms_text,
        ),
        market_submissions_enabled=parse_bool(
            runtime_config.get("MARKET_SUBMISSIONS_ENABLED"),
            settings.market_submissions_enabled,
        ),
        market_comments_enabled=parse_bool(
            runtime_config.get("MARKET_COMMENTS_ENABLED"),
            settings.market_comments_enabled,
        ),
        market_likes_enabled=parse_bool(
            runtime_config.get("MARKET_LIKES_ENABLED"),
            settings.market_likes_enabled,
        ),
        plugin_auto_approve_enabled=parse_bool(
            runtime_config.get("PLUGIN_AUTO_APPROVE_ENABLED"),
            settings.plugin_auto_approve_enabled,
        ),
        max_plugin_tags=parse_int(runtime_config.get("MAX_PLUGIN_TAGS"), settings.max_plugin_tags),
        email_provider=normalize_email_provider(
            runtime_config.get("EMAIL_PROVIDER", settings.email_provider)
        ),
        smtp_host=runtime_config.get("SMTP_HOST", settings.smtp_host),
        smtp_port=parse_int(runtime_config.get("SMTP_PORT"), settings.smtp_port),
        smtp_username=runtime_config.get("SMTP_USERNAME", settings.smtp_username),
        smtp_password=runtime_config.get("SMTP_PASSWORD", settings.smtp_password),
        smtp_from=runtime_config.get("SMTP_FROM", settings.smtp_from),
        smtp_from_name=sender_name(
            runtime_config.get("SMTP_FROM_NAME", settings.smtp_from_name),
        ),
        smtp_ssl=normalize_smtp_encryption(
            runtime_config.get("SMTP_ENCRYPTION", settings.smtp_encryption)
        )
        == "ssl_tls",
        smtp_encryption=normalize_smtp_encryption(
            runtime_config.get("SMTP_ENCRYPTION", settings.smtp_encryption)
        ),
        smtp_auth_method=normalize_smtp_auth_method(
            runtime_config.get("SMTP_AUTH_METHOD", settings.smtp_auth_method)
        ),
        smtp_validate_certs=parse_bool(
            runtime_config.get("SMTP_VALIDATE_CERTS"),
            settings.smtp_validate_certs,
        ),
        cloudflare_email_account_id=runtime_config.get(
            "CLOUDFLARE_EMAIL_ACCOUNT_ID",
            settings.cloudflare_email_account_id,
        ),
        cloudflare_email_api_token=runtime_config.get(
            "CLOUDFLARE_EMAIL_API_TOKEN",
            settings.cloudflare_email_api_token,
        ),
        cloudflare_email_from=runtime_config.get(
            "CLOUDFLARE_EMAIL_FROM",
            settings.cloudflare_email_from,
        ),
        cloudflare_email_from_name=sender_name(
            runtime_config.get(
                "CLOUDFLARE_EMAIL_FROM_NAME",
                settings.cloudflare_email_from_name,
            ),
        ),
        email_daily_limit=parse_int(
            runtime_config.get("EMAIL_DAILY_LIMIT"),
            settings.email_daily_limit,
        ),
        email_verification_daily_limit_per_user=parse_int(
            runtime_config.get("EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER"),
            settings.email_verification_daily_limit_per_user,
        ),
    )


def get_store(request: Request) -> InMemoryMarketStore | PgRedisMarketStore:
    return request.app.state.store


async def call_store(request: Request, method_name: str, *args: Any) -> Any:
    method = getattr(get_store(request), method_name)
    result = method(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def effective_runtime_config(request: Request) -> dict[str, str]:
    settings = get_settings(request)
    system_options = await load_system_options(get_store(request))
    return {**settings_config_values(settings), **system_options}


async def save_system_options(request: Request, values: dict[str, str]) -> dict[str, str]:
    return await save_system_options_to_store(get_store(request), values)


async def save_system_options_to_store(store: Any, values: dict[str, str]) -> dict[str, str]:
    system_values = {key: str(value) for key, value in values.items() if key in SYSTEM_OPTION_KEYS}
    method = getattr(store, "upsert_options", None)
    if not method:
        return {}
    return await resolve_optional_awaitable(method(system_values))


async def current_user(request: Request) -> dict[str, Any] | None:
    settings = get_settings(request)
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        user = await call_store(request, "get_user_by_session", session_token)
        if user:
            return user

    if not settings.enable_dev_auth:
        return None
    dev_login = request.headers.get("x-dev-github-login", "").strip()
    if not dev_login:
        return None
    return await call_store(request, "upsert_github_user", {"login": dev_login, "name": dev_login})


async def require_user(request: Request) -> dict[str, Any]:
    user = await current_user(request)
    if not user:
        raise error(401, "Not authenticated")
    return user


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {**user, "has_github_token": bool(user.get("github_token"))}.items()
        if key not in {"password_hash", "github_token", *PRIVATE_USER_FIELDS}
    }


def private_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        **public_user(user),
        "github_email": user.get("github_email") or "",
        "notification_email": user.get("notification_email") or "",
    }


async def require_admin(request: Request) -> dict[str, Any]:
    user = await require_user(request)
    if not is_admin(user):
        raise error(403, "Forbidden")
    return user


async def get_plugin_or_404(request: Request, plugin_id: str) -> dict[str, Any]:
    plugin = await call_store(request, "get_plugin", plugin_id)
    if not plugin:
        raise error(404, "Plugin not found")
    return plugin


def notification_preference_enabled(user: dict[str, Any] | None, key: str) -> bool:
    if not user:
        return False
    return user.get(key, True) is not False


def user_display_name(user: dict[str, Any]) -> str:
    return (
        user.get("github_name")
        or user.get("github_login")
        or user.get("internal_username")
        or "用户"
    )


def plugin_display_name(plugin: dict[str, Any] | None) -> str:
    if not plugin:
        return "插件"
    return plugin.get("display_name") or plugin.get("name") or plugin.get("id") or "插件"


def notification_excerpt(value: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def notification_email_address(user: dict[str, Any]) -> str:
    return str(user.get("notification_email") or user.get("github_email") or "").strip()


def notification_email_footer() -> str:
    return "\n\n如果不想继续接收此类邮件，可到个人设置的通知偏好中关闭对应邮件通知。"


async def send_preference_email(
    request: Request,
    recipient: dict[str, Any] | None,
    preference_key: str | None,
    subject: str,
    body: str,
) -> None:
    if not preference_key or not notification_preference_enabled(recipient, preference_key):
        return
    receiver = notification_email_address(recipient or {})
    if not receiver or not is_valid_email(receiver):
        return
    try:
        settings = await runtime_settings_for_app(request.app)
        if settings.email_provider == "disabled":
            return
        await send_email(
            request.app,
            settings,
            receiver,
            f"{settings.site_name} - {subject}",
            f"{body}{notification_email_footer()}",
        )
    except HTTPException as exc:
        LOGGER.warning(
            "Notification email failed for user %s: %s",
            (recipient or {}).get("id"),
            exc.detail,
        )
    except Exception as exc:
        LOGGER.warning(
            "Notification email failed for user %s: %s",
            (recipient or {}).get("id"),
            safe_exception_message(exc),
        )


async def create_preference_notification(
    request: Request,
    recipient_user_id: str | None,
    actor_user_id: str | None,
    preference_key: str,
    title: str,
    body: str,
    notification_type: str,
    metadata: dict[str, Any],
    email_preference_key: str | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
    skip_self: bool = True,
) -> None:
    if not recipient_user_id:
        return
    if skip_self and actor_user_id and recipient_user_id == actor_user_id:
        return
    recipient = await call_store(request, "get_user_by_id", recipient_user_id)
    if not recipient:
        return
    if notification_preference_enabled(recipient, preference_key):
        await call_store(
            request,
            "create_notification",
            recipient_user_id,
            title,
            body,
            notification_type,
            metadata,
        )
    await send_preference_email(
        request,
        recipient,
        email_preference_key,
        email_subject or title,
        email_body or body,
    )


async def notify_plugin_review(
    request: Request,
    plugin: dict[str, Any],
    reviewer: dict[str, Any],
    auto_approved: bool = False,
) -> None:
    plugin_name = plugin_display_name(plugin)
    body = (
        f"{plugin_name} 已自动审核通过并上架。"
        if auto_approved
        else f"{plugin_name} 已通过审核并上架。"
    )
    await create_preference_notification(
        request,
        plugin.get("owner_user_id"),
        reviewer.get("id"),
        "notify_plugin_review",
        "插件审核通过",
        body,
        "plugin_listed",
        {
            "plugin_id": plugin.get("id"),
            "plugin_name": plugin.get("name") or plugin.get("id"),
            "moderator_user_id": reviewer.get("id"),
            "auto_approved": auto_approved,
        },
        email_preference_key="email_notify_plugin_review",
        skip_self=False,
    )


async def notify_pending_plugin_review(
    request: Request,
    plugin: dict[str, Any],
    submitter: dict[str, Any],
) -> None:
    admins = await call_store(request, "list_users")
    candidates = [
        user
        for user in admins
        if normalize_role(user.get("role")) in {Role.CORE_ADMIN, Role.ADMIN}
        and notification_preference_enabled(user, "email_notify_pending_review")
        and is_valid_email(notification_email_address(user))
    ]
    if not candidates:
        return
    recipient = secrets.choice(candidates)
    await send_preference_email(
        request,
        recipient,
        "email_notify_pending_review",
        "有新的插件待审查",
        f"{user_display_name(submitter)} 提交了 {plugin_display_name(plugin)}"
        f"（{plugin.get('name') or plugin.get('id')}），请进入插件审核处理。",
    )


async def notify_plugin_unlisted(
    request: Request,
    plugin: dict[str, Any],
    moderator: dict[str, Any],
    reason: str,
) -> None:
    plugin_name = plugin_display_name(plugin)
    body = f"{plugin_name} 已被管理员下架。原因：{reason}"
    await create_preference_notification(
        request,
        plugin.get("owner_user_id"),
        moderator.get("id"),
        "notify_unlist",
        "插件已下架",
        body,
        "plugin_unlisted",
        {
            "plugin_id": plugin.get("id"),
            "plugin_name": plugin.get("name") or plugin.get("id"),
            "reason": reason,
            "moderator_user_id": moderator.get("id"),
        },
        email_preference_key="email_notify_unlist",
        skip_self=False,
    )


async def notify_plugin_comment(
    request: Request,
    plugin: dict[str, Any],
    comment: dict[str, Any],
    actor: dict[str, Any],
) -> None:
    await create_preference_notification(
        request,
        plugin.get("owner_user_id"),
        actor["id"],
        "notify_comments",
        "你的插件有新评论",
        f"{user_display_name(actor)} 评论了 {plugin_display_name(plugin)}："
        f"{notification_excerpt(comment.get('body', ''))}",
        "plugin_comment",
        {
            "plugin_id": plugin.get("id"),
            "plugin_name": plugin.get("name") or plugin.get("id"),
            "comment_id": comment.get("id"),
            "actor_user_id": actor["id"],
        },
        email_preference_key="email_notify_comments",
    )


async def notify_comment_reply(
    request: Request,
    plugin: dict[str, Any],
    parent_comment: dict[str, Any] | None,
    comment: dict[str, Any],
    actor: dict[str, Any],
) -> None:
    if not parent_comment:
        return
    await create_preference_notification(
        request,
        parent_comment.get("user_id"),
        actor["id"],
        "notify_replies",
        "你的评论有新回复",
        f"{user_display_name(actor)} 回复了你在 {plugin_display_name(plugin)} 的评论："
        f"{notification_excerpt(comment.get('body', ''))}",
        "comment_reply",
        {
            "plugin_id": plugin.get("id"),
            "plugin_name": plugin.get("name") or plugin.get("id"),
            "comment_id": comment.get("id"),
            "parent_id": parent_comment.get("id"),
            "actor_user_id": actor["id"],
        },
        email_preference_key="email_notify_replies",
    )


async def notify_plugin_like(
    request: Request,
    plugin: dict[str, Any],
    actor: dict[str, Any],
) -> None:
    await create_preference_notification(
        request,
        plugin.get("owner_user_id"),
        actor["id"],
        "notify_likes",
        "你的插件收到了点赞",
        f"{user_display_name(actor)} 点赞了 {plugin_display_name(plugin)}。",
        "plugin_like",
        {
            "plugin_id": plugin.get("id"),
            "plugin_name": plugin.get("name") or plugin.get("id"),
            "actor_user_id": actor["id"],
        },
        email_preference_key="email_notify_likes",
    )


async def notify_comment_like(
    request: Request,
    plugin: dict[str, Any] | None,
    comment: dict[str, Any],
    actor: dict[str, Any],
) -> None:
    await create_preference_notification(
        request,
        comment.get("user_id"),
        actor["id"],
        "notify_likes",
        "你的评论收到了点赞",
        f"{user_display_name(actor)} 点赞了你在 {plugin_display_name(plugin)} 的评论。",
        "comment_like",
        {
            "plugin_id": comment.get("plugin_id"),
            "plugin_name": (plugin or {}).get("name") or comment.get("plugin_id"),
            "comment_id": comment.get("id"),
            "actor_user_id": actor["id"],
        },
        email_preference_key="email_notify_likes",
    )


async def plugin_with_interaction_state(
    request: Request,
    plugin: dict[str, Any] | None,
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    if not plugin:
        raise error(404, "Plugin not found")
    comments = await call_store(request, "list_comments", plugin["id"])
    liked_comment_ids = set()
    plugin_liked = False
    if user:
        plugin_liked = await call_store(request, "has_plugin_like", plugin["id"], user["id"])
        liked_comment_ids = set(
            await call_store(request, "list_liked_comment_ids", plugin["id"], user["id"])
        )
    return {
        **plugin,
        "liked": plugin_liked,
        "comments": [
            with_comment_permissions(
                comment,
                user,
                comment["id"] in liked_comment_ids,
                plugin,
                index + 1,
            )
            for index, comment in enumerate(comments)
        ],
    }


async def delete_comment_by_user(
    request: Request,
    comment_id: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    comment = await call_store(request, "get_comment", comment_id)
    if not comment:
        raise error(404, "Comment not found")
    if comment.get("user_id") != user["id"] and not can_moderate_community(user):
        raise error(403, "Forbidden")
    deleted = await call_store(request, "delete_comment", comment_id, user["id"])
    if not deleted:
        raise error(404, "Comment not found")
    return deleted


def with_comment_permissions(
    comment: dict[str, Any],
    user: dict[str, Any] | None,
    liked: bool,
    plugin: dict[str, Any] | None = None,
    floor: int | None = None,
) -> dict[str, Any]:
    return {
        **comment,
        "liked": liked,
        "floor": floor,
        "is_admin": normalize_role(comment.get("role")) in {Role.CORE_ADMIN, Role.ADMIN},
        "is_plugin_author": is_plugin_author_comment(comment, plugin),
        "can_delete": bool(user)
        and (comment.get("user_id") == user.get("id") or can_moderate_community(user)),
    }


def is_plugin_author_comment(
    comment: dict[str, Any],
    plugin: dict[str, Any] | None,
) -> bool:
    if not plugin:
        return False
    if plugin.get("owner_user_id") and comment.get("user_id") == plugin.get("owner_user_id"):
        return True
    return bool(
        plugin.get("owner_github_login")
        and comment.get("github_login")
        and plugin["owner_github_login"] == comment["github_login"]
    )


def validate_plugin_submission(payload: dict[str, Any], settings: Settings | None = None) -> None:
    for field in ("name", "repo", "desc", "author"):
        if not payload.get(field):
            raise error(400, "Missing required plugin fields")
    validate_plugin_name(payload["name"])
    validate_github_repo(payload["repo"])
    payload["category"] = validate_plugin_category(payload.get("category", ""))
    validate_plugin_tag_count(payload.get("tags") or [], settings)


def normalize_plugin_category(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def validate_plugin_category(value: Any) -> str:
    category = normalize_plugin_category(value)
    if category and category not in OFFICIAL_PLUGIN_CATEGORIES:
        raise error(400, "Plugin category is invalid")
    return category


def validate_plugin_tag_count(tags: list[str], settings: Settings | None = None) -> None:
    if settings and settings.max_plugin_tags and len(tags) > settings.max_plugin_tags:
        raise error(400, f"Plugin can have at most {settings.max_plugin_tags} tags")


def validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME_PATTERN.match(name or ""):
        raise error(400, "Plugin name must use astrbot_plugin_ prefix")


def validate_github_repo(repo: str) -> re.Match[str]:
    match = GITHUB_REPO_PATTERN.match(repo or "")
    if not match:
        raise error(400, "Plugin repo must be a GitHub URL")
    return match


async def enforce_user_rpm_limit(
    request: Request,
    scope: str,
    user: dict[str, Any],
    rpm: int,
) -> None:
    if rpm <= 0:
        return
    now = int(datetime.now(UTC).timestamp())
    window = now // RATE_LIMIT_WINDOW_SECONDS
    retry_after = RATE_LIMIT_WINDOW_SECONDS - (now % RATE_LIMIT_WINDOW_SECONDS)
    subject = str(user.get("id") or user.get("github_login") or "unknown")
    key = f"rate_limit:{scope}:{subject}:{window}"
    count = await redis_rate_limit_count(request, key, RATE_LIMIT_WINDOW_SECONDS + 5)
    if count is None:
        count = memory_rate_limit_count(request, key, now + retry_after, now)
    if count > rpm:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


async def redis_rate_limit_count(request: Request, key: str, ttl_seconds: int) -> int | None:
    redis_client = getattr(getattr(request.app.state, "store", None), "redis", None)
    if not redis_client:
        return None
    try:
        count = int(await redis_client.incr(key))
        if count == 1:
            await redis_client.expire(key, ttl_seconds)
        return count
    except Exception:
        LOGGER.warning("Redis rate limit counter failed; falling back to memory", exc_info=True)
        return None


def memory_rate_limit_count(
    request: Request,
    key: str,
    expires_at: int,
    now: int,
) -> int:
    counters = getattr(request.app.state, "rate_limit_counters", None)
    if not isinstance(counters, dict):
        counters = {}
        request.app.state.rate_limit_counters = counters
    if len(counters) > 1000:
        for counter_key, entry in list(counters.items()):
            if int(entry.get("expires_at") or 0) <= now:
                counters.pop(counter_key, None)
    entry = counters.get(key)
    if not entry or int(entry.get("expires_at") or 0) <= now:
        entry = {"count": 0, "expires_at": expires_at}
        counters[key] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    return int(entry["count"])


class GithubMetadataError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def github_metadata_sync_worker(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(GITHUB_METADATA_SYNC_WORKER_SLEEP_SECONDS)
        await sync_due_github_plugin_metadata_once(app, GITHUB_METADATA_SYNC_BATCH_SIZE)


async def sync_due_github_plugin_metadata_once(app: FastAPI, limit: int) -> int:
    runtime_config = await load_system_options(app.state.store)
    settings = settings_with_runtime_config(app.state.settings, runtime_config)
    if not settings.github_metadata_sync_enabled:
        return 0
    token_statuses = parse_github_api_token_statuses(
        runtime_config.get("GITHUB_API_TOKEN_STATUS", "")
    )
    plugins = await list_due_github_sync_plugins(app.state.store, limit)
    for plugin in plugins:
        try:
            owner = await get_plugin_owner_for_sync(app.state.store, plugin)
            token = (
                ""
                if owner and owner.get("github_token")
                else next_system_github_api_token(app, settings, token_statuses)
            )
            await refresh_plugin_github_metadata_for_plugin(app, plugin, owner, token=token)
            token_statuses = parse_github_api_token_statuses(
                (await load_system_options(app.state.store)).get("GITHUB_API_TOKEN_STATUS", "")
            )
        except Exception as exc:
            await update_plugin_github_sync_failure(
                app.state.store,
                plugin,
                settings,
                safe_exception_message(exc),
            )
            token_statuses = parse_github_api_token_statuses(
                (await load_system_options(app.state.store)).get("GITHUB_API_TOKEN_STATUS", "")
            )
    return len(plugins)


async def list_due_github_sync_plugins(store: Any, limit: int) -> list[dict[str, Any]]:
    method = getattr(store, "list_due_github_sync_plugins", None)
    if method:
        return await resolve_optional_awaitable(method(limit))
    plugins = await resolve_optional_awaitable(store.list_public_plugins())
    due = [plugin for plugin in plugins if is_plugin_due_for_github_sync(plugin)]
    return due[:limit]


def is_plugin_due_for_github_sync(plugin: dict[str, Any]) -> bool:
    if plugin.get("status") != "listed":
        return False
    next_sync = parse_iso_datetime(plugin.get("github_next_sync_at"))
    return next_sync is None or next_sync <= datetime.now(UTC)


async def get_plugin_owner_for_sync(
    store: Any,
    plugin: dict[str, Any],
) -> dict[str, Any] | None:
    owner_id = plugin.get("owner_user_id")
    if not owner_id:
        return None
    method = getattr(store, "get_user_by_id", None)
    if not method:
        return None
    return await resolve_optional_awaitable(method(owner_id))


def queue_plugin_github_metadata_refresh(
    background_tasks: BackgroundTasks,
    app: FastAPI,
    plugin: dict[str, Any],
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
) -> None:
    background_tasks.add_task(
        refresh_plugin_github_metadata_for_plugin,
        app,
        plugin,
        user,
        token=token,
        raise_errors=False,
    )


async def refresh_plugin_github_metadata(
    request: Request,
    plugin_id: str,
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
    save_token: bool = False,
    refresh_interval_seconds: int | None = None,
    raise_errors: bool = False,
) -> dict[str, Any] | None:
    plugin = await call_store(request, "get_plugin", plugin_id)
    if not plugin:
        return None
    if user:
        user = await update_user_github_sync_preferences(
            request,
            user,
            token,
            save_token,
            refresh_interval_seconds,
        )
    return await refresh_plugin_github_metadata_for_plugin(
        request.app,
        plugin,
        user,
        token=token,
        raise_errors=raise_errors,
    )


async def update_user_github_sync_preferences(
    request: Request,
    user: dict[str, Any],
    token: str,
    save_token: bool,
    refresh_interval_seconds: int | None,
) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    if save_token and token:
        profile["github_token"] = token
    if refresh_interval_seconds is not None and (
        user.get("github_token") or profile.get("github_token")
    ):
        profile["github_refresh_interval_seconds"] = refresh_interval_seconds
    if not profile:
        return user
    return await call_store(request, "update_user_profile", user["id"], profile) or user


async def refresh_plugin_github_metadata_for_plugin(
    app: FastAPI,
    plugin: dict[str, Any],
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
    raise_errors: bool = False,
) -> dict[str, Any] | None:
    settings = await runtime_settings_for_app(app)
    try:
        metadata = await fetch_plugin_github_metadata(
            plugin.get("repo") or "",
            settings,
            user,
            token=token,
            store=app.state.store if token else None,
        )
    except GithubMetadataError as exc:
        await update_plugin_github_sync_failure(app.state.store, plugin, settings, exc.message)
        if raise_errors:
            raise error(exc.status_code, exc.message) from exc
        return plugin
    if not metadata:
        return plugin
    metadata.update(github_sync_success_metadata(settings, user))
    return await resolve_optional_awaitable(
        app.state.store.update_plugin_metadata(plugin["id"], metadata)
    )


async def safe_fetch_plugin_github_metadata(
    repo: str,
    settings: Settings,
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
    store: Any | None = None,
) -> dict[str, Any]:
    try:
        return await fetch_plugin_github_metadata(repo, settings, user, token=token, store=store)
    except GithubMetadataError:
        return {}


async def fetch_plugin_submission_metadata_preview(
    repo: str,
    settings: Settings,
    user: dict[str, Any],
    store: Any | None = None,
) -> dict[str, Any]:
    match = validate_github_repo(repo)
    owner = match.group("owner")
    repo_name = match.group("repo")
    preview = build_plugin_submission_metadata_preview(owner, repo_name, settings=settings)
    if not settings.github_metadata_sync_enabled:
        return preview

    runtime_config = await load_system_options(store) if store else {}
    token_statuses = parse_github_api_token_statuses(
        runtime_config.get("GITHUB_API_TOKEN_STATUS", "")
    )
    headers = github_api_headers(user, settings, token_statuses=token_statuses)
    uses_system_token = bool(headers.get("authorization")) and not user.get("github_token")
    status_store = store if uses_system_token else None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            repository = await fetch_github_repository(
                client,
                owner,
                repo_name,
                headers,
                store=status_store,
            )
            metadata = (
                await fetch_github_plugin_metadata_files(
                    client,
                    owner,
                    repo_name,
                    headers,
                    store=status_store,
                )
                if repository
                else {}
            )
    except httpx.HTTPError as exc:
        raise GithubMetadataError("GitHub metadata fetch failed", 502) from exc
    return build_plugin_submission_metadata_preview(
        owner,
        repo_name,
        repository=repository,
        metadata=metadata,
        settings=settings,
    )


async def fetch_plugin_github_metadata(
    repo: str,
    settings: Settings,
    user: dict[str, Any] | None = None,
    *,
    token: str = "",
    store: Any | None = None,
) -> dict[str, Any]:
    if not settings.github_metadata_sync_enabled:
        return {}
    match = validate_github_repo(repo)
    owner = match.group("owner")
    repo_name = match.group("repo")
    runtime_config = await load_system_options(store) if store else {}
    token_statuses = parse_github_api_token_statuses(
        runtime_config.get("GITHUB_API_TOKEN_STATUS", "")
    )
    headers = github_api_headers(user, settings, token, token_statuses)
    uses_system_token = bool(headers.get("authorization")) and not (user or {}).get("github_token")
    status_store = store if uses_system_token else None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            repository = await fetch_github_repository(
                client,
                owner,
                repo_name,
                headers,
                store=status_store,
            )
            if not repository:
                return {}
            metadata = await fetch_github_plugin_metadata_files(
                client,
                owner,
                repo_name,
                headers,
                store=status_store,
            )
            logo = await fetch_github_plugin_logo_url(
                client,
                owner,
                repo_name,
                repository.get("default_branch") or "main",
                headers,
                store=status_store,
            )
    except httpx.HTTPError as exc:
        raise GithubMetadataError("GitHub metadata fetch failed", 502) from exc
    payload: dict[str, Any] = {
        "stars": int(repository.get("stargazers_count") or 0),
        "updated_at": repository.get("updated_at") or "",
    }
    for field in PLUGIN_METADATA_SYNC_FIELDS:
        value = normalize_plugin_metadata_field(field, metadata.get(field))
        if has_metadata_value(value):
            payload[field] = value
    if logo:
        payload["logo"] = logo
    return payload


async def fetch_github_repository(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    headers: dict[str, str],
    store: Any | None = None,
) -> dict[str, Any]:
    response = await github_get(
        client,
        f"https://api.github.com/repos/{owner}/{repo}",
        headers,
        store=store,
    )
    raise_for_github_rate_limit(response)
    if response.status_code != 200:
        return {}
    data = response.json()
    return data if isinstance(data, dict) else {}


async def github_get(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    store: Any | None = None,
) -> httpx.Response:
    response = await client.get(url, headers=headers)
    token = github_token_from_headers(headers)
    if store and token:
        await record_github_api_token_response(store, token, response)
    if github_response_allows_public_fallback(response) and token:
        return await client.get(url, headers=github_public_headers())
    return response


def github_token_from_headers(headers: dict[str, str]) -> str:
    authorization = str(headers.get("authorization") or "")
    if not authorization.lower().startswith("bearer "):
        return ""
    return authorization.split(" ", 1)[1].strip()


def github_response_allows_public_fallback(response: Any) -> bool:
    return response.status_code in {401, 403}


def github_api_token_status_from_response(response: Any) -> dict[str, Any] | None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code not in {401, 403, 429}:
        return None
    rate_limited = is_github_rate_limit_response(response)
    if status_code == 429 or rate_limited:
        retry_after_seconds, reset_at = github_retry_after(response)
        return {
            "disabled": False,
            "status": "rate_limited",
            "error_code": status_code,
            "error_message": github_response_message(response) or GITHUB_RATE_LIMIT_MESSAGE,
            "retry_after_seconds": retry_after_seconds,
            "reset_at": reset_at,
            "checked_at": isoformat_utc(datetime.now(UTC)),
        }
    if status_code in {401, 403}:
        return {
            "disabled": True,
            "status": "disabled",
            "error_code": status_code,
            "error_message": github_response_message(response) or "GitHub API token rejected",
            "checked_at": isoformat_utc(datetime.now(UTC)),
        }
    return None


def github_retry_after(response: Any) -> tuple[int, str]:
    headers = getattr(response, "headers", {}) or {}
    retry_after = parse_positive_int(headers.get("retry-after"))
    if retry_after:
        return retry_after, ""
    reset_epoch = parse_positive_int(headers.get("x-ratelimit-reset"))
    if not reset_epoch:
        return 0, ""
    reset_at = datetime.fromtimestamp(reset_epoch, UTC)
    seconds = max(0, int((reset_at - datetime.now(UTC)).total_seconds()))
    return seconds, isoformat_utc(reset_at)


def parse_positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


async def record_github_api_token_response(store: Any, token: str, response: Any) -> None:
    status = github_api_token_status_from_response(response)
    if not status:
        return
    runtime_config = await load_system_options(store)
    token_pool = runtime_config.get("GITHUB_API_TOKEN", "")
    if token not in parse_github_api_tokens(token_pool):
        return
    statuses = clean_github_api_token_statuses(
        token_pool,
        runtime_config.get("GITHUB_API_TOKEN_STATUS", ""),
    )
    statuses[github_api_token_hash(token)] = status
    await save_system_options_to_store(
        store,
        {"GITHUB_API_TOKEN_STATUS": serialize_github_api_token_statuses(statuses)},
    )


async def fetch_github_plugin_metadata_files(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    headers: dict[str, str],
    store: Any | None = None,
) -> dict[str, Any]:
    for filename in ("metadata.yaml", "metadata.yml"):
        response = await github_get(
            client,
            f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}",
            headers,
            store=store,
        )
        raise_for_github_rate_limit(response)
        if response.status_code != 200:
            continue
        data = response.json()
        content = data.get("content") if isinstance(data, dict) else ""
        if not isinstance(content, str) or not content:
            continue
        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            continue
        metadata = parse_plugin_metadata_yaml(text)
        if metadata:
            return metadata
    return {}


async def fetch_github_plugin_logo_url(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    default_branch: str,
    headers: dict[str, str],
    store: Any | None = None,
) -> str:
    response = await github_get(
        client,
        f"https://api.github.com/repos/{owner}/{repo}/contents/logo.png",
        headers,
        store=store,
    )
    raise_for_github_rate_limit(response)
    if response.status_code == 200:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/logo.png"
    return ""


def build_plugin_submission_metadata_preview(
    owner: str,
    repo_name: str,
    *,
    repository: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    repository = repository or {}
    metadata = metadata or {}
    repo_owner = repository_owner_login(repository) or owner
    repository_name = metadata_text(repository.get("name")) or repo_name
    payload: dict[str, Any] = {
        "repo": f"https://github.com/{owner}/{repo_name}",
        "display_name": repository_name,
        "author": repo_owner,
        "social_link": f"https://github.com/{repo_owner}",
    }
    if PLUGIN_NAME_PATTERN.match(repository_name):
        payload["name"] = repository_name

    description = metadata_text(repository.get("description"))
    if description:
        payload["desc"] = truncate_plugin_description(description)

    homepage = metadata_text(repository.get("homepage"))
    if homepage:
        payload["social_link"] = homepage

    topics = normalize_plugin_metadata_field("tags", repository.get("topics"))
    if has_metadata_value(topics):
        payload["tags"] = limit_plugin_tags(topics, settings)

    for field in ("name", "display_name", "author", "social_link", "category"):
        value = normalize_plugin_metadata_field(field, metadata.get(field))
        if has_metadata_value(value):
            payload[field] = value

    desc = normalize_plugin_metadata_field("desc", metadata.get("desc"))
    if not has_metadata_value(desc):
        desc = normalize_plugin_metadata_field("short_desc", metadata.get("short_desc"))
    if has_metadata_value(desc):
        payload["desc"] = truncate_plugin_description(str(desc))

    tags = normalize_plugin_metadata_field("tags", metadata.get("tags"))
    if has_metadata_value(tags):
        payload["tags"] = limit_plugin_tags(tags, settings)

    return drop_empty_submission_preview_fields(payload)


def repository_owner_login(repository: dict[str, Any]) -> str:
    owner = repository.get("owner")
    if isinstance(owner, dict):
        return metadata_text(owner.get("login"))
    return ""


def truncate_plugin_description(value: str, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def limit_plugin_tags(value: Any, settings: Settings | None = None) -> list[str]:
    max_tags = settings.max_plugin_tags if settings else 8
    if max_tags <= 0:
        return []
    if isinstance(value, list):
        tags = [str(tag).strip() for tag in value if str(tag).strip()]
    else:
        tags = []
    return list(dict.fromkeys(tags))[:max_tags]


def drop_empty_submission_preview_fields(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if has_metadata_value(value):
            result[key] = value
    return result


def raise_for_github_rate_limit(response: Any) -> None:
    if is_github_rate_limit_response(response):
        raise GithubMetadataError(GITHUB_RATE_LIMIT_MESSAGE, 429)


def is_github_rate_limit_response(response: Any) -> bool:
    if response.status_code not in {403, 429}:
        return False
    headers = getattr(response, "headers", {}) or {}
    message = github_response_message(response).lower()
    return headers.get("x-ratelimit-remaining") == "0" or "rate limit" in message


def github_response_message(response: Any) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("message") or "")
    return ""


def normalize_plugin_metadata_field(field: str, value: Any) -> Any:
    if field == "name" and value and not PLUGIN_NAME_PATTERN.match(str(value)):
        return ""
    if field == "category":
        category = normalize_plugin_category(value)
        return category if category in OFFICIAL_PLUGIN_CATEGORIES else ""
    if field in {"tags", "support_platforms"}:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []
    return value


def metadata_text(value: Any) -> str:
    return str(value or "").strip()


def github_sync_success_metadata(
    settings: Settings,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    interval = github_sync_interval_seconds(settings, user)
    return {
        "github_synced_at": isoformat_utc(now),
        "github_next_sync_at": isoformat_utc(now + timedelta(seconds=interval)),
        "github_refresh_interval_seconds": interval,
        "github_sync_status": "success",
        "github_sync_error": "",
    }


async def update_plugin_github_sync_failure(
    store: Any,
    plugin: dict[str, Any],
    settings: Settings,
    message: str,
) -> None:
    now = datetime.now(UTC)
    interval = github_sync_interval_seconds(settings)
    await resolve_optional_awaitable(
        store.update_plugin_metadata(
            plugin["id"],
            {
                "github_synced_at": isoformat_utc(now),
                "github_next_sync_at": isoformat_utc(now + timedelta(seconds=interval)),
                "github_sync_status": "error",
                "github_sync_error": message,
            },
        )
    )


def github_sync_interval_seconds(
    settings: Settings,
    user: dict[str, Any] | None = None,
) -> int:
    if user and user.get("github_token"):
        return clamp_sync_interval(user.get("github_refresh_interval_seconds"))
    return clamp_sync_interval(settings.github_metadata_sync_interval_seconds)


def clamp_sync_interval(value: Any) -> int:
    try:
        seconds = int(value or 3600)
    except (TypeError, ValueError):
        seconds = 3600
    return min(max(seconds, 300), 86400)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_plugin_metadata_yaml(text: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = METADATA_FIELD_PATTERN.match(line)
        if not match:
            index += 1
            continue
        indent, key, raw_value = match.groups()
        value = parse_metadata_scalar(raw_value)
        if value == "" and key in {"support_platforms", "tags"}:
            value, index = parse_metadata_list(lines, index, len(indent))
        if has_metadata_value(value):
            metadata[key] = value
        index += 1
    return metadata


def parse_metadata_list(
    lines: list[str],
    start_index: int,
    parent_indent: int,
) -> tuple[list[str] | str, int]:
    items: list[str] = []
    index = start_index + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            return items or "", index - 1
        stripped = line.strip()
        if not stripped.startswith("- "):
            return items or "", index - 1
        item = parse_metadata_scalar(stripped[2:])
        if item:
            items.append(str(item))
        index += 1
    return items or "", index - 1


def parse_metadata_scalar(value: str) -> Any:
    value = strip_yaml_comment(value).strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        items = [parse_metadata_scalar(item) for item in value[1:-1].split(",")]
        return [item for item in items if item]
    return value


def strip_yaml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return value[:index].rstrip()
    return value.strip()


def has_metadata_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return value is not None


def validate_setup_payload(payload: SetupConfig) -> None:
    if not payload.admin.username:
        raise error(400, "Core admin username is required")
    if len(payload.admin.password) < 8:
        raise error(400, "Core admin password must be at least 8 characters")
    if not payload.postgres.host:
        raise error(400, "PostgreSQL host is required")
    if not payload.postgres.database:
        raise error(400, "PostgreSQL database is required")
    if not payload.postgres.username:
        raise error(400, "PostgreSQL username is required")
    if not payload.postgres.password:
        raise error(400, "PostgreSQL password is required")
    if not payload.redis.host:
        raise error(400, "Redis host is required")
    if not payload.site.name:
        raise error(400, "Site name is required")
    if not payload.site.icon_url:
        raise error(400, "Site icon URL is required")
    if not is_valid_site_icon_url(payload.site.icon_url):
        raise error(400, "Site icon URL must be an absolute URL or root-relative path")


async def initialize_setup_infrastructure(
    payload: SetupConfig,
    database_url: str,
    redis_url: str,
    core_admin_password_hash: str,
) -> PgRedisMarketStore:
    await ensure_postgres_database(payload.postgres.model_dump())
    store = PgRedisMarketStore(database_url, redis_url, session_ttl_seconds=60)
    try:
        await store.connect()
        await store.create_internal_admin(payload.admin.username, core_admin_password_hash)
        return store
    except HTTPException:
        await store.close()
        raise
    except asyncpg.PostgresError as exc:
        await store.close()
        raise error(
            400,
            f"PostgreSQL schema initialization failed: {safe_exception_message(exc)}",
        ) from exc
    except OSError as exc:
        await store.close()
        raise error(
            400, f"Infrastructure connection failed: {safe_exception_message(exc)}"
        ) from exc
    except Exception as exc:
        await store.close()
        raise error(
            400,
            f"PostgreSQL or Redis initialization failed: {safe_exception_message(exc)}",
        ) from exc


async def activate_setup_store(app: FastAPI, new_store: Any) -> None:
    if not new_store:
        await bootstrap_internal_core_admin(app)
        return

    old_store = app.state.store
    app.state.store = new_store
    if old_store is new_store:
        return
    close = getattr(old_store, "close", None)
    if close:
        await resolve_optional_awaitable(close())


async def close_setup_store(store: Any) -> None:
    close = getattr(store, "close", None)
    if close:
        await resolve_optional_awaitable(close())


async def ensure_postgres_database(config: dict[str, Any]) -> None:
    target_database = config["database"]
    try:
        connection = await connect_postgres_database(config, target_database)
    except asyncpg.InvalidCatalogNameError:
        await create_postgres_database(config, target_database)
        return
    except asyncpg.PostgresError as exc:
        raise error(
            400,
            f"PostgreSQL connection failed: {safe_exception_message(exc)}",
        ) from exc
    except OSError as exc:
        raise error(
            400,
            f"PostgreSQL connection failed: {safe_exception_message(exc)}",
        ) from exc
    else:
        await connection.close()


async def create_postgres_database(config: dict[str, Any], target_database: str) -> None:
    try:
        connection = await connect_postgres_database(config, POSTGRES_MAINTENANCE_DATABASE)
    except asyncpg.InvalidCatalogNameError as exc:
        raise error(
            400,
            "PostgreSQL maintenance database 'postgres' is unavailable; "
            "create the target database manually first.",
        ) from exc
    except asyncpg.PostgresError as exc:
        raise error(
            400,
            f"PostgreSQL database creation failed: {safe_exception_message(exc)}",
        ) from exc
    except OSError as exc:
        raise error(
            400,
            f"PostgreSQL database creation failed: {safe_exception_message(exc)}",
        ) from exc

    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            target_database,
        )
        if not exists:
            await connection.execute(
                f"CREATE DATABASE {quote_postgres_identifier(target_database)}"
            )
    except asyncpg.DuplicateDatabaseError:
        return
    except asyncpg.PostgresError as exc:
        raise error(
            400,
            f"PostgreSQL database creation failed: {safe_exception_message(exc)}",
        ) from exc
    finally:
        await connection.close()


async def connect_postgres_database(
    config: dict[str, Any],
    database: str,
) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=config["host"],
        port=config["port"],
        user=config["username"],
        password=config["password"],
        database=database,
        ssl=config["ssl"] or None,
    )


def quote_postgres_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def safe_exception_message(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def validate_system_settings_payload(
    payload: SystemSettingsPayload,
    runtime_config: dict[str, str],
    settings: Settings | None = None,
) -> None:
    if not payload.site.name:
        raise error(400, "Site name is required")
    if not payload.site.icon_url or not is_valid_site_icon_url(payload.site.icon_url):
        raise error(400, "Site icon URL must be an absolute URL or root-relative path")
    if not payload.site.web_url or not is_valid_public_url(payload.site.web_url):
        raise error(400, "Web URL must be http(s)")
    if payload.site.docs_url and not is_valid_public_url(payload.site.docs_url):
        raise error(400, "Documentation URL must be http(s)")
    if payload.site.contact_email and not is_valid_email(payload.site.contact_email):
        raise error(400, "Contact email is invalid")
    if payload.auth.login_agreement_enabled and not payload.auth.login_agreement_text:
        raise error(400, "Login agreement text is required when enabled")
    if payload.auth.service_terms_enabled and not payload.auth.service_terms_text:
        raise error(400, "Service terms text is required when enabled")
    if payload.auth.github_login_enabled:
        if not payload.github.client_id:
            raise error(400, "GitHub OAuth client ID is required when GitHub login is enabled")
        if not payload.github.callback_url or not is_valid_public_url(payload.github.callback_url):
            raise error(400, "GitHub callback URL must be http(s)")
        if is_local_url(payload.github.callback_url):
            raise error(400, "GitHub callback URL must use a public host")
        if is_local_url(payload.site.web_url):
            raise error(400, "Web URL must use a public host when GitHub login is enabled")
        if not settings or not settings.github_client_secret:
            raise error(
                400,
                "GitHub OAuth client secret must be configured in the deployment environment",
            )
    if payload.email.provider == "smtp":
        if not payload.email.smtp.host:
            raise error(400, "SMTP host is required when SMTP email is enabled")
        if not payload.email.smtp.from_address or not is_valid_email(
            payload.email.smtp.from_address
        ):
            raise error(400, "SMTP from address is invalid")
        smtp_auth_method = normalize_smtp_auth_method(payload.email.smtp.auth_method)
        existing_smtp_password = runtime_config.get("SMTP_PASSWORD") or (
            settings.smtp_password if settings else ""
        )
        if smtp_auth_method in {"login", "plain"} and not payload.email.smtp.username:
            raise error(400, "SMTP username is required for explicit authentication")
        if (
            smtp_auth_method != "none"
            and payload.email.smtp.username
            and not has_secret_value(payload.email.smtp.password, existing_smtp_password)
        ):
            raise error(400, "SMTP password is required when SMTP authentication is enabled")
    if payload.email.provider == "cloudflare":
        if not payload.email.cloudflare.account_id:
            raise error(400, "Cloudflare account ID is required")
        existing_cloudflare_token = runtime_config.get("CLOUDFLARE_EMAIL_API_TOKEN") or (
            settings.cloudflare_email_api_token if settings else ""
        )
        if not has_secret_value(payload.email.cloudflare.api_token, existing_cloudflare_token):
            raise error(400, "Cloudflare API token is required")
        if not payload.email.cloudflare.from_address or not is_valid_email(
            payload.email.cloudflare.from_address
        ):
            raise error(400, "Cloudflare from address is invalid")


def get_site_config(
    settings: Settings,
    runtime_config: dict[str, str] | None = None,
) -> dict[str, str]:
    if runtime_config is not None:
        return build_site_settings(settings, runtime_config)
    return {
        "name": settings.site_name,
        "icon_url": settings.site_icon_url,
        "web_url": settings.web_url,
        "subtitle": settings.site_subtitle,
        "description": settings.site_description,
        "contact_email": settings.site_contact_email,
        "docs_url": settings.site_docs_url,
    }


def get_public_auth_config(
    settings: Settings,
    runtime_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    auth = (
        build_auth_settings(settings, runtime_config)
        if runtime_config is not None
        else {
            "github_login_enabled": settings.github_login_enabled,
            "public_login_enabled": settings.public_login_enabled,
            "login_agreement_enabled": settings.login_agreement_enabled,
            "login_agreement_text": settings.login_agreement_text,
            "service_terms_enabled": settings.service_terms_enabled,
            "service_terms_text": settings.service_terms_text,
        }
    )
    effective_settings = settings.with_updates(
        login_agreement_enabled=auth["login_agreement_enabled"],
        login_agreement_text=auth["login_agreement_text"],
        service_terms_enabled=auth["service_terms_enabled"],
        service_terms_text=auth["service_terms_text"],
    )
    return {**auth, "terms_revision": digest_terms(effective_settings)}


def get_public_market_config(
    settings: Settings,
    runtime_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    if runtime_config is not None:
        market = build_market_settings(settings, runtime_config)
        return {
            "submissions_enabled": market["submissions_enabled"],
            "comments_enabled": market["comments_enabled"],
            "likes_enabled": market["likes_enabled"],
            "max_plugin_tags": market["max_plugin_tags"],
        }
    return {
        "submissions_enabled": settings.market_submissions_enabled,
        "comments_enabled": settings.market_comments_enabled,
        "likes_enabled": settings.market_likes_enabled,
        "max_plugin_tags": settings.max_plugin_tags,
    }


def digest_terms(settings: Settings) -> str:
    payload = json.dumps(
        {
            "login": settings.login_agreement_text if settings.login_agreement_enabled else "",
            "service": settings.service_terms_text if settings.service_terms_enabled else "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def settings_config_values(settings: Settings) -> dict[str, str]:
    values = {
        "CLOUDFLARE_EMAIL_ACCOUNT_ID": settings.cloudflare_email_account_id,
        "CLOUDFLARE_EMAIL_API_TOKEN": settings.cloudflare_email_api_token,
        "CLOUDFLARE_EMAIL_FROM": settings.cloudflare_email_from,
        "CLOUDFLARE_EMAIL_FROM_NAME": settings.cloudflare_email_from_name,
        "CORE_ADMIN_PASSWORD_HASH": settings.core_admin_password_hash,
        "CORE_ADMIN_USERNAME": settings.core_admin_username,
        "DATABASE_URL": settings.database_url,
        "EMAIL_DAILY_LIMIT": str(settings.email_daily_limit),
        "EMAIL_PROVIDER": settings.email_provider,
        "EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER": str(
            settings.email_verification_daily_limit_per_user
        ),
        "GITHUB_ADMIN_ORG": settings.github_admin_org,
        "GITHUB_API_TOKEN": settings.github_api_token,
        "GITHUB_CALLBACK_URL": settings.github_callback_url,
        "GITHUB_CLIENT_ID": settings.github_client_id,
        "GITHUB_CLIENT_SECRET": settings.github_client_secret,
        "GITHUB_LOGIN_ENABLED": serialize_bool(settings.github_login_enabled),
        "GITHUB_METADATA_SYNC_ENABLED": serialize_bool(settings.github_metadata_sync_enabled),
        "GITHUB_METADATA_SYNC_INTERVAL_SECONDS": str(
            settings.github_metadata_sync_interval_seconds
        ),
        "GITHUB_SCOPE": settings.github_scope,
        "LOGIN_AGREEMENT_ENABLED": serialize_bool(settings.login_agreement_enabled),
        "LOGIN_AGREEMENT_TEXT": settings.login_agreement_text,
        "MARKET_COMMENTS_ENABLED": serialize_bool(settings.market_comments_enabled),
        "MARKET_LIKES_ENABLED": serialize_bool(settings.market_likes_enabled),
        "MARKET_SUBMISSIONS_ENABLED": serialize_bool(settings.market_submissions_enabled),
        "MAX_PLUGIN_TAGS": str(settings.max_plugin_tags),
        "PLUGIN_AUTO_APPROVE_ENABLED": serialize_bool(settings.plugin_auto_approve_enabled),
        "PUBLIC_LOGIN_ENABLED": serialize_bool(settings.public_login_enabled),
        "REDIS_URL": settings.redis_url,
        "SERVICE_TERMS_ENABLED": serialize_bool(settings.service_terms_enabled),
        "SERVICE_TERMS_TEXT": settings.service_terms_text,
        "SITE_CONTACT_EMAIL": settings.site_contact_email,
        "SITE_DESCRIPTION": settings.site_description,
        "SITE_DOCS_URL": settings.site_docs_url,
        "SITE_ICON_URL": settings.site_icon_url,
        "SITE_NAME": settings.site_name,
        "SITE_SUBTITLE": settings.site_subtitle,
        "SMTP_AUTH_METHOD": settings.smtp_auth_method,
        "SMTP_ENCRYPTION": settings.smtp_encryption,
        "SMTP_FROM": settings.smtp_from,
        "SMTP_FROM_NAME": settings.smtp_from_name,
        "SMTP_HOST": settings.smtp_host,
        "SMTP_PASSWORD": settings.smtp_password,
        "SMTP_PORT": str(settings.smtp_port),
        "SMTP_SSL": serialize_bool(settings.smtp_encryption == "ssl_tls"),
        "SMTP_USERNAME": settings.smtp_username,
        "SMTP_VALIDATE_CERTS": serialize_bool(settings.smtp_validate_certs),
        "WEB_URL": settings.web_url,
    }
    if settings.database_url:
        postgres = parse_postgres_url(settings.database_url)
        values.update(
            {
                "POSTGRES_DATABASE": str(postgres["database"]),
                "POSTGRES_HOST": str(postgres["host"]),
                "POSTGRES_PASSWORD": str(postgres["password"]),
                "POSTGRES_PORT": str(postgres["port"]),
                "POSTGRES_SSL": serialize_bool(bool(postgres["ssl"])),
                "POSTGRES_USER": str(postgres["username"]),
            }
        )
    if settings.redis_url:
        redis = parse_redis_url(settings.redis_url)
        values.update(
            {
                "REDIS_DATABASE": str(redis["database"]),
                "REDIS_HOST": str(redis["host"]),
                "REDIS_PASSWORD": str(redis["password"]),
                "REDIS_PORT": str(redis["port"]),
                "REDIS_SSL": serialize_bool(bool(redis["ssl"])),
            }
        )
    return {key: value for key, value in values.items() if value != ""}


def setup_env_values(
    payload: SetupConfig,
    database_url: str,
    redis_url: str,
    core_admin_password_hash: str,
) -> dict[str, str]:
    return {
        "CORE_ADMIN_PASSWORD_HASH": core_admin_password_hash,
        "CORE_ADMIN_USERNAME": payload.admin.username,
        "DATABASE_URL": database_url,
        "POSTGRES_DATABASE": payload.postgres.database,
        "POSTGRES_HOST": payload.postgres.host,
        "POSTGRES_PASSWORD": payload.postgres.password,
        "POSTGRES_PORT": str(payload.postgres.port),
        "POSTGRES_SSL": serialize_bool(payload.postgres.ssl),
        "POSTGRES_USER": payload.postgres.username,
        "REDIS_DATABASE": str(payload.redis.database),
        "REDIS_HOST": payload.redis.host,
        "REDIS_PASSWORD": payload.redis.password,
        "REDIS_PORT": str(payload.redis.port),
        "REDIS_SSL": serialize_bool(payload.redis.ssl),
        "REDIS_URL": redis_url,
    }


def build_saved_setup_config(
    settings: Settings,
    runtime_config: dict[str, str],
) -> dict[str, Any]:
    database_url = runtime_config.get("DATABASE_URL") or settings.database_url
    redis_url = runtime_config.get("REDIS_URL") or settings.redis_url
    system_settings = build_system_settings(settings, runtime_config, include_secrets=False)
    return {
        "postgres": build_saved_postgres_config(runtime_config, database_url),
        "redis": build_saved_redis_config(runtime_config, redis_url),
        **system_settings,
    }


def build_setup_status_response(
    settings: Settings,
    runtime_config: dict[str, str],
    *,
    include_saved_setup: bool,
    redact_saved_setup: bool,
) -> dict[str, Any]:
    runtime_database_url = runtime_config.get("DATABASE_URL", "")
    runtime_redis_url = runtime_config.get("REDIS_URL", "")
    status = {
        "required": settings.is_setup_required(),
        "missing": list(settings.missing_setup_fields()),
        "database_configured": bool(settings.database_url or runtime_database_url),
        "redis_configured": bool(settings.redis_url or runtime_redis_url),
        "restart_required": settings_restart_required(settings, runtime_config),
    }
    if include_saved_setup:
        saved_setup = build_saved_setup_config(settings, runtime_config)
        if redact_saved_setup:
            saved_setup = redact_setup_infrastructure(saved_setup)
        status["site"] = saved_setup["site"]
        status["saved_setup"] = saved_setup
    return status


def redact_setup_infrastructure(config: dict[str, Any]) -> dict[str, Any]:
    redacted = {
        "postgres": {**DEFAULT_POSTGRES_CONFIG},
        "redis": {**DEFAULT_REDIS_CONFIG},
        "site": {**config["site"]},
        "auth": {**config["auth"]},
        "github": redact_github_settings(config.get("github", {})),
        "market": redact_market_settings(config.get("market", {})),
        "email": redact_email_settings(config.get("email", {})),
    }
    return redacted


def build_system_settings(
    settings: Settings,
    runtime_config: dict[str, str],
    *,
    include_secrets: bool,
) -> dict[str, Any]:
    config = {
        "site": build_site_settings(settings, runtime_config),
        "auth": build_auth_settings(settings, runtime_config),
        "github": build_github_settings(settings, runtime_config),
        "market": build_market_settings(settings, runtime_config),
        "email": build_email_settings(settings, runtime_config),
    }
    if include_secrets:
        return config
    config["github"] = redact_github_settings(config["github"])
    config["market"] = redact_market_settings(config["market"])
    config["email"] = redact_email_settings(config["email"])
    return config


def build_site_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, str]:
    return {
        "name": runtime_config.get("SITE_NAME", settings.site_name),
        "icon_url": runtime_config.get("SITE_ICON_URL", settings.site_icon_url),
        "web_url": runtime_config.get("WEB_URL", settings.web_url),
        "subtitle": runtime_config.get("SITE_SUBTITLE", settings.site_subtitle),
        "description": runtime_config.get("SITE_DESCRIPTION", settings.site_description),
        "contact_email": runtime_config.get("SITE_CONTACT_EMAIL", settings.site_contact_email),
        "docs_url": runtime_config.get("SITE_DOCS_URL", settings.site_docs_url),
    }


def build_auth_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "github_login_enabled": parse_bool(
            runtime_config.get("GITHUB_LOGIN_ENABLED"), settings.github_login_enabled
        ),
        "public_login_enabled": parse_bool(
            runtime_config.get("PUBLIC_LOGIN_ENABLED"), settings.public_login_enabled
        ),
        "login_agreement_enabled": parse_bool(
            runtime_config.get("LOGIN_AGREEMENT_ENABLED"),
            settings.login_agreement_enabled,
        ),
        "login_agreement_text": runtime_config.get(
            "LOGIN_AGREEMENT_TEXT",
            settings.login_agreement_text,
        ),
        "service_terms_enabled": parse_bool(
            runtime_config.get("SERVICE_TERMS_ENABLED"),
            settings.service_terms_enabled,
        ),
        "service_terms_text": runtime_config.get(
            "SERVICE_TERMS_TEXT",
            settings.service_terms_text,
        ),
    }


def build_github_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "client_id": runtime_config.get("GITHUB_CLIENT_ID", settings.github_client_id),
        "client_secret": settings.github_client_secret,
        "callback_url": runtime_config.get("GITHUB_CALLBACK_URL", settings.github_callback_url),
        "scope": runtime_config.get("GITHUB_SCOPE", settings.github_scope),
        "admin_org": runtime_config.get("GITHUB_ADMIN_ORG", settings.github_admin_org),
    }


def build_market_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    return {
        "submissions_enabled": parse_bool(
            runtime_config.get("MARKET_SUBMISSIONS_ENABLED"),
            settings.market_submissions_enabled,
        ),
        "comments_enabled": parse_bool(
            runtime_config.get("MARKET_COMMENTS_ENABLED"),
            settings.market_comments_enabled,
        ),
        "likes_enabled": parse_bool(
            runtime_config.get("MARKET_LIKES_ENABLED"),
            settings.market_likes_enabled,
        ),
        "plugin_auto_approve_enabled": parse_bool(
            runtime_config.get("PLUGIN_AUTO_APPROVE_ENABLED"),
            settings.plugin_auto_approve_enabled,
        ),
        "max_plugin_tags": parse_int(
            runtime_config.get("MAX_PLUGIN_TAGS"),
            settings.max_plugin_tags,
        ),
        "api_token": runtime_config.get("GITHUB_API_TOKEN", settings.github_api_token),
        "api_token_status": runtime_config.get("GITHUB_API_TOKEN_STATUS", ""),
        "metadata_sync_enabled": parse_bool(
            runtime_config.get("GITHUB_METADATA_SYNC_ENABLED"),
            settings.github_metadata_sync_enabled,
        ),
        "metadata_sync_interval_seconds": clamp_sync_interval(
            runtime_config.get(
                "GITHUB_METADATA_SYNC_INTERVAL_SECONDS",
                str(settings.github_metadata_sync_interval_seconds),
            )
        ),
    }


def build_email_settings(settings: Settings, runtime_config: dict[str, str]) -> dict[str, Any]:
    smtp_port = parse_int(runtime_config.get("SMTP_PORT"), settings.smtp_port)
    smtp_encryption = normalize_smtp_encryption(
        runtime_config.get("SMTP_ENCRYPTION", settings.smtp_encryption)
    )
    return {
        "provider": normalize_email_provider(
            runtime_config.get("EMAIL_PROVIDER", settings.email_provider)
        ),
        "smtp": {
            "host": runtime_config.get("SMTP_HOST", settings.smtp_host),
            "port": smtp_port,
            "username": runtime_config.get("SMTP_USERNAME", settings.smtp_username),
            "password": runtime_config.get("SMTP_PASSWORD", settings.smtp_password),
            "from_address": runtime_config.get("SMTP_FROM", settings.smtp_from),
            "from_name": sender_name(
                runtime_config.get("SMTP_FROM_NAME", settings.smtp_from_name),
            ),
            "ssl": smtp_encryption == "ssl_tls",
            "encryption": smtp_encryption,
            "auth_method": normalize_smtp_auth_method(
                runtime_config.get("SMTP_AUTH_METHOD", settings.smtp_auth_method)
            ),
            "validate_certs": parse_bool(
                runtime_config.get("SMTP_VALIDATE_CERTS"),
                settings.smtp_validate_certs,
            ),
        },
        "cloudflare": {
            "account_id": runtime_config.get(
                "CLOUDFLARE_EMAIL_ACCOUNT_ID", settings.cloudflare_email_account_id
            ),
            "api_token": runtime_config.get(
                "CLOUDFLARE_EMAIL_API_TOKEN", settings.cloudflare_email_api_token
            ),
            "from_address": runtime_config.get(
                "CLOUDFLARE_EMAIL_FROM", settings.cloudflare_email_from
            ),
            "from_name": sender_name(
                runtime_config.get(
                    "CLOUDFLARE_EMAIL_FROM_NAME",
                    settings.cloudflare_email_from_name,
                ),
            ),
        },
        "daily_limit": parse_int(
            runtime_config.get("EMAIL_DAILY_LIMIT"), settings.email_daily_limit
        ),
        "verification_daily_limit_per_user": parse_int(
            runtime_config.get("EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER"),
            settings.email_verification_daily_limit_per_user,
        ),
    }


def redact_github_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {
        **config,
        "client_secret": MASKED_SECRET if config.get("client_secret") else "",
        "client_secret_configured": bool(config.get("client_secret")),
    }


def redact_market_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw_api_token = str(config.get("api_token", "") or "")
    raw_token_status = str(config.get("api_token_status", "") or "")
    return {
        **config,
        "api_token": MASKED_SECRET if raw_api_token else "",
        "api_token_status": "",
        "api_token_configured": bool(raw_api_token),
        "api_token_previews": redact_token_previews(raw_api_token),
        "api_token_statuses": redact_token_statuses(raw_api_token, raw_token_status),
    }


def redact_email_settings(config: dict[str, Any]) -> dict[str, Any]:
    smtp = {**config.get("smtp", {})}
    cloudflare = {**config.get("cloudflare", {})}
    raw_api_token = str(cloudflare.get("api_token", "") or "")
    smtp["password_configured"] = bool(smtp.get("password"))
    smtp["password"] = MASKED_SECRET if smtp.get("password") else ""
    cloudflare["api_token_configured"] = bool(raw_api_token)
    cloudflare["api_token"] = MASKED_SECRET if raw_api_token else ""
    cloudflare["api_token_previews"] = redact_token_previews(raw_api_token)
    return {**config, "smtp": smtp, "cloudflare": cloudflare}


def redact_token_previews(value: str) -> list[str]:
    tokens = parse_github_api_tokens(value)
    if not tokens:
        return []
    return [redact_token_preview(token) for token in tokens]


def redact_token_statuses(value: str, status_value: str = "") -> list[dict[str, Any]]:
    statuses = parse_github_api_token_statuses(status_value)
    return [
        {
            "token": redact_token_preview(token),
            **github_api_token_public_status(statuses.get(github_api_token_hash(token))),
        }
        for token in parse_github_api_tokens(value)
    ]


def redact_token_preview(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) <= 2:
        return token[0] + "*" * max(len(token) - 1, 0)
    return f"{token[0]}{'*' * max(len(token) - 2, 0)}{token[-1]}"


def github_api_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def parse_github_api_token_statuses(value: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    statuses: dict[str, dict[str, Any]] = {}
    for token_hash, status in data.items():
        if not isinstance(token_hash, str) or not isinstance(status, dict):
            continue
        statuses[token_hash] = {
            "disabled": bool(status.get("disabled")),
            "status": str(status.get("status") or ""),
            "error_code": int(status.get("error_code") or 0),
            "error_message": str(status.get("error_message") or "")[:200],
            "retry_after_seconds": int(status.get("retry_after_seconds") or 0),
            "reset_at": str(status.get("reset_at") or ""),
            "checked_at": str(status.get("checked_at") or ""),
        }
    return statuses


def serialize_github_api_token_statuses(statuses: dict[str, dict[str, Any]]) -> str:
    clean = {
        token_hash: status
        for token_hash, status in statuses.items()
        if isinstance(token_hash, str) and isinstance(status, dict)
    }
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean_github_api_token_statuses(
    token_value: str,
    status_value: str,
) -> dict[str, dict[str, Any]]:
    allowed_hashes = {
        github_api_token_hash(token) for token in parse_github_api_tokens(token_value)
    }
    return {
        token_hash: status
        for token_hash, status in parse_github_api_token_statuses(status_value).items()
        if token_hash in allowed_hashes
    }


def github_api_token_public_status(status: dict[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {
            "disabled": False,
            "status": "active",
            "error_code": None,
            "error_message": "",
            "checked_at": "",
        }
    error_code = int(status.get("error_code") or 0)
    return {
        "disabled": bool(status.get("disabled")),
        "status": str(status.get("status") or ("disabled" if status.get("disabled") else "active")),
        "error_code": error_code or None,
        "error_message": str(status.get("error_message") or "")[:200],
        "retry_after_seconds": int(status.get("retry_after_seconds") or 0),
        "reset_at": str(status.get("reset_at") or ""),
        "checked_at": str(status.get("checked_at") or ""),
    }


def runtime_values_from_system_settings(
    payload: SystemSettingsPayload,
    runtime_config: dict[str, str],
) -> dict[str, str]:
    values = {
        "SITE_CONTACT_EMAIL": payload.site.contact_email,
        "SITE_DESCRIPTION": payload.site.description,
        "SITE_DOCS_URL": payload.site.docs_url,
        "SITE_ICON_URL": payload.site.icon_url,
        "SITE_NAME": payload.site.name,
        "SITE_SUBTITLE": payload.site.subtitle,
        "WEB_URL": payload.site.web_url,
        "GITHUB_LOGIN_ENABLED": serialize_bool(payload.auth.github_login_enabled),
        "LOGIN_AGREEMENT_ENABLED": serialize_bool(payload.auth.login_agreement_enabled),
        "LOGIN_AGREEMENT_TEXT": payload.auth.login_agreement_text,
        "PUBLIC_LOGIN_ENABLED": serialize_bool(payload.auth.public_login_enabled),
        "SERVICE_TERMS_ENABLED": serialize_bool(payload.auth.service_terms_enabled),
        "SERVICE_TERMS_TEXT": payload.auth.service_terms_text,
        "GITHUB_ADMIN_ORG": payload.github.admin_org,
        "GITHUB_CALLBACK_URL": payload.github.callback_url,
        "GITHUB_CLIENT_ID": payload.github.client_id,
        "GITHUB_SCOPE": payload.github.scope,
        **runtime_values_from_market_settings(
            payload.market,
            runtime_config,
            legacy_api_token=payload.github.api_token,
        ),
        **runtime_values_from_email_settings(payload.email, runtime_config),
    }
    return values


def runtime_values_from_market_settings(
    payload: Any,
    runtime_config: dict[str, str] | None = None,
    legacy_api_token: str = "",
) -> dict[str, str]:
    values = {
        "GITHUB_METADATA_SYNC_ENABLED": serialize_bool(payload.metadata_sync_enabled),
        "GITHUB_METADATA_SYNC_INTERVAL_SECONDS": str(payload.metadata_sync_interval_seconds),
        "MARKET_COMMENTS_ENABLED": serialize_bool(payload.comments_enabled),
        "MARKET_LIKES_ENABLED": serialize_bool(payload.likes_enabled),
        "MARKET_SUBMISSIONS_ENABLED": serialize_bool(payload.submissions_enabled),
        "MAX_PLUGIN_TAGS": str(payload.max_plugin_tags),
        "PLUGIN_AUTO_APPROVE_ENABLED": serialize_bool(payload.plugin_auto_approve_enabled),
    }
    runtime_config = runtime_config or {}
    api_token = system_github_api_token_payload(payload, legacy_api_token)
    remove_indexes = getattr(payload, "api_token_remove_indexes", []) or []
    if should_write_secret(api_token) or remove_indexes:
        merged_token_pool = merge_github_api_token_pool(
            runtime_config.get("GITHUB_API_TOKEN", ""),
            api_token,
            remove_indexes,
        )
        values["GITHUB_API_TOKEN"] = merged_token_pool
        values["GITHUB_API_TOKEN_STATUS"] = serialize_github_api_token_statuses(
            clean_github_api_token_statuses(
                merged_token_pool,
                runtime_config.get("GITHUB_API_TOKEN_STATUS", ""),
            )
        )
    elif "GITHUB_API_TOKEN" in runtime_config:
        values["GITHUB_API_TOKEN"] = runtime_config["GITHUB_API_TOKEN"]
        values["GITHUB_API_TOKEN_STATUS"] = serialize_github_api_token_statuses(
            clean_github_api_token_statuses(
                runtime_config["GITHUB_API_TOKEN"],
                runtime_config.get("GITHUB_API_TOKEN_STATUS", ""),
            )
        )
    return values


def system_github_api_token_payload(payload: Any, legacy_api_token: str = "") -> str:
    if getattr(payload, "api_token", ""):
        return payload.api_token
    return legacy_api_token


def merge_github_api_token_pool(
    existing_value: str,
    incoming_value: str,
    remove_indexes: list[int],
) -> str:
    existing_tokens = parse_github_api_tokens(existing_value)
    remove_set = set(remove_indexes or [])
    kept_tokens = [token for index, token in enumerate(existing_tokens) if index not in remove_set]
    incoming_tokens = (
        parse_github_api_tokens(incoming_value) if should_write_secret(incoming_value) else []
    )
    return "\n".join(dedupe_tokens([*kept_tokens, *incoming_tokens]))


def dedupe_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def runtime_values_from_email_settings(
    payload: Any,
    runtime_config: dict[str, str],
) -> dict[str, str]:
    smtp_encryption = normalize_smtp_encryption(
        payload.smtp.encryption,
    )
    values = {
        "CLOUDFLARE_EMAIL_ACCOUNT_ID": payload.cloudflare.account_id,
        "CLOUDFLARE_EMAIL_FROM": payload.cloudflare.from_address,
        "CLOUDFLARE_EMAIL_FROM_NAME": payload.cloudflare.from_name,
        "EMAIL_DAILY_LIMIT": str(payload.daily_limit),
        "EMAIL_PROVIDER": payload.provider,
        "EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER": str(payload.verification_daily_limit_per_user),
        "SMTP_AUTH_METHOD": normalize_smtp_auth_method(payload.smtp.auth_method),
        "SMTP_ENCRYPTION": smtp_encryption,
        "SMTP_FROM": payload.smtp.from_address,
        "SMTP_FROM_NAME": payload.smtp.from_name,
        "SMTP_HOST": payload.smtp.host,
        "SMTP_PORT": str(payload.smtp.port),
        "SMTP_SSL": serialize_bool(smtp_encryption == "ssl_tls"),
        "SMTP_USERNAME": payload.smtp.username,
        "SMTP_VALIDATE_CERTS": serialize_bool(payload.smtp.validate_certs),
    }
    if should_write_secret(payload.smtp.password):
        values["SMTP_PASSWORD"] = payload.smtp.password
    elif "SMTP_PASSWORD" in runtime_config:
        values["SMTP_PASSWORD"] = runtime_config["SMTP_PASSWORD"]
    if should_write_secret(payload.cloudflare.api_token):
        values["CLOUDFLARE_EMAIL_API_TOKEN"] = payload.cloudflare.api_token
    elif "CLOUDFLARE_EMAIL_API_TOKEN" in runtime_config:
        values["CLOUDFLARE_EMAIL_API_TOKEN"] = runtime_config["CLOUDFLARE_EMAIL_API_TOKEN"]
    return values


def settings_from_system_settings(
    current: Settings,
    payload: SystemSettingsPayload,
    runtime_config: dict[str, str],
) -> Settings:
    smtp_encryption = normalize_smtp_encryption(
        payload.email.smtp.encryption,
    )
    return current.with_updates(
        site_name=payload.site.name,
        site_icon_url=payload.site.icon_url,
        web_url=payload.site.web_url,
        site_subtitle=payload.site.subtitle,
        site_description=payload.site.description,
        site_contact_email=payload.site.contact_email,
        site_docs_url=payload.site.docs_url,
        github_login_enabled=payload.auth.github_login_enabled,
        public_login_enabled=payload.auth.public_login_enabled,
        login_agreement_enabled=payload.auth.login_agreement_enabled,
        login_agreement_text=payload.auth.login_agreement_text,
        service_terms_enabled=payload.auth.service_terms_enabled,
        service_terms_text=payload.auth.service_terms_text,
        github_client_id=payload.github.client_id,
        github_client_secret=current.github_client_secret,
        github_callback_url=payload.github.callback_url,
        github_scope=payload.github.scope,
        github_admin_org=payload.github.admin_org,
        github_api_token=merge_github_api_token_pool(
            runtime_config.get("GITHUB_API_TOKEN") or current.github_api_token,
            system_github_api_token_payload(payload.market, payload.github.api_token),
            payload.market.api_token_remove_indexes,
        ),
        github_metadata_sync_enabled=payload.market.metadata_sync_enabled,
        github_metadata_sync_interval_seconds=payload.market.metadata_sync_interval_seconds,
        market_submissions_enabled=payload.market.submissions_enabled,
        market_comments_enabled=payload.market.comments_enabled,
        market_likes_enabled=payload.market.likes_enabled,
        plugin_auto_approve_enabled=payload.market.plugin_auto_approve_enabled,
        max_plugin_tags=payload.market.max_plugin_tags,
        email_provider=payload.email.provider,
        smtp_host=payload.email.smtp.host,
        smtp_port=payload.email.smtp.port,
        smtp_username=payload.email.smtp.username,
        smtp_password=preserve_secret(
            payload.email.smtp.password,
            runtime_config.get("SMTP_PASSWORD") or current.smtp_password,
        ),
        smtp_from=payload.email.smtp.from_address,
        smtp_from_name=sender_name(payload.email.smtp.from_name),
        smtp_ssl=smtp_encryption == "ssl_tls",
        smtp_encryption=smtp_encryption,
        smtp_auth_method=normalize_smtp_auth_method(payload.email.smtp.auth_method),
        smtp_validate_certs=payload.email.smtp.validate_certs,
        cloudflare_email_account_id=payload.email.cloudflare.account_id,
        cloudflare_email_api_token=preserve_secret(
            payload.email.cloudflare.api_token,
            runtime_config.get("CLOUDFLARE_EMAIL_API_TOKEN") or current.cloudflare_email_api_token,
        ),
        cloudflare_email_from=payload.email.cloudflare.from_address,
        cloudflare_email_from_name=sender_name(payload.email.cloudflare.from_name),
        email_daily_limit=payload.email.daily_limit,
        email_verification_daily_limit_per_user=payload.email.verification_daily_limit_per_user,
    )


def settings_restart_required(settings: Settings, runtime_config: dict[str, str]) -> bool:
    runtime_database_url = runtime_config.get("DATABASE_URL", "")
    runtime_redis_url = runtime_config.get("REDIS_URL", "")
    return bool(runtime_database_url or runtime_redis_url) and (
        runtime_database_url != settings.database_url or runtime_redis_url != settings.redis_url
    )


def build_saved_postgres_config(
    runtime_config: dict[str, str], database_url: str
) -> dict[str, Any]:
    parsed = parse_postgres_url(database_url)
    return {
        "host": runtime_config.get("POSTGRES_HOST", parsed["host"]),
        "port": parse_int(runtime_config.get("POSTGRES_PORT"), parsed["port"]),
        "database": runtime_config.get("POSTGRES_DATABASE", parsed["database"]),
        "username": runtime_config.get("POSTGRES_USER", parsed["username"]),
        "password": runtime_config.get("POSTGRES_PASSWORD", parsed["password"]),
        "ssl": parse_bool(runtime_config.get("POSTGRES_SSL"), parsed["ssl"]),
    }


def build_saved_redis_config(runtime_config: dict[str, str], redis_url: str) -> dict[str, Any]:
    parsed = parse_redis_url(redis_url)
    return {
        "host": runtime_config.get("REDIS_HOST", parsed["host"]),
        "port": parse_int(runtime_config.get("REDIS_PORT"), parsed["port"]),
        "database": parse_int(runtime_config.get("REDIS_DATABASE"), parsed["database"]),
        "password": runtime_config.get("REDIS_PASSWORD", parsed["password"]),
        "ssl": parse_bool(runtime_config.get("REDIS_SSL"), parsed["ssl"]),
    }


def build_postgres_url(config: dict[str, Any]) -> str:
    username = quote(config["username"], safe="")
    password = quote(config["password"], safe="")
    database = quote(config["database"], safe="")
    host = format_url_host(config["host"])
    query = urlencode({"sslmode": "require" if config["ssl"] else "disable"})
    return f"postgresql://{username}:{password}@{host}:{config['port']}/{database}?{query}"


def build_redis_url(config: dict[str, Any]) -> str:
    scheme = "rediss" if config["ssl"] else "redis"
    password = f":{quote(config['password'], safe='')}@" if config["password"] else ""
    database = int(config["database"])
    host = format_url_host(config["host"])
    return f"{scheme}://{password}{host}:{config['port']}/{database}"


def format_url_host(host: str) -> str:
    value = host.strip()
    if ":" in value and not value.startswith("["):
        return f"[{value}]"
    return value


def parse_postgres_url(value: str) -> dict[str, Any]:
    config = DEFAULT_POSTGRES_CONFIG.copy()
    parsed = urlparse(value or "")
    if not parsed.scheme.startswith("postgresql"):
        return config
    query = parse_qs(parsed.query)
    sslmode = (query.get("sslmode") or [""])[0].lower()
    config.update(
        {
            "host": parsed.hostname or config["host"],
            "port": parsed.port or config["port"],
            "database": unquote(parsed.path.lstrip("/")),
            "username": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "ssl": sslmode not in {"", "disable", "prefer"},
        }
    )
    return config


def parse_redis_url(value: str) -> dict[str, Any]:
    config = DEFAULT_REDIS_CONFIG.copy()
    parsed = urlparse(value or "")
    if parsed.scheme not in {"redis", "rediss"}:
        return config
    config.update(
        {
            "host": parsed.hostname or config["host"],
            "port": parsed.port or config["port"],
            "database": parse_int(parsed.path.lstrip("/"), config["database"]),
            "password": unquote(parsed.password or ""),
            "ssl": parsed.scheme == "rediss",
        }
    )
    return config


def is_valid_site_icon_url(value: str) -> bool:
    if value.startswith("/"):
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_valid_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_local_url(value: str) -> bool:
    hostname = (urlparse(value or "").hostname or "").lower()
    return hostname in {"localhost", "::1", "0.0.0.0"} or hostname.startswith("127.")


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_PATTERN.match(value or ""))


def has_secret_value(incoming: str, existing: str | None) -> bool:
    return should_write_secret(incoming) or bool(existing)


def should_write_secret(value: str | None) -> bool:
    return bool(value and value != MASKED_SECRET)


def preserve_secret(incoming: str, existing: str) -> str:
    return incoming if should_write_secret(incoming) else existing


def github_profile_payload(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(profile["id"]),
        "login": profile["login"],
        "name": profile.get("name") or profile["login"],
        "github_email": profile.get("email") or "",
        "avatar_url": profile.get("avatar_url") or "",
    }


async def link_github_profile_to_user(
    request: Request,
    user: dict[str, Any],
    profile: dict[str, str],
) -> dict[str, Any]:
    existing = await call_store(request, "get_user_by_github_login", profile["login"])
    if existing and existing["id"] != user["id"]:
        if not can_merge_github_user(user, existing):
            raise error(409, "This GitHub account is already linked to another user")
        await transfer_user_owned_records(request, existing["id"], user["id"])
    updated = await call_store(
        request,
        "update_user_profile",
        user["id"],
        {
            "auth_source": "github",
            "avatar_url": profile.get("avatar_url") or "",
            "github_id": profile["id"],
            "github_login": profile["login"],
            "github_name": profile.get("name") or profile["login"],
            "github_email": profile.get("github_email") or profile.get("email") or "",
        },
    )
    if not updated:
        raise error(404, "User not found")
    return updated


def can_merge_github_user(current: dict[str, Any], existing: dict[str, Any]) -> bool:
    return (
        not existing.get("internal_username")
        and not existing.get("password_hash")
        and normalize_role(existing.get("role")) == Role.USER
        and normalize_role(current.get("role")) in {Role.CORE_ADMIN, Role.ADMIN}
    )


async def transfer_user_owned_records(
    request: Request,
    from_user_id: str,
    to_user_id: str,
) -> None:
    transfer = getattr(get_store(request), "merge_user_into_user", None)
    if transfer:
        await resolve_optional_awaitable(transfer(from_user_id, to_user_id))


def normalize_email_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in {"disabled", "smtp", "cloudflare"} else "disabled"


def sender_name(value: Any) -> str:
    return str(value or "").strip() or DEFAULT_EMAIL_FROM_NAME


def formatted_sender(address: str, name: Any) -> str:
    return formataddr((sender_name(name), address))


async def send_email(
    app: FastAPI,
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if settings.email_provider == "disabled":
        raise error(400, "Email service is disabled")
    check_email_daily_limit(app, settings)
    if settings.email_provider == "cloudflare":
        await send_email_via_cloudflare(settings, receiver, subject, content)
    elif settings.email_provider == "smtp":
        await send_email_via_smtp(settings, receiver, subject, content)
    else:
        raise error(400, "Unsupported email provider")
    increment_email_daily_count(app, settings)


def check_email_daily_limit(app: FastAPI, settings: Settings) -> None:
    if settings.email_daily_limit <= 0:
        return
    today = datetime.now(UTC).strftime("%Y%m%d")
    counter = getattr(app.state, "email_daily_counter", {"date": "", "count": 0})
    if counter.get("date") != today:
        counter = {"date": today, "count": 0}
        app.state.email_daily_counter = counter
    if int(counter.get("count", 0)) >= settings.email_daily_limit:
        raise error(429, "Daily email limit exceeded")


def increment_email_daily_count(app: FastAPI, settings: Settings) -> None:
    if settings.email_daily_limit <= 0:
        return
    today = datetime.now(UTC).strftime("%Y%m%d")
    counter = getattr(app.state, "email_daily_counter", {"date": today, "count": 0})
    if counter.get("date") != today:
        counter = {"date": today, "count": 0}
    counter["count"] = int(counter.get("count", 0)) + 1
    app.state.email_daily_counter = counter


async def send_email_via_cloudflare(
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if not settings.cloudflare_email_account_id:
        raise error(400, "Cloudflare account ID is not configured")
    if not settings.cloudflare_email_api_token:
        raise error(400, "Cloudflare API token is not configured")
    if not settings.cloudflare_email_from:
        raise error(400, "Cloudflare from address is not configured")
    payload = {
        "to": receiver,
        "from": {
            "email": settings.cloudflare_email_from,
            "name": sender_name(settings.cloudflare_email_from_name),
        },
        "subject": subject[:998],
        "text": content,
        "html": html.escape(content).replace("\n", "<br>"),
    }
    endpoint = CLOUDFLARE_EMAIL_SEND_ENDPOINT.format(
        account_id=quote(settings.cloudflare_email_account_id, safe="")
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            endpoint,
            headers={
                "authorization": f"Bearer {settings.cloudflare_email_api_token}",
                "content-type": "application/json",
            },
            json=payload,
        )
    data = response.json() if response.content else {}
    if response.status_code == 429:
        raise error(502, "Cloudflare email service is rate limited")
    if response.status_code in {401, 403}:
        raise error(502, "Cloudflare email authentication failed")
    if response.status_code >= 500:
        raise error(502, f"Cloudflare email service error: HTTP {response.status_code}")
    if response.status_code >= 400 or not data.get("success", False):
        raise error(502, cloudflare_email_error_message(data, response.status_code))
    permanent_bounces = (data.get("result") or {}).get("permanent_bounces") or []
    if permanent_bounces:
        raise error(502, f"Cloudflare email permanently bounced: {permanent_bounces}")


def cloudflare_email_error_message(data: dict[str, Any], status_code: int) -> str:
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        messages = [
            f"[{item.get('code')}] {item.get('message')}"
            for item in errors
            if isinstance(item, dict)
        ]
        return "Cloudflare email API error: " + "; ".join(messages)
    return f"Cloudflare email API error: HTTP {status_code}"


async def send_email_via_smtp(
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    if not settings.smtp_host:
        raise error(400, "SMTP host is not configured")
    if not settings.smtp_from:
        raise error(400, "SMTP from address is not configured")
    try:
        await send_email_via_smtp_client(settings, receiver, subject, content)
    except (aiosmtplib.errors.SMTPException, OSError, TimeoutError, ValueError) as exc:
        raise error(502, smtp_error_message(exc)) from exc


async def send_email_via_smtp_client(
    settings: Settings,
    receiver: str,
    subject: str,
    content: str,
) -> None:
    message = EmailMessage()
    message["From"] = formatted_sender(settings.smtp_from, settings.smtp_from_name)
    message["To"] = receiver
    message["Subject"] = subject
    message.set_content(content)
    smtp_encryption = normalize_smtp_encryption(
        settings.smtp_encryption,
    )
    client_options = {
        "hostname": settings.smtp_host,
        "port": settings.smtp_port,
        "timeout": 10,
        "use_tls": smtp_encryption == "ssl_tls",
        "validate_certs": settings.smtp_validate_certs,
    }
    if smtp_encryption == "starttls":
        client_options["start_tls"] = True
    elif smtp_encryption == "none":
        client_options["start_tls"] = False
    client = aiosmtplib.SMTP(**client_options)
    try:
        await client.connect()
        await authenticate_smtp_client(client, settings)
        await client.send_message(message)
    finally:
        if client.is_connected:
            with suppress(aiosmtplib.errors.SMTPException, OSError, TimeoutError):
                await client.quit()


async def authenticate_smtp_client(client: aiosmtplib.SMTP, settings: Settings) -> None:
    auth_method = normalize_smtp_auth_method(settings.smtp_auth_method)
    if auth_method == "none" or not settings.smtp_username:
        return
    if auth_method == "login":
        await client.auth_login(settings.smtp_username, settings.smtp_password)
        return
    if auth_method == "plain":
        await client.auth_plain(settings.smtp_username, settings.smtp_password)
        return
    await client.login(settings.smtp_username, settings.smtp_password)


def smtp_error_message(exc: Exception) -> str:
    details = safe_exception_message(exc)
    if isinstance(exc, aiosmtplib.errors.SMTPResponseException):
        code = getattr(exc, "code", "")
        response = getattr(exc, "message", details)
        details = f"{exc.__class__.__name__} code={code} message={response}"
    elif isinstance(exc, aiosmtplib.errors.SMTPRecipientsRefused):
        details = f"{exc.__class__.__name__}: {getattr(exc, 'recipients', details)}"
    elif details:
        details = f"{exc.__class__.__name__}: {details}"
    else:
        details = exc.__class__.__name__
    return f"SMTP send failed: {details}"


def serialize_bool(value: bool) -> str:
    return "true" if value else "false"


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: str | int | None, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def validate_repo_owner(repo: str, user: dict[str, Any]) -> None:
    owner = validate_github_repo(repo).group("owner")
    if not user.get("github_login"):
        raise error(403, "GitHub login is required to prove repository ownership")
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
        raise error(
            502,
            data.get("error_description")
            or data.get("error")
            or "GitHub OAuth token exchange failed",
        )
    return data["access_token"]


async def fetch_github_profile(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://api.github.com/user",
            headers=github_headers(access_token),
        )
        data = response.json()
        if response.status_code < 400 and data.get("login") and not data.get("email"):
            data["email"] = await fetch_github_primary_email(client, access_token)
    if response.status_code >= 400 or not data.get("login"):
        raise error(502, data.get("message") or "GitHub profile lookup failed")
    return data


async def fetch_github_primary_email(client: httpx.AsyncClient, access_token: str) -> str:
    try:
        response = await client.get(
            "https://api.github.com/user/emails",
            headers=github_headers(access_token),
        )
        data = response.json()
    except Exception:
        return ""
    if response.status_code >= 400 or not isinstance(data, list):
        return ""
    verified = [
        item
        for item in data
        if isinstance(item, dict) and item.get("email") and item.get("verified") is True
    ]
    primary = next((item for item in verified if item.get("primary") is True), None)
    return str((primary or (verified[0] if verified else {})).get("email") or "")


async def promote_org_admin_if_needed(
    request: Request,
    user: dict[str, Any],
    access_token: str,
) -> None:
    settings = await runtime_settings_for_app(request.app)
    if not settings.github_admin_org or is_admin(user):
        return
    if await is_github_org_member(settings.github_admin_org, access_token):
        await call_store(request, "update_user_role", user["id"], "admin")


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


def github_public_headers() -> dict[str, str]:
    return {
        "accept": "application/vnd.github+json",
        "user-agent": "astrbot-community-plugins",
    }


def parse_github_api_tokens(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[\n,;]+", value or "") if token.strip()]


def is_github_api_token_disabled(token: str, statuses: dict[str, dict[str, Any]] | None) -> bool:
    if not statuses:
        return False
    return bool((statuses.get(github_api_token_hash(token)) or {}).get("disabled"))


def first_github_api_token(
    value: str,
    statuses: dict[str, dict[str, Any]] | None = None,
) -> str:
    tokens = parse_github_api_tokens(value)
    for token in tokens:
        if not is_github_api_token_disabled(token, statuses):
            return token
    return ""


def next_system_github_api_token(
    app: FastAPI,
    settings: Settings,
    statuses: dict[str, dict[str, Any]] | None = None,
) -> str:
    tokens = parse_github_api_tokens(settings.github_api_token)
    if not tokens:
        return ""
    active_tokens = [token for token in tokens if not is_github_api_token_disabled(token, statuses)]
    if not active_tokens:
        return ""
    index = int(getattr(app.state, "github_api_token_index", 0) or 0)
    app.state.github_api_token_index = index + 1
    return active_tokens[index % len(active_tokens)]


def github_api_headers(
    user: dict[str, Any] | None = None,
    settings: Settings | None = None,
    token: str = "",
    token_statuses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    token = str(token or (user or {}).get("github_token") or "").strip()
    if not token and settings:
        token = first_github_api_token(settings.github_api_token, token_statuses)
    if not token:
        return github_public_headers()
    return {
        **github_public_headers(),
        "authorization": f"Bearer {token}",
    }


async def all_api_keys(request: Request) -> list[ApiKey | dict[str, Any]]:
    settings = get_settings(request)
    return [*settings.api_keys, *await call_store(request, "list_api_keys")]


def public_api_key(key: ApiKey | dict[str, Any], include_key: bool = False) -> dict[str, Any]:
    if isinstance(key, ApiKey):
        return {"name": key.name, "scopes": list(key.scopes)}
    data = {
        "id": key.get("id"),
        "name": key.get("name"),
        "scopes": key.get("scopes", []),
        "created_at": key.get("created_at"),
    }
    if include_key:
        data["key"] = key.get("key", "")
    return data


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
