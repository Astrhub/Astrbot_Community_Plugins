<template>
  <div
    class="search-container"
    :class="{
      'search-container--compact': compact,
      'search-container--header': onHeader,
      'search-container--mobile': mobile
    }"
  >
    <div
      class="search-wrapper"
      :class="{
        'search-wrapper--compact': compact,
        'search-wrapper--mobile': mobile
      }"
    >
      <div
        class="custom-search-box"
        :class="{
          'custom-search-box--compact': compact,
          'custom-search-box--mobile': mobile
        }"
      >
        <n-icon class="search-icon"><search /></n-icon>
        <input
          :value="searchQuery"
          @input="handleSearchInput"
          :placeholder="searchPlaceholder"
          type="search"
          name="plugin-search"
          aria-label="搜索插件"
          autocomplete="off"
          spellcheck="false"
          class="search-input"
        />
        <button
          v-if="searchQuery"
          type="button"
          class="clear-button"
          aria-label="清除搜索"
          @click="handleClearSearch"
        >
          <n-icon class="clear-icon" aria-hidden="true">
            <close-circle />
          </n-icon>
        </button>
      </div>
      <n-select
        v-if="showCategoryFilter && hasCategoryFilters"
        :value="props.selectedCategory"
        :options="props.categoryOptions"
        :placeholder="mobile ? '分类' : '官方分类…'"
        aria-label="插件官方分类"
        @update:value="handleCategoryChange"
        :size="controlSize"
        class="sort-select category-select"
        :class="{
          'sort-select--compact': compact,
          'category-select--compact': compact
        }"
      />
      <n-select
        v-if="props.tagOptions.length || mobile"
        :value="props.selectedTag"
        :options="props.tagOptions"
        :placeholder="mobile ? '标签' : '标签…'"
        aria-label="插件标签"
        filterable
        clearable
        :disabled="mobile && props.tagOptions.length === 0"
        @update:value="handleTagChange"
        :size="controlSize"
        class="sort-select tag-select"
        :class="{
          'sort-select--compact': compact,
          'tag-select--compact': compact
        }"
      />
      <n-switch
        v-if="!mobile"
        :value="fuzzySearchEnabled"
        :size="controlSize"
        class="fuzzy-switch"
        aria-label="搜索匹配模式"
        @update:value="handleFuzzySearchChange"
      >
        <template #checked>模糊</template>
        <template #unchecked>精确</template>
      </n-switch>
      <n-button
        v-else
        secondary
        :type="fuzzySearchEnabled ? 'primary' : 'default'"
        :size="controlSize"
        class="mobile-mode-button"
        :aria-label="fuzzySearchEnabled ? '当前模糊搜索，点击切换精确搜索' : '当前精确搜索，点击切换模糊搜索'"
        @click="handleFuzzySearchChange(!fuzzySearchEnabled)"
      >
        {{ fuzzySearchEnabled ? '模糊' : '精确' }}
      </n-button>
      <n-select
        :value="props.sortBy"
        :options="dense ? compactSortOptions : sortOptions"
        aria-label="排序方式"
        @update:value="handleSortChange"
        :size="controlSize"
        class="sort-select sort-by-select"
        :class="{ 'sort-select--compact': compact }"
      />
      <n-button
        secondary
        :size="controlSize"
        class="sort-direction-button"
        :aria-label="sortDirection === 'asc' ? '当前正序，点击切换倒序' : '当前倒序，点击切换正序'"
        @click="toggleSortDirection"
      >
        {{ sortDirection === 'asc' ? '正序' : '倒序' }}
      </n-button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { NButton, NSelect, NIcon, NSwitch } from 'naive-ui'
import { Search, CloseCircle } from '@vicons/ionicons5'

const props = defineProps({
  searchQuery: String,
  currentPage: Number,
  sortBy: String,
  sortDirection: {
    type: String,
    default: 'desc'
  },
  fuzzySearchEnabled: {
    type: Boolean,
    default: false
  },
  selectedCategory: {
    type: String,
    default: 'all'
  },
  categoryOptions: {
    type: Array,
    default: () => []
  },
  selectedTag: {
    type: String,
    default: null
  },
  tagOptions: {
    type: Array,
    default: () => []
  },
  compact: {
    type: Boolean,
    default: false
  },
  onHeader: {
    type: Boolean,
    default: false
  },
  mobile: {
    type: Boolean,
    default: false
  },
  showCategoryFilter: {
    type: Boolean,
    default: true
  }
})

const emit = defineEmits([
  'update:searchQuery',
  'update:currentPage',
  'update:sortBy',
  'update:sortDirection',
  'update:fuzzySearchEnabled',
  'update:selectedCategory',
  'update:selectedTag'
])

const hasCategoryFilters = computed(() =>
  props.categoryOptions.some((option) => option.value !== 'all' && option.value !== 'other')
)

const dense = computed(() => props.compact || props.mobile)
const controlSize = computed(() => dense.value ? 'small' : 'medium')
const searchPlaceholder = computed(() => {
  if (props.mobile) return '搜索插件'
  return props.compact ? '搜索…' : '搜索插件…'
})

const sortOptions = [
  { label: '默认排序', value: 'default' },
  { label: '随机推荐', value: 'random' },
  { label: '按更新时间', value: 'updated' },
  { label: '按 Star 数量', value: 'stars' },
  { label: '按点赞数量', value: 'likes' },
  { label: '按评论数量', value: 'comments' }
]

const compactSortOptions = [
  { label: '默认', value: 'default' },
  { label: '随机', value: 'random' },
  { label: '时间', value: 'updated' },
  { label: 'Star', value: 'stars' },
  { label: '点赞', value: 'likes' },
  { label: '评论', value: 'comments' }
]

const handleSortChange = (value) => {
  emit('update:sortBy', value)
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}

const toggleSortDirection = () => {
  emit('update:sortDirection', props.sortDirection === 'asc' ? 'desc' : 'asc')
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}

const handleFuzzySearchChange = (value) => {
  emit('update:fuzzySearchEnabled', value)
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}

const handleCategoryChange = (value) => {
  emit('update:selectedCategory', value || 'all')
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}

const handleTagChange = (value) => {
  emit('update:selectedTag', value || null)
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}

const handleSearchInput = (e) => {
  const value = e.target.value
  emit('update:searchQuery', value)
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}

const handleClearSearch = () => {
  emit('update:searchQuery', '')
  if (props.currentPage > 1) {
    emit('update:currentPage', 1)
  }
}
</script>

<style scoped>
/* 搜索框容器 */
.search-container {
  display: flex;
  justify-content: center;
  max-width: 800px;
  margin: 0 auto 16px;
  position: relative;
  z-index: 1;
}

.search-wrapper {
  display: flex;
  width: 100%;
  gap: 12px;
  align-items: center;
}

/* 排序选择器样式 */
.sort-select {
  width: 150px;
  flex-shrink: 0;
}

.category-select {
  width: 150px;
}

.tag-select {
  width: 150px;
}

.fuzzy-switch,
.sort-direction-button {
  flex-shrink: 0;
}

.sort-direction-button {
  min-width: 64px;
}

:deep(.sort-select .n-base-selection) {
  background: transparent !important;
  border: 0px solid rgba(0, 0, 0, 0.08) !important;
  transition: border-color 0.2s ease, box-shadow 0.2s ease, background-color 0.2s ease !important;
  height: 44px !important;
  border-radius: 12px !important;
  padding: 0 0px !important;
}

:deep(.sort-select .n-base-selection-overlay) {
  background: var(--input-bg) !important;
  border-radius: 12px !important;
  box-shadow: var(--shadow-sm) !important;
  transition: background-color 0.3s ease, box-shadow 0.3s ease !important;
}

:deep(.sort-select .n-base-selection-overlay:hover) {
  background: var(--input-bg-hover) !important;
  box-shadow: var(--shadow-md) !important;
}

:deep(.sort-select .n-base-selection:focus-within .n-base-selection-overlay) {
  background: var(--input-bg-focus) !important;
  box-shadow: var(--shadow-md), 0 0 0 3px rgba(96, 165, 250, 0.2) !important;
}

:deep(.sort-select .n-base-selection-label) {
  color: var(--input-text) !important;
  background: var(--input-bg, rgba(0, 0, 0, 0.03));
  height: 44px !important;
  display: flex !important;
  align-items: center !important;
  padding: 0 12px !important;
  font-weight: 500 !important;
  transition: background-color 0.2s ease, color 0.2s ease !important;
}

:deep(.sort-select .n-base-selection:hover .n-base-selection-label) {
  background: var(--input-bg-hover) !important;
  color: var(--input-text) !important;
}

:deep(.sort-select .n-base-selection:focus-within .n-base-selection-label) {
  background: var(--input-bg-focus) !important;
  color: var(--primary-color) !important;
}

:deep(.sort-select .n-base-selection-input__content) {
  color: var(--input-text) !important;
  font-weight: 500 !important;
}

:deep(.sort-select .n-base-selection-placeholder) {
  color: var(--input-placeholder) !important;
  font-weight: 400 !important;
  opacity: 0.6;
}

:deep(.sort-select .n-base-selection__border) {
  display: none !important;
}

:deep(.sort-select .n-base-selection__state-border) {
  display: none !important;
}

/* 下拉菜单 */
:deep(.n-base-select-menu) {
  border-radius: 16px !important;
  padding: 8px !important;
  box-shadow: var(--shadow-lg) !important;
  border: none !important;
  background: var(--input-bg) !important;
  color: var(--input-text) !important;
}

:deep(.n-base-select-option) {
  border-radius: 12px !important;
  margin: 2px 0 !important;
  padding: 8px 12px !important;
  transition: background-color 0.2s ease, color 0.2s ease !important;
  color: var(--input-text) !important;
}

:deep(.n-base-select-option:hover) {
  background: var(--input-bg-hover) !important;
}

/* 自定义搜索框  */
.custom-search-box {
  display: flex;
  align-items: center;
  width: 100%;
  height: 44px;
  background: var(--input-bg, rgba(0, 0, 0, 0.03));
  border: 2px solid rgba(0, 0, 0, 0.08);
  border-radius: 12px;
  transition: background-color 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
  overflow: hidden;
  padding: 0 16px;
  gap: 12px;
}

.custom-search-box:hover {
  background: var(--input-bg-hover, rgba(0, 0, 0, 0.04));
  border-color: rgba(0, 0, 0, 0.12);
}

.custom-search-box:focus-within {
  background: var(--input-bg-focus, #ffffff);
  border-color: var(--primary-color);
  box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.15);
}

/* 搜索图标 */
.search-icon {
  color: var(--input-text);
  font-size: 18px;
  flex-shrink: 0;
  opacity: 0.7;
}

.custom-search-box:focus-within .search-icon {
  color: var(--primary-color);
  opacity: 1;
}

.clear-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 999px;
  color: var(--input-text);
  background: transparent;
  cursor: pointer;
  opacity: 0.5;
  transition: opacity 0.2s ease, transform 0.2s ease, background-color 0.2s ease;
}

.clear-button:hover,
.clear-button:focus-visible {
  opacity: 0.85;
  background: var(--primary-light);
  outline: none;
}

.clear-button:focus-visible {
  box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.2);
}

.clear-button:active {
  opacity: 1;
  transform: scale(0.95);
}

.clear-icon {
  color: var(--input-text);
  font-size: 18px;
  flex-shrink: 0;
}

/* 搜索输入框 */
.search-input {
  flex: 1;
  height: 100%;
  border: none;
  outline: none;
  background: transparent;
  color: var(--input-text);
  font-size: 16px;
  font-weight: 500;
  padding: 0;
  margin: 0;
}

.search-input::placeholder {
  color: var(--input-placeholder);
  font-weight: 400;
}

/* 响应式 */
@media (max-width: 768px) {
  .search-container {
    max-width: 90%;
    margin: 0 auto 12px;
  }
}

@media (max-width: 480px) {
  .search-wrapper {
    flex-direction: column;
    gap: 8px;
    align-items: center;
  }

  .sort-select {
    width: 100%;
  }

  .fuzzy-switch,
  .sort-direction-button {
    width: 100%;
    justify-content: center;
  }

  :deep(.sort-select .n-base-selection) {
    height: 40px !important;
    max-width: 120px;
  }

  :deep(.sort-select .n-base-selection-label) {
    height: 40px !important;
  }

  .search-container {
    padding: 0 8px;
    margin-bottom: 12px;
  }
  
  .custom-search-box {
    height: 40px;
    padding: 0 14px;
    gap: 10px;
  }
  
  .search-icon {
    font-size: 16px;
  }
  
  .search-input {
    font-size: 15px;
  }
}

@media (max-width: 360px) {
  .search-container {
    max-width: 80%;
  }
  
  .custom-search-box {
    height: 36px;
    padding: 0 10px;
    gap: 8px;
  }
  
  .search-icon {
    font-size: 15px;
  }
  
  .search-input {
    font-size: 13px;
  }
}

@media (hover: none) and (pointer: coarse) {
  .custom-search-box:hover {
    background: var(--input-bg);
    border-color: var(--input-border);
    box-shadow: var(--shadow-sm);
  }
}

/* ===== Compact 模式样式 ===== */
.search-container--compact {
  margin: 0;
  max-width: 100%;
}

.search-wrapper--compact {
  gap: 8px;
}

.custom-search-box--compact {
  height: 36px;
  padding: 0 12px;
  gap: 8px;
  border-radius: 8px;
  background: var(--bg-card);
  border: 2px solid var(--border-base);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: none;
}

.custom-search-box--compact .search-icon {
  font-size: 16px;
  color: var(--text-secondary);
}

.custom-search-box--compact .search-input {
  font-size: 14px;
  color: var(--text-secondary);
}

.custom-search-box--compact .search-input::placeholder {
  color: var(--text-tertiary);
  opacity: 0.8;
}

.custom-search-box--compact .clear-icon {
  font-size: 16px;
  color: var(--text-secondary);
}

.sort-select--compact {
  width: 80px;
}

.category-select--compact,
.tag-select--compact {
  width: 112px;
}

.search-wrapper--compact .fuzzy-switch {
  max-width: 74px;
}

.search-wrapper--compact .sort-direction-button {
  min-width: 52px;
  padding: 0 8px;
}

:deep(.sort-select--compact .n-base-selection) {
  height: 36px !important;
  background: var(--bg-card) !important;
  border: 2px solid var(--border-base) !important;
  border-radius: 8px !important;
  backdrop-filter: blur(10px) !important;
  -webkit-backdrop-filter: blur(10px) !important;
  box-shadow: none !important;
}

:deep(.sort-select--compact .n-base-selection:hover) {
  background: var(--bg-hover) !important;
  border-color: var(--primary-color) !important;
}

:deep(.sort-select--compact .n-base-selection:focus-within) {
  background: var(--bg-card) !important;
  border-color: var(--primary-color) !important;
  box-shadow: none !important;
}

:deep(.sort-select--compact .n-base-selection-label) {
  height: 36px !important;
  font-size: 13px !important;
  color: var(--text-secondary) !important;
  background: var(--bg-card) !important;
  padding: 0 8px !important;
}

:deep(.sort-select--compact .n-base-selection-input__content) {
  color: var(--text-secondary) !important;
}
:deep(.sort-select--compact .n-base-selection-placeholder) {
  color: var(--text-tertiary) !important;
}

:deep(.sort-select--compact .n-base-selection-overlay) {
  background: var(--bg-card) !important;
  box-shadow: none !important;
}

.search-container--header :deep(.sort-select .n-base-selection-overlay) {
  background: var(--input-bg) !important;
}
.search-container--header :deep(.sort-select .n-base-selection-overlay:hover) {
  background: var(--input-bg-hover) !important;
}
.search-container--header :deep(.sort-select .n-base-selection:focus-within .n-base-selection-overlay) {
  background: var(--input-bg-focus) !important;
}

.search-container--header :deep(.sort-select .n-base-selection-label) {
  color: var(--input-text) !important;
  background: var(--input-bg) !important;
}
.search-container--header :deep(.sort-select .n-base-selection:hover .n-base-selection-label) {
  background: var(--input-bg-hover) !important;
  color: var(--input-text) !important;
}
.search-container--header :deep(.sort-select .n-base-selection:focus-within .n-base-selection-label) {
  background: var(--input-bg-focus) !important;
}
.search-container--header :deep(.sort-select .n-base-selection-input__content) {
  color: var(--input-text) !important;
}
.search-container--header :deep(.sort-select .n-base-selection-placeholder) {
  color: var(--input-placeholder) !important;
}

.search-container--header .custom-search-box {
  background: var(--input-bg) !important;
  border-color: var(--input-border) !important;
}
.search-container--header .custom-search-box:hover {
  background: var(--input-bg-hover) !important;
  border-color: var(--input-border-hover) !important;
}
.search-container--header .custom-search-box:focus-within {
  background: var(--input-bg-focus) !important;
}

.search-container--header :deep(.n-base-select-menu) {
  background: var(--input-bg) !important;
  color: var(--text-primary) !important;
}
.search-container--header :deep(.n-base-select-option:hover) {
  background: var(--input-bg-hover) !important;
}

.search-container--compact .custom-search-box {
  border: 2px solid var(--border-base);
}

.custom-search-box--compact:hover {
  background: var(--bg-hover);
  border-color: var(--primary-color) !important;
}
.custom-search-box--compact:focus-within {
  background: var(--bg-card);
  border-color: var(--primary-color) !important;
  box-shadow: none;
}

.search-container--header :deep(.n-base-selection-input__content),
.search-container--header :deep(.n-base-selection-label) {
  color: var(--input-text) !important;
}
.search-container--header :deep(.n-base-selection-placeholder) {
  color: var(--input-placeholder) !important;
}

.search-container--compact :deep(.n-base-selection-input__content),
.search-container--compact :deep(.n-base-selection-label) {
  color: var(--text-secondary) !important;
}
.search-container--compact :deep(.n-base-selection-placeholder) {
  color: var(--text-tertiary) !important;
}

.search-container--header .custom-search-box {
  border: none !important;
}
.search-container--header .custom-search-box:hover {
  border: none !important;
}
.search-container--header .custom-search-box:focus-within {
  border: none !important;
}

/* ===== Mobile top filter bar ===== */
.search-container--mobile {
  max-width: 100%;
  margin: 0;
  padding: 0;
}

.search-wrapper--mobile {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 64px 104px 56px 118px;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.custom-search-box--mobile {
  min-width: 0;
  height: 38px;
  padding: 0 10px;
}

.search-wrapper--mobile .sort-by-select {
  width: 104px;
}

.search-wrapper--mobile .tag-select {
  width: 118px;
}

.mobile-mode-button {
  width: 64px;
  min-width: 64px;
  padding: 0 8px;
}

.search-wrapper--mobile .sort-direction-button {
  width: 56px;
  min-width: 56px;
  padding: 0 8px;
}

.search-container--mobile :deep(.sort-select .n-base-selection) {
  width: 100%;
  max-width: none;
  height: 38px !important;
}

.search-container--mobile :deep(.sort-select .n-base-selection-label) {
  height: 38px !important;
  padding: 0 8px !important;
}

@media (max-width: 620px) {
  .search-wrapper--mobile {
    grid-template-columns: minmax(0, 1fr) 64px 92px;
  }

  .search-wrapper--mobile .custom-search-box {
    grid-column: 1 / 3;
  }

  .search-wrapper--mobile .mobile-mode-button {
    grid-column: 3;
    width: 100%;
    min-width: 0;
  }

  .search-wrapper--mobile .sort-by-select,
  .search-wrapper--mobile .tag-select,
  .search-wrapper--mobile .sort-direction-button {
    width: 100%;
    min-width: 0;
  }
}

@media (max-width: 360px) {
  .search-wrapper--mobile {
    grid-template-columns: minmax(0, 1fr) 58px 80px;
    gap: 6px;
  }

  .search-wrapper--mobile .mobile-mode-button,
  .search-wrapper--mobile .sort-direction-button {
    padding: 0 6px;
    font-size: 12px;
  }
}
</style>
