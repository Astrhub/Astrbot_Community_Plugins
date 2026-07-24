<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { NButton, NEmpty, NIcon, NInput, NSelect, NSpin } from "naive-ui";
import {
  ArchiveOutline,
  CloudUploadOutline,
  CreateOutline,
  RefreshOutline,
} from "@vicons/ionicons5";
import FieldHint from "@/components/FieldHint.vue";
import { useExternalOpenConfirm } from "@/composables/useExternalOpenConfirm";
import { PLUGIN_CATEGORY_LABELS, PLUGIN_CATEGORY_OPTIONS } from "@/stores/plugins";
import type { Plugin, PluginCategory, PluginStatus } from "@/types";
import { resolvePluginLogoUrl, setDefaultPluginLogo } from "@/utils/github";

interface PluginDraft {
  display_name: string;
  desc: string;
  tags: string[];
  category: PluginCategory;
  social_link: string;
}

interface SavePluginPayload {
  plugin: Plugin;
  changes: PluginDraft;
}

interface StatusMeta {
  label: string;
  className: string;
}

const props = withDefaults(
  defineProps<{
    plugins?: Plugin[];
    loading?: boolean;
    busyIds?: Record<string, string>;
    maxTags?: number;
  }>(),
  {
    plugins: () => [],
    loading: false,
    busyIds: () => ({}),
    maxTags: 8,
  },
);

const emit = defineEmits<{
  refresh: [];
  savePlugin: [payload: SavePluginPayload];
  requestList: [plugin: Plugin];
  unlist: [plugin: Plugin];
}>();

const { confirmExternalOpen } = useExternalOpenConfirm();
const drafts = reactive<Record<string, PluginDraft>>({});
const editorOpen = reactive<Record<string, boolean>>({});

const statusMeta: Record<PluginStatus, StatusMeta> = {
  listed: { label: "已上架", className: "status--listed" },
  pending: { label: "待审查", className: "status--pending" },
  unlisted: { label: "已下架", className: "status--unlisted" },
};

const categoryOptions = [
  ...PLUGIN_CATEGORY_OPTIONS,
  { label: PLUGIN_CATEGORY_LABELS.other, value: "other" as PluginCategory },
];

const tagOptions = computed(() => {
  const tags = new Set<string>();
  for (const plugin of props.plugins) {
    for (const tag of plugin.tags || []) tags.add(tag);
  }
  for (const draft of Object.values(drafts)) {
    for (const tag of draft.tags) tags.add(tag);
  }
  return Array.from(tags)
    .sort((left, right) => left.localeCompare(right, "zh-CN"))
    .map((tag) => ({ label: tag, value: tag }));
});

watch(
  () => props.plugins,
  (plugins) => {
    const activeKeys = new Set(plugins.map(pluginKey));
    for (const plugin of plugins) {
      const key = pluginKey(plugin);
      if (!editorOpen[key]) drafts[key] = createDraft(plugin);
    }
    for (const key of Object.keys(drafts)) {
      if (!activeKeys.has(key)) delete drafts[key];
    }
    for (const key of Object.keys(editorOpen)) {
      if (!activeKeys.has(key)) delete editorOpen[key];
    }
  },
  { immediate: true },
);

function pluginKey(plugin: Plugin): string {
  return String(plugin.id);
}

function createDraft(plugin: Plugin): PluginDraft {
  return {
    display_name: String(plugin.display_name || ""),
    desc: String(plugin.desc || ""),
    tags: cleanTags(plugin.tags),
    category: plugin.category || "other",
    social_link: String(plugin.social_link || ""),
  };
}

function cleanTags(tags: unknown): string[] {
  if (!Array.isArray(tags)) return [];
  return Array.from(new Set(tags.map((tag) => String(tag || "").trim()).filter(Boolean)));
}

function pluginTitle(plugin: Plugin): string {
  return plugin.display_name || plugin.name || String(plugin.id);
}

function pluginVersion(plugin: Plugin): string {
  const version = String(plugin.version || "1.0.0");
  return version.startsWith("v") ? version : `v${version}`;
}

function pluginStatus(plugin: Plugin): StatusMeta {
  const status = plugin.status || "pending";
  return statusMeta[status] || { label: status, className: "status--pending" };
}

function busyAction(plugin: Plugin): string {
  return props.busyIds[pluginKey(plugin)] || "";
}

function isBusy(plugin: Plugin): boolean {
  return Boolean(busyAction(plugin));
}

function toggleEditor(plugin: Plugin): void {
  const key = pluginKey(plugin);
  if (!drafts[key]) drafts[key] = createDraft(plugin);
  editorOpen[key] = !editorOpen[key];
}

function cancelEditor(plugin: Plugin): void {
  const key = pluginKey(plugin);
  drafts[key] = createDraft(plugin);
  editorOpen[key] = false;
}

function savePlugin(plugin: Plugin): void {
  const key = pluginKey(plugin);
  const draft = drafts[key] || createDraft(plugin);
  emit("savePlugin", {
    plugin,
    changes: {
      display_name: draft.display_name.trim(),
      desc: draft.desc.trim(),
      tags: cleanTags(draft.tags).slice(0, props.maxTags),
      category: draft.category || "other",
      social_link: draft.social_link.trim(),
    },
  });
  editorOpen[key] = false;
}
</script>

<template>
  <section class="plugin-manager" aria-label="我的插件">
    <div class="plugin-toolbar">
      <span class="plugin-count">{{ plugins.length }} 个插件</span>
      <n-button
        quaternary
        circle
        size="small"
        :loading="loading"
        aria-label="刷新插件列表"
        @click="emit('refresh')"
      >
        <template #icon>
          <n-icon><refresh-outline /></n-icon>
        </template>
      </n-button>
    </div>

    <n-spin :show="loading">
      <n-empty v-if="plugins.length === 0" description="你还没有可管理的插件" />

      <div v-else class="plugin-list">
        <article v-for="plugin in plugins" :key="plugin.id" class="pm-row">
          <div class="pm-head">
            <img
              :src="resolvePluginLogoUrl(plugin)"
              :alt="`${pluginTitle(plugin)} 插件图标`"
              class="pm-logo"
              width="38"
              height="38"
              @error="setDefaultPluginLogo"
            />

            <div class="pm-who">
              <div class="pm-title">
                <h2>{{ pluginTitle(plugin) }}</h2>
                <span class="status-tag" :class="pluginStatus(plugin).className">
                  {{ pluginStatus(plugin).label }}
                </span>
              </div>
              <p class="pm-sub">
                <span>{{ plugin.name }}</span>
                <span aria-hidden="true">·</span>
                <span>{{ pluginVersion(plugin) }}</span>
                <template v-if="plugin.repo">
                  <span aria-hidden="true">·</span>
                  <button
                    type="button"
                    class="repo-link"
                    @click="confirmExternalOpen(String(plugin.repo))"
                  >
                    仓库 ↗
                  </button>
                </template>
              </p>
            </div>

            <div class="pm-actions">
              <router-link
                class="pm-action-link"
                :to="{ name: 'PluginDetails', params: { name: plugin.id } }"
              >
                查看
              </router-link>
              <n-button
                secondary
                size="small"
                :disabled="isBusy(plugin)"
                @click="toggleEditor(plugin)"
              >
                <template #icon>
                  <n-icon><create-outline /></n-icon>
                </template>
                {{ editorOpen[pluginKey(plugin)] ? "收起" : "编辑" }}
              </n-button>
              <n-button
                v-if="plugin.status === 'listed'"
                secondary
                size="small"
                type="warning"
                :loading="busyAction(plugin) === 'unlist'"
                :disabled="isBusy(plugin) && busyAction(plugin) !== 'unlist'"
                @click="emit('unlist', plugin)"
              >
                <template #icon>
                  <n-icon><archive-outline /></n-icon>
                </template>
                下架
              </n-button>
              <n-button v-else-if="plugin.status === 'pending'" secondary size="small" disabled>
                审查中
              </n-button>
              <n-button
                v-else
                secondary
                size="small"
                type="primary"
                :loading="busyAction(plugin) === 'request'"
                :disabled="isBusy(plugin) && busyAction(plugin) !== 'request'"
                @click="emit('requestList', plugin)"
              >
                <template #icon>
                  <n-icon><cloud-upload-outline /></n-icon>
                </template>
                申请上架
              </n-button>
            </div>
          </div>

          <form
            v-if="editorOpen[pluginKey(plugin)] && drafts[pluginKey(plugin)]"
            class="pm-editor"
            @submit.prevent="savePlugin(plugin)"
          >
            <div class="field">
              <div class="field-label">
                <label :for="`display-name-${pluginKey(plugin)}`">展示名称</label>
                <field-hint content="给用户看的友好名称。" />
              </div>
              <n-input
                :id="`display-name-${pluginKey(plugin)}`"
                v-model:value="drafts[pluginKey(plugin)].display_name"
                placeholder="Group Welcome"
                maxlength="120"
                show-count
              />
            </div>

            <div class="field">
              <div class="field-label">
                <label :for="`description-${pluginKey(plugin)}`">描述</label>
                <field-hint content="插件简介，会显示在卡片与详情页。" />
              </div>
              <n-input
                :id="`description-${pluginKey(plugin)}`"
                v-model:value="drafts[pluginKey(plugin)].desc"
                type="textarea"
                placeholder="一句话说明这个插件能做什么"
                :autosize="{ minRows: 2, maxRows: 5 }"
                maxlength="1000"
                show-count
              />
            </div>

            <div class="field">
              <div class="field-label">
                <label>标签</label>
                <field-hint :content="`最多 ${maxTags} 个，输入后按回车创建标签。`" />
              </div>
              <n-select
                v-model:value="drafts[pluginKey(plugin)].tags"
                multiple
                filterable
                tag
                :max="maxTags"
                :max-tag-count="4"
                :options="tagOptions"
                placeholder="工具、联网搜索"
              />
            </div>

            <div class="field">
              <div class="field-label">
                <label>分类</label>
                <field-hint content="选择最贴近插件用途的官方分类。" />
              </div>
              <n-select
                v-model:value="drafts[pluginKey(plugin)].category"
                :options="categoryOptions"
                placeholder="生活实用"
              />
            </div>

            <div class="field">
              <div class="field-label">
                <label :for="`social-link-${pluginKey(plugin)}`">作者主页</label>
                <field-hint content="可选，填写你的 GitHub 或其他公开社交主页。" />
              </div>
              <n-input
                :id="`social-link-${pluginKey(plugin)}`"
                v-model:value="drafts[pluginKey(plugin)].social_link"
                placeholder="https://github.com/you"
                maxlength="500"
              />
            </div>

            <div class="editor-actions">
              <n-button
                type="primary"
                attr-type="submit"
                size="small"
                :loading="busyAction(plugin) === 'save'"
                :disabled="isBusy(plugin) && busyAction(plugin) !== 'save'"
              >
                保存修改
              </n-button>
              <n-button
                tertiary
                size="small"
                :disabled="isBusy(plugin)"
                @click="cancelEditor(plugin)"
              >
                取消
              </n-button>
            </div>
          </form>
        </article>
      </div>
    </n-spin>
  </section>
</template>

<style scoped>
.plugin-manager {
  min-width: 0;
}

.plugin-toolbar {
  min-height: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
}

.plugin-list {
  border-top: 1px solid var(--border-base);
}

.pm-row {
  padding: 16px 0;
  border-bottom: 1px solid var(--border-base);
}

.pm-head {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 14px;
}

.pm-logo {
  width: 38px;
  height: 38px;
  flex: none;
  padding: 3px;
  object-fit: contain;
  background: var(--logo-bg);
  border: 1px solid var(--logo-border);
  border-radius: 8px;
}

.pm-who {
  min-width: 0;
  flex: 1;
}

.pm-title {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}

.pm-title h2 {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--text-primary);
  font-size: 14.5px;
  font-weight: 650;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-tag {
  flex: none;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.3;
  border: 1px solid currentColor;
  border-radius: 999px;
}

.status--listed {
  color: var(--success-color);
}

.status--pending {
  color: var(--metric-comment);
}

.status--unlisted {
  color: var(--accent-amber);
}

.pm-sub {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 4px 0 0;
  overflow: hidden;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pm-sub > span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.repo-link {
  padding: 0;
  color: var(--primary-color);
  font: inherit;
  white-space: nowrap;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.repo-link:hover,
.repo-link:focus-visible {
  text-decoration: underline;
  outline: 0;
}

.pm-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.pm-action-link {
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  padding: 0 11px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 500;
  line-height: 1;
  text-decoration: none;
  border: 1px solid var(--border-base);
  border-radius: 3px;
}

.pm-action-link:hover,
.pm-action-link:focus-visible {
  color: var(--primary-color);
  border-color: var(--border-hover);
  outline: 0;
}

.pm-editor {
  margin-top: 14px;
  padding: 2px 18px 14px;
  background: color-mix(in srgb, var(--primary-color) 6%, var(--bg-card));
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.field {
  padding: 12px 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border-base) 72%, transparent);
}

.field-label {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 7px;
  color: var(--text-secondary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11.5px;
}

.editor-actions {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}

@media (max-width: 760px) {
  .pm-head {
    display: grid;
    grid-template-columns: 38px minmax(0, 1fr);
    align-items: start;
  }

  .pm-actions {
    grid-column: 2;
    flex-wrap: wrap;
  }
}

@media (max-width: 480px) {
  .pm-actions {
    grid-column: 1 / -1;
  }

  .pm-editor {
    padding-inline: 12px;
  }
}
</style>
