from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import cache_control_for_path, create_app
from app.store import InMemoryMarketStore


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "plugin" / "Alice" / "astrbot_plugin_demo").mkdir(parents=True)
    (dist / "index.html").write_text("<html>market</html>", encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    (dist / "plugin" / "Alice" / "astrbot_plugin_demo" / "index.html").write_text(
        "<html>Demo plugin</html>", encoding="utf-8"
    )
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", dist)
    store = InMemoryMarketStore()
    plugin = store.register_plugin(
        {"id": "alice", "github_login": "Alice"},
        {
            "name": "astrbot_plugin_demo",
            "desc": "demo",
            "author": "Alice",
            "repo": "https://github.com/organization/astrbot_plugin_demo",
        },
    )
    store.update_plugin_status(plugin["id"], "listed", None)
    return TestClient(create_app(store=store))


def test_market_web_files_routes_and_real_404(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

    prerendered = client.get("/plugin/astrbot_plugin_demo")
    assert prerendered.status_code == 200
    assert "Demo plugin" in prerendered.text
    redirect = client.get("/plugin/astrbot_plugin_demo?ref=old", follow_redirects=False)
    assert redirect.status_code == 301
    assert redirect.headers["location"] == "/plugin/Alice/astrbot_plugin_demo?ref=old"
    assert redirect.headers["cache-control"] == "public, max-age=0, must-revalidate"
    detail = client.get("/v1/plugins/by-author/alice/astrbot_plugin_demo")
    assert detail.status_code == 200
    assert detail.json()["canonical_path"] == "/plugin/Alice/astrbot_plugin_demo"
    assert detail.headers["cache-control"] == "private, no-store"
    for path in (
        "/plugin/missing",
        "/plugin/bob/astrbot_plugin_demo",
        "/plugin/Alice/missing",
        "/plugin/Alice/astrbot_plugin_demo/index.html",
    ):
        assert client.get(path).status_code == 404
    client.app.state.store.update_plugin_status("astrbot_plugin_demo", "unlisted", None)
    assert client.get("/plugin/Alice/astrbot_plugin_demo").status_code == 404
    assert client.get("/v1/plugins/by-author/Alice/astrbot_plugin_demo").status_code == 404

    fallback = client.get("/submit")
    assert fallback.status_code == 200
    assert fallback.headers["cache-control"] == "public, max-age=0, must-revalidate"

    workbench = client.get("/plugin-workbench")
    assert workbench.status_code == 200
    assert workbench.headers["cache-control"] == "public, max-age=0, must-revalidate"

    plugin_root = client.get("/plugin")
    assert plugin_root.status_code == 404
    assert plugin_root.headers["cache-control"] == "public, max-age=0, must-revalidate"

    missing = client.get("/nonexistent-xyz")
    assert missing.status_code == 404
    assert "market" in missing.text
    assert missing.headers["cache-control"] == "public, max-age=0, must-revalidate"


def test_cache_control_classification() -> None:
    assert cache_control_for_path("/v1/plugins") == "public, max-age=60"
    assert cache_control_for_path("/v1/site") == "public, max-age=60"
    assert cache_control_for_path("/v1/plugins/demo") == "private, no-store"
    assert cache_control_for_path("/plugins.json") == "public, max-age=300"
    assert cache_control_for_path("/plugins-md5.json") == "public, max-age=300"
    assert cache_control_for_path("/v1/astrbot/plugins") == "public, max-age=300"
    assert cache_control_for_path("/sitemap.xml") == "public, max-age=3600"
    assert cache_control_for_path("/robots.txt") == "public, max-age=3600"
    assert cache_control_for_path("/llms.txt") == "public, max-age=3600"
