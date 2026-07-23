# 插件制品审查与发布系统 - 设计文档

## Overview

本设计把当前“插件记录即提交、即审核、即发布”的模型拆为四个独立概念：

- `plugin`：稳定身份、所有权、仓库、市场展示和上架状态。
- `artifact`：一次不可变的 ZIP 或 GitHub commit 候选制品。
- `review`：针对 artifact 的预检、静态扫描、findings 和人工决策。
- `publication`：已发布对象及插件当前 artifact 指针。

P0/P1 不执行插件代码，也不接入 LLM。系统只完成安全接收、隔离、确定性检查、人工批准和不可变包发布。

### 设计目标

1. 仓库版本继续准确同步，但不会让新版本号指向旧 CDN 包。
2. artifact 创建后内容不可变，发布内容与过审内容 SHA-256 完全一致。
3. API、worker、对象存储和数据库之间的失败可以重试且不会破坏稳定版本。
4. 新功能通过独立模块接入，避免继续扩大现有大型 `main.py`、`store.py` 和前端插件 store。
5. 旧 listed 插件、GitHub 直连和现有插件源响应保持兼容。

### 核心不变量

- `repo_version` 表示仓库有效 metadata 中的最新版本。
- `published_version` 由 `current_artifact_id` 指向的 artifact 派生。
- `repo_version != published_version` 时，公开 feed 的 `version` 使用 `repo_version`，但 `download_url` 为空。
- 只有 `publication_status=published` 且 artifact 版本等于 `repo_version` 时，feed 才输出 CDN URL。
- 所有 artifact 状态变化均通过服务端领域动作完成，客户端不能任意 PATCH 状态。
- 任何重试不得改变 artifact 的 `archive_sha256`、`path_suffix`、`published_key` 或最终 URL。

## Architecture

### 总体架构

```mermaid
flowchart LR
    Web[Vue Market Web] --> API[FastAPI API]
    API --> PG[(PostgreSQL)]
    API --> Q[Private Quarantine Storage]
    Worker[Artifact Worker] --> PG
    Worker --> Q
    Worker --> Pub[Published Object Storage]
    Worker --> Mail[Email Provider]
    Pub --> CDN[CDN Domain]
    API --> Feed[/plugins.json]
    PG --> Feed
```

### 进程边界

#### FastAPI API

- 认证、所有权校验和请求限流。
- 流式接收 ZIP 或触发 GitHub commit 获取。
- 创建 plugin registration、artifact 和持久任务。
- 提供作者、管理员和公开 feed API。
- 不解压完整 artifact，不执行静态扫描，不执行插件代码，不负责发布包复制。

#### Artifact Worker

- 独立命令：`python -m app.artifacts.worker`。
- 从 PostgreSQL 领取 `precheck`、`static_scan`、`publish`、`revoke` 和 `outbox` 任务。
- 使用租约、幂等键和有限重试保证至少一次处理安全。
- P0/P1 不导入 artifact 中的 Python 模块，不安装 requirements。

#### PostgreSQL

- 保存插件、artifact、文件清单、review runs、findings、decisions、jobs 和 outbox。
- 是任务和发布状态的唯一权威来源。
- 使用事务和行锁保护人工决策与当前 artifact 指针切换。

#### Redis

- 保持现有用途，仅存登录会话。
- P0/P1 不把 Redis 作为 artifact 队列或审查状态来源。

#### 对象存储

- `quarantine`：私有、不可公开访问，保存原始 artifact 和受控提取出的文本内容。
- `published`：只保存已批准的不可变 ZIP。
- 本地开发使用文件系统后端；生产使用 S3 兼容后端，覆盖 R2。

### 部署拓扑

`docker-compose.yml` 增加 `artifact-worker` 服务，复用应用镜像但使用不同启动命令。使用本地存储时，API 与 worker 共享专用 `artifact-data` volume；使用 R2 时不共享业务文件卷。

artifact 功能默认关闭。只有 PostgreSQL、worker、隔离存储、发布存储和 CDN 基础地址均配置完成后才能启用。

## Components and Interfaces

### 后端模块边界

```text
apps/api/app/
├── artifacts/
│   ├── __init__.py
│   ├── schemas.py             # FastAPI/Pydantic 请求响应
│   ├── models.py              # 枚举、领域对象、状态转换
│   ├── repository.py          # Protocol、PG 实现、测试内存实现
│   ├── storage.py             # Local/S3 quarantine 与 publish 后端
│   ├── github_source.py       # repo/ref 解析、commit 固定、流式下载
│   ├── archive.py             # ZIP 安全检查、路径规范化、文件清单
│   ├── static_scan.py         # AST/requirements 基础规则
│   ├── service.py             # artifact 创建、查询和权限编排
│   ├── runtime.py             # API/worker 共用的 fail-closed 装配
│   ├── notifications.py       # outbox 领取、站内信和邮件编排
│   ├── mail.py                # worker 可复用的状态邮件适配器
│   ├── jobs.py                # 预检、扫描、发布、撤回和孤儿清理
│   ├── worker.py              # 独立 worker 入口
│   └── routes.py              # APIRouter
├── migrations/
│   ├── __init__.py
│   └── 20260710_001_artifact_foundation.sql
└── schema_migrations.py       # 版本化 SQL runner
```

`main.py` 只新增以下接入：

- 注册 artifact router 和 OpenAPI tag。
- 在应用启动后构造 artifact runtime。
- 在 setup 热切换 store 时重新绑定 artifact repository。
- 公共插件列表和 feed 构建前批量补充当前 artifact 发布信息。

### 版本化迁移

新增 `market_schema_migrations`：

- `version text primary key`
- `checksum text not null`
- `applied_at timestamptz not null`

迁移 runner 使用 PostgreSQL advisory lock，按文件名排序逐个执行。每个迁移在独立事务中完成，并校验已应用文件的 checksum，禁止静默修改历史迁移。

现有 `SCHEMA_SQL` 和兼容性 `ALTER TABLE` 暂时保留；artifact 及后续结构变更全部进入版本化迁移，不再追加到 `_ensure_schema()`。

### ArtifactRepository

```python
class ArtifactRepository(Protocol):
    async def create_artifact(...): ...
    async def get_artifact(artifact_id: str): ...
    async def list_user_artifacts(...): ...
    async def list_review_queue(...): ...
    async def replace_artifact_files(...): ...
    async def create_review_run(...): ...
    async def complete_review_run(...): ...
    async def replace_findings(...): ...
    async def claim_jobs(...): ...
    async def complete_job(...): ...
    async def fail_job(...): ...
    async def decide_artifact(...): ...
    async def publish_artifact(...): ...
```

生产实现从现有 `PgRedisMarketStore` 获取 asyncpg pool，不创建第二套业务数据库。测试使用独立 `InMemoryArtifactRepository`；未配置 PostgreSQL 时 artifact API 返回 `503 artifact_pipeline_unavailable`。

### ArtifactStorage

```python
class ArtifactStorage(Protocol):
    async def put_quarantine(stream, key, max_bytes, sha256): ...
    async def open_quarantine(key): ...
    async def put_text_content(key, content): ...
    async def publish_if_absent(source_key, published_key, expected_sha256): ...
    async def stat_published(key): ...
    async def delete_quarantine(key): ...
    async def revoke_published(key): ...
    def public_url(key: str) -> str: ...
```

实现：

- `LocalArtifactStorage`：开发和测试使用；根目录来自配置，不写入仓库。
- `S3ArtifactStorage`：使用成熟 S3 SDK，通过 endpoint URL 兼容 R2；同步 SDK 调用放入 `asyncio.to_thread()`，避免阻塞事件循环。

对象发布使用条件创建。目标已存在时：

- 若对象 metadata 中 SHA-256 与 artifact 一致，视为幂等成功。
- 若摘要不一致，产生 `published_key_conflict`，禁止覆盖。

### CDN key 生成

固定格式：

```text
{author_id}/{repo_name}/{version}/{plugin_name}-{version}-{suffix}.zip
```

规则：

- `author_id` 使用稳定公开用户 ID，不使用可变昵称或 GitHub login。
- `repo_name` 从规范化 GitHub URL 提取。
- `version` 使用 metadata 原始展示版本的路径安全形式，同时保存规范化比较版本。
- `plugin_name` 必须满足现有 `astrbot_plugin_` 规则。
- `suffix` 使用 `secrets.token_hex(5)` 生成 10 位十六进制串，只生成一次。
- URL 使用 `ARTIFACT_CDN_BASE_URL` 和逐段 URL 编码构造。

示例：

```text
https://cdn.example.com/123456/astrbot_plugin_demo/v1.2.0/
astrbot_plugin_demo-v1.2.0-a1b2c3d4e5.zip
```

### GitHubSourceClient

仅支持公开 GitHub 仓库：

1. 规范化并验证 `https://github.com/{owner}/{repo}`。
2. 复用现有仓库所有者校验。
3. 通过 GitHub commits API 把请求 ref 或默认分支解析为 40 位 commit SHA。
4. 从允许的 GitHub/codeload 主机流式下载该 commit 的 ZIP。
5. 下载过程中执行压缩包大小限制和 SHA-256 计算。
6. 记录 `source_ref` 与 `source_commit_sha`，后续步骤不再访问分支或标签。

用户 Token、系统 Token和授权头不得写入 job payload、review run 或日志。P1 不支持私有仓库、submodule、LFS 和任意 Git URL。

### ArchivePrechecker

不使用 `ZipFile.extractall()`。处理步骤：

1. 读取 central directory 并验证条目数量、声明大小、压缩比、加密标记和文件属性。
2. 对路径执行 Unicode NFC 规范化、分隔符统一、绝对路径和 `..` 检查。
3. 拒绝符号链接、设备文件、重复路径和不区分大小写冲突。
4. 允许 ZIP 根目录直接包含插件，或包含一个 GitHub archive wrapper 目录；最终统一为插件根相对路径。
5. 验证根目录 `main.py` 和唯一 metadata 文件。
6. 使用安全 YAML loader 解析 metadata，限制文档大小、嵌套深度和 alias。
7. 验证必填字段、插件名、repo、作者所有权和版本。
8. 逐文件流式计算 SHA-256、文本/二进制属性、行数和 tree hash。
9. 对允许展示的文本写入私有 `content_key`；大文件只保存清单，不保存全文副本。

确定性失败将 artifact 置为 `rejected`，创建 `precheck` run、结构化 finding 和系统 decision；系统故障将 artifact 置为 `processing_failed` 并按策略重试。

### StaticScanner

P1 使用 Python AST、tokenize 和 requirements 解析实现确定性规则，不执行源码：

- `PY001`：`eval`、`exec`、动态 compile。
- `PY002`：`os.system`、`subprocess`、Shell 调用。
- `PY003`：下载后执行、动态 import 和远程加载。
- `PY004`：常见凭据文件、环境变量批量读取和敏感目录访问。
- `PY005`：`marshal`、大量 base64/zlib 组合和明显混淆。
- `REQ001`：editable、本地路径、VCS 或直接 URL requirements。
- `REQ002`：无法解析或相互冲突的依赖声明。

规则命中生成 finding，不自动批准。所有正常完成静态扫描的 artifact 进入 `pending_review`；最高 finding severity 写入 artifact `risk_level`。

### JobRunner

任务类型：

- `precheck`
- `static_scan`
- `publish`
- `revoke`
- `outbox`
- `cleanup_orphan`

领取查询使用 `FOR UPDATE SKIP LOCKED`，设置 `lease_owner` 和 `lease_expires_at`。worker 定期续租；崩溃后其他 worker 可以领取过期任务。

只有系统故障重试；确定性校验失败直接完成任务并进入业务终态。所有 job 使用唯一 `idempotency_key`，后续任务在前一步数据库事务内创建。

### 人工决策服务

批准流程在事务中：

1. `SELECT ... FOR UPDATE` 锁定 artifact。
2. 校验管理员权限、artifact 为 `pending_review`、所有强制 runs 成功。
3. 校验 artifact version 等于插件当前 `repo_version`；不一致返回 `409 repo_version_changed`。
4. 写入 `review_decisions(action=approve)`。
5. 设置 `review_status=approved`。
6. 创建唯一 publish job；批准状态通知使用 artifact 级 dedupe key 写入 outbox。

拒绝流程写入不可变 decision、设置 `review_status=rejected` 并创建通知 outbox，不删除当前 artifact 或旧 CDN 对象。

作者不能批准自己的 artifact，即使其同时具有管理员角色；核心管理员可通过明确的紧急 override 接口处理，并留下不同 action 类型。

### Publisher

发布流程：

1. 读取 artifact、插件和批准 decision，确认状态仍可发布。
2. 生成或读取已持久化 `published_key`。
3. 将 quarantine 原始 ZIP 条件写入 published storage。
4. 校验公开对象大小和 SHA-256。
5. 在数据库事务中锁定插件与 artifact。
6. 再次确认 `artifact.version == plugin.repo_version`。
7. 设置 `publication_status=published`、保存 URL 和时间。
8. 原子更新 `market_plugins.current_artifact_id`。
9. 创建发布成功通知 outbox。

对象成功但数据库失败时，对象成为可识别孤儿；cleanup job 删除前重新读取 artifact，若该 key 已成为当前 published 对象则跳过，避免与发布重试竞态。

撤回请求在一个数据库事务中写入 decision、把 artifact 设为 `revoking`、立即把插件移出 feed 并创建唯一 revoke job。worker 随后删除公开对象并清空当前指针；失败保留指针和审计记录、进入 `revoke_failed`，管理员可从同一入口重试。发布和撤回的终态任务可以幂等重放，用于恢复“数据库已提交但 outbox 写入暂时失败”的情况。

站内通知由数据库唯一 dedupe key 保证不重复。SMTP/Cloudflare 这类外部邮件通道没有通用事务或幂等协议，因此进程恰好在“邮件已发送、outbox 尚未确认”之间崩溃时，邮件可能重复；邮件只承担站内查看提醒，站内通知和 artifact 状态仍是权威。若业务要求邮件严格去重，必须选择支持 provider idempotency key 的通道或增加独立投递回执协议，不能用 LLM 或进程内标记伪装成 exactly-once。

### FeedComposer

公共插件查询先批量加载当前 artifact，避免 N+1：

```text
feed_version = repo_version || legacy_version || published_version
feed_download_url =
  current_artifact.download_url
  if current_artifact is published
     and normalized(current_artifact.version) == normalized(feed_version)
  else ""
```

GitHub metadata sync：

- metadata `version` 写入 `market_plugins.repo_version`。
- 不再同步 metadata 中的 `download_url`。
- stars、logo、desc、tags 等现有非发布字段继续同步。
- 仓库版本变化会使 feed MD5 变化，并在新 artifact 发布前清空 CDN URL。

旧插件没有 current artifact 时，只有旧 `version` 与 `repo_version` 一致才保留既有 legacy `download_url`。

### 通知与 outbox

artifact 事务只写 `outbox_events`。worker 投递时：

- 调用现有 store 创建站内通知。
- 通过抽取后的通用 email service 发送邮件。
- 使用 outbox `dedupe_key` 防止重复。
- 邮件只包含插件名、版本、状态、简短原因和 `${WEB_URL}/plugin-workbench?artifact={id}`。

邮件不包含源码、finding evidence、对象 key、运行日志或 Token。

### API 接口

#### 插件注册和 artifact 创建

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/v1/plugins/registrations` | 登录用户 | 创建或返回本人插件稳定身份，不修改已存在稳定字段 |
| POST | `/v1/plugins/{plugin_id}/artifacts/upload` | 所有者 | multipart ZIP 上传并创建 artifact |
| POST | `/v1/plugins/{plugin_id}/artifacts/github` | 所有者 | 从 ref/default branch 固定 commit 并创建 artifact |

#### 作者查询

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/v1/me/artifacts` | 登录用户 | 分页查询本人 artifacts |
| GET | `/v1/artifacts/{artifact_id}` | 所有者/管理员 | artifact 摘要和状态 |
| GET | `/v1/artifacts/{artifact_id}/runs` | 所有者/管理员 | review runs |
| GET | `/v1/artifacts/{artifact_id}/findings` | 所有者/管理员 | 结构化 findings |

#### 管理员操作

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/v1/admin/artifacts` | 管理员 | 待审队列和过滤 |
| POST | `/v1/admin/artifacts/{artifact_id}/approve` | 管理员 | 批准并排队发布 |
| POST | `/v1/admin/artifacts/{artifact_id}/reject` | 管理员 | 拒绝，理由必填 |
| POST | `/v1/admin/artifacts/{artifact_id}/retry-publish` | 管理员 | 重试失败发布 |
| POST | `/v1/admin/plugins/{plugin_id}/revoke-release` | 管理员 | 移除 feed、撤回对象并清理 CDN |

所有列表使用 `limit/offset` 和状态过滤。文件内容接口延后到 P2；P1 API 不直接返回完整源码。

### OpenAPI 与权限

- 新增 `artifacts` 和 `reviews` tags。
- `openapi_filter.py` 为 public/user/admin/core_admin 配置相应可见 tag。
- 后端每个接口显式调用所有权或管理员权限函数。
- 手写 `docs/api/openapi.yaml` 与生成 schema 同步更新。

### 前端组件设计

P1 建立最小工作台壳，P2 在相同边界上扩展：

```text
src/
├── views/PluginWorkbench.vue
├── components/workbench/
│   ├── ArtifactSubmissionPanel.vue
│   ├── ArtifactListPanel.vue
│   ├── ArtifactReviewPanel.vue
│   └── ArtifactStatusBadge.vue
├── stores/artifacts.ts
└── types/artifacts.ts
```

组件职责：

- `PluginWorkbench.vue`：路由级组合面，只负责加载用户、同步 URL 过滤器和布局。
- `ArtifactSubmissionPanel`：选择 ZIP/GitHub、插件、ref，并发出 `created` 事件。
- `ArtifactListPanel`：展示作者版本历史或管理员待审队列，发出 `select` 事件。
- `ArtifactReviewPanel`：展示 runs、findings、版本差异状态和管理员决定按钮。
- `ArtifactStatusBadge`：纯展示状态映射。

`stores/artifacts.ts` 只保存列表摘要、选中 ID、详情和加载状态；过滤器保存在 route query。所有 mutation 通过 typed actions 完成，不把 ZIP、源码或大段 raw result 放入 Pinia。

路由新增：

```text
/plugin-workbench?artifact={artifact_id}&status={status}
```

路由使用 `meta.requiresAuth`；管理员操作仍依赖服务端 RBAC。个人中心和管理员插件页面只增加工作台入口，不复制审查逻辑。

## Data Models

### `market_plugins` 增量字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `repo_version` | text | 仓库 metadata 最新有效版本 |
| `current_artifact_id` | text nullable | 当前已发布 artifact FK |
| `category` | text | 当前有效分类，从 legacy metadata 回填 |
| `category_source` | text | `user/ai/reviewer`，P1 只产生 user/reviewer |

`published_version` 不冗余存储，由 current artifact 派生。

### `plugin_artifacts`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text PK | `artifact_{uuid}` |
| `plugin_id` | text FK | 所属插件 |
| `version` | text | metadata 原始版本 |
| `normalized_version` | text | 比较用规范版本 |
| `source_type` | text | `upload/github` |
| `source_repo` | text | 规范化 repo |
| `source_ref` | text | 用户请求 ref |
| `source_commit_sha` | text | GitHub 固定 commit |
| `archive_sha256` | text | 原始 ZIP 摘要 |
| `tree_sha256` | text | 规范化文件树摘要 |
| `size_bytes` | bigint | 压缩包大小 |
| `quarantine_key` | text unique | 私有对象 key |
| `published_key` | text unique nullable | 公开对象 key |
| `path_suffix` | text | 固定随机短串 |
| `download_url` | text | 最终 CDN URL |
| `review_status` | text | 审查状态 |
| `publication_status` | text | 发布状态 |
| `risk_level` | text | `none/low/medium/high/critical` |
| `base_artifact_id` | text nullable FK | 后续 diff 基线 |
| `submitted_by` | text nullable FK | 删除用户后保留审计 |
| `submitted_by_snapshot` | jsonb | 提交时 login/nickname |
| `suggested_category` | text | 后续 AI 使用 |
| `category_confidence` | numeric | 后续 AI 使用 |
| `category_reason` | text | 后续 AI 使用 |
| `rejection_code` | text | 结构化失败码 |
| `created_at/updated_at` | timestamptz | 时间 |
| `reviewed_at/published_at/revoked_at` | timestamptz nullable | 生命周期时间 |

约束与索引：

- 唯一 `(plugin_id, archive_sha256)`，实现内容幂等。
- 唯一 `published_key` 和 `download_url`。
- 部分唯一 `(plugin_id, normalized_version)` where `publication_status='published'`。
- 索引 `(plugin_id, created_at desc)`、`(review_status, created_at)`、`(publication_status, updated_at)`。

### `artifact_files`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text PK | 文件 ID |
| `artifact_id` | text FK cascade | artifact |
| `path` | text | 规范化相对路径 |
| `language` | text | 推断语言 |
| `mime_type` | text | MIME |
| `sha256` | text | 文件摘要 |
| `size_bytes` | bigint | 文件大小 |
| `line_count` | integer nullable | 文本行数 |
| `is_text` | boolean | 是否允许文本查看 |
| `content_key` | text nullable | 私有文本对象 key |
| `flags` | jsonb | executable/binary/generated 等 |

唯一 `(artifact_id, path)`。

### `review_runs`

字段包括：`id`、`artifact_id`、`type`、`status`、`attempt`、`ruleset_version`、`model`、`summary`、`raw_result`、`raw_result_key`、`error_code`、`started_at`、`completed_at`、`created_at`。

P1 type 仅使用 `precheck/static`，预留 `runtime/llm_package/llm_file/llm_summary`。

### `review_findings`

字段包括原计划字段，并增加：

- `fingerprint text`
- `rule_id text`
- `confidence numeric`
- `status text`：`open/accepted/resolved/false_positive`
- `metadata jsonb`

唯一 `(artifact_id, fingerprint)`。

### `review_decisions`

| 字段 | 说明 |
|---|---|
| `action` | `auto_reject/approve/reject/retry_publish/revoke/emergency_override` |
| `from_status/to_status` | 状态转换 |
| `reason` | 人工或系统理由 |
| `reviewer_user_id` | 可空 FK，系统决定为空 |
| `reviewer_nickname` | 快照 |
| `policy_version` | 决策策略版本 |
| `idempotency_key` | 唯一请求键 |
| `created_at` | 决策时间 |

### `artifact_jobs`

字段：`id`、`artifact_id`、`type`、`status`、`payload`、`attempts`、`max_attempts`、`available_at`、`lease_owner`、`lease_expires_at`、`idempotency_key`、`last_error_code`、`last_error`、`created_at`、`updated_at`、`completed_at`。

状态：`queued/running/succeeded/failed/cancelled`。

### `outbox_events`

字段：`id`、`event_type`、`aggregate_type`、`aggregate_id`、`recipient_user_id`、`payload`、`dedupe_key`、`status`、`attempts`、`available_at`、`delivered_at`、`last_error`、`created_at`。

### 状态机

```text
review_status:
quarantined -> prechecking -> scanning -> pending_review
prechecking -> rejected | processing_failed
scanning -> pending_review | processing_failed
pending_review -> approved | rejected | withdrawn
approved -> approved

publication_status:
unpublished -> publishing -> published
publishing -> publish_failed
publish_failed -> publishing
published -> revoking -> revoked | revoke_failed
revoke_failed -> revoking
```

状态转换由 repository 中带条件的 SQL 和服务层共同校验。

## Error Handling

### 错误分类

| 分类 | 示例 | 处理 |
|---|---|---|
| 用户输入错误 | 非 ZIP、非法 ref、非本人插件 | HTTP 400/403，不创建任务 |
| 确定性预检失败 | 路径穿越、缺 metadata | artifact rejected，不重试 |
| 确定性静态 finding | 危险调用 | pending_review，展示风险 |
| 外部暂时故障 | GitHub 5xx、对象存储超时 | job 重试，指数退避 |
| 配置错误 | 未配置 CDN/worker | HTTP 503，fail closed |
| 并发冲突 | 重复批准、repo_version 已变化 | HTTP 409，不重复发布 |
| 发布冲突 | key 已存在且摘要不同 | publish_failed，禁止覆盖 |
| 系统缺陷 | 未知异常 | processing_failed，记录安全错误摘要 |

### API 错误格式

保持现有 `{"error": "..."}` 外观，并为 artifact API 增加稳定 `code`、`artifact_id` 和可选 `retryable`：

```json
{
  "error": "Repository version changed during review",
  "code": "repo_version_changed",
  "artifact_id": "artifact_...",
  "retryable": false
}
```

不得把异常堆栈、对象 key、Token、源码或 SQL 暴露给客户端。

### 重试策略

- GitHub、对象存储和邮件：最多 3 次指数退避。
- 数据库连接失败：释放租约，由 worker 外层恢复。
- 预检和静态规则失败：不重试。
- publish/revoke 重试复用固定 key 和 path suffix。
- 超过重试次数后保留人工重试入口。

## Security Considerations

- 上传接口按用户和 IP 限流，并在读取过程中强制最大字节数。
- 所有路径段使用 allowlist 校验和逐段编码。
- ZIP 文件只通过受控流读取；不信任 CRC、Content-Length 或扩展名。
- YAML 禁止自定义 tag，限制 alias、深度和总节点数。
- 文件内容 API 在 P1 不开放，降低源码泄漏和 XSS 面。
- 所有文本在 Vue 中使用转义插值，不使用未清洗 `v-html`。
- S3/R2 凭据只从环境读取，不进入数据库公开设置。
- quarantine bucket 禁止公开访问；published bucket 只允许 worker 写入。
- 随机 suffix 只解决对象重名，不作为访问控制或安全令牌。
- worker 日志只记录 ID、阶段、规则码和安全截断后的异常。

## Testing Strategy

### 后端单元测试

- CDN key 生成、路径字符校验和固定 suffix。
- 版本规范化与 `repo_version/published_version` 比较。
- ZIP 路径穿越、绝对路径、符号链接、重复路径、大小写冲突。
- zip bomb、加密 ZIP、文件数/大小/深度限制。
- wrapper 根目录规范化、metadata 安全解析和必填字段。
- AST/requirements 各规则命中及 finding fingerprint 稳定性。
- job 租约、过期回收、重试和幂等。
- feed 在版本一致、不一致和 legacy 场景下的输出。

### 后端 API 测试

- 作者只能注册、提交和读取自己的 artifact。
- 管理员队列、批准、拒绝和发布重试权限。
- 并发或重复批准只生成一个 publish job。
- repo_version 改变后批准返回 409。
- 发布失败不改变 current artifact。
- 发布成功后 URL 符合约定路径且 feed 输出该 URL。
- 拒绝后 feed 使用 repo_version 并清空 CDN URL。
- 邮件/outbox payload 不含源码和 evidence。

### PostgreSQL 集成测试

- 迁移可在空库和现有 schema 上执行。
- 重复启动不会重复应用迁移。
- 修改已应用迁移 checksum 会失败。
- 部分唯一索引和 FK 行为正确。
- `FOR UPDATE SKIP LOCKED` 不会让两个 worker 领取同一 job。

### 存储测试

- Local backend 条件创建和摘要验证。
- S3 backend 使用 mock S3 或兼容测试容器验证 put/stat/revoke。
- 对象已存在且摘要一致时幂等成功，不一致时失败。
- orphan cleanup 不删除被引用对象。

### 前端测试

- `artifacts` Pinia store 对列表、详情和决定请求进行正确状态更新。
- 提交面板在 ZIP/GitHub 模式生成正确请求。
- 作者看不到管理按钮，管理员能够看到批准/拒绝命令。
- 异步操作使用 `flushPromises`，测试用户可见结果和 emitted events，不访问组件内部实现。
- 路由守卫把未登录用户引导到登录入口，并保留返回地址。

### 回归验证

- `npm test`
- `npm --prefix apps/market-web test`
- `npm run build:web`
- `uv run --project apps/api ruff check apps/api`
- `uv run --project apps/api ruff format --check apps/api`

## Rollout and Compatibility

1. 部署代码但保持 `ARTIFACTS_ENABLED=false`，应用数据库迁移。
2. 配置 quarantine、published storage、CDN 基础地址和 worker。
3. 启动 worker，验证 health 中 artifact 子系统状态。
4. 开启 artifact 功能并仅允许管理员测试账号提交。
5. 验证发布、版本不一致清空 URL、拒绝和撤回流程。
6. 开放普通作者使用。

旧插件处理：

- 不自动创建伪 artifact。
- `current_artifact_id` 保持空。
- GitHub 直连继续可用。
- legacy `download_url` 只有在版本一致时继续输出。
- 作者下一次提交 artifact 并过审后自动进入新发布模型。

旧 `/v1/plugins/submissions` 继续保留。新前端使用 `/v1/plugins/registrations` 和 artifact API；启用 artifact 流水线时，旧 `PLUGIN_AUTO_APPROVE_ENABLED` 不作用于 artifact。

## Design Decisions and Trade-offs

### 使用 PostgreSQL 队列而不是 Celery/RQ

当前预期吞吐较低，PostgreSQL 已是强依赖。`SKIP LOCKED` 可以提供足够的持久性和并发领取能力，同时避免新增 broker 和复杂运维。未来吞吐增长时可以替换 job transport，但数据库状态模型保持不变。

### 使用随机 suffix 而不是覆盖同版本对象

同一版本可能存在被拒绝后重新提交的不同 artifact。固定随机 suffix 保证对象路径不冲突；SHA-256 仍是内容身份和发布校验依据。发布 artifact 一旦成功永不覆盖。

### repo_version 与 published_version 分离

单一 `version` 无法同时准确描述 GitHub 最新代码和旧 CDN 包。分离后，仓库更新可及时反映；版本不一致时 CDN URL 为空，避免错误下载，同时保留 GitHub 直连。

### P1 不开放源码浏览

P1 只需要结构化 runs/findings 和人工决策。延后文件内容 API 可以减少权限、XSS、超大文件和行号映射复杂度，P2 再基于 `artifact_files.content_key` 增加工作台代码浏览。
