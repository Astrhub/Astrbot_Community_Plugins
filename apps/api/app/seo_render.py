"""Runtime SEO head/body injection, dynamic sitemap.xml, and robots.txt rendering."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, Response

from .config import (
    DEFAULT_SITE_DESCRIPTION,
    DEFAULT_SITE_NAME,
    DEFAULT_SITE_SUBTITLE,
    Settings,
)

LOGGER = logging.getLogger(__name__)

SSR_HEAD_START = "<!-- SSR_HEAD_START -->"
SSR_HEAD_END = "<!-- SSR_HEAD_END -->"
SSR_BODY_MARKER = "<!-- SSR_BODY -->"

FALLBACK_SITE_ORIGIN = "http://127.0.0.1:8787"
SITEMAP_CACHE_TTL_SECONDS = 60.0
HOME_PLUGIN_LIST_LIMIT = 60

HTML_CACHE_CONTROL = "public, max-age=0, must-revalidate"
CRAWLER_CACHE_CONTROL = "public, max-age=3600"

EXTERNAL_SITEMAPS: tuple[str, ...] = (
    "https://blog.astrbot.app/sitemap.xml",
    "https://astrbot.app/sitemap.xml",
    "https://astrbot.tech/sitemap.xml",
    "https://docs.astrbot.app/sitemap.xml",
    "https://docs.astrbot.tech/sitemap.xml",
)

PRIVATE_ROUTE_TITLES: dict[str, str] = {
    "/setup": "安装向导",
    "/settings": "系统设置",
    "/settings/personal": "账号与插件",
    "/notifications": "消息中心",
    "/admin": "后台登录",
    "/admin/settings": "系统设置",
    "/admin/plugins": "插件审查工作台",
    "/plugin-workbench": "插件审查工作台",
}

PLUGIN_CATEGORY_LABELS: dict[str, str] = {
    "ai_tools": "AI 增强",
    "entertainment": "娱乐",
    "integrations": "外部集成",
    "productivity": "效率",
    "utilities": "生活实用",
    "other": "其他",
}


class TemplateMarkerError(RuntimeError):
    """Raised when dist/index.html is missing required runtime SEO markers."""


@dataclass(frozen=True)
class SiteSeoContext:
    origin: str
    brand_name: str
    site_subtitle: str
    site_description: str
    default_og_image: str


@dataclass(frozen=True)
class PageSpec:
    title: str
    description: str
    canonical_path: str
    body_html: str
    robots: str = "index,follow"
    og_type: str = "website"
    og_image: str = ""
    json_ld: dict[str, Any] | None = None
    json_ld_id: str = "page-jsonld"


@dataclass(frozen=True)
class _ParsedTemplate:
    mtime_ns: int
    size: int
    before_head: str
    between_head_and_body: str
    after_body: str


_TEMPLATE_CACHE: dict[Path, _ParsedTemplate] = {}


def escape_html(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def serialize_json_ld(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def encode_path_segment(value: str) -> str:
    return quote(str(value or "").strip(), safe="")


def safe_external_http_url(raw_url: Any) -> str:
    value = str(raw_url or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return ""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return ""
    return value


def resolve_site_origin(raw_url: str | None) -> str:
    value = str(raw_url or "").strip()
    if value:
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme.lower() in {"http", "https"}
            and parsed.netloc
            and parsed.hostname
            and not any(ch.isspace() for ch in parsed.netloc)
        ):
            return f"{parsed.scheme.lower()}://{parsed.netloc}".rstrip("/")
    LOGGER.warning(
        "Invalid or empty runtime WEB_URL (%r); falling back to %s for SEO metadata",
        raw_url,
        FALLBACK_SITE_ORIGIN,
    )
    return FALLBACK_SITE_ORIGIN


def build_site_seo_context(
    settings: Settings,
    runtime_config: Mapping[str, str] | None = None,
) -> SiteSeoContext:
    config = runtime_config or {}
    raw_web_url = str(config.get("WEB_URL") or settings.web_url or "").strip()
    origin = resolve_site_origin(raw_web_url)
    brand_name = (
        str(config.get("SITE_NAME") or settings.site_name or "").strip() or DEFAULT_SITE_NAME
    )
    site_subtitle = (
        str(config.get("SITE_SUBTITLE") or settings.site_subtitle or "").strip()
        or DEFAULT_SITE_SUBTITLE
    )
    site_description = (
        str(config.get("SITE_DESCRIPTION") or settings.site_description or "").strip()
        or DEFAULT_SITE_DESCRIPTION
    )
    return SiteSeoContext(
        origin=origin,
        brand_name=brand_name,
        site_subtitle=site_subtitle,
        site_description=site_description,
        default_og_image=f"{origin}/og-cover.png",
    )


def plugin_owner_handle(plugin: Mapping[str, Any]) -> str:
    return str(plugin.get("owner_username") or plugin.get("owner_github_login") or "").strip()


def plugin_public_path(plugin: Mapping[str, Any]) -> str | None:
    owner = plugin_owner_handle(plugin)
    name = str(plugin.get("name") or plugin.get("id") or "").strip()
    if not owner or not name:
        return None
    return f"/plugin/{encode_path_segment(owner)}/{encode_path_segment(name)}"


def author_public_path(username: str) -> str:
    return f"/author/{encode_path_segment(username)}"


def canonical_author_username(author: Mapping[str, Any] | None, fallback: str = "") -> str:
    if author:
        for key in ("public_username", "github_login", "internal_username"):
            value = str(author.get(key) or "").strip()
            if value:
                return value
    return str(fallback or "").strip()


def load_index_template(index_path: Path) -> _ParsedTemplate:
    resolved = index_path.resolve()
    stat = resolved.stat()
    cached = _TEMPLATE_CACHE.get(resolved)
    if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
        return cached

    raw = resolved.read_text(encoding="utf-8")
    head_start = raw.find(SSR_HEAD_START)
    head_end = raw.find(SSR_HEAD_END)
    body_pos = raw.find(SSR_BODY_MARKER)
    if head_start < 0 or head_end < 0 or body_pos < 0:
        raise TemplateMarkerError(
            "Market web index.html is missing required SSR_HEAD_START, "
            "SSR_HEAD_END, or SSR_BODY markers."
        )
    if head_end < head_start or body_pos < head_end:
        raise TemplateMarkerError(
            "Market web index.html has out-of-order SSR_HEAD_START, "
            "SSR_HEAD_END, or SSR_BODY markers."
        )

    parsed = _ParsedTemplate(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        before_head=raw[:head_start],
        between_head_and_body=raw[head_end + len(SSR_HEAD_END) : body_pos],
        after_body=raw[body_pos + len(SSR_BODY_MARKER) :],
    )
    _TEMPLATE_CACHE[resolved] = parsed
    return parsed


def clear_template_cache() -> None:
    _TEMPLATE_CACHE.clear()


def render_head_html(ctx: SiteSeoContext, page: PageSpec) -> str:
    full_title = f"{page.title} - {ctx.brand_name}"
    canonical_path = (
        page.canonical_path if page.canonical_path.startswith("/") else f"/{page.canonical_path}"
    )
    canonical_url = f"{ctx.origin}{canonical_path}"
    og_image = page.og_image or ctx.default_og_image
    tags = [
        f"<title>{escape_html(full_title)}</title>",
        f'<meta name="description" content="{escape_html(page.description)}" />',
        f'<meta name="robots" content="{escape_html(page.robots)}" />',
        f'<link rel="canonical" href="{escape_html(canonical_url)}" />',
        f'<meta property="og:title" content="{escape_html(full_title)}" />',
        f'<meta property="og:description" content="{escape_html(page.description)}" />',
        f'<meta property="og:type" content="{escape_html(page.og_type)}" />',
        f'<meta property="og:url" content="{escape_html(canonical_url)}" />',
        f'<meta property="og:site_name" content="{escape_html(ctx.brand_name)}" />',
        f'<meta property="og:image" content="{escape_html(og_image)}" />',
        '<meta name="twitter:card" content="summary_large_image" />',
    ]
    if page.json_ld is not None:
        tags.append(
            f'<script id="{escape_html(page.json_ld_id)}" type="application/ld+json">'
            f"{serialize_json_ld(page.json_ld)}</script>"
        )
    return "\n    ".join(tags)


def render_html_document(index_path: Path, ctx: SiteSeoContext, page: PageSpec) -> str:
    template = load_index_template(index_path)
    head_html = render_head_html(ctx, page)
    return (
        f"{template.before_head}{head_html}"
        f"{template.between_head_and_body}{page.body_html}"
        f"{template.after_body}"
    )


def _plugin_timestamp(plugin: Mapping[str, Any]) -> str:
    return str(plugin.get("updated_at") or plugin.get("created_at") or "").strip()


def _plugin_summary_text(plugin: Mapping[str, Any]) -> str:
    return str(plugin.get("short_desc") or plugin.get("desc") or "").strip()


def _render_plugin_list_items(plugins: Iterable[Mapping[str, Any]]) -> str:
    items: list[str] = []
    for plugin in plugins:
        path = plugin_public_path(plugin)
        if not path:
            continue
        owner = plugin_owner_handle(plugin)
        display_name = str(plugin.get("display_name") or plugin.get("name") or "").strip()
        author_label = str(plugin.get("author") or owner).strip()
        summary = _plugin_summary_text(plugin)
        summary_html = f"<p>{escape_html(summary)}</p>" if summary else ""
        items.append(
            "<li>"
            f'<a href="{escape_html(path)}">{escape_html(display_name)}</a>'
            f' · <a href="{escape_html(author_public_path(owner))}">'
            f"{escape_html(author_label)}</a>"
            f"{summary_html}"
            "</li>"
        )
    if not items:
        return ""
    return f'<ul class="ssr-list">{"".join(items)}</ul>'


def build_home_page(ctx: SiteSeoContext, listed_plugins: Iterable[Mapping[str, Any]]) -> PageSpec:
    recent_plugins = sorted(
        (item for item in listed_plugins if item.get("status", "listed") == "listed"),
        key=lambda item: (
            _plugin_timestamp(item),
            str(item.get("name") or item.get("id") or ""),
        ),
        reverse=True,
    )[:HOME_PLUGIN_LIST_LIMIT]
    list_html = _render_plugin_list_items(recent_plugins)
    body_html = (
        '<main class="ssr-shell">'
        "<h1>社区插件市场</h1>"
        f'<p class="ssr-lead">{escape_html(ctx.site_description)}</p>'
        '<nav class="ssr-nav" aria-label="站点导航">'
        '<a href="/submit">提交插件</a>'
        '<a href="/docs/rest">REST API 文档</a>'
        "</nav>"
        f"{list_html}"
        "</main>"
    )
    return PageSpec(
        title="AstrBot 社区插件",
        description=ctx.site_description,
        canonical_path="/",
        body_html=body_html,
        robots="index,follow",
        og_type="website",
        json_ld_id="website-jsonld",
        json_ld={
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": ctx.brand_name,
            "url": f"{ctx.origin}/",
            "potentialAction": {
                "@type": "SearchAction",
                "target": f"{ctx.origin}/?q={{search_term_string}}",
                "query-input": "required name=search_term_string",
            },
        },
    )


def build_plugin_detail_page(ctx: SiteSeoContext, plugin: Mapping[str, Any]) -> PageSpec:
    owner = plugin_owner_handle(plugin)
    name = str(plugin.get("name") or plugin.get("id") or "").strip()
    display_name = str(plugin.get("display_name") or name or "插件详情").strip()
    canonical_path = plugin_public_path(plugin) or "/"
    raw_desc = str(
        plugin.get("desc") or plugin.get("short_desc") or "查看 AstrBot 社区插件详情。"
    ).strip()
    meta_desc = f"{owner} 发布的 {display_name}。{raw_desc}" if owner else raw_desc
    title = f"{display_name} · {owner}" if owner else display_name
    author_name = str(plugin.get("author") or owner or "AstrBot 社区开发者").strip()
    version = str(plugin.get("version") or "").strip()
    category_key = str(plugin.get("category") or "other").strip().lower()
    category_label = PLUGIN_CATEGORY_LABELS.get(category_key, "其他")
    raw_tags = plugin.get("tags")
    tags = (
        [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        if isinstance(raw_tags, list)
        else []
    )
    likes = int(plugin.get("likes") or 0)
    comments_count = int(plugin.get("comments_count") or 0)
    date_modified = _plugin_timestamp(plugin)

    json_ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": display_name,
        "applicationCategory": "UtilitiesApplication",
        "operatingSystem": "AstrBot",
        "author": {"@type": "Person", "name": author_name},
        "description": raw_desc,
        "url": f"{ctx.origin}{canonical_path}",
        "interactionStatistic": [
            {
                "@type": "InteractionCounter",
                "interactionType": "https://schema.org/LikeAction",
                "userInteractionCount": likes,
            },
            {
                "@type": "InteractionCounter",
                "interactionType": "https://schema.org/CommentAction",
                "userInteractionCount": comments_count,
            },
        ],
    }
    if version:
        json_ld["softwareVersion"] = version
    if date_modified:
        json_ld["dateModified"] = date_modified

    facts = [
        f'<span>作者：<a href="{escape_html(author_public_path(owner))}">'
        f"{escape_html(author_name)}</a></span>"
        if owner
        else f"<span>作者：{escape_html(author_name)}</span>",
        f"<span>分类：{escape_html(category_label)}</span>",
        f"<span>点赞：{likes}</span>",
        f"<span>评论：{comments_count}</span>",
    ]
    if version:
        facts.insert(1, f"<span>版本：{escape_html(version)}</span>")
    tags_html = f'<p class="ssr-meta">标签：{escape_html(" / ".join(tags))}</p>' if tags else ""
    body_html = (
        '<main class="ssr-shell">'
        '<nav class="ssr-nav" aria-label="面包屑"><a href="/">返回首页</a></nav>'
        f"<h1>{escape_html(display_name)}</h1>"
        f'<p class="ssr-meta">{" · ".join(facts)}</p>'
        f"{tags_html}"
        f"<p>{escape_html(raw_desc)}</p>"
        "</main>"
    )
    return PageSpec(
        title=title,
        description=meta_desc,
        canonical_path=canonical_path,
        body_html=body_html,
        robots="index,follow",
        og_type="article",
        json_ld_id="software-application-jsonld",
        json_ld=json_ld,
    )


def build_author_page(
    ctx: SiteSeoContext,
    *,
    canonical_username: str,
    author_display_name: str,
    oauth_profiles: Iterable[Mapping[str, Any]],
    listed_plugins: Iterable[Mapping[str, Any]],
) -> PageSpec:
    canonical_path = author_public_path(canonical_username)
    author_page_url = f"{ctx.origin}{canonical_path}"
    safe_profiles: list[tuple[str, str]] = []
    for item in oauth_profiles:
        safe_url = safe_external_http_url(item.get("url"))
        if not safe_url:
            continue
        provider = str(item.get("provider") or "").strip()
        label = "GitHub 主页" if provider.lower() == "github" else f"{provider or '外部'} 主页"
        safe_profiles.append((label, safe_url))

    same_as = [url for _, url in safe_profiles]
    main_entity: dict[str, Any] = {
        "@type": "Person",
        "name": author_display_name,
        "url": same_as[0] if same_as else author_page_url,
    }
    if same_as:
        main_entity["sameAs"] = same_as

    links_html = ""
    if safe_profiles:
        anchors = "".join(
            f'<a href="{escape_html(url)}" rel="noopener noreferrer">{escape_html(label)}</a>'
            for label, url in safe_profiles
        )
        links_html = f'<nav class="ssr-nav" aria-label="外部主页">{anchors}</nav>'

    plugin_list_html = _render_plugin_list_items(listed_plugins)
    if not plugin_list_html:
        plugin_list_html = "<p>该作者目前还没有已上架的插件。</p>"

    body_html = (
        '<main class="ssr-shell">'
        '<nav class="ssr-nav" aria-label="面包屑"><a href="/">返回首页</a></nav>'
        f"<h1>{escape_html(author_display_name)}</h1>"
        f'<p class="ssr-meta">@{escape_html(canonical_username)}</p>'
        f"{links_html}"
        f"{plugin_list_html}"
        "</main>"
    )
    return PageSpec(
        title=f"{author_display_name} 的插件",
        description=f"查看 {author_display_name} 在 {ctx.brand_name} 发布的所有插件。",
        canonical_path=canonical_path,
        body_html=body_html,
        robots="index,follow",
        og_type="website",
        json_ld_id="page-jsonld",
        json_ld={
            "@context": "https://schema.org",
            "@type": "ProfilePage",
            "mainEntity": main_entity,
            "url": author_page_url,
        },
    )


def build_submit_page(ctx: SiteSeoContext) -> PageSpec:
    description = f"向 {ctx.brand_name} 提交 AstrBot 社区插件。"
    body_html = (
        '<main class="ssr-shell">'
        '<nav class="ssr-nav" aria-label="面包屑"><a href="/">返回首页</a></nav>'
        "<h1>提交插件</h1>"
        f"<p>{escape_html(description)}</p>"
        "</main>"
    )
    return PageSpec(
        title="提交插件",
        description=description,
        canonical_path="/submit",
        body_html=body_html,
        robots="index,follow",
    )


def build_rest_docs_page(ctx: SiteSeoContext) -> PageSpec:
    description = f"{ctx.brand_name} REST API 文档与在线调试。"
    body_html = (
        '<main class="ssr-shell">'
        '<nav class="ssr-nav" aria-label="面包屑"><a href="/">返回首页</a></nav>'
        "<h1>REST API 文档</h1>"
        f"<p>{escape_html(description)}</p>"
        "</main>"
    )
    return PageSpec(
        title="REST API 文档",
        description=description,
        canonical_path="/docs/rest",
        body_html=body_html,
        robots="index,follow",
    )


def build_private_page(ctx: SiteSeoContext, path: str) -> PageSpec:
    heading = PRIVATE_ROUTE_TITLES[path]
    return PageSpec(
        title=heading,
        description=f"{heading} - {ctx.brand_name}",
        canonical_path=path,
        body_html=f'<main class="ssr-shell"><h1>{escape_html(heading)}</h1></main>',
        robots="noindex,nofollow",
    )


def build_not_found_page(ctx: SiteSeoContext) -> PageSpec:
    body_html = (
        '<main class="ssr-shell">'
        "<h1>页面不存在</h1>"
        "<p>这个页面不存在或已被移除。</p>"
        '<p><a href="/">返回首页</a></p>'
        "</main>"
    )
    return PageSpec(
        title="页面不存在",
        description="请求的页面不存在。",
        canonical_path="/404",
        body_html=body_html,
        robots="noindex,nofollow",
    )


def build_sitemap_xml(origin: str, listed_plugins: Iterable[Mapping[str, Any]]) -> str:
    entries: list[tuple[str, str]] = [
        (f"{origin}/", ""),
        (f"{origin}/submit", ""),
        (f"{origin}/docs/rest", ""),
    ]
    plugin_urls: dict[str, str] = {}
    author_lastmod: dict[str, tuple[str, str]] = {}

    for plugin in listed_plugins:
        if plugin.get("status", "listed") != "listed":
            continue
        path = plugin_public_path(plugin)
        if not path:
            continue
        lastmod = _plugin_timestamp(plugin)
        url = f"{origin}{path}"
        if url not in plugin_urls or lastmod > plugin_urls[url]:
            plugin_urls[url] = lastmod

        owner = plugin_owner_handle(plugin)
        owner_key = owner.lower()
        existing = author_lastmod.get(owner_key)
        if existing is None or lastmod > existing[1]:
            author_lastmod[owner_key] = (owner, lastmod)

    for url in sorted(plugin_urls):
        entries.append((url, plugin_urls[url]))

    for owner, lastmod in sorted(author_lastmod.values(), key=lambda item: item[0].lower()):
        entries.append((f"{origin}{author_public_path(owner)}", lastmod))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in entries:
        if lastmod:
            lines.append(
                f"  <url><loc>{escape_html(loc)}</loc>"
                f"<lastmod>{escape_html(lastmod)}</lastmod></url>"
            )
        else:
            lines.append(f"  <url><loc>{escape_html(loc)}</loc></url>")
    lines.append("</urlset>\n")
    return "\n".join(lines)


def get_cached_sitemap_xml(app: FastAPI, origin: str) -> str | None:
    cache: dict[str, tuple[float, str]] | None = getattr(app.state, "_seo_sitemap_cache", None)
    if not cache:
        return None
    entry = cache.get(origin)
    if not entry:
        return None
    expires_at, xml = entry
    if time.monotonic() >= expires_at:
        cache.pop(origin, None)
        return None
    return xml


def set_cached_sitemap_xml(app: FastAPI, origin: str, xml: str) -> None:
    cache: dict[str, tuple[float, str]] | None = getattr(app.state, "_seo_sitemap_cache", None)
    if cache is None:
        cache = {}
        app.state._seo_sitemap_cache = cache
    cache[origin] = (time.monotonic() + SITEMAP_CACHE_TTL_SECONDS, xml)


def invalidate_seo_caches(app: FastAPI) -> None:
    cache: dict[str, tuple[float, str]] | None = getattr(app.state, "_seo_sitemap_cache", None)
    if cache is not None:
        cache.clear()


def build_robots_txt(origin: str) -> str:
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {origin}/sitemap.xml",
        *(f"Sitemap: {url}" for url in EXTERNAL_SITEMAPS),
    ]
    return "\n".join(lines) + "\n"


def weak_etag(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()[:16]
    return f'W/"{digest}"'


def _strip_weak_prefix(tag: str) -> str:
    value = tag.strip()
    if value.startswith(("W/", "w/")):
        return value[2:].strip()
    return value


def etag_matches(if_none_match: str | None, current_etag: str) -> bool:
    if not if_none_match:
        return False
    target = _strip_weak_prefix(current_etag)
    for part in if_none_match.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        if candidate == "*" or _strip_weak_prefix(candidate) == target:
            return True
    return False


def build_text_response(
    request: Request,
    content: str,
    *,
    media_type: str,
    status_code: int = 200,
    cache_control: str,
    enable_etag: bool = True,
) -> Response:
    body = content.encode("utf-8")
    headers = {"Cache-Control": cache_control}
    if enable_etag:
        etag = weak_etag(body)
        headers["ETag"] = etag
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
    if request.method.upper() == "HEAD":
        headers["Content-Length"] = str(len(body))
        return Response(
            content=b"",
            status_code=status_code,
            media_type=media_type,
            headers=headers,
        )
    return Response(
        content=body,
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )
