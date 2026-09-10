# 插件元数据读取

核对来源：用户指定的 `/root/work/AstrBot`，版本 4.28.0、提交 `f66eeb224516f2680a19123752dad667b23570bc`；以及 [helloworld 模板元数据](https://github.com/Soulter/helloworld/blob/8bbec36ebe40846b24d779c9a168b042470eb160/metadata.yaml)。此次没有升级服务器固定的 AstrBot 动态审查版本。

提交页的插件名、展示名、描述、短描述、作者、标签、分类和社交链接只从 `metadata.yaml` 读取（兼容 `metadata.yml`）。GitHub 仓库选择只负责确定访问的仓库，不使用仓库名称、About、topics、主页或 OAuth 用户名补齐元数据字段。缺失字段留空；已手动修改的字段不会被迟到的读取响应覆盖。

读取规则与 `astrbot/core/star/star_manager.py`、`updater.py` 对齐：

- `desc` 键不存在时兼容 `description`；显式空的 `desc` 不被别名覆盖。
- `display_name` 可选。填写、存储和导出的字段保持为空，不因其与描述相同而替换内容。卡片标题可显示实际插件名。
- `short_desc` 可选，独立于完整描述保存。仅在卡片展示时使用 `short_desc → desc → 空`，不把短描述写回完整描述字段。
- 正确解析 YAML 多行文本、引号和注释；只读取顶层字段，不把嵌套同名键当成插件字段；描述不再静默截为 120 字。
- 制品预检的核心必填字段为非空字符串 `name`、`desc`、`version`、`author`。`repo` 仍作为市场的仓库一致性要求，`display_name` 不再作为必填项。

市场的插件名前缀、分类和标签数量规则仍在提交时校验。预填时展示文件中实际的名称，不用选中的仓库名掩盖需要修改的名称。

本体的翻译资源来自 `.astrbot-plugin/i18n/<locale>.json`，不是 metadata.yaml 内的嵌套字段；这次提交表单读取不从翻译资源合成缺失字段。
