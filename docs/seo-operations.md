# SEO 运维指南

## 构建时效与预渲染产物

生产环境构建前端时，会从 `/v1/plugins` 拉取已上架插件清单，并从 `/v1/authors` 补充无上架插件作者，自动生成 `sitemap.xml`、`robots.txt` 以及首页、提交页、REST 文档页和各插件/作者详情页的静态预渲染 HTML。作者数据返回后会一次性提交标题、canonical 与 Open Graph 元信息，预渲染脚本等待这些元信息就绪后再保存快照。

- 建议至少每天定时重新构建一次前端，或在插件新上架、下架、重命名后立即触发重建。
- 构建时必须显式传入公网域名（例如 `VITE_BASE_URL=https://your-domain npm run build:web`），并在部署的 `apps/market-web/dist` 中保留生成的 `sitemap.xml`、`robots.txt`、`plugin/<owner>/<name>/index.html` 与 `author/<owner>/index.html`。

> [!IMPORTANT]
> 生产构建若遗漏 `VITE_BASE_URL`，构建脚本会自动跳过 Sitemap 生成与 Puppeteer 静态预渲染，导致搜索引擎抓取结果退化。

## Cloudflare 手动检查清单

如果站点前置接入了 Cloudflare，请在上线或大版本发布前逐项核对以下配置：

- **Bot Fight Mode**：关闭站点级 Bot Fight Mode，或在支持关闭 JavaScript Detections 的套餐中禁用该检测；确认返回的模块脚本保持 `type="module"`，且爬虫抓取到的 HTML 中未注入隐藏的 `/cdn-cgi/content` 链接。
- **缓存规则（Cache Rules / Browser Cache TTL）**：配置为遵循源站 `Cache-Control` 响应头，不要设置覆盖源站缓存分级的 Edge Cache TTL 或 Browser Cache TTL（缓存分级标准见仓库根目录 `AGENTS.md`）。
- **robots.txt 管理**：关闭 Cloudflare 托管的 `robots.txt` 以直接透传构建生成的静态文件，或在托管配置中追加 `Sitemap: https://your-domain/sitemap.xml`。
- **缓存刷新（Purge）**：每次生产重建并上线后，定向清理 HTML 页面、`sitemap.xml`、`robots.txt` 及受影响的插件详情 URL；除非构建产物本身异常，不要全局清理带指纹哈希的 `/assets/*` 静态资源。

> [!TIP]
> 如需通过脚本调用 Cloudflare API 巡检上述配置，请使用具备 Zone 只读权限的 Account Token；无法使用 API 时，发布前在 Cloudflare 控制台逐项人工核对即可。

## 搜索引擎提交与上线抽检

在 Google Search Console、Bing Webmaster Tools 与百度搜索资源平台完成站点所有权验证，并提交 `https://your-domain/sitemap.xml`。每次涉及路由、SEO 元信息或预渲染流程的重大发布后，至少抽检以下三类页面：

1. 首页（验证唯一 `<h1>`、`WebSite` + `SearchAction` JSON-LD、canonical 与绝对路径 `og:image`）；
2. 任意一个已上架插件详情页（验证 `SoftwareApplication` JSON-LD 与预渲染正文）；
3. 任意一个不存在的路径（验证源站返回真实 HTTP `404`）。
