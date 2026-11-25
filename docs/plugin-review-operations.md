# 插件审查运维手册

本文档面向负责部署、日常巡检与安全事故值守的运维人员。自动扫描、隔离运行和 LLM 审查只能提供风险信号，不能证明插件绝对安全；人工决定同样不能替代持续监控与事后处置。

## 服务与信任边界

| 进程 | 必需权限 | 明确禁止 |
| --- | --- | --- |
| FastAPI API | PostgreSQL、Redis、隔离上传存储、站点邮件配置 | Docker socket、扫描器端点、YARA 规则、漏洞库 Token、LLM API Key |
| `artifact-worker` | PostgreSQL、Redis、隔离/发布存储、runtime 结果只读、启用的审查工具 | Docker socket、直接执行插件代码 |
| `runtime-runner` | 最小权限 PostgreSQL 角色、artifact 只读、结果目录可写、独立 rootless 容器引擎 | Redis、站点/OAuth/邮件/LLM/对象存储凭据 |
| 一次性 probe 容器 | 当前插件 ZIP、固定 probe 脚本、受限安装网络（smoke 阶段无网络） | 数据库、Redis、站点服务、宿主机 socket、其他 artifact |

API、`artifact-worker` 与 `runtime-runner` 必须使用各自独立的环境文件（模板见 `deploy/compose/` 与 `deploy/systemd/`），真实密钥应由部署平台的密钥管理机制注入，切勿提交到代码仓库。`runtime-runner` 使用的数据库账号仅应具备任务领取、心跳上报与必要 artifact 身份读取权限，不得访问市场用户或会话数据。

### systemd 三用户隔离基线

裸机部署应创建三个无登录权限的系统用户，并通过共享组 `astrbot-market` 控制目录访问边界：

- artifact 目录归 `astrbot-market-worker` 所有并设置 `setgid` 组可写，API 与 Worker 通过共享组写入，Runner 仅以附加组身份配合 systemd `ReadOnlyPaths` 只读访问。
- 运行时结果目录归 `astrbot-runtime` 所有并仅对 `astrbot-market` 组开放只读权限；API 服务的 unit 显式将该目录标记为不可访问（`InaccessiblePaths`）。

参考初始化命令如下（UID 可按目标主机调整）：

```bash
groupadd --system astrbot-market
useradd --system --no-create-home --shell /usr/sbin/nologin --gid astrbot-market astrbot-market-api
useradd --system --no-create-home --shell /usr/sbin/nologin --gid astrbot-market astrbot-market-worker
useradd --system --create-home --shell /usr/sbin/nologin astrbot-runtime
usermod --append --groups astrbot-market astrbot-runtime
install -d -o astrbot-market-worker -g astrbot-market -m 2770 /var/lib/astrbot-market/artifacts
install -d -o astrbot-runtime -g astrbot-market -m 2750 /var/lib/astrbot-runtime-results
```

rootless 容器引擎应由 `astrbot-runtime` 用户独立安装和运行，并在 Runner 环境文件中指向其实际 socket 路径（`/run/user/<uid>/docker.sock`）。Runner 的 systemd unit 启用 `ProtectHome=read-only`，既保持 rootless socket 可连接，又阻止写入 `/home`、`/root` 与 `/run/user`。三个 unit 均设置 `UMask=0007` 以维持共享目录组权限，环境文件本身则必须由 `root` 以 `0600` 权限保管。

> [!CAUTION]
> 切勿为了方便调试而将 API 或 `artifact-worker` 用户加入 `docker` 组。

Docker Compose 为兼容共享命名卷，三个服务默认复用同一非 root UID，但通过卷的只读挂载与未挂载边界隔离数据面；高隔离要求的生产环境建议采用上述 systemd 三用户模型或独立节点编排。

## 版本与 Feed 语义

> [!WARNING]
> 当前 source-backed 运行时探针适配器固定对应 AstrBot `4.26.6`（源码提交 `5d10e0d428b41308cc63215db00359c61ee17195`）。升级 AstrBot 版本前必须先比对插件生命周期、管理器、处理器与工具注册路径，并同步更新适配器、镜像目录与策略目标；仅修改版本号不符合升级要求。

市场插件源（`/plugins.json`）始终保留规范化仓库地址 `repo`，`version` 跟随仓库 `metadata.yaml` 更新；仅当仓库版本与当前已发布 artifact 的规范化版本一致时，Feed 才输出 HTTPS CDN `download_url`。当仓库出现尚未过审的新版本时，旧 CDN 对象保持不动但不会冒充新版本，此时省略 `download_url`，用户仍可通过原仓库直连安装（非 GitHub 仓库在关闭 CDN 时需 AstrBot `v4.27.0` 及以上版本）。

## 启动顺序与就绪校验

推荐按以下顺序启动完整审查链路：

1. 启动 PostgreSQL、Redis 与隔离/发布存储，确认数据库迁移与 checksum 校验全部通过。
2. 启动 API 服务，确认公开 `/health` 仅暴露粗粒度状态。
3. 启动 `artifact-worker`，按启用项连接 LLM、ClamAV、服务端 YARA 规则集与依赖漏洞库。
4. 在独立节点或隔离域启动 rootless 容器引擎、受限包安装代理与 `runtime-runner`。
5. 使用核心管理员账号请求 `/v1/core-admin/review-tools/health`，确认 Worker/Runner 心跳未过期且各扫描工具版本与数据新鲜度符合要求（已配置不等于已就绪）。
6. 创建审查策略草稿，依次执行校验（validate）与激活（activate）。开启自动审批前，务必使用真实插件 fixture 验证全部必需阶段。

> [!IMPORTANT]
> 一旦出现心跳缺失、规则或漏洞库过期、扫描器连接失败、LLM 输出非法 JSON 或运行时隔离证明不足，对应阶段必须保持降级（degraded）、阻断（blocked）或未知（`health_unknown`）状态，严禁通过删除必需阶段或伪造心跳来掩盖故障。

Compose 中 `artifacts` 与 `runtime-runner` profile 默认不启用，需按需显式拉起：

```bash
docker compose --profile artifacts --profile runtime-runner config --quiet
docker compose up -d postgres redis app
docker compose --profile artifacts up -d artifact-worker
docker compose --profile runtime-runner up -d runtime-package-proxy runtime-runner
```

Compose 默认将 Runner 指向 `/run/user/10001/docker.sock`、UID/GID `10001:10001`，并保持 `RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false`。若目标节点的 rootless 用户 UID 不同，需同时覆盖 `RUNTIME_RUNNER_UID_GID` 与 `RUNTIME_RUNNER_DOCKER_SOCKET`。显式启用 root socket 仅限本地排障，其生成的运行证明不能作为生产自动审批依据。

## 可选社区源 CDN

插件提交默认不开启 CDN，不触发包审查，按站点自动通过开关直接上架或进入人工上架审核。勾选社区源 CDN 后，插件进入逐版本包审查流程；如果审查服务不可用，后端会明确拒绝开启，不会静默降级为免审。

- **开关权限与影响**：插件所有者可在个人设置关闭自有插件的 CDN，核心管理员可管理任意插件的 CDN，普通管理员仅能管理自有插件。关闭 CDN 无需重新授权平台 OAuth，插件维持原有上架状态，历史包与审计记录保留；Feed 最多受 300 秒缓存影响，之后不再输出 CDN 链接，已有历史文件不会自动删除。重新开启 CDN 须再次验证对应平台仓库管理权限，并为当前未审版本创建送审请求。
- **管理员白名单直发（`cdn_whitelist`）**：仅限 `admin` / `core_admin` 在提交或 `PATCH /v1/plugins/{id}/cdn` 中为本人名下的 CDN 插件启用。启用后仍会完整执行 `PrecheckStage` 压缩包完整性与元数据校验，随后由 `artifact-worker` 实时复核插件所有者与提交者的管理员角色并写入 `review_decisions`（`action="auto_approve"`、`metadata={"cdn_whitelist": true}`），直接进入 `publish` 阶段；若管理员被降权或关闭白名单，将自动回到常规包审查流程。
- **自动跟踪与发布复核**：后台元数据同步发现新版本后会自动送审；GitHub 优先抓取正式 Release ZIP 资产，第三方 OAuth 平台（GitLab.com / Gitee / AtomGit / Gitea）在通过持久凭据自动刷新并复核 `market_git_repository_bindings` 有效性与代次后，固定默认分支 40 位 commit SHA 拉取官方 HTTPS 归档（`source_type='git'`）。任务按版本去重；若管理员通过 `/v1/admin/git-bindings/{id}/revoke` 或 `transfer` 撤销或转移仓库绑定，旧代次待发布任务会被立即阻断。发布事务提交前会再次核对 CDN 开关、授权代次与绑定状态。
- **历史迁移兼容**：迁移 `20260927_007_optional_cdn` 为既有已发布 artifact 的插件保留开启状态，其余插件默认关闭；缺少完整授权快照的历史订阅保留既有包，后续新版本送审需重新开启并完成仓库权限授权。
- **多存储后端切换与旧服务退役**：核心管理员可在 `/admin/settings` 的“基础设施”标签页管理多套对象存储 Profile（本地文件系统 / SQLite / PostgreSQL、S3/R2、WebDAV、CloudFlare ImgBed）并按 `artifact-quarantine`、`artifact-published`、`avatar` 三个用途独立绑定。切换绑定后，新写入立即切向目标 Profile，历史对象由市场后台迁移队列逐项复制、校验 SHA-256 与 Range，并为公开对象写入 `market_storage_url_aliases` 以保持 `/packages/...` 历史下载链接可用。迁移中遇到异常可暂停/继续或仅重试失败项；如需回滚，停止新批次后未迁移对象留在源 Profile，已迁移对象可按迁移条目记录逐项切回。旧服务下线前必须先确认无活动绑定、无未迁移对象、无未完成任务且已人工处理所有 `unknown_objects`，再通过 `retire` 撤销凭据与停止市场访问（不会自动删除远端历史文件），外部旧服务停机与清理作为独立部署步骤执行。

## 人工审查、重跑与 ClamAV 时区

- **人工审查操作**：管理员在扫描中、待审或处理失败状态下均可发表版本评论，并在核对包哈希后提交人工放行；包结构完整性、版本一致性、权限校验与禁止自批规则始终生效。人工评论、审批决定与发布任务在同一事务中持久化，失败或未执行的扫描结果不会被改写为通过。
- **相关接口**：工作台通过 `GET /v1/artifacts/{id}/review-context` 获取基础版本与决定信息，通过 `POST /v1/admin/artifacts/{id}/review-action` 提交 `comment`、`manual_approve` 或 `retry_review` 操作（需携带理由、包 SHA-256 与幂等键，后两者还需 `confirmed=true`）。上述接口均返回 `private, no-store`。
- **重跑审查**：发起重跑前可调用 `GET /v1/admin/artifacts/{id}/retry-review-preview` 预览影响范围。重跑会保留原固定策略与历史报告，将旧任务与报告标记为已被替代，并从静态检查阶段重新执行（若包尚未完成预检则从预检开始）。存在在途任务时系统会拒绝重跑，人工放行不受此限制；人工批准生效后会取消未完成的扫描调度，并阻止滞后的扫描结果覆盖决定。
- **ClamAV 时区对齐**：ClamAV 的 `VERSION` 响应不含时区偏移，Worker 必须通过 `ARTIFACT_CLAMAV_TIMEZONE` 显式指定 `clamd` 所在主机的 IANA 时区（默认 `UTC`，例如东八区主机应设为 `Asia/Shanghai`）。部署时应核对 `VERSION` 输出与病毒库头部的 UTC 时间，不要通过放宽过期阈值掩盖时差问题。

## 策略变更与回滚

所有策略写操作均要求新的请求 ID、稳定的幂等键与人工操作原因，推荐按以下流程变更：

1. 基于当前生效策略（active policy）新建草稿版本，不直接修改历史快照。
2. 执行策略校验，逐项处理 schema、跨字段一致性、工具就绪状态与脱敏 diff 中报告的问题。
3. 激活新策略后，持续观察任务队列堆积、各阶段失败率、转人工比例与路由分布；已提交的既有 artifact 会保持其提交时绑定的策略快照。
4. 发现回归时，对先前已停用（retired）的策略执行回滚；服务会重新校验目标版本与工具就绪状态，通过后原子替换当前生效策略。
5. 仅在没有可用替代策略的极端场景下停用当前策略；停用会阻止新 artifact 绑定有效策略，不应作为常规暂停手段。

策略的激活、停用、回滚与审计事件、通知 outbox 在同一事务中提交。普通管理员仅可只读查看当前生效策略快照，只有核心管理员能够变更策略。策略通知邮件只包含固定状态与工作台链接，完整原因与变更 diff 仅在鉴权页面查看。

## 通知投递

通知邮件正文完全由服务端按事件类型白名单渲染，包含插件或策略名称、版本、固定状态和工作台链接。批准与发布通知会附带该版本已保存的管理员批准评价（限长 1000 字，自动移除代码块并脱敏敏感内容），不直接转发请求体的 `reason` 或行级评论。

源码、requirements、comment、diff、evidence、日志、对象 key 和内部路径不会自动附带在邮件中，完整审查记录仅保存在站内鉴权视图。人工批准决定、发布任务与通知 outbox 在同一事务中提交；实际投递时读取后台最新邮件配置，并遵循作者的邮箱与通知偏好，邮件发送异常不会撤销已经完成的批准。

若运维需按管理员明确要求静默重试发布，可调用 `retry_publish(..., suppress_email=True)`。静默标记会写入审计记录与任务上下文并传递到结果通知：站内仍记录状态及 `email_delivery=suppressed_by_admin_request`，不触发外部邮件，也不改动用户的全局通知偏好。

站内通知通过 outbox 事件 `dedupe_key` 条件写入，Worker 重试不会产生重复记录。外部 SMTP/Cloudflare 通道按 at-least-once 语义投递，若进程在邮件发送成功但尚未确认 outbox 前退出，可能出现重复邮件；邮件失败不影响审查、发布或下架状态。

## 孤儿资源清理

- `runtime-runner` 会周期性清理超过 `RUNTIME_RUNNER_ORPHAN_TTL_SECONDS` 且带有受管标签的残留容器与数据卷，不要用全局 `docker system prune` 替代。
- 当发布存储条件创建成功、但数据库指针尚未提交时，系统会自动排入 `cleanup_orphan` 清理任务；执行前会再次核对当前已发布对象 key，避免误删仍在被引用的对象。
- 隔离包在按保留策略清理前，须确保留足审查、申诉与事故取证窗口。当前未内置跨存储供应商的统一自动清理任务，运维应基于服务端生成的对象前缀与数据库引用关系保守执行清理。
- 切勿手动删除 `current_artifact_id` 对应的已发布文件，应通过工作台发起下架（revoke），确保 Feed 隐藏、决策记录、清理任务与结果全程可审计。

## 事故处置

发现候选版本存在高危风险时，先确认该版本已处于自动拒绝或待人工复核状态，完整保留运行记录、发现详情与工具快照，并通知作者与管理员，切勿在邮件中附带源码或扫描证据。

如果风险可能波及当前已发布的稳定版本，按以下步骤处置：

1. 在工作台核对确定性的文件路径与 SHA-256、依赖漏洞元组或兼容指纹，或由管理员人工核查并填写关联理由；仅来自 LLM 的结论不得用于自动关联稳定版本。
2. 发起紧急下架（emergency revoke）。服务端在同一事务中写入决策记录、从公开 Feed 隐藏插件、置状态为 `revoking` 并排入对象删除任务。
3. 若公开对象删除失败，状态会转入 `revoke_failed` 且 Feed 保持隐藏；修复存储问题后重试下架，切勿恢复旧的公开指针。
4. 轮换可能受影响的 Worker、存储或第三方服务凭据，必要时隔离 Runner 节点，并归档脱敏日志与策略/工具版本快照。
5. 修复后的新插件包必须重新完整走完审查流程，不得直接恢复旧对象或复用旧 `download_url`。

当扫描或外部依赖基础设施发生异常时，应优先关闭自动审批或切换为 fail-closed 策略，同时保留用户通过 GitHub 直连安装插件的能力。停止 `artifact-worker` 只会暂停新任务推进，不影响既有稳定 CDN；停止 API 前应先让 Worker/Runner 停止领取新任务并确认租约可平滑恢复。

## 已知限制

- 当前仅提供 Docker 执行器，Kubernetes 执行器尚未实现同等级契约与真实 fixture 验证。
- 网络标签仅作为运行时隔离证明（attestation）的校验输入，本身不具备防火墙功能；生产环境仍须在宿主机或网络层实施出站、私网与云元数据地址阻断策略。
- ClamAV、YARA 与依赖漏洞库只能覆盖已知特征和当前已同步的数据，未命中告警并不代表插件完全安全。
- LLM 仅生成结构化审查建议，不能单独批准版本、关联稳定风险或充当安全背书。
- 外部邮件投递可能存在重复，且邮件服务商返回成功不代表收件人已查收。
- 每次升级 AstrBot 运行时基线版本后，都必须重新构建探针镜像、审计适配器并使用真实插件 fixture 完成验收。

运行时容器隔离细则见 [runtime-runner.md](runtime-runner.md)，系统整体边界见 [architecture.md](architecture.md)，安全模型与假设见 [security.md](security.md)。
