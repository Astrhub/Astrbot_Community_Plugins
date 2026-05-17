<template>
  <header ref="fullHeader" class="app-header">
    <nav class="top-nav" aria-label="主导航">
      <div class="brand">
        <img :src="siteIconUrl" :alt="siteName" class="brand-logo" width="40" height="40">
        <span class="brand-name">{{ siteName }}</span>
      </div>
      <div class="nav-actions">
        <theme-mode-button class="theme-button" />
        <n-dropdown
          v-if="currentUser"
          :options="userMenuOptions"
          trigger="click"
          @select="handleUserMenuSelect"
        >
          <n-button secondary type="primary">
            {{ displayUserName }}
          </n-button>
        </n-dropdown>
        <n-button v-if="isAdminUser" secondary @click="goAdminPlugins">插件审核</n-button>
        <n-button v-if="!currentUser" secondary type="primary" @click="openLoginModal">
          <template #icon>
            <n-icon><log-in-outline /></n-icon>
          </template>
          登录
        </n-button>
      </div>
    </nav>

    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">{{ siteSubtitle }}</p>
        <p class="hero-subtitle">{{ siteDescription }}</p>
      </div>
      <div class="hero-toolbar">
        <n-button type="primary" size="large" class="source-copy-button" @click="copyPluginSource">
          <template #icon>
            <n-icon><link-outline /></n-icon>
          </template>
          复制 AstrBot 插件源
        </n-button>
        <search-toolbar
          class="hero-search-toolbar"
          :search-query="searchQuery"
          :current-page="currentPage"
          :sort-by="sortBy"
          :sort-direction="sortDirection"
          :fuzzy-search-enabled="fuzzySearchEnabled"
          :on-header="true"
          @update:searchQuery="handleSearchQueryChange"
          @update:currentPage="handleCurrentPageChange"
          @update:sortBy="handleSortByChange"
          @update:sortDirection="handleSortDirectionChange"
          @update:fuzzySearchEnabled="handleFuzzySearchEnabledChange"
        />
      </div>
    </section>
  </header>

  <header
    class="sticky-header"
    :class="{ 'sticky-header--visible': showStickyHeader, 'is-search-open': isMobileSearchOpen }"
  >
    <div class="sticky-header-content">
      <div class="sticky-header-left">
        <img :src="siteIconUrl" :alt="siteName" class="sticky-logo" width="32" height="32">
        <h2 class="sticky-title" :class="{ 'hidden-on-search': isMobileSearchOpen }">{{ siteName }}</h2>
      </div>
      <div class="sticky-header-center">
        <search-toolbar
          class="sticky-desktop-toolbar"
          :search-query="searchQuery"
          :current-page="currentPage"
          :sort-by="sortBy"
          :sort-direction="sortDirection"
          :fuzzy-search-enabled="fuzzySearchEnabled"
          :compact="true"
          @update:searchQuery="handleSearchQueryChange"
          @update:currentPage="handleCurrentPageChange"
          @update:sortBy="handleSortByChange"
          @update:sortDirection="handleSortDirectionChange"
          @update:fuzzySearchEnabled="handleFuzzySearchEnabledChange"
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
        <n-button
          quaternary
          circle
          class="hide-on-mobile-search"
          @click="copyPluginSource"
          aria-label="复制 AstrBot 插件源"
        >
          <n-icon><link-outline /></n-icon>
        </n-button>
        <n-button
          v-if="isAdminUser"
          quaternary
          circle
          class="hide-on-mobile-search"
          @click="goAdminPlugins"
          aria-label="插件审核"
        >
          <n-icon><shield-checkmark-outline /></n-icon>
        </n-button>
        <n-dropdown
          v-if="currentUser"
          :options="userMenuOptions"
          trigger="click"
          @select="handleUserMenuSelect"
        >
          <n-button
            quaternary
            circle
            class="hide-on-mobile-search"
            :aria-label="`当前用户：${displayUserName}`"
          >
            <n-icon><person-outline /></n-icon>
          </n-button>
        </n-dropdown>
        <n-button
          v-else
          quaternary
          circle
          class="hide-on-mobile-search"
          @click="openLoginModal"
          aria-label="登录"
        >
          <n-icon><log-in-outline /></n-icon>
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
        <theme-mode-button circle class="hide-on-mobile-search" />
      </div>
    </div>
  </header>
  <div class="sticky-header-spacer" aria-hidden="true"></div>

  <n-modal v-model:show="isLoginModalOpen" preset="card" title="登录" class="login-modal">
    <n-form :model="loginForm" label-placement="top">
      <n-form-item label="内部账号">
        <n-input v-model:value="loginForm.username" placeholder="admin" />
      </n-form-item>
      <n-form-item label="密码">
        <n-input
          v-model:value="loginForm.password"
          type="password"
          show-password-on="click"
          placeholder="内部管理员密码"
          @keyup.enter="submitInternalLogin"
        />
      </n-form-item>
      <n-alert v-if="agreementText" type="info" :bordered="false" class="agreement-box">
        <div class="agreement-text">{{ agreementText }}</div>
        <n-checkbox v-model:checked="agreementAccepted">我已阅读并同意以上条款</n-checkbox>
      </n-alert>
      <div class="login-actions">
        <n-button
          v-if="siteConfig.auth.github_login_enabled"
          tertiary
          :disabled="!canSubmitLogin"
          @click="loginWithGithub"
        >
          <template #icon>
            <n-icon><logo-github /></n-icon>
          </template>
          GitHub 登录
        </n-button>
        <n-button type="primary" :loading="isLoggingIn" :disabled="!canSubmitLogin" @click="submitInternalLogin">
          登录
        </n-button>
      </div>
    </n-form>
  </n-modal>
</template>

<script setup>
import { computed, h, onMounted, ref, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { NAlert, NCheckbox, NDropdown, NForm, NFormItem, NIcon, NButton, NInput, NModal, useMessage } from 'naive-ui'
import {
  CloseOutline,
  LinkOutline,
  LogInOutline,
  LogOutOutline,
  LogoGithub,
  PersonOutline,
  SearchOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
} from '@vicons/ionicons5'
import SearchToolbar from './SearchToolbar.vue'
import ThemeModeButton from './ThemeModeButton.vue'
import { usePluginStore } from '../stores/plugins'

defineProps({
  searchQuery: String,
  currentPage: Number,
  totalPages: Number,
  sortBy: String,
  sortDirection: String,
  fuzzySearchEnabled: Boolean,
  selectedTag: String,
  tagOptions: Array
})

const emit = defineEmits([
  'update:searchQuery',
  'update:currentPage',
  'update:sortBy',
  'update:sortDirection',
  'update:fuzzySearchEnabled',
  'update:selectedTag'
])

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { currentUser, siteConfig } = storeToRefs(store)
const { loginWithGithub, loginWithPassword, logout } = store

const fullHeader = ref(null)
const showStickyHeader = ref(false)
const isMobileSearchOpen = ref(false)
const isLoginModalOpen = ref(false)
const isLoggingIn = ref(false)
const agreementAccepted = ref(false)
const loginForm = ref({ username: 'admin', password: '' })
const pluginSourceUrl = computed(() => store.pluginSourceUrl)
const siteName = computed(() => siteConfig.value.name)
const siteIconUrl = computed(() => siteConfig.value.icon_url)
const siteSubtitle = computed(() => siteConfig.value.subtitle)
const siteDescription = computed(() => siteConfig.value.description)
const isCoreAdmin = computed(() => currentUser.value?.role === 'core_admin')
const isAdminUser = computed(() => ['core_admin', 'admin'].includes(currentUser.value?.role))
const displayUserName = computed(() => (
  currentUser.value?.github_login ||
  currentUser.value?.internal_username ||
  currentUser.value?.login ||
  '已登录'
))
const userMenuOptions = computed(() => [
  {
    key: 'profile',
    label: '个人设置',
    icon: renderIcon(PersonOutline)
  },
  {
    key: 'settings',
    label: '系统设置',
    icon: renderIcon(SettingsOutline),
    disabled: !isCoreAdmin.value
  },
  {
    key: 'divider',
    type: 'divider'
  },
  {
    key: 'logout',
    label: '退出登录',
    icon: renderIcon(LogOutOutline)
  }
])
const agreementText = computed(() => {
  const auth = siteConfig.value.auth || {}
  const parts = []
  if (auth.login_agreement_enabled && auth.login_agreement_text) {
    parts.push(auth.login_agreement_text)
  }
  if (auth.service_terms_enabled && auth.service_terms_text) {
    parts.push(auth.service_terms_text)
  }
  return parts.join('\n\n')
})
const canSubmitLogin = computed(() => !agreementText.value || agreementAccepted.value)

const handleSearchQueryChange = (value) => {
  emit('update:searchQuery', value)
}

const handleCurrentPageChange = (value) => {
  emit('update:currentPage', value)
}

const handleSortByChange = (value) => {
  emit('update:sortBy', value)
}

const handleSortDirectionChange = (value) => {
  emit('update:sortDirection', value)
}

const handleFuzzySearchEnabledChange = (value) => {
  emit('update:fuzzySearchEnabled', value)
}

const goSettings = () => {
  router.push('/settings')
}

const goAdminPlugins = () => {
  router.push('/admin/plugins')
}

function renderIcon(icon) {
  return () => h(NIcon, null, { default: () => h(icon) })
}

async function handleUserMenuSelect(key) {
  if (key === 'profile') {
    router.push('/settings/personal')
    return
  }
  if (key === 'settings') {
    goSettings()
    return
  }
  if (key === 'logout') {
    try {
      await logout()
      message.success('已退出登录')
      router.push('/')
    } catch (error) {
      message.error(error.message || '退出失败')
    }
  }
}

const openLoginModal = () => {
  if (!siteConfig.value.auth?.public_login_enabled) {
    message.warning('当前站点已关闭登录')
    return
  }
  isLoginModalOpen.value = true
}

const submitInternalLogin = async () => {
  if (!canSubmitLogin.value) {
    message.warning('请先同意登录条款')
    return
  }
  isLoggingIn.value = true
  try {
    await loginWithPassword(loginForm.value)
    isLoginModalOpen.value = false
    message.success('已登录')
  } catch (error) {
    message.error(error.message || '登录失败')
  } finally {
    isLoggingIn.value = false
  }
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
  max-width: min(420px, 45vw);
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
  overflow: hidden;
  text-overflow: ellipsis;
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
  gap: 18px;
  padding: 28px 0 24px;
}

.hero-copy {
  max-width: 860px;
}

.eyebrow {
  color: var(--primary-color);
  font-weight: 700;
  margin: 0 0 8px;
}

.hero-subtitle {
  max-width: 760px;
  margin: 0;
  color: var(--text-secondary);
  font-size: 16px;
  line-height: 1.55;
}

.hero-toolbar {
  display: grid;
  grid-template-columns: auto minmax(360px, 1fr);
  align-items: center;
  gap: 12px;
}

.source-copy-button {
  min-height: 44px;
  white-space: nowrap;
}

.hero-search-toolbar {
  min-width: 0;
}

.hero-search-toolbar :deep(.search-container) {
  max-width: none;
  margin: 0;
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
  overflow: hidden;
  text-overflow: ellipsis;
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

:global(.login-modal) {
  width: min(420px, calc(100vw - 32px));
  border-radius: 8px;
}

.agreement-box {
  margin-bottom: 18px;
}

.agreement-text {
  max-height: 160px;
  overflow: auto;
  margin-bottom: 12px;
  white-space: pre-wrap;
}

.login-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

@media (max-width: 900px) {
  .hero-toolbar {
    grid-template-columns: 1fr;
  }

  .source-copy-button {
    justify-self: start;
  }
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
    right: 176px;
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

  .is-search-open .hide-on-mobile-search {
    display: none;
  }

  .is-search-open .mobile-inline-search {
    right: 56px;
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
    right: 56px;
  }
}
</style>
