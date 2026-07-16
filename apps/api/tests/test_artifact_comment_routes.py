from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.artifacts.diff import ArtifactDiffService, manifest_tree_sha256
from app.artifacts.storage import build_content_key
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


def test_artifact_comment_routes_enforce_roles_locking_and_private_projection(
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
    owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        owner,
        {
            "name": "astrbot_plugin_comment_routes",
            "display_name": "Comment Routes",
            "desc": "Comment route fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_comment_routes",
            "tags": [],
        },
    )
    reviewer = store.upsert_github_user(
        {"id": "reviewer-1", "login": "reviewer", "name": "Reviewer"}
    )
    store.update_user_role(reviewer["id"], "admin")
    app = create_app(settings=settings, store=store)

    with TestClient(app) as client:
        repository = app.state.artifact_runtime.repository
        storage = app.state.artifact_runtime.storage

        async def seed() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            base = await repository.create_artifact(_artifact_payload(plugin, owner, "route-base"))
            current = await repository.create_artifact(
                {
                    **_artifact_payload(plugin, owner, "route-current"),
                    "base_artifact_id": base["id"],
                }
            )
            await _seed_manifest(
                repository,
                storage,
                base,
                {"main.py": (b"VALUE = 1\n", True)},
            )
            current = await _seed_manifest(
                repository,
                storage,
                current,
                {"main.py": (b"VALUE = 2\n", True)},
            )
            await ArtifactDiffService().build(
                artifact=current,
                repository=repository,
                storage=storage,
            )
            file = (await repository.list_artifact_files(current["id"]))[0]
            diff = (await repository.list_artifact_diffs(current["id"]))[0]
            document = await app.state.artifact_runtime.service.artifact_diff_content(
                current,
                diff["id"],
                hunk_id=None,
            )
            return current, file, {"diff": diff, "hunk": document["hunks"][0]}

        current, file, diff_data = asyncio.run(seed())
        owner_headers = {"x-dev-github-login": "alice"}
        admin_headers = {"x-dev-github-login": "reviewer"}
        other_headers = {"x-dev-github-login": "mallory"}
        create_payload = {
            "file_id": file["id"],
            "side": "current",
            "line_start": 1,
            "line_end": 1,
            "body": "<script>alert('stored as text')</script>",
            "diff_id": diff_data["diff"]["id"],
            "hunk_id": diff_data["hunk"]["id"],
        }

        assert client.get(f"/v1/artifacts/{current['id']}/comments").status_code == 401
        assert (
            client.post(
                f"/v1/admin/artifacts/{current['id']}/comments",
                headers={**owner_headers, "idempotency-key": "owner-create"},
                json=create_payload,
            ).status_code
            == 403
        )
        created = client.post(
            f"/v1/admin/artifacts/{current['id']}/comments",
            headers={**admin_headers, "idempotency-key": "route-comment-create"},
            json=create_payload,
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store, private"
        thread = created.json()["comment"]
        assert thread["body"] == create_payload["body"]
        _assert_private_keys_absent(thread)

        assert (
            client.get(
                f"/v1/artifacts/{current['id']}/comments",
                headers=other_headers,
            ).status_code
            == 403
        )
        listed = client.get(
            f"/v1/artifacts/{current['id']}/comments",
            headers=owner_headers,
        )
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store, private"
        assert listed.json()["total"] == 1
        _assert_private_keys_absent(listed.json())

        reply = client.post(
            f"/v1/artifacts/{current['id']}/comments/{thread['id']}/replies",
            headers={**owner_headers, "idempotency-key": "route-comment-reply"},
            json={"expected_version": 1, "body": "Addressed in the next version."},
        )
        assert reply.status_code == 200
        assert reply.json()["comment"]["version"] == 2
        addressed = client.post(
            f"/v1/artifacts/{current['id']}/comments/{thread['id']}/author-addressed",
            headers={**owner_headers, "idempotency-key": "route-comment-addressed"},
            json={"expected_version": 2, "body": ""},
        )
        assert addressed.status_code == 200
        assert addressed.json()["comment"]["version"] == 3

        owner_resolve = client.post(
            f"/v1/admin/artifacts/{current['id']}/comments/{thread['id']}/resolve",
            headers={**owner_headers, "idempotency-key": "owner-resolve"},
            json={"expected_version": 3},
        )
        assert owner_resolve.status_code == 403
        edited = client.post(
            f"/v1/admin/artifacts/{current['id']}/comments/{thread['id']}/edit",
            headers={**admin_headers, "idempotency-key": "route-comment-edit"},
            json={"expected_version": 3, "body": "Updated root note"},
        )
        assert edited.status_code == 200
        resolved = client.post(
            f"/v1/admin/artifacts/{current['id']}/comments/{thread['id']}/resolve",
            headers={**admin_headers, "idempotency-key": "route-comment-resolve"},
            json={"expected_version": 4},
        )
        assert resolved.status_code == 200
        assert resolved.json()["comment"]["resolved"] is True
        reopened = client.post(
            f"/v1/admin/artifacts/{current['id']}/comments/{thread['id']}/reopen",
            headers={**admin_headers, "idempotency-key": "route-comment-reopen"},
            json={"expected_version": 5},
        )
        assert reopened.status_code == 200
        assert reopened.json()["comment"]["resolved"] is False

        asyncio.run(repository.transition_review_status(current["id"], "prechecking"))
        asyncio.run(repository.transition_review_status(current["id"], "scanning"))
        asyncio.run(repository.transition_review_status(current["id"], "pending_review"))
        requested = client.post(
            f"/v1/admin/artifacts/{current['id']}/request-changes",
            headers={**admin_headers, "idempotency-key": "route-request-changes"},
            json={"reason": "Please revise"},
        )
        assert requested.status_code == 200
        after_decision = client.post(
            f"/v1/artifacts/{current['id']}/comments/{thread['id']}/replies",
            headers={**owner_headers, "idempotency-key": "route-reply-after-lock"},
            json={"expected_version": 6, "body": "Too late"},
        )
        assert after_decision.status_code == 409
        assert after_decision.json()["code"] == "comment_thread_locked"
        assert after_decision.headers["cache-control"] == "no-store, private"


def _artifact_payload(
    plugin: Mapping[str, Any], user: Mapping[str, Any], marker: str
) -> dict[str, Any]:
    sha256 = hashlib.sha256(marker.encode()).hexdigest()
    return {
        "plugin_id": plugin["id"],
        "version": "1.0.0",
        "normalized_version": "1.0.0",
        "source_type": "upload",
        "source_repo": plugin["repo"],
        "archive_sha256": sha256,
        "size_bytes": 128,
        "quarantine_key": f"quarantine/{sha256[:12]}.zip",
        "submitted_by": user["id"],
    }


async def _seed_manifest(
    repository: Any,
    storage: Any,
    artifact: Mapping[str, Any],
    files: Mapping[str, tuple[bytes, bool]],
) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    for index, (path, (content, is_text)) in enumerate(sorted(files.items())):
        file_id = f"file_{str(artifact['id']).removeprefix('artifact_')}_{index}"
        content_key = build_content_key(str(artifact["id"]), file_id) if is_text else None
        if content_key:
            await storage.put_text_content(content_key, content)
        manifests.append(
            {
                "id": file_id,
                "path": path,
                "language": "python",
                "mime_type": "text/x-python",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "line_count": len(content.decode().splitlines()) if is_text else None,
                "is_text": is_text,
                "content_key": content_key,
            }
        )
    await repository.replace_artifact_files(
        str(artifact["id"]), manifests, manifest_tree_sha256(manifests)
    )
    updated = await repository.get_artifact(str(artifact["id"]))
    assert updated is not None
    return updated


def _assert_private_keys_absent(value: Any) -> None:
    forbidden = {
        "reviewer_user_id",
        "actor_user_id",
        "idempotency_key",
        "content_key",
        "hunks_key",
        "quarantine_key",
    }
    if isinstance(value, Mapping):
        assert forbidden.isdisjoint(value)
        for item in value.values():
            _assert_private_keys_absent(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_private_keys_absent(item)
