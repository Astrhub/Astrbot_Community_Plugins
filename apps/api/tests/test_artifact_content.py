from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.artifacts.content import (
    ArtifactContentError,
    ArtifactContentLimits,
    ArtifactContentService,
)
from app.artifacts.diff import ArtifactDiffService, manifest_tree_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.store import InMemoryMarketStore


def test_content_service_pages_registered_text_and_never_exposes_private_keys(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any, Any, str]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "current"),
                "base_artifact_id": base["id"],
            }
        )
        base = await _seed_manifest(
            repository,
            storage,
            base,
            {
                "main.py": (b"first\nsecond\nthird\n", True),
                "asset.bin": (b"\x00\x01\x02", False),
            },
        )
        current = await _seed_manifest(
            repository,
            storage,
            current,
            {
                "main.py": (b"first\nchanged\nthird\n", True),
                "asset.bin": (b"\x00\x01\x02", False),
                "README.md": (b"# Demo\n", True),
            },
        )
        await ArtifactDiffService().build(
            artifact=current,
            repository=repository,
            storage=storage,
        )
        service = ArtifactContentService(repository, storage)
        files = await service.list_files(current, limit=2, offset=0)
        main_file = next(
            item
            for item in await repository.list_artifact_files(current["id"])
            if item["path"] == "main.py"
        )
        base_main = next(
            item
            for item in await repository.list_artifact_files(base["id"])
            if item["path"] == "main.py"
        )
        content = await service.read_file(
            current,
            main_file["id"],
            start_line=2,
            line_limit=1,
        )
        diffs = await service.list_diffs(current, limit=20, offset=0)
        main_diff = next(item for item in diffs["items"] if item["path"] == "main.py")
        document = await service.read_diff(current, main_diff["id"])
        selected_hunk = await service.read_diff(
            current,
            main_diff["id"],
            hunk_id=document["hunks"][0]["id"],
        )
        binary_diff = next(item for item in diffs["items"] if item["path"] == "asset.bin")
        unavailable = await service.read_diff(current, binary_diff["id"])
        with pytest.raises(ArtifactContentError) as cross_artifact:
            await service.read_file(current, base_main["id"], start_line=1, line_limit=20)
        return files, content, diffs, selected_hunk, unavailable, cross_artifact.value.code

    files, content, diffs, selected_hunk, unavailable, cross_artifact_code = asyncio.run(scenario())

    assert files["total"] == 3
    assert len(files["items"]) == 2
    assert content["lines"] == [{"number": 2, "text": "changed"}]
    assert content["total_lines"] == 3
    assert content["truncated"] is True
    assert diffs["total"] == 3
    assert selected_hunk["hunks"][0]["id"].startswith("hunk-")
    assert unavailable["hunks_available"] is False
    assert unavailable["hunks"] == []
    assert cross_artifact_code == "artifact_file_not_found"
    _assert_no_private_keys(files)
    _assert_no_private_keys(content)
    _assert_no_private_keys(diffs)
    _assert_no_private_keys(selected_hunk)


def test_content_service_rejects_binary_invalid_ranges_utf8_size_and_sha_drift(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, str]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "errors"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {
                "main.py": (b"one\ntwo\n", True),
                "invalid.py": (b"\xff\xfe", True),
                "asset.bin": (b"\x00\x01", False),
                "large.txt": (b"0123456789", True),
            },
        )
        rows = {item["path"]: item for item in await repository.list_artifact_files(artifact["id"])}
        service = ArtifactContentService(
            repository,
            storage,
            ArtifactContentLimits(max_file_bytes=8, max_response_bytes=1024),
        )
        errors: dict[str, str] = {}

        for label, path, start, limit in (
            ("binary", "asset.bin", 1, 20),
            ("utf8", "invalid.py", 1, 20),
            ("large", "large.txt", 1, 20),
            ("start", "main.py", 0, 20),
            ("past_end", "main.py", 3, 20),
            ("limit", "main.py", 1, 501),
        ):
            with pytest.raises(ArtifactContentError) as caught:
                await service.read_file(
                    artifact,
                    rows[path]["id"],
                    start_line=start,
                    line_limit=limit,
                )
            errors[label] = caught.value.code

        content_path = storage.content_root / str(rows["main.py"]["content_key"])
        content_path.write_bytes(b"bad\nbad\n")
        with pytest.raises(ArtifactContentError) as caught:
            await service.read_file(
                artifact,
                rows["main.py"]["id"],
                start_line=1,
                line_limit=20,
            )
        errors["sha"] = caught.value.code
        return errors

    errors = asyncio.run(scenario())

    assert errors == {
        "binary": "artifact_file_not_text",
        "utf8": "artifact_file_invalid_utf8",
        "large": "artifact_file_too_large",
        "start": "artifact_content_range_invalid",
        "past_end": "artifact_content_range_invalid",
        "limit": "artifact_content_range_invalid",
        "sha": "artifact_file_sha_changed",
    }


def test_content_service_rejects_a_single_line_over_response_budget(tmp_path: Path) -> None:
    async def scenario() -> str:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "long-line"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {"main.py": (b"x" * 400 + b"\n", True)},
        )
        row = (await repository.list_artifact_files(artifact["id"]))[0]
        service = ArtifactContentService(
            repository,
            storage,
            ArtifactContentLimits(max_file_bytes=1024, max_response_bytes=256),
        )
        with pytest.raises(ArtifactContentError) as caught:
            await service.read_file(
                artifact,
                row["id"],
                start_line=1,
                line_limit=20,
            )
        return caught.value.code

    assert asyncio.run(scenario()) == "artifact_file_too_large"


def test_content_service_rejects_file_tree_over_response_budget(tmp_path: Path) -> None:
    async def scenario() -> str:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "large-tree"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {f"{'界' * 120}.py": (b"VALUE = 1\n", True)},
        )
        service = ArtifactContentService(
            repository,
            storage,
            ArtifactContentLimits(max_response_bytes=256),
        )
        with pytest.raises(ArtifactContentError) as caught:
            await service.list_files(artifact, limit=20, offset=0)
        return caught.value.code

    assert asyncio.run(scenario()) == "artifact_response_too_large"


def test_content_service_rejects_path_ids_and_foreign_object_keys_before_storage(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[str, str, str]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "bad-ids"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {"main.py": (b"VALUE = 1\n", True)},
        )
        foreign = await repository.create_artifact(_artifact_payload(plugin, user, "foreign-key"))
        foreign = await _seed_manifest(
            repository,
            storage,
            foreign,
            {"main.py": (b"VALUE = 1\n", True)},
        )
        foreign_row = (await repository.list_artifact_files(foreign["id"]))[0]
        service = ArtifactContentService(repository, storage)

        async def forbidden_storage_read(*_: Any, **__: Any) -> Any:
            raise AssertionError("storage must not be called for an unregistered ID")

        storage.read_text_content_range = forbidden_storage_read  # type: ignore[method-assign]
        with pytest.raises(ArtifactContentError) as file_error:
            await service.read_file(
                artifact,
                "../../artifacts/other/files/file.txt",
                start_line=1,
                line_limit=20,
            )
        with pytest.raises(ArtifactContentError) as diff_error:
            await service.read_diff(
                artifact,
                "artifacts/other/diffs/diff.json",
            )
        repository.files[artifact["id"]][0]["content_key"] = foreign_row["content_key"]
        with pytest.raises(ArtifactContentError) as foreign_key:
            await service.read_file(
                artifact,
                repository.files[artifact["id"]][0]["id"],
                start_line=1,
                line_limit=20,
            )
        return file_error.value.code, diff_error.value.code, foreign_key.value.code

    assert asyncio.run(scenario()) == (
        "artifact_file_not_found",
        "artifact_diff_not_found",
        "artifact_file_sha_changed",
    )


def test_file_content_detects_manifest_change_after_storage_read(tmp_path: Path) -> None:
    async def scenario() -> str:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        artifact = await repository.create_artifact(_artifact_payload(plugin, user, "file-race"))
        artifact = await _seed_manifest(
            repository,
            storage,
            artifact,
            {"main.py": (b"VALUE = 1\n", True)},
        )
        row = (await repository.list_artifact_files(artifact["id"]))[0]
        original_read = storage.read_text_content_range

        async def racing_read(*args: Any, **kwargs: Any) -> Any:
            result = await original_read(*args, **kwargs)
            repository.artifacts[artifact["id"]]["tree_sha256"] = "0" * 64
            return result

        storage.read_text_content_range = racing_read  # type: ignore[method-assign]
        with pytest.raises(ArtifactContentError) as caught:
            await ArtifactContentService(repository, storage).read_file(
                artifact,
                row["id"],
                start_line=1,
                line_limit=20,
            )
        return caught.value.code

    assert asyncio.run(scenario()) == "artifact_file_sha_changed"


def test_diff_content_rejects_unknown_hunk_and_tree_drift(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str, str, str, str, str]:
        repository, user, plugin = _repository_fixture()
        storage = LocalArtifactStorage(tmp_path, "https://cdn.example.test")
        base = await repository.create_artifact(_artifact_payload(plugin, user, "diff-base"))
        current = await repository.create_artifact(
            {
                **_artifact_payload(plugin, user, "diff-current"),
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
        diff = (await repository.list_artifact_diffs(current["id"]))[0]
        service = ArtifactContentService(repository, storage)
        with pytest.raises(ArtifactContentError) as missing_hunk:
            await service.read_diff(current, diff["id"], hunk_id="hunk-999")

        hunk_path = storage.content_root / str(diff["hunks_key"])
        original_hunk = hunk_path.read_bytes()
        hunk_path.write_bytes(original_hunk.replace(b"VALUE = 2", b"VALUE = 3"))
        with pytest.raises(ArtifactContentError) as tampered:
            await service.read_diff(current, diff["id"])
        hunk_path.write_bytes(original_hunk)

        repository.diffs[current["id"]][0]["current_file_id"] = next(
            item["id"]
            for item in await repository.list_artifact_files(base["id"])
            if item["path"] == "main.py"
        )
        with pytest.raises(ArtifactContentError) as cross_side:
            await service.read_diff(current, diff["id"])
        repository.diffs[current["id"]][0]["current_file_id"] = diff["current_file_id"]

        original_read = storage.read_text_content_range

        async def racing_read(*args: Any, **kwargs: Any) -> Any:
            result = await original_read(*args, **kwargs)
            repository.diffs[current["id"]][0]["path"] = "changed-after-read.py"
            return result

        storage.read_text_content_range = racing_read  # type: ignore[method-assign]
        with pytest.raises(ArtifactContentError) as raced:
            await service.read_diff(current, diff["id"])
        storage.read_text_content_range = original_read  # type: ignore[method-assign]
        repository.diffs[current["id"]][0]["path"] = diff["path"]

        with pytest.raises(ArtifactContentError) as response_too_large:
            await ArtifactContentService(
                repository,
                storage,
                ArtifactContentLimits(max_response_bytes=128),
            ).read_diff(current, diff["id"])

        repository.artifacts[current["id"]]["tree_sha256"] = "0" * 64
        with pytest.raises(ArtifactContentError) as stale:
            await service.read_diff(current, diff["id"])

        repository.artifacts[current["id"]]["tree_sha256"] = current["tree_sha256"]
        repository.diffs[current["id"]][0]["hunks_key"] = None
        with pytest.raises(ArtifactContentError) as missing_key:
            await service.read_diff(current, diff["id"])
        return (
            missing_hunk.value.code,
            tampered.value.code,
            cross_side.value.code,
            raced.value.code,
            response_too_large.value.code,
            stale.value.code,
            missing_key.value.code,
        )

    (
        missing_hunk,
        tampered,
        cross_side,
        raced,
        response_too_large,
        stale,
        missing_key,
    ) = asyncio.run(scenario())

    assert missing_hunk == "artifact_diff_hunk_not_found"
    assert tampered == "diff_tree_changed"
    assert cross_side == "diff_tree_changed"
    assert raced == "diff_tree_changed"
    assert response_too_large == "artifact_diff_too_large"
    assert stale == "diff_tree_changed"
    assert missing_key == "diff_tree_changed"


def _repository_fixture() -> tuple[InMemoryArtifactRepository, dict[str, Any], dict[str, Any]]:
    store = InMemoryMarketStore()
    user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
    plugin = store.submit_plugin(
        user,
        {
            "name": "astrbot_plugin_content",
            "display_name": "Content",
            "desc": "Content fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_content",
            "tags": [],
        },
    )
    return InMemoryArtifactRepository(store), user, plugin


def _artifact_payload(
    plugin: Mapping[str, Any],
    user: Mapping[str, Any],
    marker: str,
) -> dict[str, Any]:
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
                "language": "python" if path.endswith(".py") else "text",
                "mime_type": "text/x-python"
                if path.endswith(".py")
                else "application/octet-stream",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "line_count": len(content.decode("utf-8", errors="ignore").splitlines())
                if is_text
                else None,
                "is_text": is_text,
                "content_key": content_key,
            }
        )
    tree_sha256 = manifest_tree_sha256(manifests)
    await repository.replace_artifact_files(str(artifact["id"]), manifests, tree_sha256)
    updated = await repository.get_artifact(str(artifact["id"]))
    assert updated is not None
    return updated


def _assert_no_private_keys(value: Any) -> None:
    forbidden = {"content_key", "hunks_key", "quarantine_key", "published_key"}
    if isinstance(value, Mapping):
        assert forbidden.isdisjoint(value)
        for item in value.values():
            _assert_no_private_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_private_keys(item)
