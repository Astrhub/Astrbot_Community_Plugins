# AstrBot Community Plugins

服务端驱动的 AstrBot 社区插件市场。仓库托管市场前端、FastAPI 后端、插件包审查链路与部署模板；插件记录、评论、点赞、审核状态、用户账户与站点配置均保存在市场服务端，不依赖 GitHub 作为业务数据库。

- 同一个 FastAPI 服务同时对外提供 **Web 市场**、**REST API** 与 **AstrBot 插件源**（`/plugins.json`）。
- 支持**完整社区模式**（PostgreSQL + Redis + 可选插件包审查与 CDN）与**本地自用单进程模式**（单文件 JSON 持久化，无需 PostgreSQL 和 Redis）。
- 支持通过 **GitHub**、**GitLab.com**、**Gitee**、**AtomGit / GitCode** 与 **Gitea（gitea.com）** 进行 OAuth 身份登录、仓库归属校验以及插件元数据与归档抓取。

## 功能特性

- **浏览、检索与 SEO**：插件卡片网格、关键词与模糊搜索、标签与官方分类筛选、多种排序（更新时间、星标、点赞、评论、随机）与 URL 查询分页；插件详情页（`/plugin/{username}/{name}`）与作者页（`/author/{username}`）支持独立路由、Markdown README 渲染、仓库目录浏览、文本/图片预览以及运行时 SEO 与 Sitemap；海外平台（`GitLab.com` / `Gitea`）的插件 Logo、README 仓库内图片与海外用户头像提供受限服务端代理，国内平台（Gitee / AtomGit）保持直连。
- **可选社区源 CDN 与插件包审查**：提交插件默认关闭 CDN（通过原仓库直连安装，非 GitHub 仓库直连需 AstrBot `v4.27.0+`）；按插件开启 CDN 后，ZIP、公开 GitHub 仓库（优先采用正式 Release ZIP）或受支持第三方 OAuth 平台仓库（`source_type="git"`，按固定 commit SHA 拉取官方 HTTPS 归档）会固定为不可变 artifact 进入私有隔离区，经预检、静态分析、依赖漏洞/ClamAV/YARA/LLM 辅助审查、一次性容器运行时探针和人工复核后才发布到 CDN；管理员自有 CDN 插件可选白名单直发，在通过压缩包预检并写入审计决策后直接发布。
- **版本审查工作台**：`/plugin-workbench` 提供作者版本历史、文件 diff 与自动审查发现，以及管理员待审队列、评论、人工批准、重跑审查、重试发布与安全下架。
- **GitHub 元数据同步**：后台任务周期性同步仓库星标、版本与 README，支持多 API Token 轮转应对速率限制；插件所有者与管理员也可手动触发刷新。
- **社区互动与通知中心**：服务端持久化嵌套评论（Markdown + 语法高亮）、插件与评论点赞；回复、点赞、审核与下架事件自动触发站内通知，支持按维度配置站内与邮件通知偏好。
- **身份、权限与 API Key**：内置核心管理员账号登录、邮箱注册与多平台 OAuth 登录（GitHub / GitLab.com / Gitee / AtomGit / Gitea）；五级身份层级（`core_admin` / `admin` / `developer` / `user` / `guest`）与受信任 GitHub 组织自动提权；支持全局静态 Key、管理员颁发 Key 与个人 Key（`sk-ah-` 前缀，明文仅展示一次），按 `scopes` 通过 `Bearer` 鉴权。
- **运行时配置与首次启动向导**：首次启动自动进入 `/setup` 分步初始化并热切换存储，无需重启进程；核心管理员可在 `/admin/settings` 热更新站点信息、GitHub OAuth、市场开关、邮件服务（SMTP / Cloudflare Email）与同步策略。
- **API 文档与机器可读契约**：`/docs/rest` 提供内置 REST API 文档与在线试用；`/openapi.json` 和 `/llms.txt` 按当前登录角色动态过滤端点。

## 技术栈

| 层 | 技术选型 |
| --- | --- |
| 前端 | Vue 3.5 · Vite+（Vite Plus）· TypeScript · Naive UI · Pinia 3 · Vue Router 4 · `@unhead/vue` · `marked` + `marked-alert` + `DOMPurify` + `highlight.js` · `vue-api-playground` |
| 后端 | Python 3.11+ · FastAPI · uvicorn · Pydantic 2 · asyncpg · redis-py（异步）· httpx · cryptography · boto3 · aiosmtplib · yara-python |
| 存储 | 完整模式：PostgreSQL（业务数据、artifact、审查任务与 outbox）+ Redis（会话与加密短期 OAuth 凭据）+ 本地私有目录或 S3/R2；本地自用模式：单文件 JSON 原子持久化 |
| 部署与构建 | Docker Compose · systemd · uv（Python）· npm / Vite+（前端）· Puppeteer 预渲染 |

> [!NOTE]
> 前端全部采用 `<script setup lang="ts">` 与原生 `fetch`（`credentials: "include"` cookie 会话），类型定义集中在 [apps/market-web/src/types/index.ts](apps/market-web/src/types/index.ts)。

## 项目结构

```text
.
├── .github/workflows/ci.yml    # 按变更路径执行检查的 GitHub Actions 工作流
├── .vite-hooks/                # 前端 pre-commit（vp staged）与后端 pre-push（ruff）钩子
├── apps/
│   ├── market-web/             # Vue 3 + Vite+ + TypeScript 前端应用
│   └── api/                    # FastAPI 后端、artifact 审查管线、runtime-runner 与 pytest 套件
├── deploy/
│   ├── compose/                # Docker Compose 角色分离 env 模板（API / artifact-worker）
│   ├── runtime-runner/         # 运行时探针隔离代理与构建脚本
│   └── systemd/                # 裸机部署的 systemd unit 与 env 模板
├── docs/                       # 架构、安全、运维手册与 OpenAPI 契约
├── Dockerfile                  # 多阶段镜像（按需安装 Chromium 预渲染；api / runtime-runner 目标）
├── Dockerfile.runtime-probe    # 固定基础镜像 digest 的一次性插件运行时探针镜像
├── Dockerfile.runtime-proxy    # 探针受限出站包安装代理镜像
├── docker-compose.yml          # 完整版编排（app + PostgreSQL + Redis + 可选 artifacts/runtime-runner）
├── docker-compose-local.yml    # 本地自用单进程版编排（仅 app，单文件持久化，无需 PG/Redis）
├── .env.example                # 根目录 Docker Compose 环境变量示例
├── SECURITY.md                 # 安全漏洞报告策略与受支持版本说明
└── package.json                # 根目录快捷脚本（dev:api / dev:web / build:web / start:api / test）
```

## 快速开始（本地开发）

```bash
# 安装后端与前端依赖
uv sync --project apps/api
npm install --prefix apps/market-web

# 在两个终端分别启动后端与前端
npm run dev:api    # FastAPI（uvicorn --reload），监听 127.0.0.1:8787
npm run dev:web    # Vite+ 开发服务器（vp dev），监听 0.0.0.0:3000
```

开发态前后端运行在不同端口，需要在 `apps/market-web/.env` 中指明后端地址，否则前端请求会发往自身端口：

```env
# apps/market-web/.env
VITE_API_BASE_URL=http://127.0.0.1:8787
```

后端默认允许的跨域来源为 `http://127.0.0.1:3000,http://localhost:3000`。未配置持久化存储时后端会回退到内存模式，适合直接调试；若希望本地开发也能持久化，可设置 `MARKET_STORAGE_MODE=local`，或在 `/setup` 中连接 PostgreSQL 与 Redis。

常用本地构建与测试命令：

```bash
npm run build:web                       # 生产构建前端到 apps/market-web/dist
npm test                                # 运行后端 pytest（无需真实 PG/Redis）
npm --prefix apps/market-web test       # 运行前端 Vitest（vp test --run）
```

## 部署指南

生产部署时由 FastAPI 直接托管构建好的 `apps/market-web/dist`，同一服务同时暴露以下入口：

- 网站与 SPA 页面：`http://your-host:8787/`
- AstrBot 插件源：`http://your-host:8787/plugins.json`（以及 `/plugins-md5.json`）
- 市场 API：`http://your-host:8787/v1/...`
- REST API 文档页：`http://your-host:8787/docs/rest`
- 角色过滤 OpenAPI 与 LLM 索引：`http://your-host:8787/openapi.json`、`http://your-host:8787/llms.txt`

> [!NOTE]
> FastAPI 通过保留路径区分 API、Feed、爬虫文件与前端页面，未知路径返回真实 HTTP 404。原生 Swagger UI 和 ReDoc 已关闭（`/docs`、`/redoc` 返回 404），请统一访问 `/docs/rest`。

### 模式选择

| 部署模式 | 适用场景 | 外部依赖 | 核心特性 |
| --- | --- | --- | --- |
| **本地自用单进程版**（`docker-compose-local.yml`） | 个人自建市场与私有 AstrBot 插件源 | 无（单容器单进程） | 单文件 JSON 原子持久化、三步向导、管理员免 OAuth 直填仓库提交、跳过 Chromium 构建 |
| **完整社区版**（`docker-compose.yml` / `systemd`） | 公开社区市场、多人协作与 CDN 分发 | PostgreSQL 16 + Redis 7 | GitHub OAuth 归属校验、不可变 artifact 审查管线、独立 Worker 与容器沙箱探针、SEO 预渲染 |

### 方式一：Docker Compose（本地自用单进程版）

如果只需要一个轻量、开箱即用的个人插件市场和 AstrBot 插件源，无需准备 PostgreSQL、Redis 或手动创建 `apps/api/.env`，直接启动即可：

```bash
docker compose -f docker-compose-local.yml up -d --build
```

启动后的默认行为：

1. 数据文件与初始化配置保存在 Docker 命名卷 `market-local-data`（容器内 `/var/lib/astrbot-market/data/`），重建或重启容器不会丢失数据。
2. 构建阶段不再安装 Chromium 或执行构建期预渲染，SEO 元数据、`/sitemap.xml` 与 `/robots.txt` 由后端按运行期 `WEB_URL` 实时生成，镜像更轻、构建更快。
3. 访问 `http://127.0.0.1:8787/setup`，只需填写站点名称和核心管理员账号密码即可启用；登录后可在 `/submit` 直接粘贴公开 GitHub 插件仓库地址读取元数据并上架。

> [!TIP]
> 若希望跳过网页向导或自定义端口与 GitHub Token，可先执行 `cp .env.example .env`，按需填写 `APP_PORT`、`GITHUB_API_TOKEN`、`CORE_ADMIN_USERNAME` 和 `CORE_ADMIN_PASSWORD` 后再启动。明文密码仅在启动时用于生成哈希，不会回写磁盘。

### 方式二：Docker Compose（完整社区版）

> [!IMPORTANT]
> 完整版挂载宿主机文件 `./apps/api/.env` 到容器内。首次启动前必须先创建该文件并把属主设为容器默认非 root UID/GID `10001:10001`，否则 Docker 会将其误建为目录，且 `/setup` 无法写回配置。

```bash
cp .env.example .env
cp apps/api/.env.docker.example apps/api/.env
sudo chown 10001:10001 apps/api/.env
docker compose up -d --build
```

> [!TIP]
> 面向公网部署时，请在根目录 `.env` 或后台系统设置中配置 `WEB_URL=https://your-domain`；后端会按运行期 `WEB_URL` 实时注入 SEO 元数据并动态提供 `/sitemap.xml` 与 `/robots.txt`，无需在构建期注入域名或重新构建前端。

打开 `http://127.0.0.1:8787/setup` 完成初始化，Compose 内置服务连接信息如下：

- **PostgreSQL**：主机 `postgres`，端口 `5432`，默认库名、用户名与密码均为 `market`（可在根目录 `.env` 用 `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` 覆盖）。
- **Redis**：主机 `redis`，端口 `6379`。

完成 `/setup` 后，可将 `docker-compose.yml` 中 `apps/api/.env` 的挂载权限调整为 `:ro`。常用运维命令：

```bash
docker compose ps
docker compose logs -f app
docker compose restart app
docker compose down
```

如需启用不可变插件包审查与 CDN 分发，请为 API 与 Worker 使用分离的环境文件：先从 [deploy/compose/artifact-worker.env.example](deploy/compose/artifact-worker.env.example) 复制一份不入库的私有配置，填好数据库、存储、CDN 和审查工具参数并将 `ARTIFACTS_ENABLED=true`，再启动 `artifacts` profile：

```bash
ARTIFACT_WORKER_ENV_FILE=/etc/astrbot-community-plugins/artifact-worker.env \
  docker compose --profile artifacts up -d --build
docker compose logs -f artifact-worker
```

> [!CAUTION]
> API 进程和 `artifact-worker` 严禁挂载 Docker socket 或直接执行插件源码。运行时烟雾测试必须通过独立的 `runtime-runner` profile 和一次性隔离容器运行，隔离边界与事故处置详见 [docs/runtime-runner.md](docs/runtime-runner.md) 与 [docs/plugin-review-operations.md](docs/plugin-review-operations.md)。

运行时探针的 source-backed 适配器当前固定对应 AstrBot `4.26.6`（提交 `5d10e0d428b41308cc63215db00359c61ee17195`）。

> [!WARNING]
> 升级 AstrBot 源码前必须先完成插件生命周期与工具注册路径的审计和验证，不要仅修改版本号。

### 方式三：Systemd（裸机源码部署）

裸机生产部署默认采用三用户隔离模型（`astrbot-market-api`、`astrbot-market-worker`、`astrbot-runtime`）与分离的 `/etc/astrbot-community-plugins/*.env` 配置文件，共享目录通过 `astrbot-market` 组控制读写权限。详细建用户、目录权限和 rootless 容器引擎步骤见 [docs/plugin-review-operations.md](docs/plugin-review-operations.md)。

```bash
sudo mkdir -p /opt /etc/astrbot-community-plugins
sudo rsync -a --delete ./ /opt/astrbot-community-plugins/
cd /opt/astrbot-community-plugins

sudo cp deploy/systemd/astrbot-community-plugins-api.env.example /etc/astrbot-community-plugins/api.env
sudo cp deploy/systemd/astrbot-artifact-worker.env.example /etc/astrbot-community-plugins/artifact-worker.env
sudo cp deploy/systemd/astrbot-runtime-runner.env.example /etc/astrbot-community-plugins/runtime-runner.env
sudo cp deploy/systemd/astrbot-community-plugins.service /etc/systemd/system/
sudo cp deploy/systemd/astrbot-artifact-worker.service /etc/systemd/system/
sudo cp deploy/systemd/astrbot-runtime-runner.service /etc/systemd/system/

npm install --prefix apps/market-web
npm run build:web
uv sync --project apps/api --no-dev
```

完成 `/etc/astrbot-community-plugins/*.env` 配置与服务用户创建后启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now astrbot-community-plugins
# 仅在 ARTIFACTS_ENABLED=true 且存储/CDN 配置完整时启用：
sudo systemctl enable --now astrbot-artifact-worker
# 仅在 rootless engine、安装代理、最小权限数据库角色与 runner env 验证完成后启用：
sudo systemctl enable --now astrbot-runtime-runner
```

> [!IMPORTANT]
> systemd 模板将 `/etc/astrbot-community-plugins/` 视为只读配置，不支持通过 Web `/setup` 写回 `/etc`，请在启动服务前完成数据库、核心管理员与密钥配置。前置 Nginx/Caddy 启用 HTTPS 后务必设置 `COOKIE_SECURE=true`。

<details>
<summary>使用国内镜像源安装依赖或构建 Docker 镜像</summary>

依赖锁文件默认指向官方源，需要加速时请通过临时环境变量传入镜像地址：

```bash
export npm_config_registry=https://registry.npmmirror.com
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
npm install --prefix apps/market-web
uv sync --project apps/api --no-dev
```

Docker 构建同样支持通过构建变量传入：

```bash
NPM_REGISTRY=https://registry.npmmirror.com \
PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
docker compose build
```

> [!WARNING]
> 请勿因为使用国内镜像而修改并提交 `package-lock.json` 或 `uv.lock`。

</details>

### 首次启动向导（`/setup`）

- **本地自用模式（`MARKET_STORAGE_MODE=local`）**：未配置核心管理员时打开 `/setup`，向导精简为 **站点 → 管理员 → 确认** 三步，保存后直接写入本地持久化文件与 env 文件并立即生效。
- **完整社区模式（`MARKET_STORAGE_MODE=postgres`）**：PostgreSQL 或 Redis 未配置时打开 `/setup`，向导引导填写 **站点 → 管理员 → PostgreSQL → Redis → 确认** 五步；后端会先验证连接（目标库不存在时自动建库）、初始化表结构并创建核心管理员，全部成功后才写回 `.env` 并在进程内热切换存储，无需重启。
- 初始化完成后 `/v1/setup` 与 `/v1/setup/status` 自动关闭（返回 404）；GitHub OAuth、邮件服务、服务条款与市场策略随后由核心管理员在 `/admin/settings` 配置。

### CI/CD

[.github/workflows/ci.yml](.github/workflows/ci.yml) 仅在代码或部署配置改动时触发，并按变更类型运行对应检查，不包含自动部署步骤：

- 仅后端或部署配置改动（`apps/api/**`、`Dockerfile*`、`docker-compose*.yml`、`deploy/**`）：运行 `Python API`（`ruff check` + `pytest`）。
- 仅前端改动（`apps/market-web/**`）：运行 `Vue market`（`vp check` + `vp test` + `vp build`）。
- 纯文档改动（如 `README.md`、`SECURITY.md`、`docs/**`、`LICENSE`）：不触发 `Code Quality Checks` 工作流。
- 修改 `.github/workflows/ci.yml` 本身、进入 `merge_group` 或手动触发（`workflow_dispatch`）：同时运行 `Python API` 与 `Vue market` 全部检查。

## 配置参考

### 后端环境变量

完整模板见 [apps/api/.env.example](apps/api/.env.example) 与 [apps/api/.env.docker.example](apps/api/.env.docker.example)。

> [!NOTE]
> 配置优先级从低到高为：`apps/api/.env`（或 `APP_ENV_FILE` 指定文件）→ 系统环境变量 → Web 后台 `/admin/settings` 写入持久化配置表的运行时设置。基础设施连接（`DATABASE_URL`、`REDIS_URL`、`MARKET_STORAGE_MODE`、`LOCAL_DATA_PATH`）仅从环境层读取。

<details open>
<summary>常用后端环境变量速查</summary>

| 变量 | 代码默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8787` | 服务监听地址与端口 |
| `CORS_ORIGIN` | `http://127.0.0.1:3000,http://localhost:3000` | 允许的前端来源（逗号分隔） |
| `WEB_URL` | `http://127.0.0.1:8787` | 站点公开访问 URL |
| `MARKET_STORAGE_MODE` | `postgres` | 存储模式：`postgres`（PostgreSQL + Redis）或 `local`（本地单文件持久化） |
| `LOCAL_DATA_PATH` | `<env 目录>/data/market-state.json` | 本地模式的数据文件路径 |
| `CORE_ADMIN_USERNAME` / `CORE_ADMIN_PASSWORD` | 空 | 可选：启动时自动初始化内部核心管理员（明文仅启动时哈希，不落盘） |
| `DATABASE_URL` / `REDIS_URL` | 空 | `postgres` 模式下同时配置后启用持久化存储；首次部署可留空并在 `/setup` 配置 |
| `OAUTH_CREDENTIAL_KEY` | 空（必填） | 独立凭据加密主密钥（至少 16 位，仅环境变量注入）；启用 OAuth 平台或加密保存对象存储 Profile 凭据前必须配置（按独立 HKDF 域隔离派生） |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | 空 | GitHub OAuth App 凭证（`CLIENT_SECRET` 仅由部署环境注入，不存入数据库） |
| `GITHUB_CALLBACK_URL` | `http://127.0.0.1:8787/v1/auth/github/callback` | GitHub OAuth 回调地址（生产启用登录时必须使用公网域名） |
| `GITLAB_*` / `GITEE_*` / `ATOMGIT_*` / `GITEA_*` | 默认关闭 | 第三方已知平台 OAuth 配置（`*_LOGIN_ENABLED`、`*_CLIENT_ID`、`*_CLIENT_SECRET`、`*_CALLBACK_URL`、`*_SCOPE` 及 `ATOMGIT_API_BASE_URL`）；所有 `*_CLIENT_SECRET` 仅环境变量注入 |
| `GITHUB_ADMIN_ORG` | 空 | 该 GitHub 组织成员登录后自动提权为 `admin` |
| `GITHUB_API_TOKEN` | 空 | 元数据同步用 GitHub Token，支持逗号分隔多 Token 自动轮转 |
| `GITHUB_METADATA_SYNC_ENABLED` / `..._INTERVAL_SECONDS` | `true` / `3600` | 后台元数据同步开关与周期（300 ~ 86400 秒） |
| `MARKET_SUBMISSIONS_ENABLED` / `MARKET_COMMENTS_ENABLED` / `MARKET_LIKES_ENABLED` | `true` | 插件提交、评论、点赞功能开关 |
| `PLUGIN_AUTO_APPROVE_ENABLED` | `false` | 仅对未开启 CDN 的普通提交生效；开启 CDN 的包审查永不绕过扫描与人工复核 |
| `MAX_PLUGIN_TAGS` | `8` | 单个插件允许的最大标签数 |
| `ARTIFACTS_ENABLED` | `false` | 启用不可变插件包、审查任务队列与 CDN 发布门控 |
| `ARTIFACT_STORAGE_BACKEND` / `ARTIFACT_LOCAL_ROOT` | `local` / `/var/lib/astrbot-market/artifacts` | 默认遗留隔离区与发布区存储后端（`local` 或 `s3`）及本地根目录；更灵活的多 Profile 存储（SQLite / PostgreSQL / S3 / WebDAV / ImgBed）与用途迁移可在 `/admin/settings`「基础设施」标签页配置 |
| `ARTIFACT_CDN_BASE_URL` | 空 | 已发布对象的公开 CDN 域名（隔离对象永不暴露在该域名下） |
| `ARTIFACT_S3_*` / `ARTIFACT_*_BUCKET` | 空 | S3/R2 兼容对象存储的 endpoint、区域、凭证、隔离桶与发布桶 |
| `ARTIFACT_MAX_UPLOAD_BYTES` / `...UNPACKED...` / `...MAX_FILES` | `32 MiB` / `128 MiB` / `2000` | 插件包流式上传与解压预检硬限制 |
| `ARTIFACT_SUBMISSION_RPM` | `6` | Artifact 提交按用户和 IP 的每分钟限流，`0` 表示关闭 |
| `EMAIL_PROVIDER` | `disabled` | 邮件通道：`disabled`、`smtp` 或 `cloudflare` |
| `EMAIL_REGISTRATION_ENABLED` | `false` | 是否开放普通用户邮箱注册 |
| `EMAIL_VERIFICATION_REQUIRED` | `true` | 邮箱注册与换绑是否需要邮件验证码校验 |
| `EMAIL_DAILY_LIMIT` / `EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER` | `0` / `5` | 全站每日邮件总额度（0 不限）与单邮箱每日验证码上限 |
| `SESSION_MAX_AGE_SECONDS` | `604800` | 登录会话有效期（默认 7 天） |
| `COOKIE_SECURE` / `COOKIE_SAME_SITE` | `false` / `Lax` | Cookie 安全属性 |
| `ENABLE_DEV_AUTH` | `false` | 开发调试登录头开关（`apps/api/.env.example` 为便于本地开发设为 `true`，生产必须 `false`） |
| `MARKET_API_KEYS` | 空 | 全局静态 API Key，格式 `name:key:scope1\|scope2`，逗号分隔多个 |

</details>

> [!CAUTION]
> 生产环境必须保持 `ENABLE_DEV_AUTH=false`（禁止通过 `X-Dev-GitHub-Login` 伪造登录），并在 HTTPS 反向代理后设置 `COOKIE_SECURE=true`。

<details>
<summary>直接通过环境变量启动完整持久化模式的最小示例</summary>

```env
DATABASE_URL=postgresql://market:market@127.0.0.1:5432/market
REDIS_URL=redis://127.0.0.1:6379/0
WEB_URL=https://your-market-domain
CORS_ORIGIN=https://your-market-domain
COOKIE_SECURE=true
ENABLE_DEV_AUTH=false
GITHUB_CALLBACK_URL=https://your-market-domain/v1/auth/github/callback
SITE_NAME=Astrhub 插件市场
SITE_ICON_URL=/logo.webp
SITE_SUBTITLE=全新社区插件市场
SITE_DESCRIPTION=发现、评价和提交 AstrBot 插件。
EMAIL_PROVIDER=disabled
```

</details>

### 前端环境变量

构建与开发变量见 [apps/market-web/.env.example](apps/market-web/.env.example)：

| 变量 | 说明 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端 API 基础地址。前后端分端口本地开发时配置，同源部署通常留空 |
| `VITE_COMMUNITY_REPO_URL` | 页脚展示的社区仓库链接（留空时自动读取当前 Git 仓库地址） |

## 身份与五级角色

| 身份层级 | 推导条件 | 能力范围 |
| --- | --- | --- |
| **核心管理员（`core_admin`）** | 首次向导或环境引导创建 | 管理系统设置、认证/验证开关、管理员队伍、本地用户、公告与任意插件 CDN |
| **普通管理员（`admin`）** | 核心管理员任命或命中 `GITHUB_ADMIN_ORG` | 审核上架/下架插件、处理包审查、删除评论、禁言用户、刷新任意插件元数据 |
| **开发者（`developer`）** | 绑定至少一个 OAuth 账号或名下存在上架/待审插件 | 包含注册用户全部能力；有有效 OAuth 绑定时可提交与上架插件、配置 CDN、上传制品及创建写入权限（`market:write`）个人 API Key |
| **注册用户（`user`）** | 通过邮箱注册或本地创建且无有效 OAuth 绑定与存量插件 | 浏览、评论、点赞、站内信通知、管理个人资料与只读（`market:read`）个人 API Key |
| **游客（`guest`）** | 未登录 | 浏览公开插件、详情、作者页、公告与 AstrBot 插件源 |

邮箱注册账号与 GitHub OAuth 身份解耦，OAuth 绑定记录保存在通用 `market_oauth_identities` 表中。OAuth 用户不会自动成为核心管理员；受信任组织成员（`GITHUB_ADMIN_ORG`）登录后可自动提权为 `admin`。权限实现位于 [apps/api/app/auth.py](apps/api/app/auth.py) 与 [apps/api/app/oauth_identities.py](apps/api/app/oauth_identities.py)，完整安全模型见 [docs/security.md](docs/security.md)。

## AstrBot 集成

在 AstrBot WebUI 中将市场地址添加为自定义插件源：

```text
https://your-market-domain/plugins.json
```

接口完全兼容 AstrBot 自定义仓库格式：`repo` 字段始终保留归一化后的公开 HTTPS 地址（`gitcode.com` 自动归一化为 `atomgit.com`）；`version` 随仓库元数据同步；仅当最新已发布 artifact 的规范化版本与当前仓库版本一致时才输出 CDN `download_url`。仓库发布尚未过审的新版本或关闭 CDN 时，`download_url` 为空，用户仍可通过原仓库直连安装（非 GitHub 仓库直连安装需 AstrBot `v4.27.0` 及以上版本）。已发布对象路径固定为 `/{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`。

## 开发规范与 Git Hooks

```bash
# 后端：Python 3.11+，Ruff 行宽 100
cd apps/api
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest

# 前端：统一通过 Vite+（vp）执行，不要直接调用 vite 或 vitest
cd apps/market-web
npm install
npm run dev       # vp dev
npm run build     # vp build
vp check          # 格式、Lint 与类型检查
vp test --run     # 前端单元测试
```

安装本地 Git hooks（前端在 `pre-commit` 自动运行 `vp staged`，后端在 `pre-push` 运行 Ruff 检查）：

```bash
ln -sf ../../.vite-hooks/pre-commit .git/hooks/pre-commit
uv sync --project apps/api
uv run --project apps/api --directory apps/api pre-commit install --hook-type pre-push
```

- 插件 ID 遵循 `astrbot_plugin_<name>` 命名约定，所有 API 路由位于 `/v1/*`，管理员与核心管理员操作使用显式路径。
- 后端测试位于 `apps/api/tests/`，基于 `pytest` + FastAPI `TestClient`，无需真实 PostgreSQL/Redis 即可运行主测试套件。
- 前端测试采用 `*.test.ts` 就近放置，通过 `vp test --run` 执行（`npm test` 为等价别名）。

## 相关文档与安全反馈

- [安全策略与漏洞上报（SECURITY.md）](SECURITY.md)：受支持版本、私有安全公告通道与安全联系邮箱
- [系统架构（docs/architecture.md）](docs/architecture.md)：模块划分、数据模型与端点清单
- [安全模型（docs/security.md）](docs/security.md)：认证、会话、权限与存储边界
- [运行时探针设计（docs/runtime-runner.md）](docs/runtime-runner.md)：容器隔离与受限网络代理
- [插件审查运维手册（docs/plugin-review-operations.md）](docs/plugin-review-operations.md)：生产部署、健康度门控与事故处置

## 许可

Copyright (C) 2026 [Astrhub](https://github.com/Astrhub)

本项目基于 [GNU Affero General Public License v3.0（AGPL-3.0-only）](LICENSE) 开源。
