<script setup lang="ts">
defineSlots<{
  header(): unknown;
  sidebar(): unknown;
  submission?(): unknown;
  default(): unknown;
}>();
</script>

<template>
  <div class="review-workspace">
    <header class="review-workspace__header">
      <slot name="header" />
    </header>
    <div class="review-workspace__body">
      <aside class="review-workspace__sidebar">
        <slot name="sidebar" />
      </aside>
      <main class="review-workspace__main">
        <slot name="submission" />
        <slot />
      </main>
    </div>
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
  grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);
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

@media (max-width: 860px) {
  .review-workspace__body {
    grid-template-columns: 1fr;
  }

  .review-workspace__sidebar {
    border-right: 0;
    border-bottom: 1px solid var(--border-base);
  }

  .review-workspace__main {
    padding: 16px;
  }
}
</style>
