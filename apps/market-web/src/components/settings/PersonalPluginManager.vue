<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { NButton, NEmpty, NIcon, NSelect, NSpace, NSpin, NTag } from "naive-ui";
import {
  AppsOutline,
  ArchiveOutline,
  CloudUploadOutline,
  OpenOutline,
  PricetagOutline,
  RefreshOutline,
  SaveOutline,
} from "@vicons/ionicons5";

const props = defineProps({
  plugins: {
    type: Array,
    default: () => [],
  },
  loading: {
    type: Boolean,
    default: false,
  },
  busyIds: {
    type: Object,
    default: () => ({}),
  },
  maxTags: {
    type: Number,
    default: 8,
  },
});

const emit = defineEmits(["refresh", "save-tags", "request-list", "unlist", "open-plugin"]);

const tagDrafts = reactive({});

const statusMeta = Object.freeze({
  listed: { label: "已上架", type: "success" },
  pending: { label: "待审查", type: "info" },
  unlisted: { label: "已下架", type: "warning" },
});

watch(
  () => props.plugins,
  (plugins) => {
    const activeIds = new Set();
    plugins.forEach((plugin) => {
      activeIds.add(plugin.id);
      tagDrafts[plugin.id] = [...(plugin.tags || [])];
    });
    Object.keys(tagDrafts).forEach((pluginId) => {
      if (!activeIds.has(pluginId)) delete tagDrafts[pluginId];
    });
  },
  { immediate: true },
);

const tagOptions = computed(() => {
  const tags = new Set();
  props.plugins.forEach((plugin) => {
    (plugin.tags || []).forEach((tag) => tags.add(tag));
  });
  Object.values(tagDrafts).forEach((draft) => {
    (draft || []).forEach((tag) => tags.add(tag));
  });
  return Array.from(tags)
    .sort()
    .map((tag) => ({ label: tag, value: tag }));
});

function pluginTitle(plugin) {
  return plugin.display_name || plugin.name || plugin.id;
}

function pluginStatus(plugin) {
  return statusMeta[plugin.status] || { label: plugin.status || "未知", type: "default" };
}

function cleanTags(tags) {
  return Array.from(new Set((tags || []).map((tag) => String(tag || "").trim()).filter(Boolean)));
}

function draftTags(plugin) {
  return cleanTags(tagDrafts[plugin.id] || []);
}

function tagsChanged(plugin) {
  return draftTags(plugin).join("\n") !== cleanTags(plugin.tags).join("\n");
}

function isBusy(plugin) {
  return Boolean(props.busyIds?.[plugin.id]);
}

function saveTags(plugin) {
  emit("save-tags", {
    plugin,
    tags: draftTags(plugin).slice(0, props.maxTags),
  });
}
</script>

<template>
  <section class="settings-section">
    <div class="section-heading">
      <div class="section-icon">
        <NIcon><AppsOutline /></NIcon>
      </div>
      <div class="section-copy">
        <p class="section-kicker">我的插件</p>
        <h2 class="section-title">插件管理</h2>
      </div>
      <NButton tertiary :loading="loading" class="refresh-button" @click="emit('refresh')">
        <template #icon>
          <NIcon><RefreshOutline /></NIcon>
        </template>
        刷新
      </NButton>
    </div>

    <NSpin :show="loading">
      <NEmpty v-if="plugins.length === 0" description="暂无可管理插件" />

      <div v-else class="plugin-list">
        <article v-for="plugin in plugins" :key="plugin.id" class="plugin-row">
          <div class="plugin-main">
            <div class="plugin-title-row">
              <strong>{{ pluginTitle(plugin) }}</strong>
              <NTag :type="pluginStatus(plugin).type" size="small" round>
                {{ pluginStatus(plugin).label }}
              </NTag>
            </div>

            <div class="plugin-meta">
              <span>{{ plugin.name }}</span>
              <span>{{ plugin.repo }}</span>
            </div>

            <div class="tag-editor">
              <NIcon class="tag-icon"><PricetagOutline /></NIcon>
              <NSelect
                v-model:value="tagDrafts[plugin.id]"
                multiple
                filterable
                tag
                :max-tag-count="3"
                :options="tagOptions"
                placeholder="添加标签"
              />
            </div>
          </div>

          <NSpace class="plugin-actions" :size="8">
            <NButton tertiary :disabled="isBusy(plugin)" @click="emit('open-plugin', plugin)">
              <template #icon>
                <NIcon><OpenOutline /></NIcon>
              </template>
              查看
            </NButton>

            <NButton
              secondary
              type="primary"
              :disabled="!tagsChanged(plugin)"
              :loading="isBusy(plugin) && busyIds[plugin.id] === 'tags'"
              @click="saveTags(plugin)"
            >
              <template #icon>
                <NIcon><SaveOutline /></NIcon>
              </template>
              保存标签
            </NButton>

            <NButton
              v-if="plugin.status === 'listed'"
              secondary
              type="warning"
              :loading="isBusy(plugin) && busyIds[plugin.id] === 'unlist'"
              @click="emit('unlist', plugin)"
            >
              <template #icon>
                <NIcon><ArchiveOutline /></NIcon>
              </template>
              下架
            </NButton>

            <NButton v-else-if="plugin.status === 'pending'" secondary disabled>待审查</NButton>

            <NButton
              v-else
              secondary
              type="primary"
              :loading="isBusy(plugin) && busyIds[plugin.id] === 'request'"
              @click="emit('request-list', plugin)"
            >
              <template #icon>
                <NIcon><CloudUploadOutline /></NIcon>
              </template>
              申请上架
            </NButton>
          </NSpace>
        </article>
      </div>
    </NSpin>
  </section>
</template>

<style scoped>
.settings-section {
  padding: 22px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--card-color);
}

.section-heading,
.plugin-title-row,
.tag-editor {
  display: flex;
  align-items: center;
}

.section-heading {
  gap: 12px;
  margin-bottom: 18px;
}

.section-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  color: #0e74e4;
  background: rgba(14, 116, 228, 0.12);
  border-radius: 8px;
}

.section-copy {
  min-width: 0;
  flex: 1;
}

.section-kicker,
.section-title {
  margin: 0;
}

.section-kicker {
  color: var(--text-color-3);
  font-size: 12px;
}

.section-title {
  font-size: 18px;
  line-height: 1.3;
}

.refresh-button {
  flex: none;
}

.plugin-list {
  display: grid;
  gap: 12px;
}

.plugin-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: flex-start;
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.plugin-main {
  min-width: 0;
  display: grid;
  gap: 10px;
}

.plugin-title-row {
  gap: 8px;
}

.plugin-title-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 15px;
}

.plugin-meta {
  min-width: 0;
  display: grid;
  gap: 3px;
  color: var(--text-color-3);
  font-size: 12px;
}

.plugin-meta span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tag-editor {
  gap: 8px;
}

.tag-icon {
  flex: none;
  color: var(--text-color-3);
}

.plugin-actions {
  justify-content: flex-end;
}

@media (max-width: 860px) {
  .plugin-row {
    grid-template-columns: 1fr;
  }

  .plugin-actions {
    justify-content: flex-start;
  }
}

@media (max-width: 640px) {
  .section-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .refresh-button {
    width: 100%;
  }

  .plugin-actions {
    width: 100%;
  }

  .plugin-actions :deep(.n-button) {
    flex: 1 1 130px;
  }
}
</style>
