<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { NIcon } from "naive-ui";
import { MegaphoneOutline } from "@vicons/ionicons5";
import { usePluginStore } from "../stores/plugins";

const AnnouncementModal = defineAsyncComponent(() => import("./AnnouncementModal.vue"));

const store = usePluginStore();
const { announcements } = storeToRefs(store);
const show = shallowRef(false);
const modalLoaded = shallowRef(false);
const storageKey = "astrhub_announcements_seen_v1";
const revision = computed(() =>
  announcements.value.length
    ? JSON.stringify(
        announcements.value.map(({ id, title, body, created_at, updated_at }) => [
          id,
          title,
          body,
          updated_at || created_at,
        ]),
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
  modalLoaded.value = true;
  show.value = true;
}

function markAnnouncementsSeen(): void {
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
    if (value && value !== seenRevision) openAnnouncements();
  },
  { immediate: true },
);

onMounted(() => {
  store.loadAnnouncements().catch((error: unknown) => {
    console.error("Error loading announcements:", error);
  });
});
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
  <AnnouncementModal
    v-if="modalLoaded"
    v-model:show="show"
    :announcements="announcements"
    @presented="markAnnouncementsSeen"
  />
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
</style>
