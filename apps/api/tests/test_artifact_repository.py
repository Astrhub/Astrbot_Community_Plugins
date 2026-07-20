from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.artifacts.models import ArtifactStateError, PublicationStatus, ReviewStatus
from app.artifacts.repository import InMemoryArtifactRepository
from app.store import InMemoryMarketStore


def make_repository() -> tuple[InMemoryArtifactRepository, dict, dict]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        user,
        {
            "name": "astrbot_plugin_demo",
            "display_name": "Demo",
            "desc": "Demo plugin",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_demo",
            "tags": [],
        },
    )
    plugin = store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
    return InMemoryArtifactRepository(store), user, plugin


def artifact_payload(plugin: dict, user: dict, digest: str = "a") -> dict:
    return {
        "plugin_id": plugin["id"],
        "version": "v1.0.0",
        "normalized_version": "1.0.0",
        "source_type": "upload",
        "source_repo": plugin["repo"],
        "archive_sha256": digest * 64,
        "size_bytes": 128,
        "quarantine_key": f"quarantine/{digest * 8}.zip",
        "submitted_by": user["id"],
        "submitted_by_snapshot": {"github_login": user["github_login"]},
    }


async def move_to_pending_review(repo: InMemoryArtifactRepository, artifact_id: str) -> None:
    await repo.transition_review_status(artifact_id, ReviewStatus.PRECHECKING.value)
    await repo.transition_review_status(artifact_id, ReviewStatus.SCANNING.value)
    await repo.transition_review_status(artifact_id, ReviewStatus.PENDING_REVIEW.value)


def test_create_artifact_is_idempotent_by_plugin_and_sha256() -> None:
    repo, user, plugin = make_repository()
    payload = artifact_payload(plugin, user)

    first = asyncio.run(repo.create_artifact(payload))
    second = asyncio.run(repo.create_artifact(payload))

    assert first["id"] == second["id"]
    assert first["path_suffix"] == second["path_suffix"]
    assert len(repo.artifacts) == 1


def test_concurrent_decisions_create_only_one_terminal_decision() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> list[object]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user))
        await move_to_pending_review(repo, artifact["id"])
        return await asyncio.gather(
            repo.decide_artifact(
                artifact["id"],
                action="approve",
                target_status="approved",
                reason="pass",
                reviewer={"id": "admin-1", "internal_username": "admin1"},
                idempotency_key="decision-1",
            ),
            repo.decide_artifact(
                artifact["id"],
                action="approve",
                target_status="approved",
                reason="pass",
                reviewer={"id": "admin-2", "internal_username": "admin2"},
                idempotency_key="decision-2",
            ),
            return_exceptions=True,
        )

    results = asyncio.run(scenario())

    assert sum(isinstance(item, dict) for item in results) == 1
    errors = [item for item in results if isinstance(item, ArtifactStateError)]
    assert len(errors) == 1
    assert errors[0].code == "artifact_already_decided"
    assert len(repo.decisions) == 1


def test_same_decision_idempotency_key_returns_existing_result() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[dict | None, dict | None]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user))
        await move_to_pending_review(repo, artifact["id"])
        kwargs = {
            "action": "reject",
            "target_status": "rejected",
            "reason": "unsafe",
            "reviewer": {"id": "admin", "internal_username": "admin"},
            "idempotency_key": "same-decision",
        }
        return (
            await repo.decide_artifact(artifact["id"], **kwargs),
            await repo.decide_artifact(artifact["id"], **kwargs),
        )

    first, second = asyncio.run(scenario())

    assert first and second
    assert first["id"] == second["id"]
    assert len(repo.decisions) == 1


def test_workers_claim_distinct_jobs_and_reclaim_expired_lease() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[list[dict], list[dict], list[dict]]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user))
        for suffix in ("one", "two"):
            await repo.enqueue_job(
                {
                    "artifact_id": artifact["id"],
                    "type": "precheck",
                    "idempotency_key": f"precheck-{suffix}",
                }
            )
        first, second = await asyncio.gather(
            repo.claim_jobs("worker-a", 1, 60),
            repo.claim_jobs("worker-b", 1, 60),
        )
        expired_job = first[0]
        repo.jobs[expired_job["id"]]["lease_expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        reclaimed = await repo.claim_jobs("worker-c", 1, 60)
        return first, second, reclaimed

    first, second, reclaimed = asyncio.run(scenario())

    assert first[0]["id"] != second[0]["id"]
    assert reclaimed[0]["id"] == first[0]["id"]
    assert reclaimed[0]["lease_owner"] == "worker-c"
    assert reclaimed[0]["attempts"] == 2


def test_outbox_deduplicates_events() -> None:
    repo, _, _ = make_repository()
    payload = {
        "event_type": "artifact_pending_review",
        "aggregate_type": "artifact",
        "aggregate_id": "artifact-1",
        "dedupe_key": "artifact-1:pending",
    }

    first = asyncio.run(repo.enqueue_outbox(payload))
    second = asyncio.run(repo.enqueue_outbox(payload))

    assert first["id"] == second["id"]
    assert len(repo.outbox) == 1


def test_site_notifications_are_idempotent_by_outbox_event() -> None:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    metadata = {"outbox_event_id": "outbox-1"}

    first = store.create_notification_once(
        user["id"], "title", "body", "plugin_artifact", metadata, "outbox-1"
    )
    second = store.create_notification_once(
        user["id"], "title", "body", "plugin_artifact", metadata, "outbox-1"
    )

    assert first["id"] == second["id"]
    assert len(store.list_notifications(user["id"])) == 1


def test_only_one_artifact_can_publish_the_same_plugin_version() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> None:
        first = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        second = await repo.create_artifact(artifact_payload(plugin, user, "b"))
        await repo.transition_publication_status(first["id"], PublicationStatus.PUBLISHING.value)
        await repo.publish_artifact(
            first["id"],
            expected_repo_version="v1.0.0",
            published_key="100/demo/v1/first.zip",
            download_url="https://cdn.example.com/first.zip",
        )
        await repo.transition_publication_status(second["id"], PublicationStatus.PUBLISHING.value)
        with pytest.raises(ValueError, match="published_version_conflict"):
            await repo.publish_artifact(
                second["id"],
                expected_repo_version="v1.0.0",
                published_key="100/demo/v1/second.zip",
                download_url="https://cdn.example.com/second.zip",
            )

    asyncio.run(scenario())

    current_plugin = repo.store.get_plugin(plugin["id"])
    assert current_plugin["current_artifact_id"] in repo.artifacts


def test_revoke_request_atomically_hides_release_and_supports_retry() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[dict, dict, dict]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user))
        await repo.transition_publication_status(artifact["id"], PublicationStatus.PUBLISHING.value)
        await repo.publish_artifact(
            artifact["id"],
            expected_repo_version="v1.0.0",
            published_key="100/demo/v1/demo.zip",
            download_url="https://cdn.example.com/demo.zip",
        )
        first = await repo.request_revoke_artifact(
            artifact["id"],
            reason="critical finding",
            reviewer={"id": "admin", "internal_username": "admin"},
            idempotency_key="revoke-once",
        )
        repeated = await repo.request_revoke_artifact(
            artifact["id"],
            reason="critical finding",
            reviewer={"id": "admin", "internal_username": "admin"},
            idempotency_key="revoke-once",
        )
        await repo.transition_publication_status(
            artifact["id"], PublicationStatus.REVOKE_FAILED.value
        )
        retried = await repo.request_revoke_artifact(
            artifact["id"],
            reason="retry critical revocation",
            reviewer={"id": "admin", "internal_username": "admin"},
            idempotency_key="revoke-retry",
        )
        return first, repeated, retried

    first, repeated, retried = asyncio.run(scenario())

    assert first["publication_status"] == PublicationStatus.REVOKING.value
    assert repeated["publication_status"] == PublicationStatus.REVOKING.value
    assert retried["publication_status"] == PublicationStatus.REVOKING.value
    assert repo.store.get_plugin(plugin["id"])["status"] == "unlisted"
    assert len([job for job in repo.jobs.values() if job["type"] == "revoke"]) == 2
    assert len([item for item in repo.decisions.values() if item["action"] == "revoke"]) == 2
