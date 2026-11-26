"""Pre-parse authentication and body limits for announcement image uploads."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings

MAX_ANNOUNCEMENT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ANNOUNCEMENT_MULTIPART_BYTES = MAX_ANNOUNCEMENT_IMAGE_BYTES + 64 * 1024


class AnnouncementUploadTooLarge(Exception):
    pass


class AnnouncementImageUploadMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        user_loader: Callable[[Request], Awaitable[dict[str, Any]]],
        role_checker: Callable[[dict[str, Any]], bool],
        settings_loader: Callable[[], Settings],
    ) -> None:
        self.app = app
        self.user_loader = user_loader
        self.role_checker = role_checker
        self.settings_loader = settings_loader

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        if scope["path"] != "/v1/core/announcements/images":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            user = await self.user_loader(request)
        except HTTPException as exc:
            await self._response(scope, receive, send, exc.status_code, str(exc.detail))
            return
        if not self.role_checker(user):
            await self._response(scope, receive, send, 403, "需要核心管理员权限")
            return

        origin = request.headers.get("origin")
        if origin and origin not in self._trusted_origins():
            await self._response(scope, receive, send, 403, "图片上传来源不受信任")
            return

        content_length = request.headers.get("content-length")
        if content_length is None:
            await self._response(scope, receive, send, 411, "图片上传需要 Content-Length")
            return
        try:
            declared_bytes = int(content_length)
        except ValueError:
            await self._response(scope, receive, send, 400, "无效的 Content-Length")
            return
        if declared_bytes < 0:
            await self._response(scope, receive, send, 400, "无效的 Content-Length")
            return
        if declared_bytes > MAX_ANNOUNCEMENT_MULTIPART_BYTES:
            await self._response(scope, receive, send, 413, "图片不能超过 5 MB")
            return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > MAX_ANNOUNCEMENT_MULTIPART_BYTES:
                    raise AnnouncementUploadTooLarge
            return message

        response_started = False

        async def observe_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, observe_send)
        except AnnouncementUploadTooLarge:
            if response_started:
                raise
            await self._response(scope, receive, send, 413, "图片不能超过 5 MB")

    def _trusted_origins(self) -> set[str]:
        settings = self.settings_loader()
        candidates = [settings.web_url, *settings.cors_origins]
        origins: set[str] = set()
        for candidate in candidates:
            parsed = urlsplit(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
        return origins

    @staticmethod
    async def _response(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        message: str,
    ) -> None:
        response = JSONResponse(
            {"error": message},
            status_code=status_code,
            headers={"Cache-Control": "private, no-store"},
        )
        await response(scope, receive, send)
