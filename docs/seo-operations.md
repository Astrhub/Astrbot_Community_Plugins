# SEO 运维指南

## 运行时 SEO 注入与动态 Sitemap

后端 FastAPI（`apps/api/app/seo_render.py`）在每次页面请求时读取 `apps/market-web/dist/index.html` 模板，按当前页面路由实时替换 `<!-- SSR_HEAD_START -->` / `<!-- SSR_HEAD_END -->` 与 `<!-- SSR_BODY -->` 占位，输出完整的 `<head>` 元数据、JSON-LD 以及轻量语义 `#app` 骨架（唯一 `<h1>`、摘要与可抓取 `<a href>` 链接），同时动态生成 `/sitemap.xml` 与 `/robots.txt`。

- 前端构建（`npm run build:web`）只需执行 `vp build`，不再需要 Chromium、`VITE_BASE_URL` 或在插件变更后重新构建前端。
- `/sitemap.xml` 自动汇总静态公开页（`/`、`/submit`、`/docs/rest`）、全部已上架插件的规范 3 段路径 `/plugin/{owner}/{name}`，以及拥有已上架插件的作者页 `/author/{owner}`，并在插件上架、下架、元数据更新、GitHub 同步和系统设置保存后立即失效内存缓存（默认 60 秒短缓存）。
- 站点规范域名严格取自运行期配置 `WEB_URL`（环境变量或核心管理员后台系统设置），不信任请求头 `Host` 或 `X-Forwarded-Host`，避免 Host Header 投毒污染索引与 CDN 缓存。
- 当数据库等持久化配置读取失败时，运行时 SEO 上下文会记录包含请求路径的异常日志，并回退到进程环境变量配置（`WEB_URL`、`SITE_NAME`、`SITE_SUBTITLE`、`SITE_DESCRIPTION`），不会使用过期的内存配置副本。此时 `/robots.txt`、不查询插件数据库的公开/私有页面以及与环境 `WEB_URL` 相同 origin 的已命中 `/sitemap.xml` 缓存仍可继续响应；若 `/sitemap.xml` 缓存未命中，重建过程仍依赖插件数据库。

> [!IMPORTANT]
> 公网部署时必须在运行期配置正确的环境级 `WEB_URL`（例如 `WEB_URL=https://your-domain`）；若环境变量中的 `WEB_URL` 与后台持久化配置不一致，数据库故障回退后会切换到环境 origin 对应的缓存键，无法命中此前按持久化 origin 生成的 sitemap 缓存。若 `WEB_URL` 为空或非法，服务端会记录告警并回退到 `http://127.0.0.1:8787`。

## Cloudflare 手动检查清单

如果站点前置接入了 Cloudflare，请在上线或大版本发布前逐项核对以下配置：

- **Bot Fight Mode**：关闭站点级 Bot Fight Mode，或在支持关闭 JavaScript Detections 的套餐中禁用该检测；确认返回的模块脚本保持 `type="module"`，且爬虫抓取到的 HTML 中未注入隐藏的 `/cdn-cgi/content` 链接。
- **缓存规则（Cache Rules / Browser Cache TTL）**：配置为遵循源站 `Cache-Control` 响应头，不要设置覆盖源站缓存分级的 Edge Cache TTL 或 Browser Cache TTL（缓存分级标准见仓库根目录 `AGENTS.md`）。
- **robots.txt 管理**：关闭 Cloudflare 托管的 `robots.txt` 以直接透传源站动态输出的 `/robots.txt`，或在托管配置中追加 `Sitemap: https://your-domain/sitemap.xml`。
- **缓存刷新（Purge）**：修改站点域名、品牌信息或重大路由变更后，按需定向清理 HTML 页面、`/sitemap.xml`、`/robots.txt` 及受影响的插件详情 URL；除非构建产物本身异常，不要全局清理带指纹哈希的 `/assets/*` 静态资源。

> [!TIP]
> 如需通过脚本调用 Cloudflare API 巡检上述配置，请使用具备 Zone 只读权限的 Account Token；无法使用 API 时，发布前在 Cloudflare 控制台逐项人工核对即可。

## 搜索引擎提交与上线抽检

在 Google Search Console、Bing Webmaster Tools 与百度搜索资源平台完成站点所有权验证，并提交 `https://your-domain/sitemap.xml`。每次涉及路由、SEO 元信息或运行时渲染逻辑的重大发布后，至少抽检以下页面：

1. 首页 `/`（验证唯一 `<h1>`、`WebSite` + `SearchAction` JSON-LD、`canonical` 与绝对路径 `og:image`）；
2. 任意已上架插件详情页 `/plugin/{owner}/{name}` 与作者页 `/author/{owner}`（验证 `SoftwareApplication` / `ProfilePage` JSON-LD、无 JS 时的可读骨架内容，以及旧 2 段路径 `/plugin/{id}` 返回真实 HTTP `404`）；
3. `/sitemap.xml`、`/robots.txt` 与任意不存在路径（验证 `Cache-Control`、`ETag`／`304` 协商和真实 HTTP `404`）。
