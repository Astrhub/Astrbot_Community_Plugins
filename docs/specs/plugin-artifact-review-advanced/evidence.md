# 插件制品高级审查系统 - 完成度证据矩阵

本文件是 Task 21 的结案依据。它区分代码存在、确定性测试、真实 PostgreSQL、真实容器/工具和浏览器视觉
证据；默认 skip、fake adapter、接口存在或历史运行记录不能单独标记为完成。

## 状态定义

| 状态 | 含义 |
|---|---|
| `VERIFIED` | 2026-07-17 在当前提交上执行了对应门禁并通过 |
| `PARTIAL` | 实现和确定性证据通过，但生产环境能力仍有明确限制 |
| `PENDING` | Task 21 尚未执行当前门禁，不得据此宣称完成 |
| `N/A` | 需求明确不要求真实外部供应商；使用固定本地 snapshot/contract 即为权威验证 |

## FR-201 至 FR-224

| FR | 生产实现 | 确定性/API 证据 | 真实/集成门禁 | 当前状态 |
|---|---|---|---|---|
| FR-201 AI 分类 | `artifacts/category.py`、`stages/category.py` | `test_artifact_category.py`（输入边界、优先级、失败） | PG 分类并发场景 | `VERIFIED` |
| FR-202 隔离执行器 | `runtime_runner/docker_executor.py`、`execution.py` | `test_docker_executor.py`、`test_container_executor.py` | `test_runtime_docker_integration.py` opt-in；镜像实际构建 | `VERIFIED` |
| FR-203 AstrBot smoke | `runtime_runner/probe/{install,smoke,entrypoint}.py` | `test_install_probe.py`、`test_smoke_probe.py`、runtime fixtures | 真实 pass/dependency-conflict/import-failure 容器 | `VERIFIED` |
| FR-204 runtime 版本 | `artifacts/runtime_targets.py`、`stages/runtime.py` | `test_runtime_targets.py`、`test_astrbot_source_contract.py` | `/root/work/AstrBot` 精确 version/commit audit | `VERIFIED` |
| FR-205 LLM 包级 | `package_review.py`、`stages/llm_package.py` | `test_artifact_package_review.py`（schema、预算、invalid JSON） | provider 真实调用不属于完成门禁；adapter contract 为权威 | `VERIFIED` |
| FR-206 LLM 文件级 | `file_review.py`、`stages/llm_file.py` | `test_artifact_file_review.py`（选择、SHA/行号/evidence） | 固定 fake provider，服务端内容读取为真实实现 | `VERIFIED` |
| FR-207 LLM 汇总边界 | `summary_review.py`、`stages/llm_summary.py` | file/summary/routing tests（不降级、不直接决定） | `N/A` | `VERIFIED` |
| FR-208 自动路由 | `routing_evaluator.py`、`stages/routing.py` | `test_artifact_routing.py`、`test_artifact_orchestration.py` | PG concurrent auto-approve | `VERIFIED` |
| FR-209 artifact diff | `diff.py`、`stages/diff_graph.py` | `test_artifact_diff.py` | PG tree binding | `VERIFIED` |
| FR-210 import 图 | `import_graph.py` | `test_artifact_import_graph.py` | PG graph atomicity | `VERIFIED` |
| FR-211 内容 API | `content.py`、`routes.py` | `test_artifact_content.py`、`test_artifact_content_routes.py` | Local/S3 contract tests | `VERIFIED` |
| FR-212 行级评论 | `comments.py`、`comment_service.py` | `test_artifact_comments.py`、`test_artifact_comment_routes.py` | PG concurrent comment scenario | `VERIFIED` |
| FR-213 修改/重提 | `resubmission.py`、decision routes | `test_artifact_resubmission.py`、comment/route tests | Task 21 业务场景组 | `VERIFIED` |
| FR-214 审查历史 | `history.py` | `test_artifact_history.py` | PG history keyset scenario | `VERIFIED` |
| FR-215 稳定风险 | `stable_risk.py`、revoke service/routes | `test_stable_risk.py` | PG stable-risk/revoke transaction | `VERIFIED` |
| FR-216 ClamAV | `malware.py`、`stages/malware.py` | fake clamd、五态、critical tests | 真实 clamd clean/EICAR opt-in | `VERIFIED` |
| FR-217 YARA | `malware.py`、`yara_helper.py` | 真实 `yara-python` subprocess、limits/bad rules | 本地服务端 ruleset fixture；无外部 daemon | `VERIFIED` |
| FR-218 依赖/SBOM | `requirements_parser.py`、`sbom.py`、`advisory.py`、dependency stage | parser/SBOM/advisory/dependency tests | 真实 runtime SBOM sidecar + 固定 advisory snapshot | `VERIFIED` |
| FR-219 网络策略 | `runtime_runner/network_policy.py`、proxy config、Docker executor | network label/argv/fail-closed tests | 真实 install proxy allowlist、外网/metadata 阻断 | `VERIFIED` |
| FR-220 版本化策略 | `policy.py`、`policy_service.py`、repository/routes | policy/service/route tests | PG migration、并发激活、snapshot | `VERIFIED` |
| FR-221 健康/指标 | `artifacts/runtime.py`、`observability.py` | `test_review_observability.py`、health route tests | PG heartbeat/metrics + Compose process split | `VERIFIED` |
| FR-222 通知 | `notifications.py`、transactional outbox | `test_artifact_notifications.py` | PG policy outbox、retry/dedupe | `VERIFIED` |
| FR-223 API/OpenAPI | artifact routes/schemas、OpenAPI role filter | content/comment/history/policy routes、`test_openapi.py` | API full suite + PG concurrency | `VERIFIED` |
| FR-224 工作台 UI | `PluginWorkbench.vue`、共享 workbench components/stores | frontend Vitest、`vp check`、build | `e2e/plugin-workbench.pw.ts` + [桌面/移动截图](visual-evidence/) + DOM bounds | `VERIFIED` |

## P2 验收

| ID | 验收项 | 证据 | 状态 |
|---|---|---|---|
| P2-01 | API/普通 worker 不执行插件/import/install/subprocess | process wiring、runtime boundary tests、secret/socket scan | `VERIFIED` |
| P2-02 | runtime 只在一次性受限容器运行并清理 | real Docker fixture + managed resource scan | `VERIFIED` |
| P2-03 | smoke 保存版本、install/import/startup/注册/清理 | real result contract + fixture matrix | `VERIFIED` |
| P2-04 | 依赖冲突/import/handler/tool 有稳定 finding | runtime finding/fixture tests；真实 conflict/import | `VERIFIED` |
| P2-05 | 容器阻断 DB/Redis/socket/metadata/smoke 外网 | real proxy/network integration；attestation 限制另列 | `VERIFIED` |
| P2-06 | AI 分类优先级、置信度、理由 | category tests + PG | `VERIFIED` |
| P2-07 | package 不收全量源码，file 只收验证集合 | package/file input projection tests | `VERIFIED` |
| P2-08 | LLM schema invalid/timeout/budget fail visible | package/file/summary tests | `VERIFIED` |
| P2-09 | LLM 行号/evidence 重新验证 | file review invalid line/evidence tests | `VERIFIED` |
| P2-10 | LLM 不降确定性风险、不直接决定 | summary/routing/stable-risk tests | `VERIFIED` |
| P2-11 | auto approve 默认关，开启仍全门禁并可审计 | routing + PG concurrent auto approve | `VERIFIED` |

## P3 验收

| ID | 验收项 | 证据 | 状态 |
|---|---|---|---|
| P3-01 | 五类 diff/rename 与 tree hash 绑定 | diff corpus + PG | `VERIFIED` |
| P3-02 | AST 图不执行代码并覆盖入口/反向/删除影响 | import graph corpus | `VERIFIED` |
| P3-03 | 动态 import/syntax/base 缺失显式降级 | diff/import/routing tests | `VERIFIED` |
| P3-04 | metadata/requirements 变化触发完整 runtime/dependency | import graph/orchestration tests | `VERIFIED` |
| P3-05 | 作者隔离，管理员权限矩阵 | content/comment/history route tests | `VERIFIED` |
| P3-06 | 内容 API 拒 binary/path/oversize/unregistered key | content/storage tests | `VERIFIED` |
| P3-07 | 评论 side/SHA/line/concurrency | comment domain/routes + PG | `VERIFIED` |
| P3-08 | request_changes 不发布，重提新 artifact 保留历史 | Task 21 business scenario | `VERIFIED` |
| P3-09 | 工作台 files/diff/comments/decision/history，文本 inert | Vitest + 可复跑 Playwright fixture/截图 | `VERIFIED` |
| P3-10 | critical 无证据不撤回；关联后先隐藏再撤对象 | stable risk tests + PG | `VERIFIED` |
| P3-11 | 更新失败不改稳定指针/旧 CDN，repo 保留 | publish/feed drift scenario | `VERIFIED` |

## P4 验收

| ID | 验收项 | 证据 | 状态 |
|---|---|---|---|
| P4-01 | 真实 clamd 区分 clean/infected/unknown/error/stale 并阻断 EICAR | real clamd + deterministic stage tests | `VERIFIED` |
| P4-02 | YARA hash/version/timeout/offset 可审计且受限 | real yara subprocess corpus | `VERIFIED` |
| P4-03 | requirements/final graph/SBOM/advisory | parser/install/SBOM/dependency tests + real runtime sidecar | `VERIFIED` |
| P4-04 | advisory stale/error 不显示无漏洞 | advisory/dependency tests | `VERIFIED` |
| P4-05 | install/smoke profile 由基础设施强制并阻断私网/metadata/站点 | real Docker network test | `VERIFIED` |
| P4-06 | 只有 core admin 改策略，无效策略不替换 active | policy API/service tests | `VERIFIED` |
| P4-07 | 并发仅一个 active，artifact 固定 snapshot | PG policy concurrency | `VERIFIED` |
| P4-08 | policy/病毒库/YARA/advisory/model 版本进入审计 | run/decision projection + PG | `VERIFIED` |

## 全局回归

| ID | 验收项 | 证据 | 状态 |
|---|---|---|---|
| G-01 | P0/P1 API、旧 listed、GitHub sync、feed 不回归 | API full suite + business scenarios | `VERIFIED` |
| G-02 | CDN key 仍为 author/repo/version/name-version-suffix 且重试稳定 | storage/pipeline tests | `VERIFIED` |
| G-03 | repo/published version 不一致时 URL 空但 repo 保留 | publish/feed drift scenario | `VERIFIED` |
| G-04 | Ruff/pytest/vp/Vitest/build/OpenAPI/Compose/deploy 全通过 | Task 21 final command log | `VERIFIED` |
| G-05 | 至少一条真实容器 smoke fixture | real Docker fixture matrix | `VERIFIED` |
| G-06 | 文档准确描述边界、前置、失败和限制 | docs contract + OMP advisory + self-review | `VERIFIED` |

## 当前已知限制

- 本机 Docker daemon 是 rootful。真实 probe 仍以非 root、只读 rootfs、无 capabilities、无 smoke 网络运行，
  但 rootless daemon attestation 只能保持 `unknown`，不能宣称已验证生产 rootless 节点。
- YARA 使用真实 `yara-python` 隔离 subprocess 和服务端固定 ruleset，不存在需要连接的外部 daemon；这验证
  engine contract，不代表任何特定生产 ruleset 已覆盖全部威胁。
- Advisory 完成门禁使用版本化本地 snapshot；在线 provider 不是默认完成条件。`stale/not_queried/error` 必须
  继续与“无已知漏洞”分离。
- LLM 使用版本化 schema 和确定性 fake provider 验证边界；不调用真实模型来证明安全性。

## Task 21 运行记录

本节只记录当前提交上实际执行的命令、汇总结果和必要的 image/tool/database version，不记录凭据、
endpoint、对象 key 或原始日志。

- **业务状态机场景**：发布/feed 漂移、request changes/resubmit、LLM invalid/degraded、runtime findings 和
  stable-risk/revoke 组合测试 `24 passed`。断言候选失败不移动稳定指针、不覆盖旧 CDN 对象，feed 保留
  `repo` 且版本不一致时隐藏 `download_url`。
- **真实 runtime 容器**：实际构建 `astrbot-runtime-probe:local`
  `sha256:9936ffe871dace7906d3c0203c85dea92b5ea4224b1bcecf19417ac946ad61f8`（镜像用户
  `65532:65532`）和 package proxy
  `sha256:d1d99ba21a5f1739d862c66a9134536b7b634f00c8da28129ac6dea21936b69f`；显式运行 opt-in
  Docker integration，pass、依赖冲突、import failure、PyPI allowlist/站点与 metadata 阻断共
  `4 passed in 971.71s`，每个场景后 managed container/volume 均为空。
- **恶意软件工具**：真实 ClamAV `1.5.3/28062` 扫描 clean ZIP 与 EICAR ZIP，YARA 使用真实
  `yara-python` 隔离 subprocess；整组 `29 passed`。首次运行因数据库 `28058` 超过 72 小时返回
  `stale`，刷新并重载到 `28062` 后才通过，证明 freshness 不是被忽略的告警。
- **依赖安全**：requirements parser、install result、CycloneDX SBOM、版本化 advisory snapshot 和
  dependency stages 共 `41 passed`；真实 runtime pass fixture 同时生成并回收 SBOM sidecar。
- **PostgreSQL/Redis**：专用 PostgreSQL `16.14` 运行空库/P1 schema/重复迁移/checksum conflict、策略并发激活、
  auto-approve、评论/决策、分类优先级、历史、stable-risk/revoke、heartbeat/metrics，共
  `19 passed in 10.50s`；另用 Redis `7.4.9` 执行完整市场 store round trip，`1 passed`。
- **AstrBot 源码契约**：`ASTRBOT_SOURCE_PATH=/root/work/AstrBot` 的 contract/smoke 测试 `13 passed`；
  source audit 确认为 AstrBot `4.26.6`、commit `5d10e0d428b4`，pyproject 与运行时版本一致。
- **后端全量**：`ruff check` 与全目录 `ruff format --check` 通过；API `578 passed, 20 skipped`。`-rs`
  确认全部 skip 仅由显式 PostgreSQL/Redis、ClamAV、AstrBot source 和 Docker opt-in 产生，且均已在上述
  独立命令启用。OpenAPI、迁移、runtime Compose 和运维文档专项 `29 passed`；
  `artifacts + runtime-runner` Compose config 解析通过。
- **前端全量**：固定 npm `11.18.0` 下 Vitest `15 files / 47 tests passed`，新增 E2E 后 `vp check`
  验证 80 个格式文件、75 个 lint/type 文件，production build 完成（1277 modules）；production 与完整
  `npm audit` 均为 0 vulnerability。
- **浏览器视觉**：Playwright `1.61.1`/Chromium 覆盖 1440/1280 桌面和 390x844 移动端的 loaded、loading、
  empty、503 error、队列抽屉及第二 artifact 切换；6 张截图人工复核，所有状态 global horizontal overflow
  为 0，关键同级元素 overlap 为 0，无未处理 API fixture 或页面异常。fixture 固化在
  `apps/market-web/e2e/plugin-workbench.pw.ts`；浏览器还进入 files/diff/comments/history 并断言不可信 HTML
  保持纯文本。截图保存在 [visual-evidence](visual-evidence/)。
- **配置与泄漏检查**：双 profile Compose、运行进程边界、OpenAPI 角色过滤、迁移 checksum 和文档链接均由
  当前测试覆盖；production tracked files 的 private-key/AWS/GitHub/OpenAI token pattern 扫描无命中。
- **最终审查**：OMP CLI 的全部角色固定为 `new-api/grok-4.5`，未使用任何 5.2 模型。有效首轮指出视觉
  证据不可复跑与 checklist 未同步，修复后又指出 loading 缺显式断言、tabs/inert 覆盖和冷机 Chromium
  步骤；逐项本地复核并修复。最终 staged diff 复审返回 `NO CRITICAL OR WARNING`，结论再由上述源码、
  测试、容器和截图独立确认，模型不承担安全背书。

## 可复跑命令

以下命令均从仓库根目录执行；真实服务地址和测试凭据只通过临时环境变量注入，不写入证据文件。

```bash
# 业务状态机与 fail-visible 场景
uv run --project apps/api pytest -q \
  apps/api/tests/test_artifact_pipeline.py::test_full_p1_pipeline_publishes_immutable_version_and_gates_feed \
  apps/api/tests/test_artifact_routes.py::test_admin_can_idempotently_request_artifact_changes \
  apps/api/tests/test_artifact_resubmission.py \
  apps/api/tests/test_artifact_package_review.py::test_service_retries_429_but_does_not_retry_invalid_json \
  apps/api/tests/test_artifact_package_review.py::test_package_stage_failure_is_degraded_and_never_clean \
  apps/api/tests/test_artifact_orchestration.py::test_unavailable_tool_is_degraded_without_fake_success \
  apps/api/tests/test_runtime_findings.py apps/api/tests/test_stable_risk.py

# 当前 runtime 镜像与真实隔离门禁
docker compose --profile runtime-runner build runtime-probe-image runtime-package-proxy
docker compose --profile runtime-runner up -d runtime-package-proxy
ASTRBOT_RUNTIME_DOCKER_INTEGRATION=1 uv run --project apps/api pytest -q \
  apps/api/tests/test_runtime_docker_integration.py

# 真实 ClamAV、真实 YARA subprocess 与依赖安全
CLAMAV_TEST_HOST="$TASK21_CLAMAV_HOST" CLAMAV_TEST_PORT="$TASK21_CLAMAV_PORT" \
  uv run --project apps/api pytest -q apps/api/tests/test_artifact_malware.py
uv run --project apps/api pytest -q apps/api/tests/test_requirements_parser.py \
  apps/api/tests/test_install_probe.py apps/api/tests/test_dependency_sbom.py \
  apps/api/tests/test_dependency_advisory.py apps/api/tests/test_dependency_stages.py

# 真实 PostgreSQL/Redis 和 AstrBot source contract
ASTRBOT_TEST_DATABASE_URL="$TASK21_DATABASE_URL" uv run --project apps/api pytest -q \
  apps/api/tests/test_advanced_artifact_postgres.py apps/api/tests/test_schema_migrations.py
ASTRBOT_TEST_DATABASE_URL="$TASK21_DATABASE_URL" ASTRBOT_TEST_REDIS_URL="$TASK21_REDIS_URL" \
  uv run --project apps/api pytest -q \
  apps/api/tests/test_market.py::test_pg_redis_store_round_trip_from_env
ASTRBOT_SOURCE_PATH=/root/work/AstrBot uv run --project apps/api pytest -q \
  apps/api/tests/test_astrbot_source_contract.py apps/api/tests/test_smoke_probe.py
uv run --no-project python /root/.agents/skills/skill-astrbot-dev/scripts/audit_astrbot_source.py \
  --source /root/work/AstrBot --check

# 后端、契约、Compose 与前端全量
uv run --project apps/api ruff check apps/api
uv run --project apps/api ruff format --check apps/api
uv run --project apps/api pytest -q -rs
docker compose --profile artifacts --profile runtime-runner config --quiet
npx --yes npm@11.18.0 --prefix apps/market-web test
(cd apps/market-web && ./node_modules/.bin/vp check)
npx --yes npm@11.18.0 run build:web
apps/market-web/node_modules/.bin/playwright install chromium
WORKBENCH_EVIDENCE_DIR="$PWD/docs/specs/plugin-artifact-review-advanced/visual-evidence" \
  npx --yes npm@11.18.0 --prefix apps/market-web run test:e2e
```

视觉产物：

- [桌面数据态](visual-evidence/workbench-loaded-desktop.png)
- [移动数据态](visual-evidence/workbench-loaded-mobile.png)
- [桌面 loading](visual-evidence/workbench-loading-desktop.png)
- [桌面 empty](visual-evidence/workbench-empty-desktop.png)
- [桌面 error](visual-evidence/workbench-error-desktop.png)
- [移动队列抽屉](visual-evidence/workbench-drawer-mobile.png)
