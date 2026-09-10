# jp 全量部署记录（2026-09-10）

部署内容为 `5d25015` 基础上的当前工作区版本，包含移动端筛选/统计、公告首次提示、完整工作台，以及本次部署验证发现的修复。未提交或推送 Git。

## 运行位置

- 发布目录：`/opt/astrbot-market/releases/20260910-5d25015`，入口链接 `/opt/astrbot-market/current`。
- API：`astrbot-community-plugins.service`，监听 `127.0.0.1:8788`。
- 审核任务：`astrbot-artifact-worker.service`，独立 `astrbot-market-worker` 用户。
- 动态校验：`astrbot-runtime-runner.service`，独立 `astrbot-runtime` 用户，单任务并发。
- rootless Docker：`/run/user/1002/docker.sock`；用户服务已启用 linger。
- 环境文件：`/etc/astrbot-community-plugins/{api,artifact-worker,runtime-runner}.env`。worker 和 runner 不持有站点 OAuth 凭据；runner 使用仅能访问调度和心跳表的数据库角色。API 环境文件由 API 用户以 0600 持有，以兼容现有系统设置写入契约，其余环境文件为 root 0600。
- Artifact 根目录：`/1panel/1panel/www/market-artifacts`；仅 `published/` 映射到公网 `/packages/`，隔离包和源码不公开。
- runner 的 Artifact 输入根目录配置为上述根目录下的 `quarantine/`；结果目录为 `/var/lib/astrbot-runtime-results`，worker 只读，API 服务不可访问。
- 私有本机镜像仓库监听 `127.0.0.1:5057`。probe 固定为 `localhost:5057/astrbot-runtime-probe@sha256:0ad5e594cba28b01b25241b9f2b243b1e2d7c9439e8df415cf6c6dca9424c0ef`。

## 生效策略与验证

当前策略 `jp-auto-static-20260910` 已通过 validate 并激活：静态扫描、Diff、导入图、AstrBot 4.26.6 / Python 3.12 动态校验、ClamAV、YARA、PyPI 依赖漏洞检查。用户已授权自动审批：必需阶段全部成功、覆盖完整、仓库版本一致且没有达到 `low` 人工阈值的发现时自动批准；确定性阻断拒绝，其余风险或工具异常转人工。LLM 保持关闭。既有 Artifact 保留其固定策略。

- 前端 23 个测试文件、70 项测试通过；后端 587 项通过、20 项按环境跳过。
- 生产构建使用 `VITE_BASE_URL=https://plugins.eloina.cn`，91 个已上架插件均有 sitemap 和预渲染页面。
- jp 真实 rootless fixture：正常插件、依赖冲突、导入失败均符合预期，受管容器和卷清理完成。
- 验证 PyPI 代理允许依赖下载，阻断非白名单及私网/metadata 访问；smoke 无网络。
- 验证 API、worker、runner 三用户之间的隔离包、源码和结果文件读取契约。
- 公网验证移动端统计和标签布局；公告首次弹出、刷新不重复、手动查看；未登录工作台接口返回 401；未知路径返回真实 404。
- 验证 HTML、指纹资源、API、插件 feed、crawler 文件及发布包的 Cache-Control。`/v1/plugins` 沿用已存在的 `public, max-age=60` 例外；发布包使用 `public, max-age=300`。

本次额外修复：切换版本时禁用旧详情决策；公告不进入预渲染快照；rootless staging 容器仅增加 FOWNER 以规范化复制文件权限，插件安装和 smoke 仍 cap-drop ALL；准备失败先清理容器再删卷；本地对象与运行结果按共享服务组提供读取权限。

## 回滚材料

- 原部署目录 `/home/ubuntu/Astrbot_Community_Plugins` 未覆盖；原 `astrbot-market.service` 已停止并禁用，但 unit 保留。
- 数据库备份：`/var/lib/astrbot-market/backups/20260910/market-before.dump`。四个 schema migration 已完成；代码回滚不应自动恢复数据库或删除新表。
- 原反代：`/1panel/1panel/www/sites/plugin/proxy/backups/20260910-full-release/root.conf`。
- 环境文件启用 runtime 前备份：`/etc/astrbot-community-plugins/backups/20260910-runtime/`。

需要回滚时，先停止新 worker/runner 领取任务，恢复旧 API 服务（8787）和上述反代配置，通过 `nginx -t` 后 reload，再停止新 API；依据实际数据变化另行决定数据库恢复。不要执行全局 Docker prune 或覆盖旧工作区改动。

## 静态扫描与自动审批追加部署

- ClamAV 1.5.3 使用官方 FreshClam 更新，`clamav-daemon.socket` 仅额外监听 `127.0.0.1:3310`。保留腾讯云主机防护，不设置白名单。病毒库和 OSV 快照超过 48 小时即不作为通过依据。
- YARA 使用 `yara-community-0f93570194a8-v1`：从 [Yara-Rules](https://github.com/Yara-Rules/rules) 选取并验证的 13 条规则，含 1 个 private helper。命中映射为人工复核风险，未引入宽泛的通用字符串规则。规则及许可证在 `/var/lib/astrbot-review-data/yara/`；版本固定，更新须重新审查规则并发布匹配的新策略。
- PyPI 漏洞数据来自 [OSV 官方导出](https://google.github.io/osv.dev/data/)，首份转换快照含 44,664 条记录。离散范围分别存储，显式预发布版本补充覆盖；无上游严重度时按人工复核分类处理，无法准确转换的 75 处范围保守转人工，不当作无漏洞。此快照不提供许可证审计。
- `astrbot-advisory-sync.timer` 每六小时更新快照，使用源文件的 Last-Modified 判断新鲜度；失败保留最后有效快照。成功更新后重启 worker 载入新快照。数据目录 `/var/lib/astrbot-review-data` 对 API unit 不可访问，API 使用 worker 脱敏心跳进行工具就绪校验。
- 后端 594 项测试通过、20 项环境集成测试跳过；服务器只使用干净 ZIP 验证 ClamAV/YARA，验证了 OSV 漏洞版本与修复边界、过期数据，以及完整/缺失/异常/版本变化/风险结果的审批路由。
- 本次曾将含 WebShell 特征字节的验证脚本 `/tmp/jp-static-verify.py` 放入服务器，触发腾讯云主机防护。已核对与本地脚本 SHA-256 一致并删除，未执行 PHP；临时目录和测试进程均无遗留。恶意特征正例仅在本地验证，禁止通过混淆测试内容、停用防护或白名单规避告警。
- 静态配置前备份位于 `/etc/astrbot-community-plugins/backups/20260910-static/`，自动开关前备份位于 `backups/20260910-auto-static/`，ClamAV 原配置位于 `/etc/clamav/backups/20260910-static/`。

暂停自动审批应停住 worker 并按固定策略语义处理在途任务、激活人工策略；当前路由实际使用 Artifact 固定的 `policy.routing.auto_approve`，不能仅依赖环境中的 `ARTIFACT_AUTO_APPROVE_ENABLED=false` 暂停已有自动策略任务。

## OAuth 命名空间与首页浏览

`20260910-namespace-waterfall` 已部署到 jp。数据库备份在 `/var/lib/astrbot-market/backups/20260910-namespace-waterfall/market-before.dump`，迁移 005 保留了全部 94 条插件身份记录（91 条已上架）。详情地址使用保存的 OAuth 用户名，旧单段地址返回 301；未知插件返回实际 404。完整前后端与审查服务均包含在该发布中。

首页新增可切换的瀑布流、分批追加和有限图标预载；标签可搜索、多选，“全部”清空选择；提交页仓库列表无感定时刷新。公网验证 320、390、1440px 无溢出，均可加载到全部 91 条，卡片没有重叠，旧链接和缓存分类正常。

该轮验证：后端 622 项通过（含 PostgreSQL 集成）、8 项按环境跳过；前端 79 项通过；94 个公开页面及其中 91 个插件页面通过标题、描述、H1、canonical、OG 图和 sitemap/预渲染检查。后续元数据读取规则见 [metadata-contract.md](metadata-contract.md)。

## 元数据来源修正

`20260910-metadata-contract` 已部署，当前入口仍为 `/opt/astrbot-market/current`。部署前数据库备份在 `/var/lib/astrbot-market/backups/20260910-metadata-contract/market-before.dump`；全部 94 条插件身份记录保留。

提交预填只读取元数据文件，缺失字段留空，移除仓库 About/topics、仓库名和账号名兜底。共享 YAML 解析支持 description 兼容别名、多行文本；展示名可选，短描述独立保存并用于卡片摘要。正文不再截为 120 字，已有真实展示名不会因与正文相同而被替换。

验证：后端全套 618 项通过、21 项按环境跳过，随后相关回归 101 项通过及 PostgreSQL 身份/可选展示名测试 3 项通过；前端全套 81 项通过，追加名称保真和摘要切换回归通过。94 个公开页面 SEO/预渲染检查通过。jp 使用实际模板仓库验证字段读取；公网前端用受控登录/元数据响应验证留空、切换仓库清除旧值和 320/390/1440px 布局。API、worker、runner 和三个扫描更新服务均为 active。

仓库排序追加发布 `20260910-repository-push-sort`：提交页仓库列表按 GitHub 最后推送时间倒序，分页与自动刷新采用相同顺序。后端 618 项测试通过、21 项按环境跳过。沿用已验证的前端构建；数据库备份位于 `/var/lib/astrbot-market/backups/20260910-repository-push-sort/market-before.dump`。
