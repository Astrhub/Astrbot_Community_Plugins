from __future__ import annotations

import asyncio
import hashlib
import base64
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from test_market import make_client

from app.announcement_upload import MAX_ANNOUNCEMENT_MULTIPART_BYTES
from app.auth import Role, hash_password
from app.config import load_settings
from app.main import create_app
from app.object_storage.models import (
    ObjectStorageError,
    StorageObjectStatus,
    StoragePurpose,
)
from app.store import InMemoryMarketStore, LocalFileMarketStore

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _login(client: TestClient, username: str, password: str = "password123") -> None:
    response = client.post(
        "/v1/auth/internal/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def test_core_admin_can_publish_edit_and_delete_announcements() -> None:
    client = make_client()
    store = client.app.state.store
    store.create_internal_admin("owner", hash_password("password123"))
    store.create_internal_user(
        "ordinary_user",
        hash_password("password123"),
        role=Role.USER.value,
    )
    _login(client, "owner")

    created_response = client.post(
        "/v1/core/announcements",
        json={"title": "维护通知", "body": "原始内容"},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert created["updated_at"] == created["created_at"]

    updated_response = client.patch(
        f"/v1/core/announcements/{created['id']}",
        json={"body": "更新后的 **Markdown** 内容", "expected_updated_at": created["updated_at"]},
    )
    assert updated_response.status_code == 200, updated_response.text
    assert updated_response.headers["cache-control"] == "private, no-store"
    updated = updated_response.json()
    assert updated["title"] == "维护通知"
    assert updated["body"] == "更新后的 **Markdown** 内容"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    stale_response = client.patch(
        f"/v1/core/announcements/{created['id']}",
        json={"title": "过期表单", "expected_updated_at": created["updated_at"]},
    )
    assert stale_response.status_code == 409
    assert stale_response.headers["cache-control"] == "private, no-store"

    public_response = TestClient(client.app).get("/v1/announcements")
    assert public_response.status_code == 200
    assert public_response.headers["cache-control"] == "private, no-store"
    assert public_response.json()["items"][0]["body"] == updated["body"]

    ordinary_client = TestClient(client.app)
    _login(ordinary_client, "ordinary_user")
    forbidden_edit = ordinary_client.patch(
        f"/v1/core/announcements/{created['id']}", json={"title": "越权修改"}
    )
    assert forbidden_edit.status_code == 403
    forbidden_delete = ordinary_client.delete(f"/v1/core/announcements/{created['id']}")
    assert forbidden_delete.status_code == 403

    empty_update = client.patch(f"/v1/core/announcements/{created['id']}", json={})
    assert empty_update.status_code == 400
    deleted_response = client.delete(f"/v1/core/announcements/{created['id']}")
    assert deleted_response.status_code == 200
    assert deleted_response.headers["cache-control"] == "private, no-store"
    remaining_response = client.get("/v1/announcements")
    assert remaining_response.json()["items"] == []
    missing_delete = client.delete(f"/v1/core/announcements/{created['id']}")
    assert missing_delete.status_code == 404


def test_local_file_store_persists_announcement_changes(tmp_path: Path) -> None:
    data_path = tmp_path / "market-state.json"
    store = LocalFileMarketStore(data_path)
    store.connect()
    author = store.create_internal_admin("owner", hash_password("password123"))
    created = store.publish_announcement("标题", "正文", author["id"])

    updated = store.update_announcement(created["id"], title="新标题", body="新正文")
    assert updated is not None
    store.close()

    reloaded = LocalFileMarketStore(data_path)
    reloaded.connect()
    persisted = reloaded.list_announcements()[0]
    assert persisted["title"] == "新标题"
    assert persisted["body"] == "新正文"
    assert persisted["created_at"] == created["created_at"]
    assert persisted["updated_at"] >= created["updated_at"]

    deleted = reloaded.delete_announcement(created["id"])
    assert deleted is True
    reloaded.close()
    deleted_check = LocalFileMarketStore(data_path)
    deleted_check.connect()
    assert deleted_check.list_announcements() == []


def test_announcement_image_upload_validation_and_public_read(tmp_path: Path) -> None:
    store = InMemoryMarketStore()
    store.create_internal_admin("owner", hash_password("password123"))
    store.create_internal_user(
        "ordinary_user",
        hash_password("password123"),
        role=Role.USER.value,
    )
    base_settings = load_settings()
    settings = base_settings.with_updates(
        enable_dev_auth=True,
        database_url="postgresql://unused:unused@localhost/unused",
        artifacts=replace(
            base_settings.artifacts,
            enabled=True,
            local_root=str(tmp_path / "objects"),
        ),
    )
    app = create_app(settings=settings, store=store)
    client = TestClient(app)
    _login(client, "owner")

    # Avatar no longer inherits the plugin published profile; bind a dedicated test profile.
    created = client.post(
        "/v1/admin/storage/profiles",
        json={
            "id": "announcement-images",
            "name": "Announcement Images",
            "backend": "local",
            "config": {
                "mode": "filesystem",
                "root": str(tmp_path / "announcement-images"),
            },
        },
    )
    assert created.status_code == 201, created.text
    bound = client.put(
        "/v1/admin/storage/bindings/avatar",
        json={"profile_id": "announcement-images"},
    )
    assert bound.status_code == 200, bound.text

    uploaded = client.post(
        "/v1/core/announcements/images",
        files={"image": ("pixel.png", PNG_1X1, "image/png")},
        headers={"Origin": settings.web_url},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.headers["cache-control"] == "private, no-store"
    payload = uploaded.json()
    assert payload["content_type"] == "image/png"
    assert payload["size_bytes"] == len(PNG_1X1)
    assert payload["url"].startswith("/v1/storage/public/")

    repeated = client.post(
        "/v1/core/announcements/images",
        files={"image": ("retry.png", PNG_1X1, "image/png")},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["url"] == payload["url"]
    public_response = TestClient(app).get(payload["url"])
    assert public_response.status_code == 200, public_response.text
    assert public_response.content == PNG_1X1
    assert public_response.headers["content-type"] == "image/png"
    assert public_response.headers["cache-control"] == "public, max-age=3600"

    bindings = client.get("/v1/admin/storage/bindings")
    assert bindings.status_code == 200, bindings.text
    avatar = next(item for item in bindings.json()["items"] if item["purpose"] == "avatar")
    assert avatar["profile_id"] == "announcement-images"

    repository = app.state.storage_manager.repository
    original_upsert = repository.upsert_object

    async def fail_activation(record) -> None:
        if (
            record.purpose == StoragePurpose.AVATAR
            and record.namespace == "announcements"
            and record.status == StorageObjectStatus.ACTIVE
        ):
            raise ObjectStorageError("activation_failed", "Simulated activation failure")
        await original_upsert(record)

    failed_content = PNG_1X1 + b"retry"
    failed_key = f"{hashlib.sha256(failed_content).hexdigest()}.png"
    repository.upsert_object = fail_activation
    try:
        failed = client.post(
            "/v1/core/announcements/images",
            files={"image": ("failure.png", failed_content, "image/png")},
        )
    finally:
        repository.upsert_object = original_upsert
    assert failed.status_code == 503, failed.text

    async def assert_failed_upload_removed() -> None:
        record = await repository.find_active_object(
            profile_id=avatar["profile_id"],
            namespace="announcements",
            key=failed_key,
        )
        assert record is None
        driver = app.state.storage_manager.service.driver_for_purpose(StoragePurpose.AVATAR)
        assert await driver.stat_object(failed_key, namespace="announcements") is None

    asyncio.run(assert_failed_upload_removed())
    invalid = client.post(
        "/v1/core/announcements/images",
        files={"image": ("fake.png", b"not an image", "image/png")},
    )
    assert invalid.status_code == 415
    oversized = client.post(
        "/v1/core/announcements/images",
        files={"image": ("large.png", b"0" * (5 * 1024 * 1024 + 1), "image/png")},
    )
    assert oversized.status_code == 413
    generic_heif = client.post(
        "/v1/core/announcements/images",
        files={
            "image": (
                "generic.heic",
                b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00heic",
                "image/heic",
            )
        },
    )
    assert generic_heif.status_code == 415
    declared_oversized = client.post(
        "/v1/core/announcements/images",
        headers={"Content-Length": str(MAX_ANNOUNCEMENT_MULTIPART_BYTES + 1)},
    )
    assert declared_oversized.status_code == 413
    # Direct ASGI chunks cover a body that exceeds its declared length.
    untrusted_origin = client.post(
        "/v1/core/announcements/images",
        files={"image": ("pixel.png", PNG_1X1, "image/png")},
        headers={"Origin": "https://untrusted.example"},
    )
    assert untrusted_origin.status_code == 403
    unauthenticated = TestClient(app).post(
        "/v1/core/announcements/images",
        headers={"Content-Length": str(MAX_ANNOUNCEMENT_MULTIPART_BYTES + 1)},
    )
    assert unauthenticated.status_code == 401

    ordinary_client = TestClient(app)
    _login(ordinary_client, "ordinary_user")
    forbidden = ordinary_client.post(
        "/v1/core/announcements/images",
        files={"image": ("pixel.png", PNG_1X1, "image/png")},
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["cache-control"] == "private, no-store"
