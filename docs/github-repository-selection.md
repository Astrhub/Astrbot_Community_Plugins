# GitHub 仓库选择与权限复核

提交页使用现有 GitHub OAuth 登录，列出公开、未归档、未禁用且名称以 `astrbot_plugin_` 开头的可管理仓库。选中后自动读取 metadata，无需填写仓库地址或个人 Token。默认 OAuth scope 不增加 `repo` 或 `public_repo` 写权限。

仓库列表来自 GitHub `/user/repos`，包含个人、协作和组织成员仓库，按最后推送时间倒序排列（`sort=pushed&direction=desc`）。分页加载和后台自动刷新沿用这一顺序。服务端只接受 GitHub 返回的 `push`、`maintain`、`admin` 权限；个人仓库以稳定 owner ID 识别。组织拥有者可通过 `read:org` 的 active/admin 成员身份补充验证。普通成员身份本身不赋予提交权限。组织限制 OAuth 应用访问时，需要在 GitHub 上允许该应用；不能通过站点 Token 或匿名请求代替用户权限。

## 授权生命周期

- OAuth 回调将访问凭据加密后保存在 Redis 独立键中，绑定市场用户 ID、GitHub 用户 ID和登录会话。最长有效期为 8 小时，浏览器不接收 GitHub 访问凭据。
- 加密使用 Fernet，密钥从 API 独有的 OAuth client secret 经 HKDF 派生；worker 不持有该 secret。轮换 OAuth client secret 后用户需重新连接。
- 旧登录会话没有新凭据时，提交页显示“连接 GitHub”，完成授权后返回 `/submit`。过期、撤销和账号换绑不会沿用原授权。
- 提交、编辑、重新上架、下架和手动刷新重新核验权限。更换仓库时先核验旧仓库，再核验新仓库，不能用更换地址绕过撤权。
- 新 Artifact 保存提交时的 repo ID、owner ID、提交身份及市场维护者快照。人工批准后，API 使用后台站点令牌复核公开仓库身份与归属，并确认批准人仍是管理员、批准包哈希和市场归属未变化，不依赖作者的短期 OAuth 会话。站点令牌缺失、仓库转移/改名/私有化或归档时停止发布；网络故障可重试。
- 作者提交、编辑和开启 CDN 仍核验个人 OAuth 权限。CDN 自动更新使用开启时保存的仓库权限快照和站点令牌复核，不依赖短期会话；普通手动上传的自动批准仍核验个人 OAuth。发布事务继续检查版本、仓库、维护者及 CDN 开关，只复制已审查的不可变包。
- 站点管理员的既有管理权限仍单独处理，发布前同样确认管理员角色没有失效。历史没有授权快照的 Artifact 保留原有行为，不宣称已补做历史授权验证。

## 服务端部署配置

API 和 artifact worker 的独立环境文件中都设置同一个随机 `ARTIFACT_AUTHORIZATION_TOKEN`，建议使用服务端生成的 48 字节随机值。它仅用于内部发布权限复核，不是用户 Token。

worker 额外设置 `ARTIFACT_AUTHORIZATION_URL` 为 API 根地址：systemd 通常为 `http://127.0.0.1:8787`（jp 当前为 8788），Compose 为 `http://app:8787`。API 通过隐藏于 OpenAPI 的内部端点返回版本和仓库范围，绝不返回 OAuth 凭据。缺少配置时，新 Artifact 不会跳过发布复核。

同步 `apps/api/uv.lock` 安装 cryptography 依赖，然后同步更新 API、worker 和前端。不要把 API 的 OAuth client secret 加入 worker 环境。

`GET /v1/me/github/repositories` 和内部权限端点均使用 `private, no-store`。仓库列表按 GitHub 分页返回，前端自动继续查找并为大型账号保留“继续加载仓库”。既有市场插件仍有登记账号归属限制，有 GitHub 写权限并不自动接管别人登记的插件。
