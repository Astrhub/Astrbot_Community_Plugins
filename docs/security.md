# 安全模型与部署安全

本文档说明 AstrBot Community Plugins 的身份认证、会话管理、权限控制、凭证保护与部署隔离边界。系统整体架构与权限矩阵见 [architecture.md](architecture.md#认证与权限模型)，漏洞上报流程见仓库根目录的 [SECURITY.md](../SECURITY.md)。

## 身份认证

首次启动时通过初始化向导或环境变量创建核心管理员账号（用户名 + 密码），用于管理站点策略、认证开关、OAuth 与管理员队伍。普通社区成员可通过邮箱注册（受 `EMAIL_REGISTRATION_ENABLED` 与 `EMAIL_VERIFICATION_REQUIRED` 控制）或通过 GitHub OAuth 注册/登录；账号主体与 OAuth 身份解耦，OAuth 绑定关系存储在通用 `market_oauth_identities` 表中。OAuth 用户**不会**自动成为核心管理员，只能由核心管理员显式提升，或在命中 `GITHUB_ADMIN_ORG` 受信任组织配置时自动获得普通管理员权限。

### 登录与注册方式

| 方式 | 端点 | 启用条件 |
| --- | --- | --- |
| 用户名或邮箱密码登录 | `POST /v1/auth/internal/login` | `PUBLIC_LOGIN_ENABLED=true`（关闭时仅核心管理员可密码登录） |
| 邮箱注册（免验证直通） | `POST /v1/auth/email/register` | `EMAIL_REGISTRATION_ENABLED=true` 且 `EMAIL_VERIFICATION_REQUIRED=false` |
| 邮箱注册（验证码两步） | `POST /v1/auth/email/register/request` → `verify` | `EMAIL_REGISTRATION_ENABLED=true`、`EMAIL_VERIFICATION_REQUIRED=true` 且 `EMAIL_PROVIDER != disabled` |
| 找回 / 重置密码 | `POST /v1/auth/email/password-reset/request` → `verify` | `EMAIL_PROVIDER != disabled` |
| GitHub OAuth | `GET /v1/auth/github/login` → `/callback` | `GITHUB_LOGIN_ENABLED=true` |
| 开发调试 | `GET /v1/auth/debug-login` | 仅限 `ENABLE_DEV_AUTH=true`（生产环境必须为 `false`） |

公开用户名与另一账号的内部登录名发生冲突时，密码登录会返回 `409 login_identifier_ambiguous`，不会按查询顺序选择账号。迁移与本地种子数据回填会避开这类命名冲突。

> [!CAUTION]
> 开发调试登录允许通过 `X-Dev-GitHub-Login` 请求头直接伪造用户身份，生产环境必须保持 `ENABLE_DEV_AUTH=false`。

### OAuth 绑定、解绑与账号合并

- **非破坏性绑定**：已登录的邮箱用户或本地管理员绑定 GitHub OAuth 时，系统将身份写入 `market_oauth_identities` 并同步兼容字段，不会覆盖原有 `auth_source`、`public_username` 以及已自定义的头像和显示名。若该 GitHub 账号此前仅以无密码、无邮箱的普通 OAuth 身份登录过，且当前操作者为管理员，则将其名下记录合并到当前管理员账号。
- **防锁死解绑**：用户解绑 `market_oauth_identities` 中的身份时，必须已设置本地密码或至少保留另一条有效 OAuth 绑定，禁止解绑唯一登录方式。
- **存量插件与权限回落**：解绑 OAuth 不会删除或自动下架已上架/审核中的插件或已发布 CDN 包；作者仍可直接自下架插件或关闭 CDN，但在重新绑定 OAuth 前不能提交新插件、重新申请上架、修改仓库地址或开启 CDN。
- **按身份清理**：解绑多个 GitHub 身份之一时保留其他身份的兼容字段和仓库授权，仅删除被解绑身份对应的会话凭据；最后一个 GitHub 身份解绑后，旧凭据不能继续访问仓库。

## 会话与 Cookie

系统采用服务端有状态 Cookie 会话（`astrbot_market_session`，非 JWT），支持随时在服务端吊销：

- **Token 格式**：`sess_{token_urlsafe(24)}`（生成逻辑位于 `apps/api/app/store.py`）。
- **存储方式**：完整模式下存入 Redis（键格式为 `astrbot_market:session:{token}`，值为 `{token, user_id, session_version, created_at, last_seen_at}`）；本地单进程模式（`MARKET_STORAGE_MODE=local`）存入本地持久化 JSON 文件并按 TTL 清理。
- **会话版本吊销**：`market_users.session_version` 在密码重置时递增并同步清理该用户的所有已有会话；旧会话读取时若 `session_version` 不匹配会立即失效。重置码绑定申请时的邮箱，换绑邮箱后旧重置码不能修改密码或验证新邮箱。
- **过期与续期**：有效期由 `SESSION_MAX_AGE_SECONDS` 控制（默认 7 天）；每次 `get_user_by_session()` 校验会话时自动刷新活跃时间。
- **注销**：`POST /v1/auth/logout` 同时清除浏览器 Cookie、服务端会话及关联的短期 GitHub 仓库授权凭据。

Cookie 安全属性如下：

| 属性 | 代码默认值 | 生产环境要求 |
| --- | --- | --- |
| `HttpOnly` | `true` | 禁止前端脚本读取会话 Cookie |
| `SameSite` | `Lax` | 抑制常规跨站 CSRF 请求 |
| `Secure` | `false`（由 `COOKIE_SECURE` 控制） | HTTPS 部署必须设为 `true` |

### OAuth State 防 CSRF

发起 GitHub OAuth 登录时，服务端通过 `uuid4()` 生成随机 `state` 并写入有效期 10 分钟的独立 Cookie（`astrbot_market_oauth_state`）；回调阶段严格比对请求参数与 Cookie 中的 `state`，不一致即拒绝登录。

## 五级权限模型

数据库 `market_users.role` 仅保留管理层级（`user` / `admin` / `core_admin`），对外响应与 OpenAPI / `/llms.txt` 统一按五级身份分层实时推导：

| 分层 | 推导条件 | 能力范围 |
| --- | --- | --- |
| 游客（`guest` / `public`） | 未登录 | 浏览插件、详情、作者页、公告与 AstrBot 插件源 |
| 注册用户（`user`） | 已登录、`role = user`、无有效 OAuth 绑定且无上架/待审插件 | 评论、点赞、回复、站内信、个人资料与只读（`market:read`）个人 API Key；绑定 OAuth 后升级为开发者 |
| 开发者（`developer`） | `role = user` 且存在至少一条有效 OAuth 绑定，或名下仍有上架/待审插件 | 包含注册用户全部能力；有有效 OAuth 绑定时可提交与上架插件、管理仓库/CDN、上传制品并创建写入（`market:write`）个人 API Key |
| 管理员（`admin`） | `role = admin` | 审核上架/下架插件、处理包审查、删除评论、禁言用户、刷新任意插件元数据、颁发全局 API Key（`/v1/admin/*`） |
| 核心管理员（`core_admin`） | `role = core_admin` | 管理系统设置、认证/验证开关、邮件、审查策略、管理员队伍、本地用户与全站公告（`/v1/core/*`、`/v1/admin/settings`）；可管理任意插件 CDN |

权限检查函数集中在 `apps/api/app/auth.py` 与 `apps/api/app/oauth_identities.py`（包括 `resolve_account_tier()`、`can_issue_write_api_key()`、`can_edit_plugin()`、`can_configure_plugin_cdn()` 与 `can_manage_plugin_submission()`）。所有敏感接口均在后端路由层强制鉴权，个人 `market:write` API Key 在签发及写权限校验时还会实时重检持有者的五级身份及有效 OAuth 绑定（管理员除外）；前端界面上的角色显隐仅用于交互展示，不作为安全边界。

## API Key

API Key 面向自动化工具与客户端接入，通过 `Authorization: Bearer <key>` 鉴权，分为三种来源：

| 来源 | 配置或端点 | 权限来源 | 特征 |
| --- | --- | --- | --- |
| 全局静态 Key | `MARKET_API_KEYS` 环境变量 | 按配置指定 `scopes` | 格式 `name:key:scope1\|scope2`，逗号分隔多个 |
| 管理员颁发 Key | `POST /v1/api-keys` | `admin` 及以上 | `sk-ah-` 前缀 |
| 个人 Key | `/v1/me/api-keys` | 登录用户自建自删 | `sk-ah-` 前缀 |

主要安全属性：

- **作用域隔离**：支持 `market:read` 与 `market:write` 权限位，接口按需校验。当前个人 Key 仅接入只读接口，会话写接口不接受个人 Key 代替登录。
- **写权限 OAuth 约束**：普通开发者必须有有效 OAuth 绑定才能创建或使用 `market:write` 个人 Key；仅因历史插件保留开发者层级不会绕过该检查，管理员按角色授权。
- **明文仅展示一次**：仅在创建响应中返回完整 Key，后续查询只返回名称、作用域和创建时间。
- **即时撤销**：删除后立即失效，数据库 `key` 列具备唯一约束。

> [!NOTE]
> 服务端当前未单独记录 API Key 级别的操作审计日志。如需追溯机器客户端的详细调用记录，请在反向代理或 API 网关层启用访问日志。

## 凭证与密钥安全

### 密码哈希

内部账号密码采用 **PBKDF2-SHA256（260,000 次迭代）**存储（`apps/api/app/auth.py`），校验阶段通过 `secrets.compare_digest` 进行常量时间比对以抵御时序侧信道攻击。通过环境变量 `CORE_ADMIN_PASSWORD` 引导初始化时，明文密码仅在启动阶段用于计算哈希，不会写入 `.env` 或数据文件。GitHub OAuth 用户不保存本地密码。

### 敏感字段脱敏保留

系统设置中的 GitHub Client Secret、SMTP 密码、Cloudflare Email API Token 以及用户的 `github_token` 均不会通过任何接口明文回显，返回时统一遮蔽为 `********`。前端提交设置更新时，若对应字段保持遮蔽值，后端会保留原有密钥而不写入遮蔽字符串，避免保存表单时误清空密钥。

### GitHub API Token 轮转

`GITHUB_API_TOKEN` 支持使用逗号、分号或换行配置多个 Token，后端通过 `app.state.github_api_token_index` 轮转调用 GitHub API 以缓解速率限制。遇到上游限流时显式向调用方报告错误，不静默吞错。核心管理员可在 `/admin/settings` 中测试并轮换站点 Token。

### 存储边界

- **PostgreSQL**：完整模式下持久化业务数据、artifact 状态机、结构化审查发现、人工决策、租约任务与通知 outbox。
- **Redis**：存储登录会话 Token（带 TTL），以及用于仓库权限校验的加密短期 GitHub OAuth 授权凭据（密钥由 `GITHUB_CLIENT_SECRET` 经 HKDF 派生并用 Fernet 加密，TTL 不超过 8 小时，注销或会话失效时删除）。不保存明文 OAuth 令牌或长期业务数据。
- **本地单文件存储**：仅在自用单进程模式（`MARKET_STORAGE_MODE=local`）启用，通过原子替换持久化市场状态与会话，并默认关闭 artifact 审查管线。
- **隔离存储**：上传与抓取的插件 ZIP 以及审查提取的文本对象存放在私有本地目录或私有对象存储桶中，不生成公开 URL。
- **发布存储**：仅接收已过审 artifact 的原始隔离 ZIP，采用条件写入禁止覆盖；公开下载链接统一由独立的 CDN 域名拼接生成。

### Artifact 信任边界

- API 进程只做流式接收、鉴权和任务入队，不解压 ZIP、不导入插件模块，也不安装依赖。
- ZIP 与 GitHub 来源的 artifact 提交按登录用户和直接来源 IP 做每分钟限流；Redis 不可用时退化为单进程内存计数。
- `artifact-worker` 在独立进程或容器中执行预检、YAML `safe_load`、Python AST 静态规则、依赖漏洞匹配及可选的 ClamAV/YARA/LLM 检查，整个过程不执行插件代码。
- 预检会拒绝路径穿越、绝对路径、符号链接、加密条目、Zip Bomb、超限文件数或体积、重复路径、缺失或重复 metadata、非法版本或插件名、Git LFS、submodule 以及原生可执行制品。
- GitHub 来源仅允许公开 `https://github.com/<owner>/<repo>` 仓库，优先抓取正式 Release ZIP 资产，分支与标签均先固定为 40 位 commit SHA 后再从固定 `codeload` 主机下载。
- 运行时烟雾测试通过独立 `runtime-runner` 和一次性隔离容器执行，不复用 API 或 `artifact-worker` 进程。
- LLM 分析结果仅作为辅助建议展示，不能直接背书或绕过人工决策。

已发布对象路径固定为 `/{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`，其中 `suffix` 首次生成后持久化。目标路径已存在时，仅在 SHA-256 完全一致时视为幂等成功，否则发布失败。

## 输入验证与内容安全

所有外部输入（插件元数据、README、仓库文件预览、评论）均视为不可信数据：

- **插件名校验**：必须符合 `astrbot_plugin_*` 命名格式。
- **仓库地址校验**：必须为 `https://github.com/<owner>/<repo>` 格式；社区模式下提交者必须证明 GitHub 仓库管理权限，本地单进程模式下仅管理员在不启用 CDN 时可直填公开仓库地址。
- **分类与标签限制**：插件分类必须在官方白名单内，标签数量受 `MAX_PLUGIN_TAGS`（默认 8）限制。
- **Markdown 与预览清洗**：前端先通过 `marked` 渲染 Markdown，再由 `DOMPurify` 清洗输出，并在写入 `v-html` 前再次执行最终 `DOMPurify` 门禁；图片预览强制校验 `http(s)://` 协议。
- **评论软删除**：删除根评论会同时隐藏其下回复（通过 `deleted` 标记与部分索引 `WHERE deleted = false` 实现），保留服务端审计记录。
- **审查内容最小披露**：源码、diff、扫描证据和运行日志仅通过所有者或管理员鉴权接口按需读取。状态通知邮件使用固定模板，批准与发布邮件仅附带经过长度限制、去除代码块并脱敏敏感字段的管理员评价，不透出原始源码、行评论、内部路径、对象 key 或扫描证据。

## 网络与部署安全

- **CORS**：由 `CORS_ORIGIN` 严格控制允许的前端来源（逗号分隔）；生产同源部署时应限定为站点自身域名。
- **HTTPS 与 Cookie**：生产环境应在前置 Nginx/Caddy 启用 TLS，并将 `COOKIE_SECURE` 设为 `true`。
- **systemd 加固**：`deploy/systemd/` 中的服务单元启用了三用户分离、独立 env 文件、`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=strict` 与受限 `ReadWritePaths`。API 无法访问运行时结果目录；Runner 对 artifact 目录仅只读，并通过 `ProtectHome=read-only` 保持 rootless 容器引擎 socket 可连接。
- **Worker 分离**：Docker 通过 `artifacts` profile 启动独立 Worker，systemd 使用独立 unit，API 与 Worker 仅通过 PostgreSQL 租约协调，不在 Redis 保存审查状态。
- **Runtime Runner 分离**：生产 runner 使用独立 rootless engine 和最小权限数据库角色；Compose 默认使用
  rootless 用户 socket 且 rootful 开关关闭。显式 root socket 只用于本地降级验证，不能报告为生产级隔离。详见 [runtime-runner.md](runtime-runner.md)。
- **故障显性暴露**：配置存在并不代表工具就绪。一旦 Worker/Runner 心跳缺失或过期、ClamAV/YARA/漏洞库数据过期、LLM 输出格式无效或运行时隔离证明不满足要求，系统必须标记为降级或阻断，不得显示为通过或参与自动审批。
- **安全结论边界**：自动扫描、LLM 建议与人工复核都无法保证绝对安全。出现重大风险时，需按 [plugin-review-operations.md](plugin-review-operations.md) 执行稳定版本关联核查、紧急下架、凭据轮换与重新审查。
- **开发认证与回调校验**：Docker 生产模板默认关闭 `ENABLE_DEV_AUTH`。启用 GitHub 登录时，后端会拒绝本地回环地址作为 `GITHUB_CALLBACK_URL` 或 `WEB_URL`，防止开发配置误用于公网。

## 速率限制与配额

- **GitHub API**：通过多 Token 轮转分担额度，触达速率限制时显式返回状态。
- **邮件与验证码限额**：全站每日邮件总额度 `EMAIL_DAILY_LIMIT` 与单邮箱每日验证码上限 `EMAIL_VERIFICATION_DAILY_LIMIT_PER_USER` 均写入共享存储（完整模式存 Redis，本地模式存本地存储），与通知邮件共用日限额。发送前通过 Redis Lua 原子预留，发送失败或无需发送的早退会释放预留；验证码采用随机盐 HMAC-SHA256 摘要存储、10 分钟有效、最多尝试 5 次、60 秒重发冷却，并通过原子占位防止并发重放。免验证直通注册与验证码申请同时受来源 IP 窗口限流约束。
- **上游错误脱敏**：Cloudflare Email 发送失败时，仅向管理员返回错误摘要，不暴露完整上游响应体。

## 首次初始化与配置优先级

### `/setup` 初始化安全

- **完整模式**：缺少 PostgreSQL 或 Redis 配置时，前端打开 `/setup` 并调用 `POST /v1/setup`。后端会先完成建库、表结构迁移、Redis 连通性验证与核心管理员创建；**任一步骤失败都不会写入 `apps/api/.env`**，避免残留半成品配置。全部成功后写回 `.env` 并在进程内热切换存储。
- **本地单进程模式**：缺少核心管理员时，`/setup` 仅收集站点名称、图标与核心管理员账号密码，保存后写入本地持久化文件与 env 文件并立即启用。
- 初始化完成后，`/v1/setup` 与 `/v1/setup/status` 自动关闭（返回 404）。

### 配置覆盖优先级

配置按以下顺序从低到高生效：

1. `apps/api/.env`（或 `APP_ENV_FILE` 指定的环境文件）；
2. 同名系统环境变量（覆盖 `.env`）；
3. Web 后台写入持久化配置表（`market_options` / 本地 `options`）的运行时设置（覆盖 `.env` 与系统环境变量中的同名业务配置）。

基础设施连接参数（`DATABASE_URL`、`REDIS_URL`、`MARKET_STORAGE_MODE`、`LOCAL_DATA_PATH`）仅从环境层读取，不通过 Web 后台暴露或修改。

## 安全测试覆盖

`apps/api/tests/` 中的安全测试（涵盖 `test_market.py`、`test_github_authorization.py`、`test_local_market_store.py`、`test_runtime_compose.py`、`test_manual_publication_authorization.py` 等）基于内存或临时本地存储与 `TestClient` 覆盖以下关键边界：

- 角色越权防护（GitHub 用户不自动成为 `core_admin`；普通 `admin` 无法管理管理员队伍或系统设置）。
- `PUBLIC_LOGIN_ENABLED=false` 时仅核心管理员允许内部登录。
- 敏感凭据脱敏与回传遮蔽值时的密钥保留机制。
- API Key 明文单次返回、`sk-ah-` 前缀与 `scopes` 权限校验。
- `/v1/setup` 失败不写 `.env`、初始化完成后自动关闭以及进程内存储热切换。
- GitHub OAuth `state` 校验、本地回环回调拒绝、短期加密授权凭据吊销与发布前仓库身份复核。
- 插件命名空间冲突、评论软删除、点赞唯一约束、分类白名单与标签数量上限。
- Docker Compose 与 systemd 部署模板的最小权限边界和 fail-closed 默认值。
