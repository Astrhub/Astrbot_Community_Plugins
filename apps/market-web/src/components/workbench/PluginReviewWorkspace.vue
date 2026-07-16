<script setup lang="ts">
import { NButton, NDrawer, NDrawerContent, NIcon } from "naive-ui";
import { MenuOutline } from "@vicons/ionicons5";
import type { ReviewWorkspaceView } from "@/types/artifacts";

defineProps<{
  activeView: ReviewWorkspaceView;
  drawerOpen: boolean;
}>();

defineEmits<{
  "update:drawerOpen": [open: boolean];
}>();

defineSlots<{
  header(): unknown;
  sidebar(): unknown;
  submission?(): unknown;
  default(): unknown;
  thread?(): unknown;
  decision?(): unknown;
}>();
</script>

<template>
  <div class="review-workspace">
    <header class="review-workspace__header">
      <slot name="header" />
    </header>
    <div class="review-workspace__mobile-toolbar">
      <NButton quaternary aria-label="打开版本队列" @click="$emit('update:drawerOpen', true)">
        <template #icon
          ><NIcon><MenuOutline /></NIcon
        ></template>
        版本队列
      </NButton>
    </div>
    <div class="review-workspace__body">
      <aside class="review-workspace__sidebar">
        <slot name="sidebar" />
      </aside>
      <main class="review-workspace__main">
        <slot name="submission" />
        <slot />
      </main>
      <aside class="review-workspace__rail">
        <section
          class="review-workspace__thread"
          :class="{ 'review-workspace__pane--mobile-active': activeView === 'comments' }"
        >
          <slot name="thread" />
        </section>
        <section
          class="review-workspace__decision"
          :class="{ 'review-workspace__pane--mobile-active': activeView === 'summary' }"
        >
          <slot name="decision" />
        </section>
      </aside>
    </div>
    <NDrawer
      :show="drawerOpen"
      placement="left"
      width="min(88vw, 360px)"
      @update:show="$emit('update:drawerOpen', $event)"
    >
      <NDrawerContent title="版本队列" closable body-content-style="padding: 0">
        <slot name="sidebar" />
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
.review-workspace {
  min-height: 100vh;
  background: var(--body-color);
}

.review-workspace__header {
  position: sticky;
  z-index: 10;
  top: 0;
}

.review-workspace__body {
  display: grid;
  grid-template-columns: minmax(260px, 300px) minmax(0, 1fr) minmax(320px, 380px);
  min-height: calc(100vh - 72px);
}

.review-workspace__sidebar {
  border-right: 1px solid var(--border-base);
  background: var(--card-color);
}

.review-workspace__main {
  display: grid;
  align-content: start;
  gap: 18px;
  padding: 24px;
  overflow: hidden;
}

.review-workspace__rail {
  display: grid;
  min-width: 0;
  align-content: start;
  gap: 14px;
  padding: 18px 18px 18px 0;
  border-left: 1px solid var(--border-base);
}

.review-workspace__thread,
.review-workspace__decision {
  min-width: 0;
}

.review-workspace__thread {
  position: sticky;
  top: 90px;
  max-height: calc(100vh - 108px);
}

.review-workspace__decision {
  position: sticky;
  top: calc(100vh - 300px);
}

.review-workspace__mobile-toolbar {
  display: none;
}

@media (max-width: 1240px) and (min-width: 861px) {
  .review-workspace__body {
    grid-template-columns: minmax(250px, 290px) minmax(0, 1fr);
  }

  .review-workspace__sidebar {
    grid-row: 1 / 3;
  }

  .review-workspace__rail {
    grid-column: 2;
    padding: 0 20px 20px;
    border-left: 0;
  }

  .review-workspace__thread,
  .review-workspace__decision {
    position: static;
    max-height: none;
  }
}

@media (max-width: 860px) {
  .review-workspace__body {
    display: block;
    min-height: 0;
  }

  .review-workspace__sidebar {
    display: none;
  }

  .review-workspace__mobile-toolbar {
    display: flex;
    min-height: 44px;
    align-items: center;
    padding: 4px 10px;
    border-bottom: 1px solid var(--border-base);
    background: var(--card-color);
  }

  .review-workspace__main {
    padding: 12px;
  }

  .review-workspace__rail {
    display: block;
    padding: 0 12px 18px;
    border-left: 0;
  }

  .review-workspace__thread,
  .review-workspace__decision {
    display: none;
    position: static;
    max-height: none;
  }

  .review-workspace__pane--mobile-active {
    display: block;
  }
}
</style>
