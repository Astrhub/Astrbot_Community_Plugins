# AstrBot Community Plugins

服务端驱动的 AstrBot 社区插件市场。本仓库托管市场前端、后端 API 与 API 契约；插件记录、评论、点赞、审核状态、用户账户与站点配置均存储在市场服务器（PostgreSQL）上，而非 GitHub。

- 同一个 FastAPI 服务同时提供**网站**、**市场 API** 和 **AstrBot 插件源**。
- GitHub 仅作为身份来源（OAuth）与插件仓库元数据的抓取目标，不是插件数据的权威存储。

## 功能特性

- **浏览与检索**：插件卡片网格、关键词搜索、标签/分类筛选、多种排序（更新时间/星标/点赞/评论/随机）、模糊搜索、分页；插件详情弹窗内浏览 README 及仓库文件（Markdown 渲染、图片预览、目录导航、文本文件查看）。
- **插件包与审核**：ZIP 或公开 GitHub 仓库被固定为不可变 artifact，先进入私有隔离区，再执行基础校验、静态扫描和人工复核；只有过审版本才会获得 CDN 链接。
- **版本工作台**：`/plugin-workbench` 提供作者版本历史、结构化自动审查结果以及管理员待审队列、批准、拒绝、重试发布和安全下架。
- **GitHub 元数据同步**：后台 worker 周期性抓取仓库 stars、README、版本等信息；支持多 API token 轮转应对速率限制；所有者与管理员可手动触发刷新。
- **社区互动**：嵌套评论（支持 Markdown + 语法高亮）、插件与评论点赞、Giscus 评论集成；回复与点赞触发站内通知。
- **身份与角色**：内部核心管理员（用户名/密码登录）+ GitHub OAuth 登录；三级角色（core_admin / admin / user）+ 受信任 GitHub 组织自动提权。
- **用户管理**：管理员可创建内部用户、调整角色、禁言/解禁、删除用户；核心管理员管理管理员队伍。
- **API Key**：全局静态 Key（环境变量）、管理员动态颁发 Key、登录用户个人 Key（`sk-ah-` 前缀，原文仅返回一次），均支持 scopes 与 `Bearer` 鉴权。
- **API 文档与机器可读契约**：`/docs/rest` 提供 Vue API Reference 与在线试用；`/openapi.json` 和 `/llms.txt` 会按当前登录角色过滤可见端点，便于外部工具与 LLM 接入。
- **通知中心**：未读徽标、分页列表、标记已读、批量/单条/清空删除；用户可按回复/点赞维度关闭通知偏好。
- **运行时配置**：核心管理员在 `/admin/settings` 热更新站点展示、GitHub OAuth、市场功能开关、自动上架、最大标签数、邮件服务，无需重启进程。
- **首次启动向导**：未配置数据库时，前端 `/setup` 分步引导填写站点信息、核心管理员、PostgreSQL、Redis、邮件；保存后进程内热切换到持久化存储。
- **AstrBot 集成**：`/plugins.json` 与 `/plugins-md5.json` 输出兼容 AstrBot 自定义插件源格式，可直接作为 AstrBot 插件源。
- **邮件服务**：SMTP 或 Cloudflare Email Service 二选一，含每日发送限额。
- **主题**：亮色/暗色/跟随系统，页面切换动画。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3.5 + Vite+（Vite Plus）+ TypeScript + Naive UI + Pinia 3 + Vue Router 4；`marked` + `marked-alert` + `DOMPurify` + `highlight.js` 渲染 Markdown；`vue-api-playground` 在线试用 API；`@giscus/vue` 评论 |
| 后端 | Python 3.11+ · FastAPI · uvicorn · Pydantic 2 · asyncpg · redis-py（异步）· httpx |
| 存储 | PostgreSQL（市场、artifact、审查、任务与 outbox）· Redis（登录会话）· 本地私有目录或 S3/R2（隔离包与已发布包） |
| 部署 | Docker Compose · systemd · uv（Python 包管理）· npm（前端） |

> 前端使用 TypeScript（`<script setup lang="ts">`），类型定义见 `apps/market-web/src/types/index.ts`。API 调用使用原生 `fetch`（`credentials: 'include'` cookie session）；`axios` 仅作为历史依赖保留，当前未被引用。

## 项目结构

```
.
├── .vite-hooks/         # Vite+ pre-commit（前端检查）与 pre-push（后端 ruff）钩子
├── apps/
│   ├── market-web/      # Vue 3 + Vite+ + TypeScript 前端 SPA
│   └── api/             # FastAPI 后端（路由 / schemas / auth / store / tests）
├── deploy/systemd/      # 裸机部署的 systemd service 与 env 模板
├── docs/                # 架构、安全、OpenAPI 文档
├── Dockerfile           # 多阶段构建（前端 dist + 后端）
├── docker-compose.yml   # 单机部署（app + artifact-worker + PostgreSQL + Redis）
└── package.json         # 工作区根脚本（dev:api / dev:web / build:web / start:api / test）
```

## 快速开始（开发）

```bash
# 后端依赖
uv sync --project apps/api

# 前端依赖
npm install --prefix apps/market-web

# 分别启动（两个终端）
npm run dev:api    # FastAPI + uvicorn --reload，监听 127.0.0.1:8787
npm run dev:web    # Vite+ 开发服务器（vp dev），监听 0.0.0.0:3000
```

开发态前后端分属不同端口（前端 `3000`，后端 `8787`）。前端需通过环境变量指向后端，否则请求会打到前端自身域名：

```bash
# apps/market-web/.env（开发）
VITE_API_BASE_URL=http://127.0.0.1:8787
```

后端默认允许的 CORS 来源为 `http://127.0.0.1:3000,http://localhost:3000`（见 `apps/api/.env.example` 的 `CORS_ORIGIN`）。未配置 `DATABASE_URL`/`REDIS_URL` 时后端回退到内存存储，可直接启动；生产持久化存储请通过 `/setup` 或环境变量配置 PostgreSQL + Redis。

```bash
npm run build:web   # Vite+ 生产构建（vp build），输出到 apps/market-web/dist，供 FastAPI 托管
npm test            # 运行后端 pytest（使用内存存储，无需真实 PG/Redis）
cd apps/market-web && vp test --run  # 运行前端 Vite+ 测试
```

## 部署

生产部署时先构建前端，再由 FastAPI 直接托管 `apps/market-web/dist`。同一个服务对外提供：

- 网站首页与 SPA 路由：`http://your-host:8787/`
- AstrBot 插件源：`http://your-host:8787/plugins.json`
- 市场 API：`http://your-host:8787/v1/...`
- REST API 文档页：`http://your-host:8787/docs/rest`
- 角色过滤 OpenAPI：`http://your-host:8787/openapi.json`
- LLM 友好 API 索引：`http://your-host:8787/llms.txt`

> FastAPI 通过路径保留机制区分 API 路由（`v1`、`health`、`plugins.json`、`plugins-md5.json`、`openapi.json`、`llms.txt`、`docs`、`redoc`）与静态文件回退，互不冲突。REST API 文档可访问 `/docs/rest`（Vue + Naive UI API Reference + 在线试用）。原生 Swagger UI 与 ReDoc 已关闭，`/docs` 与 `/redoc` 返回 404。

后端依赖 **PostgreSQL**（持久化）与 **Redis**（会话）。两者均配置时启用 `PgRedisMarketStore`；否则回退 `InMemoryMarketStore`，仅适合开发与首次启动。

### Docker Compose

首次运行前先创建后端 `.env` 文件，否则 Docker 会把挂载目标创建成目录：

```bash
cp apps/api/.env.docker.example apps/api/.env
docker compose up -d --build
```

打开 `http://127.0.0.1:8787/setup` 完成初始化。compose 内置服务地址：

- PostgreSQL：host `postgres` / port `5432` / 默认库名、用户、密码均为 `market`（可用 `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` 覆盖）
- Redis：host `redis` / port `6379`

启用插件包功能时，先在 `apps/api/.env` 配置 `ARTIFACTS_ENABLED=true`、CDN 与存储，再启动独立 Worker：

```bash
docker compose --profile artifacts up -d --build
docker compose logs -f artifact-worker
```

API 进程不执行插件源码；P0/P1 的 Worker 只读取 ZIP、YAML 和 Python AST。后续 runtime smoke test 也必须继续运行在独立隔离容器中。

容器镜像：`node:24`（前端构建）、`python:3.11` + `uv:0.9.7`（后端）、`postgres:16-alpine`、`redis:7-alpine`。对外端口默认 `8787`，可用 `APP_PORT` 覆盖。

常用命令：

```bash
docker compose ps
docker compose logs -f app
docker compose restart app
docker compose down
```

### Systemd（裸机源码部署）

示例路径 `/opt/astrbot-community-plugins`，服务用户 `astrbot-market`：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin astrbot-market
sudo mkdir -p /opt /etc/astrbot-community-plugins
sudo rsync -a --delete ./ /opt/astrbot-community-plugins/
sudo chown -R astrbot-market:astrbot-market /opt/astrbot-community-plugins
cd /opt/astrbot-community-plugins
sudo cp deploy/systemd/astrbot-community-plugins.env.example /etc/astrbot-community-plugins/astrbot-community-plugins.env
sudo cp deploy/systemd/astrbot-community-plugins.service /etc/systemd/system/
sudo cp deploy/systemd/astrbot-artifact-worker.service /etc/systemd/system/
```

构建与安装依赖：

```bash
npm install --prefix apps/market-web
npm run build:web
uv sync --project apps/api --no-dev
```

依赖锁文件默认使用官方源。本地网络需要镜像时，**不要修改并提交锁文件**，仅在本机命令前设置环境变量：

```bash
export npm_config_registry=https://registry.npmmirror.com
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
npm install --prefix apps/market-web
uv sync --project apps/api --no-dev
```

Docker 构建同样默认用官方源，需要镜像时通过构建变量传入：

```bash
NPM_REGISTRY=https://registry.npmmirror.com \
PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
docker compose build
```

systemd service 已启用安全加固（`NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=full` / `ReadWritePaths` 限定后端目录）。编辑 env 后启动：

```bash
sudo systemctl daemon-reload
sudo mkdir -p /var/lib/astrbot-market/artifacts
sudo chown -R astrbot-market:astrbot-market /var/lib/astrbot-market
sudo systemctl enable --now astrbot-community-plugins
# 仅在 ARTIFACTS_ENABLED=true 且存储/CDN 配置完整时启用：
sudo systemctl enable --now astrbot-artifact-worker
sudo systemctl status astrbot-community-plugins
journalctl -u astrbot-community-plugins -f
```

若 `.env` 中暂不填写 `DATABASE_URL` 与 `REDIS_URL`，首次访问 `/setup` 完成初始化；初始化成功后后端会写入 `apps/api/.env`。生产环境通常还需在前置 Nginx/Caddy 启用 HTTPS，并相应设置 `COOKIE_SECURE=true`。

### 首次启动向导

若 PostgreSQL 或 Redis 缺失，前端会打开 `/setup`。向导只收集站点名称/图标、内部核心管理员、PostgreSQL、Redis 与邮件必要字段；GitHub OAuth、登录条款、服务条款和市场策略在核心管理员登录后到 `/admin/settings` 配置。

保存首次配置时，后端会先连接 PostgreSQL（目标库不存在则创建）、初始化 schema、验证 Redis、写入核心管理员；全部成功后才把基础设施连接与核心管理员引导信息写入 `apps/api/.env`，站点展示与系统设置写入数据库配置表。当前 FastAPI 进程会立即切换到 PostgreSQL/Redis 存储，**无需重启服务**。初始化完成后 `/v1/setup` 关闭，基础设施连接后续只通过 `.env` 调整。

### CI/CD

`.github/workflows/ci.yml` 仅执行代码质量检查（ruff lint + pytest + 前端 build），**不包含部署步骤**。artifact 表通过带 checksum 和 advisory lock 的版本化 SQL migration 初始化；尚未提供 Kubernetes/Helm、Nginx/Caddy 反向代理模板与 Terraform。

## 配置

### 后端环境变量

完整列表见 `apps/api/.env.example`，关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8787` | 监听地址与端口 |
| `CORS_ORIGIN` | `http://127.0.0.1:3000,http://localhost:3000` | 允许的前端来源（逗号分隔） |
| `WEB_URL` | `http://127.0.0.1:8787` | 站点公开 URL |
| `DATABASE_URL` / `REDIS_URL` | 空 | 首次留空，经 `/setup` 配置；同时配置才启用持久化存储 |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | 空 | GitHub OAuth App 凭证 |
| `GITHUB_CALLBACK_URL` | 空 | OAuth 回调 URL，留空则在 `/admin/settings` 配置 |
| `GITHUB_ADMIN_ORG` | 空 | 该组织成员登录后自动提权为 admin |
| `GITHUB_API_TOKEN` | 空 | 元数据同步用 GitHub token，多 token 用 `,` 分隔自动轮转 |
| `GITHUB_METADATA_SYNC_ENABLED` / `..._INTERVAL_SECONDS` | `true` / `3600` | 后台元数据同步开关与间隔（5 分钟 ~ 24 小时） |
| `MARKET_SUBMISSIONS_ENABLED` / `MARKET_COMMENTS_ENABLED` / `MARKET_LIKES_ENABLED` | `true` | 市场功能开关 |
| `PLUGIN_AUTO_APPROVE_ENABLED` | `false` | 仅保留给关闭 artifact 功能时的旧提交流程；artifact 流程永不绕过扫描与人工批准 |
| `MAX_PLUGIN_TAGS` | `8` | 插件最大标签数 |
| `ARTIFACTS_ENABLED` | `false` | 启用不可变插件包、审查任务和 CDN 发布门控 |
| `ARTIFACT_STORAGE_BACKEND` | `local` | `local` 或 `s3`（兼容 R2/S3 API） |
| `ARTIFACT_LOCAL_ROOT` | `/var/lib/astrbot-market/artifacts` | 本地隔离、文本清单与发布对象目录；API/Worker 必须共享 |
| `ARTIFACT_CDN_BASE_URL` | 空 | 已发布对象的公开 CDN 域名；隔离对象不会使用该域名 |
| `ARTIFACT_S3_*` / `ARTIFACT_*_BUCKET` | 空 | S3/R2 endpoint、区域、凭据、隔离桶与发布桶 |
| `ARTIFACT_MAX_UPLOAD_BYTES` / `...UNPACKED...` / `...MAX_FILES` | `32 MiB` / `128 MiB` / `2000` | ZIP 流式接收和预检硬限制 |
| `ARTIFACT_SUBMISSION_RPM` | `6` | Artifact ZIP/GitHub 提交按用户和来源 IP 的每分钟上限；`0` 关闭限制 |
| `EMAIL_PROVIDER` | `disabled` | `disabled` / `smtp` / `cloudflare` |
| `SESSION_MAX_AGE_SECONDS` | `604800` | 会话有效期（默认 7 天） |
| `COOKIE_SECURE` / `COOKIE_SAME_SITE` | `false` / `Lax` | Cookie 安全属性（生产 HTTPS 应设 `COOKIE_SECURE=true`） |
| `ENABLE_DEV_AUTH` | `true` | 开发调试登录（生产必须 `false`） |
| `MARKET_API_KEYS` | `local:dev-market-key:market:read\|market:write` | 全局静态 API Key，格式 `name:key:scope1\|scope2` |

> 环境变量优先级：默认读取 `apps/api/.env`，同名系统环境变量覆盖之；测试或特殊部署可用 `APP_ENV_FILE` 指向其他 env 文件。Web 后台保存的站点、OAuth、市场策略与邮件设置进入数据库配置表，并覆盖 `.env` 中的同名系统设置。

直接用环境变量启动生产持久化的最小示例：

```env
DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market
REDIS_URL=redis://127.0.0.1:6379/0
WEB_URL=https://your-market-domain
GITHUB_CALLBACK_URL=https://your-market-domain/v1/auth/github/callback
SITE_NAME=AstrBot Community Plugins
SITE_ICON_URL=/logo.webp
SITE_SUBTITLE=全新社区插件市场
SITE_DESCRIPTION=发现、评价和提交 AstrBot 插件。
EMAIL_PROVIDER=disabled
```

### 前端环境变量

定义在 `apps/market-web/.env.example`：

| 变量 | 说明 |
|---|---|
| `VITE_BASE_URL` | 站点公开基础 URL。留空时使用当前域名；构建时若已设置还会生成 `sitemap.xml` 与 `robots.txt` |
| `VITE_API_BASE_URL` | API 服务器地址（优先级最高）。开发态前后端分端口时需要；生产同源部署通常留空 |
| `VITE_COMMUNITY_REPO_URL` | 页脚展示的社区仓库链接 |

## 身份与角色

| 角色 | 能力 |
|---|---|
| **核心管理员**（core_admin） | 首次向导注册的内部账号；管理系统设置、管理员队伍、内部用户、发布公告 |
| **普通管理员**（admin） | 审核上架/下架插件、删除评论、禁言用户、处理审核、刷新任意插件元数据 |
| **插件所有者** | 经 GitHub 仓库所有权验证后，编辑自有插件元数据、申请上架、自主下架、手动刷新 |
| **普通用户**（user） | 浏览、提交插件、评论、点赞；管理个人 API Key 与通知偏好 |

GitHub 用户不会自动成为核心管理员；受信任组织成员（`GITHUB_ADMIN_ORG`）登录后可自动提权为 admin。已登录的内部管理员可在个人设置绑定 GitHub，绑定时会将该 GitHub 账号名下的插件、评论与提交记录合并到当前管理员账号。权限检查函数位于 `apps/api/app/auth.py`，详见 [docs/security.md](docs/security.md)。

## AstrBot 集成

AstrBot 可将本市场作为自定义插件源。在 AstrBot WebUI 中添加：

```text
https://your-market-domain/plugins.json
```

数据格式兼容 AstrBot 自定义仓库格式：`repo` 永远保留；`version` 随 GitHub 仓库 metadata 更新；`download_url` 仅在当前已发布 artifact 的规范化版本与仓库版本一致时输出。仓库出现未过审新版本时，旧 CDN 对象仍保留但不会冒充新版本，feed 的 `download_url` 为空，用户仍可选择 GitHub 直连。发布路径固定为 `/{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`。

未来的 AstrBot WebUI 插件将通过 API Key 消费本 API，不应在本地重复存储市场状态。

## 开发指引

```bash
# 后端
cd apps/api
uv sync
uv run uvicorn app.main:app --reload
uv run pytest

# 前端
cd apps/market-web
npm install
npm run dev      # Vite+ 开发服务器（vp dev）
npm run build    # Vite+ 生产构建（vp build）
vp test --run    # 前端 Vite+ 测试
```

安装 Git hooks：

```bash
# 前端 Vite+ pre-commit hook（暂存文件自动 vp check --fix）
# hook 脚本位于 .vite-hooks/pre-commit，手动创建符号链接激活：
ln -sf ../../.vite-hooks/pre-commit .git/hooks/pre-commit

# 后端 Ruff pre-push hook（推送前检查 Python 文件）
uv sync --project apps/api
uv run --project apps/api --directory apps/api pre-commit install --hook-type pre-push
```

- Python 3.11+，4 空格缩进，公共 helper 加类型注解，单一职责的小函数。
- 插件 ID 遵循 `astrbot_plugin_<name>` 模式；路由统一在 `/v1/*`，admin 与 core_admin 动作使用显式路径。
- 后端测试位于 `apps/api/tests/`，基于 pytest + FastAPI `TestClient`，覆盖角色检查、登录/会话、提交流程、所有权校验、审核与失败路径。
- 前端测试使用 `vp test --run`，放在对应源码旁边的 `*.test.ts` 文件中；当前覆盖插件卡片 logo 解析与默认 logo 回退。`npm test` 只是 `apps/market-web/package.json` 里的等价脚本别名。

## 许可

详见 [LICENSE](LICENSE)。
