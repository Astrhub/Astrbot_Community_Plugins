<script setup lang="ts">
import { computed, shallowRef } from "vue";
import { storeToRefs } from "pinia";
import { NButton, NIcon, NInput, NModal, NTooltip, useDialog, useMessage } from "naive-ui";
import {
  ChatbubbleEllipsesOutline,
  CheckmarkOutline,
  CloudOfflineOutline,
  HeartOutline,
  LinkOutline,
  PersonOutline,
  StarSharp,
} from "@vicons/ionicons5";
import type { Plugin } from "@/types";
import { usePluginStore } from "@/stores/plugins";
import { DEFAULT_PLUGIN_LOGO_URL, resolvePluginLogoUrl } from "@/utils/github";
import { isNewPlugin } from "@/utils/pluginFreshness";

const props = withDefaults(
  defineProps<{
    plugin: Plugin;
    index?: number;
    seed?: number | string;
  }>(),
  {
    index: 0,
    seed: 0,
  },
);

const store = usePluginStore();
const { currentUser } = storeToRefs(store);
const { loadPlugins, setCurrentPage, setSearchQuery, updatePluginListing } = store;
const message = useMessage();
const dialog = useDialog();

const isCopied = shallowRef(false);
const isUnlisting = shallowRef(false);
const showUnlistModal = shallowRef(false);
const unlistReason = shallowRef("");
const displayName = computed(() => props.plugin.display_name || props.plugin.name);
const formattedVersion = computed(() => {
  const version = String(props.plugin.version || "1.0.0");
  return version.startsWith("v") ? version : `v${version}`;
});
const logoUrl = computed(() => resolvePluginLogoUrl(props.plugin));
const isNew = computed(() => isNewPlugin(props.plugin.created_at));
const isAdminUser = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);
const animationStyle = computed(() => ({
  "--card-index": String(props.index),
  "--card-seed": String(props.seed),
}));

function searchAuthor(event: MouseEvent): void {
  event.stopPropagation();
  const author = String(props.plugin.author || "").trim();
  if (!author) return;
  setSearchQuery(author);
  setCurrentPage(1);
}

async function copyRepoUrl(event: MouseEvent): Promise<void> {
  event.stopPropagation();
  const repo = String(props.plugin.repo || "");
  if (!repo) return;
  try {
    await navigator.clipboard.writeText(repo);
    isCopied.value = true;
    window.setTimeout(() => (isCopied.value = false), 1600);
  } catch {
    message.error("复制失败，请手动复制");
  }
}

function openExternal(url: string | undefined, event: MouseEvent): void {
  event.stopPropagation();
  if (!url) return;
  dialog.info({
    title: "即将打开外链",
    content: `将跳转到：${url}`,
    positiveText: "继续打开",
    negativeText: "取消",
    onPositiveClick: () => window.open(url, "_blank", "noopener,noreferrer"),
  });
}

function openUnlistModal(): void {
  unlistReason.value = "";
  showUnlistModal.value = true;
}

async function unlistPlugin(): Promise<void> {
  const reason = unlistReason.value.trim();
  if (!reason) {
    message.warning("请填写下架原因");
    return;
  }

  isUnlisting.value = true;
  try {
    await updatePluginListing(props.plugin.id, "unlist", { reason });
    await loadPlugins({ force: true });
    showUnlistModal.value = false;
    message.success("插件已下架");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "下架失败");
  } finally {
    isUnlisting.value = false;
  }
}

function handleLogoError(event: Event): void {
  const image = event.target as HTMLImageElement;
  if (image.src.endsWith(DEFAULT_PLUGIN_LOGO_URL)) return;
  image.src = DEFAULT_PLUGIN_LOGO_URL;
}
</script>

<template>
  <article class="plugin-card" :style="animationStyle" :aria-label="`插件：${displayName}`">
    <router-link
      class="plugin-card__detail-link"
      :to="{ name: 'PluginDetails', params: { name: plugin.id } }"
      :aria-label="`查看 ${displayName} 插件详情`"
    />

    <div class="plugin-card__top">
      <img
        :src="logoUrl"
        :alt="`${displayName} 插件图标`"
        class="plugin-logo"
        width="48"
        height="48"
        loading="lazy"
        @error="handleLogoError"
      />
      <div class="plugin-identity">
        <div class="plugin-title-row">
          <h2 class="plugin-name">{{ displayName }}</h2>
          <span v-if="isNew" class="new-badge" aria-label="新发布插件">NEW</span>
        </div>
        <p class="version-author">
          <span>{{ formattedVersion }}</span>
          <span aria-hidden="true">·</span>
          <button type="button" class="author-button" @click="searchAuthor">
            @{{ plugin.author || "community" }}
          </button>
        </p>
      </div>
    </div>

    <p class="description">{{ plugin.desc }}</p>

    <div class="tags-container" aria-label="插件标签">
      <span v-for="tag in plugin.tags" :key="tag" class="plugin-tag">{{ tag }}</span>
    </div>

    <footer class="plugin-footer">
      <div class="metric-list" aria-label="插件互动数据">
        <span class="metric-item metric-item--star" :aria-label="`星标数：${plugin.stars || 0}`">
          <n-icon><star-sharp /></n-icon>{{ plugin.stars || 0 }}
        </span>
        <span class="metric-item metric-item--like" :aria-label="`点赞数：${plugin.likes || 0}`">
          <n-icon><heart-outline /></n-icon>{{ plugin.likes || 0 }}
        </span>
        <span
          class="metric-item metric-item--comment"
          :aria-label="`评论数：${plugin.comments_count || 0}`"
        >
          <n-icon><chatbubble-ellipses-outline /></n-icon>{{ plugin.comments_count || 0 }}
        </span>
      </div>

      <div class="plugin-actions" aria-label="插件快捷操作">
        <n-tooltip trigger="hover" placement="top">
          <template #trigger>
            <n-button
              quaternary
              circle
              size="tiny"
              :aria-label="isCopied ? '已复制仓库链接' : '复制仓库链接'"
              @click="copyRepoUrl"
            >
              <template #icon>
                <n-icon><checkmark-outline v-if="isCopied" /><link-outline v-else /></n-icon>
              </template>
            </n-button>
          </template>
          {{ isCopied ? "已复制" : "复制仓库链接" }}
        </n-tooltip>
        <n-tooltip v-if="plugin.social_link" trigger="hover" placement="top">
          <template #trigger>
            <n-button
              quaternary
              circle
              size="tiny"
              :aria-label="`打开 ${plugin.author} 的主页`"
              @click="openExternal(plugin.social_link, $event)"
            >
              <template #icon
                ><n-icon><person-outline /></n-icon
              ></template>
            </n-button>
          </template>
          作者主页
        </n-tooltip>
        <n-tooltip v-if="isAdminUser" trigger="hover" placement="top">
          <template #trigger>
            <n-button
              quaternary
              circle
              size="tiny"
              type="warning"
              :loading="isUnlisting"
              :aria-label="`下架 ${displayName}`"
              @click.stop="openUnlistModal"
            >
              <template #icon
                ><n-icon><cloud-offline-outline /></n-icon
              ></template>
            </n-button>
          </template>
          下架插件
        </n-tooltip>
        <span class="detail-pill" aria-hidden="true">详情 ↗</span>
      </div>
    </footer>
  </article>

  <n-modal
    v-model:show="showUnlistModal"
    preset="card"
    title="填写下架原因"
    style="max-width: 420px"
  >
    <n-input
      v-model:value="unlistReason"
      type="textarea"
      placeholder="例如：仓库失效或违反社区规范"
      :autosize="{ minRows: 3, maxRows: 6 }"
      maxlength="500"
      show-count
    />
    <template #footer>
      <div class="unlist-modal-actions">
        <n-button tertiary @click="showUnlistModal = false">取消</n-button>
        <n-button type="warning" :loading="isUnlisting" @click="unlistPlugin">确认下架</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.plugin-card {
  position: relative;
  min-width: 0;
  min-height: 208px;
  display: flex;
  flex-direction: column;
  padding: 22px 26px 18px;
  color: var(--text-primary);
  background: var(--bg-card);
  border-right: 1px solid var(--border-base);
  border-bottom: 1px solid var(--border-base);
  transition: background-color 160ms ease;
}

.plugin-card:hover,
.plugin-card:focus-within {
  background: color-mix(in srgb, var(--bg-hover) 56%, var(--bg-card));
}

.plugin-card:nth-of-type(3n) {
  border-right: 0;
}

.plugin-card__detail-link {
  position: absolute;
  inset: 0;
  z-index: 1;
}

.plugin-card__detail-link:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: -3px;
}

.plugin-card__top {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 14px;
}

.plugin-logo {
  width: 48px;
  height: 48px;
  flex: 0 0 auto;
  object-fit: cover;
  background: var(--logo-bg);
  border: 1px solid var(--logo-border);
  border-radius: 10px;
}

.plugin-identity {
  min-width: 0;
}

.plugin-title-row {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.plugin-name {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--text-primary);
  font-size: 15.5px;
  font-weight: 750;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.new-badge {
  flex: 0 0 auto;
  padding: 2px 7px;
  color: var(--metric-like);
  font-size: 9px;
  font-weight: 750;
  line-height: 1.3;
  border: 1px solid currentColor;
  border-radius: 999px;
}

.version-author {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 5px 0 0;
  overflow: hidden;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.author-button {
  position: relative;
  z-index: 2;
  min-width: 0;
  padding: 0;
  overflow: hidden;
  color: inherit;
  font: inherit;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.author-button:hover,
.author-button:focus-visible {
  color: var(--primary-color);
  outline: 0;
}

.description {
  min-height: 40px;
  margin: 15px 0 0;
  display: -webkit-box;
  overflow: hidden;
  color: var(--text-secondary);
  font-size: 12.5px;
  line-height: 1.6;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.tags-container {
  min-height: 26px;
  display: flex;
  flex-wrap: wrap;
  align-content: flex-start;
  gap: 6px;
  margin-top: 10px;
  overflow: hidden;
}

.plugin-tag {
  max-width: 126px;
  padding: 3px 9px;
  overflow: hidden;
  color: var(--tag-text);
  font-size: 11px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: transparent;
  border: 1px solid var(--tag-border);
  border-radius: 999px;
}

.plugin-footer {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--border-base);
}

.metric-list,
.plugin-actions {
  position: relative;
  z-index: 2;
  display: inline-flex;
  align-items: center;
}

.metric-list {
  gap: 12px;
}

.metric-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11.5px;
}

.metric-item--star {
  color: var(--metric-star);
}

.metric-item--like {
  color: var(--metric-like);
}

.metric-item--comment {
  color: var(--metric-comment);
}

.plugin-actions {
  gap: 2px;
}

.detail-pill {
  margin-left: 4px;
  padding: 4px 10px;
  color: var(--primary-color);
  font-size: 11px;
  font-weight: 700;
  border: 1px solid var(--border-base);
  border-radius: 999px;
}

.unlist-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

@media (max-width: 1024px) and (min-width: 769px) {
  .plugin-card:nth-of-type(3n) {
    border-right: 1px solid var(--border-base);
  }

  .plugin-card:nth-of-type(2n) {
    border-right: 0;
  }
}

@media (max-width: 768px) {
  .plugin-card {
    min-height: 196px;
    padding: 18px 16px 16px;
    border-right: 0;
  }
}

@media (max-width: 420px) {
  .detail-pill {
    display: none;
  }

  .plugin-actions {
    gap: 0;
  }
}
</style>
