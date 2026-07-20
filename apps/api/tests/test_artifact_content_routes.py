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


def test_artifact_content_routes_enforce_visibility_binding_limits_and_no_store(
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
            "name": "astrbot_plugin_content_routes",
            "display_name": "Content Routes",
            "desc": "Content route fixture",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_content_routes",
            "tags": [],
        },
    )
    app = create_app(settings=settings, store=store)

    with TestClient(app) as client:
        repository = app.state.artifact_runtime.repository
        storage = app.state.artifact_runtime.storage

        async def seed() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
            base = await repository.create_artifact(_artifact_payload(plugin, owner, "route-base"))
            current = await repository.create_artifact(
                {
                    **_artifact_payload(plugin, owner, "route-current"),
                    "base_artifact_id": base["id"],
                }
            )
            base = await _seed_manifest(
                repository,
                storage,
                base,
                {
                    "main.py": (b"VALUE = 1\n", True),
                    "asset.bin": (b"\x00\x01", False),
                },
            )
            current = await _seed_manifest(
                repository,
                storage,
                current,
                {
                    "main.py": (b"VALUE = 2\n", True),
                    "asset.bin": (b"\x00\x01", False),
                },
            )
            await ArtifactDiffService().build(
                artifact=current,
                repository=repository,
                storage=storage,
            )
            current_files = {
                item["path"]: item for item in await repository.list_artifact_files(current["id"])
            }
            diff = next(
                item
                for item in await repository.list_artifact_diffs(current["id"])
                if item["path"] == "main.py"
            )
            return base, current, current_files, diff

        base, current, files, diff = asyncio.run(seed())
        owner_headers = {"x-dev-github-login": "alice"}
        other_headers = {"x-dev-github-login": "mallory"}

        assert client.get(f"/v1/artifacts/{current['id']}/files").status_code == 401
        assert (
            client.get(
                f"/v1/artifacts/{current['id']}/files",
                headers=other_headers,
            ).status_code
            == 403
        )

        tree = client.get(
            f"/v1/artifacts/{current['id']}/files?limit=1&offset=0",
            headers=owner_headers,
        )
        assert tree.status_code == 200
        assert tree.headers["cache-control"] == "no-store, private"
        assert tree.json()["total"] == 2
        assert len(tree.json()["items"]) == 1

        content = client.get(
            (
                f"/v1/artifacts/{current['id']}/files/{files['main.py']['id']}/content"
                "?start_line=1&line_limit=20&content_key=../../private&path=../../../etc/passwd"
            ),
            headers=owner_headers,
        )
        assert content.status_code == 200
        assert content.headers["cache-control"] == "no-store, private"
        assert content.headers["x-content-type-options"] == "nosniff"
        assert content.json()["lines"] == [{"number": 1, "text": "VALUE = 2"}]

        binary = client.get(
            f"/v1/artifacts/{current['id']}/files/{files['asset.bin']['id']}/content",
            headers=owner_headers,
        )
        invalid_range = client.get(
            (f"/v1/artifacts/{current['id']}/files/{files['main.py']['id']}/content?start_line=0"),
            headers=owner_headers,
        )
        assert binary.status_code == 415
        assert binary.json()["code"] == "artifact_file_not_text"
        assert binary.headers["cache-control"] == "no-store, private"
        assert invalid_range.status_code == 416
        assert invalid_range.json()["code"] == "artifact_content_range_invalid"
        assert invalid_range.headers["cache-control"] == "no-store, private"

        base_file_id = next(
            item["id"]
            for item in asyncio.run(repository.list_artifact_files(base["id"]))
            if item["path"] == "main.py"
        )
        cross_file = client.get(
            f"/v1/artifacts/{current['id']}/files/{base_file_id}/content",
            headers=owner_headers,
        )
        assert cross_file.status_code == 404
        assert cross_file.json()["code"] == "artifact_file_not_found"

        diff_list = client.get(
            f"/v1/artifacts/{current['id']}/diff",
            headers=owner_headers,
        )
        diff_detail = client.get(
            f"/v1/artifacts/{current['id']}/diff/{diff['id']}",
            headers=owner_headers,
        )
        cross_diff = client.get(
            f"/v1/artifacts/{base['id']}/diff/{diff['id']}",
            headers=owner_headers,
        )
        assert diff_list.status_code == 200
        assert diff_detail.status_code == 200
        assert diff_detail.headers["cache-control"] == "no-store, private"
        assert diff_detail.json()["hunks"]
        assert cross_diff.status_code == 404
        assert cross_diff.json()["code"] == "artifact_diff_not_found"

        reviewer = store.upsert_github_user({"id": "200", "login": "reviewer", "name": "Reviewer"})
        store.update_user_role(reviewer["id"], "admin")
        admin_tree = client.get(
            f"/v1/artifacts/{current['id']}/files",
            headers={"x-dev-github-login": "reviewer"},
        )
        assert admin_tree.status_code == 200

        for payload in (tree.json(), content.json(), diff_list.json(), diff_detail.json()):
            _assert_no_private_keys(payload)


def _artifact_payload(
    plugin: Mapping[str, Any],
    user: Mapping[str, Any],
    marker: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(marker.encode()).hexdigest()
    return {
        "plugin_id": plugin["id"],
        "version": "1.0.0",
        "normalized_version": "1.0.0",
        "source_type": "upload",
        "source_repo": plugin["repo"],
        "archive_sha256": digest,
        "size_bytes": 128,
        "quarantine_key": f"quarantine/{digest[:12]}.zip",
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
                "language": "python" if path.endswith(".py") else "binary",
                "mime_type": "text/x-python" if is_text else "application/octet-stream",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "line_count": len(content.decode().splitlines()) if is_text else None,
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
