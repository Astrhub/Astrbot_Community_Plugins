<script setup lang="ts">
import { computed, ref, watch } from "vue";

const props = defineProps<{
  modelValue: string[];
  options: { label: string; value: string; count?: number }[];
}>();
const emit = defineEmits<{ "update:modelValue": [value: string[]] }>();
const expanded = ref(false);
const query = ref("");
const limit = ref(80);
const selected = computed(() => new Set(props.modelValue));
const matching = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase();
  return props.options.filter(
    (option) => !keyword || option.label.toLocaleLowerCase().includes(keyword),
  );
});
const visible = computed(() =>
  expanded.value ? matching.value.slice(0, limit.value) : props.options.slice(0, 10),
);
watch(query, () => {
  limit.value = 80;
});

function toggle(value: string): void {
  emit(
    "update:modelValue",
    selected.value.has(value)
      ? props.modelValue.filter((tag) => tag !== value)
      : [...props.modelValue, value],
  );
}
</script>

<template>
  <section class="tag-filter" aria-label="插件标签筛选">
    <div class="tag-filter__heading">
      <span
        >标签
        <small v-if="modelValue.length">已选 {{ modelValue.length }} 个 · 匹配任一标签</small></span
      >
      <button
        type="button"
        class="tag-filter__expand"
        :aria-expanded="expanded"
        aria-controls="market-tag-options"
        @click="expanded = !expanded"
      >
        {{ expanded ? "收起标签" : `浏览全部 ${options.length} 个标签` }}
        <span aria-hidden="true">{{ expanded ? "−" : "+" }}</span>
      </button>
    </div>
    <label v-if="expanded" class="tag-filter__search">
      <span class="sr-only">搜索标签</span>
      <input v-model="query" type="search" placeholder="搜索标签…" aria-label="搜索标签" />
      <span>{{ matching.length }} 个</span>
    </label>
    <div id="market-tag-options" class="tag-filter__options" :class="{ 'is-expanded': expanded }">
      <button
        type="button"
        class="tag-chip tag-chip--all"
        :class="{ 'is-active': !modelValue.length }"
        :aria-pressed="!modelValue.length"
        @click="emit('update:modelValue', [])"
      >
        全部
      </button>
      <button
        v-for="option in visible"
        :key="option.value"
        type="button"
        class="tag-chip"
        :class="{ 'is-active': selected.has(option.value) }"
        :aria-pressed="selected.has(option.value)"
        @click="toggle(option.value)"
      >
        <span>{{ option.label }}</span
        ><small v-if="option.count !== undefined">{{ option.count }}</small>
      </button>
      <button
        v-if="expanded && matching.length > limit"
        type="button"
        class="tag-filter__expand"
        @click="limit += 80"
      >
        显示更多标签
      </button>
      <span v-if="expanded && !matching.length" class="tag-filter__empty">没有匹配的标签</span>
    </div>
    <div v-if="modelValue.length" class="tag-filter__selected" aria-label="已选标签">
      <span>已选</span>
      <button
        v-for="tag in modelValue"
        :key="tag"
        type="button"
        :aria-label="`移除标签 ${tag}`"
        @click="toggle(tag)"
      >
        {{ tag }} <span aria-hidden="true">×</span>
      </button>
    </div>
  </section>
</template>

<style scoped>
.tag-filter {
  padding: 18px 24px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border-base);
}
.tag-filter__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 650;
}
.tag-filter__heading small {
  margin-left: 8px;
  color: var(--text-tertiary);
  font-weight: 400;
}
.tag-filter__expand {
  padding: 4px 0;
  background: none;
  border: 0;
  color: var(--primary-color);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.tag-filter__search {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  padding: 8px 12px;
  color: var(--text-tertiary);
  background: var(--bg-base);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  font-size: 12px;
}
.tag-filter__search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  font-size: 14px;
}
.tag-filter__options {
  display: flex;
  flex-wrap: wrap;
  align-content: flex-start;
  gap: 8px;
}
.tag-filter__options.is-expanded {
  max-height: 240px;
  overflow-y: auto;
  padding: 2px;
}
.tag-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 100%;
  min-height: 32px;
  padding: 5px 12px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-base);
  color: var(--text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.tag-chip > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tag-chip small {
  opacity: 0.7;
  font-variant-numeric: tabular-nums;
}
.tag-chip.is-active {
  color: var(--primary-color);
  background: var(--primary-light);
  border-color: var(--primary-color);
}
.tag-chip--all {
  flex: 0 0 auto;
  font-weight: 650;
}
.tag-chip:focus-visible,
.tag-filter__expand:focus-visible,
.tag-filter__selected button:focus-visible {
  outline: 2px solid var(--primary-color);
  outline-offset: 2px;
}
.tag-filter__selected {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 12px;
  color: var(--text-tertiary);
  font-size: 12px;
}
.tag-filter__selected button {
  padding: 3px 8px;
  border: 0;
  border-radius: 5px;
  color: var(--primary-color);
  background: var(--primary-light);
  font: inherit;
  cursor: pointer;
}
.tag-filter__empty {
  align-self: center;
  color: var(--text-tertiary);
  font-size: 12px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
@media (max-width: 680px) {
  .tag-filter {
    padding: 16px;
  }
  .tag-filter__heading small {
    display: block;
    margin: 4px 0 0;
  }
}
</style>
