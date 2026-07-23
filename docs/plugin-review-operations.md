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

邮件正文由服务端事件白名单生成，只包含插件或策略名称、版本、固定状态、固定短原因和工作台链接。
源码、requirements、comment、diff、evidence、日志、对象 key、内部路径、自由文本 reason/code 和凭据不得
进入邮件。站内通知可显示规范化且有长度上限的 reason，并受 owner/admin 权限保护。

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
