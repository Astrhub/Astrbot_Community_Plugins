# Runtime Runner 部署指南

`runtime-runner` 是插件动态烟雾测试的独立安全域，既不是 API 的子进程，也不是 `artifact-worker` 的后台线程。只有 `runtime-runner` 有权连接容器引擎；运行插件的一次性容器只能读取当前 artifact ZIP 和固定探针代码，无法访问市场数据库、Redis、对象存储凭据或 Docker socket。

## 生产隔离要求

生产环境启用运行时校验时，必须同时满足以下条件：

- `runtime-runner` 运行在独立节点或隔离安全域，连接非 root 用户的 rootless Docker socket；
- `RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false`；
- 探针镜像在镜像仓库中通过不可变 `sha256` digest 固定，且审查策略的 target catalog 配置完全一致的 digest；
- 依赖安装阶段容器仅接入受限安装网络，由受控包代理强制执行出站白名单；
- 烟雾测试（smoke）容器使用 `network=none`，彻底断网；
- `runtime-runner` 使用独立最小权限 PostgreSQL 角色，仅能领取、续租、完成 dispatch 任务并上报心跳；
- artifact 目录对 Runner 仅只读挂载，结果目录单独可写；
- 严禁向插件容器注入数据库 URL、Redis URL、对象存储密钥或 LLM 凭据。

> [!IMPORTANT]
> Docker Compose 的 `runtime-runner` profile 默认挂载独立服务用户的 `/run/user/10001/docker.sock`，并将 `RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT` 设为 `false`。如果目标主机 rootless 用户的 UID 不同，需同时调整 `RUNTIME_RUNNER_UID_GID` 与 `RUNTIME_RUNNER_DOCKER_SOCKET`。显式改用 root socket 仅限本地非生产排障，即使其他网络检查全部通过，其生成的隔离证明（attestation）也不会被生产自动审批采信。

当前生命周期适配器固定对应 AstrBot `4.26.6`（源码提交 `5d10e0d428b41308cc63215db00359c61ee17195`）。升级 AstrBot 源码版本时，必须先完成源码审计与生命周期差异比对，再更新适配器与 runtime target catalog。

## Docker Compose 部署

先启动基础服务与 `artifact-worker`，再按需构建并启动 `runtime-runner` 相关服务：

```bash
docker compose --profile artifacts up -d --build
docker compose --profile runtime-runner build runtime-probe-image runtime-package-proxy runtime-runner
docker compose --profile runtime-runner up -d runtime-package-proxy runtime-runner
```

本地使用仓库内真实 fixture 调试时，可通过以下命令获取探针镜像的本地 ID：

```bash
docker image inspect astrbot-runtime-probe:local --format '{{.Id}}'
```

控制面仅在 runtime target catalog 配置了完全一致的 `sha256:` digest 后才会下发对应 dispatch 任务，不得使用可变标签、`latest` 或构建时间替代 digest。

## 受限安装网络

依赖安装容器只允许接入 `astrbot-runtime-install` 网络，该网络需满足以下约束：

- Docker 网络属性为 `Internal=true`；
- 禁用地址伪装（`com.docker.network.bridge.enable_ip_masquerade=false`）；
- 网络标签中的 profile、策略版本与包源哈希与任务 dispatch 保持一致；
- 同一网络内的对端只能是带有 `astrbot.runtime.package-proxy=true` 标签的受控代理，或由 Runner 创建的受管容器；
- 包代理挂载到独立出站网络，白名单仅放行 `pypi.org` 与 `pythonhosted.org`；
- 宿主机网关、内网网段、云元数据端点与市场站点服务的阻断规则由宿主机防火墙或网络策略强制执行。

> [!WARNING]
> Docker 网络标签仅作为运行时证明的校验输入，本身不具备防火墙能力。生产运维必须在节点防火墙或网络策略真正生效后，才可配置 `host-gateway-blocked`、`private-network-blocked` 等标签。若 Runner 无法核验网络属性、标签或对端身份，将以 `runtime_network_unverified` 失败关闭（fail-closed）。

## Rootless Docker 配置

生产 Runner 应以普通系统用户运行，并通过以下环境变量指向该用户的 rootless socket：

```env
RUNTIME_RUNNER_EXECUTOR_BACKEND=rootless-docker
RUNTIME_RUNNER_DOCKER_HOST=unix:///run/user/1000/docker.sock
RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false
RUNTIME_RUNNER_DOCKER_IMAGE_REPOSITORY=registry.example/astrbot-runtime-probe
```

> [!CAUTION]
> 严禁把 root Docker socket 暴露给 API、`artifact-worker` 或插件容器；`runtime-runner` 自身也不得与公开 Web 服务共用容器或系统账号。

## Kubernetes 适配说明

仓库当前仅实现了 Docker 执行器，尚未交付 Kubernetes 执行器。若后续迁移到 Kubernetes，应保持统一的 `ContainerExecutor` 契约，并落实独立 Namespace、最小权限 ServiceAccount、NetworkPolicy、只读 rootfs、非 root UID、seccomp `RuntimeDefault`、CPU/内存限额与一次性 PVC。在通过同等级契约测试与真实插件 fixture 验证之前，不得将 Kubernetes 路径标记为就绪。

## 部署验证与生产验收

每次调整部署配置或镜像后，至少先运行以下本地校验：

```bash
docker compose --profile runtime-runner config
uv run --project apps/api pytest -q apps/api/tests/test_docker_executor.py
uv run --project apps/api pytest -q apps/api/tests/test_runtime_compose.py
```

在目标生产节点开启自动审批前，还必须完成以下端到端验收：

1. 使用私有或受控镜像仓库中的不可变 `sha256` digest 固定探针镜像，不要直接依赖本地未推送镜像 ID。
2. 在真实 rootless Docker 节点上运行正常插件、核心依赖冲突和 import 失败三类真实 fixture，确认返回的结构化结果与预期一致，且运行时证明（attestation）不为 `unknown`（rootful daemon 下的验证仅能证明失败关闭逻辑，不能替代生产 rootless 验收）。
3. 核验包安装代理仅放行 PyPI 白名单域名，并阻断公网其他地址、私网与云元数据访问；核验 smoke 阶段容器完全无网络。
4. 确认每次执行结束后，受管一次性容器与数据卷均已彻底清理，无残留对象。

策略回滚、孤儿资源清理与事故处置步骤见 [plugin-review-operations.md](plugin-review-operations.md)。
