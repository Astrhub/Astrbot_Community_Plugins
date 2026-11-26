from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from test_market import make_client

from app.auth import Role, hash_password
from app.config import load_settings
from app.main import create_app
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
        json={"body": "更新后的 **Markdown** 内容"},
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert updated["title"] == "维护通知"
    assert updated["body"] == "更新后的 **Markdown** 内容"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]

    public_response = TestClient(client.app).get("/v1/announcements")
    assert public_response.status_code == 200
    assert public_response.headers["cache-control"] == "private, no-store"
    assert public_response.json()["items"][0]["body"] == updated["body"]

    ordinary_client = TestClient(client.app)
    _login(ordinary_client, "ordinary_user")
    assert (
        ordinary_client.patch(
            f"/v1/core/announcements/{created['id']}", json={"title": "越权修改"}
        ).status_code
        == 403
    )
    assert ordinary_client.delete(f"/v1/core/announcements/{created['id']}").status_code == 403

    assert client.patch(f"/v1/core/announcements/{created['id']}", json={}).status_code == 400
    assert client.delete(f"/v1/core/announcements/{created['id']}").status_code == 200
    assert client.get("/v1/announcements").json()["items"] == []
    assert client.delete(f"/v1/core/announcements/{created['id']}").status_code == 404


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

    assert reloaded.delete_announcement(created["id"]) is True
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

    uploaded = client.post(
        "/v1/core/announcements/images",
        files={"image": ("pixel.png", PNG_1X1, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = uploaded.json()
    assert payload["content_type"] == "image/png"
    assert payload["size_bytes"] == len(PNG_1X1)
    assert payload["url"].startswith("/v1/storage/public/")

    public_response = TestClient(app).get(payload["url"])
    assert public_response.status_code == 200, public_response.text
    assert public_response.content == PNG_1X1
    assert public_response.headers["content-type"] == "image/png"
    assert public_response.headers["cache-control"] == "public, max-age=3600"

    bindings = client.get("/v1/admin/storage/bindings")
    assert bindings.status_code == 200, bindings.text
    avatar = next(item for item in bindings.json()["items"] if item["purpose"] == "avatar")
    assert avatar["profile_id"]

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

    ordinary_client = TestClient(app)
    _login(ordinary_client, "ordinary_user")
    forbidden = ordinary_client.post(
        "/v1/core/announcements/images",
        files={"image": ("pixel.png", PNG_1X1, "image/png")},
    )
    assert forbidden.status_code == 403
