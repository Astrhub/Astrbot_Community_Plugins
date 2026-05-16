import { ref, computed, watch } from 'vue'
import { defineStore } from 'pinia'

const normalizeBaseUrl = (value) => String(value || '').trim().replace(/\/$/, '')
const BASE_URL = normalizeBaseUrl(import.meta.env.VITE_BASE_URL) || window.location.origin
const DEFAULT_SITE_CONFIG = Object.freeze({
  name: 'AstrBot Community Plugins',
  icon_url: '/logo.webp',
  subtitle: '全新社区插件市场',
  description: '发现、评价和提交 AstrBot 插件。',
  contact_email: '',
  docs_url: 'https://docs.astrbot.app/dev/star/plugin.html',
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
    admin_org: ''
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
  const randomSeed = ref(0)
  const irisMaskActive = ref(false)
  const irisMaskPosition = ref({ x: window.innerWidth / 2, y: window.innerHeight / 2 })

  const apiBaseUrl = BASE_URL
  const pluginSourceUrl = `${BASE_URL}/plugins.json`
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

  const allTags = computed(() => {
    const tags = new Set()
    plugins.value.forEach((plugin) => {
      if (Array.isArray(plugin.tags)) plugin.tags.forEach((tag) => tags.add(tag))
    })
    return Array.from(tags).sort()
  })

  const tagOptions = computed(() => allTags.value.map((tag) => ({ label: tag, value: tag })))

  const filteredPlugins = computed(() => {
    const searchValue = searchQuery.value ? searchQuery.value.toLowerCase() : ''
    let filtered = plugins.value.filter((plugin) => {
      if (!searchValue && !selectedTag.value) return true
      const matchesSearch = !searchValue ||
        (plugin.name && plugin.name.toLowerCase().includes(searchValue)) ||
        (plugin.display_name && plugin.display_name.toLowerCase().includes(searchValue)) ||
        (plugin.id && plugin.id.toLowerCase().includes(searchValue)) ||
        (plugin.desc && plugin.desc.toLowerCase().includes(searchValue)) ||
        (plugin.author && plugin.author.toLowerCase().includes(searchValue))
      const matchesTag = !selectedTag.value ||
        (Array.isArray(plugin.tags) && plugin.tags.includes(selectedTag.value))
      return matchesSearch && matchesTag
    })

    if (sortBy.value === 'stars') {
      filtered.sort((a, b) => (b.stars || 0) - (a.stars || 0))
    } else if (sortBy.value === 'updated') {
      filtered.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0))
    } else if (sortBy.value === 'random') {
      filtered.sort((a, b) => {
        const ha = stableHash(a.id || a.name || '', randomSeed.value)
        const hb = stableHash(b.id || b.name || '', randomSeed.value)
        return ha - hb
      })
    }

    return filtered
  })

  const totalPages = computed(() => {
    if (sortBy.value === 'random') return filteredPlugins.value.length > 0 ? 1 : 0
    return Math.ceil(filteredPlugins.value.length / pageSize.value)
  })

  const paginatedPlugins = computed(() => {
    if (sortBy.value === 'random') return filteredPlugins.value.slice(0, pageSize.value)
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
      plugins.value = items.map((plugin, index) => {
        const id = plugin.id || plugin.name || `plugin-${index}`
        return {
          ...plugin,
          id,
          name: plugin.name || id,
          display_name: plugin.display_name || plugin.name || id,
          tags: Array.isArray(plugin.tags) ? plugin.tags : [],
          stars: plugin.stars || 0
        }
      })
    } catch (error) {
      console.error('Error loading plugins:', error)
      plugins.value = []
    } finally {
      isLoading.value = false
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
    isLoading,
    randomSeed,
    apiBaseUrl,
    pluginSourceUrl,
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
    saveSetupConfig,
    loadSystemSettings,
    saveSystemSettings,
    sendTestEmail,
    submitPlugin,
    setSearchQuery,
    setSelectedTag,
    setCurrentPage,
    setSortBy,
    toggleTheme,
    useSystemTheme,
    refreshRandomOrder,
    triggerIrisAnimation
  }
})
