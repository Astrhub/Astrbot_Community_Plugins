# 安全

本文档描述 AstrBot Community Plugins 的身份认证、权限、凭证与部署安全模型。权限矩阵与角色定义见 [architecture.md](architecture.md#认证与权限模型)。

## 身份认证

首次启动向导注册内部核心管理员账号（用户名 + 密码），用于配置 GitHub OAuth、管理员权限与站点策略。GitHub OAuth 是普通用户与插件所有者的身份来源；GitHub 用户**不会**自动成为核心管理员，仅能通过核心管理员显式提升或 `GITHUB_ADMIN_ORG` 受信任组织规则获得管理员权限。

### 登录方式

| 方式 | 端点 | 触发条件 |
|---|---|---|
| 内部账号 | `POST /v1/auth/internal/login` | 用户名 + 密码（核心管理员及内部用户） |
| GitHub OAuth | `GET /v1/auth/github/login` → `/callback` | `GITHUB_LOGIN_ENABLED=true` |
| 开发调试 | `GET /v1/auth/debug-login` | **仅** `ENABLE_DEV_AUTH=true`（生产必须 `false`） |

OAuth 登录后，若用户属于 `GITHUB_ADMIN_ORG` 且当前非 admin，自动提权为 admin（`main.py`）。开发调试登录通过 `X-Dev-GitHub-Login` 请求头自动创建用户，**绝不可在生产开启**。

### GitHub 身份绑定与合并

已登录的内部管理员可在个人设置绑定 GitHub。若该 GitHub 账号此前仅作为普通 GitHub 用户登录过，系统会将其名下的插件、评论与提交记录合并到当前管理员账号，避免身份分裂。`PUBLIC_LOGIN_ENABLED=false` 时，仅核心管理员可内部登录，GitHub 登录仍按其开关独立控制。

## 会话与 Cookie

认证基于 cookie session（`astrbot_market_session`），**非 JWT**——服务端可即时撤销。

- **token 格式**：`sess_{token_urlsafe(24)}`（`store.py`）。
- **存储**：Redis，key 为 `astrbot_market:session:{token}`，value 为 `{token, user_id, created_at, last_seen_at}`。
- **过期**：TTL = `SESSION_MAX_AGE_SECONDS`（默认 7 天）；每次 `get_user_by_session()` 读取时刷新 TTL，活跃用户不会过期。
- **注销**：`POST /v1/auth/logout` 同时清除客户端 cookie 与 Redis session。

Cookie 属性（`main.py`）：

| 属性 | 默认 | 生产建议 |
|---|---|---|
| `HttpOnly` | `true` | 防止 JS 读取 session |
| `SameSite` | `Lax` | 抑制跨站 CSRF |
| `Secure` | `false`（`COOKIE_SECURE`） | HTTPS 部署必须设 `true` |

### OAuth state 防 CSRF

GitHub OAuth 使用 `uuid4()` 生成 state，存入独立 cookie（`astrbot_market_oauth_state`，有效期 10 分钟），回调时校验一致性，不匹配则拒绝（`main.py`）。

## 权限模型

| 角色 | 能力范围 |
|---|---|
| 核心管理员（core_admin） | 系统设置、邮件、管理员队伍、内部用户、公告（`/v1/core/*`、`/v1/admin/settings`） |
| 普通管理员（admin） | 审核上架/下架、删除评论、禁言用户、刷新任意插件、颁发全局 Key（`/v1/admin/*`） |
| 插件所有者 | 经仓库所有权验证后编辑自有插件元数据、申请/自主下架、手动刷新 |
| 普通用户（user） | 浏览、提交、评论、点赞、管理个人 Key 与通知偏好 |

权限函数位于 `apps/api/app/auth.py`：`can_edit_plugin(user, plugin)`（admin 或所有者匹配 `owner_user_id` / `owner_github_login`）、`can_manage_plugin_submission(user, plugin)`。所有敏感端点在路由层强制角色，前端组件内的角色判定仅为 UX，不构成安全边界。

## API Key

API Key 用于机器客户端（如未来的 AstrBot WebUI 插件），通过 `Authorization: Bearer <key>` 鉴权。共有三种来源：

| 来源 | 端点 / 配置 | 权限 | 特征 |
|---|---|---|---|
| 全局静态 | `MARKET_API_KEYS` 环境变量 | 按配置 | 格式 `name:key:scope1\|scope2`，逗号分隔多个 |
| 管理员颁发 | `POST /v1/api-keys` | admin | `sk-ah-` 前缀 |
| 个人 Key | `/v1/me/api-keys` | 登录用户（自建自删） | `sk-ah-` 前缀 |

已实现的属性：

- **作用域（scopes）**：`market:read` / `market:write`，鉴权时按 scope 校验（如列出全局 Key 需 `market:read`）。
- **原文仅返回一次**：创建响应中返回明文 Key，后续列表只返回名称、scopes 与创建时间。
- **可撤销**：删除（DELETE）即立即失效，数据库 `key` 列有唯一约束。
- **脱敏**：管理接口不回显明文 Key。

> 当前未实现 Key 级操作审计日志。如需机器客户端行为追溯，建议在反向代理或网关层记录。

## 凭证与密钥安全

### 密码哈希

内部账号密码使用 **PBKDF2-SHA256，260,000 次迭代**（`auth.py`）。`verify_password` 使用 `secrets.compare_digest` 进行常量时间比较，抵御时序攻击。GitHub 用户无本地密码。

### 敏感字段脱敏保留

系统设置中的 GitHub Client Secret、SMTP 密码、Cloudflare Email API Token、用户的 `github_token` 等**不会通过管理或用户接口明文返回**，统一以 `********` 遮蔽。关键机制：前端回传遮蔽值时，后端**保留已有密钥**而非写入遮蔽文本，避免误清空（测试 `test_core_admin_can_update_system_settings_and_preserve_masked_secrets` 覆盖）。

### GitHub token 轮转

`GITHUB_API_TOKEN` 可配置多个 token（`,` / `;` / 换行分隔），通过 `app.state.github_api_token_index` 轮转调用 GitHub API，缓解单 token 速率限制。刷新遇 GitHub rate limit 时会向上游报告，不静默失败。系统级 token 也可经 `/v1/admin/settings` 轮换。

### 存储边界

- **PostgreSQL**：持久化业务数据、artifact 状态机、结构化 findings、决策、lease 任务与通知 outbox。
- **Redis**：**仅**存登录会话 token（带 TTL）。不存储 GitHub 密钥、OAuth 原始令牌等超出登录流程所需的敏感数据。
- **隔离存储**：上传/GitHub ZIP 与审查文本对象使用私有本地目录或私有 bucket，不生成公开 URL。
- **发布存储**：只接收已批准 artifact 的原始隔离 ZIP，使用条件创建禁止覆盖；公开 URL 由独立 CDN base URL 组成。

### Artifact 信任边界

- API 只做流式接收、鉴权和任务入队，不解压 ZIP、不 import 插件、不安装 requirements。
- ZIP/GitHub artifact 提交同时按登录用户和直接来源 IP 做每分钟限流；生产 Redis 不可用时退化为单进程内存计数。
- `artifact-worker` 在独立进程/容器中执行 P0/P1 的 ZIP 目录检查、YAML safe load、Python AST 与 requirements 静态规则；这些步骤仍不执行插件代码。
- 预检拒绝路径穿越、绝对路径、符号链接、加密 entry、zip bomb、超限文件/数量、重复路径、缺失或重复 metadata、非法版本/插件名、Git LFS、submodule 与原生可执行制品。
- GitHub 来源只允许公开 `https://github.com/<owner>/<repo>`，分支/标签必须先解析为 40 位 commit SHA，再从固定 codeload 主机下载。
- 后续 runtime smoke test 必须使用一次性隔离容器；不得复用 API 进程或当前 P0/P1 worker 进程。
- LLM 结果只能产生结构化建议，不能成为最终安全背书或绕过人工决策。

发布路径为 `/{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`；`suffix` 创建一次并持久化。对象已存在时只有 SHA-256 相同才视为幂等，否则发布失败。

## 输入验证与内容安全

所有用户输入（插件元数据、README 内容、评论）均视为不可信：

- **插件名校验**：必须匹配 `astrbot_plugin_*` 模式。
- **仓库 URL 校验**：必须为 `https://github.com/<owner>/<repo>` 格式；提交者需证明 GitHub 仓库所有权。
- **分类白名单**：插件分类必须是预定义官方分类之一。
- **标签上限**：受 `MAX_PLUGIN_TAGS`（默认 8）限制。
- **Markdown 渲染**：前端使用 `DOMPurify` 清理渲染输出，`marked` + `highlight.js` 渲染；审核操作在服务端存储。
- **评论软删除**：删除根评论会隐藏其回复（`deleted` 标记 + partial index `WHERE deleted = false`），保留审计痕迹。
- **审查内容最小披露**：P1 API 不提供完整源码接口；仅返回有限 `evidence_excerpt`。状态邮件只包含状态、原因与站内链接，不包含源码或 finding 证据。

## 网络与部署安全

- **CORS**：`CORS_ORIGIN` 控制允许的前端来源（逗号分隔）；开发态默认允许 `http://127.0.0.1:3000`。生产同源部署时设为站点自身域名。
- **Cookie**：生产 HTTPS 必须设 `COOKIE_SECURE=true`，配合反向代理（Nginx/Caddy）启用 TLS。
- **systemd 加固**：`deploy/systemd/` 的 service 启用 `NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full`，并通过 `ReadWritePaths` 限定仅后端目录可写。
- **Worker 分离**：Docker 使用 `artifacts` profile 启动独立 Worker；systemd 使用单独 unit。两者通过 PostgreSQL lease 协调，不在 Redis 保存审查状态。
- **Runtime Runner 分离**：生产 runner 使用独立 rootless engine 和最小权限数据库角色；Compose `runtime-runner` profile 的 root socket 只用于本地降级验证，不能报告为生产级隔离。详见 [runtime-runner.md](runtime-runner.md)。
- **开发认证**：`ENABLE_DEV_AUTH` 生产必须为 `false`，否则任何人可通过 `X-Dev-GitHub-Login` 头伪造登录。Docker 模板（`.env.docker.example`）默认已设 `false`。
- **OAuth callback URL**：当 GitHub 登录启用时，系统拒绝本地回环 callback，防止开发态配置泄漏到生产（测试 `test_system_settings_reject_local_oauth_callback_when_enabled`）。callback URL 优先级：运行时数据库 options > `.env` > 初始设置。

## 速率限制与配额

- **GitHub API**：依赖 token 轮转缓解速率限制；元数据刷新显式报告 rate limit，不掩盖。
- **邮件配额**：`EMAIL_DAILY_LIMIT` 控制每日发送上限（内存计数器，按日清零）；`EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER` 限制单用户每日验证邮件。
- **Cloudflare Email**：发送失败时仅将 Cloudflare 错误摘要返回给管理员，不暴露完整上游响应。

## 首次启动与环境安全

若首次启动缺少 PostgreSQL 或 Redis 配置，前端 `/v1/setup` 分步收集必要字段。保存时后端：

1. 连接 PostgreSQL（目标库不存在则创建）→ 初始化 schema → 验证 Redis → 写入核心管理员；
2. **任一步失败都不写入 `apps/api/.env`**（测试 `test_setup_initialization_failure_does_not_write_env_file`），避免半成品配置污染；
3. 全部成功后写入 `.env`，进程内切换到 PgRedis 存储，**无需重启**；
4. 初始化完成后 `/v1/setup` 关闭（测试 `test_setup_after_first_run_is_closed`）。

### 环境变量优先级

1. 默认读取 `apps/api/.env`；
2. 同名**系统环境变量**覆盖 `.env`；
3. `APP_ENV_FILE` 可指向其他 env 文件（测试 / 特殊部署）；
4. Web 后台（`market_options` 表）保存的站点、OAuth、市场策略、邮件设置**覆盖** `.env` 同名系统设置（运行时层高于静态层）。

基础设施连接（`DATABASE_URL` / `REDIS_URL`）后续只通过 `.env` 修改，不在 Web 后台暴露。

## 安全测试覆盖

`apps/api/tests/test_market.py`（72 个测试，基于 `InMemoryMarketStore` + `TestClient`，无需真实 PG/Redis）覆盖的关键安全场景：

- 角色不可越级（GitHub 用户不自动成为 core_admin；admin 不能管理管理员）。
- `PUBLIC_LOGIN_ENABLED=false` 时仅核心管理员可登录。
- 敏感字段脱敏保留（系统设置、用户 `github_token`、管理员列表）。
- API Key 原文仅返回一次、scope 校验、`sk-ah-` 前缀。
- setup 失败不写 `.env`、初始化后 `/setup` 关闭、进程内切换存储。
- OAuth callback 优先级与本地 callback 拒绝。
- 评论软删除与点赞唯一约束。
- 插件分类白名单、标签上限、自动上架开关。
- GitHub 身份绑定合并、token 轮转与 rate limit 报告。
