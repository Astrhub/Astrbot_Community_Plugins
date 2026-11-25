"""Contract tests for the unified object_storage layer, drivers, credentials, and legacy mapping."""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.artifacts.runtime import ArtifactRuntime
from app.artifacts.storage import (
    ArtifactStorageError,
    build_published_key,
    build_quarantine_key,
    create_artifact_storage,
)
from app.config import load_settings
from app.git_sources.credentials import oauth_credential_cipher
from app.object_storage import (
    LEGACY_PUBLISHED_PROFILE_ID,
    LEGACY_QUARANTINE_PROFILE_ID,
    InMemoryStorageSecretStore,
    LocalFilesystemDriver,
    ObjectStorageError,
    ObjectStorageService,
    S3ObjectStorageDriver,
    StorageBackend,
    StorageObjectStatus,
    StorageProfile,
    StorageProfileStatus,
    StoragePurpose,
    build_legacy_object_storage_service,
    decrypt_storage_secret,
    encrypt_storage_secret,
)
from app.store import InMemoryMarketStore


async def _chunk_stream(*chunks: bytes):
    for chunk in chunks:
        yield chunk


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str, dict[str, str]]] = {}

    def put_object(self, **kwargs):
        location = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and location in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        body = kwargs["Body"]
        content = body.read() if hasattr(body, "read") else bytes(body)
        content_type = str(kwargs.get("ContentType") or "application/octet-stream")
        self.objects[location] = (content, content_type, dict(kwargs.get("Metadata") or {}))
        return {}

    def head_object(self, *, Bucket: str, Key: str):
        location = (Bucket, Key)
        if location not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NotFound", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        content, content_type, metadata = self.objects[location]
        return {
            "ContentLength": len(content),
            "ContentType": content_type,
            "Metadata": metadata,
        }

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        location = (bucket, key)
        if location not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        content, _, _ = self.objects[location]
        Path(filename).write_bytes(content)

    def get_object(self, *, Bucket: str, Key: str, **kwargs):
        location = (Bucket, Key)
        if location not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        content, _, _ = self.objects[location]
        range_header = str(kwargs.get("Range") or "")
        if range_header:
            start_s, end_s = range_header.removeprefix("bytes=").split("-", 1)
            start = int(start_s)
            end = int(end_s) if end_s else len(content) - 1
            end = min(end, len(content) - 1)
            if start >= len(content) or end < start:
                raise ClientError(
                    {
                        "Error": {"Code": "InvalidRange", "Message": "invalid range"},
                        "ResponseMetadata": {"HTTPStatusCode": 416},
                    },
                    "GetObject",
                )
            return {
                "Body": io.BytesIO(content[start : end + 1]),
                "ContentRange": f"bytes {start}-{end}/{len(content)}",
            }
        return {"Body": io.BytesIO(content)}

    def delete_object(self, *, Bucket: str, Key: str):
        self.objects.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", ContinuationToken: str = ""):
        keys = sorted(
            key for (bucket, key) in self.objects if bucket == Bucket and key.startswith(Prefix)
        )
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + 2]
        next_index = start + len(page)
        response = {
            "Contents": [{"Key": key, "Size": len(self.objects[(Bucket, key)][0])} for key in page],
            "IsTruncated": next_index < len(keys),
        }
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(next_index)
        return response


@pytest.mark.parametrize("backend_kind", ["local", "s3"])
def test_object_storage_driver_contract(tmp_path: Path, backend_kind: str) -> None:
    s3_client = FakeS3Client()
    if backend_kind == "local":
        driver = LocalFilesystemDriver(
            profile_id="local-main",
            root=tmp_path / "store",
            public_base_url="https://plugins.eloina.cn/packages",
        )
    else:
        driver = S3ObjectStorageDriver(
            profile_id="s3-main",
            bucket="market-public",
            endpoint_url="https://s3.example.test",
            region="auto",
            access_key_id="ak",
            secret_access_key="sk",
            public_base_url="https://plugins.eloina.cn/packages",
            client=s3_client,
        )

    caps = driver.capabilities()
    assert caps.conditional_create is True
    assert caps.range_read is True
    assert caps.public_read is True
    assert caps.image_variants is False
    assert caps.list_objects is True

    async def scenario() -> None:
        image_bytes = b"RIFFdemoWEBP"
        image_sha = hashlib.sha256(image_bytes).hexdigest()
        stored_image = await driver.put_object(
            "users/u1/avatar.webp",
            _chunk_stream(image_bytes[:4], image_bytes[4:]),
            namespace="avatars",
            max_bytes=128,
            expected_sha256=image_sha,
            if_absent=True,
        )
        assert stored_image.profile_id == driver.profile_id
        assert stored_image.backend == backend_kind
        assert stored_image.namespace == "avatars"
        assert stored_image.content_type == "image/webp"
        assert stored_image.size_bytes == len(image_bytes)
        assert stored_image.sha256 == image_sha
        assert (
            stored_image.public_url
            == "https://plugins.eloina.cn/packages/avatars/users/u1/avatar.webp"
        )
        assert (
            driver.public_url("users/u1/avatar.webp", namespace="avatars")
            == stored_image.public_url
        )

        # Idempotent create with identical content succeeds; conflicting content raises conflict.
        repeated = await driver.put_object(
            "users/u1/avatar.webp",
            image_bytes,
            namespace="avatars",
            if_absent=True,
        )
        assert repeated.sha256 == image_sha
        with pytest.raises(ObjectStorageError) as conflict_exc:
            await driver.put_object(
                "users/u1/avatar.webp",
                b"different-avatar",
                namespace="avatars",
                if_absent=True,
            )
        assert conflict_exc.value.code == "object_key_conflict"

        # Explicit overwrite succeeds when if_absent=False.
        updated_bytes = b"RIFFnew2WEBP"
        overwritten = await driver.put_object(
            "users/u1/avatar.webp",
            updated_bytes,
            namespace="avatars",
            if_absent=False,
        )
        assert overwritten.sha256 == hashlib.sha256(updated_bytes).hexdigest()
        assert (
            await driver.read_object("users/u1/avatar.webp", namespace="avatars") == updated_bytes
        )

        # Size limit and SHA mismatch are rejected cleanly.
        with pytest.raises(ObjectStorageError) as too_large:
            await driver.put_object("big.zip", _chunk_stream(b"0123456789"), max_bytes=4)
        assert too_large.value.code == "object_too_large"
        with pytest.raises(ObjectStorageError) as bad_sha:
            await driver.put_object("bad.zip", b"abc", expected_sha256="0" * 64)
        assert bad_sha.value.code == "sha256_mismatch"

        # Range reads enforce bounds, size, and SHA verification.
        json_bytes = b'{"hello":"world","ok":true}'
        json_sha = hashlib.sha256(json_bytes).hexdigest()
        stored_json = await driver.put_object("meta/info.json", json_bytes)
        assert stored_json.content_type == "application/json; charset=utf-8"
        ranged = await driver.read_range(
            "meta/info.json",
            start_byte=2,
            max_bytes=5,
            max_object_bytes=128,
            expected_size_bytes=len(json_bytes),
            expected_sha256=json_sha,
        )
        assert ranged.content == b"hello"
        assert ranged.start_byte == 2
        assert ranged.end_byte == 7
        assert ranged.truncated is True
        assert ranged.sha256 == json_sha

        # Same-profile copy, listing across pages, download, and deletion.
        copied = await driver.copy_object(
            "meta/info.json",
            "meta/info-copy.json",
            expected_sha256=json_sha,
            if_absent=True,
        )
        assert copied.sha256 == json_sha
        assert copied.content_type == "application/json; charset=utf-8"
        await driver.put_object("meta/extra.txt", b"hello")

        listed = await driver.list_objects(prefix="meta/")
        assert [entry.key for entry in listed] == [
            "meta/extra.txt",
            "meta/info-copy.json",
            "meta/info.json",
        ]

        downloaded_path = tmp_path / f"{backend_kind}-downloaded.json"
        downloaded = await driver.download_to_path("meta/info-copy.json", downloaded_path)
        assert downloaded.sha256 == json_sha
        assert downloaded_path.read_bytes() == json_bytes

        await driver.delete_object("meta/info-copy.json")
        assert await driver.stat_object("meta/info-copy.json") is None

        # Path traversal and invalid namespaces are rejected.
        for bad_key in ("", "/abs/path", "../escape.zip", "a/../b.zip"):
            with pytest.raises(ObjectStorageError):
                await driver.put_object(bad_key, b"x")
        with pytest.raises(ObjectStorageError):
            await driver.put_object("ok.txt", b"x", namespace="../bad")

    asyncio.run(scenario())


def test_storage_credential_encryption_reuses_oauth_key_with_domain_isolation() -> None:
    master_key = "0123456789abcdef0123456789abcdef"
    secret = {"access_key_id": "ak-plugins", "secret_access_key": "sk-super-secret"}

    ciphertext = encrypt_storage_secret(
        master_key,
        profile_id="s3-plugins",
        secret_payload=secret,
    )
    assert "sk-super-secret" not in ciphertext
    assert (
        decrypt_storage_secret(master_key, profile_id="s3-plugins", ciphertext=ciphertext) == secret
    )

    # Domain isolation: ciphertext bound to s3-plugins fails for s3-avatars and for OAuth cipher.
    with pytest.raises(ObjectStorageError) as other_profile:
        decrypt_storage_secret(master_key, profile_id="s3-avatars", ciphertext=ciphertext)
    assert other_profile.value.code == "storage_credential_decrypt_failed"

    oauth_cipher = oauth_credential_cipher(master_key)
    with pytest.raises(Exception):
        oauth_cipher.decrypt(ciphertext.encode("ascii"))

    # Wrong master key or unconfigured master key fails explicitly.
    with pytest.raises(ObjectStorageError) as wrong_key:
        decrypt_storage_secret(
            "fedcba9876543210fedcba9876543210", profile_id="s3-plugins", ciphertext=ciphertext
        )
    assert wrong_key.value.code == "storage_credential_decrypt_failed"

    with pytest.raises(ObjectStorageError) as missing_key:
        encrypt_storage_secret("short", profile_id="s3-plugins", secret_payload=secret)
    assert missing_key.value.code == "storage_credential_key_unconfigured"

    # InMemoryStorageSecretStore round-trip and profile guard.
    secret_store = InMemoryStorageSecretStore(master_key)
    ref = secret_store.save_secret(profile_id="s3-plugins", secret_payload=secret)
    assert secret_store.load_secret(ref, profile_id="s3-plugins") == secret
    with pytest.raises(ObjectStorageError) as mismatch:
        secret_store.load_secret(ref, profile_id="s3-avatars")
    assert mismatch.value.code == "storage_credential_profile_mismatch"
    assert secret_store.delete_secret(ref) is True
    assert secret_store.load_secret(ref, profile_id="s3-plugins") is None


def test_multi_profile_bindings_object_registry_and_cross_profile_copy(tmp_path: Path) -> None:
    s3_client = FakeS3Client()
    service = ObjectStorageService()

    local_quarantine = LocalFilesystemDriver(
        profile_id="local-quarantine",
        root=tmp_path / "quarantine",
    )
    s3_plugins = S3ObjectStorageDriver(
        profile_id="s3-plugins",
        bucket="plugins-bucket",
        endpoint_url="https://s3.example.test",
        public_base_url="https://plugins.eloina.cn/packages",
        client=s3_client,
    )
    s3_avatars = S3ObjectStorageDriver(
        profile_id="s3-avatars",
        bucket="avatars-bucket",
        endpoint_url="https://s3.example.test",
        public_base_url="https://cdn.example.test/avatars",
        client=s3_client,
    )

    service.register_profile(
        StorageProfile(id="local-quarantine", name="Local Q", backend=StorageBackend.LOCAL),
        local_quarantine,
    )
    service.register_profile(
        StorageProfile(
            id="s3-plugins",
            name="S3 Plugins",
            backend=StorageBackend.S3,
            secret_ref="storage_secret_1",
        ),
        s3_plugins,
    )
    service.register_profile(
        StorageProfile(
            id="s3-avatars",
            name="S3 Avatars",
            backend=StorageBackend.S3,
            secret_ref="storage_secret_2",
        ),
        s3_avatars,
    )
    assert service.get_profile("s3-plugins").to_public_dict()["has_secret"] is True
    assert "secret_ref" not in service.get_profile("s3-plugins").to_public_dict()

    # Inactive profile or disallowed backend for quarantine cannot be bound.
    class DummyImgbedDriver(LocalFilesystemDriver):
        backend = StorageBackend.IMGBED

    service.register_profile(
        StorageProfile(
            id="imgbed-avatars",
            name="ImgBed",
            backend=StorageBackend.IMGBED,
            status=StorageProfileStatus.ACTIVE,
        ),
        DummyImgbedDriver(profile_id="imgbed-avatars", root=tmp_path / "imgbed"),
    )
    service.register_profile(
        StorageProfile(
            id="draining-local",
            name="Draining",
            backend=StorageBackend.LOCAL,
            status=StorageProfileStatus.DRAINING,
        ),
        LocalFilesystemDriver(profile_id="draining-local", root=tmp_path / "draining"),
    )

    with pytest.raises(ObjectStorageError) as disallowed:
        service.bind_purpose(StoragePurpose.ARTIFACT_QUARANTINE, "imgbed-avatars")
    assert disallowed.value.code == "storage_backend_disallowed_for_purpose"

    with pytest.raises(ObjectStorageError) as not_active:
        service.bind_purpose(StoragePurpose.ARTIFACT_PUBLISHED, "draining-local")
    assert not_active.value.code == "storage_profile_not_active"

    # Bind distinct profiles per purpose, including two S3 profiles for different purposes.
    q_binding = service.bind_purpose(StoragePurpose.ARTIFACT_QUARANTINE, "local-quarantine")
    p_binding = service.bind_purpose(StoragePurpose.ARTIFACT_PUBLISHED, "s3-plugins")
    a_binding = service.bind_purpose(StoragePurpose.AVATAR, "s3-avatars")
    assert (q_binding.generation, p_binding.generation, a_binding.generation) == (1, 1, 1)

    # Rebinding increments generation cleanly.
    a_binding_v2 = service.bind_purpose(StoragePurpose.AVATAR, "imgbed-avatars", updated_by="core")
    assert a_binding_v2.generation == 2
    service.bind_purpose(StoragePurpose.AVATAR, "s3-avatars", updated_by="core")

    async def scenario() -> None:
        # Failed write discards pending object registration.
        with pytest.raises(ObjectStorageError):
            await service.put_object(
                purpose=StoragePurpose.ARTIFACT_QUARANTINE,
                key="artifacts/bad/source.zip",
                data=b"corrupt",
                expected_sha256="0" * 64,
            )
        assert service.list_object_records(purpose=StoragePurpose.ARTIFACT_QUARANTINE) == []

        # ArtifactStorage facade publishes from local quarantine to S3 published transparently.
        facade = create_artifact_storage(
            load_settings({"ARTIFACTS_ENABLED": "false"}).artifacts,
            object_storage=service,
        )
        zip_bytes = b"PK\x03\x04fake-plugin-zip"
        zip_sha = hashlib.sha256(zip_bytes).hexdigest()
        q_key = build_quarantine_key("art_cross_1")
        pub_key = build_published_key(
            author_id="u1",
            repo_name="astrbot_plugin_demo",
            version="v1.0.0",
            plugin_name="astrbot_plugin_demo",
            suffix="a1b2c3d4e5",
        )

        q_obj = await facade.put_quarantine(_chunk_stream(zip_bytes), q_key, 1024, zip_sha)
        assert q_obj.sha256 == zip_sha
        pub_obj = await facade.publish_if_absent(q_key, pub_key, zip_sha)
        assert pub_obj.sha256 == zip_sha
        assert facade.public_url(pub_key) == f"https://plugins.eloina.cn/packages/{pub_key}"
        assert ("plugins-bucket", pub_key) in s3_client.objects
        assert s3_client.objects[("plugins-bucket", pub_key)][1] == "application/zip"

        # Avatar purpose writes to separate S3 bucket without touching plugins bucket.
        avatar_bytes = b"RIFFavatarWEBP"
        avatar = await service.put_object(
            purpose=StoragePurpose.AVATAR,
            key="u1/avatar.webp",
            data=avatar_bytes,
            owner_type="user",
            owner_id="u1",
        )
        assert ("avatars-bucket", "u1/avatar.webp") in s3_client.objects
        assert s3_client.objects[("avatars-bucket", "u1/avatar.webp")][1] == "image/webp"
        assert avatar.public_url == "https://cdn.example.test/avatars/u1/avatar.webp"

        # Object registry tracks active records per profile and marks deleted on revoke.
        pub_record = service.find_object_record(profile_id="s3-plugins", key=pub_key)
        assert pub_record is not None
        assert pub_record.status == StorageObjectStatus.ACTIVE
        assert pub_record.sha256 == zip_sha

        await facade.revoke_published(pub_key)
        assert await facade.stat_published(pub_key) is None
        revoked_record = service.get_object_record(pub_record.id)
        assert revoked_record is not None
        assert revoked_record.status == StorageObjectStatus.DELETED

    asyncio.run(scenario())


def test_legacy_config_mapping_preserves_artifact_urls_and_leaves_avatar_unbound(
    tmp_path: Path,
) -> None:
    local_settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "local",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "legacy-local"),
            "ARTIFACT_CDN_BASE_URL": "https://plugins.eloina.cn/packages",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )
    service = build_legacy_object_storage_service(local_settings.artifacts)
    assert service.get_binding(StoragePurpose.ARTIFACT_QUARANTINE).profile_id == (
        LEGACY_QUARANTINE_PROFILE_ID
    )
    assert service.get_binding(StoragePurpose.ARTIFACT_PUBLISHED).profile_id == (
        LEGACY_PUBLISHED_PROFILE_ID
    )
    assert service.get_binding(StoragePurpose.AVATAR) is None
    assert service.public_url(
        purpose=StoragePurpose.ARTIFACT_PUBLISHED,
        key="100/repo/v1.0.0/astrbot_plugin_demo-v1.0.0-a1b2c3d4e5.zip",
    ) == (
        "https://plugins.eloina.cn/packages/"
        "100/repo/v1.0.0/astrbot_plugin_demo-v1.0.0-a1b2c3d4e5.zip"
    )

    # ArtifactRuntime exposes the underlying ObjectStorageService alongside ArtifactStorage.
    runtime = ArtifactRuntime(
        config=local_settings.artifacts,
        settings=local_settings,
        database_url=local_settings.database_url,
        store=InMemoryMarketStore(),
        allow_in_memory_artifacts=True,
    )
    asyncio.run(runtime.start(runtime.store))
    try:
        assert runtime.available is True
        assert runtime.object_storage is not None
        assert runtime.storage.object_storage is runtime.object_storage
        assert runtime.object_storage.get_binding(StoragePurpose.AVATAR) is None
    finally:
        asyncio.run(runtime.close())

    bad_settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "unsupported",
            "ARTIFACT_CDN_BASE_URL": "https://plugins.eloina.cn/packages",
        }
    )
    with pytest.raises(ArtifactStorageError) as bad_backend:
        create_artifact_storage(bad_settings.artifacts)
    assert bad_backend.value.code == "storage_backend_invalid"
