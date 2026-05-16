# Astrbot Community Plugins

Astrbot Community Plugins 是一个服务端驱动的 AstrBot 社区插件市场。GitHub 仓库仅托管市场源码和 API 契约，插件记录、评论、点赞、审核状态和用户账户数据均存储在市场服务器上。

## 项目结构

- `apps/market-web/` — Vue 3 + Vite 前端，用于浏览、搜索和提交插件。
- `apps/api/` — FastAPI 后端，提供 GitHub OAuth、角色权限、审核等接口。
- `docs/` — 架构、安全和 OpenAPI 文档。

## 部署方式

### 服务器端部署

前端不再走 Vercel。生产部署时先构建 `apps/market-web`，再由 `apps/api` 的 FastAPI 应用直接托管 `apps/market-web/dist`：

```bash
npm install --prefix apps/market-web
npm run build:web
uv sync --project apps/api
npm run start:api
```

启动后同一个服务同时提供网站、API 和 AstrBot 插件源：

- 网站首页：`http://your-host:8787/`
- AstrBot 插件源：`http://your-host:8787/plugins.json`
- API：`http://your-host:8787/v1/...`

后端使用 **FastAPI + uvicorn**，依赖 **PostgreSQL**（持久化存储）和 **Redis**（会话存储）。配置 `DATABASE_URL` 与 `REDIS_URL` 后会启用 `PgRedisMarketStore`；未配置时回退到 `InMemoryMarketStore` 方便首次启动和本地开发。

仓库目前尚未提供完整生产运维配置，包括：

- Dockerfile / docker-compose
- Kubernetes / Helm 配置
- systemd / supervisor 配置
- Nginx / Caddy 反向代理配置
- Terraform / 基础设施即代码配置
- 数据库迁移脚本（无 Alembic 等）

首次启动时，若 `DATABASE_URL` 或 `REDIS_URL` 缺失，前端会打开 `/setup` 页面，将基础设施配置写入 `apps/api/data/runtime.env`，随后需重启 API 进程使配置生效。

PostgreSQL schema 会在后端启动时自动创建，包含用户、插件、提交记录、评论、公告和 API key 表；Redis 使用带过期时间的 session key 保存登录态。

### CI/CD

`.github/workflows/ci.yml` 仅包含代码质量检查（ruff lint + pytest + 前端 build），**没有部署步骤**。

## 开发

```bash
uv sync --project apps/api
npm install --prefix apps/market-web
npm run dev:api    # 启动 API，监听 127.0.0.1:8787
npm run dev:web    # 启动前端开发服务器
npm run build:web  # 构建生产前端，供 FastAPI 托管
npm run start:api  # 生产方式启动 API，监听 0.0.0.0:8787
```

本地安装推送前 Ruff hook：

```bash
uv sync --project apps/api
uv run --project apps/api --directory apps/api pre-commit install --hook-type pre-push
```

前端环境变量定义在 `apps/market-web/.env.example`：

- `VITE_BASE_URL` — 可选的公开基础 URL。留空时前端使用当前网站域名；部署在同一 FastAPI 服务下通常不需要设置。

后端使用 Python 3.11+ 和 `uv`：

```bash
cd apps/api
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
```

后端完整环境变量见 `apps/api/.env.example`。

生产启用持久化存储至少需要：

```env
DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market
REDIS_URL=redis://127.0.0.1:6379/0
WEB_URL=https://your-market-domain
GITHUB_CALLBACK_URL=https://your-market-domain/v1/auth/github/callback
```

## 身份与角色

- 第一个 GitHub 用户自动成为核心管理员（core admin）。
- 核心管理员可以授予或撤销管理员，发布公告。
- 普通管理员可以下架/上架插件，删除评论，禁言用户，处理插件审核。
- 插件所有者经过 GitHub 仓库所有权验证后，可编辑自己的插件元数据。

## 集成说明

未来的 AstrBot WebUI 插件将通过 API key 调用公开 API。市场网站仅支持 GitHub OAuth 登录和审核操作。插件通过网页表单提交，不走 GitHub Issues。

AstrBot 本身可将此市场作为自定义插件源。在 AstrBot WebUI 中添加：

```text
https://your-market-domain/plugins.json
```

数据格式兼容 AstrBot 当前的自定义仓库格式：以插件名为键的 JSON 对象，包含 `name`、`display_name`、`desc`、`author`、`repo`、`tags`、`version`、`logo`、`stars`、`updated_at`、`download_url`、`astrbot_version`、`category` 和 `support_platforms`。API 同时提供 `/plugins-md5.json` 用于 AstrBot 的源缓存校验。
