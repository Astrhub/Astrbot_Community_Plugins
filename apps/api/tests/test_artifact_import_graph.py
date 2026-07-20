from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.artifacts.diff import ArtifactDiffService, manifest_tree_sha256
from app.artifacts.import_graph import (
    IMPORT_GRAPH_TOOL_VERSION,
    ImportGraphLimits,
    ImportGraphService,
)
from app.artifacts.models import JobType
from app.artifacts.policy import review_policy_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.stages import DiffGraphStage, StageContext, StageOutcomeKind
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.store import InMemoryMarketStore


def test_import_graph_resolves_packages_relative_imports_cycles_and_external_edges(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "graph"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {
                "main.py": b"import json\nimport third_party\nfrom .pkg import service\n",
                "pkg/__init__.py": b"from .service import run\n",
                "pkg/service.py": b"from . import helper\nfrom .sub import worker\n",
                "pkg/helper.py": b"from .service import run\n",
                "pkg/sub/__init__.py": b"",
                "pkg/sub/worker.py": b"from .. import helper\n",
            },
        )
        result = await ImportGraphService().build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )
        return (
            result,
            await repository.list_dependency_edges(artifact["id"]),
            await repository.list_artifact_files(artifact["id"]),
        )

    result, edges, files = asyncio.run(scenario())

    local_pairs = {
        (edge["source_path"], edge["target_path"]) for edge in edges if edge.get("target_file_id")
    }
    assert result.coverage["complete"] is True
    assert result.coverage["outcome"] == "completed"
    assert ("main.py", "pkg/service.py") in local_pairs
    assert ("pkg/__init__.py", "pkg/service.py") in local_pairs
    assert ("pkg/service.py", "pkg/helper.py") in local_pairs
    assert ("pkg/service.py", "pkg/sub/worker.py") in local_pairs
    assert ("pkg/helper.py", "pkg/service.py") in local_pairs
    assert ("pkg/sub/worker.py", "pkg/helper.py") in local_pairs
    external = {
        edge["target_name"]
        for edge in edges
        if (edge.get("metadata") or {}).get("scope") == "external"
    }
    assert {"json", "third_party"} <= external
    by_path = {item["path"]: item for item in files}
    assert by_path["main.py"]["is_entrypoint"] is True
    assert all(item["is_reachable"] for item in files)
    assert all(item["graph_status"] == "complete" for item in files)
    assert result.coverage["review_paths"] == sorted(by_path)


def test_import_graph_marks_dynamic_path_mutation_syntax_and_relative_unknown_incomplete(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "incomplete"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {
                "main.py": (
                    b"import importlib\nimport sys\n"
                    b"sys.path.append('unsafe')\n"
                    b"plugin = importlib.import_module(name)\n"
                    b"other = __import__('pkg.helper')\n"
                    b"from . import missing_local\n"
                    b"from .. import impossible\n"
                ),
                "broken.py": b"def broken(:\n    pass\n",
            },
        )
        result = await ImportGraphService().build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )
        return (
            result,
            await repository.list_dependency_edges(artifact["id"]),
            await repository.list_artifact_files(artifact["id"]),
        )

    result, edges, files = asyncio.run(scenario())

    reasons = result.coverage["reasons"]
    assert result.coverage["outcome"] == "completed"
    assert result.coverage["complete"] is False
    assert result.coverage["full_review_required"] is True
    assert any(reason.startswith("dynamic_import:main.py") for reason in reasons)
    assert any(reason.startswith("sys_path_mutation:main.py") for reason in reasons)
    assert any(reason.startswith("syntax_error:broken.py") for reason in reasons)
    assert any(reason.startswith("relative_import_unknown:main.py") for reason in reasons)
    assert any(reason.startswith("local_import_unresolved:main.py") for reason in reasons)
    assert sum(edge["edge_type"] == "dynamic" for edge in edges) == 2
    assert any(edge["edge_type"] == "unknown" for edge in edges)
    by_path = {item["path"]: item for item in files}
    assert by_path["main.py"]["graph_status"] == "incomplete"
    assert by_path["broken.py"]["graph_status"] == "incomplete"
    assert result.coverage["review_paths"] == ["broken.py", "main.py"]


def test_import_graph_scope_includes_reverse_and_deleted_base_impact(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "scope-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "scope-current"),
                "base_artifact_id": base["id"],
            }
        )
        await _seed_manifest(
            repository,
            storage,
            base,
            {
                "main.py": b"from . import feature, consumer\n",
                "feature.py": b"from . import helper\n",
                "helper.py": b"VALUE = 1\n",
                "consumer.py": b"from . import old_deleted\n",
                "old_deleted.py": b"OLD = True\n",
                "requirements.txt": b"demo==1.0\n",
                "metadata.yaml": b"name: demo\n",
            },
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {
                "main.py": b"from . import feature, consumer\n",
                "feature.py": b"from . import helper\n",
                "helper.py": b"VALUE = 2\n",
                "consumer.py": b"from . import old_deleted\n",
                "requirements.txt": b"demo==2.0\n",
                "metadata.yaml": b"name: demo\n",
            },
        )
        await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
            forced_paths={"metadata.yaml", "requirements.txt"},
        )
        result = await ImportGraphService().build(
            artifact=current,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
            forced_paths={"metadata.yaml", "requirements.txt"},
        )
        return result, await repository.list_artifact_files(current["id"])

    result, files = asyncio.run(scenario())

    coverage = result.coverage
    assert coverage["changed_paths"] == ["helper.py", "requirements.txt"]
    assert coverage["reverse_impact_paths"] == ["feature.py", "main.py"]
    assert coverage["removed_paths"] == ["old_deleted.py"]
    assert coverage["removed_impact_paths"] == ["consumer.py", "main.py"]
    assert coverage["force_full_runtime"] is True
    assert coverage["force_full_dependency"] is True
    assert coverage["complete"] is False
    assert any(
        reason.startswith("removed_local_import:consumer.py") for reason in coverage["reasons"]
    )
    assert {
        "consumer.py",
        "feature.py",
        "helper.py",
        "main.py",
        "metadata.yaml",
        "requirements.txt",
    } <= set(coverage["review_paths"])
    by_path = {item["path"]: item for item in files}
    assert by_path["main.py"]["is_entrypoint"] is True
    assert by_path["helper.py"]["is_reachable"] is True


def test_entrypoint_only_change_forces_full_runtime_and_dependency(tmp_path: Path) -> None:
    async def scenario() -> Any:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "entry-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "entry-current"),
                "base_artifact_id": base["id"],
            }
        )
        await _seed_manifest(repository, storage, base, {"main.py": b"VALUE = 1\n"})
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {"main.py": b"VALUE = 2\n"},
        )
        await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        return await ImportGraphService().build(
            artifact=current,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )

    result = asyncio.run(scenario())

    assert result.coverage["changed_paths"] == ["main.py"]
    assert result.coverage["force_full_runtime"] is True
    assert result.coverage["force_full_dependency"] is True


def test_removed_impact_maps_exactly_renamed_importers(tmp_path: Path) -> None:
    async def scenario() -> Any:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "rename-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "rename-current"),
                "base_artifact_id": base["id"],
            }
        )
        consumer = b"from . import old_deleted\n"
        await _seed_manifest(
            repository,
            storage,
            base,
            {
                "main.py": b"from . import consumer\n",
                "consumer.py": consumer,
                "old_deleted.py": b"OLD = True\n",
            },
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {
                "main.py": b"from . import consumer_new\n",
                "consumer_new.py": consumer,
            },
        )
        await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        return await ImportGraphService().build(
            artifact=current,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )

    result = asyncio.run(scenario())

    assert result.coverage["removed_paths"] == ["old_deleted.py"]
    assert result.coverage["removed_impact_paths"] == ["consumer_new.py", "main.py"]


def test_import_graph_matches_astrbot_module_prefix_without_guessing_absolute_local(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "runtime"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {
                "__init__.py": b"ROOT = True\n",
                "main.py": (
                    b"import absolute_helper\n"
                    b"from . import relative_helper\n"
                    b"from data.plugins.astrbot_plugin_graph import prefixed_helper\n"
                ),
                "absolute_helper.py": b"VALUE = 'absolute'\n",
                "relative_helper.py": b"VALUE = 'relative'\n",
                "prefixed_helper.py": b"VALUE = 'prefixed'\n",
            },
        )
        result = await ImportGraphService().build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )
        return (
            result,
            await repository.list_dependency_edges(artifact["id"]),
            await repository.list_artifact_files(artifact["id"]),
        )

    result, edges, files = asyncio.run(scenario())

    by_target = {edge["target_name"]: edge for edge in edges}
    assert result.coverage["complete"] is True
    assert by_target["absolute_helper"]["target_file_id"] is None
    assert by_target["absolute_helper"]["metadata"]["scope"] == "external"
    assert by_target["relative_helper"]["target_path"] == "relative_helper.py"
    assert by_target["prefixed_helper"]["target_path"] == "prefixed_helper.py"
    reachable = {item["path"] for item in files if item["is_reachable"]}
    assert "absolute_helper.py" not in reachable
    assert {"__init__.py", "main.py", "relative_helper.py", "prefixed_helper.py"} <= reachable
    root_init = next(item for item in files if item["path"] == "__init__.py")
    assert root_init["is_entrypoint"] is True


def test_import_graph_resource_limit_is_visible_and_falls_back_to_all_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> Any:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "limit"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {
                "main.py": b"from . import helper\n",
                "helper.py": b"a = 1\nb = 2\nc = 3\n",
                "README.md": b"review me\n",
            },
        )
        return await ImportGraphService(
            ImportGraphLimits(max_lines_per_file=2, max_total_lines=4)
        ).build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )

    result = asyncio.run(scenario())

    assert result.coverage["complete"] is False
    assert any(
        reason.startswith("graph_file_too_many_lines:helper.py")
        for reason in result.coverage["reasons"]
    )
    assert result.coverage["review_paths"] == ["README.md", "helper.py", "main.py"]


def test_import_graph_stage_persists_coverage_and_recovers_validated_graph(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any, list[dict[str, Any]], dict[str, Any]]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "stage"),
                "base_artifact_id": "artifact_missing_base",
            }
        )
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {
                "main.py": (
                    b"from . import helper\nimport importlib\n"
                    b"plugin = importlib.import_module(name)\n"
                ),
                "helper.py": b"VALUE = 1\n",
            },
        )
        policy_payload = _review_policy_payload()
        policy = await repository.create_review_policy(
            {
                "version": "graph-policy-v1",
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
        await repository.bind_artifact_policy(artifact["id"], policy["id"])
        await repository.transition_review_status(artifact["id"], "prechecking")
        scanning = await repository.transition_review_status(artifact["id"], "scanning")
        assert scanning is not None
        job = {
            "id": "job-import-graph",
            "artifact_id": artifact["id"],
            "type": JobType.DIFF_GRAPH.value,
            "attempts": 1,
            "policy_version_id": policy["id"],
            "payload": {
                "stage": "import_graph",
                "stage_name": "import_graph",
                "tool_version": IMPORT_GRAPH_TOOL_VERSION,
                "input_sha256": "3" * 64,
            },
        }
        stage = DiffGraphStage()
        first = await stage.execute(
            StageContext.create(
                job=job,
                artifact=scanning,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-import-graph-stage"),
            )
        )
        refreshed = await repository.get_artifact(artifact["id"])
        assert refreshed is not None
        recovered = await stage.execute(
            StageContext.create(
                job={**job, "attempts": 2},
                artifact=refreshed,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-import-graph-stage"),
            )
        )
        repository.artifacts[artifact["id"]]["review_coverage"]["import_graph"]["review_paths"] = []
        tampered = await repository.get_artifact(artifact["id"])
        assert tampered is not None
        repaired = await stage.execute(
            StageContext.create(
                job={**job, "attempts": 3},
                artifact=tampered,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-import-graph-stage"),
            )
        )
        succeeded_runs = [
            run
            for run in await repository.list_review_runs(artifact["id"])
            if run["type"] == "import_graph" and run["status"] == "succeeded"
        ]
        repository.runs[str(succeeded_runs[-1]["id"])]["coverage"]["complete"] = True
        refreshed_after_run_tamper = await repository.get_artifact(artifact["id"])
        assert refreshed_after_run_tamper is not None
        recovered_after_run_tamper = await stage.execute(
            StageContext.create(
                job={**job, "attempts": 4},
                artifact=refreshed_after_run_tamper,
                policy=policy,
                repository=repository,
                storage=storage,
                tools={},
                logger=logging.getLogger("test-import-graph-stage"),
            )
        )
        final_artifact = await repository.get_artifact(artifact["id"])
        assert final_artifact is not None
        return (
            first,
            recovered,
            repaired,
            recovered_after_run_tamper,
            await repository.list_review_runs(artifact["id"]),
            final_artifact,
        )

    first, recovered, repaired, recovered_after_run_tamper, runs, artifact = asyncio.run(scenario())

    assert first.kind == StageOutcomeKind.COMPLETED
    assert recovered.kind == StageOutcomeKind.COMPLETED
    assert recovered.coverage["recovered"] is True
    assert repaired.kind == StageOutcomeKind.COMPLETED
    assert recovered_after_run_tamper.kind == StageOutcomeKind.COMPLETED
    assert recovered_after_run_tamper.coverage["recovered"] is True
    assert recovered_after_run_tamper.coverage["complete"] is False
    assert len([run for run in runs if run["type"] == "import_graph"]) == 2
    assert first.coverage["complete"] is False
    assert first.coverage["requested_base_artifact_id"] == "artifact_missing_base"
    assert first.coverage["base_artifact_id"] is None
    assert "base_graph_unavailable" in first.coverage["reasons"]
    assert artifact["review_coverage"]["import_graph"]["complete"] is False
    assert artifact["review_coverage"]["import_graph"]["review_paths"]


def _repository_fixture() -> tuple[InMemoryArtifactRepository, dict[str, Any], dict[str, Any]]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        user,
        {
            "name": "astrbot_plugin_graph",
            "display_name": "Graph",
            "desc": "Graph fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_graph",
            "tags": [],
        },
    )
    return InMemoryArtifactRepository(store), user, plugin


def _review_policy_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "required_stages": ["static", "diff", "import_graph"],
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


def _artifact_payload(plugin: Mapping[str, Any], user: Mapping[str, Any], marker: str) -> dict:
    archive_sha256 = hashlib.sha256(marker.encode()).hexdigest()
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
) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    for index, (path, content) in enumerate(sorted(files.items())):
        file_id = f"file_{str(artifact['id']).removeprefix('artifact_')}_{index}"
        content_key = build_content_key(str(artifact["id"]), file_id)
        await storage.put_text_content(content_key, content)
        suffix = Path(path).suffix.lower()
        language = "python" if suffix == ".py" else "text"
        manifests.append(
            {
                "id": file_id,
                "path": path,
                "language": language,
                "mime_type": "text/x-python" if language == "python" else "text/plain",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "line_count": len(content.decode().splitlines()),
                "is_text": True,
                "content_key": content_key,
            }
        )
    tree_sha256 = manifest_tree_sha256(manifests)
    await repository.replace_artifact_files(str(artifact["id"]), manifests, tree_sha256)
    updated = await repository.get_artifact(str(artifact["id"]))
    assert updated is not None
    return updated
