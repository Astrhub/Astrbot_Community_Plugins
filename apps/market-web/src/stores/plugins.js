import { ref, computed, watch } from 'vue'
import { defineStore } from 'pinia'

const normalizeBaseUrl = (value) => String(value || '').trim().replace(/\/$/, '')
const BASE_URL = normalizeBaseUrl(import.meta.env.VITE_BASE_URL) || window.location.origin
const COMMUNITY_REPO_URL = String(import.meta.env.VITE_COMMUNITY_REPO_URL || '')
const DEFAULT_SITE_CONFIG = Object.freeze({
  name: 'AstrBot Community Plugins',
  icon_url: '/logo.webp',
  subtitle: '全新社区插件市场',
  description: '发现、评价和提交 AstrBot 插件。',
  contact_email: '',
  docs_url: 'https://docs.astrbot.app/dev/star/plugin-new.html',
  auth: {
    github_login_enabled: false,
    public_login_enabled: true,
    login_agreement_enabled: false,
    login_agreement_text: '',
    service_terms_enabled: false,
    service_terms_text: '',
    terms_revision: ''
  },
  market: {
    submissions_enabled: true,
    comments_enabled: true,
    likes_enabled: true,
    max_plugin_tags: 8
  }
})

const createDefaultSetupConfig = () => ({
  site: {
    name: DEFAULT_SITE_CONFIG.name,
    icon_url: DEFAULT_SITE_CONFIG.icon_url,
    subtitle: DEFAULT_SITE_CONFIG.subtitle,
    description: DEFAULT_SITE_CONFIG.description,
    contact_email: DEFAULT_SITE_CONFIG.contact_email,
    docs_url: DEFAULT_SITE_CONFIG.docs_url
  },
  admin: {
    username: 'admin',
    password: ''
  },
  auth: { ...DEFAULT_SITE_CONFIG.auth },
  github: {
    client_id: '',
    client_secret: '',
    callback_url: `${BASE_URL}/v1/auth/github/callback`,
    scope: 'read:user user:email read:org',
    admin_org: '',
    api_token: '',
    api_token_configured: false,
    metadata_sync_enabled: true,
    metadata_sync_interval_seconds: 3600
  },
  market: {
    submissions_enabled: true,
    comments_enabled: true,
    likes_enabled: true,
    plugin_auto_approve_enabled: false,
    max_plugin_tags: 8
  },
  email: {
    provider: 'disabled',
    smtp: {
      host: '',
      port: 587,
      username: '',
      password: '',
      from_address: '',
      ssl: false
    },
    cloudflare: {
      account_id: '',
      api_token: '',
      from_address: ''
    },
    daily_limit: 0,
    verification_daily_limit_per_user: 5
  },
  postgres: {
    host: '127.0.0.1',
    port: 5432,
    database: '',
    username: '',
    password: '',
    ssl: false
  },
  redis: {
    host: '127.0.0.1',
    port: 6379,
    database: 0,
    password: '',
    ssl: false
  }
})

export const usePluginStore = defineStore('plugins', () => {
  const plugins = ref([])
  const currentUser = ref(null)
  const setupStatus = ref({
    required: false,
    missing: [],
    database_configured: true,
    redis_configured: true,
    saved_setup: createDefaultSetupConfig(),
    restart_required: false
  })
  const siteConfig = ref({ ...DEFAULT_SITE_CONFIG })
  const searchQuery = ref('')
  const selectedTag = ref(null)
  const currentPage = ref(1)
  const pageSize = ref(12)
  const isDarkMode = ref(false)
  const themeMode = ref('system')
  const isLoading = ref(true)
  const sortBy = ref('default')
  const sortDirection = ref('asc')
  const fuzzySearchEnabled = ref(false)
  const randomSeed = ref(0)
  const irisMaskActive = ref(false)
  const irisMaskPosition = ref({ x: window.innerWidth / 2, y: window.innerHeight / 2 })

  const apiBaseUrl = BASE_URL
  const pluginSourceUrl = `${BASE_URL}/plugins.json`
  const communityRepoUrl = COMMUNITY_REPO_URL
  let mediaQuery = null

  function prefersDark() {
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches || false
  }

  function applyThemeFromSystem() {
    if (themeMode.value === 'system') {
      isDarkMode.value = prefersDark()
    }
  }

  function initTheme() {
    themeMode.value = localStorage.getItem('theme-mode') || 'system'
    if (themeMode.value === 'dark') {
      isDarkMode.value = true
    } else if (themeMode.value === 'light') {
      isDarkMode.value = false
    } else {
      applyThemeFromSystem()
    }

    if (!mediaQuery && window.matchMedia) {
      mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      mediaQuery.addEventListener?.('change', applyThemeFromSystem)
    }
  }

  const toggleTheme = () => {
    themeMode.value = isDarkMode.value ? 'light' : 'dark'
    localStorage.setItem('theme-mode', themeMode.value)
    isDarkMode.value = themeMode.value === 'dark'
  }

  function useSystemTheme() {
    themeMode.value = 'system'
    localStorage.setItem('theme-mode', 'system')
    applyThemeFromSystem()
  }

  function normalizeSiteConfig(value = {}) {
    return {
      name: String(value.name || DEFAULT_SITE_CONFIG.name).trim() || DEFAULT_SITE_CONFIG.name,
      icon_url: String(value.icon_url || DEFAULT_SITE_CONFIG.icon_url).trim() ||
        DEFAULT_SITE_CONFIG.icon_url,
      subtitle: String(value.subtitle ?? DEFAULT_SITE_CONFIG.subtitle).trim(),
      description: String(value.description ?? DEFAULT_SITE_CONFIG.description).trim(),
      contact_email: String(value.contact_email || '').trim(),
      docs_url: String(value.docs_url ?? DEFAULT_SITE_CONFIG.docs_url).trim(),
      auth: { ...DEFAULT_SITE_CONFIG.auth, ...(value.auth || {}) },
      market: { ...DEFAULT_SITE_CONFIG.market, ...(value.market || {}) }
    }
  }

  function applySiteMetadata(config) {
    if (typeof document === 'undefined') return
    document.title = config.name
    setMetaContent('application-name', config.name)
    setMetaContent('og:title', config.name, 'property')
    setMetaContent('og:image', config.icon_url, 'property')
    updateLink('icon', config.icon_url)
    updateLink('shortcut icon', config.icon_url)
    updateLink('preload', config.icon_url)
  }

  function setMetaContent(name, content, attribute = 'name') {
    const element = document.querySelector(`meta[${attribute}="${name}"]`)
    if (element) element.setAttribute('content', content)
  }

  function updateLink(rel, href) {
    const element = document.querySelector(`link[rel="${rel}"]`)
    if (element) element.setAttribute('href', href)
  }

  function applySiteConfig(value) {
    const config = normalizeSiteConfig(value)
    siteConfig.value = config
    applySiteMetadata(config)
    return config
  }

  watch(sortBy, (value) => {
    if (value === 'random') randomSeed.value = Math.random()
  })

  function stableHash(input, seedNumber) {
    let h = (Math.floor(seedNumber * 1e9) ^ 5381) >>> 0
    for (let i = 0; i < input.length; i += 1) {
      h = (((h << 5) + h) + input.charCodeAt(i)) >>> 0
    }
    return h >>> 0
  }

  function normalizeSearchValue(value) {
    return String(value || '').trim().toLowerCase()
  }

  function pluginSearchText(plugin) {
    return [
      plugin.name,
      plugin.display_name,
      plugin.id,
      plugin.desc,
      plugin.short_desc,
      plugin.author,
      plugin.owner_github_login,
      plugin.owner_user_id,
      plugin.repo,
      ...(Array.isArray(plugin.tags) ? plugin.tags : [])
    ].filter(Boolean).join(' ').toLowerCase()
  }

  function pluginMatchesSearch(plugin, searchValue) {
    const text = pluginSearchText(plugin)
    if (!fuzzySearchEnabled.value) return text.includes(searchValue)
    return fuzzyIncludes(text, searchValue)
  }

  function fuzzyIncludes(text, query) {
    let index = 0
    for (const char of query) {
      index = text.indexOf(char, index)
      if (index === -1) return false
      index += 1
    }
    return true
  }

  function comparePlugins(a, b) {
    const direction = sortDirection.value === 'asc' ? 1 : -1
    if (sortBy.value === 'random') {
      return direction * compareRandomPlugins(a, b)
    }
    if (sortBy.value === 'updated') {
      return direction * compareValues(getPluginTime(a), getPluginTime(b))
    }
    return direction * compareValues(getPluginSortValue(a), getPluginSortValue(b))
  }

  function getPluginSortValue(plugin) {
    if (sortBy.value === 'stars') return Number(plugin.stars || 0)
    if (sortBy.value === 'likes') return Number(plugin.likes || 0)
    if (sortBy.value === 'comments') return Number(plugin.comments_count || 0)
    return Number(plugin.list_index || 0)
  }

  function getPluginTime(plugin) {
    return new Date(plugin.updated_at || plugin.created_at || 0).getTime() || 0
  }

  function compareValues(a, b) {
    if (a > b) return 1
    if (a < b) return -1
    return 0
  }

  function compareRandomPlugins(a, b) {
    const ha = stableHash(a.id || a.name || '', randomSeed.value)
    const hb = stableHash(b.id || b.name || '', randomSeed.value)
    return ha - hb
  }

  const allTags = computed(() => {
    const tags = new Set()
    plugins.value.forEach((plugin) => {
      if (Array.isArray(plugin.tags)) plugin.tags.forEach((tag) => tags.add(tag))
    })
    return Array.from(tags).sort()
  })

  const tagOptions = computed(() => allTags.value.map((tag) => ({ label: tag, value: tag })))

  const filteredPlugins = computed(() => {
    const searchValue = normalizeSearchValue(searchQuery.value)
    let filtered = plugins.value.filter((plugin) => {
      if (!searchValue && !selectedTag.value) return true
      const matchesSearch = !searchValue || pluginMatchesSearch(plugin, searchValue)
      const matchesTag = !selectedTag.value ||
        (Array.isArray(plugin.tags) && plugin.tags.includes(selectedTag.value))
      return matchesSearch && matchesTag
    })

    filtered.sort(comparePlugins)

    return filtered
  })

  const totalPages = computed(() => {
    return Math.ceil(filteredPlugins.value.length / pageSize.value)
  })

  const paginatedPlugins = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value
    return filteredPlugins.value.slice(start, start + pageSize.value)
  })

  async function loadPlugins() {
    isLoading.value = true
    try {
      const response = await fetch(`${apiBaseUrl}/v1/plugins`, { cache: 'no-store' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json()
      const items = Array.isArray(data) ? data : (data.items || [])
      plugins.value = items.map((plugin, index) => normalizePluginItem(plugin, index))
    } catch (error) {
      console.error('Error loading plugins:', error)
      plugins.value = []
    } finally {
      isLoading.value = false
    }
  }

  function normalizePluginItem(plugin, index) {
    const id = plugin.id || plugin.name || `plugin-${index}`
    return {
      ...plugin,
      id,
      name: plugin.name || id,
      display_name: plugin.display_name || plugin.name || id,
      version: plugin.version || '1.0.0',
      logo: plugin.logo || '',
      tags: Array.isArray(plugin.tags) ? plugin.tags : [],
      stars: Number(plugin.stars || 0),
      likes: Number(plugin.likes || 0),
      comments_count: Number(plugin.comments_count || 0),
      list_index: index
    }
  }

  async function loadSetupStatus() {
    const response = await fetch(`${apiBaseUrl}/v1/setup/status`, {
      credentials: 'include',
      cache: 'no-store'
    })
    const data = await response.json().catch(() => ({}))
    const site = applySiteConfig(data.site || data.saved_setup?.site || siteConfig.value)
    setupStatus.value = {
      required: Boolean(data.required),
      missing: Array.isArray(data.missing) ? data.missing : [],
      database_configured: Boolean(data.database_configured),
      redis_configured: Boolean(data.redis_configured),
      saved_setup: mergeSetupConfig(data.saved_setup, site),
      restart_required: Boolean(data.restart_required)
    }
    return setupStatus.value
  }

  async function loadSiteConfig() {
    try {
      const response = await fetch(`${apiBaseUrl}/v1/site`, { cache: 'no-store' })
      const data = await response.json().catch(() => ({}))
      return applySiteConfig(response.ok ? data : siteConfig.value)
    } catch {
      return applySiteConfig(siteConfig.value)
    }
  }

  function mergeSetupConfig(value = {}, site = siteConfig.value) {
    const defaults = createDefaultSetupConfig()
    return {
      site: normalizeSiteConfig(value.site || site),
      admin: { ...defaults.admin, ...(value.admin || {}) },
      auth: { ...defaults.auth, ...(value.auth || {}) },
      github: { ...defaults.github, ...(value.github || {}) },
      market: { ...defaults.market, ...(value.market || {}) },
      email: {
        ...defaults.email,
        ...(value.email || {}),
        smtp: { ...defaults.email.smtp, ...(value.email?.smtp || {}) },
        cloudflare: { ...defaults.email.cloudflare, ...(value.email?.cloudflare || {}) }
      },
      postgres: { ...defaults.postgres, ...(value.postgres || {}) },
      redis: { ...defaults.redis, ...(value.redis || {}) }
    }
  }

  async function saveSetupConfig(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/setup`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '保存失败')
    await loadSetupStatus()
    return data
  }

  async function loadSystemSettings() {
    const response = await fetch(`${apiBaseUrl}/v1/admin/settings`, {
      credentials: 'include',
      cache: 'no-store'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '加载设置失败')
    return mergeSetupConfig(data, data.site)
  }

  async function loadAdminPlugins() {
    const response = await fetch(`${apiBaseUrl}/v1/admin/plugins`, {
      credentials: 'include',
      cache: 'no-store'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '加载插件审核列表失败')
    return data.items || []
  }

  async function loadPluginDetail(pluginId) {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}`, {
      credentials: 'include',
      cache: 'no-store'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '加载插件详情失败')
    return data
  }

  async function likePlugin(pluginId) {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/like`, {
      method: 'POST',
      credentials: 'include'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '点赞失败')
    return data
  }

  async function unlikePlugin(pluginId) {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/unlike`, {
      method: 'POST',
      credentials: 'include'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '取消点赞失败')
    return data
  }

  async function addPluginComment(pluginId, payload) {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/comments`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '评论失败')
    return data
  }

  async function deletePluginComment(commentId) {
    const response = await fetch(`${apiBaseUrl}/v1/comments/${commentId}`, {
      method: 'DELETE',
      credentials: 'include'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '删除评论失败')
    return data
  }

  async function likePluginComment(commentId) {
    const response = await fetch(`${apiBaseUrl}/v1/comments/${commentId}/like`, {
      method: 'POST',
      credentials: 'include'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '点赞评论失败')
    return data
  }

  async function unlikePluginComment(commentId) {
    const response = await fetch(`${apiBaseUrl}/v1/comments/${commentId}/unlike`, {
      method: 'POST',
      credentials: 'include'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '取消评论点赞失败')
    return data
  }

  async function updatePluginListing(pluginId, action, payload = null) {
    const options = {
      method: 'POST',
      credentials: 'include'
    }
    if (payload && Object.keys(payload).length > 0) {
      options.headers = { 'content-type': 'application/json' }
      options.body = JSON.stringify(payload)
    }
    const response = await fetch(`${apiBaseUrl}/v1/admin/plugins/${pluginId}/${action}`, options)
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '更新插件状态失败')
    return data
  }

  async function refreshPluginGithubMetadata(pluginId, payload = null) {
    const options = {
      method: 'POST',
      credentials: 'include'
    }
    if (payload && Object.keys(payload).length > 0) {
      options.headers = { 'content-type': 'application/json' }
      options.body = JSON.stringify(payload)
    }
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/refresh-github`, options)
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '刷新 GitHub 数据失败')
    return data
  }

  async function loadNotifications() {
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications`, {
      credentials: 'include',
      cache: 'no-store'
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '加载消息失败')
    return data.items || []
  }

  async function saveSystemSettings(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/admin/settings`, {
      method: 'PUT',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '保存设置失败')
    if (data.settings?.site) {
      applySiteConfig({
        ...data.settings.site,
        auth: data.settings.auth,
        market: data.settings.market
      })
    }
    return data
  }

  async function sendTestEmail(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/admin/settings/email/test`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '测试邮件发送失败')
    return data
  }

  async function loadCurrentUser() {
    try {
      const response = await fetch(`${apiBaseUrl}/v1/me`, { credentials: 'include' })
      if (!response.ok) {
        currentUser.value = null
        return
      }
      currentUser.value = await response.json()
    } catch {
      currentUser.value = null
    }
  }

  function loginWithGithub() {
    window.location.href = `${apiBaseUrl}/v1/auth/github/login`
  }

  async function loginWithPassword(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/auth/internal/login`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '登录失败')
    currentUser.value = data.user
    return data.user
  }

  async function logout() {
    await fetch(`${apiBaseUrl}/v1/auth/logout`, {
      method: 'POST',
      credentials: 'include'
    })
    currentUser.value = null
  }

  async function updateProfile(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/me/profile`, {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '更新失败')
    currentUser.value = data
    return data
  }

  async function submitPlugin(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/submissions`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '提交失败')
    return data
  }

  function setSearchQuery(query) {
    searchQuery.value = query
  }

  function setSelectedTag(tag) {
    selectedTag.value = tag
  }

  function setCurrentPage(page) {
    currentPage.value = page
  }

  function setSortBy(value) {
    sortBy.value = value
    if (value === 'random') randomSeed.value = Math.random()
    currentPage.value = 1
  }

  function setSortDirection(value) {
    sortDirection.value = value === 'asc' ? 'asc' : 'desc'
    currentPage.value = 1
  }

  function setFuzzySearchEnabled(value) {
    fuzzySearchEnabled.value = Boolean(value)
    currentPage.value = 1
  }

  function updatePluginInList(plugin) {
    const pluginId = plugin?.id
    if (!pluginId) return
    plugins.value = plugins.value.map((item) =>
      item.id === pluginId ? { ...item, ...normalizePluginItem(plugin, item.list_index) } : item
    )
  }

  function resetPluginFilters() {
    searchQuery.value = ''
    selectedTag.value = null
    currentPage.value = 1
    sortBy.value = 'updated'
    sortDirection.value = 'desc'
    fuzzySearchEnabled.value = false
  }

  function refreshRandomOrder() {
    if (sortBy.value === 'random') randomSeed.value = Math.random()
  }

  function triggerIrisAnimation(position = null, callback = null) {
    irisMaskPosition.value = position || { x: window.innerWidth / 2, y: window.innerHeight / 2 }
    irisMaskActive.value = true
    setTimeout(() => {
      if (callback) callback()
      setTimeout(() => {
        irisMaskActive.value = false
      }, 400)
    }, 800)
  }

  return {
    plugins,
    currentUser,
    setupStatus,
    siteConfig,
    searchQuery,
    selectedTag,
    currentPage,
    isDarkMode,
    themeMode,
    sortBy,
    sortDirection,
    fuzzySearchEnabled,
    isLoading,
    randomSeed,
    apiBaseUrl,
    pluginSourceUrl,
    communityRepoUrl,
    irisMaskActive,
    irisMaskPosition,
    allTags,
    tagOptions,
    filteredPlugins,
    totalPages,
    paginatedPlugins,
    initTheme,
    loadSiteConfig,
    loadSetupStatus,
    loadPlugins,
    loadCurrentUser,
    loginWithGithub,
    loginWithPassword,
    logout,
    updateProfile,
    saveSetupConfig,
    loadSystemSettings,
    loadAdminPlugins,
    loadPluginDetail,
    likePlugin,
    unlikePlugin,
    addPluginComment,
    deletePluginComment,
    likePluginComment,
    unlikePluginComment,
    updatePluginListing,
    refreshPluginGithubMetadata,
    loadNotifications,
    saveSystemSettings,
    sendTestEmail,
    submitPlugin,
    setSearchQuery,
    setSelectedTag,
    setCurrentPage,
    setSortBy,
    setSortDirection,
    setFuzzySearchEnabled,
    updatePluginInList,
    resetPluginFilters,
    toggleTheme,
    useSystemTheme,
    refreshRandomOrder,
    triggerIrisAnimation
  }
})
