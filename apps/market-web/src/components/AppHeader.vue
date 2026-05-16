<template>
  <header ref="fullHeader" class="app-header">
    <nav class="top-nav" aria-label="主导航">
      <div class="brand">
        <img src="/logo.webp" alt="AstrBot Community Plugins" class="brand-logo" width="40" height="40">
        <span class="brand-name">AstrBot Community</span>
      </div>
      <div class="nav-actions">
        <n-button quaternary class="theme-button" @click="toggleTheme">
          <template #icon>
            <n-icon>
              <moon v-if="!isDarkMode" />
              <sunny v-else />
            </n-icon>
          </template>
          {{ isDarkMode ? '浅色' : '深色' }}
        </n-button>
        <n-button v-if="currentUser" secondary type="primary">
          {{ currentUser.github_login || currentUser.login }}
        </n-button>
        <n-button v-else secondary type="primary" @click="loginWithGithub">
          <template #icon>
            <n-icon><logo-github /></n-icon>
          </template>
          GitHub 登录
        </n-button>
        <n-button type="primary" @click="goSubmit">提交插件</n-button>
      </div>
    </nav>

    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">全新社区插件市场</p>
        <h1>AstrBot Community Plugins</h1>
        <p class="hero-subtitle">
          发现、评价和提交 AstrBot 插件。发布与管理只通过 GitHub OAuth 识别身份。
        </p>
        <div class="hero-actions">
          <n-button type="primary" size="large" class="source-copy-button" @click="copyPluginSource">
            <template #icon>
              <n-icon><link-outline /></n-icon>
            </template>
            复制 AstrBot 插件源
          </n-button>
          <span class="source-url">{{ pluginSourceUrl }}</span>
        </div>
      </div>
      <search-toolbar
        :search-query="searchQuery"
        :current-page="currentPage"
        :sort-by="sortBy"
        :on-header="true"
        @update:searchQuery="handleSearchQueryChange"
        @update:currentPage="handleCurrentPageChange"
        @update:sortBy="handleSortByChange"
      />
    </section>
  </header>

  <header
    class="sticky-header"
    :class="{ 'sticky-header--visible': showStickyHeader, 'is-search-open': isMobileSearchOpen }"
  >
    <div class="sticky-header-content">
      <div class="sticky-header-left">
        <img src="/logo.webp" alt="AstrBot Community Plugins" class="sticky-logo" width="32" height="32">
        <h2 class="sticky-title" :class="{ 'hidden-on-search': isMobileSearchOpen }">Community Plugins</h2>
      </div>
      <div class="sticky-header-center">
        <search-toolbar
          class="sticky-desktop-toolbar"
          :search-query="searchQuery"
          :current-page="currentPage"
          :sort-by="sortBy"
          :compact="true"
          @update:searchQuery="handleSearchQueryChange"
          @update:currentPage="handleCurrentPageChange"
          @update:sortBy="handleSortByChange"
        />
        <div class="mobile-inline-search" :class="{ 'is-open': isMobileSearchOpen }">
          <n-input
            size="medium"
            :value="searchQuery"
            @update:value="handleSearchQueryChange"
            placeholder="搜索插件"
            clearable
            autofocus
          />
        </div>
      </div>
      <div class="sticky-actions">
        <n-button quaternary circle @click="copyPluginSource" aria-label="复制 AstrBot 插件源">
          <n-icon><link-outline /></n-icon>
        </n-button>
        <n-button
          quaternary
          circle
          class="mobile-only"
          @click="toggleMobileSearch"
          :aria-expanded="isMobileSearchOpen"
          :aria-label="isMobileSearchOpen ? '关闭搜索' : '打开搜索'"
        >
          <n-icon>
            <close-outline v-if="isMobileSearchOpen" />
            <search-outline v-else />
          </n-icon>
        </n-button>
        <n-button quaternary circle @click="toggleTheme" :aria-label="isDarkMode ? '切换浅色模式' : '切换深色模式'">
          <n-icon>
            <sunny v-if="isDarkMode" />
            <moon v-else />
          </n-icon>
        </n-button>
        <n-button circle type="primary" class="mobile-only" @click="goSubmit" aria-label="提交插件">
          <n-icon><add-circle-outline /></n-icon>
        </n-button>
      </div>
    </div>
  </header>
  <div class="sticky-header-spacer" aria-hidden="true"></div>
</template>

<script setup>
import { computed, onMounted, ref, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { NIcon, NButton, NInput, useMessage } from 'naive-ui'
import {
  AddCircleOutline,
  CloseOutline,
  LinkOutline,
  LogoGithub,
  Moon,
  SearchOutline,
  Sunny
} from '@vicons/ionicons5'
import SearchToolbar from './SearchToolbar.vue'
import { usePluginStore } from '../stores/plugins'

defineProps({
  searchQuery: String,
  currentPage: Number,
  totalPages: Number,
  sortBy: String,
  selectedTag: String,
  tagOptions: Array
})

const emit = defineEmits([
  'update:searchQuery',
  'update:currentPage',
  'update:sortBy',
  'update:selectedTag'
])

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { isDarkMode, currentUser } = storeToRefs(store)
const { loginWithGithub, toggleTheme } = store

const fullHeader = ref(null)
const showStickyHeader = ref(false)
const isMobileSearchOpen = ref(false)
const pluginSourceUrl = computed(() => store.pluginSourceUrl)

const handleSearchQueryChange = (value) => {
  emit('update:searchQuery', value)
}

const handleCurrentPageChange = (value) => {
  emit('update:currentPage', value)
}

const handleSortByChange = (value) => {
  emit('update:sortBy', value)
}

const goSubmit = () => {
  router.push('/submit')
}

const copyPluginSource = async () => {
  try {
    await writeClipboard(pluginSourceUrl.value)
    message.success('插件源已复制')
  } catch {
    message.error(`复制失败，请手动复制：${pluginSourceUrl.value}`)
  }
}

const writeClipboard = async (value) => {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(value)
    return
  }
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(textarea)
  if (!copied) throw new Error('copy failed')
}

const toggleMobileSearch = () => {
  isMobileSearchOpen.value = !isMobileSearchOpen.value
}

const handleScroll = () => {
  if (window.matchMedia('(max-width: 768px)').matches) {
    showStickyHeader.value = true
    return
  }
  if (!fullHeader.value) return
  showStickyHeader.value = fullHeader.value.getBoundingClientRect().bottom <= 0
}

onMounted(() => {
  window.addEventListener('scroll', handleScroll, { passive: true })
  handleScroll()
})

onUnmounted(() => {
  window.removeEventListener('scroll', handleScroll)
})
</script>

<style scoped>
.app-header {
  padding: 20px;
  margin-bottom: 28px;
  background: var(--header-gradient);
  border-bottom: 1px solid var(--border-base);
  position: relative;
  overflow: hidden;
}

.app-header::before {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--header-overlay);
  pointer-events: none;
}

.top-nav,
.hero {
  position: relative;
  z-index: 1;
  max-width: 1180px;
  margin: 0 auto;
}

.top-nav {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.brand-logo,
.sticky-logo {
  object-fit: contain;
  border-radius: 8px;
}

.brand-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
  white-space: nowrap;
}

.nav-actions {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.theme-button {
  color: var(--text-secondary);
}

.hero {
  display: grid;
  gap: 28px;
  padding: 54px 0 34px;
}

.hero-copy {
  max-width: 780px;
}

.eyebrow {
  color: var(--primary-color);
  font-weight: 700;
  margin: 0 0 10px;
}

.hero h1 {
  margin: 0;
  color: var(--text-primary);
  font-size: clamp(2.2rem, 6vw, 4.8rem);
  line-height: 0.98;
  letter-spacing: 0;
  font-weight: 800;
}

.hero-subtitle {
  max-width: 680px;
  margin: 18px 0 0;
  color: var(--text-secondary);
  font-size: 18px;
  line-height: 1.6;
}

.hero-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 24px;
}

.source-copy-button {
  min-height: 44px;
}

.source-url {
  max-width: min(100%, 520px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 8px 12px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-card);
  color: var(--text-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
}

.sticky-header {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  transform: translateY(-100%);
  opacity: 0;
  transition: transform 0.22s ease, opacity 0.22s ease;
  pointer-events: none;
  backdrop-filter: blur(22px) saturate(140%);
  background: var(--sticky-bg);
  border-bottom: 1px solid var(--border-base);
  box-shadow: var(--shadow-sm);
}

.sticky-header--visible {
  transform: translateY(0);
  opacity: 1;
  pointer-events: auto;
}

.sticky-header-spacer {
  height: 0;
}

.sticky-header-content {
  max-width: 1180px;
  margin: 0 auto;
  padding: 10px 20px;
  display: grid;
  grid-template-columns: auto minmax(240px, 520px) auto;
  align-items: center;
  gap: 18px;
}

.sticky-header-left {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.sticky-title {
  margin: 0;
  color: var(--text-primary);
  font-size: 16px;
  font-weight: 800;
  white-space: nowrap;
}

.sticky-header-center {
  min-width: 0;
}

.sticky-actions {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.mobile-only,
.mobile-inline-search {
  display: none;
}

@media (max-width: 768px) {
  .app-header {
    display: none;
  }

  .sticky-header {
    transform: translateY(0);
    opacity: 1;
    pointer-events: auto;
  }

  .sticky-header-spacer {
    height: 66px;
  }

  .sticky-header-content {
    grid-template-columns: auto 1fr auto;
    padding: 10px 14px;
    gap: 10px;
  }

  .sticky-logo {
    width: 34px;
    height: 34px;
  }

  .sticky-title {
    font-size: 15px;
  }

  .sticky-desktop-toolbar {
    display: none;
  }

  .mobile-only {
    display: inline-flex;
  }

  .mobile-inline-search {
    display: block;
    position: absolute;
    left: 96px;
    right: 128px;
    top: 50%;
    transform: translateY(-50%) scaleY(0.96);
    opacity: 0;
    pointer-events: none;
    transition: transform 0.18s ease, opacity 0.18s ease;
  }

  .mobile-inline-search.is-open {
    opacity: 1;
    transform: translateY(-50%) scaleY(1);
    pointer-events: auto;
  }

  .hidden-on-search {
    display: none;
  }

  .nav-actions {
    display: none;
  }
}

@media (max-width: 480px) {
  .sticky-title {
    font-size: 14px;
  }

  .mobile-inline-search {
    left: 58px;
    right: 118px;
  }
}
</style>
