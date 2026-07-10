<script setup lang="ts">
import { NButton, NIcon, NLayoutHeader, NTag } from "naive-ui";
import { ArrowBack, RefreshOutline } from "@vicons/ionicons5";
import ThemeModeButton from "@/components/ThemeModeButton.vue";

defineProps<{
  isAdmin: boolean;
  itemCount: number;
  refreshing?: boolean;
}>();

defineEmits<{
  back: [];
  refresh: [];
}>();
</script>

<template>
  <NLayoutHeader class="workbench-header">
    <div class="workbench-header__left">
      <NButton quaternary circle aria-label="返回" @click="$emit('back')">
        <template #icon
          ><NIcon><ArrowBack /></NIcon
        ></template>
      </NButton>
      <div>
        <p class="workbench-header__eyebrow">插件工作台</p>
        <h1 class="workbench-header__title">
          {{ isAdmin ? "版本审查队列" : "版本与发布" }}
        </h1>
      </div>
      <NTag size="small" round :bordered="false">{{ itemCount }} 个版本</NTag>
    </div>
    <div class="workbench-header__actions">
      <NButton quaternary :loading="refreshing" @click="$emit('refresh')">
        <template #icon
          ><NIcon><RefreshOutline /></NIcon
        ></template>
        刷新
      </NButton>
      <ThemeModeButton circle />
    </div>
  </NLayoutHeader>
</template>

<style scoped>
.workbench-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 72px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border-base);
  background: color-mix(in srgb, var(--card-color) 94%, transparent);
  backdrop-filter: blur(12px);
}

.workbench-header__left,
.workbench-header__actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.workbench-header__eyebrow,
.workbench-header__title {
  margin: 0;
}

.workbench-header__eyebrow {
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.workbench-header__title {
  color: var(--text-primary);
  font-size: 20px;
}

@media (max-width: 640px) {
  .workbench-header {
    padding: 10px 12px;
  }

  .workbench-header__actions :deep(.n-button__content) {
    font-size: 0;
  }
}
</style>
