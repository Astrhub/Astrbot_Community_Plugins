<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { NButton, NIcon, NInput, NModal, NSelect, NSwitch } from "naive-ui";
import { ArchiveOutline, CloudUploadOutline } from "@vicons/ionicons5";
import FieldHint from "@/components/FieldHint.vue";
import GitProviderIcon from "@/components/GitProviderIcon.vue";
import { useExternalOpenConfirm } from "@/composables/useExternalOpenConfirm";
import { PLUGIN_CATEGORY_LABELS, PLUGIN_CATEGORY_OPTIONS } from "@/stores/plugins";
import type { Plugin, PluginCategory, PluginStatus } from "@/types";
import {
  detectGitProvider,
  getGitProviderLabel,
  isGithubRepoUrl,
  resolvePluginLogoUrl,
  setDefaultPluginLogo,
} from "@/utils/github";
import { pluginDetailPath } from "@/utils/pluginRoute";

export interface PluginDraft {
  display_name: string;
  desc: string;
  tags: string[];
  category: PluginCategory;
  social_link: string;
}

export interface SavePluginPayload {
  plugin: Plugin;
  changes: PluginDraft;
}

interface StatusMeta {
  label: string;
  className: string;
}

const props = withDefaults(
  defineProps<{
    show: boolean;
    plugin: Plugin | null;
    availableTags?: string[];
    maxTags?: number;
    busyAction?: string;
    isAdminUser?: boolean;
    apiBaseUrl?: string;
  }>(),
  {
    availableTags: () => [],
    maxTags: 8,
    busyAction: "",
    isAdminUser: false,
    apiBaseUrl: "",
  },
);

const emit = defineEmits<{
  "update:show": [value: boolean];
  save: [payload: SavePluginPayload];
  toggleCdn: [payload: { plugin: Plugin; enabled: boolean; whitelist?: boolean }];
  unlist: [plugin: Plugin];
  requestList: [plugin: Plugin];
}>();

const { confirmExternalOpen } = useExternalOpenConfirm();

const statusMeta: Record<PluginStatus, StatusMeta> = {
  listed: { label: "已上架", className: "status--listed" },
  pending: { label: "待审查", className: "status--pending" },
  unlisted: { label: "已下架", className: "status--unlisted" },
};

const categoryOptions = [
  ...PLUGIN_CATEGORY_OPTIONS,
  { label: PLUGIN_CATEGORY_LABELS.other, value: "other" as PluginCategory },
];

const draft = reactive<PluginDraft>({
  display_name: "",
  desc: "",
  tags: [],
  category: "other",
  social_link: "",
});

let initializedPluginId = "";

function cleanTags(tags: unknown): string[] {
  if (!Array.isArray(tags)) return [];
  return Array.from(new Set(tags.map((tag) => String(tag || "").trim()).filter(Boolean)));
}

function resetDraft(plugin: Plugin): void {
  draft.display_name = String(plugin.display_name || "");
  draft.desc = String(plugin.desc || "");
  draft.tags = cleanTags(plugin.tags);
  draft.category = plugin.category || "other";
  draft.social_link = String(plugin.social_link || "");
}

watch(
  [() => props.show, () => props.plugin],
  ([show, plugin]) => {
    if (!show || !plugin) {
      initializedPluginId = "";
      return;
    }
    const id = String(plugin.id);
    if (id !== initializedPluginId) {
      resetDraft(plugin);
      initializedPluginId = id;
    }
  },
  { immediate: true },
);

const isBusy = computed(() => Boolean(props.busyAction));
const pluginKey = computed(() => (props.plugin ? String(props.plugin.id) : ""));
const pluginTitle = computed(() => {
  if (!props.plugin) return "编辑插件";
  return props.plugin.display_name || props.plugin.name || String(props.plugin.id);
});
const pluginVersion = computed(() => {
  const version = String(props.plugin?.version || "1.0.0");
  return version.startsWith("v") ? version : `v${version}`;
});
const pluginStatus = computed<StatusMeta>(() => {
  const status = props.plugin?.status || "pending";
  return statusMeta[status] || { label: status, className: "status--pending" };
});
const detailPath = computed(() =>
  props.plugin && props.plugin.status === "listed" ? pluginDetailPath(props.plugin) : null,
);
const repoProvider = computed(() =>
  props.plugin
    ? props.plugin.repo_provider || detectGitProvider(props.plugin.repo) || "github"
    : "github",
);
const tagOptions = computed(() => {
  const tags = new Set<string>();
  for (const tag of props.availableTags) {
    if (tag) tags.add(tag);
  }
  for (const tag of props.plugin?.tags || []) {
    if (tag) tags.add(tag);
  }
  for (const tag of draft.tags) {
    if (tag) tags.add(tag);
  }
  return Array.from(tags)
    .sort((left, right) => left.localeCompare(right, "zh-CN"))
    .map((tag) => ({ label: tag, value: tag }));
});

function handleShowUpdate(value: boolean): void {
  if (!value && isBusy.value) return;
  emit("update:show", value);
}

function closeModal(): void {
  if (isBusy.value) return;
  emit("update:show", false);
}

function submitForm(): void {
  if (!props.plugin || isBusy.value) return;
  emit("save", {
    plugin: props.plugin,
    changes: {
      display_name: draft.display_name.trim(),
      desc: draft.desc.trim(),
      tags: cleanTags(draft.tags).slice(0, props.maxTags),
      category: draft.category || "other",
      social_link: draft.social_link.trim(),
    },
  });
}

function handleToggleCdn(enabled: boolean, whitelist?: boolean): void {
  if (!props.plugin || isBusy.value) return;
  emit("toggleCdn", {
    plugin: props.plugin,
    enabled,
    ...(whitelist === undefined ? {} : { whitelist }),
  });
}
</script>

<template>
  <n-modal
    :show="show && Boolean(plugin)"
    preset="card"
    :bordered="false"
    :mask-closable="!isBusy"
    :closable="!isBusy"
    :close-on-esc="!isBusy"
    class="plugin-edit-modal"
    style="width: min(640px, calc(100vw - 32px))"
    @update:show="handleShowUpdate"
  >
    <template #header>
      <div v-if="plugin" class="modal-head">
        <img
          :src="resolvePluginLogoUrl(plugin, props.apiBaseUrl)"
          :alt="`${pluginTitle} 插件图标`"
          class="modal-logo"
          width="40"
          height="40"
          referrerpolicy="no-referrer"
          @error="setDefaultPluginLogo"
        />
        <div class="modal-identity">
          <div class="modal-title-row">
            <span class="modal-title">{{ pluginTitle }}</span>
            <span class="status-tag" :class="pluginStatus.className">
              {{ pluginStatus.label }}
            </span>
          </div>
          <p class="modal-sub">
            <span>{{ plugin.name }}</span>
            <span aria-hidden="true">·</span>
            <span>{{ pluginVersion }}</span>
            <template v-if="plugin.repo">
              <span aria-hidden="true">·</span>
              <button
                type="button"
                class="repo-link"
                @click="confirmExternalOpen(String(plugin.repo))"
              >
                <git-provider-icon :provider="repoProvider" :size="12" />
                {{ getGitProviderLabel(repoProvider) }} 仓库 ↗
              </button>
            </template>
          </p>
        </div>
      </div>
    </template>

    <div v-if="plugin" class="modal-body">
      <form id="plugin-edit-form" class="edit-form" @submit.prevent="submitForm">
        <div class="field">
          <div class="field-label">
            <label :for="`edit-display-name-${pluginKey}`">展示名称</label>
            <field-hint content="给用户看的友好名称。" />
          </div>
          <n-input
            v-model:value="draft.display_name"
            :input-props="{ id: `edit-display-name-${pluginKey}` }"
            placeholder="Group Welcome"
            maxlength="120"
            show-count
            :disabled="isBusy"
          />
        </div>

        <div class="field">
          <div class="field-label">
            <label :for="`edit-description-${pluginKey}`">描述</label>
            <field-hint content="插件简介，会显示在卡片与详情页。" />
          </div>
          <n-input
            v-model:value="draft.desc"
            :input-props="{ id: `edit-description-${pluginKey}` }"
            type="textarea"
            placeholder="一句话说明这个插件能做什么"
            :autosize="{ minRows: 3, maxRows: 5 }"
            maxlength="1000"
            show-count
            :disabled="isBusy"
          />
        </div>

        <div class="field-grid">
          <div class="field">
            <div class="field-label">
              <label>分类</label>
              <field-hint content="选择最贴近插件用途的官方分类。" />
            </div>
            <n-select
              v-model:value="draft.category"
              :options="categoryOptions"
              placeholder="生活实用"
              :disabled="isBusy"
            />
          </div>

          <div class="field">
            <div class="field-label">
              <label :for="`edit-social-link-${pluginKey}`">作者主页</label>
              <field-hint content="可选，填写你的 GitHub 或其他公开社交主页。" />
            </div>
            <n-input
              v-model:value="draft.social_link"
              :input-props="{ id: `edit-social-link-${pluginKey}` }"
              placeholder="https://github.com/you"
              maxlength="500"
              :disabled="isBusy"
            />
          </div>
        </div>

        <div class="field">
          <div class="field-label">
            <label>标签</label>
            <field-hint :content="`最多 ${maxTags} 个，输入后按回车创建标签。`" />
          </div>
          <n-select
            v-model:value="draft.tags"
            multiple
            filterable
            tag
            :max="maxTags"
            :max-tag-count="4"
            :options="tagOptions"
            placeholder="工具、联网搜索"
            :disabled="isBusy"
          />
        </div>
      </form>

      <section class="cdn-panel" aria-label="分发与 CDN 设置">
        <div class="cdn-controls">
          <div class="cdn-switch-item">
            <span :id="`modal-cdn-label-${pluginKey}`" class="cdn-label">社区源 CDN</span>
            <n-switch
              :value="plugin.cdn_enabled === true"
              :aria-labelledby="`modal-cdn-label-${pluginKey}`"
              :disabled="isBusy"
              :loading="busyAction === 'cdn'"
              @update:value="(enabled: boolean) => handleToggleCdn(enabled)"
            />
          </div>
          <div v-if="isAdminUser && plugin.cdn_enabled === true" class="cdn-switch-item">
            <span :id="`modal-cdn-wl-label-${pluginKey}`" class="cdn-label">白名单直发</span>
            <n-switch
              :value="plugin.cdn_whitelist === true"
              :aria-labelledby="`modal-cdn-wl-label-${pluginKey}`"
              :disabled="isBusy"
              :loading="busyAction === 'cdn'"
              @update:value="(whitelist: boolean) => handleToggleCdn(true, whitelist)"
            />
          </div>
        </div>
        <p class="cdn-note">
          开启后每次新版本都会自动送审，通过前不提供该版本的 CDN 链接；关闭后仍可通过原 Git
          仓库安装，不影响插件上架。
          <template
            v-if="isAdminUser && plugin.cdn_enabled === true && plugin.cdn_whitelist === true"
          >
            当前已开启管理员白名单直发，新版本完成基础预检后将跳过审查直接发布。
          </template>
          <template
            v-if="plugin.repo && !isGithubRepoUrl(plugin.repo) && plugin.cdn_enabled !== true"
          >
            非 GitHub 仓库关闭 CDN 后，需要 AstrBot v4.27.0 及以上版本才能直接从仓库安装。
          </template>
        </p>
      </section>
    </div>

    <template #footer>
      <div v-if="plugin" class="modal-footer">
        <div class="modal-footer__left">
          <router-link
            v-if="detailPath"
            class="modal-detail-link"
            :to="detailPath"
            @click="closeModal"
          >
            查看详情
          </router-link>
          <n-button
            v-if="plugin.status === 'listed'"
            secondary
            size="small"
            type="warning"
            :loading="busyAction === 'unlist'"
            :disabled="isBusy && busyAction !== 'unlist'"
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
            :loading="busyAction === 'request'"
            :disabled="isBusy && busyAction !== 'request'"
            @click="emit('requestList', plugin)"
          >
            <template #icon>
              <n-icon><cloud-upload-outline /></n-icon>
            </template>
            申请上架
          </n-button>
        </div>

        <div class="modal-footer__right">
          <n-button tertiary size="small" :disabled="isBusy" @click="closeModal">取消</n-button>
          <n-button
            type="primary"
            size="small"
            :loading="busyAction === 'save'"
            :disabled="isBusy && busyAction !== 'save'"
            @click="submitForm"
          >
            保存修改
          </n-button>
        </div>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.modal-head {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.modal-logo {
  width: 40px;
  height: 40px;
  flex: none;
  padding: 3px;
  object-fit: contain;
  background: var(--logo-bg);
  border: 1px solid var(--logo-border);
  border-radius: 8px;
}

.modal-identity {
  min-width: 0;
  flex: 1;
}

.modal-title-row {
  min-width: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.modal-title {
  min-width: 0;
  overflow: hidden;
  color: var(--text-primary);
  font-size: 16px;
  font-weight: 700;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-tag {
  flex: none;
  padding: 2px 8px;
  font-size: 11px;
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

.modal-sub {
  min-width: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 0;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11.5px;
}

.repo-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
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

.modal-body {
  max-height: min(68vh, 600px);
  overflow-y: auto;
  padding-right: 2px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.edit-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.field {
  min-width: 0;
}

.field-label {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
  color: var(--text-secondary);
  font-size: 12.5px;
  font-weight: 500;
}

.cdn-panel {
  padding: 14px 16px;
  background: color-mix(in srgb, var(--bg-hover) 72%, var(--bg-card));
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.cdn-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
}

.cdn-switch-item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.cdn-label {
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 600;
}

.cdn-note {
  margin: 8px 0 0;
  color: var(--text-tertiary);
  font-size: 12.5px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.modal-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}

.modal-footer__left,
.modal-footer__right {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.modal-detail-link {
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
  border-radius: 4px;
}

.modal-detail-link:hover,
.modal-detail-link:focus-visible {
  color: var(--primary-color);
  border-color: var(--border-hover);
  outline: 0;
}

@media (max-width: 560px) {
  .field-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .modal-footer {
    flex-direction: column-reverse;
    align-items: stretch;
  }

  .modal-footer__left,
  .modal-footer__right {
    justify-content: flex-end;
  }
}
</style>
