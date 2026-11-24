"""Generate ``/llms.txt`` content based on the 5-tier user hierarchy.

Provides a concise, machine-readable summary of the API capabilities
for LLM consumption. Content is filtered by tier just like OpenAPI.
"""

from __future__ import annotations

_HEADER = """\
# AstrBot 社区插件市场 API

> 发现、评价和提交 AstrBot 插件。

## 机器读取

- OpenAPI Schema: GET /openapi.json（按 5 级身份返回分级端点）
- 本文件: GET /llms.txt

## 认证

调用需认证的端点请使用 API Key：
Authorization: Bearer <your-api-key>

API Key 可在「个人设置」页面创建（注册用户可创建只读密钥，开发者及以上可创建写入密钥）。
"""

_PUBLIC_SECTION = """\
## 可用能力

### 浏览
- 获取已上架插件列表：GET /v1/plugins
- 获取插件详情：GET /v1/plugins/{plugin_id}
- 获取公开作者列表：GET /v1/authors
- 获取作者已上架插件：GET /v1/authors/{username}/plugins
- 获取海外平台插件图标/README 图片/作者头像代理：GET /v1/plugins/{plugin_id}/logo · GET /v1/plugins/{plugin_id}/readme-asset · GET /v1/authors/{username}/avatar
- 获取站点配置：GET /v1/site
- 获取公告：GET /v1/announcements
- 获取插件源（机器可读）：GET /plugins.json
- 获取插件源 MD5：GET /plugins-md5.json

### 互动
- 点赞插件：POST /v1/plugins/{plugin_id}/like（需登录）
- 取消点赞：POST /v1/plugins/{plugin_id}/unlike（需登录）
- 评论插件：POST /v1/plugins/{plugin_id}/comments（需登录）
- 点赞评论：POST /v1/comments/{comment_id}/like（需登录）

### 认证
- 用户名或邮箱登录：POST /v1/auth/internal/login
- 邮箱一步注册（关闭邮箱验证时）：POST /v1/auth/email/register
- 请求注册验证码：POST /v1/auth/email/register/request
- 验证并完成注册：POST /v1/auth/email/register/verify
- 请求找回密码验证码：POST /v1/auth/email/password-reset/request
- 验证并重置密码：POST /v1/auth/email/password-reset/verify
- GitHub OAuth 登录：GET /v1/auth/github/login
- 第三方 Git 平台 OAuth 登录（GitLab / Gitee / AtomGit / Gitea）：GET /v1/auth/oauth/{provider}/login
- 退出登录：POST /v1/auth/logout
- 检查会话：GET /v1/auth/session
"""

_USER_SECTION = """\
### 注册用户能力
- 获取当前用户信息与 5 级分层状态：GET /v1/me
- 更新个人资料：PATCH /v1/me/profile
- 修改或设置本地密码：POST /v1/me/password
- 申请换绑邮箱：POST /v1/me/email/change/request
- 验证换绑邮箱：POST /v1/me/email/change/verify
- 查看与解绑 OAuth 身份：GET /v1/me/oauth-identities · DELETE /v1/me/oauth-identities/{identity_id}
- 获取通知列表：GET /v1/me/notifications
- 获取未读通知数：GET /v1/me/notifications/unread-count
- 标记通知已读：POST /v1/me/notifications/read
- 创建个人 API Key（注册用户仅只读）：POST /v1/me/api-keys
"""

_DEVELOPER_SECTION = """\
## 开发者能力

### 插件提交与管理
- 预取仓库元数据：POST /v1/plugins/submissions/metadata-preview（社区模式需绑定任意 OAuth）
- 提交新插件：POST /v1/plugins/submissions（社区模式需绑定任意 OAuth）
- 获取我的插件：GET /v1/me/plugins
- 查看可管理 GitHub 仓库：GET /v1/me/github/repositories
- 查看指定 Git 平台可管理仓库：GET /v1/me/git/{provider}/repositories
- 更新自有插件信息：PATCH /v1/plugins/{plugin_id}
- 开关自有插件 CDN：PATCH /v1/plugins/{plugin_id}/cdn
- 申请上架或主动下架：POST /v1/plugins/{plugin_id}/request-list · POST /v1/plugins/{plugin_id}/unlist
- 查看我的插件包版本：GET /v1/me/artifacts
"""

_ADMIN_SECTION = """\
## 管理员能力

### 审核
- 获取所有插件（含未上架）：GET /v1/admin/plugins
- 审核上架：POST /v1/admin/plugins/{plugin_id}/list
- 管理员下架：POST /v1/admin/plugins/{plugin_id}/unlist
- 获取待审核列表：GET /v1/plugins/submissions

### 用户管理
- 获取所有用户：GET /v1/admin/users
- 禁言用户：POST /v1/admin/users/{user_id}/mute
- 解除禁言：POST /v1/admin/users/{user_id}/unmute
- 删除评论：DELETE /v1/admin/comments/{comment_id}
- 管理 Git 仓库绑定：GET /v1/admin/git-bindings · POST /v1/admin/git-bindings/{binding_id}/revoke · POST /v1/admin/git-bindings/{binding_id}/transfer

### API Key
- 签发 API Key：POST /v1/api-keys
- 获取 API Key 列表：GET /v1/api-keys
"""

_CORE_ADMIN_SECTION = """\
## 核心管理员能力

### 系统
- 获取系统设置：GET /v1/admin/settings
- 更新系统设置：PUT /v1/admin/settings
- 测试邮件发送：POST /v1/admin/settings/email/test
- 获取安装状态：GET /v1/admin/setup/status

### 用户管理
- 创建本地用户：POST /v1/core/users
- 删除用户：DELETE /v1/core/users/{user_id}
- 修改管理员角色：POST /v1/core/admins/{user_id}
- 发布公告：POST /v1/core/announcements
"""

_BOUNDARIES = """\
## 界限

- 插件具体 bug 请引导用户去评论区或仓库 Issues
- 账号问题请联系站点管理员
- 管理操作（审核/下架/删除）应提醒用户谨慎操作
"""


def build_llms_txt(role: str) -> str:
    """Build the llms.txt content for the given 5-tier role."""
    parts = [_HEADER, _PUBLIC_SECTION]
    if role in ("user", "developer", "admin", "core_admin"):
        parts.append(_USER_SECTION)
    if role in ("developer", "admin", "core_admin"):
        parts.append(_DEVELOPER_SECTION)
    if role in ("admin", "core_admin"):
        parts.append(_ADMIN_SECTION)
    if role == "core_admin":
        parts.append(_CORE_ADMIN_SECTION)
    parts.append(_BOUNDARIES)
    return "\n".join(parts)
