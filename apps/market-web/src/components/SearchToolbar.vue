<script setup lang="ts">
import { computed } from "vue";
import { NButton, NIcon, NSelect } from "naive-ui";
import type { SelectOption } from "naive-ui";
import {
  ArrowDownOutline,
  ArrowUpOutline,
  CloseCircle,
  GitCompareOutline,
  SearchOutline,
} from "@vicons/ionicons5";

const props = withDefaults(
  defineProps<{
    searchQuery?: string;
    currentPage?: number;
    sortBy?: string;
    sortDirection?: string;
    fuzzySearchEnabled?: boolean;
    selectedCategory?: string;
    categoryOptions?: SelectOption[];
    selectedTag?: string | null;
    tagOptions?: SelectOption[];
    compact?: boolean;
    onHeader?: boolean;
    mobile?: boolean;
    showCategoryFilter?: boolean;
  }>(),
  {
    searchQuery: "",
    currentPage: 1,
    sortBy: "default",
    sortDirection: "asc",
    fuzzySearchEnabled: false,
    selectedCategory: "all",
    categoryOptions: () => [],
    selectedTag: null,
    tagOptions: () => [],
    compact: false,
    onHeader: false,
    mobile: false,
    showCategoryFilter: true,
  },
);

const emit = defineEmits<{
  "update:searchQuery": [value: string];
  "update:currentPage": [value: number];
  "update:sortBy": [value: string];
  "update:sortDirection": [value: string];
  "update:fuzzySearchEnabled": [value: boolean];
  "update:selectedCategory": [value: string];
  "update:selectedTag": [value: string | null];
}>();

const hasCategoryFilters = computed(() =>
  props.categoryOptions.some((option) => option.value !== "all" && option.value !== "other"),
);
const searchPlaceholder = computed(() => (props.mobile ? "搜索插件" : "搜索插件、作者、描述..."));
const sortOptions: SelectOption[] = [
  { label: "默认排序", value: "default" },
  { label: "随机推荐", value: "random" },
  { label: "按更新时间", value: "updated" },
  { label: "按 Star", value: "stars" },
  { label: "按点赞", value: "likes" },
  { label: "按评论", value: "comments" },
];

function resetPage(): void {
  if (props.currentPage > 1) emit("update:currentPage", 1);
}

function updateSearch(event: Event): void {
  emit("update:searchQuery", (event.target as HTMLInputElement).value);
  resetPage();
}

function clearSearch(): void {
  emit("update:searchQuery", "");
  resetPage();
}

function updateCategory(value: string | null): void {
  emit("update:selectedCategory", value || "all");
  resetPage();
}

function updateTag(value: string | null): void {
  emit("update:selectedTag", value || null);
  resetPage();
}

function updateSort(value: string): void {
  emit("update:sortBy", value);
  resetPage();
}

function toggleSearchMode(): void {
  emit("update:fuzzySearchEnabled", !props.fuzzySearchEnabled);
  resetPage();
}

function toggleSortDirection(): void {
  emit("update:sortDirection", props.sortDirection === "asc" ? "desc" : "asc");
  resetPage();
}
</script>

<template>
  <div
    class="search-toolbar"
    :class="{
      'search-toolbar--compact': compact,
      'search-toolbar--header': onHeader,
      'search-toolbar--mobile': mobile,
    }"
  >
    <label class="search-field">
      <n-icon class="search-icon"><search-outline /></n-icon>
      <input
        :value="searchQuery"
        type="search"
        name="plugin-search"
        :placeholder="searchPlaceholder"
        aria-label="搜索插件"
        autocomplete="off"
        spellcheck="false"
        @input="updateSearch"
      />
      <button
        v-if="searchQuery"
        type="button"
        class="clear-button"
        aria-label="清除搜索"
        @click="clearSearch"
      >
        <n-icon><close-circle /></n-icon>
      </button>
    </label>

    <n-select
      v-if="showCategoryFilter && hasCategoryFilters"
      :value="selectedCategory"
      :options="categoryOptions"
      aria-label="插件官方分类"
      class="toolbar-select category-select"
      @update:value="updateCategory"
    />
    <n-select
      :value="selectedTag"
      :options="tagOptions"
      placeholder="全部标签"
      aria-label="插件标签"
      filterable
      clearable
      class="toolbar-select tag-select"
      @update:value="updateTag"
    />

    <n-button
      quaternary
      class="mode-button"
      :title="fuzzySearchEnabled ? '当前为模糊搜索' : '当前为精确搜索'"
      :aria-label="fuzzySearchEnabled ? '切换为精确搜索' : '切换为模糊搜索'"
      @click="toggleSearchMode"
    >
      <template #icon
        ><n-icon><git-compare-outline /></n-icon
      ></template>
      <span class="mode-label">{{ fuzzySearchEnabled ? "模糊" : "精确" }}</span>
    </n-button>

    <n-select
      :value="sortBy"
      :options="sortOptions"
      aria-label="排序方式"
      class="toolbar-select sort-select"
      @update:value="updateSort"
    />
    <n-button
      quaternary
      class="direction-button"
      :title="sortDirection === 'asc' ? '当前正序' : '当前倒序'"
      :aria-label="sortDirection === 'asc' ? '切换为倒序' : '切换为正序'"
      @click="toggleSortDirection"
    >
      <template #icon>
        <n-icon>
          <arrow-up-outline v-if="sortDirection === 'asc'" />
          <arrow-down-outline v-else />
        </n-icon>
      </template>
    </n-button>
  </div>
</template>

<style scoped>
.search-toolbar {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(280px, 1fr) 148px 148px 86px 142px 52px;
  align-items: stretch;
  background: var(--bg-card);
}

.search-field {
  min-width: 0;
  height: 50px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  color: var(--text-tertiary);
  background: transparent;
}

.search-field:focus-within {
  color: var(--primary-color);
  box-shadow: inset 0 -2px var(--primary-color);
}

.search-icon {
  flex: 0 0 auto;
  font-size: 18px;
}

.search-field input {
  min-width: 0;
  width: 100%;
  height: 100%;
  padding: 0;
  color: var(--text-primary);
  font: inherit;
  font-size: 14px;
  background: transparent;
  border: 0;
  outline: 0;
}

.search-field input::placeholder {
  color: var(--text-tertiary);
  opacity: 0.8;
}

.clear-button {
  width: 28px;
  height: 28px;
  display: inline-grid;
  flex: 0 0 auto;
  padding: 0;
  place-items: center;
  color: var(--text-tertiary);
  background: transparent;
  border: 0;
  cursor: pointer;
}

.clear-button:hover,
.clear-button:focus-visible {
  color: var(--primary-color);
  outline: 0;
}

.toolbar-select,
.mode-button,
.direction-button {
  min-width: 0;
  height: 50px;
  border-left: 1px solid var(--border-base);
  border-radius: 0 !important;
}

.mode-button,
.direction-button {
  color: var(--text-secondary);
}

:deep(.toolbar-select .n-base-selection),
:deep(.toolbar-select .n-base-selection-label),
:deep(.toolbar-select .n-base-selection-overlay) {
  height: 50px !important;
  min-height: 50px !important;
  background: transparent !important;
  border: 0 !important;
  border-radius: 0 !important;
  box-shadow: none !important;
}

:deep(.toolbar-select .n-base-selection-label) {
  padding: 0 16px !important;
}

:deep(.toolbar-select .n-base-selection__border),
:deep(.toolbar-select .n-base-selection__state-border) {
  display: none !important;
}

:deep(.toolbar-select .n-base-selection-input__content),
:deep(.toolbar-select .n-base-selection-placeholder) {
  color: var(--text-secondary) !important;
  font-size: 13px !important;
}

:deep(.toolbar-select:hover),
.mode-button:hover,
.direction-button:hover {
  background: var(--bg-hover) !important;
}

@media (max-width: 1180px) {
  .search-toolbar {
    grid-template-columns: minmax(240px, 1fr) 132px 132px 50px 124px 48px;
  }

  .mode-label {
    display: none;
  }
}

@media (max-width: 820px) {
  .search-toolbar {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 46px 46px;
    border: 1px solid var(--border-base);
  }

  .search-field {
    grid-column: 1 / -1;
    border-bottom: 1px solid var(--border-base);
  }

  .category-select {
    grid-column: 1;
  }

  .tag-select {
    grid-column: 2;
  }

  .sort-select {
    grid-column: 1 / 3;
    grid-row: 3;
    border-top: 1px solid var(--border-base);
    border-left: 0;
  }

  .mode-button {
    grid-column: 3;
    grid-row: 2 / 4;
  }

  .direction-button {
    grid-column: 4;
    grid-row: 2 / 4;
  }
}

@media (max-width: 520px) {
  .search-toolbar {
    grid-template-columns: minmax(0, 1fr) 44px 44px;
  }

  .category-select,
  .tag-select,
  .sort-select {
    grid-column: 1;
  }

  .tag-select {
    grid-row: 3;
    border-top: 1px solid var(--border-base);
    border-left: 0;
  }

  .sort-select {
    grid-row: 4;
  }

  .mode-button {
    grid-column: 2;
    grid-row: 2 / 5;
  }

  .direction-button {
    grid-column: 3;
    grid-row: 2 / 5;
  }
}
</style>
