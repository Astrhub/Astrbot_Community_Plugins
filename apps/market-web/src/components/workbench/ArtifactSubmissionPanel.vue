<script setup lang="ts">
import { computed, shallowRef, watch } from "vue";
import { NAlert, NButton, NCard, NInput, NSelect, NTabPane, NTabs } from "naive-ui";
import type { Plugin } from "@/types";
import type { PluginArtifact } from "@/types/artifacts";

const props = defineProps<{
  plugins: Plugin[];
  submitting: boolean;
  supersedesArtifact?: PluginArtifact | null;
}>();

const emit = defineEmits<{
  upload: [payload: { pluginId: string; file: File; supersedesArtifactId: string }];
  github: [payload: { pluginId: string; sourceRef: string; supersedesArtifactId: string }];
}>();

const pluginId = shallowRef("");
const sourceRef = shallowRef("");
const selectedFile = shallowRef<File | null>(null);
const pluginOptions = computed(() =>
  props.plugins.map((plugin) => ({
    label: `${plugin.display_name || plugin.name} (${plugin.name})`,
    value: String(plugin.id),
  })),
);
const isResubmission = computed(
  () => props.supersedesArtifact?.review_status === "changes_requested",
);
const title = computed(() => (isResubmission.value ? "重新提交修订版" : "提交新版本"));

watch(
  () => [props.supersedesArtifact?.id, props.plugins] as const,
  () => {
    const artifact = props.supersedesArtifact;
    if (!artifact) return;
    const plugin = props.plugins.find(
      (item) => String(item.id) === artifact.plugin_id || item.name === artifact.plugin_id,
    );
    if (plugin) pluginId.value = String(plugin.id);
  },
  { immediate: true },
);

function handleFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  selectedFile.value = input.files?.[0] ?? null;
}

function submitUpload(): void {
  if (!pluginId.value || !selectedFile.value) return;
  emit("upload", {
    pluginId: pluginId.value,
    file: selectedFile.value,
    supersedesArtifactId: isResubmission.value ? props.supersedesArtifact?.id || "" : "",
  });
}

function submitGithub(): void {
  if (!pluginId.value) return;
  emit("github", {
    pluginId: pluginId.value,
    sourceRef: sourceRef.value.trim(),
    supersedesArtifactId: isResubmission.value ? props.supersedesArtifact?.id || "" : "",
  });
}
</script>

<template>
  <NCard class="submission-panel" size="small" :title="title">
    <NAlert v-if="isResubmission" class="submission-panel__notice" type="warning" :bordered="false">
      将为 {{ supersedesArtifact?.version }} 创建新的不可变 Artifact，并保留原版本、评论和审查历史。
    </NAlert>
    <NAlert v-if="!plugins.length" type="info" :bordered="false">
      请先登记插件或在原提交页完成插件身份登记。
    </NAlert>
    <template v-else>
      <NSelect
        v-model:value="pluginId"
        class="submission-panel__plugin"
        :options="pluginOptions"
        filterable
        placeholder="选择要更新的插件"
        aria-label="选择要提交版本的插件"
        :disabled="isResubmission"
      />
      <NTabs type="segment" animated>
        <NTabPane name="upload" tab="上传 ZIP">
          <div class="submission-panel__form">
            <label class="file-picker">
              <span>{{ selectedFile?.name || "选择 .zip 插件包" }}</span>
              <input type="file" accept=".zip,application/zip" @change="handleFileChange" />
            </label>
            <NButton
              type="primary"
              :loading="submitting"
              :disabled="!pluginId || !selectedFile"
              @click="submitUpload"
            >
              {{ isResubmission ? "重新提交 ZIP" : "上传并进入隔离审查" }}
            </NButton>
          </div>
        </NTabPane>
        <NTabPane name="github" tab="GitHub 引用">
          <div class="submission-panel__form submission-panel__form--github">
            <NInput
              v-model:value="sourceRef"
              placeholder="分支、标签或 commit；留空使用默认分支"
              clearable
            />
            <NButton
              type="primary"
              :loading="submitting"
              :disabled="!pluginId"
              @click="submitGithub"
            >
              {{ isResubmission ? "重新提交 GitHub 引用" : "固定 commit 并提交" }}
            </NButton>
          </div>
        </NTabPane>
      </NTabs>
    </template>
  </NCard>
</template>

<style scoped>
.submission-panel {
  border-radius: 8px;
}

.submission-panel__plugin {
  margin-bottom: 14px;
}

.submission-panel__notice {
  margin-bottom: 12px;
}

.submission-panel__form {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 8px;
}

.submission-panel__form--github :deep(.n-input) {
  flex: 1;
}

.file-picker {
  display: flex;
  flex: 1;
  align-items: center;
  min-height: 34px;
  padding: 0 12px;
  overflow: hidden;
  border: 1px dashed var(--border-base);
  border-radius: 6px;
  color: var(--text-secondary);
  cursor: pointer;
}

.file-picker span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-picker input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

@media (max-width: 640px) {
  .submission-panel__form {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
