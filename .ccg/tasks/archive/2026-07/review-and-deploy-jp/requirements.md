# 需求

- 审查本地 `main` 相对 jp 实例现有版本的变更。
- 运行后端测试、前端测试和生产构建，阻断有明确失败的部署。
- 将当前已提交源码同步到 `ssh jp` 的既有部署目录。
- 保留远端 `.env`、`.venv`、Git 元数据、运行数据和 systemd unit。
- 暂不启用 artifact/review worker、runtime runner 或任何自动审查开关。
- 重启既有 `astrbot-market.service`，验证服务、健康接口和公共站点。

# 验收标准

- 本地检查无阻断问题，测试和构建通过。
- 远端依赖与锁文件同步，数据库迁移由应用启动流程正常完成。
- `astrbot-market.service` 为 active，`/health` 和公共页面正常返回。
- 审查相关功能保持关闭，未启动额外 worker/runner。
