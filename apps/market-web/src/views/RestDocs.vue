<script setup>
import { computed, onMounted, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NEmpty,
  NIcon,
  NInput,
  NSelect,
  NSkeleton,
  NSpin,
  NTag,
  useMessage
} from 'naive-ui'
import {
  ArrowBackOutline,
  CopyOutline,
  DocumentTextOutline,
  OpenOutline,
  RefreshOutline,
  SearchOutline
} from '@vicons/ionicons5'
import { storeToRefs } from 'pinia'
import ThemeModeButton from '../components/ThemeModeButton.vue'
import { usePluginStore } from '../stores/plugins'

const METHODS = ['get', 'post', 'put', 'patch', 'delete']
const METHOD_LABELS = {
  get: 'GET',
  post: 'POST',
  put: 'PUT',
  patch: 'PATCH',
  delete: 'DELETE'
}
const METHOD_TAG_TYPES = {
  get: 'info',
  post: 'success',
  put: 'warning',
  patch: 'warning',
  delete: 'error'
}

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { siteConfig } = storeToRefs(store)

const spec = shallowRef(null)
const loading = shallowRef(true)
const errorMessage = shallowRef('')
const searchText = shallowRef('')
const selectedMethod = shallowRef('all')
const selectedTag = shallowRef('all')
const selectedKey = shallowRef('')

const openapiUrl = computed(() => `${store.apiBaseUrl || ''}/openapi.json`)
const apiTitle = computed(() => spec.value?.info?.title || 'AstrBot Community Plugins API')
const apiVersion = computed(() => spec.value?.info?.version || '0.1.0')
const openapiVersion = computed(() => spec.value?.openapi || '3.x')
const operations = computed(() => {
  const paths = spec.value?.paths || {}
  return Object.entries(paths).flatMap(([path, pathItem]) =>
    METHODS
      .filter((method) => pathItem?.[method])
      .map((method) => normalizeOperation(path, method, pathItem[method]))
  )
})
const tags = computed(() => {
  const values = operations.value.map((operation) => operation.tag)
  return Array.from(new Set(values)).sort((a, b) => a.localeCompare(b))
})
const methodOptions = computed(() => [
  { label: '全部方法', value: 'all' },
  ...METHODS
    .filter((method) => operations.value.some((operation) => operation.method === method))
    .map((method) => ({ label: METHOD_LABELS[method], value: method }))
])
const tagOptions = computed(() => [
  { label: '全部标签', value: 'all' },
  ...tags.value.map((tag) => ({ label: tag, value: tag }))
])
const visibleOperations = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return operations.value.filter((operation) => {
    if (selectedMethod.value !== 'all' && operation.method !== selectedMethod.value) return false
    if (selectedTag.value !== 'all' && operation.tag !== selectedTag.value) return false
    if (!keyword) return true
    return operation.search.includes(keyword)
  })
})
const selectedOperation = computed(() =>
  visibleOperations.value.find((operation) => operation.key === selectedKey.value) ||
  visibleOperations.value[0] ||
  null
)
const hasFilters = computed(() =>
  Boolean(searchText.value.trim()) ||
  selectedMethod.value !== 'all' ||
  selectedTag.value !== 'all'
)

watch(visibleOperations, (items) => {
  if (!items.length) {
    selectedKey.value = ''
    return
  }
  if (!items.some((operation) => operation.key === selectedKey.value)) {
    selectedKey.value = items[0].key
  }
}, { immediate: true })

onMounted(loadSpec)

function normalizeOperation(path, method, operation) {
  const tag = operation.tags?.[0] || 'Default'
  const summary = operation.summary || operation.operationId || path
  const description = cleanDescription(operation.description || '')
  const parameters = Array.isArray(operation.parameters) ? operation.parameters : []
  const requestContent = Object.keys(operation.requestBody?.content || {})
  const responses = operation.responses || {}
  const responseCodes = Object.keys(responses)
  const key = `${METHOD_LABELS[method]} ${path}`
  const search = [
    key,
    tag,
    summary,
    description,
    operation.operationId,
    ...parameters.map((parameter) => parameter.name),
    ...responseCodes
  ].join(' ').toLowerCase()

  return {
    key,
    path,
    method,
    tag,
    summary,
    description,
    operationId: operation.operationId || '',
    parameters,
    requestContent,
    responses,
    responseCodes,
    security: operation.security || [],
    deprecated: Boolean(operation.deprecated),
    search
  }
}

function cleanDescription(value) {
  return String(value || '')
    .replace(/<[^>]+>/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function methodType(method) {
  return METHOD_TAG_TYPES[method] || 'default'
}

function selectOperation(operation) {
  selectedKey.value = operation.key
}

function resetFilters() {
  searchText.value = ''
  selectedMethod.value = 'all'
  selectedTag.value = 'all'
}

function goBack() {
  router.push('/')
}

async function loadSpec() {
  loading.value = true
  errorMessage.value = ''
  try {
    const response = await fetch(openapiUrl.value, { cache: 'no-store' })
    if (!response.ok) {
      throw new Error(`OpenAPI 加载失败：HTTP ${response.status}`)
    }
    spec.value = await response.json()
  } catch (error) {
    errorMessage.value = error.message || 'OpenAPI 加载失败'
  } finally {
    loading.value = false
  }
}

async function copyPath(operation) {
  try {
    await navigator.clipboard.writeText(`${METHOD_LABELS[operation.method]} ${operation.path}`)
    message.success('已复制端点')
  } catch {
    message.warning('复制失败')
  }
}
</script>

<template>
  <main class="docs-page">
    <header class="docs-topbar">
      <div class="docs-nav-left">
        <n-button quaternary circle aria-label="返回首页" @click="goBack">
          <template #icon>
            <n-icon><arrow-back-outline /></n-icon>
          </template>
        </n-button>
        <div class="docs-brand">
          <img :src="siteConfig.icon_url" :alt="siteConfig.name" class="docs-logo">
          <div class="docs-brand-copy">
            <strong>REST API 文档</strong>
            <span>{{ apiTitle }} · v{{ apiVersion }} · OpenAPI {{ openapiVersion }} · {{ operations.length }} 个端点</span>
          </div>
        </div>
      </div>
      <div class="docs-actions">
        <n-button tag="a" href="/openapi.json" target="_blank" secondary>
          <template #icon>
            <n-icon><open-outline /></n-icon>
          </template>
          OpenAPI
        </n-button>
        <theme-mode-button circle />
      </div>
    </header>

    <section class="docs-toolbar" aria-label="文档筛选">
      <n-input
        v-model:value="searchText"
        clearable
        class="docs-search"
        placeholder="搜索路径、标签、摘要、参数"
      >
        <template #prefix>
          <n-icon><search-outline /></n-icon>
        </template>
      </n-input>
      <n-select
        v-model:value="selectedMethod"
        :options="methodOptions"
        class="docs-select"
        aria-label="按 HTTP 方法筛选"
      />
      <n-select
        v-model:value="selectedTag"
        :options="tagOptions"
        class="docs-select"
        aria-label="按标签筛选"
      />
      <n-button secondary :loading="loading" @click="loadSpec">
        <template #icon>
          <n-icon><refresh-outline /></n-icon>
        </template>
        刷新
      </n-button>
    </section>

    <n-alert
      v-if="errorMessage"
      type="error"
      :bordered="false"
      class="docs-alert"
    >
      {{ errorMessage }}
    </n-alert>

    <n-spin :show="loading">
      <section class="docs-content">
        <aside class="endpoint-panel" aria-label="端点列表">
          <div class="panel-title">
            <span>{{ visibleOperations.length }} 个端点</span>
            <n-button v-if="hasFilters" quaternary size="small" @click="resetFilters">重置</n-button>
          </div>
          <div v-if="loading" class="endpoint-skeleton">
            <n-skeleton v-for="index in 8" :key="index" height="64px" round />
          </div>
          <n-empty
            v-else-if="visibleOperations.length === 0"
            description="没有匹配的端点"
            class="docs-empty"
          />
          <div v-else class="endpoint-list">
            <button
              v-for="operation in visibleOperations"
              :key="operation.key"
              type="button"
              class="endpoint-item"
              :class="{ 'is-active': selectedOperation?.key === operation.key }"
              @click="selectOperation(operation)"
            >
              <n-tag :type="methodType(operation.method)" size="small" class="method-tag">
                {{ METHOD_LABELS[operation.method] }}
              </n-tag>
              <span class="endpoint-path">{{ operation.path }}</span>
              <span class="endpoint-summary">{{ operation.summary }}</span>
            </button>
          </div>
        </aside>

        <article v-if="selectedOperation" class="operation-panel">
          <div class="operation-heading">
            <div>
              <div class="operation-kicker">
                <n-tag :type="methodType(selectedOperation.method)" size="small">
                  {{ METHOD_LABELS[selectedOperation.method] }}
                </n-tag>
                <n-tag size="small" round>{{ selectedOperation.tag }}</n-tag>
                <n-tag v-if="selectedOperation.deprecated" type="warning" size="small">Deprecated</n-tag>
              </div>
              <h2>{{ selectedOperation.path }}</h2>
              <p>{{ selectedOperation.summary }}</p>
            </div>
            <n-button secondary circle :aria-label="`复制 ${selectedOperation.path}`" @click="copyPath(selectedOperation)">
              <template #icon>
                <n-icon><copy-outline /></n-icon>
              </template>
            </n-button>
          </div>

          <p v-if="selectedOperation.description" class="operation-description">
            {{ selectedOperation.description }}
          </p>

          <section class="detail-section">
            <h3>
              <n-icon><document-text-outline /></n-icon>
              参数
            </h3>
            <div v-if="selectedOperation.parameters.length" class="parameter-list">
              <div
                v-for="parameter in selectedOperation.parameters"
                :key="`${parameter.in}:${parameter.name}`"
                class="parameter-row"
              >
                <div>
                  <strong>{{ parameter.name }}</strong>
                  <span>{{ parameter.in }}</span>
                </div>
                <n-tag v-if="parameter.required" type="warning" size="small">required</n-tag>
                <p>{{ cleanDescription(parameter.description || parameter.schema?.type || '未描述') }}</p>
              </div>
            </div>
            <p v-else class="muted-text">无参数</p>
          </section>

          <section class="detail-section">
            <h3>请求体</h3>
            <div v-if="selectedOperation.requestContent.length" class="chip-list">
              <n-tag
                v-for="contentType in selectedOperation.requestContent"
                :key="contentType"
                round
              >
                {{ contentType }}
              </n-tag>
            </div>
            <p v-else class="muted-text">无请求体</p>
          </section>

          <section class="detail-section">
            <h3>响应</h3>
            <div class="response-list">
              <div
                v-for="code in selectedOperation.responseCodes"
                :key="code"
                class="response-row"
              >
                <n-tag :type="code.startsWith('2') ? 'success' : code.startsWith('4') || code.startsWith('5') ? 'error' : 'default'">
                  {{ code }}
                </n-tag>
                <span>{{ selectedOperation.responses[code]?.description || '未描述' }}</span>
              </div>
            </div>
          </section>

          <section v-if="selectedOperation.operationId || selectedOperation.security.length" class="detail-section meta-section">
            <div v-if="selectedOperation.operationId">
              <span>operationId</span>
              <code>{{ selectedOperation.operationId }}</code>
            </div>
            <div>
              <span>auth</span>
              <code>{{ selectedOperation.security.length ? 'required' : 'public' }}</code>
            </div>
          </section>
        </article>
      </section>
    </n-spin>
  </main>
</template>

<style scoped>
.docs-page {
  min-height: 100vh;
  color: var(--text-primary);
  background: var(--bg-base);
}

.docs-topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 52px;
  padding: 8px clamp(14px, 3vw, 36px);
  background: var(--sticky-bg);
  border-bottom: 1px solid var(--border-base);
  backdrop-filter: blur(18px);
}

.docs-nav-left,
.docs-actions,
.docs-brand,
.operation-kicker,
.detail-section h3,
.chip-list,
.response-row,
.meta-section > div {
  display: flex;
  align-items: center;
}

.docs-nav-left,
.docs-actions {
  gap: 12px;
}

.docs-brand {
  min-width: 0;
  gap: 10px;
}

.docs-logo {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  object-fit: cover;
  box-shadow: var(--shadow-sm);
}

.docs-brand-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.docs-brand-copy strong,
.docs-brand-copy span,
.endpoint-path,
.endpoint-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.docs-brand-copy strong {
  max-width: 180px;
  color: var(--text-primary);
  font-size: 14px;
}

.docs-brand-copy span {
  max-width: min(58vw, 680px);
  color: var(--text-tertiary);
  font-size: 12px;
}

.docs-toolbar {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) 160px 180px auto;
  gap: 12px;
  padding: 12px clamp(14px, 3vw, 36px);
  border-bottom: 1px solid var(--border-base);
  background: var(--bg-card);
}

.docs-search,
.docs-select {
  min-width: 0;
}

.docs-alert {
  margin: 12px clamp(14px, 3vw, 36px) 0;
}

.docs-content {
  display: grid;
  grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
  gap: 16px;
  padding: 14px clamp(14px, 3vw, 36px) 36px;
}

.endpoint-panel,
.operation-panel {
  min-width: 0;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-card);
  box-shadow: var(--shadow-sm);
}

.endpoint-panel {
  align-self: start;
  position: sticky;
  top: 68px;
  max-height: calc(100vh - 82px);
  overflow: hidden;
}

.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
  border-bottom: 1px solid var(--border-base);
  color: var(--text-secondary);
  font-weight: 600;
}

.endpoint-list,
.endpoint-skeleton {
  display: grid;
  gap: 8px;
  max-height: calc(100vh - 138px);
  overflow: auto;
  padding: 10px;
}

.endpoint-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px 10px;
  width: 100%;
  min-height: 74px;
  padding: 12px;
  text-align: left;
  color: var(--text-primary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
}

.endpoint-item:hover,
.endpoint-item.is-active {
  background: var(--bg-hover);
  border-color: var(--border-hover);
}

.method-tag {
  justify-self: start;
  font-weight: 700;
}

.endpoint-path {
  align-self: center;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}

.endpoint-summary {
  grid-column: 1 / -1;
  color: var(--text-tertiary);
  font-size: 13px;
}

.operation-panel {
  padding: clamp(18px, 3vw, 28px);
}

.operation-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border-base);
}

.operation-kicker {
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.operation-heading h2 {
  margin: 0;
  color: var(--text-primary);
  overflow-wrap: anywhere;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 24px;
  line-height: 1.3;
  letter-spacing: 0;
}

.operation-heading p {
  margin: 10px 0 0;
  color: var(--text-secondary);
  font-size: 15px;
}

.operation-description {
  margin: 18px 0 0;
  padding: 14px 16px;
  color: var(--text-secondary);
  line-height: 1.75;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-base);
}

.detail-section {
  margin-top: 22px;
}

.detail-section h3 {
  gap: 8px;
  margin: 0 0 12px;
  color: var(--text-primary);
  font-size: 16px;
  letter-spacing: 0;
}

.parameter-list,
.response-list {
  display: grid;
  gap: 10px;
}

.parameter-row,
.response-row,
.meta-section > div {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-base);
}

.parameter-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px 12px;
}

.parameter-row div {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.parameter-row strong {
  color: var(--text-primary);
  overflow-wrap: anywhere;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

.parameter-row span,
.meta-section span {
  color: var(--text-tertiary);
  font-size: 12px;
}

.parameter-row p {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--text-secondary);
  line-height: 1.6;
}

.chip-list {
  flex-wrap: wrap;
  gap: 8px;
}

.response-row {
  gap: 12px;
}

.response-row span {
  min-width: 0;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.meta-section {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.meta-section > div {
  justify-content: space-between;
  gap: 12px;
}

.meta-section code {
  min-width: 0;
  color: var(--text-primary);
  overflow-wrap: anywhere;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

.muted-text,
.docs-empty {
  color: var(--text-tertiary);
}

.muted-text {
  margin: 0;
}

@media (max-width: 980px) {
  .docs-content,
  .docs-toolbar {
    grid-template-columns: 1fr;
  }

  .endpoint-panel {
    position: static;
    max-height: none;
  }

  .endpoint-list,
  .endpoint-skeleton {
    max-height: 380px;
  }
}

@media (max-width: 640px) {
  .docs-topbar {
    gap: 10px;
  }

  .docs-actions {
    flex: 0 0 auto;
  }

  .docs-brand-copy strong {
    max-width: 120px;
  }

  .docs-brand-copy span {
    display: none;
  }

  .meta-section {
    grid-template-columns: 1fr;
  }

  .operation-heading {
    align-items: stretch;
    flex-direction: column;
  }

  .operation-heading h2 {
    font-size: 19px;
  }
}
</style>
