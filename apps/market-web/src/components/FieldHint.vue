<script setup lang="ts">
import { shallowRef } from "vue";
import { NIcon, NTooltip } from "naive-ui";
import { HelpCircleOutline } from "@vicons/ionicons5";

withDefaults(
  defineProps<{
    content: string;
    placement?: "top" | "top-start" | "top-end" | "bottom" | "bottom-start" | "bottom-end";
  }>(),
  { placement: "top" },
);

const visible = shallowRef(false);
const pinned = shallowRef(false);

function showHint(): void {
  visible.value = true;
}

function hideHint(): void {
  if (!pinned.value) visible.value = false;
}

function toggleHint(): void {
  pinned.value = !pinned.value;
  visible.value = pinned.value;
}

function closeHint(): void {
  pinned.value = false;
  visible.value = false;
}
</script>

<template>
  <n-tooltip :show="visible" trigger="manual" :placement="placement" :to="false">
    <template #trigger>
      <button
        type="button"
        class="field-hint"
        aria-label="查看字段说明"
        :aria-expanded="visible"
        @mouseenter="showHint"
        @mouseleave="hideHint"
        @focus="showHint"
        @blur="hideHint"
        @click="toggleHint"
        @keydown.esc="closeHint"
      >
        <n-icon aria-hidden="true"><help-circle-outline /></n-icon>
      </button>
    </template>
    <span class="field-hint__content">{{ content }}</span>
  </n-tooltip>
</template>

<style scoped>
.field-hint {
  width: 20px;
  height: 20px;
  padding: 0;
  display: inline-grid;
  place-items: center;
  color: var(--text-tertiary);
  background: transparent;
  border: 0;
  border-radius: 50%;
  cursor: help;
  vertical-align: middle;
}

.field-hint:hover,
.field-hint:focus-visible {
  color: var(--primary-color);
  outline: 2px solid color-mix(in srgb, var(--primary-color) 28%, transparent);
  outline-offset: 1px;
}

.field-hint__content {
  display: block;
  max-width: 280px;
  line-height: 1.55;
}
</style>
