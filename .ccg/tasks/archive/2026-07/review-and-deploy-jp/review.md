# 单代理审查结果

## Critical

- 已修复：`scripts/prerender.mjs` 仍等待重构前的 `.plugin-profile`，导致生产构建必然超时。
- 已修复：逐页使用 `networkidle0`，会被 README/GitHub 等持续请求拖到导航超时；改为等待
  `domcontentloaded` 后按首页 `.plugin-card`、详情页 `#plugin-title` 判断业务就绪。
- 已修复：旧生产库的 `market_notifications` 表没有 `dedupe_key`，但启动时 `SCHEMA_SQL`
  先创建依赖该列的唯一索引，导致 API crash loop 和站点 502。现于主 schema 前执行幂等兼容
  DDL，并增加旧库启动顺序回归测试。

## Warning

- `npm audit` 报告 `vite-plus 0.2.3`、`vitest 4.1.9` 相关的 4 个 critical 开发工具链告警，
  修复版本由审计结果指向 `vite-plus 0.2.6`。这些包不进入当前 FastAPI 生产运行时，但本地/CI
  浏览器测试环境仍应尽快升级并重跑测试。

## Info

- `git diff --check` 通过。
- 后端：586 passed，20 skipped。
- Ruff：通过。
- 前端：20 files / 56 tests passed。
- 生产构建及 63 个插件路由 prerender 通过。
- 63 个插件快照均验证：唯一 H1、非空 title、站点 canonical、绝对 HTTPS Open Graph 图片。
- `jp` 独立 schema 启动预检通过，重启后服务 `active/running`、`NRestarts=0`。
- 公网首页、健康检查、插件 API、sitemap 和预渲染插件详情页均返回 200。
- HTML 缓存为 `public, max-age=0, must-revalidate`，指纹资源为
  `public, max-age=31536000, immutable`。
- 生产配置确认制品、增强审查、自动批准和运行时审查开关均为 `False`。
- 按用户要求未调用外部模型或审查代理。
