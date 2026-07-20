# 插件制品审查与发布系统 - 需求文档

## 1. 背景与目标

当前市场以 GitHub 仓库元数据和插件级 `pending/listed/unlisted` 状态为核心，无法区分线上稳定版本与待审更新，也缺少插件包托管、制品级审查、可靠发布和版本审计能力。

本项目将市场升级为：

> 插件源 + 插件包托管 + 自动审查 + 人工复核 + 版本发布工作台

AstrBot 侧继续消费兼容字段：

- `repo`：始终保留，代表插件身份、源码仓库和 GitHub 直连来源。
- `download_url`：仅指向已通过审查并成功发布的插件包。

本需求覆盖整体目标以及 P0、P1 的首轮落地。后续工作台、隔离运行时、LLM 审查、版本 diff 和增强安全能力保留明确扩展边界。

## 2. 核心原则

1. 插件身份、候选制品、审查证据和线上发布版本必须分离。
2. 已审查的字节必须与最终发布的字节完全一致。
3. 仓库最新版本和已过审 CDN 版本必须分别记录，未通过审查的新版本不得修改当前已发布 artifact 或覆盖旧 CDN 对象。
4. `repo` 永远保留；仓库版本与当前已发布 artifact 版本不一致时，插件源必须保留 GitHub 直连并将 `download_url` 置空。
5. GitHub 分支或标签必须解析为不可变 commit SHA 后再生成制品。
6. LLM 只提供审查建议，不承担最终安全背书。
7. 任何不可信代码执行均不得发生在主 API 进程中。
8. 严重风险下架必须同时覆盖插件源可见性和 CDN 下载可用性。
9. 邮件不得包含源码、证据片段或完整审查结果，只通知状态和站内链接。
10. 旧版市场插件源和已上架插件必须保持向后兼容。

## 3. 范围与分期

### 3.1 P0：基础设施与领域边界

- 建立版本化数据库迁移机制。
- 建立不可变 artifact、artifact 文件清单、审查运行、发现、决策、持久任务和 outbox 数据能力。
- 为插件增加当前已发布 artifact 指针，但不破坏现有插件状态和旧数据。
- 建立私有隔离区和公开发布区的存储抽象，支持本地开发存储和生产对象存储。
- 建立独立 worker 进程和可恢复、可重试、幂等的任务领取机制。
- GitHub 元数据同步继续更新 `repo_version`，但不得修改 artifact 版本、已发布对象或 artifact 的 `download_url`。
- 将 artifact 功能置于独立功能开关下；基础设施不完整时不得部分启用。

### 3.2 P1：最小发布闭环

- 支持上传 ZIP 创建 artifact。
- 支持从公开 GitHub 仓库和固定 commit 创建 artifact。
- 对完整 artifact 执行预检和基础静态扫描。
- 保存文件清单、审查运行和结构化 findings。
- 为作者提供 artifact 状态、失败原因和版本历史查询。
- 为管理员提供最小待审列表、详情、批准和拒绝操作。
- 批准后发布不可变插件包，校验摘要并原子切换当前 artifact 指针。
- `/plugins.json` 和等价端点的 `version` 跟随有效仓库 metadata；仅当该版本已有 published artifact 时输出对应 `download_url`。
- 拒绝、处理失败或发布失败均不得覆盖旧稳定版本。
- 创建站内通知和邮件 outbox；邮件只包含状态、简短原因和工作台链接。

### 3.3 后续阶段

- P2：完整 `/plugin-workbench`、文件浏览、审查报告和角色化工作区。
- P3：隔离 AstrBot runtime smoke test、AI 分类、LLM 包级与文件级审查。
- P4：artifact diff、入口依赖链、行级评论、有限自动批准和完整审查历史。
- P5：YARA、依赖风险、SBOM、强化沙箱网络策略、指标告警和应急演练。

### 3.4 P0/P1 明确不包含

- 在 API 进程中安装或运行 AstrBot、插件或其依赖。
- LLM 自动审查和 AI 自动分类。
- 自动批准新插件或新版本。
- 版本 diff、行级评论和评论线程。
- 私有 GitHub 仓库、monorepo 子目录、Git submodule 和 Git LFS 内容拉取。
- 将任意第三方 URL 作为源码下载地址。

## Core Features

### FR-001 插件稳定身份

- 插件必须继续以唯一插件名和规范化 GitHub `repo` 标识。
- 插件记录负责所有权、展示、上架状态、仓库最新版本 `repo_version` 和当前已发布 artifact 指针。
- 新 artifact 不得直接覆盖插件稳定版本字段。

### FR-002 不可变 artifact

- 每次上传或 GitHub 提交必须创建新的 artifact，重新提交不得原地修改旧 artifact。
- artifact 必须记录来源类型、来源仓库、请求 ref、解析后的 commit、原始包 SHA-256、文件树摘要、大小、版本和提交者。
- 完全相同的来源摘要应支持幂等返回，避免重复扫描。
- 已发布版本禁止用不同内容覆盖同一公开对象。

### FR-003 来源获取

- ZIP 上传必须流式处理，不得将整个文件一次性读入内存。
- GitHub 来源仅允许 `github.com` 公开仓库，并继续执行现有仓库所有者校验。
- 未指定 ref 时，系统应将默认分支当前 HEAD 解析为 commit SHA。
- 后续扫描和发布只能使用隔离区内保存的固定制品，不能重新从 GitHub 下载。

### FR-004 隔离存储

- 上传内容先写入非公开隔离区。
- 隔离对象不得通过公开 URL 或静态文件路由访问。
- 只有 artifact 所有者和管理员能够通过授权 API 查看允许展示的文件内容和审查结果。
- 公开 CDN URL 必须采用以下固定结构：

  ```text
  https://{cdn_domain}/{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip
  ```

- `author_id` 必须使用稳定、可公开且不会随昵称变化的作者 ID。
- `repo_name` 必须从规范化 GitHub repo 提取，不得直接信任客户端传入路径。
- `version` 和 `plugin_name` 必须经过路径安全校验，只允许受控字符，不得包含路径分隔符或编码后的穿越片段。
- `suffix` 必须是 8 至 12 位加密安全随机短串，在 artifact 创建时生成一次并永久保存，不得在重试发布时重新生成。
- 发布对象 key 必须全局唯一；对象存储必须使用“仅在对象不存在时创建”的语义，禁止覆盖已有对象。
- CDN 域名属于部署配置，不得由 artifact 提交者指定。

### FR-005 预检

以下条件属于硬失败，不进入人工批准流程：

- 压缩包超过配置大小。
- 解压后总大小、单文件大小、文件数量、目录深度或压缩比超过限制。
- 路径穿越、绝对路径、符号链接、重复规范化路径或大小写冲突路径。
- 加密 ZIP、损坏 ZIP、嵌套压缩包或无法安全读取的条目。
- 缺少根目录 `main.py`。
- 缺少根目录 `metadata.yaml` 或 `metadata.yml`。
- metadata 不是普通映射、存在不允许的 YAML 构造或超过大小限制。
- metadata 缺少 `name`、`desc`、`version`、`author`、`repo` 等必填字段。
- 插件名不以 `astrbot_plugin_` 开头，或与目标插件不一致。
- metadata repo 与注册 repo 不一致或提交者不具备仓库所有权。
- 版本无法按 AstrBot 兼容规则解析；应兼容 `v1.1` 和 `v1.1.1`。
- 存在 Git LFS 指针、submodule 占位或 P1 不支持的原生可执行制品。

预检失败必须保存结构化失败原因，并按保留策略清理隔离对象。

### FR-006 基础静态扫描

- 所有 Python 文本文件必须完成语法解析。
- 扫描必须覆盖完整 artifact，而不是只扫描变更文件。
- 基础规则至少识别动态执行、Shell/子进程、敏感路径访问、凭据读取、任意下载执行、启动阶段副作用、代码混淆和危险依赖声明。
- 静态命中必须保存规则 ID、严重程度、文件、行号、消息和有限证据片段。
- 扫描器异常与“扫描通过”必须严格区分；扫描异常不得自动进入批准状态。

### FR-007 审查运行与 findings

- 每个预检和静态扫描阶段必须创建独立 review run。
- review run 必须记录状态、尝试次数、规则版本、开始/结束时间、摘要和错误信息。
- findings 必须支持稳定指纹，避免重试产生重复结果。
- P1 不允许 LLM 结果参与最终决策。

### FR-008 人工决策

- 管理员能够对 `pending_review` artifact 执行批准或拒绝。
- 决策接口必须是显式领域动作，不能允许客户端任意 PATCH 状态。
- 决策必须记录操作者、理由、原状态、目标状态、策略版本和时间。
- 重复请求和并发决策必须幂等；已终态 artifact 不得被第二次批准。
- 作者不得批准自己的 artifact。

### FR-009 可靠发布

- 批准后，publisher 必须将隔离区中的原始过审对象复制到公开发布区。
- publisher 必须按照 FR-004 的作者 ID、仓库名、版本和固定随机短串生成发布 key。
- 发布后必须通过对象元数据或重新读取确认大小和 SHA-256。
- 只有对象验证成功后，数据库事务才能把 artifact 标记为 published 并切换插件的当前 artifact 指针。
- artifact 必须持久保存 `published_key` 和最终 `download_url`，相同 artifact 的发布重试必须得到相同 URL。
- 数据库事务失败留下的孤儿对象必须能够被清理。
- 发布失败必须保留 artifact、审查决定和旧稳定版本，并允许管理员重试发布。

### FR-010 插件源输出

- `/plugins.json`、`/plugins-md5.json` 和 `/v1/astrbot/*` 必须保持现有响应结构兼容。
- `repo` 始终来自插件稳定记录。
- `repo_version` 来自安全解析后的仓库 metadata，并随 GitHub 元数据同步更新。
- 公开 feed 的 `version` 优先使用 `repo_version`；没有有效仓库版本时才回退到当前 published artifact 或旧插件版本。
- 只有当前 published artifact 的版本与 feed `version` 一致时，才能输出该 artifact 的 `download_url`。
- 仓库已经发布新版本但该版本尚未过审、被拒绝或发布失败时，feed 保持新的仓库版本和 `repo`，但 `download_url` 必须为空。
- 市场 API 和工作台应分别展示 `repo_version` 与 `published_version`，不得用一个字段掩盖版本差异。
- 没有当前 artifact 的旧插件继续输出原有仓库信息；其既有兼容 `download_url` 仅在能够确认版本一致时保留，否则置空。
- 待审、拒绝或发布失败 artifact 不得出现在公开 feed 中。

### FR-011 更新与稳定版本保护

- 新版本提交后，当前 listed 插件必须继续保持可见和可下载。
- GitHub 元数据同步发现新版本后，只更新 `repo_version`，不得修改当前 artifact 指针或已发布 artifact。
- 当 `repo_version` 高于或不同于 `published_version` 时，插件源继续提供 GitHub 直连，但不得继续提供旧版本 CDN URL。
- 新版本预检失败、扫描失败、被拒绝或发布失败时，当前 artifact 指针和旧 CDN 对象不得变化；旧对象仅作为稳定历史保留，不在新版本 feed 中冒充最新包。
- 新版本发布成功后，指针切换和 artifact 发布状态必须位于同一数据库事务中。
- 新 artifact 发布成功且版本与 `repo_version` 一致后，feed 才恢复输出该 artifact 的 `download_url`。
- GitHub 定时元数据同步不得修改 artifact 权威的版本和下载地址，只能更新仓库侧版本及非发布元数据。

### FR-012 下架与撤回

- 普通下架应从插件源移除插件，但保留审计记录。
- 严重安全风险撤回必须使公开对象不可下载，并触发 CDN 清理或源站拒绝。
- 新候选 artifact 出现 critical finding 时，P1 默认拒绝该候选；除非证据同时影响当前已发布 artifact，否则不得自动撤回稳定版本。
- 所有撤回必须记录操作者和原因。

### FR-013 任务与 outbox

- artifact 处理任务必须持久化，API 或 worker 重启后能够恢复。
- worker 必须通过租约和幂等键领取任务，支持超时回收和有限重试。
- PostgreSQL 是任务状态权威来源；Redis 不得成为唯一任务记录。
- 邮件、站内通知、发布后处理和缓存清理通过 outbox 可靠投递。

### FR-014 通知

- 作者必须收到预检失败、待人工审查、批准、拒绝和发布失败通知。
- 管理员必须收到新的待审 artifact 和严重风险通知。
- 邮件正文只包含插件名、版本、状态、简短原因和站内链接。
- 邮件和通知不得包含源码、finding evidence、运行日志或访问隔离对象的直接地址。
- 重试不得产生重复通知。

### FR-015 分类字段归属

- 插件记录保存当前有效 `category` 和 `category_source`。
- artifact 可以保存用户提交分类和后续 AI 建议，但 P1 不生成 AI 分类。
- artifact 审查不得在发布前覆盖当前插件分类。

### FR-016 权限

- 作者只能创建和查看自己插件的 artifact。
- 管理员可查看审查队列、artifact 文件、findings 和决策历史。
- 只有具备插件审核权限的管理员可批准、拒绝和重试发布。
- 核心管理员继续管理基础设施和安全策略配置。
- 前端路由守卫只改善用户体验，后端权限检查始终是安全边界。

### FR-017 最小管理界面

- 现有管理员插件页面应提供 artifact 待审入口，或新增最小审查页面。
- 页面至少展示插件、版本、来源、提交者、预检/静态扫描状态、最高风险和提交时间。
- 管理员能够查看结构化失败原因并执行批准或拒绝。
- 作者在个人中心能够进入 artifact 列表并查看当前状态。
- P1 不要求代码浏览器、diff 或行级评论。

### FR-018 旧数据与迁移

- 数据库迁移不得删除或重写现有 listed 插件、评论、点赞、通知和用户数据。
- 现有 listed 插件允许 `current_artifact_id` 为空并继续通过 GitHub 直连工作。
- 现有 pending submission 不得被自动视为已审查 artifact；应继续旧流程或要求重新提交。
- 新功能关闭时，现有市场浏览、登录、评论、点赞和管理能力必须保持工作。

### FR-019 配置

- P0/P1 必须提供 artifact 功能开关、上传限制、解压限制、保留时间、隔离存储和发布存储配置。
- 缺少 PostgreSQL、任务 worker 或发布存储配置时，artifact 提交必须 fail closed。
- `PLUGIN_AUTO_APPROVE_ENABLED` 不得绕过 artifact 审查；启用新流水线后应忽略或废弃其旧语义。
- 敏感存储凭据不得通过公开站点配置或日志返回。

### FR-020 API 合约

- API 至少覆盖 ZIP/GitHub artifact 创建、作者 artifact 列表与详情、管理员队列、review runs、findings、批准、拒绝和发布重试。
- 列表接口必须支持分页和状态过滤。
- 文件内容接口必须使用 artifact file ID，不得直接接受未经校验的文件系统路径。
- OpenAPI 角色过滤和手写 API 文档必须同步更新。

## User Stories

- 作为插件作者，我希望上传 ZIP 或提交 GitHub ref，以便生成一个不会影响线上版本的待审 artifact。
- 作为插件作者，我希望看到预检和静态扫描的结构化失败原因，以便修复后创建新 artifact。
- 作为插件作者，我希望未通过的新版本不会破坏当前稳定下载，以便现有用户继续使用旧版本。
- 作为管理员，我希望看到明确的待审队列和风险摘要，以便作出批准或拒绝决定。
- 作为管理员，我希望每次决定都有不可变审计记录，以便追踪误操作和安全事件。
- 作为运维人员，我希望 worker 重启后任务可以恢复，以便 API 和发布流程不会因进程故障丢失状态。
- 作为安全人员，我希望隔离对象和源码默认私有，以便未经批准的代码不会公开泄漏。
- 作为 AstrBot 用户，我希望插件源仍然包含 `repo`，并只在版本过审后得到 CDN `download_url`。
- 作为旧插件作者，我希望迁移后现有插件仍可通过 GitHub 直连，以便无需一次性重新提交全部插件。

## Acceptance Criteria

### P0 验收

- [ ] 数据库使用可重复、按版本执行的迁移，不再仅依赖不断增长的 `_ensure_schema()`。
- [ ] 新增 artifact 领域表、约束和必要索引，且旧数据无损。
- [ ] 插件记录能够指向当前 artifact；旧插件允许该指针为空。
- [ ] 存储接口支持私有隔离对象、固定 CDN 路径、公开发布对象、条件创建、摘要校验和删除。
- [ ] 独立 worker 能领取、续租、完成、失败和重试持久任务。
- [ ] API 重启不会丢失 queued/running artifact，过期租约可回收。
- [ ] GitHub 同步能够更新 `repo_version`，但不能覆盖 artifact 版本、当前 artifact 指针和已发布 `download_url`。
- [ ] 市场能够同时表达仓库最新版本和 CDN 已过审版本。
- [ ] 新功能关闭时，现有测试和插件源行为保持兼容。

### P1 验收

- [ ] 合法 ZIP 和公开 GitHub commit 均可创建隔离 artifact。
- [ ] GitHub 分支在提交后移动，不会改变已创建 artifact 的摘要或内容。
- [ ] 恶意路径、zip bomb、缺 metadata、缺 main.py、非法版本等样本被预检拒绝。
- [ ] 所有 Python 文件完成语法和基础危险行为扫描，findings 可查询。
- [ ] 预检或扫描异常不会被误判为通过。
- [ ] 管理员可以批准或拒绝 `pending_review` artifact，普通用户不能调用决策接口。
- [ ] 两名管理员并发批准时只产生一次发布和一次指针切换。
- [ ] 发布对象 SHA-256 与已审查 artifact 完全一致。
- [ ] CDN URL 符合 `{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`，且发布重试不会改变 URL。
- [ ] 不同 artifact 即使插件名和版本相同，也不会覆盖同一 CDN 对象。
- [ ] 仓库版本更新后，`/plugins.json.version` 跟随有效 metadata 更新。
- [ ] `repo_version` 与 `published_version` 不一致时，`/plugins.json` 保留 `repo` 且 `download_url` 为空。
- [ ] 发布失败时当前 artifact、`published_version` 和旧 CDN 对象保持不变，feed 不暴露旧 URL。
- [ ] 被拒绝的新版本不会覆盖稳定 artifact，但 GitHub 直连仍可使用。
- [ ] 已批准并发布的新版本出现在插件源，且 `repo` 仍然存在。
- [ ] 旧 listed 插件在没有 artifact 时继续正常出现在插件源。
- [ ] 作者不能读取其他作者的隔离文件或审查详情。
- [ ] 邮件和站内通知不包含源码、证据片段或隔离对象地址。
- [ ] 后端、前端构建、API 测试和新增前端行为测试全部通过。

## Non-functional Requirements

### 安全

- 所有上传和 GitHub 响应按不可信输入处理。
- 不使用 `ZipFile.extractall()` 直接解压未验证归档。
- metadata 使用安全 YAML 解析并限制大小、深度和别名。
- 文件路径在 Unicode 规范化后进行安全检查。
- 隔离存储凭据、发布凭据和数据库凭据按最小权限分离。
- API 日志、任务错误和通知必须过滤访问令牌、存储密钥和源码内容。
- P1 不执行插件代码、requirements 构建脚本或 import。

### 可靠性与一致性

- 任务处理采用至少一次语义，所有步骤必须幂等。
- artifact、review run、decision 和发布指针使用数据库事务维护一致性。
- 对象存储与数据库之间允许产生可清理孤儿对象，但不允许产生指向不存在对象的稳定指针。
- 发布和撤回操作必须提供重试与审计记录。

### 性能与容量

- 默认单包压缩大小上限不高于 32 MiB，并允许核心管理员下调。
- 上传使用流式 I/O；文件清单和静态扫描由 worker 执行。
- 队列、artifact、run 和 finding 列表必须分页。
- 单个 artifact 的失败不得阻塞其他 artifact 处理。

### 可观测性

- 日志必须包含 `artifact_id`、`run_id`、`job_id` 和阶段名称。
- 至少记录队列深度、各阶段耗时、失败数、发布失败和待审数量。
- 不在指标标签中使用插件源码、文件路径全文或用户敏感信息。

### 兼容性

- 保持现有 `/plugins.json` 字段和 GitHub 直连语义。
- 保持现有 `market_plugins.status` 行为，直到独立迁移明确替代它。
- 保持现有内存存储开发模式；artifact 功能可在无生产基础设施时关闭。
- Python 使用 3.11+，前端继续使用 Vue 3、Composition API、TypeScript、Pinia 和 Vue Router。

### 可维护性

- artifact 路由、服务、存储、扫描器和 worker 不得继续集中堆入 `main.py` 或现有大型插件 store。
- 新模块必须有单一职责、类型注解和针对边界条件的测试。
- 数据库迁移、规则版本和发布策略必须可审计、可回滚或可前向修复。

## 4. 成功判定

P0/P1 完成后，市场应具备一个不执行插件代码、不依赖 LLM、但能够可靠接收、隔离、预检、静态扫描、人工批准并发布不可变插件包的最小闭环。任何失败路径都不得破坏现有稳定插件源。
