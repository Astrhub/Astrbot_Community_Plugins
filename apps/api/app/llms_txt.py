"""Generate ``/llms.txt`` content based on user role.

Provides a concise, machine-readable summary of the API capabilities
for LLM consumption. Content is filtered by role just like OpenAPI.
"""

from __future__ import annotations

_HEADER = """\
# AstrBot 社区插件市场 API

> 发现、评价和提交 AstrBot 插件。

## 机器读取

- OpenAPI Schema: GET /openapi.json（按登录态返回分级端点）
- 本文件: GET /llms.txt

## 认证

调用需认证的端点请使用 API Key：
Authorization: Bearer <your-api-key>

API Key 可在「个人设置」页面创建。
"""

_PUBLIC_SECTION = """\
## 可用能力

### 浏览
- 获取已上架插件列表：GET /v1/plugins
- 获取插件详情：GET /v1/plugins/{plugin_id}
- 获取站点配置：GET /v1/site
- 获取公告：GET /v1/announcements
- 获取插件源（机器可读）：GET /plugins.json
- 获取插件源 MD5：GET /plugins-md5.json

### 互动
- 点赞插件：POST /v1/plugins/{plugin_id}/like（需登录）
- 取消点赞：POST /v1/plugins/{plugin_id}/unlike（需登录）
- 评论插件：POST /v1/plugins/{plugin_id}/comments（需登录）
- 点赞评论：POST /v1/comments/{comment_id}/like（需登录）

### 提交
- 提交新插件：POST /v1/plugins/submissions（需 GitHub OAuth 登录）

### 认证
- 用户名密码登录：POST /v1/auth/internal/login
- GitHub OAuth 登录：GET /v1/auth/github/login
- 退出登录：POST /v1/auth/logout
- 检查会话：GET /v1/auth/session
"""

_USER_SECTION = """\
### 个人
- 获取当前用户信息：GET /v1/me
- 更新个人资料：PATCH /v1/me/profile
- 获取我的插件：GET /v1/me/plugins
- 获取通知列表：GET /v1/me/notifications
- 获取未读通知数：GET /v1/me/notifications/unread-count
- 标记通知已读：POST /v1/me/notifications/read
- 创建 API Key：POST /v1/me/api-keys
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
- 创建内部用户：POST /v1/core/users
- 删除用户：DELETE /v1/core/users/{user_id}
- 修改管理员角色：POST /v1/core/admins/{user_id}
- 发布公告：POST /v1/core/announcements
"""

_BOUNDARIES = """\
## 界限

- 插件具体 bug 请引导用户去评论区或 GitHub Issues
- 账号问题请联系站点管理员
- 管理操作（审核/下架/删除）应提醒用户谨慎操作
"""


def build_llms_txt(role: str) -> str:
    """Build the llms.txt content for the given role."""
    parts = [_HEADER, _PUBLIC_SECTION]
    if role in ("user", "admin", "core_admin"):
        parts.append(_USER_SECTION)
    if role in ("admin", "core_admin"):
        parts.append(_ADMIN_SECTION)
    if role == "core_admin":
        parts.append(_CORE_ADMIN_SECTION)
    parts.append(_BOUNDARIES)
    return "\n".join(parts)
