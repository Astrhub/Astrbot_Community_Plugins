# Astrbot Community Plugins

Astrbot Community Plugins 是一个服务端驱动的 AstrBot 社区插件市场。GitHub 仓库仅托管市场源码和 API 契约，插件记录、评论、点赞、审核状态和用户账户数据均存储在市场服务器上。

## 项目结构

- `apps/market-web/` — Vue 3 + Vite 前端，用于浏览、搜索和提交插件。
- `apps/api/` — FastAPI 后端，提供 GitHub OAuth、角色权限、审核等接口。
- `docs/` — 架构、安全和 OpenAPI 文档。

## 部署方式

### 前端（market-web）

前端通过 **Vercel** 部署，配置文件为 `apps/market-web/vercel.json`，使用 SPA rewrite 规则。项目依赖中引入了 `@vercel/analytics` 和 `@vercel/speed-insights`。

环境变量通过 `VITE_BASE_URL` 指定后端 API 地址。若未设置，前端回退使用当前浏览器域名。

### 后端（api）

后端使用 **FastAPI + uvicorn**，依赖 **PostgreSQL**（持久化存储）和 **Redis**（会话/OAuth状态/缓存/限流）。当前仓库中的 `InMemoryMarketStore` 为开发用内存存储，生产级 PostgreSQL/Redis 存储适配器尚未实现。

**后端目前没有生产部署配置**。仓库内不存在：

- Dockerfile / docker-compose
- Kubernetes / Helm 配置
- systemd / supervisor / PM2 配置
- Nginx / Caddy 反向代理配置
- Terraform / 基础设施即代码配置
- 任何云平台（AWS/GCP/Azure/Railway/Render/Fly.io 等）的部署配置
- 数据库迁移脚本（无 Alembic 等）

首次启动时，若 `DATABASE_URL` 或 `REDIS_URL` 缺失，前端会打开 `/setup` 页面，将基础设施配置写入 `apps/api/data/runtime.env`，随后需重启 API 进程使配置生效。

### CI/CD

`.github/workflows/ci.yml` 仅包含代码质量检查（ruff lint + pytest + 前端 build），**没有部署步骤**。

## 开发

```bash
uv sync --project apps/api
npm install --prefix apps/market-web
npm run dev:api    # 启动 API，监听 127.0.0.1:8787
npm run dev:web    # 启动前端开发服务器
```

前端环境变量定义在 `apps/market-web/.env.example`：

- `VITE_BASE_URL` — FastAPI 公开访问地址。前端使用此变量构建 API 请求 URL，复制按钮追加 `/plugins.json`，sitemap 生成也使用同一域名。

后端使用 Python 3.11+ 和 `uv`：

```bash
cd apps/api
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
```

后端完整环境变量见 `apps/api/.env.example`。

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
