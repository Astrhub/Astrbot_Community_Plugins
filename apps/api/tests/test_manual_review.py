from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.artifacts.malware import _database_is_fresh, _parse_clamav_version
from app.artifacts.repository import PgArtifactRepository
from app.artifacts.schemas import ArtifactDetailResponse
from app.artifacts.service import public_artifact
from app.config import load_settings
from app.main import create_app
from app.schema_migrations import apply_schema_migrations
from test_artifact_repository import artifact_payload, make_repository


def test_clamd_daemon_timezone_and_actual_staleness():
    raw = b"ClamAV 1.5.3/28131/Tue Sep 22 14:27:06 2026"
    now = datetime(2026, 9, 22, 9, 32, 31, tzinfo=UTC)
    corrected = _parse_clamav_version(raw, ZoneInfo("Asia/Shanghai"))
    assert corrected.database_time == datetime(2026, 9, 22, 6, 27, 6, tzinfo=UTC)
    assert corrected.database_time_assumed_utc is False
    assert _database_is_fresh(corrected, now, 48)
    assert not _database_is_fresh(_parse_clamav_version(raw), now, 48)
    assert not _database_is_fresh(corrected, datetime(2026, 9, 25, tzinfo=UTC), 48)
    explicit = _parse_clamav_version(raw + b" +0000", ZoneInfo("Asia/Shanghai"))
    assert explicit.database_time.hour == 14


@pytest.mark.parametrize("state", ["scanning", "pending_review", "processing_failed"])
def test_manual_approve_without_scan_success_is_atomic_and_idempotent(state):
    async def scenario():
        repo, owner, plugin = make_repository()
        artifact = await repo.create_artifact(
            {**artifact_payload(plugin, owner), "tree_sha256": "b" * 64}
        )
        aid = artifact["id"]
        repo.artifacts[aid]["review_status"] = state
        run = await repo.create_review_run(
            {
                "artifact_id": aid,
                "type": "clamav",
                "status": "failed",
                "error_code": "clamav_database_stale",
            }
        )
        job = await repo.enqueue_job(
            {"artifact_id": aid, "type": "static_scan", "idempotency_key": "scan-old"}
        )
        args = dict(
            action="manual_approve",
            actor={"id": "admin-1", "role": "admin"},
            reason="已人工核对提交包",
            archive_sha256=artifact["archive_sha256"],
            confirmed=True,
            idempotency_key="manual-1",
        )
        approved, repeated = await asyncio.gather(
            repo.review_action(aid, **args), repo.review_action(aid, **args)
        )
        assert approved["review_status"] == repeated["review_status"] == "approved"
        assert repo.jobs[job["id"]]["status"] == "cancelled"
        assert len([j for j in repo.jobs.values() if j["type"] == "publish"]) == 1
        assert len(repo.decisions) == 1
        assert len(repo.outbox) == 1
        event = next(iter(repo.outbox.values()))
        assert event["event_type"] == "artifact_approved"
        assert event["recipient_user_id"] == owner["id"]
        assert event["payload"]["reason"] == args["reason"]
        assert event["payload"]["decision_id"] in repo.decisions
        assert repo.runs[run["id"]]["status"] == "failed"
        decision = next(iter(repo.decisions.values()))
        assert decision["metadata"]["scan_snapshot"][0]["error_code"] == "clamav_database_stale"
        await repo.update_artifact_review_coverage(aid, {"incorrect_late_result": True})
        assert "incorrect_late_result" not in repo.artifacts[aid]["review_coverage"]
        with pytest.raises(ValueError, match="review_already_approved"):
            await repo.enqueue_job(
                {"artifact_id": aid, "type": "clamav_scan", "idempotency_key": "late-scan"}
            )
        with pytest.raises(ValueError, match="idempotency_key_conflict"):
            await repo.review_action(aid, **{**args, "reason": "different"})

    asyncio.run(scenario())


def test_retry_preserves_history_and_cannot_race_running_jobs():
    async def scenario():
        repo, owner, plugin = make_repository()
        artifact = await repo.create_artifact(
            {**artifact_payload(plugin, owner), "tree_sha256": "b" * 64}
        )
        aid = artifact["id"]
        repo.artifacts[aid]["review_status"] = "pending_review"
        run = await repo.create_review_run(
            {"artifact_id": aid, "type": "clamav", "status": "failed"}
        )
        job = await repo.enqueue_job(
            {"artifact_id": aid, "type": "clamav_scan", "idempotency_key": "old"}
        )
        args = dict(
            action="retry_review",
            actor={"id": "admin-1", "role": "admin"},
            reason="修复时区后重试",
            archive_sha256=artifact["archive_sha256"],
            confirmed=True,
            idempotency_key="retry-1",
        )
        repo.jobs[job["id"]]["status"] = "running"
        with pytest.raises(ValueError, match="review_still_running"):
            await repo.review_action(aid, **args)
        assert not repo.decisions
        repo.jobs[job["id"]]["status"] = "succeeded"
        await repo.review_action(aid, **args)
        assert repo.runs[run["id"]]["status"] == "failed"
        assert repo.runs[run["id"]]["superseded_at"]
        assert await repo.list_review_runs(aid) == []
        jobs = await repo.list_artifact_jobs(aid)
        assert len(jobs) == 1 and jobs[0]["type"] == "static_scan"
        assert repo.artifacts[aid]["review_status"] == "scanning"

    asyncio.run(scenario())


def test_manual_routes_and_context_do_not_require_scan_reports(tmp_path):
    repo, owner, plugin = make_repository()
    store = repo.store
    reviewer = store.upsert_github_user({"id": "200", "login": "reviewer", "name": "Reviewer"})
    store.update_user_role(reviewer["id"], "admin")
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "DATABASE_URL": "postgresql://example.invalid/market",
            "REDIS_URL": "redis://example.invalid/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
        }
    )
    app = create_app(settings=settings, store=store)
    with TestClient(app) as client:
        repository = app.state.artifact_runtime.repository
        artifact = asyncio.run(
            repository.create_artifact({**artifact_payload(plugin, owner), "tree_sha256": "b" * 64})
        )
        aid = artifact["id"]
        repository.artifacts[aid]["review_status"] = "processing_failed"
        headers = {"x-dev-github-login": "reviewer"}
        context_url = f"/v1/artifacts/{aid}/review-context"
        action_url = f"/v1/admin/artifacts/{aid}/review-action"
        assert client.get(context_url).status_code == 401
        assert (
            client.get(context_url, headers={"x-dev-github-login": "stranger"}).status_code == 403
        )
        result = client.get(context_url, headers=headers)
        assert result.status_code == 200
        assert "no-store" in result.headers["cache-control"]
        payload = {
            "action": "comment",
            "reason": "<script>文字意见</script>",
            "archive_sha256": artifact["archive_sha256"],
            "idempotency_key": "note-1",
        }
        assert (
            client.post(
                action_url, json=payload, headers={"x-dev-github-login": "alice"}
            ).status_code
            == 403
        )
        result = client.post(action_url, json=payload, headers=headers)
        assert result.status_code == 200
        assert "no-store" in result.headers["cache-control"]
        assert (
            client.get(context_url, headers=headers).json()["decisions"][0]["reason"]
            == payload["reason"]
        )
        payload.update(action="manual_approve", idempotency_key="approve-1")
        assert client.post(action_url, json=payload, headers=headers).status_code == 409
        payload["confirmed"] = True
        result = client.post(action_url, json=payload, headers=headers)
        assert result.status_code == 200
        assert result.json()["artifact"]["review_status"] == "approved"
        assert len(repository.decisions) == 2


def test_postgres_first_release_null_and_manual_actions():
    from test_advanced_artifact_postgres import RepositoryStore, artifact_payload as pg_payload
    from test_advanced_artifact_postgres import begin_isolated_schema, database_url, seed_market

    async def scenario(url):
        connection, transaction = await begin_isolated_schema(url)
        try:
            await apply_schema_migrations(connection)
            await seed_market(connection)
            await connection.execute(
                "UPDATE market_plugins SET repo_version='v1.0.0' WHERE id='plugin-1'"
            )
            repo = PgArtifactRepository(RepositoryStore(connection))
            artifact = await repo.create_artifact({**pg_payload("a"), "tree_sha256": "b" * 64})
            aid = artifact["id"]
            await connection.execute(
                "UPDATE plugin_artifacts SET review_status='processing_failed' WHERE id=$1", aid
            )
            actual = await repo.get_artifact(aid)
            assert actual["published_version"] is None
            detail = ArtifactDetailResponse.model_validate(
                {"artifact": public_artifact(actual), "runs": [], "findings": [], "decisions": []}
            )
            assert detail.artifact.published_version == ""
            await repo.create_review_run({"artifact_id": aid, "type": "clamav", "status": "failed"})
            args = dict(
                actor={"id": "reviewer-1", "role": "admin"},
                reason="核对完成",
                archive_sha256=actual["archive_sha256"],
                confirmed=True,
            )
            await repo.review_action(aid, action="retry_review", idempotency_key="retry-pg", **args)
            assert await repo.list_review_runs(aid) == []
            approved = await repo.review_action(
                aid, action="manual_approve", idempotency_key="approve-pg", **args
            )
            assert approved["review_status"] == "approved"
            event = await connection.fetchrow(
                "SELECT * FROM outbox_events WHERE aggregate_id=$1 AND event_type='artifact_approved'",
                aid,
            )
            assert event and event["payload"]["reason"] == args["reason"]
            await repo.review_action(
                aid, action="manual_approve", idempotency_key="approve-pg", **args
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM artifact_jobs WHERE artifact_id=$1 AND type='publish'",
                    aid,
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM review_runs WHERE artifact_id=$1 AND status='failed'", aid
                )
                == 1
            )
        finally:
            await transaction.rollback()
            await connection.close()

    asyncio.run(scenario(database_url()))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"confirmed": False}, "review_confirmation_required"),
        ({"archive_sha256": "c" * 64}, "artifact_identity_changed"),
        ({"reason": " "}, "review_reason_required"),
        ({"actor": {"id": "user", "role": "user"}}, "review_admin_required"),
    ],
)
def test_manual_approval_rejects_invalid_commands_without_side_effects(change, code):
    async def scenario():
        repo, owner, plugin = make_repository()
        artifact = await repo.create_artifact(
            {**artifact_payload(plugin, owner), "tree_sha256": "b" * 64}
        )
        repo.artifacts[artifact["id"]]["review_status"] = "processing_failed"
        args = dict(
            action="manual_approve",
            actor={"id": "admin", "role": "admin"},
            reason="人工检查",
            archive_sha256=artifact["archive_sha256"],
            confirmed=True,
            idempotency_key="invalid",
        )
        with pytest.raises(ValueError, match=code):
            await repo.review_action(artifact["id"], **{**args, **change})
        assert not repo.decisions and not repo.jobs

    asyncio.run(scenario())


def test_rerun_releases_synthetic_downstream_and_routing_dedupe_keys():
    from app.artifacts.orchestration import ReviewOrchestrator, StageToolSnapshot
    from app.artifacts.policy import ReviewPolicyStage
    from test_artifact_orchestration import _policy_payload, _review_fixture, _complete_stage_job

    async def scenario():
        repo, artifact, _ = await _review_fixture(
            _policy_payload(
                [ReviewPolicyStage.STATIC, ReviewPolicyStage.CLAMAV, ReviewPolicyStage.RUNTIME],
                runtime_targets=[{"astrbot": "4.26.6", "python": "3.12"}],
            )
        )
        aid = artifact["id"]
        orchestrator = ReviewOrchestrator(
            repo,
            tool_snapshots={
                "clamav": StageToolSnapshot("clamd-v1"),
                "runtime": StageToolSnapshot("runtime-v1"),
            },
        )
        await orchestrator.reconcile(aid)
        await _complete_stage_job(repo, stage_name="clamav", outcome="degraded")
        result = await orchestrator.reconcile(aid)
        old_route = result.route_job_id
        assert any(
            run["error_code"] == "upstream_degraded" for run in await repo.list_review_runs(aid)
        )
        repo.artifacts[aid]["review_status"] = "pending_review"
        await repo.review_action(
            aid,
            action="retry_review",
            actor={"id": "admin", "role": "admin"},
            reason="修复后重跑",
            archive_sha256=artifact["archive_sha256"],
            confirmed=True,
            idempotency_key="retry-synthetic",
        )
        jobs = await repo.claim_jobs("rescan", 1, 60)
        assert jobs[0]["type"] == "static_scan"
        await repo.create_review_run(
            {
                "artifact_id": aid,
                "type": "static",
                "status": "succeeded",
                "coverage": {"outcome": "completed"},
            }
        )
        await repo.complete_job(jobs[0]["id"], "rescan")
        await orchestrator.reconcile(aid)
        await _complete_stage_job(repo, stage_name="clamav")
        result = await orchestrator.reconcile(aid)
        assert result.waiting_on or any(
            j["type"] == "runtime_dispatch" and j["status"] == "queued"
            for j in await repo.list_artifact_jobs(aid)
        )
        assert result.route_job_id != old_route
        assert not any(
            run.get("error_code") == "upstream_degraded" for run in await repo.list_review_runs(aid)
        )

    asyncio.run(scenario())
