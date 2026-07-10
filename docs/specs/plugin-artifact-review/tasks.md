# 插件制品审查与发布系统 - P0/P1 任务清单

## Implementation Tasks

- [x] 1. **P0：建立版本化数据库迁移**
    - [x] 1.1. 实现迁移 runner
        - *Goal*: 为 asyncpg 项目提供可审计、可重复执行的版本化 SQL 迁移。
        - *Details*: 新增 `market_schema_migrations`、advisory lock、文件 checksum、按版本排序执行和失败回滚；从 `PgRedisMarketStore._ensure_schema()` 调用 runner。
        - *Requirements*: FR-018、FR-019；P0 验收 1、2。
    - [x] 1.2. 创建 artifact foundation 迁移
        - *Goal*: 新增 P0/P1 所需表、约束、索引和插件增量字段。
        - *Details*: 创建 `plugin_artifacts`、`artifact_files`、`review_runs`、`review_findings`、`review_decisions`、`artifact_jobs`、`outbox_events`；为 `market_plugins` 增加 `repo_version`、`current_artifact_id`、`category`、`category_source` 并回填 legacy category。
        - *Requirements*: FR-001、FR-002、FR-007、FR-013、FR-015、FR-018。
    - [x] 1.3. 添加迁移测试
        - *Goal*: 证明空库、现有 schema、重复启动和 checksum 冲突行为正确。
        - *Details*: 保留现有可选 PostgreSQL 集成测试开关，增加纯 SQL/runner 单元测试和实际 PG 集成断言。
        - *Requirements*: P0 验收 1、2；Non-functional Reliability。

- [x] 2. **P0：建立 artifact 配置和运行时装配**
    - [x] 2.1. 增加 artifact 配置模型
        - *Goal*: 集中管理功能开关、存储、CDN、上传限制、任务租约和保留策略。
        - *Details*: 配置从环境读取；对象存储密钥不进入公开站点配置；增加 fail-closed 配置校验和脱敏状态输出。
        - *Requirements*: FR-019；Security、Compatibility。
    - [x] 2.2. 建立 artifact runtime factory
        - *Goal*: 为 FastAPI 和独立 worker 复用 repository、storage、service 和 job runner 装配。
        - *Details*: artifact 关闭时使用显式 unavailable runtime；setup 热切换 PostgreSQL store 后重新绑定 repository；不在 API lifespan 启动 artifact worker。
        - *Requirements*: FR-013、FR-018、FR-019。
    - [x] 2.3. 增加 artifact health 状态
        - *Goal*: 运维可以确认数据库、隔离存储、发布存储和 worker 是否就绪。
        - *Details*: `/health` 只返回状态，不返回 endpoint 凭据、bucket 私有信息或对象 key。
        - *Requirements*: FR-019；Observability。

- [x] 3. **P0：实现领域模型和 ArtifactRepository**
    - [x] 3.1. 定义枚举和状态转换
        - *Goal*: 让 review/publication/job/risk 状态具有单一来源。
        - *Details*: 实现服务端允许转换表；`critical` 仅作为 risk level；非法转换产生稳定错误码。
        - *Requirements*: FR-002、FR-007、FR-008、FR-012、FR-013。
    - [x] 3.2. 实现 PostgreSQL repository
        - *Goal*: 覆盖 artifact、files、runs、findings、decisions、jobs、outbox 和当前发布指针的事务操作。
        - *Details*: 使用带条件 SQL、`SELECT FOR UPDATE`、批量查询和分页；用户删除时保留审计快照；避免 N+1。
        - *Requirements*: FR-001、FR-002、FR-007、FR-008、FR-009、FR-013、FR-016、FR-020。
    - [x] 3.3. 实现测试用内存 repository
        - *Goal*: 让大部分 API 和领域测试不依赖真实 PostgreSQL。
        - *Details*: 与 Protocol 行为保持一致，但生产无 PostgreSQL 时 artifact API 仍返回 503，不偷偷启用内存发布。
        - *Requirements*: FR-018、FR-019；Maintainability。
    - [x] 3.4. 添加 repository 与并发测试
        - *Goal*: 验证幂等 artifact、唯一发布版本、并发决策和 job 领取。
        - *Details*: 覆盖相同 SHA 重复提交、双管理员批准、过期租约回收和 outbox dedupe。
        - *Requirements*: P0 验收 5、6；P1 验收 7。

- [x] 4. **P0：实现隔离与发布存储**
    - [x] 4.1. 定义 ArtifactStorage Protocol 和 key builder
        - *Goal*: 固定隔离、文本内容和公开发布对象的接口与命名规则。
        - *Details*: CDN key 严格使用 `{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`；suffix 只生成一次；所有路径段 allowlist 校验和逐段编码。
        - *Requirements*: FR-004、FR-009；P1 CDN 路径验收。
    - [x] 4.2. 实现 LocalArtifactStorage
        - *Goal*: 支持开发、单元测试和本地 Docker 验证。
        - *Details*: 根目录位于仓库外或专用 volume；原子写入、条件创建、stat、摘要 metadata、撤回和 orphan 删除。
        - *Requirements*: FR-004、FR-009、FR-012。
    - [x] 4.3. 实现 S3/R2ArtifactStorage
        - *Goal*: 支持生产隔离 bucket、发布 bucket 和 CDN 基础 URL。
        - *Details*: 引入成熟 S3 SDK；阻塞调用放入线程；目标存在时比较摘要，禁止覆盖不同内容；凭据只来自环境。
        - *Requirements*: FR-004、FR-009、FR-012、FR-019。
    - [x] 4.4. 添加存储和路径测试
        - *Goal*: 验证固定 URL、重试稳定性、同版本不同 artifact 不冲突和撤回。
        - *Details*: 覆盖非法作者 ID、repo/version/path traversal、条件创建、摘要一致幂等和摘要冲突。
        - *Requirements*: P0 验收 4；P1 CDN 路径验收。

- [x] 5. **P0：实现持久任务与独立 worker**
    - [x] 5.1. 实现 job claim/lease/retry
        - *Goal*: worker 重启后任务可恢复，多 worker 不重复领取有效租约任务。
        - *Details*: 使用 `FOR UPDATE SKIP LOCKED`、worker ID、lease expiry、续租、最大尝试次数和指数退避；确定性失败不重试。
        - *Requirements*: FR-013；P0 验收 5、6。
    - [x] 5.2. 实现 worker 主循环和阶段调度
        - *Goal*: 独立处理 `precheck/static_scan/publish/revoke/outbox/cleanup_orphan`。
        - *Details*: job payload 只保存 ID；日志统一带 artifact/run/job/stage；SIGTERM 时停止领取并安全释放。
        - *Requirements*: FR-006、FR-007、FR-009、FR-012、FR-013。
    - [x] 5.3. 增加 Docker worker 服务
        - *Goal*: 默认部署可以独立运行 artifact worker。
        - *Details*: 复用应用镜像；本地存储模式增加共享专用 volume；健康依赖 PostgreSQL；不挂载 Docker Socket。
        - *Requirements*: Core Principle 7、FR-013、FR-019。
    - [x] 5.4. 添加 worker 恢复测试
        - *Goal*: 验证崩溃、租约过期、重复投递和优雅退出。
        - *Details*: 模拟阶段完成前后异常，确保下一阶段最多创建一次。
        - *Requirements*: P0 验收 5、6；Reliability。

- [x] 6. **P1：实现 GitHub 固定来源与 ZIP 上传**
    - [x] 6.1. 提取共享 GitHub repo 解析和所有权校验
        - *Goal*: artifact 与现有提交/metadata 同步使用一致的规范化规则。
        - *Details*: 从 `main.py` 提取小型共享模块；保持现有 URL 校验和测试；不扩大支持范围。
        - *Requirements*: FR-001、FR-003、FR-016。
    - [x] 6.2. 实现 GitHubSourceClient
        - *Goal*: 把 ref/default branch 固定到 commit 并流式写入 quarantine。
        - *Details*: 仅公开 GitHub；允许主机白名单；限制响应大小；记录 ref/commit/SHA；Token 不进入日志或 job。
        - *Requirements*: FR-003；P1 验收 1、2。
    - [x] 6.3. 实现上传流式接收
        - *Goal*: 上传 ZIP 不一次性进入内存，并在超限时立即终止。
        - *Details*: 引入 multipart 依赖；计算 SHA；写 quarantine；成功后创建 artifact 与 precheck job；失败清理临时对象。
        - *Requirements*: FR-003、FR-004、FR-019。
    - [x] 6.4. 实现安全插件 registration
        - *Goal*: 新插件先获得稳定身份，更新 artifact 不覆盖 listed 插件记录。
        - *Details*: 新增 idempotent registration API/service；存在时只验证所有权并返回，不更新稳定字段；旧 submission API 保持兼容。
        - *Requirements*: FR-001、FR-011、FR-018、FR-020。

- [x] 7. **P1：实现预检和文件清单**
    - [x] 7.1. 实现 ZIP central directory 安全检查
        - *Goal*: 在任何提取前阻止穿越、zip bomb、加密、链接、冲突路径和资源滥用。
        - *Details*: Unicode NFC、路径深度、压缩比、总大小、单文件、文件数、wrapper 根目录和嵌套压缩检测；不调用 `extractall()`。
        - *Requirements*: FR-005；Security。
    - [x] 7.2. 实现 metadata 安全解析和身份校验
        - *Goal*: 验证 AstrBot 必要结构、`main.py`、metadata 字段、repo/name 和兼容版本。
        - *Details*: 显式依赖安全 YAML 和版本解析库；兼容 `v1.1/v1.1.1`；保存原始与规范版本；拒绝 LFS/submodule/原生执行文件。
        - *Requirements*: FR-005；AstrBot plugin layout。
    - [x] 7.3. 生成 artifact_files 和 tree hash
        - *Goal*: 保存完整文件清单并为后续 diff/浏览建立稳定基础。
        - *Details*: 流式计算文件摘要、大小、文本属性、行数；受限文本写私有 content key；按路径排序计算 tree SHA。
        - *Requirements*: FR-002、FR-004、FR-007。
    - [x] 7.4. 添加恶意 ZIP corpus 测试
        - *Goal*: 覆盖所有硬拒绝规则和资源边界。
        - *Details*: 测试文件动态生成，不提交大二进制样本；验证失败 run、finding 和 system decision。
        - *Requirements*: P1 验收 3、4、5。

- [x] 8. **P1：实现基础静态扫描**
    - [x] 8.1. 实现 Python AST/tokenize 规则
        - *Goal*: 结构化识别动态执行、Shell、远程加载、敏感访问和混淆。
        - *Details*: 每条规则有稳定 ID、severity、message、suggestion 和 fingerprint；语法错误作为 finding，不执行 import。
        - *Requirements*: FR-006、FR-007。
    - [x] 8.2. 实现 requirements 规则
        - *Goal*: 识别 editable、本地路径、VCS、直接 URL 和无法解析的依赖声明。
        - *Details*: P1 只分析文本，不联网解析漏洞，不安装依赖。
        - *Requirements*: FR-006；P0/P1 Out of Scope。
    - [x] 8.3. 实现扫描完成状态汇总
        - *Goal*: 生成 risk level 并把正常扫描 artifact 转入 `pending_review`。
        - *Details*: 扫描器异常进入 `processing_failed`；正常无命中与异常严格区分；不自动批准。
        - *Requirements*: FR-006、FR-007、FR-008。
    - [x] 8.4. 添加 scanner 测试
        - *Goal*: 验证规则命中、行号、fingerprint、重试去重和完整 artifact 覆盖。
        - *Details*: 使用小型源码字符串和多文件 fixture。
        - *Requirements*: P1 验收 4、5。

- [x] 9. **P1：实现 artifact API 与权限**
    - [x] 9.1. 实现创建和作者查询接口
        - *Goal*: 提供 registration、ZIP/GitHub artifact 创建、本人列表、详情、runs 和 findings。
        - *Details*: Pydantic typed schemas、分页、稳定错误码、所有权检查；P1 不返回完整源码。
        - *Requirements*: FR-003、FR-007、FR-016、FR-020。
    - [x] 9.2. 实现管理员队列接口
        - *Goal*: 按状态、风险、插件和时间分页查询待审 artifact。
        - *Details*: 使用 `can_moderate_plugins`；响应包含 repo_version/published_version、run 摘要和最高风险。
        - *Requirements*: FR-008、FR-016、FR-017、FR-020。
    - [x] 9.3. 更新 OpenAPI 角色过滤和静态契约
        - *Goal*: public/user/admin/core_admin 只看到允许 API。
        - *Details*: 新增 tags、schema 和错误码；同步 `docs/api/openapi.yaml` 与相关测试。
        - *Requirements*: FR-016、FR-020。
    - [x] 9.4. 添加 API 权限和分页测试
        - *Goal*: 防止跨作者读取、普通用户决定和未授权隔离访问。
        - *Details*: 覆盖未登录、非所有者、admin、core admin 和 muted 用户。
        - *Requirements*: P1 验收 6、13。

- [x] 10. **P1：实现人工决策、发布和撤回**
    - [x] 10.1. 实现批准/拒绝领域动作
        - *Goal*: 通过行锁、状态检查和 decision 审计实现幂等人工复核。
        - *Details*: 批准时校验强制 runs、repo_version 和作者隔离；拒绝理由必填；作者管理员不能普通批准自己 artifact。
        - *Requirements*: FR-008、FR-016。
    - [x] 10.2. 实现 Publisher
        - *Goal*: 条件发布确切过审 ZIP，并原子切换当前 artifact 指针。
        - *Details*: 固定 key、对象摘要验证、事务内二次版本检查、孤儿保护和 publish retry。
        - *Requirements*: FR-009、FR-011；P1 验收 7、8、9。
    - [x] 10.3. 实现撤回
        - *Goal*: 严重风险时先移出 feed，再删除公开对象并触发 CDN 清理。
        - *Details*: 保存 revoke decision；失败进入重试状态；不删除审计记录和 quarantine 保留证据。
        - *Requirements*: FR-012。
    - [x] 10.4. 添加决策与发布失败矩阵测试
        - *Goal*: 验证双批准、版本漂移、对象冲突、对象成功/DB 失败和撤回失败。
        - *Details*: 每条失败路径断言 current artifact 和旧 CDN 对象不被错误覆盖。
        - *Requirements*: P1 验收 7、8、9、10。

- [x] 11. **P1：修正仓库版本同步和插件源**
    - [x] 11.1. 将 GitHub metadata version 写入 repo_version
        - *Goal*: 仓库版本继续及时更新，但不覆盖 artifact 权威字段。
        - *Details*: 从同步字段移除 `download_url`；把 metadata `version` 映射为 `repo_version`；保持 stars/logo/desc 等现有同步。
        - *Requirements*: FR-010、FR-011；P0 验收 7、8。
    - [x] 11.2. 批量补充 current artifact 发布信息
        - *Goal*: `/v1/plugins` 和 feed 无 N+1 地得到 published_version/download_url。
        - *Details*: repository 批量查询当前 artifacts；legacy 插件走兼容分支。
        - *Requirements*: FR-010、FR-018；Performance。
    - [x] 11.3. 修改 feed 组合规则
        - *Goal*: 版本准确且 CDN URL 永远与该版本匹配。
        - *Details*: repo_version 优先；版本不一致时 URL 为空；一致且 published 才输出；MD5 随版本或 URL 变化。
        - *Requirements*: FR-010、FR-011；P1 验收 10、11、12。
    - [x] 11.4. 添加 legacy 与版本分叉回归测试
        - *Goal*: 覆盖无 artifact、legacy URL、仓库升级未过审、拒绝、发布成功和撤回。
        - *Details*: 保留现有 feed 测试并补充新断言。
        - *Requirements*: FR-018；Compatibility。

- [x] 12. **P1：实现可靠通知**
    - [x] 12.1. 抽取可复用 email service
        - *Goal*: 让 API 和 worker 共用现有 SMTP/Cloudflare 实现，避免 worker 导入 `main.py`。
        - *Details*: 保持现有邮件测试与脱敏行为；不改变系统设置 API。
        - *Requirements*: FR-014、Maintainability。
    - [x] 12.2. 实现 artifact outbox dispatcher
        - *Goal*: 可靠发送站内和邮件通知，重试不重复。
        - *Details*: 处理预检失败、待审、批准、拒绝、发布失败和撤回；模板只包含状态、简短原因和工作台链接。
        - *Requirements*: FR-013、FR-014。
    - [x] 12.3. 添加内容安全与去重测试
        - *Goal*: 证明通知不包含源码、evidence、对象 key、Token 且 dedupe 生效。
        - *Details*: 使用恶意文件内容和错误消息作为输入测试转义与截断。
        - *Requirements*: P1 验收 14。

- [x] 13. **P1：实现最小插件工作台前端**
    - [x] 13.1. 增加 TypeScript artifact 类型和独立 Pinia store
        - *Goal*: 将 artifact 状态与现有市场浏览 store 分离。
        - *Details*: typed actions 覆盖注册、提交、本人列表、管理员队列、详情、findings、批准和拒绝；不保存 ZIP/源码正文。
        - *Requirements*: FR-017、FR-020。
    - [x] 13.2. 实现工作台组件
        - *Goal*: 完成作者提交/历史和管理员最小复核闭环。
        - *Details*: `PluginWorkbench` 作为薄路由视图；Submission/List/Review/Status 组件使用 props down/events up；展示 repo_version/published_version 差异。
        - *Requirements*: FR-017。
    - [x] 13.3. 添加路由守卫和入口
        - *Goal*: 未登录用户不能进入工作台，个人中心和管理员页只保留入口。
        - *Details*: route meta 保存认证要求；过滤器放在 query；后端仍是权限边界；避免重定向循环。
        - *Requirements*: FR-016、FR-017。
    - [x] 13.4. 添加前端行为测试
        - *Goal*: 覆盖提交模式、异步状态、角色命令和路由守卫。
        - *Details*: 增加 Vue Test Utils/Pinia test 支持；黑盒断言可见结果和 emitted events；正确使用 `flushPromises`。
        - *Requirements*: P1 验收 15；Frontend Testing Strategy。

- [x] 14. **部署、文档与最终验证**
    - [x] 14.1. 更新部署配置和示例环境
        - *Goal*: 运维能够配置 local 或 S3/R2、CDN、限制和 worker。
        - *Details*: 更新 Docker Compose、Dockerfile/entry command、`.env.example`、README；不提交真实凭据和运行数据。
        - *Requirements*: FR-019；Security。
    - [x] 14.2. 更新架构、安全和 API 文档
        - *Goal*: 文档准确描述 PostgreSQL 新表、worker、对象存储、权限、feed 版本语义和撤回流程。
        - *Details*: 更新 `docs/architecture.md`、`docs/security.md`、`docs/api/openapi.yaml`。
        - *Requirements*: FR-020、Compatibility。
    - [x] 14.3. 运行全量验证
        - *Goal*: 证明 P0/P1 闭环和旧功能无回归。
        - *Details*: 运行 Ruff check/format check、API pytest、前端 Vitest、前端 build；检查 git diff、敏感文件和生成物。
        - *Requirements*: 所有 Acceptance Criteria。
    - [x] 14.4. 完成安全自审
        - *Goal*: 在交付前检查路径、权限、竞态、日志、凭据、对象可见性和撤回语义。
        - *Details*: 使用恶意 ZIP corpus 和失败注入复核；记录未覆盖风险和 P2/P3 前置事项。
        - *Requirements*: Security、Reliability、Observability。

## Task Dependencies

- Task 1 是所有持久化工作的前置。
- Tasks 2、3、4 在 Task 1 的数据模型确定后可局部并行，但合并前必须统一 interfaces。
- Task 5 依赖 Tasks 2、3、4。
- Task 6 依赖 Tasks 2、3、4；不依赖静态扫描实现。
- Task 7 依赖 Tasks 3、4、5、6。
- Task 8 依赖 Tasks 3、5、7。
- Task 9 依赖 Tasks 2、3、6、7、8。
- Task 10 依赖 Tasks 3、4、5、8、9。
- Task 11 依赖 Tasks 3、10。
- Task 12 依赖 Tasks 3、5、9、10。
- Task 13 依赖 Task 9 的 API 合约和 Task 10 的决策接口稳定。
- Task 14 在所有实现任务完成后执行。

## Estimated Timeline

本清单不作固定工期承诺，以下仅表示相对规模和执行顺序：

- Tasks 1-5（P0 基础）：Large。
- Tasks 6-8（P1 获取与扫描）：Large。
- Tasks 9-12（P1 API、发布、feed、通知）：Large。
- Task 13（最小工作台）：Medium。
- Task 14（部署、文档、验证）：Medium。

实现时按“一项任务、一次针对性验证、一次状态更新”推进；不得为了赶进度跳过迁移、权限、失败路径或兼容测试。
