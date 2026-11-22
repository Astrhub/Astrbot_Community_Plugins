from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.artifacts.archive import ArchivePrechecker, PrecheckError
from app.auth import Role
from app.config import load_settings
from app.store import InMemoryMarketStore
from tests.github_access_helpers import install_github_access_double

RESERVED_RECORD_FIELDS = {
    "plugin_id",
    "market_plugin_id",
    "market_plugin_identifier",
    "root_dir_name",
    "local_plugin_name",
    "install_method",
    "registry_url",
    "registry_name",
    "installed_at",
}
DEPRECATED_RECORD_FIELDS = {"support_platform", "platform"}


def make_client(**env_overrides: str) -> TestClient:
    settings = load_settings(
        {
            "ENABLE_DEV_AUTH": "true",
            "GITHUB_METADATA_SYNC_ENABLED": "true",
            "PLUGIN_AUTO_APPROVE_ENABLED": "true",
            **env_overrides,
        }
    )
    app = main_module.create_app(settings=settings, store=InMemoryMarketStore())
    install_github_access_double(app)
    return TestClient(app)


def test_plugins_json_follows_astrbot_2026_06_27_schema_and_stable_md5(monkeypatch) -> None:
    client = make_client(
        SITE_NAME="Astrhub Community",
        SITE_DESCRIPTION="Community plugin market",
        WEB_URL="https://plugins.eloina.cn/",
    )
    client.get("/v1/auth/debug-login?login=alice")

    metadata_yaml = "\n".join(
        [
            "name: astrbot_plugin_weather",
            "display_name: Weather Helper",
            "short_desc: Compact weather summary",
            "desc: Full weather plugin description",
            "author: Alice",
            "version: 1.4.2",
            "repo: https://github.com/alice/astrbot_plugin_weather",
            "astrbot_version: '>=4.26.2'",
            "support_platforms:",
            "  - aiocqhttp",
            "tags: [weather, daily]",
        ]
    )
    zh_i18n = {
        "metadata": {
            "display_name": "天气助手",
            "short_desc": "一句话查天气",
            "desc": "查询天气并提供出行建议",
        },
        "config": {"api_key": {"description": "不应进入市场 feed"}},
        "views": {"settings": {"title": "不应进入市场 feed"}},
    }
    en_i18n = {
        "metadata": {
            "display_name": "Weather Helper",
            "short_desc": "Quick weather lookup",
        }
    }

    class FakeResponse:
        def __init__(self, status_code: int, data: object) -> None:
            self.status_code = status_code
            self._data = data
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if url == "https://api.github.com/repos/alice/astrbot_plugin_weather":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 27,
                        "updated_at": "2026-11-20T08:30:00Z",
                        "default_branch": "main",
                    },
                )
            if url.endswith("/contents/metadata.yaml"):
                return FakeResponse(
                    200,
                    {"content": base64.b64encode(metadata_yaml.encode("utf-8")).decode("ascii")},
                )
            if url.endswith("/contents"):
                return FakeResponse(
                    200,
                    [
                        {"name": "metadata.yaml", "type": "file"},
                        {"name": "README.md", "type": "file"},
                        {"name": "CHANGELOG.md", "type": "file"},
                        {"name": "logo.png", "type": "file"},
                        {"name": ".astrbot-plugin", "type": "dir"},
                    ],
                )
            if url.endswith("/contents/.astrbot-plugin/i18n"):
                return FakeResponse(
                    200,
                    [
                        {"name": "zh-CN.json", "type": "file", "size": 256},
                        {"name": "en-US.json", "type": "file", "size": 128},
                        {"name": "empty.json", "type": "file", "size": 32},
                        {"name": "README.txt", "type": "file", "size": 32},
                    ],
                )
            if url.endswith("/contents/.astrbot-plugin/i18n/zh-CN.json"):
                encoded = base64.b64encode(
                    b"\xef\xbb\xbf" + json.dumps(zh_i18n, ensure_ascii=False).encode("utf-8")
                ).decode("ascii")
                return FakeResponse(200, {"content": encoded})
            if url.endswith("/contents/.astrbot-plugin/i18n/en-US.json"):
                encoded = base64.b64encode(json.dumps(en_i18n).encode("utf-8")).decode("ascii")
                return FakeResponse(200, {"content": encoded})
            if url.endswith("/contents/.astrbot-plugin/i18n/empty.json"):
                encoded = base64.b64encode(b'{"config": {"k": {"description": "only"}}}').decode(
                    "ascii"
                )
                return FakeResponse(200, {"content": encoded})
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    submitted = client.post(
        "/v1/plugins/submissions",
        json={
            "cdn_enabled": False,
            "name": "astrbot_plugin_weather",
            "display_name": "Weather Helper",
            "desc": "Full weather plugin description",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_weather",
            "social_link": "https://github.com/alice",
            "category": "utilities",
            "tags": ["weather"],
        },
    )
    assert submitted.status_code == 201
    refreshed = client.post(f"/v1/plugins/{submitted.json()['id']}/refresh-github", json={})
    assert refreshed.status_code == 200

    feed = client.get("/plugins.json").json()
    assert list(feed) == ["$meta", "Alice/astrbot_plugin_weather"]
    assert feed["$meta"] == {
        "schema_version": 1,
        "version": "2026-06-27",
        "name": "Astrhub Community",
        "description": "Community plugin market",
        "homepage": "https://plugins.eloina.cn",
        "updated_at": feed["Alice/astrbot_plugin_weather"]["updated_at"],
    }

    entry = feed["Alice/astrbot_plugin_weather"]
    assert RESERVED_RECORD_FIELDS.isdisjoint(entry)
    assert DEPRECATED_RECORD_FIELDS.isdisjoint(entry)
    assert entry["name"] == "astrbot_plugin_weather"
    assert entry["author"] == "Alice"
    assert entry["version"] == "1.4.2"
    assert entry["repo"] == "https://github.com/alice/astrbot_plugin_weather"
    assert entry["stars"] == 27
    assert (
        entry["logo"]
        == "https://raw.githubusercontent.com/alice/astrbot_plugin_weather/main/logo.png"
    )
    assert (
        entry["readme_url"]
        == "https://raw.githubusercontent.com/alice/astrbot_plugin_weather/main/README.md"
    )
    assert (
        entry["changelog_url"]
        == "https://raw.githubusercontent.com/alice/astrbot_plugin_weather/main/CHANGELOG.md"
    )
    assert entry["i18n"] == {
        "en-US": {
            "metadata": {
                "display_name": "Weather Helper",
                "short_desc": "Quick weather lookup",
            }
        },
        "zh-CN": {
            "metadata": {
                "display_name": "天气助手",
                "desc": "查询天气并提供出行建议",
                "short_desc": "一句话查天气",
            }
        },
    }
    assert "download_url" not in entry

    # Repeated calls keep a deterministic MD5 across both root and v1 endpoints.
    first_md5 = client.get("/plugins-md5.json").json()["md5"]
    second_md5 = client.get("/plugins-md5.json").json()["md5"]
    v1_md5 = client.get("/v1/astrbot/plugins-md5.json").json()["md5"]
    assert first_md5 == second_md5 == v1_md5


def test_github_sync_clears_removed_readme_changelog_logo_and_i18n(monkeypatch) -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice").json()
    client.app.state.store.update_user_role(login["user"]["id"], Role.ADMIN.value)
    has_optional_files = {"enabled": True}

    class FakeResponse:
        def __init__(self, status_code: int, data: object) -> None:
            self.status_code = status_code
            self._data = data
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if url == "https://api.github.com/repos/alice/astrbot_plugin_docs":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 3,
                        "updated_at": "2026-11-20T08:30:00Z",
                        "default_branch": "main",
                    },
                )
            if url.endswith("/contents/metadata.yaml"):
                text = "name: astrbot_plugin_docs\ndesc: Docs\nauthor: Alice\nversion: 1.0.0\n"
                return FakeResponse(
                    200, {"content": base64.b64encode(text.encode()).decode("ascii")}
                )
            if url.endswith("/contents"):
                items = [{"name": "metadata.yaml", "type": "file"}]
                if has_optional_files["enabled"]:
                    items.extend(
                        [
                            {"name": "logo.png", "type": "file"},
                            {"name": "readme.md", "type": "file"},
                            {"name": "changelog.md", "type": "file"},
                            {"name": ".astrbot-plugin", "type": "dir"},
                        ]
                    )
                return FakeResponse(200, items)
            if url.endswith("/contents/.astrbot-plugin/i18n"):
                return FakeResponse(200, [{"name": "en-US.json", "type": "file", "size": 64}])
            if url.endswith("/contents/.astrbot-plugin/i18n/en-US.json"):
                raw = json.dumps({"metadata": {"display_name": "Docs"}}).encode()
                return FakeResponse(200, {"content": base64.b64encode(raw).decode("ascii")})
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    created = client.post(
        "/v1/plugins/submissions",
        json={
            "cdn_enabled": False,
            "name": "astrbot_plugin_docs",
            "desc": "Docs",
            "author": "Alice",
            "repo": "https://github.com/alice/astrbot_plugin_docs",
        },
    ).json()
    entry = client.get("/plugins.json").json()["Alice/astrbot_plugin_docs"]
    assert entry["readme_url"].endswith("/main/readme.md")
    assert entry["changelog_url"].endswith("/main/changelog.md")
    assert entry["logo"].endswith("/main/logo.png")
    assert "en-US" in entry["i18n"]

    has_optional_files["enabled"] = False
    refreshed = client.post(f"/v1/plugins/{created['id']}/refresh-github", json={})
    assert refreshed.status_code == 200
    after = client.get("/plugins.json").json()["Alice/astrbot_plugin_docs"]
    assert "readme_url" not in after
    assert "changelog_url" not in after
    assert "logo" not in after
    assert "i18n" not in after


def test_author_validation_rejects_slash_and_control_chars_and_enforces_case_insensitive_uniqueness(
    tmp_path: Path,
) -> None:
    client = make_client(GITHUB_METADATA_SYNC_ENABLED="false")
    client.get("/v1/auth/debug-login?login=alice")

    for bad_author in ("alice/team", "alice\x00bot", "alice\nbot"):
        response = client.post(
            "/v1/plugins/submissions",
            json={
                "cdn_enabled": False,
                "name": "astrbot_plugin_bad_author",
                "desc": "desc",
                "author": bad_author,
                "repo": "https://github.com/alice/astrbot_plugin_bad_author",
            },
        )
        assert response.status_code == 400

    first = client.post(
        "/v1/plugins/submissions",
        json={
            "cdn_enabled": False,
            "name": "astrbot_plugin_unique",
            "desc": "desc",
            "author": "SharedTeam",
            "repo": "https://github.com/alice/astrbot_plugin_unique",
        },
    )
    assert first.status_code == 201
    plugin_id = first.json()["id"]

    bad_patch = client.patch(f"/v1/plugins/{plugin_id}", json={"author": "Shared/Team"})
    assert bad_patch.status_code == 400

    client.get("/v1/auth/debug-login?login=bob")
    conflict = client.post(
        "/v1/plugins/submissions",
        json={
            "cdn_enabled": False,
            "name": "astrbot_plugin_unique",
            "desc": "desc",
            "author": "sharedteam",
            "repo": "https://github.com/bob/astrbot_plugin_unique",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "plugin_identity_conflict"

    # Archive precheck also rejects '/' in metadata.yaml author.
    archive_path = tmp_path / "bad_author.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "metadata.yaml",
            "\n".join(
                [
                    "name: astrbot_plugin_bad_author",
                    "desc: desc",
                    "version: 1.0.0",
                    "author: bad/author",
                    "repo: https://github.com/alice/astrbot_plugin_bad_author",
                ]
            ),
        )
        archive.writestr("main.py", "print('ok')\n")
    with pytest.raises(PrecheckError) as exc_info:
        ArchivePrechecker(load_settings().artifacts).inspect(
            archive_path,
            expected_repo="https://github.com/alice/astrbot_plugin_bad_author",
        )
    assert exc_info.value.code == "plugin_author_invalid"


def test_build_astrbot_plugin_source_dedupes_case_insensitive_ids_and_omits_non_https_urls() -> (
    None
):
    feed = main_module.build_astrbot_plugin_source(
        [
            {
                "id": "p1",
                "name": "astrbot_plugin_demo",
                "author": "Alice",
                "desc": "First",
                "repo": "https://github.com/alice/astrbot_plugin_demo/",
                "social_link": "http://insecure.example.test",
                "readme_url": "http://insecure.example.test/README.md",
                "changelog_url": "https://raw.githubusercontent.com/alice/astrbot_plugin_demo/main/CHANGELOG.md",
                "version": "",
                "created_at": "2026-11-01T00:00:00Z",
                "updated_at": "2026-11-01T00:00:00Z",
            },
            {
                "id": "p2",
                "name": "astrbot_plugin_demo",
                "author": "alice",
                "desc": "Duplicate lower id",
                "repo": "https://github.com/alice2/astrbot_plugin_demo",
                "created_at": "2026-11-02T00:00:00Z",
                "updated_at": "2026-11-02T00:00:00Z",
            },
            {
                "id": "p3",
                "name": "astrbot_plugin_bad",
                "author": "bad/author",
                "desc": "Invalid author",
                "repo": "https://github.com/bad/astrbot_plugin_bad",
            },
        ]
    )
    assert list(feed) == ["$meta", "Alice/astrbot_plugin_demo"]
    entry = feed["Alice/astrbot_plugin_demo"]
    assert entry["repo"] == "https://github.com/alice/astrbot_plugin_demo"
    assert "social_link" not in entry
    assert "readme_url" not in entry
    assert entry["changelog_url"].startswith("https://")
    assert entry["version"] == ""


def test_malformed_https_url_and_non_string_author_are_handled_safely() -> None:
    assert main_module.is_https_url("https://[") is False
    assert main_module.is_valid_plugin_author(123) is False
    assert main_module.is_valid_plugin_author(["Alice"]) is False

    feed = main_module.build_astrbot_plugin_source(
        [
            {
                "id": "p-malformed",
                "name": "astrbot_plugin_links",
                "author": "Alice",
                "desc": "Malformed URL test",
                "repo": "https://github.com/alice/astrbot_plugin_links",
                "social_link": "https://[",
                "readme_url": "https://[::1",
                "changelog_url": "https://raw.githubusercontent.com/alice/astrbot_plugin_links/main/CHANGELOG.md",
            }
        ]
    )
    entry = feed["Alice/astrbot_plugin_links"]
    assert "social_link" not in entry
    assert "readme_url" not in entry
    assert entry["changelog_url"].endswith("/CHANGELOG.md")


def test_github_sync_preserves_i18n_on_transient_errors_and_skips_deep_json(monkeypatch) -> None:
    client = make_client()
    login = client.get("/v1/auth/debug-login?login=alice").json()
    client.app.state.store.update_user_role(login["user"]["id"], Role.ADMIN.value)
    mode = {"i18n_dir": "ok", "en_file": "ok", "include_deep": False}

    class FakeResponse:
        def __init__(self, status_code: int, data: object) -> None:
            self.status_code = status_code
            self._data = data
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if url == "https://api.github.com/repos/alice/astrbot_plugin_i18n_guard":
                return FakeResponse(
                    200,
                    {
                        "stargazers_count": 5,
                        "updated_at": "2026-11-20T08:30:00Z",
                        "default_branch": "main",
                    },
                )
            if url.endswith("/contents/metadata.yaml"):
                text = (
                    "name: astrbot_plugin_i18n_guard\ndesc: Guard\nauthor: Alice\nversion: 1.0.0\n"
                )
                return FakeResponse(
                    200, {"content": base64.b64encode(text.encode()).decode("ascii")}
                )
            if url.endswith("/contents"):
                return FakeResponse(
                    200,
                    [
                        {"name": "metadata.yaml", "type": "file"},
                        {"name": ".astrbot-plugin", "type": "dir"},
                    ],
                )
            if url.endswith("/contents/.astrbot-plugin/i18n"):
                if mode["i18n_dir"] == "error":
                    return FakeResponse(502, {"message": "bad gateway"})
                if mode["i18n_dir"] == "missing":
                    return FakeResponse(404, {"message": "Not Found"})
                entries = [{"name": "en-US.json", "type": "file", "size": 64}]
                if mode["include_deep"]:
                    entries.append({"name": "ja-JP.json", "type": "file", "size": 4096})
                return FakeResponse(200, entries)
            if url.endswith("/contents/.astrbot-plugin/i18n/en-US.json"):
                if mode["en_file"] == "error":
                    return FakeResponse(503, {"message": "service unavailable"})
                raw = json.dumps({"metadata": {"display_name": "Guard"}}).encode()
                return FakeResponse(200, {"content": base64.b64encode(raw).decode("ascii")})
            if url.endswith("/contents/.astrbot-plugin/i18n/ja-JP.json"):
                deep = ("[" * 2500) + ("]" * 2500)
                return FakeResponse(
                    200, {"content": base64.b64encode(deep.encode()).decode("ascii")}
                )
            return FakeResponse(404, {})

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeAsyncClient)

    created = client.post(
        "/v1/plugins/submissions",
        json={
            "cdn_enabled": False,
            "name": "astrbot_plugin_i18n_guard",
            "desc": "Guard",
            "author": "\u3000Alice\u3000",
            "repo": "https://github.com/alice/astrbot_plugin_i18n_guard",
        },
    )
    assert created.status_code == 201
    plugin_id = created.json()["id"]
    assert created.json()["author"] == "Alice"
    assert "en-US" in client.get("/plugins.json").json()["Alice/astrbot_plugin_i18n_guard"]["i18n"]

    # Transient directory 502 keeps previously stored translations.
    mode["i18n_dir"] = "error"
    assert client.post(f"/v1/plugins/{plugin_id}/refresh-github", json={}).status_code == 200
    assert "en-US" in client.get("/plugins.json").json()["Alice/astrbot_plugin_i18n_guard"]["i18n"]

    # Transient file 503 keeps previously stored translations.
    mode["i18n_dir"] = "ok"
    mode["en_file"] = "error"
    assert client.post(f"/v1/plugins/{plugin_id}/refresh-github", json={}).status_code == 200
    assert "en-US" in client.get("/plugins.json").json()["Alice/astrbot_plugin_i18n_guard"]["i18n"]

    # Deeply nested JSON triggers RecursionError and is skipped without failing refresh.
    mode["en_file"] = "ok"
    mode["include_deep"] = True
    assert client.post(f"/v1/plugins/{plugin_id}/refresh-github", json={}).status_code == 200
    assert list(client.get("/plugins.json").json()["Alice/astrbot_plugin_i18n_guard"]["i18n"]) == [
        "en-US"
    ]

    # Genuine 404 on .astrbot-plugin/i18n clears stored translations.
    mode["i18n_dir"] = "missing"
    assert client.post(f"/v1/plugins/{plugin_id}/refresh-github", json={}).status_code == 200
    assert "i18n" not in client.get("/plugins.json").json()["Alice/astrbot_plugin_i18n_guard"]
