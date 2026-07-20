from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.artifacts.history import ReviewHistoryError, ReviewHistoryService
from app.artifacts.repository import InMemoryArtifactRepository
from app.config import load_settings
from app.main import create_app
from app.store import InMemoryMarketStore


def test_history_projection_is_cursor_stable_bounded_and_redacted() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], str, str]:
        repository, user, plugin = _repository_fixture()
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "history"))
        run = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "runtime",
                "status": "succeeded",
                "summary": "runtime complete",
                "raw_result": {"token": "secret", "stdout": "private"},
                "raw_result_key": "private/run.json",
                "worker_id": "worker-private",
                "idempotency_key": "history-run",
            }
        )
        finding = (
            await repository.replace_findings(
                artifact["id"],
                run["id"],
                [
                    {
                        "fingerprint": "history-finding",
                        "severity": "critical",
                        "message": "deterministic issue",
                        "source": "runtime",
                        "deterministic": True,
                    }
                ],
            )
        )[0]
        await repository.update_finding_state(
            finding["id"],
            {
                "expected_version": 1,
                "status": "accepted",
                "actor_user_id": "admin-1",
                "actor_nickname": "Admin",
                "actor_source": "user",
                "reason": "confirmed",
                "metadata": {
                    "quarantine_key": "private/source.zip",
                    "request_fingerprint": "internal-request-fingerprint",
                    "safe": "value",
                },
                "idempotency_key": "history-finding-event",
            },
        )
        thread = await repository.create_review_comment(
            {
                "artifact_id": artifact["id"],
                "file_path": "main.py",
                "file_sha256": "a" * 64,
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "<script>plain text only</script>",
                "reviewer_user_id": "admin-1",
                "reviewer_nickname": "Admin",
                "reviewer_role": "admin",
                "idempotency_key": "history-comment",
            }
        )
        await repository.append_review_comment_event(
            thread["id"],
            {
                "type": "reply",
                "body": "Addressed",
                "actor_user_id": user["id"],
                "actor_nickname": "Alice",
                "actor_role": "author",
                "expected_version": 1,
                "metadata": {"content_key": "private/content.txt"},
                "idempotency_key": "history-comment-reply",
            },
        )
        await repository.record_decision(
            artifact["id"],
            action="request_changes",
            from_status="pending_review",
            to_status="changes_requested",
            reason="Please revise",
            reviewer={"id": "admin-1", "internal_username": "Admin"},
            idempotency_key="history-decision",
            metadata={"prompt": "private", "safe": "decision"},
        )
        repository.artifacts[artifact["id"]]["published_at"] = "2026-01-01T00:00:08Z"
        repository.artifacts[artifact["id"]]["revoked_at"] = "2026-01-01T00:00:09Z"
        _set_history_times(repository, artifact["id"], run["id"], finding["id"], thread["id"])

        service = ReviewHistoryService(repository)
        first = await service.list(artifact, limit=3, cursor="")
        second = await service.list(artifact, limit=3, cursor=first["next_cursor"] or "")
        third = await service.list(artifact, limit=20, cursor=second["next_cursor"] or "")
        items = first["items"] + second["items"] + third["items"]
        serialized = json.dumps(items, ensure_ascii=False)
        assert first["has_more"] is True
        assert len(first["items"]) == 3
        assert len({(item["type"], item["id"]) for item in items}) == len(items)
        assert [(item["occurred_at"], item["type"], item["id"]) for item in items] == sorted(
            (item["occurred_at"], item["type"], item["id"]) for item in items
        )
        return items, serialized, first["next_cursor"] or ""

    items, serialized, cursor = asyncio.run(scenario())
    assert {"run", "finding", "finding_event", "comment_event", "decision"} <= {
        item["type"] for item in items
    }
    assert {"artifact_submitted", "publication_published", "publication_revoked"} <= {
        item["type"] for item in items
    }
    for forbidden in (
        "raw_result",
        "raw_result_key",
        "request_fingerprint",
        "worker-private",
        "quarantine_key",
        "content_key",
        "published_key",
        '"prompt"',
    ):
        assert forbidden not in serialized
    assert cursor


def test_history_rejects_invalid_cursor_and_does_not_repeat_earlier_insert() -> None:
    async def scenario() -> tuple[str, set[tuple[str, str]], set[tuple[str, str]]]:
        repository, user, plugin = _repository_fixture()
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "cursor"))
        for index in range(4):
            run = await repository.create_review_run(
                {
                    "artifact_id": artifact["id"],
                    "type": "static",
                    "status": "succeeded",
                    "idempotency_key": f"cursor-run-{index}",
                }
            )
            repository.runs[run["id"]]["created_at"] = f"2026-01-01T00:00:0{index + 2}Z"
            repository.runs[run["id"]]["completed_at"] = f"2026-01-01T00:00:0{index + 2}Z"
        service = ReviewHistoryService(repository)
        first = await service.list(artifact, limit=2, cursor="")
        first_run_id = next(item["id"] for item in first["items"] if item["type"] == "run")
        repository.runs[first_run_id]["completed_at"] = "2027-01-01T00:00:00Z"
        late_insert = await repository.create_review_run(
            {
                "artifact_id": artifact["id"],
                "type": "static",
                "status": "succeeded",
                "idempotency_key": "cursor-late-earlier",
            }
        )
        repository.runs[late_insert["id"]]["created_at"] = "2026-01-01T00:00:01.500000Z"
        repository.runs[late_insert["id"]]["completed_at"] = "2026-01-01T00:00:01.500000Z"
        second = await service.list(artifact, limit=20, cursor=first["next_cursor"] or "")
        with pytest.raises(ReviewHistoryError) as invalid:
            await service.list(artifact, limit=20, cursor="not-a-valid-cursor")
        return (
            invalid.value.code,
            {(item["type"], item["id"]) for item in first["items"]},
            {(item["type"], item["id"]) for item in second["items"]},
        )

    code, first_ids, second_ids = asyncio.run(scenario())
    assert code == "history_cursor_invalid"
    assert first_ids.isdisjoint(second_ids)


def test_history_projects_publication_failures_from_immutable_outbox_events() -> None:
    async def scenario() -> tuple[tuple[str, str], tuple[str, str], int]:
        repository, user, plugin = _repository_fixture()
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "failure"))
        event = await repository.enqueue_outbox(
            {
                "event_type": "artifact_revoke_failed",
                "aggregate_type": "artifact",
                "aggregate_id": artifact["id"],
                "recipient_user_id": user["id"],
                "payload": {"artifact_id": artifact["id"], "code": "origin_unavailable"},
                "dedupe_key": f"artifact:{artifact['id']}:revoke-failed:origin",
            }
        )
        repository.outbox[event["id"]]["created_at"] = "2026-01-01T00:00:01Z"
        service = ReviewHistoryService(repository)
        first_page = await service.list(artifact, limit=20, cursor="")
        repository.artifacts[artifact["id"]]["updated_at"] = "2027-01-01T00:00:00Z"
        second_page = await service.list(artifact, limit=20, cursor="")
        first_failure = next(
            item for item in first_page["items"] if item["type"] == "publication_revoke_failed"
        )
        second_failure = next(
            item for item in second_page["items"] if item["type"] == "publication_revoke_failed"
        )
        return (
            (first_failure["id"], first_failure["occurred_at"]),
            (second_failure["id"], second_failure["occurred_at"]),
            sum(item["type"] == "publication_revoke_failed" for item in second_page["items"]),
        )

    first_key, second_key, count = asyncio.run(scenario())
    assert first_key == second_key
    assert count == 1


def test_history_route_enforces_visibility_and_no_store(tmp_path: Path) -> None:
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
            "name": "astrbot_plugin_history_route",
            "display_name": "History Route",
            "desc": "History route fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_history_route",
            "tags": [],
        },
    )
    app = create_app(settings=settings, store=store)
    with TestClient(app) as client:
        repository = app.state.artifact_runtime.repository
        artifact = asyncio.run(
            repository.create_artifact(_artifact_payload(plugin, owner, "history-route"))
        )
        asyncio.run(
            repository.create_review_run(
                {"artifact_id": artifact["id"], "type": "static", "status": "succeeded"}
            )
        )
        path = f"/v1/artifacts/{artifact['id']}/history"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"x-dev-github-login": "mallory"}).status_code == 403
        response = client.get(path, headers={"x-dev-github-login": "alice"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.json()["items"]


def _repository_fixture() -> tuple[InMemoryArtifactRepository, dict[str, Any], dict[str, Any]]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        user,
        {
            "name": "astrbot_plugin_history",
            "display_name": "History",
            "desc": "History fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_history",
            "tags": [],
        },
    )
    return InMemoryArtifactRepository(store), user, plugin


def _artifact_payload(
    plugin: Mapping[str, Any], user: Mapping[str, Any], marker: str
) -> dict[str, Any]:
    return {
        "plugin_id": plugin["id"],
        "source_type": "upload",
        "source_repo": plugin["repo"],
        "archive_sha256": marker.encode().hex().ljust(64, "0")[:64],
        "size_bytes": 128,
        "quarantine_key": f"quarantine/{marker}.zip",
        "submitted_by": user["id"],
    }


def _set_history_times(
    repository: InMemoryArtifactRepository,
    artifact_id: str,
    run_id: str,
    finding_id: str,
    thread_id: str,
) -> None:
    repository.artifacts[artifact_id]["created_at"] = "2026-01-01T00:00:00Z"
    repository.runs[run_id]["created_at"] = "2026-01-01T00:00:01Z"
    repository.runs[run_id]["completed_at"] = "2026-01-01T00:00:01Z"
    finding = next(
        item
        for findings in repository.findings.values()
        for item in findings
        if item["id"] == finding_id
    )
    finding["created_at"] = "2026-01-01T00:00:02Z"
    finding_event = next(iter(repository.finding_events.values()))
    finding_event["created_at"] = "2026-01-01T00:00:03Z"
    comment_events = sorted(
        (event for event in repository.comment_events.values() if event["thread_id"] == thread_id),
        key=lambda item: item["type"],
    )
    for index, event in enumerate(comment_events, start=4):
        event["created_at"] = f"2026-01-01T00:00:0{index}Z"
    decision = next(iter(repository.decisions.values()))
    decision["created_at"] = "2026-01-01T00:00:07Z"
