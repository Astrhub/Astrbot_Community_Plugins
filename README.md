# Astrbot Community Plugins

Astrbot Community Plugins 是一个服务端驱动的 AstrBot 社区插件市场。GitHub 仓库仅托管市场源码和 API 契约，插件记录、评论、点赞、审核状态和用户账户数据均存储在市场服务器上。

## 项目结构

- `apps/market-web/` — Vue 3 + Vite 前端，用于浏览、搜索和提交插件。
- `apps/api/` — FastAPI 后端，提供 GitHub OAuth、角色权限、审核等接口。
- `docs/` — 架构、安全和 OpenAPI 文档。

## 部署方式

### 服务器端部署

前端不再走 Vercel。生产部署时先构建 `apps/market-web`，再由 `apps/api` 的 FastAPI 应用直接托管 `apps/market-web/dist`。

启动后同一个服务同时提供网站、API 和 AstrBot 插件源：

- 网站首页：`http://your-host:8787/`
- AstrBot 插件源：`http://your-host:8787/plugins.json`
- API：`http://your-host:8787/v1/...`

后端使用 **FastAPI + uvicorn**，依赖 **PostgreSQL**（持久化存储）和 **Redis**（会话存储）。配置 PostgreSQL 与 Redis 后会启用 `PgRedisMarketStore`；未配置时回退到 `InMemoryMarketStore` 方便首次启动和本地开发。

仓库提供 Docker Compose 和 systemd 模板：

- `Dockerfile` / `docker-compose.yml` — 单机容器部署，包含 PostgreSQL 和 Redis。
- `deploy/systemd/` — 裸机源码部署的 systemd service 和环境变量模板。

尚未提供的运维配置包括：

- Kubernetes / Helm 配置
- Nginx / Caddy 反向代理配置
- Terraform / 基础设施即代码配置
- 数据库迁移脚本（无 Alembic 等）

首次启动时，若 PostgreSQL 或 Redis 缺失，前端会打开 `/setup` 页面。向导只收集站点名称/图标、内部核心管理员、PostgreSQL 和 Redis 必要字段；GitHub OAuth、条款、邮件和市场策略在核心管理员登录后到 `/settings` 配置。

核心管理员登录后可进入 `/settings` 管理运行时设置。当前支持站点名称/图标/描述、GitHub OAuth、登录条款、服务条款、市场提交/评论/点赞开关、自动上架、最大标签数，以及 SMTP 或 Cloudflare Email Service。密钥字段保存后只返回遮蔽状态；保持遮蔽值不会覆盖已有密钥。

保存首次配置时，后端会先连接 PostgreSQL，目标数据库不存在时尝试创建，再初始化 schema、验证 Redis，并把内部核心管理员写入目标库；全部成功后只把基础设施连接和核心管理员引导信息写入 `apps/api/.env`，站点展示和后续系统设置写入数据库配置表。当前 FastAPI 进程会立即切换到 PostgreSQL/Redis 存储，无需依赖 systemd、Docker 或 supervisor 重启服务。PostgreSQL schema 在后续启动时也会自动补齐；Redis 使用带过期时间的 session key 保存登录态。初始化完成后 `/v1/setup` 关闭，数据库或 Redis 连接后续只通过 `.env` 调整。

### Docker Compose

首次运行前先创建后端 `.env` 文件，否则 Docker 会把挂载目标创建成目录：

```bash
cp apps/api/.env.docker.example apps/api/.env
docker compose up -d --build
```

打开 `http://127.0.0.1:8787/setup` 完成初始化。compose 内置服务地址如下：

- PostgreSQL host：`postgres`
- PostgreSQL port：`5432`
- PostgreSQL database/user/password：默认都是 `market`，也可通过 `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` 覆盖
- Redis host：`redis`
- Redis port：`6379`

常用命令：

```bash
docker compose ps
docker compose logs -f app
docker compose restart app
docker compose down
```

### Systemd

裸机部署示例路径为 `/opt/astrbot-community-plugins`，服务用户为 `astrbot-market`：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin astrbot-market
sudo mkdir -p /opt /etc/astrbot-community-plugins
sudo rsync -a --delete ./ /opt/astrbot-community-plugins/
sudo chown -R astrbot-market:astrbot-market /opt/astrbot-community-plugins
cd /opt/astrbot-community-plugins
sudo cp deploy/systemd/astrbot-community-plugins.env.example /etc/astrbot-community-plugins/astrbot-community-plugins.env
sudo cp deploy/systemd/astrbot-community-plugins.service /etc/systemd/system/
```

构建和安装依赖：

```bash
npm install --prefix apps/market-web
npm run build:web
uv sync --project apps/api --no-dev
```

编辑 `/etc/astrbot-community-plugins/astrbot-community-plugins.env` 后启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now astrbot-community-plugins
sudo systemctl status astrbot-community-plugins
journalctl -u astrbot-community-plugins -f
```

若 `.env` 中暂不填写 `DATABASE_URL` 和 `REDIS_URL`，首次访问 `/setup` 完成初始化；初始化后后端会写入 `apps/api/.env`。生产环境通常还需要在前面放 Nginx/Caddy 并启用 HTTPS。

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

生产启用持久化存储可通过首次启动页面填写。若要直接用环境变量启动，至少需要：

```env
DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market
REDIS_URL=redis://127.0.0.1:6379/0
WEB_URL=https://your-market-domain
GITHUB_CALLBACK_URL=https://your-market-domain/v1/auth/github/callback
SITE_NAME=AstrBot Community Plugins
SITE_ICON_URL=/logo.webp
SITE_SUBTITLE=全新社区插件市场
SITE_DESCRIPTION=发现、评价和提交 AstrBot 插件。
GITHUB_LOGIN_ENABLED=false
PUBLIC_LOGIN_ENABLED=true
MARKET_SUBMISSIONS_ENABLED=true
MARKET_COMMENTS_ENABLED=true
MARKET_LIKES_ENABLED=true
EMAIL_PROVIDER=disabled
```

## 身份与角色

- 首次启动向导注册内部核心管理员（core admin），用于配置 GitHub OAuth 和高级管理项。
- 核心管理员可以授予或撤销管理员，发布公告。
- 普通管理员可以下架/上架插件，删除评论，禁言用户，处理插件审核。
- 插件所有者经过 GitHub 仓库所有权验证后，可编辑自己的插件元数据。

## 集成说明

未来的 AstrBot WebUI 插件将通过 API key 调用公开 API。市场网站支持内部核心管理员登录和可配置的 GitHub OAuth 登录；插件通过网页表单提交，不走 GitHub Issues。

邮件服务参考 Cloudflare Email Service 的 REST API：当 `EMAIL_PROVIDER=cloudflare` 时，后端使用 `POST /accounts/{account_id}/email/sending/send` 发送站点邮件；也可切换到 SMTP。

AstrBot 本身可将此市场作为自定义插件源。在 AstrBot WebUI 中添加：

```text
https://your-market-domain/plugins.json
```

数据格式兼容 AstrBot 当前的自定义仓库格式：以插件名为键的 JSON 对象，包含 `name`、`display_name`、`desc`、`author`、`repo`、`tags`、`version`、`logo`、`stars`、`updated_at`、`download_url`、`astrbot_version`、`category` 和 `support_platforms`。API 同时提供 `/plugins-md5.json` 用于 AstrBot 的源缓存校验。
