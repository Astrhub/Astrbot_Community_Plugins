# 插件审查运维手册

本文面向部署和事故值守人员。自动扫描、隔离运行和 LLM 审查只能提供风险信号，不能证明插件绝对安全；
人工决定也不能替代持续监控和事后处置。

## 服务与信任边界

| 进程 | 必需权限 | 明确禁止 |
|---|---|---|
| FastAPI API | PostgreSQL、Redis、隔离上传存储、站点邮件配置 | Docker socket、scanner endpoint、YARA 规则、advisory token、LLM key |
| artifact worker | PostgreSQL、Redis、隔离/发布存储、runtime 结果只读、配置的审查工具 | Docker socket、运行插件代码 |
| runtime runner | 最小权限 PostgreSQL 角色、artifact 只读、result 目录可写、独立 rootless engine | Redis、站点/OAuth/邮件/LLM/对象存储凭据 |
| 一次性 probe | 当前 ZIP、固定 probe、受限安装网络；smoke 阶段无网络 | 数据库、Redis、站点服务、宿主 socket、其他 artifact |

API、worker、runner 必须使用不同环境文件。示例位于 `deploy/compose/` 和 `deploy/systemd/`；真实凭据由
部署平台的 secret manager 注入，不提交到仓库。runner 的数据库角色只应访问 runtime dispatch、必要的
artifact identity 和 heartbeat 操作，不能获得市场用户或会话数据权限。

systemd 部署应创建三个无登录用户和共享 `astrbot-market` 组。artifact 目录由 worker 拥有、组可写，API 和
worker 通过该组写入；runner 只通过 supplementary group 读取，unit 的 `ReadOnlyPaths` 再强制只读。runtime
result 目录由 runner 拥有、`astrbot-market` 组只读，API unit 将该目录设为不可访问。一个可调整 UID 的
基线如下：

```bash
groupadd --system astrbot-market
useradd --system --no-create-home --shell /usr/sbin/nologin --gid astrbot-market astrbot-market-api
useradd --system --no-create-home --shell /usr/sbin/nologin --gid astrbot-market astrbot-market-worker
useradd --system --create-home --shell /usr/sbin/nologin astrbot-runtime
usermod --append --groups astrbot-market astrbot-runtime
install -d -o astrbot-market-worker -g astrbot-market -m 2770 /var/lib/astrbot-market/artifacts
install -d -o astrbot-runtime -g astrbot-market -m 2750 /var/lib/astrbot-runtime-results
```

rootless engine 由 `astrbot-runtime` 用户安装和启动；把实际 UID 对应的 `/run/user/<uid>/docker.sock` 写入
runner env。runner unit 使用 `ProtectHome=read-only`，使 rootless socket 保持可连接，同时仍禁止向
`/home`、`/root` 和 `/run/user` 写入。三个 unit 使用 `UMask=0007`，让 setgid 共享目录中的文件保留组访问；
env 文件本身仍必须由 root 以 `0600` 管理。不要为方便而把 API 或 worker 加入 Docker 组。

Compose 为兼容共享 named volume，三个服务默认使用同一个非 root UID，但依靠 volume 的 `ro`/未挂载边界
隔离数据面；生产高隔离部署应采用上述 systemd 三用户模型或等价的独立节点/编排策略。

## 版本语义

当前 source-backed smoke adapter 只接受 AstrBot `4.26.6` 和源码提交
`5d10e0d428b41308cc63215db00359c61ee17195`。本地源码更新后必须先比较 plugin lifecycle、manager、handler
和 tool 注册路径，再更新 adapter、镜像 catalog 和 policy target；仅修改显示版本是不合格的升级。

市场 feed 永久保留 `repo`。`version` 跟随仓库 metadata；只有仓库版本与当前 published artifact 的规范化
版本一致时才输出 CDN `download_url`。候选更新未过审时，旧 CDN 对象保持不变，但不会冒充仓库的新版本。

## 启动与就绪

推荐顺序：

1. 启动 PostgreSQL、Redis 和私有/发布存储，执行 migration 并核对 checksum。
2. 启动 API，确认公共 `/health` 只显示粗粒度状态。
3. 启动 artifact worker；按启用项连接 LLM、ClamAV、服务端 YARA ruleset 和 dependency advisory。
4. 在独立节点启动 rootless container engine、安装网络代理和 runtime runner。
5. 以核心管理员访问 `/v1/core-admin/review-tools/health`，确认 worker heartbeat 未过期、工具版本和数据
   freshness 符合预期。`configured` 不等于 `ready`。
6. 创建 draft policy，先 validate，再 activate。启用 auto approve 前应先用真实 fixture 验证所有 required gate。

缺 heartbeat、过期 rules/advisory、scanner 连接失败、LLM 无效 JSON 或 runtime attestation 不可信时，状态必须
是 degraded/blocked/health_unknown。不要通过删 required stage 或伪造 heartbeat 把故障变成 clean。

Compose 的 `artifacts` 和 `runtime-runner` profile 都是显式 opt-in：

```bash
docker compose --profile artifacts --profile runtime-runner config --quiet
docker compose up -d postgres redis app
docker compose --profile artifacts up -d artifact-worker
docker compose --profile runtime-runner up -d runtime-package-proxy runtime-runner
```

默认 runner 使用 `/run/user/10001/docker.sock`、容器 UID/GID `10001:10001`，并设置
`RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false`。目标节点 UID 不同时同时覆盖
`RUNTIME_RUNNER_UID_GID` 和 `RUNTIME_RUNNER_DOCKER_SOCKET`。rootful 开关只允许本地故障排查；该结果的隔离
证明不能用于生产自动批准。

## 可选社区源 CDN

提交默认不使用 CDN：不创建包审查，按站点的自动通过开关直接上架或进入普通上架审核。勾选 CDN 时进入逐版本包审查；审查服务不可用会明确拒绝，不降级为免审。

插件所有者可在个人设置关闭自有插件的 CDN，核心管理员可管理任意插件的 CDN；普通管理员仅能管理自己所有的插件。关闭无需重新连接 GitHub，插件保持原上架状态，历史包和审计记录保留；插件源最多受已有 300 秒缓存影响，之后不再提供 CDN 链接，已知历史包地址不会自动删除。开启仍需验证仓库管理权限，并为当前未审版本持久化送审请求。

GitHub 元数据同步发现新版本后自动送审，固定默认分支提交 SHA，并用站点令牌复核已授权仓库的 ID、归属和公开状态。任务按版本去重；临时故障五分钟后重试，包超限或确定性的来源、身份校验失败则停止当前版本重试，待新版本或重新开启 CDN 时再送审。新版本过审前不提供其 CDN 链接，不拿旧包充当新版本。关闭与发布并发时，发布事务会再次校验开关及授权代次。

迁移 `20260927_007_optional_cdn` 为已有 Artifact 的插件保留 CDN，其余默认关闭；历史仓库授权证据不足的订阅保留已有包，后续送审需要重新开启并授权。

## 人工审查与重跑

版本评论不需要文件或行号。管理员可在扫描中、待审或处理失败时填写意见并确认人工放行；包结构、版本一致性、权限和禁止自批仍会校验。人工意见与审批、发布任务同事务保存，失败或未执行的扫描不会改写成通过。

工作台的基础版本信息独立于扫描报告加载。`GET /v1/artifacts/{id}/review-context` 返回版本与决定；管理员使用 `POST /v1/admin/artifacts/{id}/review-action` 提交 `comment`、`manual_approve` 或 `retry_review`，并携带理由、包 SHA-256、幂等键；后两者还需 `confirmed=true`。接口均为私有、禁止缓存。

重跑前可读取 `GET /v1/admin/artifacts/{id}/retry-review-preview`。重跑保留固定策略和历史报告，标记旧任务/报告已被替代，从静态检查重新执行下游；包尚未解析时重新执行预检。在途任务未结束时拒绝重跑，人工放行不受此限制。人工批准会取消未完成的扫描调度，并阻止后续扫描覆盖决定。

ClamAV 的 `VERSION` 不携带时区，worker 必须通过 `ARTIFACT_CLAMAV_TIMEZONE` 指定 daemon 的 IANA 时区（默认 `UTC`；jp 为 `Asia/Shanghai`）。部署时对照 `VERSION` 和病毒库头部 UTC 时间验证；不要通过放宽新鲜度阈值掩盖时差。


## 策略变更与回滚

所有 mutation 都需要新的 request ID、稳定 idempotency key 和人工原因。推荐流程：

1. 从当前 active policy 创建新版本，不原地编辑历史快照。
2. validate 并处理 schema、cross-field、tool readiness 和 redacted diff 中的全部问题。
3. activate 后观察 queue、stage failure、manual wait 和 routing 指标；既有 artifact 继续使用其固定快照。
4. 回归时对先前 retired policy 执行 rollback。服务会重新校验目标和工具就绪，再原子替换 active policy。
5. 没有可用策略时可 retire 当前策略；这会阻止新 artifact 固定有效 policy，不应作为日常暂停手段。

activate、retire、rollback 与审计事件、通知 outbox 同事务提交。普通 admin 只能读取 active snapshot；只有
core admin 能改变策略。策略邮件只提供固定状态和工作台链接，完整 reason/diff 只在鉴权页面查看。

## 通知投递

邮件正文由服务端事件白名单生成，包含名称、版本、固定状态和工作台链接。批准及发布通知还附带该版本管理员批准时的简短评价（最多 1000 字）；只取已保存的批准决定，不直接转发 payload 的 reason 或行评论。
源码、requirements、comment、diff、evidence、日志、对象 key 和内部路径不自动附带；评价中的代码块省略，敏感内容隐藏或脱敏。站内保留完整审查记录。
人工批准的决定、发布任务及通知 outbox 同事务提交。每次投递读取后台最新邮件配置，继续尊重作者的邮箱和通知偏好；邮件异常不撤销批准。

运维按管理员明确要求静默重试发布时，可调用 `retry_publish(..., suppress_email=True)`；抑制标记写入重试审计及任务，传递到成功/失败通知。站内仍记录状态和 `email_delivery=suppressed_by_admin_request`，不发送邮件，也不修改用户全局通知偏好。

站内记录用 outbox event dedupe key 条件写入；worker 重试不会重复创建。SMTP/Cloudflare 是 at-least-once，
邮件发送成功但 outbox 确认前进程退出时可能重复投递。邮件失败不改变审查、发布或下架状态。

## 孤儿清理

- runtime runner 周期性清理超过 `RUNTIME_RUNNER_ORPHAN_TTL_SECONDS` 且带受管 label 的容器和 volume；不要
  用宽泛的 Docker prune 代替。
- 发布条件创建成功但数据库指针未提交时，会排入 `cleanup_orphan` job。清理前重新检查当前 published key，
  仍被引用的对象不会删除。
- 隔离包按 retention policy 清理前必须保留审查、申诉和事故调查所需窗口。当前没有跨存储供应商的统一
  自动 retention 执行器，运维策略必须按服务端生成的 key 前缀和数据库引用做保守清理。
- 不要手工删除 `current_artifact_id` 对应对象；应从工作台执行 revoke，使 feed 隐藏、decision、job 和结果
  保持可审计。

## 事故处理

发现候选版本 critical 风险时，确认自动拒绝或人工队列状态，保留 run/finding/tool snapshot，并通知作者和
管理员。不要在邮件中转发代码或证据。

风险可能影响当前稳定包时：

1. 在工作台核对 deterministic path+SHA、dependency advisory tuple、兼容 fingerprint，或由管理员明确确认
   关联并填写理由；LLM-only 结论不能自动关联稳定版本。
2. 执行 emergency revoke。事务先写 decision、隐藏 feed、设置 `revoking` 并排队删除对象。
3. 若对象删除失败，状态进入 `revoke_failed` 且 feed 继续隐藏；修复存储后重试，不要恢复公开指针。
4. 轮换可能暴露的 worker/storage/provider 凭据，隔离 runner 节点，保存脱敏日志和 policy/tool 版本。
5. 发布已修复的新 artifact 必须重新走完整审查，不能直接恢复旧对象或复用旧 `download_url`。

工具基础设施异常时优先关闭 auto approve 或激活 fail-closed policy，并保留 GitHub 直连能力。停止 worker 会
阻止新任务推进，但不会覆盖旧稳定 CDN；停止 API 前先让 worker/runner 结束新领取并等待 lease 可恢复。

## 已知限制

- 当前只实现 Docker executor；Kubernetes executor 尚未达到同等 contract 和 fixture 证据。
- 网络 label 是 attestation 输入，不是防火墙；生产仍需节点 egress、私网和 metadata endpoint 策略。
- ClamAV/YARA/advisory 只能覆盖已知规则和当前数据；clean 不代表无恶意行为。
- LLM 只产生结构化审查建议，不能单独批准、关联稳定风险或作为安全背书。
- 外部邮件可能重复，外部 provider 的成功响应也不证明收件人已读。
- 每次 AstrBot 源码升级都需要重新构建 probe、审计 adapter 并执行真实插件 fixture。

Runtime 隔离细节见 [runtime-runner.md](runtime-runner.md)，系统边界见
[architecture.md](architecture.md)，安全假设见 [security.md](security.md)。
