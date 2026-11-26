from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.auth import hash_password
from app.avatar_routes import AVATAR_UPLOADS_PER_HOUR
from app.avatar_upload import MAX_AVATAR_SOURCE_BYTES, normalize_avatar
from app.config import load_settings
from app.main import create_app
from app.object_storage.models import (
    ObjectStorageError,
    StorageObjectStatus,
    StoragePurpose,
)
from app.store import InMemoryMarketStore

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _png(width: int, height: int, color=(10, 200, 60), *, exif: bytes | None = None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), color)
    if exif is not None:
        image.save(buffer, format="JPEG", exif=exif, quality=95)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _webp(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 200, 60)).save(buffer, format="WEBP", quality=90)
    return buffer.getvalue()


def _make_app(tmp_path: Path, *, avatar_upload_enabled: bool = True):
    store = InMemoryMarketStore()
    store.create_internal_admin("owner", hash_password("password123"))
    store.create_internal_user("alice", hash_password("password123"))
    base = load_settings()
    settings = base.with_updates(
        enable_dev_auth=True,
        avatar_upload_enabled=avatar_upload_enabled,
        database_url="postgresql://unused:unused@localhost/unused",
        artifacts=replace(base.artifacts, enabled=True, local_root=str(tmp_path / "objects")),
    )
    return create_app(settings=settings, store=store), store


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/v1/auth/internal/login", json={"username": username, "password": "password123"}
    )
    assert response.status_code == 200, response.text


def _bind_avatar_profile(client: TestClient, tmp_path: Path, profile_id: str = "avatars") -> None:
    created = client.post(
        "/v1/admin/storage/profiles",
        json={
            "id": profile_id,
            "name": profile_id,
            "backend": "local",
            "config": {"mode": "filesystem", "root": str(tmp_path / profile_id)},
        },
    )
    assert created.status_code == 201, created.text
    bound = client.put("/v1/admin/storage/bindings/avatar", json={"profile_id": profile_id})
    assert bound.status_code == 200, bound.text


def _upload(
    client: TestClient, content: bytes, *, name: str = "avatar.png", mime: str = "image/png"
):
    return client.post("/v1/me/avatar", files={"file": (name, content, mime)})


def test_normalize_avatar_orients_crops_strips_metadata_and_reencodes() -> None:
    # EXIF orientation 6 rotates 90° CCW; a 400x200 landscape becomes 200x400, then cropped to
    # 200x200 around the center. UserComment must not survive the re-encode, and an asymmetric
    # left/right color split proves the pixels actually rotated rather than just the dimensions.
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x9286] = "private note"
    split = Image.new("RGB", (400, 200))
    split.paste(Image.new("RGB", (200, 200), (255, 0, 0)), (0, 0))
    split.paste(Image.new("RGB", (200, 200), (0, 0, 255)), (200, 0))
    split.paste(Image.new("RGB", (400, 100), (255, 255, 0)), (0, 100))
    buf = io.BytesIO()
    split.save(buf, format="PNG", exif=exif.tobytes())

    result = normalize_avatar(buf.getvalue())
    assert result.width == 200 and result.height == 200
    assert result.content_type == "image/webp"
    decoded = Image.open(io.BytesIO(result.content))
    assert decoded.format == "WEBP"
    assert dict(decoded.getexif()) == {}
    assert "exif" not in decoded.info
    # Orientation 6 turns the 400x200 image into 200x400: the original red left half
    # fills the whole upper band, the crop line cuts the yellow strip away, and the blue
    # right half lands at the bottom-right of the 200x200 center crop. Skipping the rotation
    # would sample the red/blue halves side by side instead, so these probes pin it down.
    pixels = decoded.convert("RGB")
    assert pixels.getpixel((50, 50))[0] > 200 and pixels.getpixel((50, 50))[2] < 60
    assert pixels.getpixel((150, 50))[0] > 200 and pixels.getpixel((150, 50))[2] < 60
    assert pixels.getpixel((50, 150))[0] > 200 and pixels.getpixel((50, 150))[2] < 60
    assert pixels.getpixel((150, 150))[2] > 200 and pixels.getpixel((150, 150))[0] < 60

    large = normalize_avatar(_png(1024, 768))
    assert (large.width, large.height) == (512, 512)

    small = normalize_avatar(_png(64, 48))
    assert (small.width, small.height) == (48, 48)

    transparent = io.BytesIO()
    Image.new("RGBA", (30, 30), (0, 0, 0, 0)).save(transparent, format="PNG")
    assert Image.open(io.BytesIO(normalize_avatar(transparent.getvalue()).content)).mode == "RGBA"


def test_normalize_avatar_preserves_palette_transparency() -> None:
    # P-mode getbands() does not report alpha, but a transparency chunk must still become one.
    palette = Image.new("P", (64, 64))
    palette.putpalette([255, 0, 0, 0, 0, 0, 0, 0, 0] + [0] * (256 - 3) * 3)
    for y in range(32):
        for x in range(64):
            palette.putpixel((x, y), 1)
    buf = io.BytesIO()
    palette.save(buf, format="PNG", transparency=1)

    result = normalize_avatar(buf.getvalue())
    decoded = Image.open(io.BytesIO(result.content))
    assert decoded.mode == "RGBA"
    upper = decoded.convert("RGBA").getpixel((32, 8))
    lower = decoded.convert("RGBA").getpixel((32, 56))
    assert upper[3] == 0
    assert lower[3] == 255 and lower[:3] == (255, 1, 0)

    # A palette without a transparency chunk flattens to opaque RGB as before.
    opaque = Image.new("P", (64, 64))
    opaque.putpalette([255, 0, 0] + [0] * 765)
    buf2 = io.BytesIO()
    opaque.save(buf2, format="PNG")
    out2 = Image.open(io.BytesIO(normalize_avatar(buf2.getvalue()).content))
    assert out2.mode == "RGB"
    large = normalize_avatar(_png(1024, 768))
    assert (large.width, large.height) == (512, 512)

    small = normalize_avatar(_png(64, 48))
    assert (small.width, small.height) == (48, 48)

    transparent = io.BytesIO()
    Image.new("RGBA", (30, 30), (0, 0, 0, 0)).save(transparent, format="PNG")
    assert Image.open(io.BytesIO(normalize_avatar(transparent.getvalue()).content)).mode == "RGBA"


@pytest.mark.parametrize(
    ("payload", "code", "status"),
    [
        (b"", "avatar_empty", 400),
        (b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_AVATAR_SOURCE_BYTES + 1), "avatar_too_large", 413),
        (b"GIF89a" + b"0" * 32, "avatar_format_unsupported", 415),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "avatar_format_unsupported", 415),
        (b"RIFF\x00\x00\x00\x00WAVEjunk", "avatar_format_unsupported", 415),
        (b"\x89PNG\r\n\x1a\n" + b"garbage" * 8, "avatar_decode_failed", 415),
    ],
)
def test_normalize_avatar_rejects_unsafe_inputs(payload: bytes, code: str, status: int) -> None:
    from app.avatar_upload import AvatarValidationError

    with pytest.raises(AvatarValidationError) as info:
        normalize_avatar(payload)
    assert info.value.code == code
    assert info.value.status_code == status


def test_normalize_avatar_rejects_decompression_bomb_before_decoding() -> None:
    from app.avatar_upload import AvatarValidationError

    oversize = _png(4100, 4100)
    with pytest.raises(AvatarValidationError) as info:
        normalize_avatar(oversize)
    assert info.value.code == "avatar_too_many_pixels"
    assert info.value.status_code == 413


def test_upload_requires_login_and_feature_flag(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    anonymous = TestClient(app).post(
        "/v1/me/avatar", files={"file": ("a.png", _png(8, 8), "image/png")}
    )
    assert anonymous.status_code == 401

    app, _ = _make_app(tmp_path, avatar_upload_enabled=False)
    client = TestClient(app)
    _login(client, "alice")
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    disabled = _upload(client, _png(8, 8))
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "avatar_upload_disabled"
    assert disabled.headers["cache-control"] == "private, no-store"


def test_upload_returns_503_when_avatar_purpose_is_unbound(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    _login(client, "alice")
    response = _upload(client, _png(8, 8))
    assert response.status_code == 503
    assert response.json()["code"] == "avatar_storage_unbound"


def test_upload_persists_avatar_and_public_read_serves_webp(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")

    response = _upload(client, _png(300, 120))
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    avatar = body["avatar"]
    assert avatar["width"] == 120 and avatar["height"] == 120
    assert avatar["content_type"] == "image/webp"
    assert avatar["url"].startswith("/v1/storage/public/")

    user = body["user"]
    assert user["effective_avatar_url"] == avatar["url"]
    assert user["avatar_backend"] == "uploaded"
    for internal in (
        "uploaded_avatar_object_id",
        "uploaded_avatar_profile_id",
        "uploaded_avatar_sha256",
        "uploaded_avatar_updated_at",
    ):
        assert internal not in user

    public = TestClient(app).get(avatar["url"])
    assert public.status_code == 200
    assert public.headers["content-type"] == "image/webp"
    assert public.headers["cache-control"] == "public, max-age=3600"
    assert Image.open(io.BytesIO(public.content)).size == (120, 120)

    stored = store.get_user_by_id(user["id"])
    assert stored["uploaded_avatar_sha256"] == avatar["sha256"]
    assert stored["uploaded_avatar_profile_id"] == "avatars"
    record = asyncio.run(
        app.state.storage_manager.repository.find_active_object(
            profile_id="avatars", namespace=f"users/{user['id']}", key=f"{avatar['sha256']}.webp"
        )
    )
    assert record is not None and record.owner_type == "user_avatar"

    me = client.get("/v1/me").json()
    assert me["effective_avatar_url"] == avatar["url"]


def test_reuploading_identical_image_reuses_object_and_delete_falls_back(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")

    content = _png(90, 90)
    first = _upload(client, content).json()
    second = _upload(client, content)
    assert second.status_code == 201
    assert second.json()["user"]["uploaded_avatar_url"] == first["user"]["uploaded_avatar_url"]
    assert second.json()["avatar"]["sha256"] == first["avatar"]["sha256"]

    removed = client.delete("/v1/me/avatar")
    assert removed.status_code == 200, removed.text
    assert removed.headers["cache-control"] == "private, no-store"
    assert removed.json()["user"]["effective_avatar_url"] == ""
    assert removed.json()["user"]["avatar_backend"] == "oauth"

    again = client.delete("/v1/me/avatar")
    assert again.status_code == 404
    assert again.json()["code"] == "avatar_not_uploaded"


def test_upload_rejects_invalid_files_through_http(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")

    oversized = _upload(client, b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_AVATAR_SOURCE_BYTES + 1))
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "avatar_too_large"

    declared = client.post(
        "/v1/me/avatar",
        files={"file": ("a.png", _png(8, 8), "image/png")},
        headers={"Content-Length": str(MAX_AVATAR_SOURCE_BYTES + 1024 * 1024)},
    )
    assert declared.status_code == 413

    fake_png = _upload(client, b"\x89PNG\r\n\x1a\n" + b"garbage" * 8)
    assert fake_png.status_code == 415
    assert fake_png.json()["code"] == "avatar_decode_failed"

    gif = _upload(client, b"GIF89a" + b"0" * 32, name="a.gif", mime="image/gif")
    assert gif.status_code == 415

    assert client.get("/v1/me").json()["uploaded_avatar_url"] == ""


def test_upload_rate_limit_counts_rejected_requests_too(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")

    # A rejected upload still burns a quota slot: the first request carries a forged MIME
    # type and answers 415, so only AVATAR_UPLOADS_PER_HOUR - 1 valid uploads may succeed
    # afterwards and the very last one must hit the 429 wall.
    rejected = _upload(client, b"GIF89a" + b"0" * 64, name="forged.webp", mime="image/webp")
    assert rejected.status_code == 415
    statuses = [
        _upload(client, _webp(20 + index, 20), name=f"{index}.webp", mime="image/webp").status_code
        for index in range(AVATAR_UPLOADS_PER_HOUR - 1)
    ]
    assert statuses == [201] * (AVATAR_UPLOADS_PER_HOUR - 1)
    limited = _upload(client, _webp(64, 64), name="over.webp", mime="image/webp")
    assert limited.status_code == 429
    assert limited.json()["code"] == "avatar_upload_rate_limited"
    assert limited.json()["retry_after"] == 3600


def test_storage_failure_leaves_user_untouched_and_cleans_pending(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")
    service = app.state.storage_manager.service

    original_put = service.put_object

    async def failing_put(*_args, **_kwargs):
        raise ObjectStorageError("imgbed_capability_check_failed", "simulated")

    service.put_object = failing_put
    try:
        response = _upload(client, _png(50, 50))
    finally:
        service.put_object = original_put
    assert response.status_code == 503
    assert response.json()["code"] == "imgbed_capability_check_failed"
    assert store.get_user_by_id(store.list_users()[1]["id"])["uploaded_avatar_url"] == ""
    records = asyncio.run(
        app.state.storage_manager.repository.list_objects(
            status=StorageObjectStatus.ACTIVE, purpose=StoragePurpose.AVATAR
        )
    )
    assert records == []


def test_database_failure_removes_uploaded_object(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")

    alice = next(user for user in store.list_users() if user["internal_username"] == "alice")

    def failing_set(*_args, **_kwargs):
        raise RuntimeError("database down")

    store.set_user_uploaded_avatar = failing_set  # type: ignore[method-assign]
    try:
        response = _upload(client, _png(40, 40))
    finally:
        del store.set_user_uploaded_avatar
    assert response.status_code == 503
    assert response.json()["code"] == "avatar_persist_failed"

    async def assert_orphan_removed() -> None:
        active = await app.state.storage_manager.repository.list_objects(
            status=StorageObjectStatus.ACTIVE, purpose=StoragePurpose.AVATAR
        )
        assert active == []
        deleted = await app.state.storage_manager.repository.list_objects(
            status=StorageObjectStatus.DELETED, purpose=StoragePurpose.AVATAR
        )
        assert [record.owner_type for record in deleted] == ["user_avatar"]
        driver = app.state.storage_manager.service.driver_for_purpose(StoragePurpose.AVATAR)
        assert await driver.stat_object(deleted[0].key, namespace=deleted[0].namespace) is None

    asyncio.run(assert_orphan_removed())
    assert store.get_user_by_id(alice["id"])["uploaded_avatar_url"] == ""


def test_reuploaded_same_content_keeps_existing_object(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")

    # The second upload carries the same content: the storage layer reuses the object and the
    # cleanup guard must not delete what the user row still points at.
    _upload(client, _png(50, 50))
    stale = store.get_user_by_id(client.get("/v1/me").json()["id"])

    original_set = store.set_user_uploaded_avatar

    def failing_set(*_args, **_kwargs):
        raise RuntimeError("connection dropped")

    store.set_user_uploaded_avatar = failing_set  # type: ignore[method-assign]
    try:
        second = _upload(client, _png(50, 50))
    finally:
        store.set_user_uploaded_avatar = original_set  # type: ignore[method-assign]
    assert second.status_code == 503
    assert second.json()["code"] == "avatar_persist_failed"

    async def assert_object_alive() -> None:
        driver = app.state.storage_manager.service.driver_for_purpose(StoragePurpose.AVATAR)
        active = await app.state.storage_manager.repository.list_objects(
            status=StorageObjectStatus.ACTIVE, purpose=StoragePurpose.AVATAR
        )
        assert len(active) == 1
        assert await driver.stat_object(active[0].key, namespace=active[0].namespace) is not None

    asyncio.run(assert_object_alive())
    # The first avatar is still fully intact on the user row.
    assert store.get_user_by_id(stale["id"])["uploaded_avatar_url"] == stale["uploaded_avatar_url"]


def test_ambiguous_commit_keeps_object(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")
    user_id = client.get("/v1/me").json()["id"]

    # The database commit lands but its response is lost: the user already points at the new
    # object, so the cleanup guard must keep the remote object instead of deleting it.
    original_set = store.set_user_uploaded_avatar

    def committing_set(*args, **kwargs):
        original_set(*args, **kwargs)
        raise RuntimeError("response lost")

    store.set_user_uploaded_avatar = committing_set  # type: ignore[method-assign]
    try:
        response = _upload(client, _png(44, 44))
    finally:
        store.set_user_uploaded_avatar = original_set  # type: ignore[method-assign]
    assert response.status_code == 503

    async def assert_kept() -> None:
        active = await app.state.storage_manager.repository.list_objects(
            status=StorageObjectStatus.ACTIVE, purpose=StoragePurpose.AVATAR
        )
        assert len(active) == 1
        driver = app.state.storage_manager.service.driver_for_purpose(StoragePurpose.AVATAR)
        assert await driver.stat_object(active[0].key, namespace=active[0].namespace) is not None

    asyncio.run(assert_kept())
    assert store.get_user_by_id(user_id)["uploaded_avatar_url"]


def test_concurrent_uploads_only_one_lands_and_other_reports_conflict(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")
    user_id = client.get("/v1/me").json()["id"]

    first = _upload(client, _png(70, 70, color=(1, 2, 3))).json()
    assert first["user"]["uploaded_avatar_url"]

    # Simulate a second request that read the user before the first upload committed: the CAS in
    # set_user_uploaded_avatar must reject it.
    stale = store.set_user_uploaded_avatar(
        user_id,
        url="/v1/storage/public/stale",
        profile_id="avatars",
        object_id="stale",
        sha256="stale",
        expected_object_id="",
    )
    assert stale is None
    assert store.get_user_by_id(user_id)["uploaded_avatar_object_id"] != "stale"

    conflict = _upload(client, _png(70, 70, color=(200, 10, 10)))
    assert conflict.status_code == 201
    assert conflict.json()["user"]["uploaded_avatar_url"] != first["user"]["uploaded_avatar_url"]


def test_profile_patch_rejects_avatar_url(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    _login(client, "alice")
    response = client.patch("/v1/me/profile", json={"avatar_url": "https://evil.example/x.png"})
    assert response.status_code == 400
    assert "avatar_url" in response.json()["error"]
    ok = client.patch("/v1/me/profile", json={"github_name": "Alice"})
    assert ok.status_code == 200
    assert ok.json()["github_name"] == "Alice"


def test_merge_transfers_uploaded_avatar_when_target_has_none(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    owner = TestClient(app)
    _login(owner, "owner")
    _bind_avatar_profile(owner, tmp_path)
    client = TestClient(app)
    _login(client, "alice")
    alice_id = client.get("/v1/me").json()["id"]

    uploaded = _upload(client, _png(60, 60)).json()
    assert uploaded["user"]["uploaded_avatar_url"]

    bob = store.upsert_github_user(
        {
            "id": "9999",
            "login": "bob",
            "name": "Bob",
            "email": "",
            "avatar_url": "",
        }
    )
    merged = store.merge_user_into_user(alice_id, bob["id"])
    assert merged is not None
    assert merged["uploaded_avatar_url"] == uploaded["user"]["uploaded_avatar_url"]
    assert merged["uploaded_avatar_object_id"]
    assert merged["uploaded_avatar_object_id"] != ""
    assert store.get_user_by_id(alice_id) is None
