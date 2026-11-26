# GitHub 仓库选择与权限复核

在完整社区模式下，插件提交页使用现有 GitHub OAuth 登录态，向用户列出公开、未归档、未禁用且仓库名以 `astrbot_plugin_` 开头的可管理仓库。选中仓库后，系统自动从仓库读取元数据，用户无需手动填写仓库地址或提供个人访问令牌（PAT），默认 OAuth scope 也不申请 `repo` 或 `public_repo` 写权限。

> [!NOTE]
> 在本地自用单进程模式（`MARKET_STORAGE_MODE=local`）下，已登录的管理员在不开启社区源 CDN 时可直接输入公开 GitHub 仓库地址预取元数据并提交；普通用户与完整社区模式均严格要求 GitHub OAuth 权限核验。

## 仓库列表与权限判定

- 仓库列表来自 GitHub API `/user/repos`，涵盖个人、协作及组织成员仓库，并按最后推送时间倒序排列（`sort=pushed&direction=desc`）；分页加载与后台自动刷新均沿用该排序。
- 服务端仅认可 GitHub 返回的 `push`、`maintain`、`admin` 仓库管理权限；个人仓库通过稳定的 `owner.id` 校验归属，组织所有者可通过 `read:org` 下的 `active` + `admin` 组织成员身份补充验证。
- 仅具备普通组织成员身份不构成插件提交权限。如果目标组织限制了第三方 OAuth App 访问，需在 GitHub 组织设置中授权本应用，系统不会使用站点 Token 或匿名请求代替个人权限校验。
- 已在市场登记的插件仍受市场账号归属约束，拥有 GitHub 仓库写权限不会自动接管其他用户已登记的插件记录。

## 授权生命周期与发布前复核

- **服务端加密存储**：OAuth 回调阶段将访问凭据分别写入两处：按当前登录会话写入 Redis 短期缓存（最长 8 小时），并同步加密持久化至 `market_oauth_credentials`；Redis 缓存过期或缺失时自动回退读取已绑定 GitHub 身份对应的持久化凭据，浏览器不接收任何 GitHub 访问凭据。
- **密钥隔离**：凭据采用 Fernet 加密，加密密钥由 API 独有的 `OAUTH_CREDENTIAL_KEY` 经 HKDF 派生。`artifact-worker` 不持有该密钥；轮换 `OAUTH_CREDENTIAL_KEY` 或撤权后，用户重新连接 GitHub 即可刷新授权。
- **失效与重连**：未绑定 GitHub 账号、已解绑该 GitHub 身份，或 GitHub API 返回 `401` 时，服务端同步撤销对应持久化凭据并要求重新连接 GitHub，授权完成后自动回到原页面。
- **操作级实时复核**：提交、编辑、申请上架、下架与手动刷新均实时核验仓库权限；更换关联仓库时，服务端先核验旧仓库权限，再核验新仓库权限，防止通过更换地址绕过撤权。
- **发布前身份复核**：每个新 Artifact 都会保存提交时的仓库 ID、所有者 ID、提交身份及维护者快照。人工批准后，API 使用后台站点令牌复核公开仓库的身份、归属与公开状态，并确认批准人仍具管理员权限且批准包哈希与市场归属未变更，不依赖作者的短期 OAuth 会话。一旦站点令牌缺失，或仓库发生转移、改名、私有化、归档，发布流程会立即中止；遇到临时网络故障则允许重试。
- **CDN 自动送审与管理员权限**：作者提交、编辑和开启 CDN 时均核验个人 OAuth 权限；开启 CDN 后的新版本自动送审使用开启时保存的权限快照与站点令牌复核，手动上传触发的自动批准仍核验个人 OAuth。发布事务提交前会再次核对版本、仓库、维护者与 CDN 开关，仅复制已过审的不可变包。站点管理员权限在发布前同样复核其角色是否仍然有效；缺少授权快照的历史 Artifact 沿用既有发布逻辑，不会追补历史授权校验。

## 服务端部署配置

1. 在 API 与 `artifact-worker` 的独立环境文件中配置相同的随机 `ARTIFACT_AUTHORIZATION_TOKEN`（建议使用服务端生成的 48 字节随机字符串）。该令牌仅用于内部发布权限复核，并非用户 Token。
2. 在 `artifact-worker` 环境文件中额外配置 `ARTIFACT_AUTHORIZATION_URL` 指向 API 根地址：systemd 部署通常为本地监听地址（如 `http://127.0.0.1:8787`），Docker Compose 部署为 `http://app:8787`。
3. 内部复核端点不在公开 OpenAPI 文档中暴露，且仅返回允许发布的版本与仓库范围，不返回任何 OAuth 凭据。缺少该配置时，新 Artifact 无法通过发布复核。
4. `GET /v1/me/github/repositories` 与内部复核端点均返回 `Cache-Control: private, no-store`。仓库列表按 GitHub 分页返回，前端自动跨空页查找并为仓库较多的账号保留“继续加载仓库”入口。

> [!CAUTION]
> API、`artifact-worker` 与前端应同步保持协议版本一致，且严禁将 API 的 `GITHUB_CLIENT_SECRET` 写入 `artifact-worker` 环境文件。
