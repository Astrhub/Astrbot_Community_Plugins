from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from app.artifacts.github_source import ResolvedGithubSource
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore
from test_artifact_pipeline import plugin_zip


def test_upload_and_github_resubmission_create_new_artifacts_and_full_precheck(
    tmp_path: Path,
) -> None:
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
                "name": "astrbot_plugin_resubmit",
                "display_name": "Resubmit",
                "desc": "Resubmission fixture",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_resubmit",
                "tags": [],
            },
        ).json()["plugin"]
        original_zip = plugin_zip(main_source="VALUE = 1\n")
        submitted = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            files={"file": ("plugin.zip", original_zip, "application/zip")},
        )
        assert submitted.status_code == 202
        original = submitted.json()["artifact"]
        repository = app.state.artifact_runtime.repository
        asyncio.run(repository.transition_review_status(original["id"], "prechecking"))
        asyncio.run(repository.transition_review_status(original["id"], "scanning"))
        asyncio.run(repository.transition_review_status(original["id"], "pending_review"))

        reviewer = store.upsert_github_user(
            {"id": "reviewer-1", "login": "reviewer", "name": "Reviewer"}
        )
        store.update_user_role(reviewer["id"], "admin")
        requested = client.post(
            f"/v1/admin/artifacts/{original['id']}/request-changes",
            headers={
                "x-dev-github-login": "reviewer",
                "idempotency-key": "resubmit-request-changes",
            },
            json={"reason": "Please revise"},
        )
        assert requested.status_code == 200

        duplicate = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            data={"supersedes_artifact_id": original["id"]},
            files={"file": ("plugin.zip", original_zip, "application/zip")},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "resubmission_content_unchanged"

        upload = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            data={"supersedes_artifact_id": original["id"]},
            files={
                "file": (
                    "plugin.zip",
                    plugin_zip(main_source="VALUE = 2\n"),
                    "application/zip",
                )
            },
        )
        assert upload.status_code == 202
        upload_artifact = upload.json()["artifact"]
        assert upload_artifact["id"] != original["id"]
        assert upload_artifact["supersedes_artifact_id"] == original["id"]
        assert upload_artifact["base_artifact_id"] == plugin.get("current_artifact_id")
        repeated_upload = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            data={"supersedes_artifact_id": original["id"]},
            files={
                "file": (
                    "plugin.zip",
                    plugin_zip(main_source="VALUE = 2\n"),
                    "application/zip",
                )
            },
        )
        assert repeated_upload.status_code == 202
        assert repeated_upload.json()["artifact"]["id"] == upload_artifact["id"]

        github_zip = plugin_zip(main_source="VALUE = 3\n")
        github = app.state.artifact_runtime.service.github

        async def resolve(_: str, requested_ref: str = "") -> ResolvedGithubSource:
            return ResolvedGithubSource(
                repo_url=plugin["repo"],
                owner="alice",
                repo_name="astrbot_plugin_resubmit",
                requested_ref=requested_ref or "main",
                commit_sha="a" * 40,
            )

        async def stream_archive(_: ResolvedGithubSource) -> AsyncIterator[bytes]:
            yield github_zip

        github.resolve = resolve  # type: ignore[method-assign]
        github.stream_archive = stream_archive  # type: ignore[method-assign]
        github_response = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/github",
            headers=owner_headers,
            json={
                "source_ref": "main",
                "supersedes_artifact_id": original["id"],
            },
        )
        assert github_response.status_code == 202
        github_artifact = github_response.json()["artifact"]
        assert github_artifact["id"] not in {original["id"], upload_artifact["id"]}
        assert github_artifact["supersedes_artifact_id"] == original["id"]

        new_ids = {upload_artifact["id"], github_artifact["id"]}
        precheck_ids = {
            job["artifact_id"] for job in repository.jobs.values() if job["type"] == "precheck"
        }
        assert new_ids <= precheck_ids
        stored_original = asyncio.run(repository.get_artifact(original["id"]))
        assert stored_original is not None
        assert stored_original["review_status"] == "changes_requested"
        assert stored_original["archive_sha256"] == original["archive_sha256"]

        invalid = client.post(
            f"/v1/plugins/{plugin['id']}/artifacts/upload",
            headers=owner_headers,
            data={"supersedes_artifact_id": upload_artifact["id"]},
            files={
                "file": (
                    "plugin.zip",
                    plugin_zip(main_source="VALUE = 4\n"),
                    "application/zip",
                )
            },
        )
        assert invalid.status_code == 409
        assert invalid.json()["code"] == "superseded_artifact_invalid"
