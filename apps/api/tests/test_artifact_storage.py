from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.artifacts.storage import (
    ArtifactStorageError,
    LocalArtifactStorage,
    S3ArtifactStorage,
    build_published_key,
    build_quarantine_key,
)
from app.config import load_settings


async def byte_stream(*chunks: bytes):
    for chunk in chunks:
        yield chunk


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

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
        self.objects[location] = (content, dict(kwargs.get("Metadata") or {}))
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
        content, metadata = self.objects[location]
        return {"ContentLength": len(content), "Metadata": metadata}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        content, _ = self.objects[(bucket, key)]
        Path(filename).write_bytes(content)

    def get_object(self, *, Bucket: str, Key: str):
        content, _ = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(content)}

    def delete_object(self, *, Bucket: str, Key: str):
        self.objects.pop((Bucket, Key), None)
        return {}


def test_build_published_key_uses_required_cdn_shape() -> None:
    key = build_published_key(
        author_id="10001",
        repo_name="astrbot_plugin_demo",
        version="v1.2.0",
        plugin_name="astrbot_plugin_demo",
        suffix="a1b2c3d4e5",
    )

    assert key == ("10001/astrbot_plugin_demo/v1.2.0/astrbot_plugin_demo-v1.2.0-a1b2c3d4e5.zip")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author_id", "../100"),
        ("repo_name", "owner/repo"),
        ("version", "v1%2F2"),
        ("plugin_name", ""),
        ("suffix", "short"),
    ],
)
def test_build_published_key_rejects_unsafe_segments(field: str, value: str) -> None:
    payload = {
        "author_id": "100",
        "repo_name": "repo",
        "version": "v1.0.0",
        "plugin_name": "astrbot_plugin_demo",
        "suffix": "a1b2c3d4e5",
    }
    payload[field] = value

    with pytest.raises(ArtifactStorageError):
        build_published_key(**payload)


def test_same_version_with_distinct_suffixes_has_distinct_keys() -> None:
    common = {
        "author_id": "100",
        "repo_name": "repo",
        "version": "v1.0.0",
        "plugin_name": "astrbot_plugin_demo",
    }

    first = build_published_key(**common, suffix="aaaaaaaaaa")
    second = build_published_key(**common, suffix="bbbbbbbbbb")

    assert first != second


def test_local_storage_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    storage = LocalArtifactStorage(tmp_path, "https://cdn.example.com")
    quarantine_key = build_quarantine_key("artifact_123")
    published_key = build_published_key(
        author_id="100",
        repo_name="repo",
        version="v1.0.0",
        plugin_name="astrbot_plugin_demo",
        suffix="a1b2c3d4e5",
    )
    content = b"plugin-archive"
    digest = hashlib.sha256(content).hexdigest()

    async def scenario() -> None:
        first = await storage.put_quarantine(
            byte_stream(content[:5], content[5:]), quarantine_key, 1024, digest
        )
        second = await storage.put_quarantine(byte_stream(content), quarantine_key, 1024, digest)
        assert first.sha256 == second.sha256 == digest

        published = await storage.publish_if_absent(quarantine_key, published_key, digest)
        repeated = await storage.publish_if_absent(quarantine_key, published_key, digest)
        assert published == repeated
        assert storage.public_url(published_key).endswith(published_key)

        await storage.revoke_published(published_key)
        assert await storage.stat_published(published_key) is None

    asyncio.run(scenario())


def test_local_storage_rejects_conflicting_object(tmp_path: Path) -> None:
    storage = LocalArtifactStorage(tmp_path, "https://cdn.example.com")
    key = build_quarantine_key("artifact_conflict")

    async def scenario() -> None:
        await storage.put_quarantine(byte_stream(b"first"), key, 1024)
        with pytest.raises(ArtifactStorageError, match="different content"):
            await storage.put_quarantine(byte_stream(b"second"), key, 1024)

    asyncio.run(scenario())


def test_s3_storage_uses_conditional_create_and_digest_metadata(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_STORAGE_BACKEND": "s3",
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.com",
            "ARTIFACT_S3_ENDPOINT_URL": "https://s3.example.com",
            "ARTIFACT_S3_ACCESS_KEY_ID": "access",
            "ARTIFACT_S3_SECRET_ACCESS_KEY": "secret",
            "ARTIFACT_QUARANTINE_BUCKET": "quarantine",
            "ARTIFACT_PUBLISHED_BUCKET": "published",
            "DATABASE_URL": "postgresql://example.invalid/market",
        }
    )
    client = FakeS3Client()
    storage = S3ArtifactStorage(settings.artifacts, client=client)
    source_key = build_quarantine_key("artifact_s3")
    published_key = build_published_key(
        author_id="100",
        repo_name="repo",
        version="v1.0.0",
        plugin_name="astrbot_plugin_demo",
        suffix="0123456789",
    )
    content = b"s3-plugin"
    digest = hashlib.sha256(content).hexdigest()

    async def scenario() -> None:
        await storage.put_quarantine(byte_stream(content), source_key, 1024, digest)
        result_key = "runtime/results/dispatch-1/result.json"
        result_content = b'{"status":"passed"}'
        result_sha256 = hashlib.sha256(result_content).hexdigest()
        await storage.put_text_content(result_key, result_content)
        assert await storage.read_text_content(result_key, 1024, result_sha256) == result_content
        with pytest.raises(ArtifactStorageError, match="exceeds limit"):
            await storage.read_text_content(result_key, 4, result_sha256)
        with pytest.raises(ArtifactStorageError, match="does not match"):
            await storage.read_text_content(result_key, 1024, "0" * 64)
        first = await storage.publish_if_absent(source_key, published_key, digest)
        second = await storage.publish_if_absent(source_key, published_key, digest)
        assert first == second
        assert await storage.stat_published(published_key) == first
        destination = tmp_path / "downloaded.zip"
        downloaded = await storage.download_quarantine(source_key, destination)
        assert downloaded.sha256 == digest
        await storage.revoke_published(published_key)
        assert await storage.stat_published(published_key) is None

    asyncio.run(scenario())
