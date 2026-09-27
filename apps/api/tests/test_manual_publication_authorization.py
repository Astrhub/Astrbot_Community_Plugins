import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app import main
from app.artifacts.service import ArtifactService
from app.github_authorization import verify_publication_access
from app.main import create_app
from test_github_authorization import authorized, repo, settings
from test_publication_authorization import setup_runner


async def manual_fixture(tmp_path):
    store, cfg, user, session, access = await authorized(
        tmp_path, lambda _: httpx.Response(200, json=repo())
    )
    proof = await access.verify("https://github.com/my-org/astrbot_plugin_demo")
    proof["plugin_owner_user_id"] = user["id"]
    store.state["plugins"].append(
        {"id": "plugin-1", "repo": proof["repo"], "owner_user_id": user["id"]}
    )
    reviewer = store.upsert_github_user({"id": "2", "login": "reviewer"})
    store.update_user_role(reviewer["id"], "admin")
    store.revoke_session(session["token"])
    artifact = {
        "id": "a1",
        "plugin_id": "plugin-1",
        "submitted_by": user["id"],
        "source_repo": proof["repo"],
        "review_status": "approved",
        "archive_sha256": "a" * 64,
        "submitted_by_snapshot": {"repository_authorization": proof},
    }
    decision = {
        "id": "d1",
        "source": "admin",
        "action": "manual_approve",
        "to_status": "approved",
        "reviewer_user_id": reviewer["id"],
        "metadata": {"archive_sha256": "a" * 64},
    }
    repository = SimpleNamespace(
        get_artifact=AsyncMock(side_effect=lambda _: deepcopy(artifact)),
        list_review_decisions=AsyncMock(side_effect=lambda _: [deepcopy(decision)]),
    )
    state = SimpleNamespace(
        store=store, settings=cfg, artifact_runtime=SimpleNamespace(repository=repository)
    )
    return SimpleNamespace(app=SimpleNamespace(state=state)), store, artifact, decision, proof


def test_expired_author_session_does_not_block_active_admin_approval(tmp_path):
    async def run():
        request, store, artifact, decision, _ = await manual_fixture(tmp_path)
        loader = AsyncMock(return_value=repo())
        result = await verify_publication_access(request, "a1", site_repository_loader=loader)
        assert result == {
            "artifact_id": "a1",
            "archive_sha256": "a" * 64,
            "repo": artifact["source_repo"],
            "owner_user_id": artifact["submitted_by"],
        }
        loader.assert_awaited_once_with(artifact["source_repo"])
        reference = store.get_latest_github_authorization(artifact["submitted_by"])
        assert reference and not store.get_github_authorization(reference)
        decision.update(action="auto_approve", source="policy")
        with pytest.raises(HTTPException) as denied:
            await verify_publication_access(request, "a1", site_repository_loader=loader)
        assert denied.value.detail["code"] == "github_authorization_required"
        assert loader.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,code",
    [
        ("reviewer", "reviewer_role_changed"),
        ("hash", "artifact_identity_changed"),
        ("market_owner", "plugin_owner_changed"),
        ("address", "github_repository_changed"),
        ("github_identity", "github_identity_changed"),
        ("proof", "github_authorization_required"),
        ("repository_id", "github_repository_changed"),
        ("owner_id", "github_repository_changed"),
        ("private", "github_repository_unavailable"),
        ("archived", "github_repository_unavailable"),
        ("disabled", "github_repository_unavailable"),
    ],
)
def test_site_publication_keeps_approval_identity_and_repository_guards(tmp_path, change, code):
    async def run():
        request, store, artifact, decision, proof = await manual_fixture(tmp_path)
        current = repo()
        if change == "reviewer":
            store.update_user_role(decision["reviewer_user_id"], "user")
        elif change == "hash":
            artifact["archive_sha256"] = "b" * 64
        elif change == "market_owner":
            store.state["plugins"][0]["owner_user_id"] = "other"
        elif change == "address":
            store.state["plugins"][0]["repo"] = "https://github.com/other/repo"
        elif change == "github_identity":
            proof["github_user_id"] = "other"
        elif change == "proof":
            proof.pop("verified_at")
        elif change == "repository_id":
            current["id"] = 90
        elif change == "owner_id":
            current["owner"]["id"] = 90
        else:
            current[change] = True
        with pytest.raises(HTTPException) as denied:
            await verify_publication_access(
                request, "a1", site_repository_loader=AsyncMock(return_value=current)
            )
        assert denied.value.detail["code"] == code

    asyncio.run(run())


@pytest.mark.parametrize("status", [200, 301, 401, 403, 404, 429, 500])
def test_site_loader_uses_runtime_token_pool_without_anonymous_fallback(
    tmp_path, monkeypatch, status
):
    from app.store import InMemoryMarketStore

    store = InMemoryMarketStore()
    store.upsert_options(
        {
            "GITHUB_API_TOKEN": "disabled-site-token,active-site-token",
            "GITHUB_API_TOKEN_STATUS": __import__("json").dumps(
                {main.github_api_token_hash("disabled-site-token"): {"disabled": True}}
            ),
        }
    )
    app = create_app(settings(tmp_path), store)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer active-site-token"
        return httpx.Response(
            status, json=repo(), headers={"location": "https://outside.example/never-follow"}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )

    async def run():
        if status == 200:
            assert (
                await main.fetch_publication_repository(
                    app, "https://github.com/my-org/astrbot_plugin_demo"
                )
            )["id"] == 21
        else:
            with pytest.raises(HTTPException):
                await main.fetch_publication_repository(
                    app, "https://github.com/my-org/astrbot_plugin_demo"
                )
        assert len(requests) == 1

    asyncio.run(run())


def test_internal_endpoint_checks_worker_token_and_returns_scoped_site_receipt(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    request, store, _, _, _ = asyncio.run(manual_fixture(tmp_path))
    app = create_app(
        request.app.state.settings.with_updates(artifact_authorization_token="internal-only"), store
    )
    app.state.artifact_runtime = request.app.state.artifact_runtime
    loader = AsyncMock(return_value=repo())
    monkeypatch.setattr(main, "fetch_publication_repository", loader)
    client = TestClient(app)
    url = "/v1/internal/artifacts/a1/authorize-publication"
    assert client.post(url).status_code == 403
    loader.assert_not_awaited()
    response = client.post(url, headers={"Authorization": "Bearer internal-only"})
    assert response.status_code == 200
    assert set(response.json()) == {"artifact_id", "archive_sha256", "repo", "owner_user_id"}
    assert response.headers["cache-control"] == "private, no-store"
    assert "internal-only" not in response.text
    loader.assert_awaited_once()


def test_unconfigured_site_token_fails_closed(tmp_path):
    from app.store import InMemoryMarketStore

    app = create_app(settings(tmp_path), InMemoryMarketStore())
    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            main.fetch_publication_repository(app, "https://github.com/my-org/astrbot_plugin_demo")
        )
    assert denied.value.detail["code"] == "site_github_authorization_unavailable"


@pytest.mark.parametrize("failure", [False, True])
def test_silent_publish_retry_carries_auditable_mail_suppression(failure):
    _, repository, storage, checker, runner, _ = setup_runner()
    repository.artifacts["a1"].update(publication_status="publish_failed", submitted_by="owner")
    checker.verify.return_value = {
        "repo": "https://github.com/org/astrbot_plugin_demo",
        "owner_user_id": "owner",
    }
    if failure:
        from app.artifacts.publication_authorization import PublicationAuthorizationError

        checker.verify.side_effect = PublicationAuthorizationError(
            "github_unavailable", "temporary", retryable=True
        )
    storage.publish_if_absent.return_value = SimpleNamespace(size_bytes=10)
    service = ArtifactService(
        repository=repository, storage=storage, github=None, max_upload_bytes=100
    )

    async def run():
        result = await service.retry_publish(
            "a1", reviewer={"id": "admin", "role": "admin"}, suppress_email=True
        )
        job = repository.jobs[result["job_id"]]
        assert job["payload"]["suppress_email"] is True
        assert next(iter(repository.decisions.values()))["metadata"]["suppress_email"] is True
        if failure:
            from app.artifacts.jobs import JobExecutionError

            with pytest.raises(JobExecutionError):
                await runner._run_publish(job)
        else:
            await runner._run_publish(job)
        assert next(iter(repository.outbox.values()))["payload"]["suppress_email"] is True

    asyncio.run(run())
