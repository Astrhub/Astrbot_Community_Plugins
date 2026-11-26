<script setup lang="ts">
import { watch } from "vue";
import { NButton, NModal } from "naive-ui";
import AnnouncementBody from "./AnnouncementBody.vue";
import type { Announcement } from "../types";

const props = defineProps<{
  show: boolean;
  announcements: Announcement[];
}>();

const emit = defineEmits<{
  "update:show": [value: boolean];
  presented: [];
}>();

watch(
  () => [props.show, props.announcements] as const,
  ([visible, items]) => {
    if (visible && items.length > 0) emit("presented");
  },
  { immediate: true, flush: "post" },
);

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="站点公告"
    class="announcement-modal"
    @update:show="(value: boolean) => emit('update:show', value)"
  >
    <div class="announcement-list">
      <article
        v-for="announcement in announcements"
        :key="announcement.id"
        class="announcement-item"
      >
        <h2>{{ announcement.title }}</h2>
        <time v-if="announcement.created_at" :datetime="announcement.created_at">{{
          formatTime(announcement.created_at)
        }}</time>
        <AnnouncementBody v-if="announcement.body" :body="announcement.body" />
      </article>
    </div>
    <template #footer>
      <n-button type="primary" @click="emit('update:show', false)">知道了</n-button>
    </template>
  </n-modal>
</template>

<style scoped>
:global(.announcement-modal) {
  width: min(560px, calc(100vw - 32px));
  border-radius: 12px;
}

.announcement-list {
  max-height: 60vh;
  overflow-y: auto;
  overflow-wrap: anywhere;
}

.announcement-item + .announcement-item {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--border-base);
}

.announcement-item h2 {
  margin: 0 0 6px;
  font-size: 17px;
}

.announcement-item time {
  color: var(--text-tertiary);
  font-size: 12px;
}
</style>
