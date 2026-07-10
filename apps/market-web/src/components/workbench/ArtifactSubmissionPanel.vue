<script setup lang="ts">
import { computed, shallowRef } from "vue";
import { NAlert, NButton, NCard, NInput, NSelect, NTabPane, NTabs } from "naive-ui";
import type { Plugin } from "@/types";

const props = defineProps<{
  plugins: Plugin[];
  submitting: boolean;
}>();

const emit = defineEmits<{
  upload: [payload: { pluginId: string; file: File }];
  github: [payload: { pluginId: string; sourceRef: string }];
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

function handleFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  selectedFile.value = input.files?.[0] ?? null;
}

function submitUpload(): void {
  if (!pluginId.value || !selectedFile.value) return;
  emit("upload", { pluginId: pluginId.value, file: selectedFile.value });
}

function submitGithub(): void {
  if (!pluginId.value) return;
  emit("github", { pluginId: pluginId.value, sourceRef: sourceRef.value.trim() });
}
</script>

<template>
  <NCard class="submission-panel" size="small" title="提交新版本">
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
              上传并进入隔离审查
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
              固定 commit 并提交
            </NButton>
          </div>
        </NTabPane>
      </NTabs>
    </template>
  </NCard>
</template>

<style scoped>
.submission-panel {
  border-radius: 10px;
}

.submission-panel__plugin {
  margin-bottom: 14px;
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
