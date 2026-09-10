import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from app.artifacts.jobs import ArtifactJobRunner, JobExecutionError
from app.artifacts.publication_authorization import (
    PublicationAuthorizationError,
    PublicationAuthorizationClient,
)
from app.artifacts.repository import InMemoryArtifactRepository
from app.store import InMemoryMarketStore


def setup_runner():
    store = InMemoryMarketStore()
    store.state["plugins"].append(
        {
            "id": "astrbot_plugin_demo",
            "name": "astrbot_plugin_demo",
            "repo": "https://github.com/org/astrbot_plugin_demo",
            "repo_version": "v1.0.0",
            "owner_user_id": "owner",
            "owner_github_login": "alice",
            "current_artifact_id": None,
        }
    )
    repository = InMemoryArtifactRepository(store)
    repository.artifacts["a1"] = {
        "id": "a1",
        "plugin_id": "astrbot_plugin_demo",
        "review_status": "approved",
        "publication_status": "unpublished",
        "source_repo": "https://github.com/org/astrbot_plugin_demo",
        "version": "v1.0.0",
        "normalized_version": "1.0.0",
        "path_suffix": "0123456789",
        "archive_sha256": "a" * 64,
        "size_bytes": 10,
        "quarantine_key": "a1/source.zip",
        "submitted_by_snapshot": {"repository_authorization": {"auth_reference": "test"}},
    }
    storage = SimpleNamespace(
        publish_if_absent=AsyncMock(), public_url=lambda _: "https://cdn.example/plugin.zip"
    )
    checker = SimpleNamespace(verify=AsyncMock())
    runner = ArtifactJobRunner(
        repository=repository,
        storage=storage,
        prechecker=None,
        scanner=None,
        worker_id="test",
        lease_seconds=30,
        poll_seconds=1,
        publication_authorizer=checker,
    )
    job = {"id": "publish-a1", "artifact_id": "a1", "payload": {"expected_repo_version": "v1.0.0"}}
    return store, repository, storage, checker, runner, job


@pytest.mark.parametrize("retryable", [False, True])
def test_failed_or_unavailable_permission_check_never_copies_public_bytes(retryable):
    _, repository, storage, checker, runner, job = setup_runner()
    checker.verify.side_effect = PublicationAuthorizationError(
        "access_denied", "权限无效", retryable=retryable
    )
    with pytest.raises(JobExecutionError) as failure:
        asyncio.run(runner._run_publish(job))
    assert failure.value.retryable is retryable
    storage.publish_if_absent.assert_not_awaited()
    assert repository.artifacts["a1"]["publication_status"] == "publish_failed"


def test_repository_change_during_storage_copy_cannot_publish_to_the_new_repository():
    store, repository, storage, checker, runner, job = setup_runner()
    checker.verify.return_value = {
        "repo": "https://github.com/org/astrbot_plugin_demo",
        "owner_user_id": "owner",
    }

    async def copy(*_):
        store.state["plugins"][0]["repo"] = "https://github.com/org/other"
        return SimpleNamespace(size_bytes=10)

    storage.publish_if_absent.side_effect = copy
    with pytest.raises(ValueError, match="repository_authorization_changed"):
        asyncio.run(runner._run_publish(job))
    assert store.state["plugins"][0]["current_artifact_id"] is None
    assert repository.artifacts["a1"]["publication_status"] == "publish_failed"
    assert any(item["type"] == "cleanup_orphan" for item in repository.jobs.values())


def test_worker_receives_only_scoped_api_receipt_and_handles_invalid_responses(monkeypatch):
    response_mode = "valid"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer internal-worker-key"
        if response_mode == "invalid":
            return httpx.Response(
                200, content=b"broken json", headers={"content-type": "application/json"}
            )
        return httpx.Response(
            200,
            json={
                "artifact_id": "a1" if response_mode == "valid" else "other",
                "archive_sha256": "a" * 64,
                "repo": "https://github.com/org/repo",
                "owner_user_id": "owner",
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def run():
        nonlocal response_mode
        client = PublicationAuthorizationClient("http://api:8787", "internal-worker-key")
        artifact = {"id": "a1", "archive_sha256": "a" * 64}
        assert (await client.verify(artifact))["owner_user_id"] == "owner"
        response_mode = "invalid"
        with pytest.raises(PublicationAuthorizationError) as invalid:
            await client.verify(artifact)
        assert invalid.value.retryable is True
        response_mode = "other"
        with pytest.raises(PublicationAuthorizationError):
            await client.verify(artifact)

    asyncio.run(run())
    assert all(request.url.host == "api" for request in requests)
