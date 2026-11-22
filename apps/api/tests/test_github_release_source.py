from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import main
from app.artifacts.archive import ArchivePrechecker, PrecheckError
from app.artifacts.github_source import (
    GithubSourceClient,
    GithubSourceError,
    ResolvedGithubSource,
    select_release_zip_asset,
)
from app.artifacts.jobs import ArtifactJobRunner
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.service import ArtifactService, ArtifactServiceError
from app.artifacts.static_scan import StaticScanner
from app.artifacts.storage import LocalArtifactStorage
from app.config import load_settings
from app.store import InMemoryMarketStore
from test_artifact_pipeline import byte_stream


COMMIT_SHA = "a" * 40


def _versioned_plugin_zip(
    *,
    version: str = "v1.0.0",
    main_source: str = "print('safe')\n",
) -> bytes:
    metadata = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "display_name: Demo",
            "desc: Demo plugin",
            f"version: {version}",
            "author: Alice",
            "repo: https://github.com/alice/astrbot_plugin_demo",
            "",
        ]
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.yaml", metadata)
        archive.writestr("main.py", main_source)
    return output.getvalue()


def test_github_source_prefers_release_zip_asset_then_tag_then_default_branch() -> None:
    def make_client(routes: dict[str, tuple[int, Any]]) -> GithubSourceClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            status, payload = routes.get(path, (404, {"message": "Not Found"}))
            return httpx.Response(status, json=payload)

        return GithubSourceClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def run() -> tuple[ResolvedGithubSource, ResolvedGithubSource, ResolvedGithubSource]:
        repo_path = "/repos/alice/astrbot_plugin_demo"
        asset_url = (
            "https://github.com/alice/astrbot_plugin_demo/releases/download/"
            "v1.2.0/astrbot_plugin_demo-v1.2.0.zip"
        )
        c1 = make_client(
            {
                repo_path: (200, {"private": False, "default_branch": "main"}),
                f"{repo_path}/releases/latest": (
                    200,
                    {
                        "tag_name": "v1.2.0",
                        "draft": False,
                        "prerelease": False,
                        "assets": [
                            {
                                "name": "checksums.txt",
                                "state": "uploaded",
                                "browser_download_url": (
                                    "https://github.com/alice/astrbot_plugin_demo/releases/"
                                    "download/v1.2.0/checksums.txt"
                                ),
                            },
                            {
                                "name": "astrbot_plugin_demo-v1.2.0.zip",
                                "state": "uploaded",
                                "browser_download_url": asset_url,
                            },
                        ],
                    },
                ),
                f"{repo_path}/commits/v1.2.0": (200, {"sha": COMMIT_SHA}),
            }
        )
        c2 = make_client(
            {
                repo_path: (200, {"private": False, "default_branch": "main"}),
                f"{repo_path}/releases/latest": (
                    200,
                    {
                        "tag_name": "v1.2.0",
                        "draft": False,
                        "prerelease": False,
                        "assets": [],
                    },
                ),
                f"{repo_path}/commits/v1.2.0": (200, {"sha": COMMIT_SHA}),
            }
        )
        c3 = make_client(
            {
                repo_path: (200, {"private": False, "default_branch": "main"}),
                f"{repo_path}/commits/main": (200, {"sha": COMMIT_SHA}),
            }
        )
        try:
            r1 = await c1.resolve("https://github.com/alice/astrbot_plugin_demo")
            r2 = await c2.resolve("https://github.com/alice/astrbot_plugin_demo")
            r3 = await c3.resolve("https://github.com/alice/astrbot_plugin_demo")
            return r1, r2, r3
        finally:
            await c1.close()
            await c2.close()
            await c3.close()

    with_asset, tag_only, branch_fallback = asyncio.run(run())

    assert with_asset.release_tag == "v1.2.0"
    assert with_asset.requested_ref == "v1.2.0"
    assert with_asset.release_asset_name == "astrbot_plugin_demo-v1.2.0.zip"
    assert with_asset.archive_url.endswith(
        "/releases/download/v1.2.0/astrbot_plugin_demo-v1.2.0.zip"
    )

    assert tag_only.release_tag == "v1.2.0"
    assert tag_only.download_url == ""
    assert (
        tag_only.archive_url
        == f"https://codeload.github.com/alice/astrbot_plugin_demo/zip/{COMMIT_SHA}"
    )

    assert branch_fallback.release_tag == ""
    assert branch_fallback.requested_ref == "main"
    assert (
        branch_fallback.archive_url
        == f"https://codeload.github.com/alice/astrbot_plugin_demo/zip/{COMMIT_SHA}"
    )


def test_select_release_zip_asset_disambiguates_and_rejects_untrusted_or_ambiguous() -> None:
    selected = select_release_zip_asset(
        {
            "tag_name": "v1.0.0",
            "assets": [
                {
                    "name": "evil.zip",
                    "state": "uploaded",
                    "browser_download_url": "https://evil.example/evil.zip",
                },
                {
                    "name": "screenshots.zip",
                    "state": "uploaded",
                    "browser_download_url": (
                        "https://github.com/alice/astrbot_plugin_demo/releases/download/"
                        "v1.0.0/screenshots.zip"
                    ),
                },
                {
                    "name": "astrbot_plugin_demo-v1.0.0.zip",
                    "state": "uploaded",
                    "browser_download_url": (
                        "https://github.com/alice/astrbot_plugin_demo/releases/download/"
                        "v1.0.0/astrbot_plugin_demo-v1.0.0.zip"
                    ),
                },
            ],
        },
        owner="alice",
        repo_name="astrbot_plugin_demo",
    )
    assert selected is not None
    assert selected[1] == "astrbot_plugin_demo-v1.0.0.zip"

    with pytest.raises(GithubSourceError) as ambiguous:
        select_release_zip_asset(
            {
                "tag_name": "v1.0.0",
                "assets": [
                    {
                        "name": "pkg-a.zip",
                        "state": "uploaded",
                        "browser_download_url": (
                            "https://github.com/alice/astrbot_plugin_demo/releases/download/"
                            "v1.0.0/pkg-a.zip"
                        ),
                    },
                    {
                        "name": "pkg-b.zip",
                        "state": "uploaded",
                        "browser_download_url": (
                            "https://github.com/alice/astrbot_plugin_demo/releases/download/"
                            "v1.0.0/pkg-b.zip"
                        ),
                    },
                ],
            },
            owner="alice",
            repo_name="astrbot_plugin_demo",
        )
    assert ambiguous.value.code == "github_release_asset_ambiguous"


def test_resolve_cdn_source_prefers_latest_release_and_falls_back_on_404(monkeypatch) -> None:
    app = main.create_app(load_settings({}), InMemoryMarketStore())
    repository = {"full_name": "alice/astrbot_plugin_demo", "default_branch": "main"}

    async def scenario() -> tuple[ResolvedGithubSource, ResolvedGithubSource]:
        async def with_release(_app, _repo, *, suffix="", allow_not_found=False):
            del _app, _repo, allow_not_found
            if suffix == "/releases/latest":
                return {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "astrbot_plugin_demo-v2.0.0.zip",
                            "state": "uploaded",
                            "browser_download_url": (
                                "https://github.com/alice/astrbot_plugin_demo/releases/download/"
                                "v2.0.0/astrbot_plugin_demo-v2.0.0.zip"
                            ),
                        }
                    ],
                }
            if suffix == "/commits/v2.0.0":
                return {"sha": COMMIT_SHA}
            raise AssertionError(f"unexpected suffix: {suffix}")

        monkeypatch.setattr(main, "_fetch_cdn_github_json_request", with_release)
        first = await main.resolve_cdn_source(app, repository)

        async def without_release(_app, _repo, *, suffix="", allow_not_found=False):
            del _app, _repo
            if suffix == "/releases/latest" and allow_not_found:
                return None
            if suffix == "/commits/main":
                return {"sha": COMMIT_SHA}
            raise AssertionError(f"unexpected suffix: {suffix}")

        monkeypatch.setattr(main, "_fetch_cdn_github_json_request", without_release)
        second = await main.resolve_cdn_source(app, repository)
        return first, second

    release_source, branch_source = asyncio.run(scenario())
    assert release_source.release_tag == "v2.0.0"
    assert release_source.archive_url.endswith("astrbot_plugin_demo-v2.0.0.zip")
    assert branch_source.release_tag == ""
    assert branch_source.requested_ref == "main"


def test_precheck_rejects_mismatched_or_invalid_release_version_and_accepts_normalized_match(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "pkg.zip"
    archive_path.write_bytes(_versioned_plugin_zip(version="1.2.0"))
    prechecker = ArchivePrechecker(load_settings({}).artifacts)

    matched = prechecker.inspect(
        archive_path,
        expected_repo="https://github.com/alice/astrbot_plugin_demo",
        expected_release_tag="v1.2.0",
    )
    assert matched.normalized_version == "1.2.0"

    with pytest.raises(PrecheckError) as mismatch:
        prechecker.inspect(
            archive_path,
            expected_repo="https://github.com/alice/astrbot_plugin_demo",
            expected_release_tag="v1.3.0",
        )
    assert mismatch.value.code == "release_version_mismatch"

    with pytest.raises(PrecheckError) as invalid_tag:
        prechecker.inspect(
            archive_path,
            expected_repo="https://github.com/alice/astrbot_plugin_demo",
            expected_release_tag="latest",
        )
    assert invalid_tag.value.code == "release_version_mismatch"


def test_github_release_version_mismatch_auto_rejects_in_precheck_and_blocks_dedupe_reuse(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict[str, Any], str]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": [],
                "category": "other",
            },
        )
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root, settings.artifacts.cdn_base_url
        )
        github = GithubSourceClient()
        mismatched_zip = _versioned_plugin_zip(version="v1.0.0")
        matching_zip = _versioned_plugin_zip(version="v2.0.0", main_source="VALUE = 2\n")
        current_bytes = mismatched_zip
        github.stream_archive = lambda _src: byte_stream(current_bytes)  # type: ignore[method-assign]
        service = ArtifactService(
            repository=repository,
            storage=storage,
            github=github,
            max_upload_bytes=settings.artifacts.max_upload_bytes,
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=ArchivePrechecker(settings.artifacts),
            scanner=StaticScanner(),
            worker_id="test-worker",
            lease_seconds=60,
            poll_seconds=1,
        )
        try:
            mismatch_source = ResolvedGithubSource(
                repo_url=plugin["repo"],
                owner="alice",
                repo_name="astrbot_plugin_demo",
                requested_ref="v1.1.0",
                commit_sha=COMMIT_SHA,
                download_url=(
                    "https://github.com/alice/astrbot_plugin_demo/releases/download/v1.1.0/demo.zip"
                ),
                release_tag="v1.1.0",
                release_asset_name="demo.zip",
            )
            submitted = await service.submit_github(
                plugin=plugin,
                user=owner,
                source_ref="v1.1.0",
                resolved_source=mismatch_source,
            )
            assert await runner.run_once() == 1
            rejected = await repository.get_artifact(submitted["id"])
            assert rejected is not None

            current_bytes = matching_zip
            valid_source = ResolvedGithubSource(
                repo_url=plugin["repo"],
                owner="alice",
                repo_name="astrbot_plugin_demo",
                requested_ref="v2.0.0",
                commit_sha=COMMIT_SHA,
                release_tag="v2.0.0",
            )
            ok = await service.submit_github(
                plugin=plugin,
                user=owner,
                source_ref="v2.0.0",
                resolved_source=valid_source,
            )
            assert await runner.run_once() == 1
            assert await runner.run_once() == 1
            ok_artifact = await repository.get_artifact(ok["id"])
            assert ok_artifact and ok_artifact["review_status"] == "pending_review"

            reused_wrong_tag = ResolvedGithubSource(
                repo_url=plugin["repo"],
                owner="alice",
                repo_name="astrbot_plugin_demo",
                requested_ref="v2.0.1",
                commit_sha=COMMIT_SHA,
                release_tag="v2.0.1",
            )
            with pytest.raises(ArtifactServiceError) as reused_error:
                await service.submit_github(
                    plugin=plugin,
                    user=owner,
                    source_ref="v2.0.1",
                    resolved_source=reused_wrong_tag,
                )
            return rejected, reused_error.value.code
        finally:
            await service.close()

    rejected, dedupe_error_code = asyncio.run(scenario())
    assert rejected["review_status"] == "rejected"
    assert rejected["rejection_code"] == "release_version_mismatch"
    assert dedupe_error_code == "release_version_mismatch"
