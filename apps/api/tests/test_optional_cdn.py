from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.artifacts.archive import PrecheckError
from app.artifacts.github_source import GithubSourceError, ResolvedGithubSource
from app.artifacts.jobs import JobExecutionError
from app.artifacts.service import ArtifactServiceError
from app.artifacts.storage import ArtifactStorageError
from app.cdn import CdnPublicationError, CdnSubscriptions, process_cdn_reviews
from app.config import load_settings
from app.github_authorization import verify_publication_access
from app.store import InMemoryMarketStore
from github_access_helpers import install_github_access_double
from test_artifact_pipeline import byte_stream, plugin_zip
from test_github_authorization import repo as github_repo
from test_manual_publication_authorization import manual_fixture
from test_publication_authorization import setup_runner


@pytest.fixture
def market(tmp_path, monkeypatch):
    cfg = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "artifacts"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "DATABASE_URL": "postgresql://unused/market",
            "REDIS_URL": "redis://unused/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
            "PLUGIN_AUTO_APPROVE_ENABLED": "true",
            "EMAIL_PROVIDER": "disabled",
        }
    )
    store = InMemoryMarketStore()
    app = main.create_app(cfg, store)
    install_github_access_double(app)
    metadata = AsyncMock(return_value={"version": "v1.0.0"})
    monkeypatch.setattr(main, "fetch_plugin_github_metadata", metadata)
    with TestClient(app) as client:
        yield client, app, store, metadata


def submit(client, **patch):
    return client.post(
        "/v1/plugins/submissions",
        headers={"x-dev-github-login": "alice"},
        json={
            "name": "astrbot_plugin_demo",
            "repo": "https://github.com/alice/astrbot_plugin_demo",
            "desc": "测试插件",
            "author": "Alice",
            **patch,
        },
    )


def test_default_non_cdn_auto_lists_without_fetching_or_reviewing(market):
    client, app, store, _ = market
    service = app.state.artifact_runtime.service
    service.submit_github = AsyncMock(side_effect=AssertionError("No package review expected"))
    response = submit(client)
    assert response.status_code == 201
    plugin = response.json()
    assert plugin["cdn_enabled"] is False
    assert plugin["status"] == "listed"
    assert "artifact" not in plugin
    assert app.state.artifact_runtime.repository.artifacts == {}
    assert app.state.artifact_runtime.repository.jobs == {}
    service.submit_github.assert_not_awaited()
    source = client.get("/plugins.json")
    assert source.json()[plugin["name"]]["download_url"] == ""
    assert source.json()[plugin["name"]]["repo"] == plugin["repo"]
    assert source.headers["cache-control"] == "public, max-age=300"
    assert store.list_submissions() == []


def test_non_cdn_respects_manual_listing_policy(market):
    client, app, _, _ = market
    app.state.settings = app.state.settings.with_updates(plugin_auto_approve_enabled=False)
    response = submit(client)
    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    assert app.state.artifact_runtime.repository.artifacts == {}


def test_cdn_submission_creates_durable_request_even_when_market_auto_approves(market):
    client, app, store, _ = market
    response = submit(client, cdn_enabled=True)
    assert response.status_code == 201
    plugin = response.json()
    assert plugin["status"] == "pending" and plugin["cdn_enabled"] is True
    assert plugin["cdn_review_pending"] is True
    subscription = asyncio.run(CdnSubscriptions(store).get(plugin["id"]))
    assert subscription["state"] == "queued"
    assert subscription["requested_version"] == "1.0.0"
    assert "auth_reference" not in subscription["authorization"]
    assert "authorization" not in plugin
    assert not app.state.artifact_runtime.repository.artifacts


def test_duplicate_submission_preserves_running_lease(market):
    client, _, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()
    first = asyncio.run(CdnSubscriptions(store).claim(1))[0]
    assert submit(client, cdn_enabled=True).status_code == 201
    current = asyncio.run(CdnSubscriptions(store).get(plugin["id"]))
    assert current["state"] == "running"
    assert current["lease_id"] == first["lease_id"]


def test_unavailable_cdn_never_silently_approves_or_registers(market):
    client, app, store, _ = market
    app.state.artifact_runtime.service = None
    response = submit(client, cdn_enabled=True)
    assert response.status_code == 503
    assert response.json()["code"] == "cdn_unavailable"
    assert not store.state["plugins"]
    assert submit(client).json()["status"] == "listed"


def test_cdn_toggle_checks_owner_but_disabling_does_not_require_oauth(market):
    client, app, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()
    store.update_plugin_status(plugin["id"], "listed", plugin["owner_user_id"])
    url = f"/v1/plugins/{plugin['id']}/cdn"
    assert client.patch(url, json={"enabled": False}).status_code == 401
    assert (
        client.patch(
            url, json={"enabled": False}, headers={"x-dev-github-login": "bob"}
        ).status_code
        == 403
    )

    def expired(*_):
        raise HTTPException(403, {"code": "github_authorization_required"})

    app.state.github_access_factory = expired
    response = client.patch(url, json={"enabled": False}, headers={"x-dev-github-login": "alice"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["cdn_enabled"] is False
    assert response.json()["status"] == "listed"
    again = client.patch(url, json={"enabled": False}, headers={"x-dev-github-login": "alice"})
    assert again.json()["cdn_generation"] == response.json()["cdn_generation"]
    assert (
        client.patch(
            url, json={"enabled": True}, headers={"x-dev-github-login": "alice"}
        ).status_code
        == 403
    )
    assert not asyncio.run(CdnSubscriptions(store).claim(2))
    install_github_access_double(app)
    result = client.post(
        f"/v1/plugins/{plugin['id']}/artifacts/upload",
        headers={"x-dev-github-login": "alice"},
        files={"file": ("plugin.zip", plugin_zip(), "application/zip")},
    )
    assert result.status_code == 409
    assert result.json()["code"] == "cdn_disabled"


def test_cdn_preference_allows_owner_or_core_admin_not_other_admins(market):
    client, _, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()
    url = f"/v1/plugins/{plugin['id']}/cdn"
    subscriptions = CdnSubscriptions(store)
    before = asyncio.run(subscriptions.get(plugin["id"]))

    admin = store.upsert_github_user({"login": "bob"})
    store.update_user_role(admin["id"], "admin")
    for enabled in (True, False):
        response = client.patch(
            url, json={"enabled": enabled}, headers={"x-dev-github-login": "bob"}
        )
        assert response.status_code == 403
    assert asyncio.run(subscriptions.get(plugin["id"])) == before
    with pytest.raises(HTTPException) as denied:
        asyncio.run(subscriptions.configure(plugin["id"], enabled=False, actor=admin))
    assert denied.value.status_code == 403
    assert asyncio.run(subscriptions.get(plugin["id"])) == before

    core_admin = store.upsert_github_user({"login": "core"})
    store.update_user_role(core_admin["id"], "core_admin")
    disabled = client.patch(url, json={"enabled": False}, headers={"x-dev-github-login": "core"})
    assert disabled.status_code == 200
    assert disabled.json()["cdn_enabled"] is False
    assert asyncio.run(subscriptions.get(plugin["id"]))["state"] == "disabled"
    assert (
        client.patch(
            url, json={"enabled": True}, headers={"x-dev-github-login": "core"}
        ).status_code
        == 403
    )
    store.update_user_role(plugin["owner_user_id"], "admin")
    assert (
        client.patch(
            url, json={"enabled": True}, headers={"x-dev-github-login": "alice"}
        ).status_code
        == 200
    )


async def process(app, store, *, source_error=None):
    plugin = store.get_plugin("astrbot_plugin_demo")
    subscription = await CdnSubscriptions(store).get(plugin["id"])
    proof = subscription["authorization"]
    loader = AsyncMock(
        return_value={
            "id": proof["id"],
            "owner": {"id": proof["owner_id"]},
            "full_name": "alice/astrbot_plugin_demo",
            "private": False,
            "archived": False,
            "default_branch": "main",
        }
    )
    source = ResolvedGithubSource(plugin["repo"], "alice", "astrbot_plugin_demo", "main", "a" * 40)
    source_loader = (
        AsyncMock(side_effect=source_error)
        if source_error is not None
        else AsyncMock(return_value=source)
    )
    app.state.artifact_runtime.service.github.stream_archive = lambda _: byte_stream(plugin_zip())
    count = await process_cdn_reviews(app, repository_loader=loader, source_loader=source_loader)
    return count, loader, source_loader


def test_automatic_review_downloads_once_pins_commit_and_requeues_only_new_versions(market):
    client, app, store, metadata = market
    plugin = submit(client, cdn_enabled=True).json()

    async def run():
        count, _, source_loader = await process(app, store)
        assert count == 1
        source_loader.assert_awaited_once()
        repo = app.state.artifact_runtime.repository
        assert len(repo.artifacts) == 1
        artifact = next(iter(repo.artifacts.values()))
        assert artifact["source_commit_sha"] == "a" * 40
        proof = artifact["submitted_by_snapshot"]["repository_authorization"]
        assert proof["mode"] == "cdn_subscription"
        assert proof["origin"] == "automatic_cdn_update"
        assert "auth_reference" not in proof
        await CdnSubscriptions(store).queue_version(plugin)
        assert (await process(app, store))[0] == 0
        assert len(repo.artifacts) == 1
        metadata.return_value = {"version": "v2.0.0"}
        updated = await main.refresh_plugin_github_metadata_for_plugin(app, plugin)
        row = await CdnSubscriptions(store).get(plugin["id"])
        assert row["requested_version"] == "2.0.0" and row["state"] == "queued"
        await CdnSubscriptions(store).queue_version(updated)
        claims = await CdnSubscriptions(store).claim(2)
        assert len(claims) == 1
        assert not await CdnSubscriptions(store).claim(2)
        await CdnSubscriptions(store).queue_version(plugin)
        assert (await CdnSubscriptions(store).get(plugin["id"]))["requested_version"] == "2.0.0"

    asyncio.run(run())


def test_fetch_failure_is_durable_and_expired_leases_recover_without_stale_completion(market):
    client, app, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()

    async def run():
        subscriptions = CdnSubscriptions(store)
        unavailable = AsyncMock(side_effect=HTTPException(503, {"code": "github_unavailable"}))
        assert (
            await process_cdn_reviews(app, repository_loader=unavailable, source_loader=AsyncMock())
            == 1
        )
        row = await subscriptions.get(plugin["id"])
        assert row["state"] == "error" and row["last_error_code"] == "github_unavailable"
        assert not app.state.artifact_runtime.repository.artifacts
        subscriptions.rows[plugin["id"]]["available_at"] = datetime.now(UTC) - timedelta(seconds=1)
        first = (await subscriptions.claim(1))[0]
        subscriptions.rows[plugin["id"]]["lease_until"] = datetime.now(UTC) - timedelta(seconds=1)
        second = (await subscriptions.claim(1))[0]
        await subscriptions.finish(first, error_code="old_failure")
        assert (await subscriptions.get(plugin["id"]))["lease_id"] == second["lease_id"]
        await subscriptions.configure(
            plugin["id"], enabled=False, actor=store.get_user_by_id(plugin["owner_user_id"])
        )
        await subscriptions.finish(second)
        assert (await subscriptions.get(plugin["id"]))["state"] == "disabled"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("failure", "state", "code"),
    [
        (ArtifactStorageError("archive_too_large", "too large"), "blocked", "archive_too_large"),
        (
            ArtifactStorageError("quarantine_object_missing", "missing"),
            "error",
            "quarantine_object_missing",
        ),
        (
            GithubSourceError("github_source_not_found", "missing"),
            "blocked",
            "github_source_not_found",
        ),
        (
            GithubSourceError("github_api_timeout", "timeout", retryable=True),
            "error",
            "github_api_timeout",
        ),
        (PrecheckError("repo_invalid", "invalid repo"), "blocked", "repo_invalid"),
        (
            ArtifactServiceError("cdn_disabled", "disabled", status_code=409),
            "blocked",
            "cdn_disabled",
        ),
        (HTTPException(413, {"code": "archive_too_large"}), "blocked", "archive_too_large"),
        (RuntimeError("temporary failure"), "error", "cdn_review_unavailable"),
    ],
)
def test_cdn_review_classifies_permanent_and_transient_failures(market, failure, state, code):
    client, app, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()

    async def run():
        if isinstance(failure, GithubSourceError):
            count, _, source_loader = await process(app, store, source_error=failure)
            source_loader.assert_awaited_once()
        else:
            service = app.state.artifact_runtime.service
            service.submit_github = AsyncMock(side_effect=failure)
            count, _, _ = await process(app, store)
            service.submit_github.assert_awaited_once()
        assert count == 1
        subscriptions = CdnSubscriptions(store)
        row = await subscriptions.get(plugin["id"])
        assert row["state"] == state and row["last_error_code"] == code
        subscriptions.rows[plugin["id"]]["available_at"] = datetime.now(UTC) - timedelta(seconds=1)
        assert bool(await subscriptions.claim(1)) is (state == "error")

    asyncio.run(run())


def test_new_version_requeues_blocked_cdn_review(market):
    client, app, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()
    app.state.artifact_runtime.service.submit_github = AsyncMock(
        side_effect=ArtifactStorageError("archive_too_large", "too large")
    )

    async def run():
        subscriptions = CdnSubscriptions(store)
        assert (await process(app, store))[0] == 1
        assert (await subscriptions.get(plugin["id"]))["state"] == "blocked"
        updated = store.update_plugin_metadata(plugin["id"], {"repo_version": "v2.0.0"})
        await subscriptions.queue_version(updated)
        row = await subscriptions.get(plugin["id"])
        assert row["state"] == "queued" and row["requested_version"] == "2.0.0"
        assert len(await subscriptions.claim(1)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("when", ["before", "during", "off_then_on"])
def test_disable_or_toggle_during_publish_cannot_expose_old_job_or_send_failure_mail(when):
    store, repository, storage, checker, runner, job = setup_runner()
    owner = {"id": "owner", "role": "user"}
    proof = {"repo": store.state["plugins"][0]["repo"], "user_id": "owner"}
    checker.verify.return_value = {"repo": proof["repo"], "owner_user_id": "owner"}

    async def run():
        preferences = CdnSubscriptions(store)

        async def disable():
            await preferences.configure("astrbot_plugin_demo", enabled=False, actor=owner)
            if when == "off_then_on":
                await preferences.configure(
                    "astrbot_plugin_demo", enabled=True, actor=owner, authorization=proof
                )

        async def copy(*_):
            await disable()
            return SimpleNamespace(size_bytes=10)

        if when == "before":
            await disable()
            with pytest.raises(JobExecutionError, match="CDN"):
                await runner._run_publish(job)
            storage.publish_if_absent.assert_not_awaited()
        else:
            storage.publish_if_absent.side_effect = copy
            with pytest.raises(CdnPublicationError):
                await runner._run_publish(job)
            assert any(item["type"] == "cleanup_orphan" for item in repository.jobs.values())
        assert store.state["plugins"][0]["current_artifact_id"] is None
        assert not repository.outbox

    asyncio.run(run())


def test_subscription_allows_expired_oauth_only_for_approved_bound_package(tmp_path):
    async def run():
        request, store, artifact, decision, proof = await manual_fixture(tmp_path)
        actor = store.get_user_by_id(artifact["submitted_by"])
        await CdnSubscriptions(store).configure(
            artifact["plugin_id"], enabled=True, actor=actor, authorization=proof
        )
        artifact["submitted_by_snapshot"]["repository_authorization"]["mode"] = "cdn_subscription"
        artifact["submitted_by_snapshot"]["repository_authorization"].pop("auth_reference")
        decision.update(source="policy", action="auto_approve")
        loader = AsyncMock(return_value=github_repo())
        result = await verify_publication_access(request, "a1", site_repository_loader=loader)
        assert result["archive_sha256"] == artifact["archive_sha256"]
        artifact["review_status"] = "pending_review"
        with pytest.raises(HTTPException) as denied:
            await verify_publication_access(request, "a1", site_repository_loader=loader)
        assert denied.value.status_code == 403
        artifact["review_status"] = "approved"
        await CdnSubscriptions(store).configure(artifact["plugin_id"], enabled=False, actor=actor)
        with pytest.raises(HTTPException) as disabled:
            await verify_publication_access(request, "a1", site_repository_loader=loader)
        assert disabled.value.detail["code"] == "cdn_disabled_or_changed"

    asyncio.run(run())


def test_feed_never_exposes_disabled_or_unreviewed_new_version(market):
    client, app, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()
    publication = {
        "id": "published-1",
        "plugin_id": plugin["id"],
        "source_repo": plugin["repo"],
        "version": "v1.0.0",
        "publication_status": "published",
        "download_url": "https://cdn.example.test/1.zip",
    }
    app.state.artifact_runtime.repository.artifacts[publication["id"]] = publication
    store.update_plugin_metadata(plugin["id"], {"current_artifact_id": publication["id"]})
    store.update_plugin_status(plugin["id"], "listed", plugin["owner_user_id"])
    assert (
        client.get("/plugins.json").json()[plugin["name"]]["download_url"]
        == publication["download_url"]
    )
    store.update_plugin_metadata(plugin["id"], {"repo_version": "v2.0.0"})
    assert client.get("/plugins.json").json()[plugin["name"]]["download_url"] == ""
    store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
    assert (
        client.patch(
            f"/v1/plugins/{plugin['id']}/cdn",
            json={"enabled": False},
            headers={"x-dev-github-login": "alice"},
        ).status_code
        == 200
    )
    for path in ("/plugins.json", "/v1/astrbot/plugins", "/v1/astrbot/plugins.json"):
        assert client.get(path).json()[plugin["name"]]["download_url"] == ""
    assert client.get("/v1/plugins").json()["items"][0]["download_url"] == ""
    assert app.state.artifact_runtime.repository.artifacts[publication["id"]] == publication


def test_postgres_cdn_migration_leases_and_publication_fence():
    from app.artifacts.repository import PgArtifactRepository
    from app.schema_migrations import apply_schema_migrations, discover_schema_migrations
    from app.store import PgRedisMarketStore
    from test_advanced_artifact_postgres import (
        SingleConnectionPool,
        artifact_payload,
        begin_isolated_schema,
        database_url,
        seed_market,
    )

    async def run():
        connection, transaction = await begin_isolated_schema(database_url())
        try:
            migrations = discover_schema_migrations()
            await apply_schema_migrations(connection, migrations[:-1])
            await seed_market(connection)
            store = PgRedisMarketStore("postgresql://unused", "redis://unused", 60)
            store.pool = SingleConnectionPool(connection)
            repository = PgArtifactRepository(store)
            plugin = await store.get_plugin("plugin-1")
            owner = await store.get_user_by_id("owner-1")
            proof = {
                "id": "repo-1",
                "repo": plugin["repo"],
                "owner_id": "100",
                "user_id": "owner-1",
                "github_user_id": "100",
                "plugin_owner_user_id": "owner-1",
                "auth_reference": "expired-session-reference",
                "verified_at": "2026-09-27T00:00:00Z",
            }
            artifact = await repository.create_artifact(
                {
                    **artifact_payload("a"),
                    "submitted_by_snapshot": {"repository_authorization": proof},
                }
            )
            await connection.execute(
                "UPDATE market_plugins SET status='listed', repo_version='v1.0.0', current_artifact_id=$1 WHERE id='plugin-1'",
                artifact["id"],
            )
            await connection.execute(
                "UPDATE plugin_artifacts SET review_status='approved', publication_status='published', version='v1.0.0', normalized_version='1.0.0' WHERE id=$1",
                artifact["id"],
            )
            await store.register_plugin(
                owner,
                {
                    "name": "astrbot_plugin_plain",
                    "repo": "https://github.com/alice/astrbot_plugin_plain",
                    "desc": "No CDN",
                    "author": "Alice",
                },
            )
            await apply_schema_migrations(connection, migrations)
            subscription = CdnSubscriptions(store)
            assert (await store.get_plugin("plugin-1"))["cdn_enabled"] is True
            assert (await store.get_plugin("astrbot_plugin_plain"))["cdn_enabled"] is False
            assert "auth_reference" not in (await subscription.get("plugin-1"))["authorization"]
            disabled = await subscription.configure("plugin-1", enabled=False, actor=owner)
            assert (
                disabled["status"] == "listed" and disabled["current_artifact_id"] == artifact["id"]
            )
            assert disabled["cdn_generation"] == 1
            enabled = await subscription.configure(
                "plugin-1", enabled=True, actor=owner, authorization=proof
            )
            assert enabled["cdn_generation"] == 2
            with pytest.raises(CdnPublicationError):
                await repository.publish_artifact(
                    artifact["id"],
                    expected_repo_version="v1.0.0",
                    published_key="never",
                    download_url="https://cdn.example.test/never",
                    expected_cdn_generation=0,
                )
            job = await repository.enqueue_job(
                {
                    "artifact_id": artifact["id"],
                    "type": "publish",
                    "payload": {"expected_repo_version": "v1.0.0"},
                    "idempotency_key": "current-cdn-generation",
                }
            )
            assert job["payload"]["cdn_generation"] == 2
            first = (await subscription.claim(2))[0]
            assert not await subscription.claim(2)
            updated = await store.update_plugin_metadata("plugin-1", {"repo_version": "v2.0.0"})
            await subscription.queue_version(updated)
            await subscription.finish(first, artifact_id=artifact["id"])
            second = (await subscription.claim(2))[0]
            assert second["requested_version"] == "2.0.0"
            assert second["lease_id"] != first["lease_id"]
            await subscription.configure("plugin-1", enabled=False, actor=owner)
            await subscription.finish(second)
            assert (await subscription.get("plugin-1"))["state"] == "disabled"
            assert await connection.fetchval("SELECT count(*) FROM outbox_events") == 0
        finally:
            await transaction.rollback()
            await connection.close()

    asyncio.run(run())


def test_muted_owner_cannot_enable_or_automatically_submit_but_can_disable(market):
    client, app, store, _ = market
    plugin = submit(client, cdn_enabled=True).json()
    user = store.get_user_by_id(plugin["owner_user_id"])
    user["muted_until"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    async def run():
        assert (await process(app, store))[0] == 1
        subscription = await CdnSubscriptions(store).get(plugin["id"])
        assert subscription["state"] == "blocked"
        assert subscription["last_error_code"] == "user_muted"
        assert not app.state.artifact_runtime.repository.artifacts

    asyncio.run(run())
    url = f"/v1/plugins/{plugin['id']}/cdn"
    headers = {"x-dev-github-login": "alice"}
    assert client.patch(url, json={"enabled": False}, headers=headers).status_code == 200
    assert client.patch(url, json={"enabled": True}, headers=headers).status_code == 403
