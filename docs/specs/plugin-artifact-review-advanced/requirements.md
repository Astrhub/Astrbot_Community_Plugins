# 插件制品高级审查系统 - 需求文档

## 1. 背景与目标

P0/P1 已完成插件身份登记、ZIP/GitHub artifact 隔离、确定性预检、基础静态扫描、人工批准、不可变 CDN 发布、版本门控、撤回和最小工作台。本阶段在不破坏这些行为的前提下完成原计划剩余能力：

- AstrBot 临时容器 runtime smoke test；
- AI 分类补全和结构化 LLM 包级/文件级审查；
- 版本 diff、变更文件与入口依赖链审查；
- 受权限保护的代码浏览、行级评论和审查历史；
- critical 风险与当前稳定版本的关联处置；
- ClamAV/YARA、依赖风险、沙箱网络策略和版本化审查策略审计。

最终形态仍是“插件源 + 插件包托管 + 自动审查 + 人工复核 + 版本发布工作台”。AstrBot 消费契约继续保持 `repo + download_url`，其中 `repo` 永远保留，`download_url` 只指向当前仓库版本对应的已过审不可变包。

## 2. 核心原则

1. **API 不执行插件代码**：FastAPI 进程只负责鉴权、查询、状态编排和任务入队。
2. **普通 artifact worker 不执行插件代码**：预检、静态分析和发布 worker 不得 import 插件或安装其依赖。
3. **runtime 必须使用一次性隔离容器**：requirements 构建、安装、import、启动和 handler/tool 注册只能发生在受限临时容器内，任务结束后销毁。
4. **LLM 不是安全边界**：LLM 只能产生结构化建议和 findings，不能单独批准、撤回或降低确定性扫描风险。
5. **稳定版本不可被候选版本污染**：候选 artifact 失败、超时或被拒绝时，不修改当前 artifact 指针和旧 CDN 对象。
6. **差异审查必须可证明完整**：无法可靠构建 diff、入口依赖链或 base artifact 时，自动退化为全量确定性审查，不得静默少审。
7. **所有决定可追溯**：运行器镜像、AstrBot 版本、模型、提示词版本、规则集、病毒库、YARA 规则、依赖数据库和策略版本都必须留下快照标识。
8. **严重风险先隔离公开面**：确认影响当前稳定版本的 critical 风险时，先从插件源隐藏，再异步删除公开对象；失败保持隐藏并可重试。
9. **最小权限**：作者只访问自己的 artifact；管理员可审查；仅核心管理员可修改和激活全局安全策略。
10. **不以“扫描通过”宣称绝对安全**：UI、API、邮件和文档必须使用“未发现已知阻断项/审查通过”等有限表述。

## 3. 范围与分期

### 3.1 P2：隔离 runtime 与 AI 自动审查

- AI 分类建议；
- AstrBot 精确目标版本的临时容器 smoke test；
- LLM 包级筛选、文件级审查和汇总；
- 统一结构化 findings；
- 基于确定性门禁的自动拒绝、可配置自动通过或人工复核路由。

### 3.2 P3：更新、diff 与完整工作台

- base/current artifact 文件级 diff；
- 变更文件、静态命中文件和入口依赖闭包选择；
- 受限源码读取、目录浏览和文本 diff；
- 行级评论、回复、解决状态、要求修改和重新提交；
- 审查历史和版本时间线；
- critical finding 与当前稳定版本关联分析和下架流程。

### 3.3 P4：增强安全与策略治理

- ClamAV 和 YARA；
- Python 依赖风险与许可证/来源风险；
- runtime 安装阶段和执行阶段的独立网络策略；
- 版本化审查策略、草稿/激活/回滚和审计日志；
- 安全工具不可用时的 fail-closed/人工复核策略。

### 3.4 明确不包含

- 不保证发现所有恶意代码或供应链风险；
- 不支持在 API 容器、宿主机或普通 worker 直接运行插件；
- 不把 LLM 文本当作可执行命令；
- 不向浏览器返回二进制文件正文、隔离对象 key、容器原始日志或密钥；
- 不自动执行插件自带测试脚本；
- 不允许作者修改、删除或覆盖历史 findings、decisions、policy snapshots 和管理员评论；
- 不为外部 SMTP/Cloudflare 邮件承诺跨系统 exactly-once。

## 4. 角色与用户故事

### 4.1 插件作者

- 作为作者，我希望看到每个版本的预检、静态、runtime、依赖和 LLM 结果，以便定位失败原因。
- 作为作者，我希望浏览自己提交包中的文本文件和 diff，以便在相同上下文中理解评论。
- 作为作者，我希望回复管理员评论、标记已处理并重新提交新 artifact，同时保留旧版本历史。
- 作为作者，我希望仓库版本变化后旧 CDN 包仍被保留但不冒充新版本，以便用户仍可选择 GitHub 直连。
- 作为作者，我希望 AI 分类只作为建议，不覆盖我或管理员明确选择的分类。

### 4.2 管理员

- 作为管理员，我希望按风险、阶段、工具状态和等待时长筛选队列，以便优先处理高风险版本。
- 作为管理员，我希望查看文件树、文本内容、diff、结构化 findings 和 runtime 注册结果，以便作出人工决定。
- 作为管理员，我希望在具体文件行上评论、回复、解决或重新打开线程，并可“要求修改”。
- 作为管理员，我希望只审查变更文件和可靠入口依赖闭包，同时明确知道何时系统退化为全量审查。
- 作为管理员，我希望确认风险影响当前稳定 artifact 后立即下架，并通知作者和其他管理员。

### 4.3 核心管理员与运维

- 作为核心管理员，我希望以版本化策略配置阈值、强制阶段、自动路由和工具开关，并审计每次变更。
- 作为运维，我希望知道 runtime runner、病毒库、YARA、依赖数据库和 LLM provider 是否就绪，但健康接口不泄露凭据。
- 作为运维，我希望容器超时、崩溃、worker 重启和外部服务故障都能恢复或进入明确终态。

### 4.4 AstrBot 用户

- 作为 AstrBot 用户，我希望插件源只返回与展示版本完全一致的已审查 CDN 包，同时始终保留 GitHub repo。
- 作为 AstrBot 用户，我希望重大风险版本从插件源和 CDN 公开面撤回，而未过审候选不会覆盖稳定包。

## 5. 功能需求

### FR-201 AI 分类建议

- 输入只包含 metadata、README 摘要、文件树、现有分类和允许的分类枚举。
- 输出必须是 JSON：`suggested_category`、`confidence`、`reason`、`model`、`prompt_version`。
- 分类优先级为 `reviewer > user > ai > default`。
- AI 不得覆盖 reviewer/user 明确分类；仅在分类为空或 `other` 且策略允许、置信度达标时应用 AI 分类。
- 原始模型响应只进入受限审计存储；公开 API 返回规范化字段，不返回隐藏推理内容。

### FR-202 runtime 任务与隔离执行器

- 新增独立 runtime job，不在 API 或普通 artifact worker 进程执行。
- 执行器必须使用一次性容器、非 root 用户、只读根文件系统、临时可写目录、capabilities 全移除、`no-new-privileges`、CPU/内存/PID/时间限制。
- 容器输入只能是指定 artifact ZIP、固定 runner 程序、精确 AstrBot 版本和策略快照；不得传入站点数据库、Redis、对象存储或 LLM 凭据。
- 容器输出只能通过受限 JSON 结果文件/标准输出协议返回；输出大小和字段必须校验。
- 运行结束、超时、取消或崩溃后都必须销毁容器和临时卷。
- 容器运行时不可用、隔离参数缺失或结果无法验证时，runtime run 失败，不得被视为通过。

### FR-203 AstrBot smoke test

按以下逻辑执行并分别记录结果：

1. 使用策略指定的精确版本安装 `astrbot==目标版本`；
2. 校验 artifact SHA-256 后安全解压；
3. 在隔离环境安装 `requirements.txt`，记录解析、构建和依赖冲突；
4. 在安装前后生成 AstrBot 核心依赖快照并比较破坏性变化；
5. 加载插件入口但不连接真实消息平台、数据库或生产 provider；
6. 记录 import、插件实例化、startup、handler、hook 和 tool 注册结果；
7. 在受控生命周期内调用清理/terminate；
8. 输出结构化阶段、耗时、退出码、安全截断日志和 findings。

runtime 必须识别至少：依赖冲突、导入失败、metadata/入口不匹配、启动异常、handler/hook/tool 注册失败和 requirements 破坏 AstrBot 依赖。

### FR-204 runtime 版本选择

- 策略保存一个或多个允许的精确 AstrBot 版本，禁止无界 `latest`。
- metadata `astrbot_version` 存在时必须与测试版本集合求交；无法求交时产生阻断 finding。
- 每个 runtime run 保存 AstrBot 版本、Python 版本、runner 镜像 digest、平台和依赖快照 hash。
- 仓库版本同步不得改变已完成 run 的版本快照。

### FR-205 LLM 包级审查

- 包级输入仅包含：规范化文件树、metadata、requirements、README 限长摘要、静态/runtime/恶意软件/依赖 findings、base diff 摘要和策略说明。
- 不把整个代码包或全部源码一次性发送给模型。
- 输出 JSON 必须通过版本化 schema 校验，至少包含风险摘要、建议审查文件、建议类别、置信度和理由。
- 模型超时、限流、JSON 无效或 schema 不匹配时 run 失败并按策略进入人工复核，不得当作无风险。

### FR-206 LLM 文件选择与文件级审查

- 候选文件集合是以下并集：入口文件、入口本地依赖闭包、变更文件、确定性命中文件、包级模型建议文件和策略强制文件。
- 只允许选择 artifact_files 中已知的受限文本文件；拒绝路径穿越、二进制、超限文件和不存在路径。
- 文件按风险和 token 预算排序；超出预算时必须记录未审文件及原因，不能声称全量完成。
- 每个文件级 run 保存文件 SHA、模型、prompt version、输入摘要 hash 和 JSON 结果。
- 证据必须引用 artifact 文件路径和有效行号；服务端重新读取对应行验证后才写 finding。

### FR-207 LLM 汇总与安全边界

- 汇总只消费已规范化的 package/file 结果和确定性 findings，不接受模型自由修改历史结果。
- LLM 可以提高风险、建议人工复核或建议分类；不得降低 ClamAV/YARA、预检、静态、依赖或 runtime 的严重度。
- LLM 不能直接创建 approve/revoke decision，也不能生成要在宿主机执行的命令。
- UI 必须标识模型结果为“自动审查建议”。

### FR-208 自动路由

- **直接拒绝**：预检硬失败、恶意软件确认、策略定义的阻断依赖/运行时错误或其他确定性 critical。
- **自动通过**：默认关闭；开启后也只能在所有强制确定性阶段成功、无 open medium+ finding、runtime 成功、工具无降级且版本完全一致时，由策略引擎创建 `auto_approve` decision。
- LLM 的“安全”结论不能单独触发自动通过；LLM medium+ 建议必须阻止自动通过并进入人工复核。
- **人工复核**：存在非阻断 finding、工具降级、预算未覆盖、模型失败、diff 不完整或策略明确要求时进入。
- 所有自动路由保存 policy version、输入 run IDs、理由和幂等键。

### FR-209 artifact diff

- 新 artifact 默认与提交时记录的 `base_artifact_id` 比较；base 不存在或不属于同一插件时拒绝比较并退化全量审查。
- 通过路径和 SHA 识别 added、deleted、modified、unchanged；可选基于相同 SHA 识别 rename，但不得把模糊相似度当成确定 rename。
- 文本 diff 使用确定性 unified hunks，保存旧/新行范围和受限上下文；二进制只显示摘要、大小和状态。
- metadata、requirements、入口文件和安全策略强制文件即使 SHA 未变，也可进入强制复核集合。
- diff 结果绑定两端 artifact/tree hash，任何一端变化后必须重算。

### FR-210 入口依赖图与增量审查范围

- 使用 Python AST 构建插件内部 import 图，不执行 import。
- 从 `main.py` 和已识别入口出发计算本地依赖闭包；动态 import、路径操作、语法错误或未知入口必须标记图不完整。
- 增量范围至少包含变更文件、其反向依赖、入口到变更文件路径、确定性命中文件和强制文件。
- 删除文件必须审查其原反向依赖；requirements/metadata 变化触发完整 runtime 和依赖审查。
- 图不完整时 LLM 可以继续给建议，但确定性阶段和人工 UI 必须显示“增量范围不完整”，并按策略全量或人工复核。

### FR-211 受限文件浏览 API

- 只有 artifact 作者、管理员和核心管理员可列文件树；只有同样角色可读取文本内容/diff。
- API 通过 `artifact_id + file_id/path` 定位已登记 manifest，不接受任意对象 key 或文件系统路径。
- 仅返回 UTF-8 受限文本，强制单文件/总响应大小、行数和分页限制；二进制只返回 metadata。
- 响应使用纯文本/JSON，不执行 Markdown/HTML；前端代码浏览使用转义文本，不使用未清洗 `v-html`。
- 每次内容读取可记录安全审计事件；不得返回容器原始日志、隐藏 prompt 或隔离下载 URL。

### FR-212 行级评论与线程

- 评论绑定 artifact、文件 SHA/path、行范围和 side（base/current）；行范围必须在对应文件或 diff hunk 内。
- 保存 reviewer user ID 和 nickname 快照、正文、创建/更新时间、线程父 ID、resolved/reopened 状态。
- 管理员可创建、编辑自己的未锁定评论、解决/重开线程；作者可回复和标记已处理，但不能修改管理员原文。
- artifact 作出最终决定后历史线程只读；后续 artifact 可引用旧线程但不能迁移或覆盖原记录。
- 评论正文按纯文本处理、限长并防止提及/链接造成注入。

### FR-213 要求修改与重新提交

- 新增 `request_changes` decision，理由必填；该 artifact 不发布 CDN。
- 作者通过新提交生成新 artifact；禁止在原 ZIP 上原地替换内容。
- 新 artifact 记录 `base_artifact_id` 和可选 `supersedes_artifact_id`，工作台展示关联线程和处理状态。
- 重新提交仍执行所有策略要求的确定性阶段，不能因旧评论已解决而跳过安全门禁。

### FR-214 审查历史

- 作者和管理员可查看版本时间线、各阶段 run、findings 状态变化、评论线程、decisions、发布/撤回记录和策略快照。
- 历史数据按服务端时间排序并分页；所有重要状态变化带 actor、source 和 idempotency key。
- 用户删除后保留必要 nickname/角色快照，不保留不需要的凭据或个人信息。

### FR-215 critical 风险与稳定版本关联

- 候选 artifact 的 critical finding 默认只拒绝候选，不自动撤回稳定版本。
- 系统通过稳定 artifact 的同路径/同 SHA、依赖图、规则 fingerprint 或管理员明确确认建立影响关联。
- 只有确定性证据命中当前 artifact，或管理员确认 `affects_current_release=true` 后，才创建紧急撤回任务。
- 紧急撤回必须记录 finding、关联证据、操作者/策略、原因和通知；先隐藏 feed，再撤回对象。
- LLM 只能建议“可能影响稳定版本”，不能单独触发撤回。

### FR-216 ClamAV

- 扫描 quarantine 原始 ZIP，并可按策略扫描受控解包流；不在 API 进程加载病毒库。
- 保存引擎版本、病毒库版本/时间、扫描目标 SHA、签名名和结果。
- confirmed infection 产生确定性 critical finding 并直接拒绝候选。
- scanner 不可用、病毒库过期或结果异常时按策略 fail closed 或强制人工复核，不能报告 clean。

### FR-217 YARA

- YARA 规则包必须版本化并保存内容 hash、来源和激活审计。
- 扫描有每文件/总字节/时间限制；规则超时或错误不得中断其他安全结果。
- finding 保存 rule namespace/name、tags、文件路径、受限匹配位置，不返回敏感大段内容。
- 规则严重度由受控映射决定，作者不能提供或覆盖服务端 YARA 规则。

### FR-218 依赖风险

- 解析 requirements 的规范名称、版本约束、marker、extra 和来源；保留直接 URL/VCS/local/editable 的既有阻断规则。
- 在隔离环境解析最终安装依赖图并生成 CycloneDX 或等价结构化 SBOM。
- 使用固定版本的漏洞数据库/服务查询已知漏洞，保存 advisory ID、受影响范围、修复版本、来源和数据库时间。
- 依赖混淆、无 hash 直链、撤回版本、许可证策略和 AstrBot 核心依赖降级可产生 finding。
- 外部漏洞服务不可用时显示数据新鲜度并按策略降级，不能把“未查询”显示为“无漏洞”。

### FR-219 沙箱网络策略

- 至少区分 `install` 和 `smoke` 两个阶段：安装阶段只允许策略批准的包源/DNS/代理，smoke 阶段默认无网络。
- 容器不得访问宿主回环、Docker socket、云 metadata、私有网段、站点 PostgreSQL/Redis、对象存储管理端和 LLM 凭据。
- 网络策略必须由执行基础设施强制，不能只靠环境变量或插件自律。
- 例外规则由核心管理员版本化配置，绑定 artifact/run 并进入审计日志。
- 无法证明网络隔离生效时 runtime run 失败或强制人工复核。

### FR-220 版本化审查策略

- 策略包含：强制阶段、工具开关、阈值、自动路由、AstrBot/Python 版本、资源限制、网络 profile、LLM 模型/token 预算、分类阈值、病毒库新鲜度和依赖严重度映射。
- 策略以 immutable version 保存，支持 draft、active、retired；同一时刻只有一个 active 默认策略。
- 只有核心管理员可创建、校验、激活和回滚；普通管理员只能查看生效快照。
- 激活前执行 JSON schema 和跨字段验证；无效策略不得影响当前 active 版本。
- 每次变更保存 actor、时间、base version、diff、原因和 request ID；密钥只保存引用，不进入策略 JSON。
- artifact 在进入第一阶段时固定 policy version；后续全流程使用该快照，除非核心管理员显式迁移并留下 decision。

### FR-221 工具健康与可观测性

- `/health` 仅报告 runtime runner、LLM、ClamAV、YARA、依赖数据和策略是否 configured/ready/degraded，不返回 endpoint、token、bucket 或规则正文。
- 每个 run 记录 queued/start/end、attempt、worker、tool version、policy version、超时/错误码和安全摘要。
- 日志使用 artifact/job/run ID 关联；不得记录源码全文、requirements 中的凭据 URL、模型 key、容器环境或对象 key。
- 指标至少覆盖队列深度、阶段耗时、失败/超时、自动路由数量、人工等待时间和撤回结果。

### FR-222 通知

- 作者收到 runtime/恶意软件/依赖失败、要求修改、批准、拒绝、发布和撤回状态通知。
- 管理员收到待审、critical、工具降级和稳定版本关联风险通知。
- 邮件只包含插件、版本、状态、简短原因和工作台链接；源码、diff、evidence、容器日志和对象地址只在有权限的站内页面查看。
- 站内通知继续使用数据库 dedupe key；外部邮件保持 at-least-once 提醒语义。

### FR-223 API 与 OpenAPI 权限

- 新增文件树/内容、diff、评论、历史、策略和工具状态 API，全部使用 typed schema、分页和稳定错误码。
- public OpenAPI 不显示内部审查/策略接口；user/admin/core_admin 文档按现有角色过滤。
- 后端权限是唯一安全边界，前端隐藏按钮不能替代鉴权。
- 并发评论、决定、策略激活、自动路由和撤回使用行锁/唯一键或乐观版本控制。

### FR-224 工作台 UI

共享组件最终至少包括：

- `PluginReviewWorkspace`
- `PluginReviewSidebar`
- `PluginReviewHeader`
- `ReviewSummaryPanel`
- `ReviewDiffViewer`
- `ReviewFileBrowser`
- `ReviewCommentThread`
- `ReviewDecisionPanel`
- `ReviewHistoryTimeline`
- `ReviewPolicyPanel`（核心管理员）

作者视图提供提交、版本历史、阶段状态、结构化报告、文件/diff、评论和重新提交；管理员视图提供队列、风险、文件/diff、评论、通过/拒绝/要求修改/下架；核心管理员额外管理策略和工具健康。

## 6. 数据需求

在现有表基础上至少补充：

- `plugin_artifacts`：`policy_version_id`、`supersedes_artifact_id`、审查覆盖摘要；
- `artifact_files`：入口/依赖图标记和可选安全扫描摘要；
- `artifact_file_diffs`：base/current file、change type、hash、hunks key、统计；
- `artifact_dependency_edges`：artifact、source、target、edge type、confidence；
- `review_comments`：行级线程、side、SHA、resolved 状态和 actor snapshots；
- `review_comment_events`：回复、解决、重开和编辑审计；
- `review_runs`：tool version、policy version、input/output hash、coverage、container/image fields；
- `review_findings`：source、deterministic、affects_current_release、关联证据和状态 actor；
- `review_policies`、`review_policy_events`；
- 可选 `artifact_sboms` 或私有对象 key；
- `artifact_jobs` 新增 runtime、LLM、diff、malware、dependency 和 policy routing job 类型。

所有新增表必须有 FK、删除策略、唯一约束、状态约束和查询索引；大结果进入私有对象存储，数据库只保留结构化摘要和 key。

## 7. 验收标准

### 7.1 P2 runtime 与 AI

- [ ] API 和普通 worker 中不存在插件 import、requirements 安装或插件 subprocess 执行路径。
- [ ] runtime job 只能由独立执行器在一次性受限容器内运行，并在成功、失败、超时后销毁。
- [ ] smoke test 结构化记录 AstrBot/Python/镜像版本、依赖安装、import、startup、注册和清理结果。
- [ ] requirements 破坏 AstrBot 依赖、导入失败和 handler/tool 注册失败均有稳定 finding。
- [ ] runtime 容器无法访问站点数据库、Redis、Docker socket、云 metadata 和 smoke 阶段外网。
- [ ] AI 分类遵循 reviewer/user/ai 优先级并保存置信度和理由。
- [ ] 包级 LLM 不接收全量源码，文件级 LLM 只能读取服务端验证的选择集合。
- [ ] 所有 LLM 输出通过 JSON schema；无效、超时或超预算进入明确失败/人工复核。
- [ ] LLM finding 行号和 evidence 经服务端文件 SHA/行范围复核。
- [ ] LLM 不能降低确定性 finding 严重度，不能直接批准或撤回。
- [ ] 自动通过默认关闭；开启时仍满足全部确定性条件并产生可审计 `auto_approve` decision。

### 7.2 P3 diff 与工作台

- [ ] added/deleted/modified/unchanged/确定 rename 的文件 diff 与 tree hash 一致。
- [ ] Python import 图不执行代码，并正确计算入口依赖、反向依赖和删除文件影响。
- [ ] 动态 import、语法错误、base 缺失等情况显式退化全量或人工复核。
- [ ] requirements/metadata 变化始终触发完整 runtime/依赖审查。
- [ ] 作者不能读取其他作者的文件、diff、评论或历史；管理员权限测试通过。
- [ ] 文件内容 API 拒绝二进制、任意路径、超限响应和未登记对象 key。
- [ ] diff 行级评论校验 side、SHA 和行范围；并发回复/解决不丢事件。
- [ ] `request_changes` 不发布当前候选，重新提交生成新 artifact 并保留关联历史。
- [ ] 工作台提供文件浏览、diff、线程、决定和版本时间线，所有不可信文本以转义方式显示。
- [ ] 候选 critical 不会无证据撤回稳定版本；确认关联后先移出 feed 再撤回对象。
- [ ] 更新未通过时当前 artifact 指针和旧 CDN 对象不变，`repo` 始终保留。

### 7.3 P4 安全增强与策略

- [ ] EICAR/受控测试签名被 ClamAV 测试适配器识别并阻断，clean/unknown/error 三态不混淆。
- [ ] YARA 规则 hash、版本、超时和命中位置可审计，恶意规则不能无限消耗资源。
- [ ] requirements 与最终安装图生成结构化 SBOM，已知漏洞和修复版本可查询。
- [ ] 漏洞数据过期/不可用显示 degraded，不显示“无漏洞”。
- [ ] install/smoke 网络 profile 被基础设施强制，并有私网/metadata/站点服务阻断测试。
- [ ] 非核心管理员不能创建或激活策略；无效策略不能替换 active 版本。
- [ ] 并发激活只产生一个 active 版本，每个 artifact 固定完整 policy snapshot。
- [ ] 策略、病毒库、YARA、依赖数据和模型版本进入 run/decision 审计。

### 7.4 全局回归

- [ ] 现有 P0/P1 API、旧 listed 插件、GitHub metadata 同步和 AstrBot feed 契约不回归。
- [ ] CDN key 继续使用 `{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip`，重试不改变 URL。
- [ ] repo_version/published_version 不一致时 CDN URL 为空，但 `repo` 保留。
- [ ] 后端 Ruff、全量 pytest、前端 `vp check`/Vitest/build、OpenAPI、Compose 和部署配置全部通过。
- [ ] 至少在可用的容器运行时执行一次真实 smoke fixture；环境不具备时必须明确保留集成门禁，不能用 mock 冒充生产验证。
- [ ] 文档准确说明信任边界、运维前置、失败语义和已知限制。

## 8. 非功能需求

### 8.1 安全

- 所有 artifact、模型输出、scanner 输出和容器输出均视为不可信。
- 容器镜像使用 digest 固定并定期更新；禁止 privileged、host network、宿主根目录挂载和 API/数据库凭据注入。
- 对象 key、token、requirements credential、prompt 和内部日志必须脱敏。
- 源码 API 必须通过服务端所有权校验、响应限额和审计，浏览器不缓存敏感响应。
- LLM provider 应配置零保留/受控数据处理策略；发送前移除可能的凭据和超范围文件。

### 8.2 可靠性

- 所有新增 job 使用持久队列、租约、幂等键、有限重试和明确终态。
- 任一高级工具失败都不能误判为通过；稳定发布指针只能在最终发布事务修改。
- 容器/worker 崩溃后任务可回收，终态通知可幂等补发。
- policy activation、自动决定、评论事件和撤回具有并发保护。

### 8.3 性能与成本

- 文件树、diff 和历史分页；禁止一次返回整个大型 artifact。
- 通过 hash 复用未变文件的确定性结果，但复用必须绑定 ruleset/tool/policy 版本。
- LLM 有每 artifact/文件/token/费用预算和并发限制；超预算进入人工复核。
- 漏洞数据、工具镜像和 AstrBot 版本可缓存，但必须校验 digest 和新鲜度。

### 8.4 兼容性

- 保持现有 `/v1/*`、`/plugins.json`、`repo`、版本门控和旧插件兼容行为。
- 新功能由独立 feature flags/策略启用；未配置高级服务时 P0/P1 可以保持人工审查模式，但不得伪造高级阶段成功。
- 数据迁移向前兼容、可重复执行并有 checksum；部署升级先迁移再启用 worker。

### 8.5 可维护性与测试

- runtime、LLM、malware、dependency 和 policy 使用小型 Protocol/adapter，单元测试使用确定性 fake。
- 生产适配器必须有契约测试；安全关键路径需要失败注入、超时、恶意输入和并发测试。
- 前端继续使用 Vue 3 Composition API、typed Pinia、props down/events up 和行为测试。
- 不在测试中执行不受信任互联网插件；真实 smoke 使用仓库内最小安全 fixture。

## 9. 完成定义

只有当 P2-P4 验收项、全局回归和部署文档均有当前代码、测试或真实运行证据时，才能宣称全部功能完成。仅有接口、mock、占位 UI、未运行迁移或未验证容器配置不算完成；因本地环境缺失而跳过的真实容器/外部工具集成必须作为未完成门禁明确保留。
