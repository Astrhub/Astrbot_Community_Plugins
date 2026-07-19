# SEO operations

## Build freshness

Production builds fetch the listed plugin inventory to generate sitemap entries and prerendered
plugin detail pages. Run a scheduled CI rebuild at least daily, or trigger a rebuild immediately
after a plugin is approved, listed, unlisted, or renamed. Always build with
`VITE_BASE_URL=https://plugins.eloina.cn` and retain the generated `sitemap.xml` and
`plugin/*/index.html` inside the deployed frontend dist.

## Cloudflare manual checklist

- Disable Bot Fight Mode for the site, or use a plan/configuration where JavaScript Detections can
  be disabled. Confirm the delivered module script remains `type="module"` and no hidden
  `/cdn-cgi/content` link is injected into crawler HTML.
- Configure Cache Rules and Browser Cache TTL to respect origin Cache-Control. Do not set an Edge
  Cache TTL or Browser TTL that overrides the origin classes documented in `AGENTS.md`.
- Disable Cloudflare managed robots.txt and serve the generated file, or append
  `Sitemap: https://plugins.eloina.cn/sitemap.xml` to the managed robots configuration.
- Purge HTML, sitemap, robots, and affected plugin detail URLs after each production rebuild. Do
  not purge fingerprinted assets globally unless the build artifact itself is invalid.

Cloudflare API inspection requires a valid account token with zone read permissions. If automated
inspection is unavailable, verify these values in the dashboard before release.

## Search engine submission

Verify ownership for Google Search Console, Bing Webmaster Tools, and Baidu Search Resource
Platform. Submit `https://plugins.eloina.cn/sitemap.xml` to each service and inspect one homepage,
one plugin detail page, and one intentionally missing URL after every major SEO deployment.
