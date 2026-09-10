<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from "vue";
import type { Plugin } from "../types";
import PluginCard from "./PluginCard.vue";

const props = defineProps<{ plugins: Plugin[]; seed: number }>();
const root = ref<HTMLElement | null>(null);
const width = shallowRef(0);
const heights = shallowRef(new Map<string, number>());
const elements = new Map<string, HTMLElement>();
const columns = computed(() => (width.value >= 1000 ? 3 : width.value >= 620 ? 2 : 1));
const cardWidth = computed(() =>
  Math.max(0, (width.value - (columns.value - 1) * 16) / columns.value),
);
const layout = computed(() => {
  const bottoms = Array(columns.value).fill(0) as number[];
  const positions = props.plugins.map((plugin) => {
    const column = bottoms.indexOf(Math.min(...bottoms));
    const y = bottoms[column]!;
    bottoms[column] = y + (heights.value.get(String(plugin.id)) || 290) + 16;
    return { plugin, x: column * (cardWidth.value + 16), y };
  });
  return { positions, height: Math.max(0, ...bottoms) };
});
let observer: ResizeObserver | undefined;
let frame = 0;
let active = false;

function schedule(): void {
  if (!active || frame) return;
  frame = window.requestAnimationFrame(() => {
    frame = 0;
    const nextWidth = root.value?.getBoundingClientRect().width || 0;
    if (Math.abs(nextWidth - width.value) > 0.5) {
      width.value = nextWidth;
      void nextTick(schedule);
      return;
    }
    const next = new Map<string, number>();
    let changed = heights.value.size !== elements.size;
    for (const [id, element] of elements) {
      const height = element.getBoundingClientRect().height;
      if (height > 0) next.set(id, height);
      if (Math.abs((heights.value.get(id) || 0) - height) > 0.5) changed = true;
    }
    if (changed) heights.value = next;
  });
}

function track(id: string, element: unknown): void {
  const previous = elements.get(id);
  if (element === previous) return;
  if (previous) observer?.unobserve(previous);
  if (element instanceof HTMLElement) {
    elements.set(id, element);
    observer?.observe(element);
  } else elements.delete(id);
  schedule();
}

watch(
  () => props.plugins,
  () => {
    void nextTick(schedule);
  },
  { flush: "post" },
);
onMounted(() => {
  active = true;
  width.value = root.value?.getBoundingClientRect().width || 0;
  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(schedule);
    if (root.value) observer.observe(root.value);
    elements.forEach((element) => observer!.observe(element));
  }
  window.addEventListener("resize", schedule);
  void nextTick(schedule);
});
onUnmounted(() => {
  active = false;
  observer?.disconnect();
  window.cancelAnimationFrame(frame);
  window.removeEventListener("resize", schedule);
});
</script>

<template>
  <div ref="root" class="plugin-masonry" role="list" :style="{ height: `${layout.height}px` }">
    <div
      v-for="(item, index) in layout.positions"
      :key="item.plugin.id"
      :ref="(element) => track(String(item.plugin.id), element)"
      class="plugin-masonry__item"
      role="listitem"
      :style="{ width: `${cardWidth}px`, transform: `translate(${item.x}px, ${item.y}px)` }"
    >
      <plugin-card :plugin="item.plugin" :index="index" :seed="seed" masonry />
    </div>
  </div>
</template>

<style scoped>
.plugin-masonry {
  position: relative;
  width: 100%;
  min-height: 200px;
}
.plugin-masonry__item {
  position: absolute;
  top: 0;
  left: 0;
  box-sizing: border-box;
}
</style>
