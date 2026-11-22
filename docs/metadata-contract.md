# 插件元数据读取约定

本约定与 AstrBot `4.28.0`（源码提交 `f66eeb224516f2680a19123752dad667b23570bc`）中的 `astrbot/core/star/star_manager.py`、`updater.py` 以及官方 [helloworld 插件模板元数据](https://github.com/Soulter/helloworld/blob/8bbec36ebe40846b24d779c9a168b4042470eb160/metadata.yaml) 保持对齐。

> [!NOTE]
> 本约定仅定义插件元数据文件的解析与表单预填行为；服务端运行时烟雾测试探针的 AstrBot 基线版本仍按 [plugin-review-operations.md](plugin-review-operations.md) 独立固定与升级。

## 元数据来源边界

- 提交页自动填充的插件名、展示名、描述、短描述、作者、标签、分类与社交链接仅从仓库根目录的 `metadata.yaml`（兼容 `metadata.yml`）读取。
- 选择 GitHub 仓库只用于确定仓库地址与核验权限，不会使用仓库名称、About 简介、Topics、Homepage 或 OAuth 用户名来补齐缺失的元数据字段。
- 元数据文件中未提供的字段在表单中保持为空；用户已手动编辑过的表单项不会被延迟到达的预取响应覆盖。
- 插件本体的多语言翻译资源位于 `.astrbot-plugin/i18n/<locale>.json`，而非 `metadata.yaml` 内的嵌套字段；提交表单预填仅解析元数据文件，不读取翻译资源补全字段，但后台 GitHub 元数据同步会采集其中的 `metadata.{display_name, desc, short_desc}` 并输出至 `/plugins.json` 的 `i18n` 字段供 AstrBot 市场展示。

## 字段解析与校验规则

- **`desc` 与别名兼容**：当 `desc` 键不存在时，自动兼容读取 `description`；如果 `desc` 键显式存在但为空，不使用别名覆盖。完整描述不作 120 字截断。
- **`display_name`**：可选字段。未填写时在表单、数据库与导出结果中均保持为空，不因展示名与描述内容相同而做替换；卡片展示时若缺少展示名，自动回退显示插件名 `name`。
- **`short_desc`**：可选字段，与完整描述 `desc` 独立保存。卡片摘要按 `short_desc → desc → 空` 顺序展示，不会把短描述写回完整描述字段。
- **YAML 解析**：完整支持多行文本、引号与注释，且只提取顶层键，避免将嵌套结构中的同名字段误判为插件顶层元数据。
- **预检必填项**：插件包预检要求 `name`、`desc`、`version`、`author` 均为非空字符串（`author` 不能包含 `/` 或 ASCII 控制字符），`repo` 用于核对市场仓库一致性，`display_name` 为可选项。
- **市场提交校验**：插件名前缀（`astrbot_plugin_*`）、`author` 字符合法性、`author/name` 全局不区分大小写唯一性、官方分类白名单与最大标签数规则仍在提交时统一校验；预填表单直接展示元数据文件中的原始名称，不用仓库名掩盖不符合规范的名称。
