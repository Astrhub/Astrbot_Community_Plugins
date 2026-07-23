from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.artifacts.comments import ReviewCommentError, ReviewCommentService
from app.artifacts.content import ArtifactContentService
from app.artifacts.diff import ArtifactDiffService, manifest_tree_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.store import InMemoryMarketStore


def test_comment_domain_enforces_anchor_roles_events_and_plain_text(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, Any], list[str]]:
        service, repository, base, current, files, diff, hunk = await _comment_fixture(tmp_path)
        admin = _admin("reviewer-1", "Reviewer")
        other_admin = _admin("reviewer-2", "Other Reviewer")
        author = _author()

        thread = await service.create(
            artifact=current,
            actor=admin,
            file_id=files["main.py"]["id"],
            side="current",
            line_start=2,
            line_end=2,
            body="  Please explain this line.\r\n  ",
            diff_id=diff["id"],
            hunk_id=hunk["id"],
            source_thread_id=None,
            idempotency_key="comment-create-1",
        )
        assert thread["body"] == "Please explain this line."
        assert thread["file_path"] == "main.py"
        assert thread["file_sha256"] == files["main.py"]["sha256"]
        assert thread["events"][0]["type"] == "create"

        replied = await service.mutate(
            artifact=current,
            thread_id=thread["id"],
            actor=author,
            event_type="reply",
            expected_version=1,
            body="Fixed in the next revision.",
            idempotency_key="comment-create-1:create",
        )
        addressed = await service.mutate(
            artifact=current,
            thread_id=thread["id"],
            actor=author,
            event_type="author_addressed",
            expected_version=2,
            body="",
            idempotency_key="comment-addressed-1",
        )
        resolved = await service.mutate(
            artifact=current,
            thread_id=thread["id"],
            actor=admin,
            event_type="resolve",
            expected_version=3,
            body="",
            idempotency_key="comment-resolve-1",
        )
        reopened = await service.mutate(
            artifact=current,
            thread_id=thread["id"],
            actor=admin,
            event_type="reopen",
            expected_version=4,
            body="",
            idempotency_key="comment-reopen-1",
        )
        edited = await service.mutate(
            artifact=current,
            thread_id=thread["id"],
            actor=admin,
            event_type="edit",
            expected_version=5,
            body="Updated\r\nreview note",
            idempotency_key="comment-edit-1",
        )

        assert replied["version"] == 2
        assert addressed["resolved"] is False
        assert resolved["resolved"] is True
        assert reopened["resolved"] is False
        assert edited["body"] == "Updated\nreview note"
        assert [event["type"] for event in edited["events"]] == [
            "create",
            "reply",
            "author_addressed",
            "resolve",
            "reopen",
            "edit",
        ]

        errors: list[str] = []
        forbidden_calls = (
            service.create(
                artifact=current,
                actor=author,
                file_id=files["main.py"]["id"],
                side="current",
                line_start=1,
                line_end=1,
                body="author cannot create",
                diff_id=None,
                hunk_id=None,
                source_thread_id=None,
                idempotency_key="forbidden-create",
            ),
            service.mutate(
                artifact=current,
                thread_id=thread["id"],
                actor=other_admin,
                event_type="edit",
                expected_version=6,
                body="cannot edit another reviewer's root comment",
                idempotency_key="forbidden-edit",
            ),
            service.mutate(
                artifact=current,
                thread_id=thread["id"],
                actor=author,
                event_type="resolve",
                expected_version=6,
                body="",
                idempotency_key="forbidden-resolve",
            ),
            service.mutate(
                artifact=current,
                thread_id=thread["id"],
                actor=admin,
                event_type="author_addressed",
                expected_version=6,
                body="",
                idempotency_key="forbidden-addressed",
            ),
        )
        for call in forbidden_calls:
            with pytest.raises(ReviewCommentError) as caught:
                await call
            errors.append(caught.value.code)

        private = {
            "reviewer_user_id",
            "actor_user_id",
            "idempotency_key",
            "content_key",
            "hunks_key",
        }
        _assert_keys_absent(edited, private)
        return edited, errors

    thread, errors = asyncio.run(scenario())
    assert thread["version"] == 6
    assert errors == ["comment_action_forbidden"] * 4


def test_comment_domain_rejects_invalid_file_side_line_hunk_and_body(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        service, _, base, current, files, diff, hunk = await _comment_fixture(tmp_path)
        admin = _admin("reviewer-1", "Reviewer")
        base_files = {
            item["path"]: item
            for item in await service.repository.list_artifact_files(str(base["id"]))
        }
        cases = (
            {
                "file_id": files["main.py"]["id"],
                "side": "current",
                "line_start": 3,
                "line_end": 3,
                "body": "past end",
            },
            {
                "file_id": files["asset.bin"]["id"],
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "binary",
            },
            {
                "file_id": base_files["main.py"]["id"],
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "wrong side",
            },
            {
                "file_id": files["main.py"]["id"],
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "wrong hunk",
                "diff_id": diff["id"],
                "hunk_id": "hunk-999",
            },
            {
                "file_id": files["main.py"]["id"],
                "side": "base",
                "line_start": 2,
                "line_end": 2,
                "body": "side mismatch",
                "diff_id": diff["id"],
                "hunk_id": hunk["id"],
            },
            {
                "file_id": files["main.py"]["id"],
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "bad\x00body",
            },
        )
        errors: list[str] = []
        for index, values in enumerate(cases):
            payload = {
                "diff_id": None,
                "hunk_id": None,
                "source_thread_id": None,
                **values,
            }
            with pytest.raises(ReviewCommentError) as caught:
                await service.create(
                    artifact=current,
                    actor=admin,
                    idempotency_key=f"invalid-comment-{index}",
                    **payload,
                )
            errors.append(caught.value.code)
        return errors

    assert asyncio.run(scenario()) == [
        "comment_line_invalid",
        "comment_line_invalid",
        "comment_line_invalid",
        "artifact_diff_hunk_not_found",
        "comment_line_invalid",
        "comment_body_invalid",
    ]


def test_comment_domain_is_idempotent_concurrent_and_locked_after_decision(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, list[str], str, bool]:
        service, repository, _, current, files, _, _ = await _comment_fixture(tmp_path)
        admin = _admin("reviewer-1", "Reviewer")
        author = _author()
        create = {
            "artifact": current,
            "actor": admin,
            "file_id": files["main.py"]["id"],
            "side": "current",
            "line_start": 1,
            "line_end": 1,
            "body": "Review this",
            "diff_id": None,
            "hunk_id": None,
            "source_thread_id": None,
            "idempotency_key": "comment-idempotent-create",
        }
        first = await service.create(**create)
        repeated = await service.create(**create)
        assert first["id"] == repeated["id"]
        with pytest.raises(ReviewCommentError) as create_conflict:
            await service.create(**{**create, "body": "Different body"})

        results = await asyncio.gather(
            service.mutate(
                artifact=current,
                thread_id=first["id"],
                actor=author,
                event_type="reply",
                expected_version=1,
                body="Reply A",
                idempotency_key="concurrent-reply-a",
            ),
            service.mutate(
                artifact=current,
                thread_id=first["id"],
                actor=author,
                event_type="reply",
                expected_version=1,
                body="Reply B",
                idempotency_key="concurrent-reply-b",
            ),
            return_exceptions=True,
        )
        errors = [item.code for item in results if isinstance(item, ReviewCommentError)]
        versions = [item["version"] for item in results if isinstance(item, dict)]
        assert versions == [2]

        await repository.transition_review_status(current["id"], "prechecking")
        await repository.transition_review_status(current["id"], "scanning")
        await repository.transition_review_status(current["id"], "pending_review")
        await repository.decide_artifact(
            current["id"],
            action="request_changes",
            target_status="changes_requested",
            reason="Please revise",
            reviewer=admin,
            idempotency_key="comment-lock-decision",
        )
        latest = await repository.get_review_comment(current["id"], first["id"])
        assert latest is not None
        replay_after_decision = await service.create(**create)
        assert replay_after_decision["id"] == first["id"]
        assert replay_after_decision["locked_at"] is not None
        with pytest.raises(ReviewCommentError) as locked:
            await service.mutate(
                artifact=current,
                thread_id=first["id"],
                actor=author,
                event_type="reply",
                expected_version=2,
                body="Too late",
                idempotency_key="reply-after-decision",
            )
        return (
            await repository.count_review_comments(current["id"]),
            errors,
            create_conflict.value.code,
            bool(latest["locked_at"]) and locked.value.code == "comment_thread_locked",
        )

    count, errors, conflict, locked = asyncio.run(scenario())
    assert count == 1
    assert errors == ["comment_version_conflict"]
    assert conflict == "idempotency_key_conflict"
    assert locked is True


def test_comment_source_thread_requires_supersedes_lineage(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        service, repository, base, current, files, _, _ = await _comment_fixture(tmp_path)
        admin = _admin("reviewer-1", "Reviewer")
        source = await service.create(
            artifact=current,
            actor=admin,
            file_id=files["main.py"]["id"],
            side="current",
            line_start=1,
            line_end=1,
            body="Original request",
            diff_id=None,
            hunk_id=None,
            source_thread_id=None,
            idempotency_key="source-thread-create",
        )
        await repository.transition_review_status(current["id"], "prechecking")
        await repository.transition_review_status(current["id"], "scanning")
        await repository.transition_review_status(current["id"], "pending_review")
        await repository.decide_artifact(
            current["id"],
            action="request_changes",
            target_status="changes_requested",
            reason="Revise",
            reviewer=admin,
            idempotency_key="source-thread-decision",
        )

        plugin = repository.store.get_plugin(str(current["plugin_id"]))
        assert plugin is not None
        new = await repository.create_artifact(
            {
                **_artifact_payload(plugin, _author(), "resubmission"),
                "base_artifact_id": base["id"],
                "supersedes_artifact_id": current["id"],
            }
        )
        new = await _seed_manifest(
            repository,
            service.content.storage,
            new,
            {"main.py": (b"first\nfixed\n", True)},
        )
        new_file = (await repository.list_artifact_files(new["id"]))[0]
        linked = await service.create(
            artifact=new,
            actor=admin,
            file_id=new_file["id"],
            side="current",
            line_start=2,
            line_end=2,
            body="Follow-up",
            diff_id=None,
            hunk_id=None,
            source_thread_id=source["id"],
            idempotency_key="linked-thread-create",
        )
        base_main = next(
            item
            for item in await repository.list_artifact_files(base["id"])
            if item["path"] == "main.py"
        )
        with pytest.raises(ReviewCommentError) as invalid:
            await service.create(
                artifact=base,
                actor=admin,
                file_id=base_main["id"],
                side="current",
                line_start=1,
                line_end=1,
                body="Invalid source",
                diff_id=None,
                hunk_id=None,
                source_thread_id=source["id"],
                idempotency_key="invalid-source-thread",
            )
        return str(linked["source_thread_id"]), invalid.value.code

    source_id, error = asyncio.run(scenario())
    assert source_id
    assert error == "comment_source_invalid"


def test_comment_projection_bounds_events_and_reports_truncation(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, bool, int]:
        service, _, _, current, files, _, _ = await _comment_fixture(tmp_path)
        thread = await service.create(
            artifact=current,
            actor=_admin("reviewer-1", "Reviewer"),
            file_id=files["main.py"]["id"],
            side="current",
            line_start=1,
            line_end=1,
            body="Bound this thread",
            diff_id=None,
            hunk_id=None,
            source_thread_id=None,
            idempotency_key="bounded-thread-create",
        )
        for index in range(12):
            thread = await service.mutate(
                artifact=current,
                thread_id=thread["id"],
                actor=_author(),
                event_type="reply",
                expected_version=index + 1,
                body=f"Reply {index}",
                idempotency_key=f"bounded-thread-reply-{index}",
            )
        page = await service.list(current, limit=1, offset=0)
        selected = page["items"][0]
        return (
            len(thread["events"]),
            thread["event_count"],
            thread["events_truncated"],
            len(selected["events"]),
        )

    event_page, total, truncated, list_page = asyncio.run(scenario())
    assert event_page == 10
    assert total == 13
    assert truncated is True
    assert list_page == 10


async def _comment_fixture(
    tmp_path: Path,
) -> tuple[
    ReviewCommentService,
    InMemoryArtifactRepository,
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    store = InMemoryMarketStore()
    author = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        author,
        {
            "name": "astrbot_plugin_comments",
            "display_name": "Comments",
            "desc": "Comment fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_comments",
            "tags": [],
        },
    )
    repository = InMemoryArtifactRepository(store)
    storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
    base = await repository.create_artifact(_artifact_payload(plugin, author, "base"))
    current = await repository.create_artifact(
        {**_artifact_payload(plugin, author, "current"), "base_artifact_id": base["id"]}
    )
    base = await _seed_manifest(
        repository,
        storage,
        base,
        {"main.py": (b"first\nold\n", True), "asset.bin": (b"\x00", False)},
    )
    current = await _seed_manifest(
        repository,
        storage,
        current,
        {"main.py": (b"first\nnew\n", True), "asset.bin": (b"\x00", False)},
    )
    await ArtifactDiffService().build(artifact=current, repository=repository, storage=storage)
    files = {
        item["path"]: item for item in await repository.list_artifact_files(str(current["id"]))
    }
    diff = next(
        item
        for item in await repository.list_artifact_diffs(str(current["id"]))
        if item["path"] == "main.py"
    )
    content = ArtifactContentService(repository, storage)
    hunk = (await content.read_diff(current, diff["id"]))["hunks"][0]
    return ReviewCommentService(repository, content), repository, base, current, files, diff, hunk


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
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
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
                "language": "python" if path.endswith(".py") else "binary",
                "mime_type": "text/x-python" if is_text else "application/octet-stream",
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


def _admin(user_id: str, nickname: str) -> dict[str, Any]:
    return {"id": user_id, "role": "admin", "nickname": nickname}


def _author() -> dict[str, Any]:
    return {"id": "100", "role": "user", "nickname": "Alice", "github_login": "alice"}


def _assert_keys_absent(value: Any, forbidden: set[str]) -> None:
    if isinstance(value, Mapping):
        assert forbidden.isdisjoint(value)
        for item in value.values():
            _assert_keys_absent(item, forbidden)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_keys_absent(item, forbidden)
