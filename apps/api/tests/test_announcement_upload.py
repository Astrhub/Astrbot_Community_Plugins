from __future__ import annotations

import asyncio

from app.announcement_upload import (
    MAX_ANNOUNCEMENT_MULTIPART_BYTES,
    AnnouncementImageUploadMiddleware,
)
from app.config import load_settings
from app.media_proxy import detect_safe_image_mime


def test_streaming_body_limit_rejects_misdeclared_content_length() -> None:
    async def exercise() -> list[dict]:
        async def application(scope, receive, send) -> None:
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
            raise AssertionError("The oversized body must be rejected before route handling")

        async def user_loader(request) -> dict:
            return {"id": "core-1", "role": "core_admin"}

        middleware = AnnouncementImageUploadMiddleware(
            application,
            user_loader=user_loader,
            role_checker=lambda user: user.get("role") == "core_admin",
            settings_loader=load_settings,
        )
        chunks = [
            {
                "type": "http.request",
                "body": b"0" * MAX_ANNOUNCEMENT_MULTIPART_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"1", "more_body": False},
        ]

        async def receive() -> dict:
            return chunks.pop(0)

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/core/announcements/images",
            "raw_path": b"/v1/core/announcements/images",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-length", b"1")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await middleware(scope, receive, send)
        return sent

    messages = asyncio.run(exercise())
    assert messages[0]["status"] == 413
    headers = dict(messages[0]["headers"])
    assert headers[b"cache-control"] == b"private, no-store"
    assert b"5 MB" in messages[1]["body"]


def test_avif_detection_requires_a_compatible_brand() -> None:
    generic_heif = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00heic"
    compatible_avif = b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00avif"

    assert detect_safe_image_mime(generic_heif) is None
    assert detect_safe_image_mime(compatible_avif) == "image/avif"
