<script setup lang="ts">
import { onMounted, onUnmounted, shallowRef } from "vue";
import { NIcon } from "naive-ui";
import { ChevronUpOutline } from "@vicons/ionicons5";

const visible = shallowRef(false);

function updateVisibility(): void {
  visible.value = window.scrollY > 420;
}

function scrollToTop(): void {
  window.scrollTo({ top: 0, behavior: "smooth" });
}

onMounted(() => {
  window.addEventListener("scroll", updateVisibility, { passive: true });
  updateVisibility();
});

onUnmounted(() => window.removeEventListener("scroll", updateVisibility));
</script>

<template>
  <transition name="back-to-top-fade">
    <button
      v-if="visible"
      type="button"
      class="back-to-top"
      aria-label="返回顶部"
      @click="scrollToTop"
    >
      <n-icon><chevron-up-outline /></n-icon>
    </button>
  </transition>
</template>

<style scoped>
.back-to-top {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 800;
  width: 42px;
  height: 42px;
  display: grid;
  padding: 0;
  place-items: center;
  color: var(--text-secondary);
  font-size: 18px;
  background: color-mix(in srgb, var(--bg-card) 92%, transparent);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
  cursor: pointer;
  backdrop-filter: blur(10px);
}

.back-to-top:hover,
.back-to-top:focus-visible {
  color: var(--primary-color);
  border-color: var(--primary-color);
  outline: 0;
}

.back-to-top-fade-enter-active,
.back-to-top-fade-leave-active {
  transition:
    opacity 140ms ease,
    transform 140ms ease;
}

.back-to-top-fade-enter-from,
.back-to-top-fade-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

@media (max-width: 680px) {
  .back-to-top {
    right: 14px;
    bottom: 14px;
    width: 38px;
    height: 38px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .back-to-top {
    scroll-behavior: auto;
  }

  .back-to-top-fade-enter-active,
  .back-to-top-fade-leave-active {
    transition: none;
  }
}
</style>
