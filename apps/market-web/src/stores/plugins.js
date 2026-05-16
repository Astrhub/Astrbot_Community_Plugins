import { ref, computed, watch } from 'vue'
import { defineStore } from 'pinia'

const normalizeBaseUrl = (value) => String(value || '').trim().replace(/\/$/, '')
const BASE_URL = normalizeBaseUrl(import.meta.env.VITE_BASE_URL) || window.location.origin

export const usePluginStore = defineStore('plugins', () => {
  const plugins = ref([])
  const currentUser = ref(null)
  const setupStatus = ref({
    required: false,
    missing: [],
    database_configured: true,
    redis_configured: true,
    saved_database_url: '',
    saved_redis_url: '',
    restart_required: false
  })
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
    const response = await fetch(`${apiBaseUrl}/v1/setup/status`, { cache: 'no-store' })
    const data = await response.json().catch(() => ({}))
    setupStatus.value = {
      required: Boolean(data.required),
      missing: Array.isArray(data.missing) ? data.missing : [],
      database_configured: Boolean(data.database_configured),
      redis_configured: Boolean(data.redis_configured),
      saved_database_url: data.saved_database_url || '',
      saved_redis_url: data.saved_redis_url || '',
      restart_required: Boolean(data.restart_required)
    }
    return setupStatus.value
  }

  async function saveSetupConfig(payload) {
    const response = await fetch(`${apiBaseUrl}/v1/setup`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || '保存失败')
    await loadSetupStatus()
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
    loadSetupStatus,
    loadPlugins,
    loadCurrentUser,
    loginWithGithub,
    saveSetupConfig,
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
