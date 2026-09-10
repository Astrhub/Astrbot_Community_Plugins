import asyncio
import base64
import zipfile

import httpx
import pytest

import app.main as market
from app.artifacts.archive import ArchivePrechecker, PrecheckError
from app.artifacts.schemas import PluginRegistrationPayload
from app.config import load_settings
from app.store import InMemoryMarketStore


def test_preview_reads_only_metadata_file_and_keeps_absent_fields_empty(monkeypatch):
    paths = []
    metadata = "name: astrbot_plugin_demo\ndescription: |\n  第一行包含 # 正文\n  第二行描述\n"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, url, **kwargs):
            paths.append(url)
            assert url.endswith("/contents/metadata.yaml"), (
                "Repository topics/about must not supply metadata"
            )
            return httpx.Response(
                200, json={"content": base64.b64encode(metadata.encode()).decode()}
            )

    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **kwargs: Client())
    preview = asyncio.run(
        market.fetch_plugin_submission_metadata_preview(
            "https://github.com/org/astrbot_plugin_demo", load_settings({}), {"id": "alice"}
        )
    )
    assert preview == {
        "repo": "https://github.com/org/astrbot_plugin_demo",
        "name": "astrbot_plugin_demo",
        "desc": "第一行包含 # 正文\n第二行描述",
    }
    assert len(paths) == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("description: 兼容描述", "兼容描述"),
        ("desc: 主描述\ndescription: 兼容描述", "主描述"),
        ("desc: ''\ndescription: 兼容描述", ""),
        ("desc: null\ndescription: 兼容描述", None),
    ],
)
def test_description_alias_matches_astrbot_key_presence(text, expected):
    assert market.parse_plugin_metadata_yaml(text)["desc"] == expected


def test_optional_metadata_is_not_synthesized_from_other_fields():
    metadata = market.parse_plugin_metadata_yaml(
        "name: astrbot_plugin_demo\nshort_desc: 卡片文字\n"
    )
    preview = market.build_plugin_submission_metadata_preview(
        "org", "astrbot_plugin_demo", metadata=metadata
    )
    assert preview == {
        "repo": "https://github.com/org/astrbot_plugin_demo",
        "name": "astrbot_plugin_demo",
        "short_desc": "卡片文字",
    }
    assert market.build_plugin_submission_metadata_preview("org", "astrbot_plugin_demo") == {
        "repo": "https://github.com/org/astrbot_plugin_demo"
    }
    assert (
        market.build_plugin_submission_metadata_preview(
            "Soulter", "helloworld", metadata={"name": "helloworld"}
        )["name"]
        == "helloworld"
    )
    assert market.normalize_plugin_metadata_field("desc", {"nested": "not text"}) == ""
    assert market.normalize_plugin_metadata_field("category", None) == ""


def test_yaml_nested_keys_do_not_override_top_level_and_long_description_is_preserved():
    text = "desc: " + "完整描述" * 80 + "\nnested:\n  desc: 不应覆盖\n  tags: [不应读取]\n"
    preview = market.build_plugin_submission_metadata_preview(
        "org", "astrbot_plugin_demo", metadata=market.parse_plugin_metadata_yaml(text)
    )
    assert preview["desc"] == "完整描述" * 80
    assert "tags" not in preview


def test_archive_accepts_description_alias_without_optional_display_name(tmp_path):
    text = "\n".join(
        [
            "name: astrbot_plugin_demo",
            "description: 插件描述",
            "version: v1.0.0",
            "author: Alice",
            "repo: https://github.com/org/astrbot_plugin_demo",
        ]
    )
    path = tmp_path / "plugin.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("metadata.yaml", text)
        archive.writestr("main.py", "# metadata compatibility fixture\n")
    result = ArchivePrechecker(load_settings({}).artifacts).inspect(
        path, expected_repo="https://github.com/org/astrbot_plugin_demo"
    )
    assert result.metadata["desc"] == "插件描述"
    assert "display_name" not in result.metadata
    payload = PluginRegistrationPayload(
        name="astrbot_plugin_demo",
        desc="插件描述",
        author="Alice",
        repo="https://github.com/org/astrbot_plugin_demo",
    )
    store = InMemoryMarketStore()
    plugin = store.register_plugin({"id": "alice", "github_login": "Alice"}, payload.model_dump())
    assert plugin["display_name"] == ""


def test_archive_rejects_non_string_core_fields(tmp_path):
    path = tmp_path / "invalid.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "metadata.yaml",
            "name: astrbot_plugin_demo\ndesc: [not, text]\nversion: v1.0.0\nauthor: Alice\nrepo: https://github.com/org/astrbot_plugin_demo\n",
        )
        archive.writestr("main.py", "# fixture\n")
    with pytest.raises(PrecheckError, match="desc"):
        ArchivePrechecker(load_settings({}).artifacts).inspect(
            path, expected_repo="https://github.com/org/astrbot_plugin_demo"
        )
