<script setup lang="ts">
import { computed, onMounted, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { NButton, NIcon, NModal } from "naive-ui";
import { MegaphoneOutline } from "@vicons/ionicons5";
import { usePluginStore } from "../stores/plugins";

const store = usePluginStore();
const { announcements } = storeToRefs(store);
const show = shallowRef(false);
const storageKey = "astrhub_announcements_seen_v1";
const revision = computed(() =>
  announcements.value.length
    ? JSON.stringify(
        announcements.value.map(({ id, title, body, created_at }) => [id, title, body, created_at]),
      )
    : "",
);
let seenRevision = "";
try {
  seenRevision = localStorage.getItem(storageKey) || "";
} catch {
  // Storage restrictions should not prevent visitors from reading announcements.
}

function openAnnouncements(): void {
  show.value = true;
  seenRevision = revision.value;
  try {
    localStorage.setItem(storageKey, seenRevision);
  } catch {
    // Keep the acknowledgement in memory when browser storage is unavailable.
  }
}

watch(
  revision,
  (value) => {
    if (!window.__ASTRHUB_PRERENDER__ && value && value !== seenRevision) openAnnouncements();
  },
  { immediate: true },
);

onMounted(() => {
  store.loadAnnouncements().catch((error: unknown) => {
    console.error("Error loading announcements:", error);
  });
});

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
}
</script>

<template>
  <button
    v-if="announcements.length"
    type="button"
    class="announcement-trigger"
    aria-label="查看站点公告"
    aria-haspopup="dialog"
    :aria-expanded="show"
    title="站点公告"
    @click="openAnnouncements"
  >
    <n-icon><megaphone-outline /></n-icon>
  </button>
  <n-modal v-model:show="show" preset="card" title="站点公告" class="announcement-modal">
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
        <p v-if="announcement.body">{{ announcement.body }}</p>
      </article>
    </div>
    <template #footer>
      <n-button type="primary" @click="show = false">知道了</n-button>
    </template>
  </n-modal>
</template>

<style scoped>
.announcement-trigger {
  display: inline-grid;
  flex: 0 0 auto;
  width: 32px;
  height: 38px;
  padding: 0;
  place-items: center;
  color: var(--text-secondary);
  font-size: 18px;
  background: transparent;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
}

.announcement-trigger:hover,
.announcement-trigger:focus-visible {
  color: var(--primary-color);
  background: var(--bg-hover);
}

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

.announcement-item p {
  margin: 12px 0 0;
  white-space: pre-wrap;
  line-height: 1.8;
}
</style>
