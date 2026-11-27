# 安全模型与部署安全

本文档说明 AstrBot Community Plugins 的身份认证、会话管理、权限控制、凭证保护与部署隔离边界。系统整体架构与权限矩阵见 [architecture.md](architecture.md#认证与权限模型)，漏洞上报流程见仓库根目录的 [SECURITY.md](../SECURITY.md)。

## 身份认证

首次启动时通过初始化向导或环境变量创建核心管理员账号（用户名 + 密码），用于管理站点策略、认证开关、OAuth 与管理员队伍。普通社区成员可通过邮箱注册（受 `EMAIL_REGISTRATION_ENABLED` 与 `EMAIL_VERIFICATION_REQUIRED` 控制）或通过受支持的 OAuth 平台（GitHub、GitLab.com、Gitee、AtomGit / GitCode、Gitea）注册/登录；账号主体与 OAuth 身份解耦，OAuth 绑定关系存储在通用 `market_oauth_identities` 表中。OAuth 用户**不会**自动成为核心管理员，只能由核心管理员显式提升，或在命中 `GITHUB_ADMIN_ORG` 受信任组织配置时自动获得普通管理员权限。

### 登录与注册方式

| 方式 | 端点 | 启用条件 |
| --- | --- | --- |
| 用户名或邮箱密码登录 | `POST /v1/auth/internal/login` | `PUBLIC_LOGIN_ENABLED=true`（关闭时仅核心管理员可密码登录） |
| 邮箱注册（免验证直通） | `POST /v1/auth/email/register` | `EMAIL_REGISTRATION_ENABLED=true` 且 `EMAIL_VERIFICATION_REQUIRED=false` |
| 邮箱注册（验证码两步） | `POST /v1/auth/email/register/request` → `verify` | `EMAIL_REGISTRATION_ENABLED=true`、`EMAIL_VERIFICATION_REQUIRED=true` 且 `EMAIL_PROVIDER != disabled` |
| 找回 / 重置密码 | `POST /v1/auth/email/password-reset/request` → `verify` | `EMAIL_PROVIDER != disabled` |
| GitHub OAuth | `GET /v1/auth/github/login` → `/callback` | `GITHUB_LOGIN_ENABLED=true` 且配置 `OAUTH_CREDENTIAL_KEY` |
| 第三方已知平台 OAuth | `GET /v1/auth/oauth/{provider}/login` → `/callback` | 对应平台开关启用（`gitlab` / `gitee` / `atomgit` / `gitea`）且配置 `OAUTH_CREDENTIAL_KEY` |
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

系统采用服务端有状态 Cookie 会话（`astrbot_market_session`，非 JWT），支持按设备区分普通会话与受信任设备，并可随时在服务端单设备或全量吊销：

- **Token 格式与哈希存储**：浏览器 Cookie 持有随机令牌 `sess_{token_urlsafe(24)}`；服务端在 PostgreSQL、Redis 与本地 JSON 中均只保存 `SHA-256` 摘要（`token_hash`），不保存明文令牌，登录/注册 JSON 响应体也不回传明文令牌。
- **持久化与缓存分层**：完整模式下，活跃会话与设备元数据（`device_label`、`user_agent`、`ip_address`、`trusted`、`expires_at`）持久化在 PostgreSQL `market_user_sessions` 表中，Redis（键格式 `astrbot_market:session:h:{token_hash}`）仅作为最长 1 小时的热读缓存；Redis 重启或驱逐缓存时自动回源 PostgreSQL，1 年期信任设备不会丢失登录态。本地单进程模式（`MARKET_STORAGE_MODE=local`）保存在本地持久化 JSON 文件中并按 `expires_at` 清理。
- **固定有效期与信任设备**：未勾选“信任此设备”时会话有效期由 `SESSION_MAX_AGE_SECONDS` 控制（默认 7 天）；勾选“信任此设备”时由 `TRUSTED_SESSION_MAX_AGE_SECONDS` 控制（默认 365 天，上限钳制在 400 天以内以符合主流浏览器限制）。全部角色（含后台核心管理员）均可按需勾选。会话采用自登录起计算的固定绝对过期时间，活跃访问仅更新 `last_seen_at`，不滑动延后过期时间。已登录用户绑定新 OAuth 身份时保留原会话及其信任状态；修改本地密码时自动吊销其他全部设备，并为当前浏览器按原信任状态重签新会话。
- **会话版本与单设备管理**：`market_users.session_version` 在改密与找回密码时递增并同步吊销全部已有会话（同时清理 Redis 缓存）；用户可在个人设置页通过 `GET /v1/me/sessions`、`DELETE /v1/me/sessions/{session_id}` 与 `POST /v1/me/sessions/revoke-others` 查看活跃/受信任设备并单点或批量下线。重置码绑定申请时的邮箱，换绑邮箱后旧重置码不能修改密码或验证新邮箱。
- **注销**：`POST /v1/auth/logout` 同时清除浏览器 Cookie（携带一致的 `HttpOnly`、`Secure`、`SameSite` 属性）、当前服务端会话记录、Redis 缓存及关联的短期 GitHub 仓库授权凭据。

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

### 接口文档裁剪与公开字段白名单

- **OpenAPI 引用闭包裁剪**：`GET /openapi.json` 在按五级角色过滤 `paths` 与 `tags` 后，还会对 `components` 执行 `$ref` 可达性闭包裁剪，只保留当前角色可见路由实际引用的 schema 与组件。匿名、注册用户、开发者与普通管理员视图不会暴露 `SetupConfig`、数据库／Redis／SMTP／OAuth 配置与 `SystemSettingsPayload` 等系统配置模型；开发调试路由 `GET /v1/auth/debug-login` 通过 `include_in_schema=False` 排除在所有角色的 OpenAPI 文档之外。
- **公开插件响应白名单**：`GET /v1/plugins`、`GET /v1/authors/{username}/plugins`、`GET /v1/plugins/{plugin_id}`、`GET /v1/plugins/by-author/{username}/{name}` 及点赞／取消点赞响应统一通过 `public_market_plugin()` 白名单塑形，剥离 `owner_user_id`、`moderated_by`、`github_sync_*`、`unlist_*`、`current_artifact_id`、`repo_external_id`、`normalized_repo`、`cdn_generation`、`cdn_whitelist` 及内部 AI 分类元数据；详情接口以布尔字段 `can_manage` 替代向公开响应透传 `owner_user_id`。审核与同步等内部字段仅在需鉴权的 `/v1/me/plugins`、`/v1/admin/plugins` 等所有者／管理员接口返回。

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

### 敏感字段与独立主加密密钥

系统设置中的各平台 OAuth Client Secret（`GITHUB_CLIENT_SECRET`、`GITLAB_CLIENT_SECRET`、`GITEE_CLIENT_SECRET`、`ATOMGIT_CLIENT_SECRET`、`GITEA_CLIENT_SECRET`）与独立主加密密钥 `OAUTH_CREDENTIAL_KEY` 仅由部署环境变量注入，**不会**写入持久化配置表 `market_options`，也不通过管理接口明文回显。启用任何 OAuth 平台（含 GitHub）都必须配置至少 16 字符的 `OAUTH_CREDENTIAL_KEY`；缺失或过短时登录、回调与凭据写入直接返回 `503 oauth_credential_key_unconfigured`，不回退至 `GITHUB_CLIENT_SECRET`。SMTP 密码、Cloudflare Email API Token 以及用户的 `github_token` 在接口中统一遮蔽为 `********`，提交遮蔽值时保留原密钥。

### GitHub API Token 轮转

`GITHUB_API_TOKEN` 支持使用逗号、分号或换行配置多个 Token，后端通过 `app.state.github_api_token_index` 轮转调用 GitHub API 以缓解速率限制。遇到上游限流时显式向调用方报告错误，不静默吞错。核心管理员可在 `/admin/settings` 中测试并轮换站点 Token。

### 存储边界

- **PostgreSQL**：完整模式下持久化业务数据、OAuth 身份、由 `OAUTH_CREDENTIAL_KEY` 加密的 `market_oauth_credentials`（含刷新令牌与轮转状态）、仓库归属绑定 `market_git_repository_bindings`、artifact 状态机、结构化审查发现、人工决策、租约任务与通知 outbox。
- **Redis**：存储登录会话 Token（带 TTL），以及用于 GitHub 仓库权限校验的加密短期 OAuth 授权缓存（密钥由 `OAUTH_CREDENTIAL_KEY` 经 HKDF-SHA256 派生并用 Fernet 加密，TTL 不超过 8 小时，注销或会话失效时删除；缓存缺失时回退读取 PostgreSQL 中已绑定身份的 `market_oauth_credentials`，仅在解绑或 GitHub 返回 `401` 时撤销持久化凭据）。不保存明文 OAuth 令牌或长期业务数据。
- **本地单文件存储**：仅在自用单进程模式（`MARKET_STORAGE_MODE=local`）启用，通过原子替换持久化市场状态与会话，并默认关闭 artifact 审查管线。
- **隔离存储**：上传与抓取的插件 ZIP 以及审查提取的文本对象存放在绑定至 `artifact-quarantine` 用途的私有 Profile（本地目录、SQLite/PostgreSQL 分块表、私有 S3 桶或受控 WebDAV）中，不生成公开 URL；ImgBed 后端禁止绑定隔离区，且同源公开读取与 `/packages/*` 别名接口对隔离区对象一律返回 404。
- **发布存储与头像存储**：`artifact-published` 与 `avatar` 分别独立绑定存储 Profile，支持本地文件系统、SQLite（禁止放在仓库目录或指向市场状态 JSON）、PostgreSQL、S3/R2、WebDAV 与 CloudFlare ImgBed。所有新增 Profile 的敏感凭据通过 `OAUTH_CREDENTIAL_KEY` 结合独立 HKDF 域（`astrhub/storage-credential/{profile_id}/v1`）派生 Fernet 密钥加密保存于 `market_storage_secrets`，编辑留空时保留原凭据且从不回显。
- **公告图片**：仅核心管理员可上传，multipart 解析前完成鉴权、Origin 与 Content-Length 校验，并限制实际流式请求体；服务端按文件魔数校验 PNG、JPEG、GIF、WebP、AVIF，单张限制 5 MB，AVIF 必须具备兼容品牌。图片按内容 SHA-256 幂等写入 `avatar` 用途，失败时清理文件与记录，通过同源公开对象地址读取。`avatar` 用途必须显式绑定，不再自动继承插件发布 Profile；删除公告保留已上传图片，避免破坏其他引用。
- **用户头像上传**：`POST /v1/me/avatar` 在读取 multipart 之前完成鉴权、功能开关（`AVATAR_UPLOAD_ENABLED`）与每用户每小时 10 次的上传配额（三种 store 实现均为原子预留，失败也计次，防止探测解码器）；`Content-Length` 预检之外仍按实际读取字节数硬限 5 MiB。服务端用 Pillow 独立解码，只接受真实 PNG/JPEG/WebP（拒绝伪造 MIME、SVG、GIF），解码前把 `MAX_IMAGE_PIXELS` 收紧为 16,000,000 以拒绝解压炸弹，按 EXIF 方向矫正后丢弃全部 EXIF 元数据并重编码为 WebP（短边居中裁剪为 1:1，最长边不超过 512），因此客户端裁剪结果不被信任。写入 `avatar` 用途后回读校验大小、SHA-256 与 Content-Type，公开 URL 追加内容摘要用于缓存穿透；写入或数据库更新失败时删除刚写入的对象并将记录标记 `deleted`，用户记录保持旧头像。`DELETE /v1/me/avatar` 只清引用、不删远端历史对象；`PATCH /v1/me/profile` 拒绝直接修改 `avatar_url`，OAuth 头像仍由登录流程写入。
- **WebDAV 与 ImgBed 边界**：WebDAV 与 ImgBed 默认强制 HTTPS、禁止在 URL 中内嵌凭据、拒绝私网/回环地址与 DNS 重绑定（仅核心管理员显式启用 `allow_private_network` 时放行受信任内网），关闭自动重定向且仅通过请求头传递认证。WebDAV 绑定前自动探测创建、读取、HTTP `206` Range、覆盖保护（无条件写服务端自动切换内容哈希命名并重读校验 SHA-256）与删除能力；ImgBed 按 Profile 声明的 `scopes` 与 `upload_folder` 在服务层实施目录隔离，保留 `/file/...` 路径而不信任底层直连 `publicUrl`，验收探针使用随机唯一 key 直接上传，由 ImgBed 自动创建缺失的 `upload_folder`，再执行列表验收。可选 `expire` 支持 `forever` 或正整数加 `h`/`d`（最长 3650 天），留空使用图床默认并随上传与验收探针传递。滚动升级时须先让全部 API 实例支持该字段再编辑 Profile；有限有效期对象在迁移和退役前按远端 HEAD 结果对账，缺失对象标记 deleted，网络或权限错误仍保持原记录并阻断后续操作；绑定 `artifact-published` 前必须具备 `upload + list + delete` 权限并通过 ZIP 白名单、GET/HEAD/Range、缓存与撤回验收。
- **受控迁移与退役门控**：切换用途绑定后新写入立即进入目标 Profile，历史对象由后台迁移队列逐项校验复制并生成历史 URL 别名，并发替换会跳过旧版本，失败项保持源可读并支持仅重试失败项。源 Profile 只有在无活动绑定、无活动/待完成对象、无未完成迁移且无未对账的 `unknown_objects` 时才允许 `retire`（停止市场访问并立即撤销本地凭据，不自动删除远端文件）或 `DELETE`（保留审计记录）。 `/v1/admin/storage/*` 全部限制为核心管理员并返回 `private, no-store`，按角色从非核心管理员 `/openapi.json` 中剔除。

### Artifact 信任边界

- API 进程只做流式接收、鉴权和任务入队，不解压 ZIP、不导入插件模块，也不安装依赖。
- ZIP 与 GitHub 来源的 artifact 提交按登录用户和直接来源 IP 做每分钟限流；Redis 不可用时退化为单进程内存计数。
- `artifact-worker` 在独立进程或容器中执行预检、YAML `safe_load`、Python AST 静态规则、依赖漏洞匹配及可选的 ClamAV/YARA/LLM 检查，整个过程不执行插件代码。
- 预检会拒绝路径穿越、绝对路径、符号链接、加密条目、Zip Bomb、超限文件数或体积、重复路径、缺失或重复 metadata、非法版本或插件名、Git LFS、submodule 以及原生可执行制品。
- 仓库来源仅允许严格白名单内的公开 HTTPS 地址：`github.com`、`gitlab.com`（支持多层子组，深度上限 20）、`gitee.com`、`atomgit.com`（兼容 `gitcode.com` 并归一化为 `atomgit.com`）与 `gitea.com`，拒绝非 HTTPS、内嵌凭据、自定义端口、查询串、片段、路径穿越及任意未知主机。GitHub 优先抓取正式 Release ZIP 资产，其余分支/标签与第三方平台均先解析固定 40 位 commit SHA 后再通过官方归档 HTTPS 端点流式拉取（记录为 `source_type='git'`），重定向仅允许白名单主机且跨主机自动剥离 `Authorization` 请求头。
- 运行时烟雾测试通过独立 `runtime-runner` 和一次性隔离容器执行，不复用 API 或 `artifact-worker` 进程。
- **管理员 CDN 白名单直发**：`cdn_whitelist` 仅面向 `admin` / `core_admin` 角色的本人名下插件开放（非管理员请求在绑定前直接返回 `403 cdn_whitelist_admin_only`，对非管理员所有者的插件开启返回 `403 cdn_whitelist_admin_owner_required`）。开启后仍须完整通过 `PrecheckStage` 压缩包安全与 `metadata.yaml` 校验，随后由工作进程实时复核所有者与提交者仍具备管理员角色，写入可审计的 `review_decisions`（`action="auto_approve"`、`source="admin"`、`metadata={"cdn_whitelist": true}`）再进入 `publish`；一旦管理员被降权，自动回退至常规静态与安全审查管线。
- LLM 分析结果仅作为辅助建议展示，不能直接背书或绕过人工决策。

已发布对象路径固定为 `/{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`，其中 `suffix` 首次生成后持久化。目标路径已存在时，仅在 SHA-256 完全一致时视为幂等成功，否则发布失败。

## 输入验证与内容安全

所有外部输入（插件元数据、README、仓库文件预览、评论）均视为不可信数据：

- **插件名校验**：必须符合 `astrbot_plugin_*` 命名格式。
- **仓库地址校验**：必须为受支持 OAuth Git 平台（`github.com`、`gitlab.com`、`gitee.com`、`atomgit.com` / `gitcode.com`、`gitea.com`）的公开 HTTPS 仓库地址；社区模式下提交者必须通过对应平台 OAuth 证明仓库维护权限并写入 `market_git_repository_bindings`，本地单进程模式下仅管理员在不启用 CDN 时可直填白名单内的公开仓库地址。
- **分类与标签限制**：插件分类必须在官方白名单内，标签数量受 `MAX_PLUGIN_TAGS`（默认 8）限制。
- **Markdown 与预览清洗**：前端先通过 `marked` 渲染 Markdown，再由 `DOMPurify` 清洗输出，并在写入 `v-html` 前再次执行最终 `DOMPurify` 门禁；图片预览强制校验 `http(s)://` 协议。
- **海外媒体与头像受限代理**：服务端仅对已登记的海外平台（`GitLab.com`、`Gitea`）插件 Logo（`/v1/plugins/{id}/logo`）、README 仓库内图片（`/v1/plugins/{id}/readme-asset`）与白名单海外头像域名（`/v1/authors/{username}/avatar`、`/v1/me/avatar`）提供代理；国内平台（Gitee / AtomGit）保持浏览器直连并设置 `referrerpolicy="no-referrer"`，GitHub 沿用既有 jsDelivr / raw 规则。代理接口不接受任意 URL，强制校验仓库内相对路径、文件大小上限与栅格图片魔数（仅放行 PNG/JPEG/GIF/WebP/AVIF，拒绝 SVG/HTML），并输出 `X-Content-Type-Options: nosniff`。
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
- OpenAPI 分级文档的 `components` 引用闭包裁剪、`/v1/auth/debug-login` 文档隐藏，以及公开插件接口的字段白名单过滤。
- Docker Compose 与 systemd 部署模板的最小权限边界和 fail-closed 默认值。
