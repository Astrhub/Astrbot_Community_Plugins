# 安全策略（Security Policy）

感谢你帮助维护 AstrBot Community Plugins 的安全。本页说明漏洞上报渠道、受支持版本范围以及安全设计文档入口。

## 受支持的版本

当前仅对仓库最新 `main` 分支及线上当前运行版本提供安全修复。

| 版本 | 是否提供安全修复 |
| --- | --- |
| `main`（最新提交） | 是 |
| 历史提交或旧部署快照 | 否（请升级到最新 `main`） |

## 报告安全漏洞

请勿通过公开 Issue、Pull Request 或社区讨论区披露未修复的安全漏洞。

本仓库已启用 GitHub Private Vulnerability Reporting，推荐优先通过私有安全公告渠道提交报告，也可以发送邮件至安全联系邮箱：

- [提交私有安全公告（GitHub Security Advisories）](https://github.com/Astrhub/Astrbot_Community_Plugins/security/advisories/new)
- 安全联系邮箱：[plugins@eloina.cn](mailto:plugins@eloina.cn)

提交报告时建议尽量提供以下信息，以便维护团队快速复现和评估：

- 受影响的组件、接口路径或文件路径；
- 最小可复现步骤、请求样例或概念验证（PoC）；
- 潜在影响范围（例如权限绕过、凭证泄露、跨站脚本、审查绕过或容器隔离失效）；
- 如已有修复思路或补丁建议，也欢迎一并附上。

维护团队在确认漏洞后会通过私有公告与报告者同步进展，并在修复发布后协调公开披露。

## 范围说明

属于本仓库安全响应范围的内容包括：

- 市场后端 API（`apps/api`）的身份认证、会话、权限控制、API Key 与数据隔离；
- 市场前端（`apps/market-web`）的 XSS、CSRF 与敏感信息保护；
- 插件包预检、静态扫描、运行时探针隔离（`runtime-runner`）与 CDN 发布门控；
- 仓库内的 Docker Compose 与 systemd 部署模板安全配置。

社区第三方插件仓库自身的功能缺陷或常规 Bug 请直接向对应插件作者的 GitHub 仓库反馈；如果发现恶意插件、插件包审查绕过或沙箱逃逸风险，请按上述私有安全公告渠道上报。

## 架构与部署安全文档

- [安全模型与部署安全（docs/security.md）](docs/security.md)：身份认证、Cookie 会话、权限矩阵、凭证脱敏、存储边界与部署加固说明；
- [系统架构与权限矩阵（docs/architecture.md）](docs/architecture.md#认证与权限模型)：前后端架构、数据模型与角色能力矩阵；
- [运行时探针安全边界（docs/runtime-runner.md）](docs/runtime-runner.md)：独立 rootless 容器引擎、网络安装代理与探针隔离契约；
- [插件审查运维手册（docs/plugin-review-operations.md）](docs/plugin-review-operations.md)：三用户隔离部署、健康度门控与安全事故处置流程。
