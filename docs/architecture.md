# 架构

市场包含两个应用，但生产环境按同一个服务器端服务部署：

1. `apps/market-web/` — Vue 3 + Vite SPA，渲染公开市场页面、主题系统、插件提交表单和用户浏览页面。生产构建产物输出到 `apps/market-web/dist`。
2. `apps/api/` — FastAPI 后端，提供 GitHub OAuth、插件 CRUD、审核、评论、点赞、公告和 API key 端点，并托管前端构建产物。使用 uvicorn 运行。

插件记录的权威来源是市场服务器及其数据库，而非 GitHub。仓库仅存储应用代码和 API 契约。GitHub OAuth 用于确认已登录用户是否拥有插件仓库或属于受信任的管理组织。

## 权限模型

- 首次启动向导注册内部核心管理员（core admin）
- 核心管理员可以授予/撤销管理员，发布公告
- 普通管理员可以上架/下架插件，删除评论，禁言用户，处理审核
- 插件所有者通过仓库所有权验证后，可编辑自己的插件元数据

## 持久化与基础设施

- **PostgreSQL** — 存储用户、插件、提交记录、评论、公告和 API key。
- **Redis** — 存储登录会话 token，使用 TTL 自动过期。

`apps/api/app/store.py` 提供两种实现：

- `InMemoryMarketStore` — 开发/首次启动用内存存储，不持久化。
- `PgRedisMarketStore` — 生产存储，配置 PostgreSQL 和 Redis 连接后自动启用。

PostgreSQL schema 在服务启动时自动创建。插件可变扩展字段使用 `jsonb`，标签字段建立 GIN 索引；用户、插件、评论等核心关系使用主键、唯一约束、外键和状态 CHECK 约束保护数据一致性。Redis session 使用 `SET key value EX seconds` 写入，并在读取 session 时刷新 TTL。

首次启动可通过前端 `/v1/setup` 页面完成基础设施配置。表单按主机名、端口、数据库名、账号、密码和 SSL 开关填写，并创建内部核心管理员。后端写入 `apps/api/data/runtime.env`、生成内部 `DATABASE_URL` / `REDIS_URL`，之后需要重启 API 进程。站点名称、图标、GitHub 登录开关、登录条款和服务条款同样可在此页面配置，并通过 `/v1/site` 提供给前端。

## 部署状态

生产部署不再使用 Vercel。标准流程是在服务器上执行 `npm run build:web`，然后启动 FastAPI：

```bash
npm install --prefix apps/market-web
npm run build:web
uv sync --project apps/api
npm run start:api
```

FastAPI 会按以下路径提供服务：

- `/` 和 SPA 路由：返回 `apps/market-web/dist/index.html`
- `/assets/...`、`/font/...` 等静态资源：返回对应构建文件
- `/plugins.json`、`/plugins-md5.json`：提供 AstrBot 自定义插件源
- `/v1/...`：提供市场 API

当前仓库内仍缺少完整生产运维配置：

- 容器化（Dockerfile / docker-compose）
- 编排（Kubernetes / Helm）
- 进程管理（systemd / supervisor）
- 反向代理（Nginx / Caddy）
- 基础设施即代码（Terraform）
- 数据库迁移工具（Alembic 等）

CI（`.github/workflows/ci.yml`）仅执行 lint、测试和前端构建，不包含部署步骤。

## AstrBot WebUI 集成

独立的 AstrBot WebUI 插件将通过 API key 消费此 API，不应在本地重复存储市场状态。API 通过 `/plugins.json` 和 `/plugins-md5.json` 端点提供与 AstrBot 自定义插件源兼容的数据格式。
