from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import hash_password
from app.config import load_settings
from app.seo_render import (
    EXTERNAL_SITEMAPS,
    FALLBACK_SITE_ORIGIN,
    PRIVATE_ROUTE_TITLES,
    TemplateMarkerError,
    clear_template_cache,
    get_cached_sitemap_xml,
    load_index_template,
    resolve_site_origin,
    safe_external_http_url,
)
from app.store import InMemoryMarketStore

TEMPLATE_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <!-- SSR_HEAD_START -->
    <title>Default Title</title>
    <!-- SSR_HEAD_END -->
  </head>
  <body>
    <div id="app"><!-- SSR_BODY --></div>
    <script type="module" src="/assets/main.js"></script>
  </body>
</html>
"""


def _extract_json_ld(html_text: str) -> dict[str, Any]:
    match = re.search(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    assert match is not None, f"JSON-LD script missing in HTML:\n{html_text}"
    return json.loads(match.group(1))


def _count_h1(html_text: str) -> int:
    return len(re.findall(r"<h1(?:\s[^>]*)?>", html_text))


def _setup_client(
    tmp_path: Path,
    monkeypatch: Any,
    env: dict[str, str] | None = None,
    store: InMemoryMarketStore | None = None,
) -> tuple[TestClient, InMemoryMarketStore]:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(TEMPLATE_HTML, encoding="utf-8")
    monkeypatch.setattr(main_module, "MARKET_WEB_DIST", dist)
    clear_template_cache()
    market_store = store or InMemoryMarketStore()
    settings = load_settings(
        {
            "WEB_URL": "https://market.example.org",
            "SITE_NAME": "测试插件市场",
            "SITE_DESCRIPTION": "自定义市场描述",
            "ENABLE_DEV_AUTH": "false",
            **(env or {}),
        }
    )
    app = main_module.create_app(settings=settings, store=market_store)
    return TestClient(app), market_store


def test_public_private_and_error_route_seo_matrix(tmp_path: Path, monkeypatch: Any) -> None:
    store = InMemoryMarketStore()
    alice = store.upsert_github_user({"id": "101", "login": "AliceGH", "name": "Alice Name"})
    for stored_user in store.state["users"]:
        if stored_user["id"] == alice["id"]:
            stored_user["public_username"] = "AlicePublic"
            alice = dict(stored_user)
            break
    plugin = store.register_plugin(
        alice,
        {
            "name": "astrbot_plugin_weather",
            "display_name": "天气助手",
            "desc": "实时天气查询插件",
            "short_desc": "查询天气",
            "author": "Alice Name",
            "repo": "https://github.com/AliceGH/astrbot_plugin_weather",
            "tags": ["weather", "tool"],
            "category": "utilities",
            "version": "1.2.3",
        },
    )
    for stored_plugin in store.state["plugins"]:
        if stored_plugin["id"] == plugin["id"]:
            stored_plugin["owner_username"] = "AlicePublic"
            stored_plugin["owner_github_login"] = "AliceGH"
            break
    store.update_plugin_status(plugin["id"], "listed", None)

    empty_author = store.create_email_user(
        username="empty_dev",
        email="empty@example.com",
        password_hash=hash_password("password123"),
    )
    assert empty_author["public_username"] == "empty_dev"

    client, _ = _setup_client(tmp_path, monkeypatch, store=store)

    # Homepage
    home = client.get("/")
    assert home.status_code == 200
    assert _count_h1(home.text) == 1
    assert "<h1>社区插件市场</h1>" in home.text
    assert "<title>AstrBot 社区插件 - 测试插件市场</title>" in home.text
    assert '<link rel="canonical" href="https://market.example.org/" />' in home.text
    assert (
        '<meta property="og:image" content="https://market.example.org/og-cover.png" />'
        in home.text
    )
    assert '<a href="/plugin/AlicePublic/astrbot_plugin_weather">' in home.text
    assert '<a href="/author/AlicePublic">' in home.text
    home_ld = _extract_json_ld(home.text)
    assert home_ld["@type"] == "WebSite"
    assert home_ld["name"] == "测试插件市场"
    assert home_ld["url"] == "https://market.example.org/"
    assert (
        home_ld["potentialAction"]["target"] == "https://market.example.org/?q={search_term_string}"
    )

    # Plugin detail canonical 3-segment route
    detail = client.get("/plugin/AlicePublic/astrbot_plugin_weather")
    assert detail.status_code == 200
    assert _count_h1(detail.text) == 1
    assert "<h1>天气助手</h1>" in detail.text
    assert "<title>天气助手 · AlicePublic - 测试插件市场</title>" in detail.text
    assert (
        '<link rel="canonical" href="https://market.example.org/plugin/AlicePublic/astrbot_plugin_weather" />'
        in detail.text
    )
    assert "实时天气查询插件" in detail.text
    detail_ld = _extract_json_ld(detail.text)
    assert detail_ld["@type"] == "SoftwareApplication"
    assert detail_ld["name"] == "天气助手"
    assert detail_ld["softwareVersion"] == "1.2.3"
    assert (
        detail_ld["url"] == "https://market.example.org/plugin/AlicePublic/astrbot_plugin_weather"
    )

    # Plugin 3-segment case mismatch & historical github_login alias both 301 with query preserved
    case_redirect = client.get(
        "/plugin/alicepublic/astrbot_plugin_weather?tab=readme", follow_redirects=False
    )
    assert case_redirect.status_code == 301
    assert (
        case_redirect.headers["location"] == "/plugin/AlicePublic/astrbot_plugin_weather?tab=readme"
    )
    assert case_redirect.headers["cache-control"] == "public, max-age=0, must-revalidate"

    alias_redirect = client.get(
        "/plugin/AliceGH/astrbot_plugin_weather?from=old", follow_redirects=False
    )
    assert alias_redirect.status_code == 301
    assert (
        alias_redirect.headers["location"] == "/plugin/AlicePublic/astrbot_plugin_weather?from=old"
    )

    # Author canonical page (with plugins) & empty registered author page
    author = client.get("/author/AlicePublic")
    assert author.status_code == 200
    assert _count_h1(author.text) == 1
    assert "<h1>Alice Name</h1>" in author.text
    assert '<link rel="canonical" href="https://market.example.org/author/AlicePublic" />' in (
        author.text
    )
    assert '<a href="/plugin/AlicePublic/astrbot_plugin_weather">' in author.text
    author_ld = _extract_json_ld(author.text)
    assert author_ld["@type"] == "ProfilePage"
    assert author_ld["url"] == "https://market.example.org/author/AlicePublic"

    # Author case & historical github_login alias redirect 301
    author_case_301 = client.get("/author/alicepublic?p=1", follow_redirects=False)
    assert author_case_301.status_code == 301
    assert author_case_301.headers["location"] == "/author/AlicePublic?p=1"

    author_alias_301 = client.get("/author/AliceGH?p=2", follow_redirects=False)
    assert author_alias_301.status_code == 301
    assert author_alias_301.headers["location"] == "/author/AlicePublic?p=2"

    # Registered author without listed plugins still returns 200 on /author/{username}
    empty_author_page = client.get("/author/empty_dev")
    assert empty_author_page.status_code == 200
    assert _count_h1(empty_author_page.text) == 1
    assert "<h1>empty_dev</h1>" in empty_author_page.text

    # /submit and /docs/rest
    for path, heading in (("/submit", "提交插件"), ("/docs/rest", "REST API 文档")):
        resp = client.get(path)
        assert resp.status_code == 200
        assert _count_h1(resp.text) == 1
        assert f"<h1>{heading}</h1>" in resp.text
        assert f'<link rel="canonical" href="https://market.example.org{path}" />' in resp.text
        assert '<meta name="robots" content="index,follow" />' in resp.text

    # Exact private routes return 200 with noindex,nofollow and unique H1
    for path, heading in PRIVATE_ROUTE_TITLES.items():
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert _count_h1(resp.text) == 1
        assert f"<h1>{heading}</h1>" in resp.text
        assert '<meta name="robots" content="noindex,nofollow" />' in resp.text
        assert f'<link rel="canonical" href="https://market.example.org{path}" />' in resp.text

    # Unknown paths, legacy 2-segment /plugin/{id}, 4-segment /plugin/*, /author/*, /admin/foo -> 404
    for path in (
        "/plugin",
        "/plugin/astrbot_plugin_weather",
        "/plugin/AlicePublic/astrbot_plugin_weather/extra",
        "/author",
        "/author/unknown_author_xyz",
        "/author/AlicePublic/extra",
        "/admin/foo",
        "/settings/foo",
        "/index.html",
        "/does-not-exist",
    ):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 404, path
        assert _count_h1(resp.text) == 1
        assert "<h1>页面不存在</h1>" in resp.text
        assert '<meta name="robots" content="noindex,nofollow" />' in resp.text
        assert '<link rel="canonical" href="https://market.example.org/404" />' in resp.text
        assert "etag" not in resp.headers


def test_host_header_poisoning_and_xss_protection(tmp_path: Path, monkeypatch: Any) -> None:
    store = InMemoryMarketStore()
    evil_user = store.upsert_github_user(
        {
            "id": "201",
            "login": "xss_author",
            "name": 'Evil </script><img src=x onerror="alert(1)">',
        }
    )
    store.upsert_oauth_identity(
        evil_user["id"],
        provider="custom",
        instance_url="javascript:alert(1)",
        provider_user_id="p-1",
        provider_login="xss_author",
    )
    plugin = store.register_plugin(
        evil_user,
        {
            "name": "astrbot_plugin_xss",
            "display_name": 'Plugin </script><script>alert("x")</script>',
            "desc": 'Desc <b>"quoted"</b> & </script>',
            "author": "xss_author",
            "repo": "https://github.com/xss_author/astrbot_plugin_xss",
        },
    )
    store.update_plugin_status(plugin["id"], "listed", None)

    client, _ = _setup_client(tmp_path, monkeypatch, store=store)
    poisoned_headers = {
        "Host": "evil.attacker.invalid",
        "X-Forwarded-Host": "evil.attacker.invalid",
    }

    home = client.get("/", headers=poisoned_headers)
    assert home.status_code == 200
    assert "evil.attacker.invalid" not in home.text
    assert "https://market.example.org/" in home.text

    sitemap = client.get("/sitemap.xml", headers=poisoned_headers)
    assert sitemap.status_code == 200
    assert "evil.attacker.invalid" not in sitemap.text
    assert "https://market.example.org/plugin/xss_author/astrbot_plugin_xss" in sitemap.text

    robots = client.get("/robots.txt", headers=poisoned_headers)
    assert robots.status_code == 200
    assert "evil.attacker.invalid" not in robots.text
    assert "Sitemap: https://market.example.org/sitemap.xml" in robots.text
    for external in EXTERNAL_SITEMAPS:
        assert f"Sitemap: {external}" in robots.text

    detail = client.get("/plugin/xss_author/astrbot_plugin_xss")
    assert detail.status_code == 200
    assert "<script>alert(" not in detail.text
    assert "Plugin &lt;/script&gt;&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in detail.text
    detail_ld = _extract_json_ld(detail.text)
    assert detail_ld["name"] == 'Plugin </script><script>alert("x")</script>'

    author = client.get("/author/xss_author")
    assert author.status_code == 200
    assert "<img src=x" not in author.text
    assert "javascript:alert(1)" not in author.text


def test_dynamic_sitemap_only_lists_authors_with_listed_plugins_and_invalidates_on_writes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = InMemoryMarketStore()
    admin = store.create_internal_user(
        username="coreadmin",
        password_hash=hash_password("password123"),
        role="core_admin",
    )
    author = store.upsert_github_user({"id": "301", "login": "Alice"})
    store.create_email_user(
        username="empty_user",
        email="empty@example.com",
        password_hash=hash_password("password123"),
    )
    plugin = store.register_plugin(
        author,
        {
            "name": "astrbot_plugin_alpha",
            "display_name": "Alpha",
            "desc": "Alpha plugin",
            "author": "Alice",
            "repo": "https://github.com/Alice/astrbot_plugin_alpha",
        },
    )
    # Abnormal record without owner handle must be skipped in sitemap.
    orphan = store.register_plugin(
        {"id": "orphan-id"},
        {
            "name": "astrbot_plugin_orphan",
            "display_name": "Orphan",
            "desc": "No owner handle",
            "author": "nobody",
            "repo": "https://github.com/nobody/astrbot_plugin_orphan",
        },
    )
    orphan["owner_username"] = ""
    orphan["owner_github_login"] = ""
    store.update_plugin_status(orphan["id"], "listed", admin["id"])

    client, _ = _setup_client(tmp_path, monkeypatch, store=store)

    # Pending plugin and empty author are excluded from sitemap.
    first = client.get("/sitemap.xml")
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("application/xml")
    assert first.headers["cache-control"] == "public, max-age=3600"
    assert "https://market.example.org/" in first.text
    assert "https://market.example.org/submit" in first.text
    assert "https://market.example.org/docs/rest" in first.text
    assert "/plugin/Alice/astrbot_plugin_alpha" not in first.text
    assert "/author/Alice" not in first.text
    assert "/author/empty_user" not in first.text
    assert "astrbot_plugin_orphan" not in first.text

    # Admin logs in and approves plugin -> sitemap cache invalidates immediately.
    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "coreadmin", "password": "password123"},
    )
    assert login.status_code == 200
    approve = client.post(f"/v1/admin/plugins/{plugin['id']}/list")
    assert approve.status_code == 200

    listed_sitemap = client.get("/sitemap.xml")
    assert "https://market.example.org/plugin/Alice/astrbot_plugin_alpha" in listed_sitemap.text
    assert "https://market.example.org/author/Alice" in listed_sitemap.text
    assert "/author/empty_user" not in listed_sitemap.text

    # Admin updates WEB_URL via settings -> sitemap immediately reflects new origin.
    current_settings = client.get("/v1/admin/settings").json()
    current_settings["site"]["web_url"] = "https://renamed.example.net"
    saved = client.put("/v1/admin/settings", json=current_settings)
    assert saved.status_code == 200

    renamed_sitemap = client.get("/sitemap.xml")
    assert "https://renamed.example.net/plugin/Alice/astrbot_plugin_alpha" in renamed_sitemap.text
    assert "https://market.example.org/" not in renamed_sitemap.text

    # Admin unlists plugin -> immediately removed from sitemap along with author.
    unlist = client.post(
        f"/v1/admin/plugins/{plugin['id']}/unlist",
        json={"reason": "下架测试"},
    )
    assert unlist.status_code == 200
    unlisted_sitemap = client.get("/sitemap.xml")
    assert "/plugin/Alice/astrbot_plugin_alpha" not in unlisted_sitemap.text
    assert "/author/Alice" not in unlisted_sitemap.text


def test_weak_etag_304_and_head_support(tmp_path: Path, monkeypatch: Any) -> None:
    client, _ = _setup_client(tmp_path, monkeypatch)

    for path, expected_cc in (
        ("/", "public, max-age=0, must-revalidate"),
        ("/submit", "public, max-age=0, must-revalidate"),
        ("/docs/rest", "public, max-age=0, must-revalidate"),
        ("/sitemap.xml", "public, max-age=3600"),
        ("/robots.txt", "public, max-age=3600"),
    ):
        get_resp = client.get(path)
        assert get_resp.status_code == 200
        etag = get_resp.headers.get("etag")
        assert etag and etag.startswith('W/"')
        assert get_resp.headers["cache-control"] == expected_cc

        not_modified = client.get(path, headers={"If-None-Match": etag})
        assert not_modified.status_code == 304
        assert not_modified.content == b""
        assert not_modified.headers["etag"] == etag
        assert not_modified.headers["cache-control"] == expected_cc

        head_resp = client.head(path)
        assert head_resp.status_code == 200
        assert head_resp.content == b""
        assert head_resp.headers["etag"] == etag
        assert head_resp.headers["cache-control"] == expected_cc

    head_404 = client.head("/missing-page")
    assert head_404.status_code == 404
    assert head_404.content == b""
    assert "etag" not in head_404.headers


def test_template_marker_validation_and_origin_fallback(tmp_path: Path) -> None:
    bad_index = tmp_path / "index.html"
    bad_index.write_text("<html><head></head><body></body></html>", encoding="utf-8")
    clear_template_cache()
    with pytest.raises(TemplateMarkerError):
        load_index_template(bad_index)

    assert resolve_site_origin("") == FALLBACK_SITE_ORIGIN
    assert resolve_site_origin("ftp://example.com") == FALLBACK_SITE_ORIGIN
    assert resolve_site_origin("https://Example.COM/path/") == "https://Example.COM"


def test_malformed_urls_fallback_and_return_400_without_500(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="app.seo_render"):
        assert resolve_site_origin("https://[") == FALLBACK_SITE_ORIGIN
        assert resolve_site_origin("https://example.com:badport") == FALLBACK_SITE_ORIGIN
    assert any("falling back" in record.message for record in caplog.records)

    assert safe_external_http_url("https://[/alice") == ""
    assert safe_external_http_url("https://example.com:badport/alice") == ""
    assert safe_external_http_url("https://github.com/alice") == "https://github.com/alice"

    store = InMemoryMarketStore()
    store.create_internal_user(
        username="coreadmin",
        password_hash=hash_password("password123"),
        role="core_admin",
    )
    author = store.upsert_github_user({"id": "401", "login": "SafeAuthor", "name": "Safe Author"})
    store.upsert_oauth_identity(
        author["id"],
        provider="gitlab",
        instance_url="https://[",
        provider_user_id="gl-401",
        provider_login="safe_gl",
    )
    client, _ = _setup_client(
        tmp_path,
        monkeypatch,
        env={"WEB_URL": "https://["},
        store=store,
    )

    home = client.get("/")
    assert home.status_code == 200
    assert f'<link rel="canonical" href="{FALLBACK_SITE_ORIGIN}/" />' in home.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert f"<loc>{FALLBACK_SITE_ORIGIN}/</loc>" in sitemap.text

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert f"Sitemap: {FALLBACK_SITE_ORIGIN}/sitemap.xml" in robots.text

    author_page = client.get("/author/SafeAuthor")
    assert author_page.status_code == 200
    assert "https://github.com/SafeAuthor" in author_page.text
    assert "https://[/safe_gl" not in author_page.text

    login = client.post(
        "/v1/auth/internal/login",
        json={"username": "coreadmin", "password": "password123"},
    )
    assert login.status_code == 200

    for field, bad_value in (
        ("web_url", "https://["),
        ("web_url", "https://example.com:badport"),
        ("icon_url", "https://["),
    ):
        payload = client.get("/v1/admin/settings").json()
        payload["site"]["web_url"] = "https://market.example.org"
        payload["site"][field] = bad_value
        resp = client.put("/v1/admin/settings", json=payload)
        assert resp.status_code == 400, (field, bad_value, resp.text)


def test_persisted_config_failure_falls_back_to_env_settings(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingOptionsStore(InMemoryMarketStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_options = False
            self.fail_plugins = False

        def list_options(self) -> dict[str, str]:
            if self.fail_options:
                raise RuntimeError("options db unavailable")
            return super().list_options()

        def list_public_plugins(self) -> list[dict[str, Any]]:
            if self.fail_plugins:
                raise RuntimeError("plugins db unavailable")
            return super().list_public_plugins()

    store = FailingOptionsStore()
    author = store.upsert_github_user({"id": "501", "login": "EnvAuthor"})
    plugin = store.register_plugin(
        author,
        {
            "name": "astrbot_plugin_cached",
            "display_name": "Cached",
            "desc": "Cached plugin",
            "author": "EnvAuthor",
            "repo": "https://github.com/EnvAuthor/astrbot_plugin_cached",
        },
    )
    store.update_plugin_status(plugin["id"], "listed", None)

    client, _ = _setup_client(
        tmp_path,
        monkeypatch,
        env={"WEB_URL": "https://env.example.org"},
        store=store,
    )

    # Pre-populate sitemap cache for the environment origin while DB is healthy.
    warm = client.get("/sitemap.xml")
    assert warm.status_code == 200
    assert "https://env.example.org/plugin/EnvAuthor/astrbot_plugin_cached" in warm.text

    # Simulate DB failure for both site options and plugin listings.
    store.fail_options = True
    store.fail_plugins = True

    with caplog.at_level("ERROR", logger="app.main"):
        robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: https://env.example.org/sitemap.xml" in robots.text
    assert any(
        "/robots.txt" in record.message and "options db unavailable" in (record.exc_text or "")
        for record in caplog.records
    )

    submit = client.get("/submit")
    assert submit.status_code == 200
    assert '<link rel="canonical" href="https://env.example.org/submit" />' in submit.text

    cached_sitemap = client.get("/sitemap.xml")
    assert cached_sitemap.status_code == 200
    assert "https://env.example.org/plugin/EnvAuthor/astrbot_plugin_cached" in cached_sitemap.text

    # On sitemap cache miss, plugin DB query errors must still propagate.
    client.app.state._seo_sitemap_cache.clear()
    with pytest.raises(RuntimeError, match="plugins db unavailable"):
        client.get("/sitemap.xml")


def test_concurrent_invalidation_prevents_stale_sitemap_cache_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        read_started = asyncio.Event()
        allow_resume = asyncio.Event()

        class CoordinatedStore(InMemoryMarketStore):
            def __init__(self) -> None:
                super().__init__()
                self.pause_first_read = True

            async def list_public_plugins(self) -> list[dict[str, Any]]:
                snapshot = super().list_public_plugins()
                if self.pause_first_read:
                    self.pause_first_read = False
                    read_started.set()
                    await allow_resume.wait()
                return snapshot

        store = CoordinatedStore()
        store.create_internal_user(
            username="coreadmin",
            password_hash=hash_password("password123"),
            role="core_admin",
        )
        author = store.upsert_github_user({"id": "601", "login": "RaceAuthor"})
        plugin = store.register_plugin(
            author,
            {
                "name": "astrbot_plugin_race",
                "display_name": "Race Plugin",
                "desc": "Race condition plugin",
                "author": "RaceAuthor",
                "repo": "https://github.com/RaceAuthor/astrbot_plugin_race",
            },
        )

        dist = tmp_path / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text(TEMPLATE_HTML, encoding="utf-8")
        monkeypatch.setattr(main_module, "MARKET_WEB_DIST", dist)
        clear_template_cache()
        settings = load_settings(
            {
                "WEB_URL": "https://market.example.org",
                "SITE_NAME": "测试插件市场",
                "SITE_DESCRIPTION": "自定义市场描述",
                "ENABLE_DEV_AUTH": "false",
            }
        )
        app = main_module.create_app(settings=settings, store=store)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            task_a = asyncio.create_task(client.get("/sitemap.xml"))
            await asyncio.wait_for(read_started.wait(), timeout=2.0)

            login = await client.post(
                "/v1/auth/internal/login",
                json={"username": "coreadmin", "password": "password123"},
            )
            assert login.status_code == 200
            approve = await client.post(f"/v1/admin/plugins/{plugin['id']}/list")
            assert approve.status_code == 200

            allow_resume.set()
            resp_a = await asyncio.wait_for(task_a, timeout=2.0)
            assert resp_a.status_code == 200
            assert "/plugin/RaceAuthor/astrbot_plugin_race" not in resp_a.text

            # Request A must not repopulate the cache with stale XML after invalidation.
            assert get_cached_sitemap_xml(app, "https://market.example.org") is None

            resp_c = await client.get("/sitemap.xml")
            assert resp_c.status_code == 200
            assert "https://market.example.org/plugin/RaceAuthor/astrbot_plugin_race" in resp_c.text
            cached = get_cached_sitemap_xml(app, "https://market.example.org")
            assert cached is not None
            assert "https://market.example.org/plugin/RaceAuthor/astrbot_plugin_race" in cached

    asyncio.run(scenario())


def test_author_page_and_api_include_legacy_github_login_plugins_without_duplicates(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = InMemoryMarketStore()
    alice = store.upsert_github_user({"id": "701", "login": "AliceGH", "name": "Alice Public"})
    for stored_user in store.state["users"]:
        if stored_user["id"] == alice["id"]:
            stored_user["public_username"] = "AlicePublic"
            alice = dict(stored_user)
            break

    other = store.create_email_user(
        username="other_user",
        email="other@example.com",
        password_hash=hash_password("password123"),
    )

    canonical_plugin = store.register_plugin(
        alice,
        {
            "name": "astrbot_plugin_canonical",
            "display_name": "规范插件",
            "desc": "Canonical plugin",
            "author": "Alice Public",
            "repo": "https://github.com/AliceGH/astrbot_plugin_canonical",
        },
    )
    legacy_plugin = store.register_plugin(
        alice,
        {
            "name": "astrbot_plugin_legacy_gh",
            "display_name": "旧身份插件",
            "desc": "Legacy plugin",
            "author": "Alice Public",
            "repo": "https://github.com/AliceGH/astrbot_plugin_legacy_gh",
        },
    )
    unlisted_legacy = store.register_plugin(
        alice,
        {
            "name": "astrbot_plugin_legacy_unlisted",
            "display_name": "未上架旧插件",
            "desc": "Unlisted legacy plugin",
            "author": "Alice Public",
            "repo": "https://github.com/AliceGH/astrbot_plugin_legacy_unlisted",
        },
    )
    unrelated_plugin = store.register_plugin(
        other,
        {
            "name": "astrbot_plugin_unrelated",
            "display_name": "他人插件",
            "desc": "Belongs to another user",
            "author": "Other",
            "repo": "https://github.com/other/astrbot_plugin_unrelated",
        },
    )

    store.update_plugin_status(canonical_plugin["id"], "listed", None)
    store.update_plugin_status(legacy_plugin["id"], "listed", None)
    store.update_plugin_status(unrelated_plugin["id"], "listed", None)

    for item in store.state["plugins"]:
        if item["id"] == canonical_plugin["id"]:
            item["owner_username"] = "AlicePublic"
            item["owner_github_login"] = "AliceGH"
            item["updated_at"] = "2026-11-20T10:00:00Z"
        elif item["id"] == legacy_plugin["id"]:
            item["owner_username"] = ""
            item["owner_github_login"] = "AliceGH"
            item["updated_at"] = "2026-11-22T10:00:00Z"
        elif item["id"] == unlisted_legacy["id"]:
            item["owner_username"] = ""
            item["owner_github_login"] = "AliceGH"
        elif item["id"] == unrelated_plugin["id"]:
            item["owner_username"] = ""
            item["owner_github_login"] = "AliceGH"
            item["owner_user_id"] = other["id"]

    client, _ = _setup_client(tmp_path, monkeypatch, store=store)

    api_resp = client.get("/v1/authors/AlicePublic/plugins")
    assert api_resp.status_code == 200
    api_items = api_resp.json()["items"]
    assert [item["name"] for item in api_items] == [
        "astrbot_plugin_legacy_gh",
        "astrbot_plugin_canonical",
    ]

    alias_api_resp = client.get("/v1/authors/AliceGH/plugins")
    assert alias_api_resp.status_code == 200
    assert alias_api_resp.json()["username"] == "AlicePublic"
    assert [item["name"] for item in alias_api_resp.json()["items"]] == [
        "astrbot_plugin_legacy_gh",
        "astrbot_plugin_canonical",
    ]

    ssr_resp = client.get("/author/AlicePublic")
    assert ssr_resp.status_code == 200
    assert ssr_resp.text.count("astrbot_plugin_canonical") == 1
    assert ssr_resp.text.count("astrbot_plugin_legacy_gh") == 1
    assert "astrbot_plugin_legacy_unlisted" not in ssr_resp.text
    assert "astrbot_plugin_unrelated" not in ssr_resp.text
    assert ssr_resp.text.index("astrbot_plugin_legacy_gh") < ssr_resp.text.index(
        "astrbot_plugin_canonical"
    )

    alias_redirect = client.get("/author/AliceGH", follow_redirects=False)
    assert alias_redirect.status_code == 301
    assert alias_redirect.headers["location"] == "/author/AlicePublic"


def test_ownerless_plugin_omits_two_segment_canonical_path_and_links(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = InMemoryMarketStore()
    alice = store.upsert_github_user({"id": "711", "login": "ValidOwner"})
    owned = store.register_plugin(
        alice,
        {
            "name": "astrbot_plugin_owned",
            "display_name": "Owned Plugin",
            "desc": "Owned plugin",
            "author": "ValidOwner",
            "repo": "https://github.com/ValidOwner/astrbot_plugin_owned",
        },
    )
    orphan = store.register_plugin(
        {"id": "orphan-owner-id"},
        {
            "name": "astrbot_plugin_ownerless",
            "display_name": "Ownerless Plugin",
            "desc": "Abnormal record without owner handle",
            "author": "nobody",
            "repo": "https://github.com/nobody/astrbot_plugin_ownerless",
        },
    )
    for item in store.state["plugins"]:
        if item["id"] == orphan["id"]:
            item["owner_username"] = ""
            item["owner_github_login"] = ""
            break
    store.update_plugin_status(owned["id"], "listed", None)
    store.update_plugin_status(orphan["id"], "listed", None)

    client, _ = _setup_client(tmp_path, monkeypatch, store=store)

    listed_api = client.get("/v1/plugins")
    assert listed_api.status_code == 200
    by_name = {item["name"]: item for item in listed_api.json()["items"]}
    assert by_name["astrbot_plugin_owned"]["canonical_path"] == (
        "/plugin/ValidOwner/astrbot_plugin_owned"
    )
    assert by_name["astrbot_plugin_ownerless"]["canonical_path"] == ""

    home = client.get("/")
    assert home.status_code == 200
    assert '<a href="/plugin/ValidOwner/astrbot_plugin_owned">' in home.text
    assert "astrbot_plugin_ownerless" not in home.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "https://market.example.org/plugin/ValidOwner/astrbot_plugin_owned" in sitemap.text
    assert "astrbot_plugin_ownerless" not in sitemap.text

    legacy_404 = client.get(f"/plugin/{orphan['id']}", follow_redirects=False)
    assert legacy_404.status_code == 404
