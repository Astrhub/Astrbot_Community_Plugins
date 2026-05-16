# 架构

市场分为两个可独立部署的部分：

1. `apps/market-web/` — Vue 3 + Vite SPA，渲染公开市场页面、主题系统、插件提交表单和用户浏览页面。当前通过 Vercel 部署（`vercel.json`）。
2. `apps/api/` — FastAPI 后端，提供 GitHub OAuth、插件 CRUD、审核、评论、点赞、公告和 API key 端点。使用 uvicorn 运行。

插件记录的权威来源是市场服务器及其数据库，而非 GitHub。仓库仅存储应用代码和 API 契约。GitHub OAuth 用于确认已登录用户是否拥有插件仓库或属于受信任的管理组织。

## 权限模型

- 第一个认证用户自动成为核心管理员（core admin）
- 核心管理员可以授予/撤销管理员，发布公告
- 普通管理员可以上架/下架插件，删除评论，禁言用户，处理审核
- 插件所有者通过仓库所有权验证后，可编辑自己的插件元数据

## 持久化与基础设施

- **PostgreSQL** — 存储用户、插件、提交记录、评论、公告和审计数据。
- **Redis** — 存储会话、OAuth 状态、缓存条目和限流计数。

当前代码中的 `InMemoryMarketStore`（`apps/api/app/store.py`）为开发用内存存储实现，所有数据存储在进程中。生产级 PostgreSQL/Redis 存储适配器尚未实现。

首次启动可通过前端 `/v1/setup` 页面完成基础设施配置，配置写入 `apps/api/data/runtime.env`（此目录在 `.gitignore` 中），之后需要重启 API 进程。

## 部署状态

**前端**：已配置 Vercel 部署（`apps/market-web/vercel.json`）。

**后端**：当前仓库内**没有生产部署配置**。缺少以下内容：

- 容器化（Dockerfile / docker-compose）
- 编排（Kubernetes / Helm）
- 进程管理（systemd / supervisor / PM2）
- 反向代理（Nginx / Caddy）
- 基础设施即代码（Terraform）
- 云平台配置（AWS / GCP / Azure / Railway / Render / Fly.io 等）
- 数据库迁移工具（Alembic 等）
- 生产启动脚本

CI（`.github/workflows/ci.yml`）仅执行 lint、测试和前端构建，不包含部署步骤。

## AstrBot WebUI 集成

独立的 AstrBot WebUI 插件将通过 API key 消费此 API，不应在本地重复存储市场状态。API 通过 `/plugins.json` 和 `/plugins-md5.json` 端点提供与 AstrBot 自定义插件源兼容的数据格式。
