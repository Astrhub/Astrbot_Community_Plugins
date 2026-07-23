# Runtime Runner 部署

`runtime-runner` 是插件动态校验的独立安全域。它不是 API 子进程，也不是
`artifact-worker` 的附加线程。只有 runner 能连接容器引擎；插件容器只能读取当前
artifact ZIP 和固定 probe 程序，不能看到站点数据库、Redis、对象存储凭据或 Docker
socket。

## 隔离等级

生产环境必须同时满足：

- runner 位于独立节点或独立安全域，使用 rootless Docker socket；
- `RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false`；
- probe 镜像由 registry digest 固定，policy catalog 保存同一 digest；
- install 网络由受控 package proxy 强制出站白名单；
- smoke 容器使用 `network=none`；
- runner 使用独立 PostgreSQL 角色，只能领取、续租和完成 runtime dispatch；
- artifact 目录在 runner 中只读，结果目录单独可写；
- 不向插件容器传入数据库 URL、Redis URL、对象存储密钥或 LLM 凭据。

Compose 的 `runtime-runner` profile 默认挂载独立服务用户的
`/run/user/10001/docker.sock`，并设置 `RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false`。可通过
`RUNTIME_RUNNER_UID_GID` 和 `RUNTIME_RUNNER_DOCKER_SOCKET` 对齐目标节点的 rootless 用户。显式改用 root
socket 只属于本地非生产排障路径；即使其余网络检查通过，结果 attestation 仍不能作为生产自动批准证据。

当前 lifecycle adapter 固定 AstrBot `4.26.6` 和提交
`5d10e0d428b41308cc63215db00359c61ee17195`。源码版本变化后必须先运行 source audit、比较 lifecycle 相关
diff，再更新 adapter 和 runtime target catalog。

## 本地 Compose

先启动基础服务和 artifact worker，再构建 opt-in runtime 服务：

```bash
docker compose --profile artifacts up -d --build
docker compose --profile runtime-runner build runtime-probe-image runtime-package-proxy runtime-runner
docker compose --profile runtime-runner up -d runtime-package-proxy runtime-runner
```

probe 镜像的本地 image ID 可用于仓库内真实 fixture 测试：

```bash
docker image inspect astrbot-runtime-probe:local --format '{{.Id}}'
```

控制面只有在 runtime target catalog 配置了完全相同的 `sha256:` digest 后才能创建对应
dispatch。不要使用 tag、`latest` 或构建时间推断 digest。

## Install 网络

install 容器只加入 `astrbot-runtime-install`。该网络必须满足：

- Docker `Internal=true`；
- `com.docker.network.bridge.enable_ip_masquerade=false`；
- profile、policy version 和 package index hash 标签与 dispatch 一致；
- peer 只能是带 `astrbot.runtime.package-proxy=true` 的代理，或 runner 创建的受管容器；
- package proxy 位于独立 egress 网络，只允许 `pypi.org` 和 `pythonhosted.org`；
- 宿主 gateway、私网、cloud metadata 和站点服务阻断由节点防火墙或网络策略实施。

网络标签是 attestation 输入，不是防火墙本身。生产运维只有在节点策略真实生效后才能设置
`host-gateway-blocked`、`private-network-blocked` 等标签。无法核验网络属性、标签或 peer
时，runner 以 `runtime_network_unverified` 失败关闭。

## Rootless Docker

生产 runner 应以普通系统用户运行，并将以下变量指向该用户的 socket：

```bash
RUNTIME_RUNNER_EXECUTOR_BACKEND=rootless-docker
RUNTIME_RUNNER_DOCKER_HOST=unix:///run/user/1000/docker.sock
RUNTIME_RUNNER_ALLOW_ROOTFUL_DEVELOPMENT=false
RUNTIME_RUNNER_DOCKER_IMAGE_REPOSITORY=registry.example/astrbot-runtime-probe
```

不要把 root Docker socket 传给 API、artifact worker 或插件容器。runner 自身也不得与
公开 Web 服务共用容器或服务账号。

## Kubernetes

当前仓库交付的是 Docker executor，未实现 Kubernetes executor。迁移到 Kubernetes 时应
保持同一 `ContainerExecutor` contract，并使用独立 namespace、最小权限 ServiceAccount、
NetworkPolicy、只读 rootfs、非 root UID、seccomp RuntimeDefault、资源限额和一次性 PVC。
在 Kubernetes executor 完成同等级 contract/fixture 验证前，不得把该路径标记为 ready。

## 验证

部署变更至少执行：

```bash
docker compose --profile runtime-runner config
uv run --project apps/api pytest -q apps/api/tests/test_docker_executor.py
uv run --project apps/api pytest -q apps/api/tests/test_runtime_compose.py
```

真实 fixture 还必须确认正常插件、依赖冲突和 import failure 的结构化结果，验证 smoke
无法连接互联网、metadata、宿主 gateway、PostgreSQL 和 Redis，并确认受管 container 与
volume 最终不存在。

## 2026-07-15 验证记录

本轮从当前源码构建并验证的本地镜像为：

- probe：`sha256:7adf912368a1ed2e281742905252e8469e32ed69976c7f9838aa41da6a6d1dcb`
  （Python `3.12.10`）；
- package proxy：`sha256:e712569a88bd93df6e544a10da430877fcd87d7b9bf93f6ac2a9443510ffb96f`；
- runtime runner：`sha256:50976df2407f5c1c11e8109906ed1bca272369266fa733c7bd291bddcd8b5bd5`。

验证结果：正常插件、核心依赖冲突、import failure 三个真实 fixture 均通过；最终代理镜像的
PyPI 白名单与外网、metadata 阻断测试通过；Task 6 定向测试为 `59 passed, 4 skipped`，全量
API 测试为 `344 passed, 12 skipped`，Ruff 和 Compose 配置检查通过。跳过项是默认关闭的
真实 Docker 测试，不是未实现功能；非 mock 场景已通过显式开关分别执行。
`runtime-runner` 与主 API Docker 目标均完成实际构建，runner 内服务模块导入和 Docker CLI
`29.6.1` 检查通过。

执行环境的 Docker daemon 是 rootful，因此 attestation 按设计保持 `unknown`，上述记录只
证明开发隔离路径和失败关闭逻辑，不能替代生产 rootless 节点验收。生产部署必须使用 registry
digest 重新固定镜像，并在目标节点重复真实 fixture 后才可启用自动批准。

策略回滚、孤儿清理和事故处置步骤见 [plugin-review-operations.md](plugin-review-operations.md)。
