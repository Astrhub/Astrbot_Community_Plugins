from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.artifacts.diff import (
    DIFF_TOOL_VERSION,
    ArtifactDiffService,
    DiffBuildError,
    DiffLimits,
    manifest_tree_sha256,
    validate_hunk_payload,
)
from app.artifacts.models import JobType
from app.artifacts.policy import review_policy_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.stages import DiffGraphStage, StageContext, StageOutcomeKind
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.store import InMemoryMarketStore


def test_diff_classifies_files_and_only_accepts_unambiguous_exact_renames(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], LocalArtifactStorage]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "a"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "b"),
                "base_artifact_id": base["id"],
            }
        )
        await _seed_manifest(
            repository,
            storage,
            base,
            {
                "metadata.yaml": b"name: demo\n",
                "main.py": "value = '旧'\r\nprint(value)\n".encode(),
                "old_name.py": b"RENAMED = True\n",
                "delete.bin": b"\x00\x01\x02",
                "duplicate_a.py": b"DUPLICATE = True\n",
                "duplicate_b.py": b"DUPLICATE = True\n",
            },
            entrypoints={"main.py"},
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {
                "metadata.yaml": b"name: demo\n",
                "main.py": "value = '新'\nprint(value)\nprint('增加')\n".encode(),
                "new_name.py": b"RENAMED = True\n",
                "added.txt": b"new file\n",
                "duplicate_c.py": b"DUPLICATE = True\n",
            },
            entrypoints={"main.py"},
        )
        result = await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
            forced_paths={"metadata.yaml"},
        )
        return result, await repository.list_artifact_diffs(current["id"]), storage

    result, diffs, storage = asyncio.run(scenario())

    by_path = {item["path"]: item for item in diffs}
    assert result.degraded_code is None
    assert result.coverage["complete"] is True
    assert result.coverage["counts"] == {
        "added": 2,
        "deleted": 3,
        "modified": 1,
        "renamed": 1,
        "unchanged": 1,
    }
    assert by_path["metadata.yaml"]["change_type"] == "unchanged"
    assert by_path["metadata.yaml"]["stats"]["forced_review"] is True
    assert by_path["main.py"]["change_type"] == "modified"
    assert by_path["main.py"]["hunks_key"]
    assert by_path["new_name.py"]["change_type"] == "renamed"
    assert by_path["new_name.py"]["base_path"] == "old_name.py"
    assert by_path["new_name.py"]["hunks_key"] is None
    assert by_path["delete.bin"]["change_type"] == "deleted"
    assert by_path["delete.bin"]["hunks_key"] is None
    assert by_path["duplicate_a.py"]["change_type"] == "deleted"
    assert by_path["duplicate_b.py"]["change_type"] == "deleted"
    assert by_path["duplicate_c.py"]["change_type"] == "added"

    main = by_path["main.py"]
    payload = asyncio.run(
        storage.read_text_content(
            main["hunks_key"],
            512 * 1024,
            main["stats"]["hunks_sha256"],
        )
    )
    document = json.loads(payload)
    assert document["schema_version"] == "1"
    assert document["base"]["sha256"] == main["base_sha256"]
    assert document["current"]["sha256"] == main["current_sha256"]
    assert document["base_tree_sha256"] == main["base_tree_sha256"]
    assert document["current_tree_sha256"] == main["current_tree_sha256"]
    assert document["hunks"][0]["header"].startswith("@@ -")
    assert any(line["text"] == "value = '新'" for line in document["hunks"][0]["lines"])
    document["current_tree_sha256"] = "0" * 64
    with pytest.raises(DiffBuildError, match="tree binding"):
        validate_hunk_payload(
            json.dumps(document).encode(),
            diff=main,
            artifact_id=str(main["artifact_id"]),
            current_tree_sha256=str(main["current_tree_sha256"]),
            base_tree_sha256=str(main["base_tree_sha256"]),
        )


def test_diff_hunks_are_deterministic_bounded_and_preserve_newline_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, Any, bytes, bytes]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "c"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "d"),
                "base_artifact_id": base["id"],
            }
        )
        base = await _seed_manifest(
            repository,
            storage,
            base,
            {"main.py": "第一行\r\n第二行\n末行".encode()},
            entrypoints={"main.py"},
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": "第一行\n第二行已修改\n末行\n新增行\n".encode()},
            entrypoints={"main.py"},
        )
        limits = DiffLimits(
            max_text_file_bytes=1024,
            max_total_text_bytes=2048,
            max_hunk_bytes=2048,
            max_total_hunk_bytes=2048,
            context_lines=1,
            max_hunks_per_file=10,
        )
        service = ArtifactDiffService(limits)
        first = await service.build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        first_diff = (await repository.list_artifact_diffs(current["id"]))[0]
        first_payload = await storage.read_text_content(
            first_diff["hunks_key"],
            limits.max_hunk_bytes,
            first_diff["stats"]["hunks_sha256"],
        )
        second = await service.build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        second_diff = (await repository.list_artifact_diffs(current["id"]))[0]
        second_payload = await storage.read_text_content(
            second_diff["hunks_key"],
            limits.max_hunk_bytes,
            second_diff["stats"]["hunks_sha256"],
        )
        return first, second, first_payload, second_payload

    first, second, first_payload, second_payload = asyncio.run(scenario())

    assert first.input_sha256 == second.input_sha256
    assert first.output_sha256 == second.output_sha256
    assert first_payload == second_payload
    assert len(first_payload) <= 2048
    document = json.loads(first_payload)
    assert document["truncated"] is False
    newline_kinds = {line["newline"] for hunk in document["hunks"] for line in hunk["lines"]}
    assert {"crlf", "lf", "none"} <= newline_kinds
    assert all(hunk["old_start"] >= 0 and hunk["new_start"] >= 0 for hunk in document["hunks"])


def test_invalid_or_missing_base_degrades_to_full_current_review(tmp_path: Path) -> None:
    async def scenario(base_mode: str) -> tuple[Any, list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path / base_mode, "https://cdn.example.test")
        base_id: str | None = None
        if base_mode == "cross_plugin":
            other = repository.store.submit_plugin(
                user,
                {
                    "name": "astrbot_plugin_other",
                    "display_name": "Other",
                    "desc": "Other plugin",
                    "author": "Alice",
                    "repo": "https://github.com/alice/astrbot_plugin_other",
                    "tags": [],
                },
            )
            base = await repository.create_artifact(_artifact_payload(other, user, "e"))
            await _seed_manifest(repository, storage, base, {"main.py": b"old\n"})
            base_id = base["id"]
        elif base_mode == "unknown":
            base_id = "artifact_missing"
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, base_mode[0]),
                "base_artifact_id": base_id,
            }
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": b"current\n", "metadata.yaml": b"name: demo\n"},
        )
        result = await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        return result, await repository.list_artifact_diffs(current["id"])

    missing, missing_diffs = asyncio.run(scenario("missing"))
    unknown, unknown_diffs = asyncio.run(scenario("unknown"))
    cross, cross_diffs = asyncio.run(scenario("cross_plugin"))

    assert missing.degraded_code == "diff_base_missing"
    assert unknown.degraded_code == "diff_base_invalid"
    assert cross.degraded_code == "diff_base_invalid"
    for result, diffs in (
        (missing, missing_diffs),
        (unknown, unknown_diffs),
        (cross, cross_diffs),
    ):
        assert result.coverage["complete"] is False
        assert result.coverage["outcome"] == "completed"
        assert result.blocking_code is None
        assert result.coverage["full_review_required"] is True
        assert result.coverage["compared_base_artifact_id"] is None
        assert {item["change_type"] for item in diffs} == {"added"}
        assert all(item["base_artifact_id"] is None for item in diffs)


def test_manifest_tree_drift_is_degraded_without_stale_diff_rows(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        current = await repository.create_artifact(_artifact_payload(plugin, user, "f"))
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": b"print('ok')\n"},
        )
        repository.artifacts[current["id"]]["tree_sha256"] = "0" * 64
        drifted = await repository.get_artifact(current["id"])
        assert drifted is not None
        result = await ArtifactDiffService().build(
            artifact=drifted,
            repository=repository,
            storage=storage,
        )
        return result, await repository.list_artifact_diffs(current["id"])

    result, diffs = asyncio.run(scenario())

    assert result.degraded_code == "diff_current_manifest_incomplete"
    assert result.blocking_code == "diff_current_manifest_incomplete"
    assert result.coverage["outcome"] == "degraded"
    assert result.coverage["complete"] is False
    assert result.coverage["full_review_required"] is True
    assert diffs == []


def test_incomplete_base_manifest_falls_back_to_current_files(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "bad-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "bad-base-current"),
                "base_artifact_id": base["id"],
            }
        )
        base = await _seed_manifest(repository, storage, base, {"main.py": b"old\n"})
        repository.artifacts[base["id"]]["tree_sha256"] = "0" * 64
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": b"current\n", "metadata.yaml": b"name: demo\n"},
        )
        result = await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        return result, await repository.list_artifact_diffs(current["id"])

    result, diffs = asyncio.run(scenario())

    assert result.degraded_code == "diff_base_manifest_incomplete"
    assert result.blocking_code is None
    assert result.coverage["outcome"] == "completed"
    assert result.coverage["compared_base_artifact_id"] is None
    assert result.coverage["full_review_required"] is True
    assert {item["change_type"] for item in diffs} == {"added"}


def test_hunk_output_limit_is_explicitly_degraded_without_partial_payload(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "limit-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "limit-current"),
                "base_artifact_id": base["id"],
            }
        )
        await _seed_manifest(
            repository,
            storage,
            base,
            {"main.py": ("A" * 400 + "\n").encode()},
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": ("B" * 400 + "\n").encode()},
        )
        result = await ArtifactDiffService(
            DiffLimits(
                max_text_file_bytes=1024,
                max_total_text_bytes=2048,
                max_hunk_bytes=512,
                max_total_hunk_bytes=512,
                context_lines=0,
                max_hunks_per_file=1,
            )
        ).build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        diff = (await repository.list_artifact_diffs(current["id"]))[0]
        return result, diff

    result, diff = asyncio.run(scenario())

    assert result.degraded_code == "diff_hunks_truncated"
    assert result.blocking_code is None
    assert result.coverage["outcome"] == "completed"
    assert result.coverage["complete"] is False
    assert result.coverage["full_review_required"] is True
    assert diff["hunks_key"] is None
    assert diff["stats"]["hunks_complete"] is False
    assert diff["stats"]["hunks_truncated"] is True
    assert diff["stats"]["added_lines"] == 1
    assert diff["stats"]["deleted_lines"] == 1


def test_text_line_limit_degrades_before_diff_algorithm_runs(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "lines-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "lines-current"),
                "base_artifact_id": base["id"],
            }
        )
        await _seed_manifest(repository, storage, base, {"main.py": b"a\nb\nc\n"})
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": b"a\nb\nchanged\n"},
        )
        result = await ArtifactDiffService(
            DiffLimits(max_text_lines_per_file=2, max_total_text_lines=4)
        ).build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        diff = (await repository.list_artifact_diffs(current["id"]))[0]
        return result, diff

    result, diff = asyncio.run(scenario())

    assert result.degraded_code == "diff_text_file_too_many_lines"
    assert result.blocking_code is None
    assert result.coverage["complete"] is False
    assert diff["hunks_key"] is None
    assert diff["stats"]["hunks_omitted_reason"] == "diff_text_file_too_many_lines"


def test_diff_stage_recovers_validated_side_effects_and_rejects_import_graph(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        Any,
        Any,
        Any,
        list[dict[str, Any]],
        list[dict[str, Any]],
        Any,
    ]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "stage-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "stage-current"),
                "base_artifact_id": base["id"],
            }
        )
        await _seed_manifest(repository, storage, base, {"main.py": b"old = True\n"})
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": b"new = True\n"},
            entrypoints={"main.py"},
        )
        policy_payload = _review_policy_payload()
        policy = await repository.create_review_policy(
            {
                "version": "diff-policy-v1",
                "schema_version": "1",
                "status": "active",
                "is_default": True,
                "policy": policy_payload,
                "policy_sha256": review_policy_sha256(policy_payload),
                "validation_summary": {"valid": True},
                "validated_at": datetime.now(UTC).isoformat(),
                "activated_at": datetime.now(UTC).isoformat(),
            }
        )
        await repository.bind_artifact_policy(current["id"], policy["id"])
        await repository.transition_review_status(current["id"], "prechecking")
        scanning = await repository.transition_review_status(current["id"], "scanning")
        assert scanning is not None
        stage = DiffGraphStage()
        job = {
            "id": "job-diff-stage",
            "artifact_id": current["id"],
            "type": JobType.DIFF_GRAPH.value,
            "attempts": 1,
            "policy_version_id": policy["id"],
            "payload": {
                "stage": "diff",
                "stage_name": "diff",
                "tool_version": DIFF_TOOL_VERSION,
                "input_sha256": "1" * 64,
            },
        }
        first = await stage.execute(
            StageContext.create(
                job=job,
                artifact=scanning,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-diff-stage"),
            )
        )
        recovered = await stage.execute(
            StageContext.create(
                job={**job, "attempts": 2},
                artifact=scanning,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-diff-stage"),
            )
        )
        repository.diffs[current["id"]][0]["stats"]["forced_review"] = False
        repaired = await stage.execute(
            StageContext.create(
                job={**job, "attempts": 3},
                artifact=scanning,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-diff-stage"),
            )
        )
        unsupported = await stage.execute(
            StageContext.create(
                job={
                    **job,
                    "id": "job-import-stage",
                    "payload": {
                        **job["payload"],
                        "stage": "import_graph",
                        "stage_name": "import_graph",
                    },
                },
                artifact=scanning,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-diff-stage"),
            )
        )
        return (
            first,
            recovered,
            repaired,
            await repository.list_review_runs(current["id"]),
            await repository.list_artifact_diffs(current["id"]),
            unsupported,
        )

    first, recovered, repaired, runs, diffs, unsupported = asyncio.run(scenario())

    assert first.kind == StageOutcomeKind.COMPLETED
    assert recovered.kind == StageOutcomeKind.COMPLETED
    assert recovered.coverage["recovered"] is True
    assert repaired.kind == StageOutcomeKind.COMPLETED
    assert len([run for run in runs if run["type"] == "diff"]) == 2
    assert diffs[0]["stats"]["forced_review"] is True
    assert diffs[0]["hunks_key"]
    assert unsupported.kind == StageOutcomeKind.TERMINAL_FAILURE
    assert unsupported.error_code == "diff_graph_stage_unsupported"


def test_diff_stage_full_review_fallback_completes_without_claiming_full_coverage(
    tmp_path: Path,
) -> None:
    async def scenario() -> Any:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "first"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {"main.py": b"print('first release')\n"},
            entrypoints={"main.py"},
        )
        policy_payload = _review_policy_payload()
        policy = await repository.create_review_policy(
            {
                "version": "diff-first-policy-v1",
                "schema_version": "1",
                "status": "active",
                "is_default": True,
                "policy": policy_payload,
                "policy_sha256": review_policy_sha256(policy_payload),
            }
        )
        await repository.bind_artifact_policy(artifact["id"], policy["id"])
        await repository.transition_review_status(artifact["id"], "prechecking")
        scanning = await repository.transition_review_status(artifact["id"], "scanning")
        assert scanning is not None
        return await DiffGraphStage().execute(
            StageContext.create(
                job={
                    "id": "job-first-diff",
                    "artifact_id": artifact["id"],
                    "type": JobType.DIFF_GRAPH.value,
                    "attempts": 1,
                    "policy_version_id": policy["id"],
                    "payload": {
                        "stage": "diff",
                        "stage_name": "diff",
                        "tool_version": DIFF_TOOL_VERSION,
                        "input_sha256": "2" * 64,
                    },
                },
                artifact=scanning,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-first-diff"),
            )
        )

    outcome = asyncio.run(scenario())

    assert outcome.kind == StageOutcomeKind.COMPLETED
    assert outcome.coverage["outcome"] == "completed"
    assert outcome.coverage["complete"] is False
    assert outcome.coverage["full_review_required"] is True
    assert outcome.coverage["reason"] == "diff_base_missing"


def _repository_fixture() -> tuple[InMemoryArtifactRepository, dict[str, Any], dict[str, Any]]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        user,
        {
            "name": "astrbot_plugin_diff",
            "display_name": "Diff",
            "desc": "Diff fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_diff",
            "tags": [],
        },
    )
    return InMemoryArtifactRepository(store), user, plugin


def _review_policy_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "required_stages": ["static", "diff"],
        "runtime_targets": [],
        "limits": {
            "cpu": 1,
            "memory_mb": 768,
            "pids": 128,
            "timeout_seconds": 120,
        },
        "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
        "llm": {"enabled": False},
        "malware": {"clamav": False},
        "dependency": {"enabled": False},
        "routing": {"auto_approve": False, "manual_review_at": "low"},
    }


def _artifact_payload(plugin: Mapping[str, Any], user: Mapping[str, Any], digest: str) -> dict:
    archive_sha256 = _sha256(digest.encode())
    return {
        "plugin_id": plugin["id"],
        "version": "1.0.0",
        "normalized_version": "1.0.0",
        "source_type": "upload",
        "source_repo": plugin["repo"],
        "archive_sha256": archive_sha256,
        "size_bytes": 128,
        "quarantine_key": f"quarantine/{archive_sha256[:12]}.zip",
        "submitted_by": user["id"],
    }


async def _seed_manifest(
    repository: InMemoryArtifactRepository,
    storage: LocalArtifactStorage,
    artifact: Mapping[str, Any],
    files: Mapping[str, bytes],
    *,
    entrypoints: set[str] | None = None,
) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    for index, (path, content) in enumerate(sorted(files.items())):
        file_id = f"file_{str(artifact['id']).removeprefix('artifact_')}_{index}"
        is_text = b"\x00" not in content
        content_key = build_content_key(str(artifact["id"]), file_id) if is_text else None
        if content_key:
            await storage.put_text_content(content_key, content)
        manifests.append(
            {
                "id": file_id,
                "path": path,
                "language": "python" if path.endswith(".py") else "text",
                "mime_type": "text/plain" if is_text else "application/octet-stream",
                "sha256": _sha256(content),
                "size_bytes": len(content),
                "line_count": len(content.decode().splitlines()) if is_text else None,
                "is_text": is_text,
                "content_key": content_key,
                "is_entrypoint": path in (entrypoints or set()),
            }
        )
    tree_sha256 = manifest_tree_sha256(manifests)
    await repository.replace_artifact_files(str(artifact["id"]), manifests, tree_sha256)
    updated = await repository.get_artifact(str(artifact["id"]))
    assert updated is not None
    return updated


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
