<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { storeToRefs } from "pinia";
import { NIcon, useMessage } from "naive-ui";
import { CopyOutline, SearchOutline } from "@vicons/ionicons5";
import AppFooter from "../components/AppFooter.vue";
import AppHeader from "../components/AppHeader.vue";
import AppPagination from "../components/AppPagination.vue";
import PluginCard from "../components/PluginCard.vue";
import PluginMasonry from "../components/PluginMasonry.vue";
import TagFilter from "../components/TagFilter.vue";
import SearchToolbar from "../components/SearchToolbar.vue";
import { normalizePluginCategory, usePluginStore } from "../stores/plugins";
import { useSeo } from "../composables/useSeo";
import { usePluginLogoPreload } from "../composables/usePluginLogoPreload";

const store = usePluginStore();
const route = useRoute();
const router = useRouter();
const message = useMessage();
const {
  searchQuery,
  selectedTags,
  selectedCategory,
  currentPage,
  sortBy,
  sortDirection,
  fuzzySearchEnabled,
  tagOptions,
  categoryOptions,
  totalPages,
  paginatedPlugins,
  isLoading,
  filteredPlugins,
  randomSeed,
  siteConfig,
  pageSize,
} = storeToRefs(store);

const pluginCount = computed(() => filteredPlugins.value.length);
const authorCount = computed(
  () =>
    new Set(
      filteredPlugins.value.map((plugin) => String(plugin.author || "").trim()).filter(Boolean),
    ).size,
);
const marketSubtitle = computed(() => siteConfig.value.subtitle || "自由的社区插件市场");

useSeo({
  title: "AstrBot 社区插件",
  description: "发现、搜索、评价和提交 AstrBot 社区插件。",
  path: "/",
  jsonLdId: "website-jsonld",
  jsonLd: {
    "@context": "https://schema.org",
    "@type": "WebSite",
    name: "Astrhub 插件市场",
    url: "https://plugins.eloina.cn/",
    potentialAction: {
      "@type": "SearchAction",
      target: "https://plugins.eloina.cn/?q={search_term_string}",
      "query-input": "required name=search_term_string",
    },
  },
});

const { refreshRandomOrder } = store;
const FILTER_QUERY_KEYS = ["q", "tag", "category", "page", "sort", "direction", "fuzzy", "view"];
const SORT_VALUES = new Set(["default", "random", "updated", "stars", "likes", "comments"]);
let applyingRouteQuery = false;
const viewMode = computed(() =>
  firstQueryValue(route.query.view) === "waterfall" ? "waterfall" : "paged",
);
const visiblePlugins = computed(() =>
  viewMode.value === "waterfall"
    ? filteredPlugins.value.slice(0, currentPage.value * pageSize.value)
    : paginatedPlugins.value,
);
const hasMore = computed(() => currentPage.value < totalPages.value);
const sentinel = ref<HTMLElement | null>(null);
const preloadReady = shallowRef(false);
const upcomingPlugins = computed(() =>
  viewMode.value === "waterfall" && preloadReady.value
    ? filteredPlugins.value.slice(
        currentPage.value * pageSize.value,
        (currentPage.value + 1) * pageSize.value,
      )
    : [],
);
usePluginLogoPreload(upcomingPlugins);
let appendObserver: IntersectionObserver | undefined;
let preloadObserver: IntersectionObserver | undefined;
let appending = false;
let mounted = false;

function modeQuery(mode: string) {
  return {
    path: "/",
    query: { ...route.query, view: mode === "waterfall" ? mode : undefined, page: undefined },
  };
}

function setupObservers(): void {
  appendObserver?.disconnect();
  preloadObserver?.disconnect();
  if (
    !mounted ||
    viewMode.value !== "waterfall" ||
    !sentinel.value ||
    typeof IntersectionObserver === "undefined"
  )
    return;
  appendObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void appendNextPage();
    },
    { rootMargin: "350px 0px" },
  );
  preloadObserver = new IntersectionObserver(
    (entries) => {
      preloadReady.value = entries.some((entry) => entry.isIntersecting);
    },
    { rootMargin: "1000px 0px" },
  );
  appendObserver.observe(sentinel.value);
  preloadObserver.observe(sentinel.value);
}

async function appendNextPage(): Promise<void> {
  if (!mounted || appending || isLoading.value || !hasMore.value || viewMode.value !== "waterfall")
    return;
  appending = true;
  currentPage.value++;
  await nextTick();
  await new Promise<void>((resolve) =>
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve())),
  );
  appending = false;
  if (
    mounted &&
    sentinel.value &&
    sentinel.value.getBoundingClientRect().top < window.innerHeight + 350
  )
    void appendNextPage();
}

function loadMore(event: MouseEvent): void {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0)
    return;
  event.preventDefault();
  void appendNextPage();
}

const nextPageHref = computed(
  () =>
    router.resolve({
      path: "/",
      query: { ...route.query, view: "waterfall", page: String(currentPage.value + 1) },
    }).href,
);

watch(() => route.query, applyQueryState, { immediate: true });
watch(
  [
    searchQuery,
    selectedTags,
    selectedCategory,
    currentPage,
    sortBy,
    sortDirection,
    fuzzySearchEnabled,
  ],
  syncRouteQuery,
);
watch(
  totalPages,
  (pages) => {
    if (pages > 0 && currentPage.value > pages) currentPage.value = pages;
  },
  { immediate: true },
);

onMounted(() => {
  mounted = true;
  store.loadPlugins();
  void nextTick(setupObservers);
});
watch([viewMode, sentinel, isLoading], () => {
  void nextTick(setupObservers);
});
onUnmounted(() => {
  mounted = false;
  appendObserver?.disconnect();
  preloadObserver?.disconnect();
});

async function copyPluginSource(): Promise<void> {
  const value = store.pluginSourceUrl;
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("copy failed");
    }
    message.success("插件源已复制");
  } catch {
    message.error(`复制失败，请手动复制：${value}`);
  }
}

function applyQueryState(): void {
  applyingRouteQuery = true;
  const query = route.query;
  const sortValue = firstQueryValue(query.sort);
  const directionValue = firstQueryValue(query.direction);
  const categoryValue = firstQueryValue(query.category);

  searchQuery.value = firstQueryValue(query.q);
  const tags = queryValues(query.tag);
  if (JSON.stringify(selectedTags.value) !== JSON.stringify(tags)) selectedTags.value = tags;
  selectedCategory.value =
    categoryValue && categoryValue !== "all" ? normalizePluginCategory(categoryValue) : "all";
  currentPage.value = parsePage(firstQueryValue(query.page));
  sortBy.value = SORT_VALUES.has(sortValue) ? sortValue : "default";
  sortDirection.value = directionValue === "desc" ? "desc" : "asc";
  fuzzySearchEnabled.value = ["1", "true"].includes(firstQueryValue(query.fuzzy));
  applyingRouteQuery = false;
}

function syncRouteQuery(): void {
  if (applyingRouteQuery) return;
  const nextQuery = mergedFilterQuery();
  if (queriesEqual(route.query, nextQuery)) return;
  router.replace({ query: nextQuery });
}

function mergedFilterQuery(): Record<string, unknown> {
  const query: Record<string, unknown> = { ...route.query };
  FILTER_QUERY_KEYS.forEach((key) => delete query[key]);
  if (searchQuery.value.trim()) query.q = searchQuery.value.trim();
  if (selectedTags.value.length) query.tag = selectedTags.value;
  if (selectedCategory.value && selectedCategory.value !== "all") {
    query.category = selectedCategory.value;
  }
  if (currentPage.value > 1) query.page = String(currentPage.value);
  if (sortBy.value !== "default") query.sort = sortBy.value;
  if (sortDirection.value !== "asc") query.direction = sortDirection.value;
  if (fuzzySearchEnabled.value) query.fuzzy = "1";
  if (viewMode.value === "waterfall") query.view = "waterfall";
  return query;
}

function firstQueryValue(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] || "");
  return String(value || "");
}

function queryValues(value: unknown): string[] {
  return [
    ...new Set(
      (Array.isArray(value) ? value : [value])
        .filter((item) => item !== null && item !== undefined)
        .map((item) => String(item).trim())
        .filter(Boolean),
    ),
  ];
}

function parsePage(value: string): number {
  const page = Number.parseInt(value, 10);
  return Number.isFinite(page) && page > 0 ? page : 1;
}

function queriesEqual(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const normalizedLeft = normalizeQuery(left);
  const normalizedRight = normalizeQuery(right);
  const leftKeys = Object.keys(normalizedLeft).sort();
  const rightKeys = Object.keys(normalizedRight).sort();
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every(
    (key, index) => key === rightKeys[index] && normalizedLeft[key] === normalizedRight[key],
  );
}

function normalizeQuery(query: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(query)
      .filter(([, value]) => queryValues(value).length)
      .map(([key, value]) => [key, JSON.stringify(queryValues(value))]),
  );
}
</script>

<template>
  <div class="home-page">
    <app-header />

    <section class="market-summary" aria-labelledby="market-title">
      <div class="market-heading">
        <h1 id="market-title">社区插件市场</h1>
        <span aria-hidden="true">›</span>
        <p>{{ marketSubtitle }}</p>
      </div>
      <div class="market-stats" aria-label="插件市场统计">
        <span
          ><strong>{{ pluginCount }}</strong> 个插件</span
        >
        <i aria-hidden="true"></i>
        <span
          ><strong>{{ authorCount }}</strong> 位作者</span
        >
        <i aria-hidden="true"></i>
        <span class="market-stats__open">开源驱动</span>
      </div>
    </section>

    <section class="market-toolbar" aria-label="插件筛选与操作">
      <search-toolbar
        v-model:search-query="searchQuery"
        v-model:selected-category="selectedCategory"
        v-model:current-page="currentPage"
        v-model:sort-by="sortBy"
        v-model:sort-direction="sortDirection"
        v-model:fuzzy-search-enabled="fuzzySearchEnabled"
        :category-options="categoryOptions"
        :on-header="true"
        @refresh-random="refreshRandomOrder"
      />
      <div class="toolbar-actions">
        <button type="button" class="toolbar-action" @click="copyPluginSource">
          <n-icon><copy-outline /></n-icon>
          复制插件源
        </button>
        <router-link class="toolbar-action toolbar-action--primary" to="/submit">
          <span aria-hidden="true">+</span>
          提交插件
        </router-link>
      </div>
    </section>

    <tag-filter
      class="market-tags"
      :model-value="selectedTags"
      :options="tagOptions"
      @update:model-value="store.setSelectedTags"
    />

    <div class="browse-toolbar">
      <span>{{
        viewMode === "waterfall"
          ? `已显示 ${visiblePlugins.length} / ${pluginCount} 个插件`
          : `共 ${pluginCount} 个插件`
      }}</span>
      <nav class="view-switch" aria-label="插件浏览方式">
        <router-link
          :to="modeQuery('paged')"
          :class="{ active: viewMode === 'paged' }"
          :aria-current="viewMode === 'paged' ? 'true' : undefined"
          >分页</router-link
        >
        <router-link
          :to="modeQuery('waterfall')"
          :class="{ active: viewMode === 'waterfall' }"
          :aria-current="viewMode === 'waterfall' ? 'true' : undefined"
          >瀑布流</router-link
        >
      </nav>
    </div>

    <main :class="viewMode === 'waterfall' ? 'plugins-waterfall' : 'plugins-grid'">
      <div v-if="isLoading" class="loading-container">
        <div class="loading-dots" aria-label="正在加载插件数据">
          <span></span><span></span><span></span>
        </div>
      </div>

      <div v-else-if="filteredPlugins.length === 0" class="empty-state">
        <n-icon size="40"><search-outline /></n-icon>
        <h2>没有找到相关插件</h2>
        <p>
          {{
            searchQuery || selectedTags.length ? "请调整搜索或筛选条件" : "当前没有可用的插件数据"
          }}
        </p>
      </div>

      <plugin-masonry
        v-else-if="viewMode === 'waterfall'"
        :plugins="visiblePlugins"
        :seed="randomSeed"
      />
      <template v-else>
        <plugin-card
          v-for="(plugin, index) in visiblePlugins"
          :key="plugin.id"
          :plugin="plugin"
          :index="index"
          :seed="randomSeed"
        />
      </template>
    </main>

    <div v-if="viewMode === 'waterfall' && !isLoading" ref="sentinel" class="waterfall-more">
      <a v-if="hasMore" :href="nextPageHref" @click="loadMore">继续加载插件</a>
      <span v-else>已展示全部 {{ pluginCount }} 个插件</span>
    </div>
    <div v-else class="bottom-pagination-wrapper">
      <app-pagination v-if="totalPages > 1" v-model="currentPage" :total-pages="totalPages" />
    </div>
    <app-footer />
  </div>
</template>

<style scoped>
.home-page {
  min-height: 100vh;
  color: var(--text-primary);
  background: var(--bg-base);
}

.market-summary,
.market-toolbar,
.plugins-grid,
.plugins-waterfall,
.market-tags,
.browse-toolbar {
  width: min(1824px, calc(100% - 96px));
  margin-right: auto;
  margin-left: auto;
  box-sizing: border-box;
}

.browse-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 0;
  gap: 12px;
  color: var(--text-tertiary);
  font-size: 12px;
}
.view-switch {
  display: flex;
  padding: 3px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-card);
}
.view-switch a {
  padding: 6px 14px;
  color: var(--text-secondary);
  text-decoration: none;
  border-radius: 5px;
}
.view-switch a.active {
  color: var(--primary-color);
  background: var(--primary-light);
}
.waterfall-more {
  min-height: 76px;
  display: grid;
  place-items: center;
  color: var(--text-tertiary);
  font-size: 13px;
}
.waterfall-more a {
  color: var(--primary-color);
  text-decoration: none;
  padding: 12px 24px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.market-summary {
  min-height: 74px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 0 28px;
}

.market-heading,
.market-stats {
  display: flex;
  align-items: center;
}

.market-heading {
  min-width: 0;
  gap: 16px;
}

.market-heading h1 {
  margin: 0;
  color: var(--text-primary);
  font-size: 20px;
  line-height: 1.2;
}

.market-heading > span,
.market-heading p {
  color: var(--text-tertiary);
}

.market-heading p {
  margin: 0;
  font-size: 12px;
}

.market-stats {
  flex: 0 0 auto;
  gap: 16px;
  color: var(--text-secondary);
  font-size: 12px;
}

.market-stats strong {
  margin-right: 5px;
  color: var(--primary-color);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}

.market-stats i {
  width: 1px;
  height: 18px;
  background: var(--border-base);
}

.market-stats__open::before {
  content: "";
  width: 6px;
  height: 6px;
  display: inline-block;
  margin-right: 7px;
  background: var(--metric-like);
  border-radius: 50%;
  vertical-align: 1px;
}

.market-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  background: var(--bg-card);
  border-top: 1px solid var(--border-base);
  border-bottom: 1px solid var(--border-base);
}

.toolbar-actions {
  display: flex;
  align-items: stretch;
}

.toolbar-action {
  min-width: 136px;
  min-height: 50px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 0 18px;
  color: var(--text-primary);
  font: inherit;
  font-size: 13px;
  font-weight: 650;
  text-decoration: none;
  white-space: nowrap;
  background: transparent;
  border: 0;
  border-left: 1px solid var(--border-base);
  cursor: pointer;
}

.toolbar-action:hover,
.toolbar-action:focus-visible {
  color: var(--primary-color);
  background: var(--bg-hover);
  outline: 0;
}

.toolbar-action--primary {
  color: #fff;
  background: var(--primary-color);
  border-left-color: var(--primary-color);
}

.toolbar-action--primary:hover,
.toolbar-action--primary:focus-visible {
  color: #fff;
  background: var(--primary-hover);
}

.plugins-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  align-content: start;
  align-items: stretch;
  border-bottom: 1px solid var(--border-base);
}

.loading-container,
.empty-state {
  grid-column: 1 / -1;
  min-height: 360px;
  display: grid;
  place-items: center;
}

.loading-dots {
  display: flex;
  gap: 7px;
}

.loading-dots span {
  width: 7px;
  height: 7px;
  background: var(--primary-color);
  border-radius: 50%;
  animation: loading-pulse 1.1s ease-in-out infinite alternate;
}

.loading-dots span:nth-child(2) {
  animation-delay: 160ms;
}

.loading-dots span:nth-child(3) {
  animation-delay: 320ms;
}

@keyframes loading-pulse {
  to {
    opacity: 0.25;
    transform: translateY(-4px);
  }
}

.empty-state {
  align-content: center;
  color: var(--text-tertiary);
  text-align: center;
}

.empty-state h2 {
  margin: 14px 0 4px;
  color: var(--text-primary);
  font-size: 17px;
}

.empty-state p {
  margin: 0;
  font-size: 13px;
}

.bottom-pagination-wrapper {
  min-height: 68px;
  display: flex;
  align-items: center;
  justify-content: center;
}

@media (max-width: 1120px) {
  .market-summary,
  .market-toolbar,
  .plugins-grid,
  .plugins-waterfall,
  .market-tags,
  .browse-toolbar {
    width: min(100% - 48px, 1824px);
  }

  .market-toolbar {
    grid-template-columns: 1fr;
  }

  .toolbar-actions {
    justify-content: flex-start;
    border-top: 1px solid var(--border-base);
  }

  .toolbar-actions .toolbar-action:first-child {
    border-left: 0;
  }
}

@media (max-width: 900px) {
  .plugins-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .market-summary {
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    gap: 12px;
    padding-top: 16px;
    padding-bottom: 16px;
  }

  .market-stats {
    flex-wrap: wrap;
    gap: 12px;
  }
}

@media (max-width: 680px) {
  .market-summary,
  .market-toolbar,
  .plugins-grid,
  .market-tags {
    width: 100%;
  }

  .plugins-waterfall,
  .browse-toolbar {
    width: calc(100% - 28px);
  }

  .market-summary {
    min-height: 64px;
    padding: 16px;
  }

  .market-heading {
    gap: 10px;
  }

  .market-heading h1 {
    font-size: 18px;
  }

  .market-heading p {
    max-width: 42vw;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .toolbar-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .toolbar-action {
    min-width: 0;
  }

  .plugins-grid {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .loading-dots span {
    animation: none;
  }
}
</style>
