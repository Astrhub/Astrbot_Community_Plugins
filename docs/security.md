# 安全

## 身份认证

首次启动向导会注册内部核心管理员账号，用于配置 GitHub OAuth、管理员权限和站点策略。GitHub OAuth 是普通用户和插件所有者的身份来源；GitHub 用户不会自动成为核心管理员，后续仅能通过核心管理员显式提升或受信任组织规则获得管理员权限。

登录流程（`apps/api/app/main.py`）：
1. 内部核心管理员通过 `/v1/auth/internal/login` 登录
2. 若开启 GitHub 登录，访问 `/v1/auth/github/login` 获取 GitHub OAuth 授权链接
3. GitHub 回调 `/v1/auth/github/callback`，交换 access token 并获取用户信息
4. 若配置了 `GITHUB_ADMIN_ORG`，自动提升组织成员为管理员

## 权限

- 核心管理员 — 授予/撤销管理员，发布公告（`/v1/core/*` 端点）
- 普通管理员 — 上架/下架插件，删除评论，禁言用户，管理审核（`/v1/admin/*` 端点）
- 插件所有者 — 仅可编辑自己通过 GitHub 仓库所有权验证的插件元数据

权限检查函数位于 `apps/api/app/auth.py`。

## API Key

API key 用于机器客户端（如未来的 AstrBot WebUI 插件）。Key 应具备作用域（scopes）、可撤销，并记录操作日志。使用时通过 `Authorization: Bearer <key>` 头发送。

API key 可通过环境变量 `MARKET_API_KEYS` 静态配置，也可通过 `/v1/api-keys` 端点动态创建（需管理员权限）。格式：`名称:密钥:scope1|scope2`。

## 数据安全

将插件元数据、README 内容和评论视为不可信输入。需验证 GitHub 仓库 URL 格式，对渲染的 Markdown 进行清理，审核操作在服务端存储。

插件名称必须匹配 `astrbot_plugin_*` 模式，仓库 URL 必须为 `https://github.com/<owner>/<repo>` 格式，且提交者需证明 GitHub 仓库所有权。

系统设置中的 GitHub Client Secret、SMTP 密码和 Cloudflare Email API Token 不会通过管理接口明文返回。前端收到 `********` 遮蔽值时，后端会保留已有密钥而不是写入遮蔽文本。

## 存储

PostgreSQL 是市场数据的持久化存储。Redis 当前用于会话令牌短期存储，并依赖 TTL 自动过期。两者均不应存储 GitHub 密钥或超出登录流程所需的最小 OAuth 原始令牌。

## 首次启动设置

若首次启动时缺少 PostgreSQL 或 Redis 配置，前端 UI 可通过 `/v1/setup` 页面按主机名、端口、数据库名、账号、密码和 SSL 开关收集配置，并注册内部核心管理员。配置写入 `apps/api/data/runtime.env`。写入后，仅核心管理员可修改。

环境变量优先级：运行时配置文件（`runtime.env`）中的值会被同名系统环境变量覆盖（`apps/api/app/config.py` 第 61 行）。

核心管理员可通过 `/v1/admin/settings` 修改站点、登录、GitHub OAuth、市场策略和邮件服务。Cloudflare Email Service 使用 Cloudflare REST API 的 `/accounts/{account_id}/email/sending/send` 端点，并只将 Cloudflare 错误摘要返回给管理员。

## Session 安全

- Cookie 默认 `httponly=True`
- `COOKIE_SECURE` 在生产环境应设为 `true`（需要 HTTPS）
- `COOKIE_SAME_SITE` 默认 `Lax`
- 会话默认有效期 7 天（`SESSION_MAX_AGE_SECONDS`）
- OAuth state 参数使用独立 cookie，有效期 10 分钟
