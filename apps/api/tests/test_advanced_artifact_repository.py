from __future__ import annotations

import asyncio

import pytest

from app.artifacts.models import ArtifactErrorCode, ReviewStatus
from app.artifacts.repository import InMemoryArtifactRepository
from app.store import InMemoryMarketStore


def make_repository() -> tuple[InMemoryArtifactRepository, dict, dict]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        user,
        {
            "name": "astrbot_plugin_advanced",
            "display_name": "Advanced",
            "desc": "Advanced review fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_advanced",
            "tags": [],
        },
    )
    return InMemoryArtifactRepository(store), user, plugin


def artifact_payload(plugin: dict, user: dict, digest: str) -> dict:
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


def policy_payload(version: str, digest: str = "f") -> dict:
    return {
        "version": version,
        "schema_version": "1",
        "policy": {"required_stages": ["precheck", "static"]},
        "policy_sha256": digest * 64,
        "created_by_nickname": "core",
    }


def test_policy_snapshot_and_stage_run_are_idempotent() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[dict, dict, dict, dict]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        policy = await repo.create_review_policy(policy_payload("policy-1"))
        repeated = await repo.create_review_policy(policy_payload("policy-1"))
        bound = await repo.bind_artifact_policy(artifact["id"], policy["id"])
        await repo.bind_artifact_policy(artifact["id"], policy["id"])
        other = await repo.create_review_policy(policy_payload("policy-2", "e"))
        with pytest.raises(ValueError, match="artifact_policy_snapshot_conflict"):
            await repo.bind_artifact_policy(artifact["id"], other["id"])
        run_payload = {
            "artifact_id": artifact["id"],
            "type": "runtime",
            "status": "queued",
            "policy_version_id": policy["id"],
            "tool_name": "runtime-runner",
            "tool_version": "1.0.0",
            "input_sha256": "1" * 64,
            "idempotency_key": "runtime-stage-once",
        }
        run = await repo.create_review_run(run_payload)
        repeated_run = await repo.create_review_run(run_payload)
        other_artifact = await repo.create_artifact(artifact_payload(plugin, user, "b"))
        with pytest.raises(ValueError, match="idempotency_key_conflict"):
            await repo.create_review_run(
                {
                    **run_payload,
                    "artifact_id": other_artifact["id"],
                }
            )
        completed = await repo.update_artifact_review_coverage(
            artifact["id"],
            {"runtime": "queued"},
            automated_review_completed=True,
        )
        assert repeated["id"] == policy["id"]
        assert bound and bound["policy_version_id"] == policy["id"]
        assert repeated_run["id"] == run["id"]
        return policy, run, completed or {}, artifact

    policy, run, completed, artifact = asyncio.run(scenario())

    assert policy["version"] == "policy-1"
    assert run["policy_version_id"] == policy["id"]
    assert completed["review_coverage"] == {"runtime": "queued"}
    assert completed["automated_review_completed_at"]
    assert len(repo.runs) == 1
    assert repo.artifacts[artifact["id"]]["policy_version_id"] == policy["id"]


def test_diff_and_dependency_graph_are_replaced_as_batches() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[list[dict], list[dict]]:
        base = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        current_payload = artifact_payload(plugin, user, "b")
        current_payload["base_artifact_id"] = base["id"]
        current = await repo.create_artifact(current_payload)
        base_tree = "2" * 64
        current_tree = "3" * 64
        await repo.replace_artifact_files(
            base["id"],
            [
                {
                    "id": "base-main",
                    "path": "main.py",
                    "sha256": "4" * 64,
                    "size_bytes": 10,
                    "is_text": True,
                }
            ],
            base_tree,
        )
        await repo.replace_artifact_files(
            current["id"],
            [
                {
                    "id": "current-main",
                    "path": "main.py",
                    "sha256": "5" * 64,
                    "size_bytes": 12,
                    "is_text": True,
                    "is_entrypoint": True,
                }
            ],
            current_tree,
        )
        await repo.replace_artifact_diffs(
            current["id"],
            base["id"],
            current_tree_sha256=current_tree,
            base_tree_sha256=base_tree,
            diffs=[
                {
                    "base_file_id": "base-main",
                    "current_file_id": "current-main",
                    "path": "main.py",
                    "base_path": "main.py",
                    "change_type": "modified",
                    "base_sha256": "4" * 64,
                    "current_sha256": "5" * 64,
                    "stats": {"added_lines": 1, "deleted_lines": 0},
                }
            ],
        )
        await repo.replace_dependency_edges(
            current["id"],
            tree_sha256=current_tree,
            edges=[
                {
                    "source_file_id": "current-main",
                    "target_name": "json",
                    "edge_type": "import",
                    "confidence": 1,
                    "line_start": 1,
                }
            ],
        )
        with pytest.raises(ValueError, match="diff_tree_changed"):
            await repo.replace_dependency_edges(
                current["id"],
                tree_sha256="0" * 64,
                edges=[],
            )
        return (
            await repo.list_artifact_diffs(current["id"]),
            await repo.list_dependency_edges(current["id"]),
        )

    diffs, edges = asyncio.run(scenario())

    assert diffs[0]["resolved_base_path"] == "main.py"
    assert diffs[0]["resolved_current_path"] == "main.py"
    assert edges[0]["source_path"] == "main.py"
    assert edges[0]["target_name"] == "json"


def test_runtime_dispatch_lease_result_and_collection_are_single_consumer() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[dict, dict, dict | None]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        run = await repo.create_review_run(
            {"artifact_id": artifact["id"], "type": "runtime", "status": "queued"}
        )
        payload = {
            "artifact_id": artifact["id"],
            "run_id": run["id"],
            "request": {"schema_version": "1", "artifact_sha256": "a" * 64},
            "request_sha256": "b" * 64,
        }
        dispatch = await repo.create_runtime_dispatch(payload)
        repeated = await repo.create_runtime_dispatch(payload)
        assert repeated["id"] == dispatch["id"]
        with pytest.raises(ValueError, match="runtime_dispatch_conflict"):
            await repo.create_runtime_dispatch({**payload, "request_sha256": "d" * 64})
        claimed = await repo.claim_runtime_dispatches("runner-a", 1, 60)
        assert await repo.renew_runtime_dispatch_lease(dispatch["id"], "runner-b", 60) is False
        assert await repo.renew_runtime_dispatch_lease(dispatch["id"], "runner-a", 60) is True
        assert (
            await repo.complete_runtime_dispatch(
                dispatch["id"],
                "runner-b",
                {"status": "succeeded", "result_key": "result.json", "result_sha256": "c" * 64},
            )
            is None
        )
        completed = await repo.complete_runtime_dispatch(
            dispatch["id"],
            "runner-a",
            {"status": "succeeded", "result_key": "result.json", "result_sha256": "c" * 64},
        )
        collected = await repo.collect_runtime_dispatch(dispatch["id"])
        repeated_collection = await repo.collect_runtime_dispatch(dispatch["id"])
        assert claimed[0]["attempts"] == 1
        assert completed and completed["status"] == "succeeded"
        return completed or {}, collected or {}, repeated_collection

    completed, collected, repeated_collection = asyncio.run(scenario())

    assert completed["result_sha256"] == "c" * 64
    assert collected["collected_at"]
    assert repeated_collection is None


def test_artifact_graph_projection_is_tree_bound_and_atomic() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[list[dict], list[dict], dict]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user, "g"))
        unrelated_base = await repo.create_artifact(artifact_payload(plugin, user, "gb"))
        tree_sha256 = "7" * 64
        await repo.replace_artifact_files(
            artifact["id"],
            [
                {
                    "id": "graph-main",
                    "path": "main.py",
                    "sha256": "8" * 64,
                    "size_bytes": 10,
                    "line_count": 1,
                    "is_text": True,
                },
                {
                    "id": "graph-helper",
                    "path": "helper.py",
                    "sha256": "9" * 64,
                    "size_bytes": 10,
                    "line_count": 1,
                    "is_text": True,
                },
            ],
            tree_sha256,
        )
        files, edges = await repo.replace_artifact_graph(
            artifact["id"],
            tree_sha256=tree_sha256,
            files=[
                {
                    "file_id": "graph-main",
                    "is_entrypoint": True,
                    "is_reachable": True,
                    "graph_status": "complete",
                    "scan_summary": {"edge_count": 1},
                },
                {
                    "file_id": "graph-helper",
                    "is_entrypoint": False,
                    "is_reachable": True,
                    "graph_status": "complete",
                    "scan_summary": {"edge_count": 0},
                },
            ],
            edges=[
                {
                    "id": "edge-main-helper",
                    "source_file_id": "graph-main",
                    "target_file_id": "graph-helper",
                    "target_name": "helper",
                    "edge_type": "import",
                    "confidence": 1,
                    "line_start": 1,
                }
            ],
            coverage={"complete": True, "output_sha256": "a" * 64},
        )
        with pytest.raises(ValueError, match="import_graph_incomplete"):
            await repo.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {
                        "file_id": "graph-main",
                        "graph_status": "incomplete",
                    }
                ],
                edges=[],
                coverage={"complete": False},
            )
        with pytest.raises(ValueError, match="import_graph_incomplete"):
            await repo.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {"file_id": "graph-main", "graph_status": "complete"},
                    {"file_id": "graph-helper", "graph_status": "complete"},
                ],
                edges=[
                    {
                        "source_file_id": "graph-main",
                        "target_file_id": "foreign-file",
                        "target_name": "foreign",
                        "edge_type": "import",
                    }
                ],
                coverage={"complete": False},
            )
        with pytest.raises(ValueError, match="diff_tree_changed"):
            await repo.replace_artifact_graph(
                artifact["id"],
                tree_sha256="0" * 64,
                files=[],
                edges=[],
                coverage={"complete": False},
            )
        with pytest.raises(ValueError, match="diff_base_invalid"):
            await repo.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {"file_id": "graph-main", "graph_status": "complete"},
                    {"file_id": "graph-helper", "graph_status": "complete"},
                ],
                edges=[],
                coverage={"complete": False},
                base_artifact_id="artifact-missing",
                base_tree_sha256="1" * 64,
            )
        with pytest.raises(ValueError, match="diff_base_invalid"):
            await repo.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {"file_id": "graph-main", "graph_status": "complete"},
                    {"file_id": "graph-helper", "graph_status": "complete"},
                ],
                edges=[],
                coverage={"complete": False},
                base_artifact_id=unrelated_base["id"],
                base_tree_sha256=str(unrelated_base["tree_sha256"]),
            )
        with pytest.raises(ValueError, match="diff_base_invalid"):
            await repo.replace_artifact_graph(
                artifact["id"],
                tree_sha256=tree_sha256,
                files=[
                    {"file_id": "graph-main", "graph_status": "complete"},
                    {"file_id": "graph-helper", "graph_status": "complete"},
                ],
                edges=[],
                coverage={"complete": False},
                base_artifact_id=artifact["id"],
                base_tree_sha256=tree_sha256,
            )
        refreshed = await repo.get_artifact(artifact["id"])
        assert refreshed is not None
        return (
            await repo.list_artifact_files(artifact["id"]),
            await repo.list_dependency_edges(artifact["id"]),
            refreshed,
        )

    files, edges, artifact = asyncio.run(scenario())

    by_path = {item["path"]: item for item in files}
    assert by_path["main.py"]["is_entrypoint"] is True
    assert by_path["helper.py"]["is_reachable"] is True
    assert edges[0]["target_path"] == "helper.py"
    assert artifact["review_coverage"]["import_graph"]["complete"] is True


def test_comment_versions_final_decision_and_history_are_atomic() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[dict, list[dict], dict]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        await repo.replace_artifact_files(
            artifact["id"],
            [
                {
                    "id": "main-file",
                    "path": "main.py",
                    "sha256": "d" * 64,
                    "size_bytes": 20,
                    "line_count": 2,
                    "is_text": True,
                }
            ],
            "e" * 64,
        )
        thread = await repo.create_review_comment(
            {
                "artifact_id": artifact["id"],
                "file_id": "main-file",
                "file_path": "main.py",
                "file_sha256": "d" * 64,
                "side": "current",
                "line_start": 1,
                "line_end": 1,
                "body": "Please address this line",
                "reviewer_user_id": "admin-1",
                "reviewer_nickname": "Admin",
                "reviewer_role": "admin",
                "idempotency_key": "comment-1",
            }
        )
        replied = await repo.append_review_comment_event(
            thread["id"],
            {
                "type": "reply",
                "body": "Addressed",
                "actor_user_id": user["id"],
                "actor_nickname": "Alice",
                "actor_role": "author",
                "expected_version": 1,
                "idempotency_key": "comment-1-reply",
            },
        )
        assert replied and replied["version"] == 2
        with pytest.raises(ValueError, match="comment_version_conflict"):
            await repo.append_review_comment_event(
                thread["id"],
                {
                    "type": "resolve",
                    "actor_user_id": "admin-1",
                    "actor_nickname": "Admin",
                    "actor_role": "admin",
                    "expected_version": 1,
                    "idempotency_key": "stale-resolve",
                },
            )
        await repo.transition_review_status(artifact["id"], ReviewStatus.PRECHECKING.value)
        await repo.transition_review_status(artifact["id"], ReviewStatus.SCANNING.value)
        await repo.transition_review_status(artifact["id"], ReviewStatus.PENDING_REVIEW.value)
        decided = await repo.decide_artifact(
            artifact["id"],
            action="request_changes",
            target_status="changes_requested",
            reason="Please address the review comment",
            reviewer={"id": "admin-1", "internal_username": "admin"},
            idempotency_key="request-changes-1",
        )
        with pytest.raises(ValueError, match="comment_thread_locked"):
            await repo.append_review_comment_event(
                thread["id"],
                {
                    "type": "reply",
                    "body": "This reply must be rejected",
                    "actor_user_id": user["id"],
                    "actor_nickname": "Alice",
                    "actor_role": "author",
                    "expected_version": 2,
                    "idempotency_key": "reply-after-lock",
                },
            )
        return (
            decided or {},
            await repo.list_review_comments(artifact["id"]),
            (await repo.get_review_history_sources(artifact["id"])),
        )

    decided, comments, history = asyncio.run(scenario())

    assert decided["review_status"] == "changes_requested"
    assert comments[0]["locked_at"]
    assert [event["type"] for event in comments[0]["events"]] == ["create", "reply"]
    assert len(history["comment_events"]) == 2
    assert history["decisions"][0]["action"] == "request_changes"


def test_finding_events_sbom_and_history_keep_structured_sources() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> tuple[dict, dict, dict]:
        artifact = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        run = await repo.create_review_run(
            {"artifact_id": artifact["id"], "type": "runtime", "status": "succeeded"}
        )
        findings = await repo.replace_findings(
            artifact["id"],
            run["id"],
            [
                {
                    "fingerprint": "runtime-import-failure",
                    "severity": "high",
                    "message": "import failed",
                    "source": "runtime",
                    "deterministic": True,
                }
            ],
        )
        finding = findings[0]
        updated = await repo.update_finding_state(
            finding["id"],
            {
                "expected_version": 1,
                "status": "accepted",
                "correlation": {"same_sha": True},
                "affects_current_release": True,
                "actor_user_id": "admin-1",
                "actor_nickname": "Admin",
                "actor_source": "user",
                "reason": "confirmed",
                "idempotency_key": "finding-state-1",
            },
        )
        repeated = await repo.update_finding_state(
            finding["id"],
            {
                "expected_version": 1,
                "status": "accepted",
                "idempotency_key": "finding-state-1",
            },
        )
        replaced = await repo.replace_findings(
            artifact["id"],
            run["id"],
            [
                {
                    "fingerprint": "runtime-import-failure",
                    "severity": "critical",
                    "message": "import still fails",
                    "source": "runtime",
                    "deterministic": True,
                }
            ],
        )
        sbom_payload = {
            "artifact_id": artifact["id"],
            "run_id": run["id"],
            "format": "cyclonedx-json",
            "document_sha256": "f" * 64,
            "object_key": "private/sbom.json",
            "package_count": 3,
            "generator": "runtime-runner",
            "tool_version": "1.0.0",
        }
        sbom = await repo.create_artifact_sbom(sbom_payload)
        repeated_sbom = await repo.create_artifact_sbom(sbom_payload)
        assert updated and repeated and updated["version"] == repeated["version"] == 2
        assert replaced[0]["id"] == finding["id"]
        assert replaced[0]["status"] == "accepted"
        assert replaced[0]["correlation"] == {"same_sha": True}
        assert repeated_sbom["id"] == sbom["id"]
        return updated or {}, sbom, await repo.get_review_history_sources(artifact["id"])

    updated, sbom, history = asyncio.run(scenario())

    assert updated["affects_current_release"] is True
    assert updated["correlation"] == {"same_sha": True}
    assert sbom["package_count"] == 3
    assert history["finding_events"][0]["type"] == "status_changed"
    assert history["runs"][0]["type"] == "runtime"


def test_record_decision_rejects_cross_artifact_idempotency_reuse() -> None:
    repo, user, plugin = make_repository()

    async def scenario() -> None:
        first = await repo.create_artifact(artifact_payload(plugin, user, "a"))
        second = await repo.create_artifact(artifact_payload(plugin, user, "b"))
        await repo.record_decision(
            first["id"],
            action="emergency_override",
            from_status="published",
            to_status="revoking",
            reason="confirmed",
            reviewer={"id": "admin-1", "internal_username": "admin"},
            idempotency_key="same-decision-key",
        )
        with pytest.raises(ValueError) as exc_info:
            await repo.record_decision(
                second["id"],
                action="emergency_override",
                from_status="published",
                to_status="revoking",
                reason="confirmed",
                reviewer={"id": "admin-1", "internal_username": "admin"},
                idempotency_key="same-decision-key",
            )
        assert str(exc_info.value) == ArtifactErrorCode.IDEMPOTENCY_KEY_CONFLICT.value

    asyncio.run(scenario())
