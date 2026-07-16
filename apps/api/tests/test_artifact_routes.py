from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore
from test_artifact_pipeline import plugin_zip


def test_artifact_routes_enforce_ownership_and_publish_feed(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_SUBMISSION_RPM": "2",
            "DATABASE_URL": "postgresql://example.invalid/market",
            "REDIS_URL": "redis://example.invalid/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
        }
    )
    store = InMemoryMarketStore()
    app = create_app(settings=settings, store=store)
    registration = {
        "name": "astrbot_plugin_demo",
        "display_name": "Demo",
        "desc": "Demo plugin",
        "author": "Alice",
        "repo": "https://github.com/alice/astrbot_plugin_demo",
        "tags": [],
        "category": "other",
    }

    with TestClient(app) as client:
        assert client.get("/v1/me/artifacts").status_code == 401
        owner_headers = {"x-dev-github-login": "alice"}
        created = client.post(
            "/v1/plugins/registrations",
            headers=owner_headers,
            json=registration,
        )
        assert created.status_code == 200
        plugin = created.json()["plugin"]
        repeated = client.post(
            "/v1/plugins/registrations",
            headers=owner_headers,
            json={**registration, "display_name": "Do not overwrite"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["plugin"]["display_name"] == "Demo"
        ownership_conflict = client.post(
            "/v1/plugins/registrations",
            headers={"x-dev-github-login": "mallory"},
            json={**registration, "repo": "https://github.com/mallory/astrbot_plugin_demo"},
        )
        assert ownership_conflict.status_code == 403
        store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})

        forbidden = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers={"x-dev-github-login": "mallory"},
            files={"file": ("plugin.zip", plugin_zip(), "application/zip")},
        )
        assert forbidden.status_code == 403

        submitted = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            files={"file": ("plugin.zip", plugin_zip(), "application/zip")},
        )
        assert submitted.status_code == 202
        artifact_id = submitted.json()["artifact"]["id"]
        repository = app.state.artifact_runtime.repository
        asyncio.run(
            repository.create_review_run(
                {
                    "artifact_id": artifact_id,
                    "type": "category",
                    "status": "failed",
                    "summary": "AI 分类建议不可用",
                    "raw_result": {"provider_response": {"hidden": "private"}},
                    "raw_result_key": "private/category-result.json",
                    "error_code": "category_result_invalid",
                }
            )
        )
        public_runs = client.get(
            f"/v1/artifacts/{artifact_id}/runs",
            headers=owner_headers,
        )
        public_detail = client.get(
            f"/v1/artifacts/{artifact_id}",
            headers=owner_headers,
        )
        assert public_runs.status_code == 200
        assert public_detail.status_code == 200
        assert all("raw_result" not in run for run in public_runs.json()["items"])
        assert all("raw_result_key" not in run for run in public_runs.json()["items"])
        assert all("raw_result" not in run for run in public_detail.json()["runs"])

        first_page = client.get(
            "/v1/me/artifacts?limit=1&offset=0",
            headers=owner_headers,
        )
        second_page = client.get(
            "/v1/me/artifacts?limit=1&offset=1",
            headers=owner_headers,
        )
        assert [item["id"] for item in first_page.json()["items"]] == [artifact_id]
        assert second_page.json()["items"] == []

        rate_limited = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            files={"file": ("plugin.zip", plugin_zip(), "application/zip")},
        )
        assert rate_limited.status_code == 429
        assert rate_limited.json()["code"] == "artifact_submission_rate_limited"

        hidden = client.get(
            f"/v1/artifacts/{artifact_id}",
            headers={"x-dev-github-login": "mallory"},
        )
        assert hidden.status_code == 403
        assert client.get("/v1/admin/artifacts", headers=owner_headers).status_code == 403

        muted = store.upsert_github_user({"id": "300", "login": "bob", "name": "Bob"})
        store.mute_user(
            muted["id"],
            (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            str(store.get_user_by_github_login("alice")["id"]),
            "spam",
        )
        muted_registration = client.post(
            "/v1/plugins/registrations",
            headers={"x-dev-github-login": "bob"},
            json={
                **registration,
                "name": "astrbot_plugin_muted",
                "repo": "https://github.com/bob/astrbot_plugin_muted",
            },
        )
        assert muted_registration.status_code == 403

        asyncio.run(app.state.artifact_runtime.job_runner.run_once())
        asyncio.run(app.state.artifact_runtime.job_runner.run_once())

        detail = client.get(f"/v1/artifacts/{artifact_id}", headers=owner_headers)
        assert detail.status_code == 200
        assert detail.json()["artifact"]["review_status"] == "pending_review"
        assert "quarantine_key" not in detail.json()["artifact"]

        owner = store.get_user_by_github_login("alice")
        store.update_user_role(owner["id"], "admin")
        self_approval = client.post(
            f"/v1/admin/artifacts/{artifact_id}/approve",
            headers={"x-dev-github-login": "alice", "idempotency-key": "self-approval"},
            json={"reason": "must be rejected"},
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == "self_approval_forbidden"
        store.update_user_role(owner["id"], "user")

        reviewer = store.upsert_github_user({"id": "200", "login": "reviewer", "name": "Reviewer"})
        store.update_user_role(reviewer["id"], "admin")
        store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.1.0"})
        drifted = client.post(
            f"/v1/admin/artifacts/{artifact_id}/approve",
            headers={"x-dev-github-login": "reviewer", "idempotency-key": "approve-drifted"},
            json={"reason": "should not publish"},
        )
        assert drifted.status_code == 409
        still_pending = client.get(f"/v1/artifacts/{artifact_id}", headers=owner_headers).json()
        assert still_pending["artifact"]["review_status"] == "pending_review"
        store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
        approved = client.post(
            f"/v1/admin/artifacts/{artifact_id}/approve",
            headers={"x-dev-github-login": "reviewer", "idempotency-key": "approve-route"},
            json={"reason": "manual review passed"},
        )
        assert approved.status_code == 200
        asyncio.run(app.state.artifact_runtime.job_runner.run_once())

        feed = client.get("/plugins.json").json()
        assert feed["astrbot_plugin_demo"]["version"] == "v1.0.0"
        assert feed["astrbot_plugin_demo"]["download_url"].startswith(
            f"https://cdn.example.test/{plugin['owner_user_id']}/astrbot_plugin_demo/v1.0.0/"
        )
        notifications = store.list_notifications(plugin["owner_user_id"])
        assert any("/plugin-workbench?artifact=" in item["body"] for item in notifications)
        assert all("print" not in item["body"] for item in notifications)

        store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.1.0", "version": "v1.1.0"})
        changed_feed = client.get("/plugins.json").json()
        assert changed_feed["astrbot_plugin_demo"]["version"] == "v1.1.0"
        assert changed_feed["astrbot_plugin_demo"]["download_url"] == ""

        revoke = client.post(
            f"/v1/admin/plugins/{plugin['id']}/revoke-release",
            headers={"x-dev-github-login": "reviewer", "idempotency-key": "revoke-route"},
            json={"reason": "confirmed critical risk"},
        )
        assert revoke.status_code == 200
        assert revoke.json()["artifact"]["publication_status"] == "revoking"
        assert "astrbot_plugin_demo" not in client.get("/plugins.json").json()
        runtime = app.state.artifact_runtime
        revoke_job = next(
            job
            for job in runtime.repository.jobs.values()
            if job["type"] == "revoke" and job["status"] == "queued"
        )
        revoke_job["max_attempts"] = 1
        original_revoke = runtime.storage.revoke_published

        async def fail_revoke(_: str) -> None:
            raise RuntimeError("CDN origin unavailable")

        runtime.storage.revoke_published = fail_revoke
        asyncio.run(runtime.job_runner.run_once())
        failed_revoke = client.get(f"/v1/artifacts/{artifact_id}", headers=owner_headers).json()
        assert failed_revoke["artifact"]["publication_status"] == "revoke_failed"
        assert "astrbot_plugin_demo" not in client.get("/plugins.json").json()

        runtime.storage.revoke_published = original_revoke
        retry_revoke = client.post(
            f"/v1/admin/plugins/{plugin['id']}/revoke-release",
            headers={"x-dev-github-login": "reviewer", "idempotency-key": "revoke-retry"},
            json={"reason": "retry confirmed critical risk"},
        )
        assert retry_revoke.status_code == 200
        asyncio.run(runtime.job_runner.run_once())
        revoked = client.get(f"/v1/artifacts/{artifact_id}", headers=owner_headers).json()
        assert revoked["artifact"]["publication_status"] == "revoked"
        assert any(item["action"] == "revoke" for item in revoked["decisions"])
        assert "astrbot_plugin_demo" not in client.get("/plugins.json").json()


def test_admin_can_idempotently_request_artifact_changes(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "ARTIFACTS_ENABLED": "true",
            "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
            "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            "ARTIFACT_SUBMISSION_RPM": "0",
            "DATABASE_URL": "postgresql://example.invalid/market",
            "REDIS_URL": "redis://example.invalid/0",
            "GITHUB_METADATA_SYNC_ENABLED": "false",
        }
    )
    store = InMemoryMarketStore()
    app = create_app(settings=settings, store=store)
    owner_headers = {"x-dev-github-login": "alice"}

    with TestClient(app) as client:
        plugin = client.post(
            "/v1/plugins/registrations",
            headers=owner_headers,
            json={
                "name": "astrbot_plugin_changes",
                "display_name": "Changes",
                "desc": "Request changes fixture",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_changes",
            },
        ).json()["plugin"]
        submitted = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            files={"file": ("plugin.zip", plugin_zip(), "application/zip")},
        )
        artifact_id = submitted.json()["artifact"]["id"]
        repository = app.state.artifact_runtime.repository
        asyncio.run(repository.transition_review_status(artifact_id, "prechecking"))
        asyncio.run(repository.transition_review_status(artifact_id, "scanning"))

        owner_forbidden = client.post(
            f"/v1/admin/artifacts/{artifact_id}/request-changes",
            headers=owner_headers,
            json={"reason": "owner cannot moderate"},
        )
        assert owner_forbidden.status_code == 403

        reviewer = store.upsert_github_user(
            {"id": "reviewer-1", "login": "reviewer", "name": "Reviewer"}
        )
        store.update_user_role(reviewer["id"], "admin")
        headers = {
            "x-dev-github-login": "reviewer",
            "idempotency-key": "request-changes-once",
        }
        invalid_state = client.post(
            f"/v1/admin/artifacts/{artifact_id}/request-changes",
            headers={
                "x-dev-github-login": "reviewer",
                "idempotency-key": "request-changes-too-early",
            },
            json={"reason": "wait for automated review"},
        )
        assert invalid_state.status_code == 409
        asyncio.run(repository.transition_review_status(artifact_id, "pending_review"))
        missing_reason = client.post(
            f"/v1/admin/artifacts/{artifact_id}/request-changes",
            headers=headers,
            json={"reason": ""},
        )
        assert missing_reason.status_code == 400
        assert missing_reason.json()["code"] == "reason_required"

        for _ in range(2):
            response = client.post(
                f"/v1/admin/artifacts/{artifact_id}/request-changes",
                headers=headers,
                json={"reason": "请移除未说明的命令执行"},
            )
            assert response.status_code == 200
            assert response.json()["artifact"]["review_status"] == "changes_requested"

        detail = client.get(f"/v1/artifacts/{artifact_id}", headers=owner_headers).json()
        assert [item["action"] for item in detail["decisions"]] == ["request_changes"]
        assert detail["artifact"]["download_url"] is None
        events = [
            event
            for event in repository.outbox.values()
            if event["event_type"] == "artifact_changes_requested"
        ]
        assert len(events) == 1
