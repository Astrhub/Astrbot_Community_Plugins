from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import load_settings
from app.store import InMemoryMarketStore

MINIMAL_SSR_INDEX = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <!-- SSR_HEAD_START -->
    <title>Default</title>
    <!-- SSR_HEAD_END -->
  </head>
  <body>
    <div id="app"><!-- SSR_BODY --></div>
  </body>
</html>
"""


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "font").mkdir(parents=True)
    (dist / "plugin" / "Alice" / "astrbot_plugin_demo").mkdir(parents=True)
    (dist / "index.html").write_text(MINIMAL_SSR_INDEX, encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    (dist / "logo.webp").write_bytes(b"RIFFwebp")
    (dist / "og-cover.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (dist / "font" / "lexend.woff2").write_bytes(b"wOF2")
    (dist / "sitemap.xml").write_text("<stale-sitemap/>", encoding="utf-8")
    (dist / "robots.txt").write_text("stale-robots", encoding="utf-8")
    (dist / "plugin" / "Alice" / "astrbot_plugin_demo" / "index.html").write_text(
        "<html>Stale prerendered snapshot</html>", encoding="utf-8"
    )
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", dist)
    store = InMemoryMarketStore()
    alice = store.upsert_github_user({"id": "101", "login": "Alice", "name": "Alice Dev"})
    plugin = store.register_plugin(
        alice,
        {
            "name": "astrbot_plugin_demo",
            "display_name": "Demo plugin",
            "desc": "demo description",
            "author": "Alice",
            "repo": "https://github.com/organization/astrbot_plugin_demo",
        },
    )
    store.update_plugin_status(plugin["id"], "listed", None)
    settings = load_settings({"WEB_URL": "https://market.example.test"})
    return TestClient(main_module.create_app(settings=settings, store=store))


def test_market_web_files_routes_and_real_404(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

    for static_path in ("/logo.webp", "/og-cover.png", "/font/lexend.woff2"):
        static_resp = client.get(static_path)
        assert static_resp.status_code == 200
        assert static_resp.headers["cache-control"] == "public, max-age=3600"

    # Legacy 2-segment /plugin/{id} now returns a real HTTP 404 (no 301 redirect).
    legacy_plugin = client.get("/plugin/astrbot_plugin_demo", follow_redirects=False)
    assert legacy_plugin.status_code == 404
    assert legacy_plugin.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert '<meta name="robots" content="noindex,nofollow" />' in legacy_plugin.text
    assert "<h1>页面不存在</h1>" in legacy_plugin.text

    # Canonical 3-segment plugin route uses runtime HTML injection, never stale snapshots.
    canonical_plugin = client.get("/plugin/Alice/astrbot_plugin_demo")
    assert canonical_plugin.status_code == 200
    assert "Stale prerendered snapshot" not in canonical_plugin.text
    assert "<h1>Demo plugin</h1>" in canonical_plugin.text
    assert "https://market.example.test/plugin/Alice/astrbot_plugin_demo" in canonical_plugin.text
    assert canonical_plugin.headers["cache-control"] == "public, max-age=0, must-revalidate"

    # Non-canonical case on 3-segment path redirects 301 and preserves query string.
    redirect = client.get("/plugin/alice/astrbot_plugin_demo?ref=old", follow_redirects=False)
    assert redirect.status_code == 301
    assert redirect.headers["location"] == "/plugin/Alice/astrbot_plugin_demo?ref=old"
    assert redirect.headers["cache-control"] == "public, max-age=0, must-revalidate"

    detail = client.get("/v1/plugins/by-author/alice/astrbot_plugin_demo")
    assert detail.status_code == 200
    assert detail.json()["canonical_path"] == "/plugin/Alice/astrbot_plugin_demo"
    assert detail.headers["cache-control"] == "private, no-store"

    for path in (
        "/plugin",
        "/plugin/missing",
        "/plugin/bob/astrbot_plugin_demo",
        "/plugin/Alice/missing",
        "/plugin/Alice/astrbot_plugin_demo/index.html",
        "/plugin/Alice/astrbot_plugin_demo/extra",
        "/admin/nonexistent",
        "/settings/nonexistent",
        "/index.html",
        "/nonexistent-xyz",
    ):
        missing = client.get(path, follow_redirects=False)
        assert missing.status_code == 404, path
        assert missing.headers["cache-control"] == "public, max-age=0, must-revalidate"
        assert '<meta name="robots" content="noindex,nofollow" />' in missing.text

    client.app.state.store.update_plugin_status("astrbot_plugin_demo", "unlisted", None)
    assert client.get("/plugin/Alice/astrbot_plugin_demo").status_code == 404
    assert client.get("/v1/plugins/by-author/Alice/astrbot_plugin_demo").status_code == 404

    submit = client.get("/submit")
    assert submit.status_code == 200
    assert submit.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert "<h1>提交插件</h1>" in submit.text

    workbench = client.get("/plugin-workbench")
    assert workbench.status_code == 200
    assert workbench.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert '<meta name="robots" content="noindex,nofollow" />' in workbench.text

    # Dynamic /sitemap.xml and /robots.txt bypass stale dist files.
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "<stale-sitemap/>" not in sitemap.text
    assert sitemap.headers["cache-control"] == "public, max-age=3600"

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "stale-robots" not in robots.text
    assert "Sitemap: https://market.example.test/sitemap.xml" in robots.text
    assert robots.headers["cache-control"] == "public, max-age=3600"


def test_cache_control_classification() -> None:
    cache_control_for_path = main_module.cache_control_for_path
    assert cache_control_for_path("/v1/plugins") == "public, max-age=60"
    assert cache_control_for_path("/v1/site") == "public, max-age=60"
    assert cache_control_for_path("/v1/authors") == "public, max-age=60"
    assert cache_control_for_path("/v1/plugins/demo") == "private, no-store"
    assert cache_control_for_path("/plugins.json") == "public, max-age=300"
    assert cache_control_for_path("/plugins-md5.json") == "public, max-age=300"
    assert cache_control_for_path("/v1/astrbot/plugins") == "public, max-age=300"
    assert cache_control_for_path("/sitemap.xml") == "public, max-age=3600"
    assert cache_control_for_path("/robots.txt") == "public, max-age=3600"
    assert cache_control_for_path("/llms.txt") == "public, max-age=3600"
    assert cache_control_for_path("/assets/app-123.js") == "public, max-age=31536000, immutable"
    assert cache_control_for_path("/og-cover.png") == "public, max-age=3600"
    assert cache_control_for_path("/logo.webp") == "public, max-age=3600"
    assert cache_control_for_path("/logo.svg") == "public, max-age=3600"
    assert cache_control_for_path("/plugin_default.png") == "public, max-age=3600"
    assert cache_control_for_path("/font/lexend.woff2") == "public, max-age=3600"
    assert cache_control_for_path("/missing.png", "text/html; charset=utf-8") == (
        "public, max-age=0, must-revalidate"
    )
    assert cache_control_for_path("/health") == "private, no-store"
