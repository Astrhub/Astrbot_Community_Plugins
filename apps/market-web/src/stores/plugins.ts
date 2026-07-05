import { ref, shallowRef, computed, watch } from "vue";
import { defineStore } from "pinia";
import type {
  Announcement,
  AdminUser,
  ApiKey,
  AppNotification,
  Comment,
  HelpSection,
  MarketConfig,
  PageOptions,
  PaginatedResult,
  Plugin,
  PluginCategory,
  PluginDetail,
  PluginSortBy,
  PluginSubmissionMetadataPreview,
  RawPlugin,
  SetupConfig,
  SetupStatus,
  SiteConfig,
  SortDirection,
  Submission,
  ThemeMode,
  User,
} from "@/types";

const normalizeBaseUrl = (value: string | undefined): string =>
  String(value ?? "")
    .trim()
    .replace(/\/$/, "");

const isLoopbackBaseUrl = (value: string): boolean => {
  try {
    const hostname = new URL(value).hostname.toLowerCase();
    return (
      hostname === "localhost" ||
      hostname === "0.0.0.0" ||
      hostname === "::1" ||
      hostname.startsWith("127.")
    );
  } catch {
    return false;
  }
};

const WINDOW_ORIGIN = window.location.origin;
const CONFIGURED_BASE_URL = normalizeBaseUrl(import.meta.env.VITE_BASE_URL);
const CONFIGURED_API_BASE_URL = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL);
const canUseConfiguredBaseUrl =
  !CONFIGURED_BASE_URL ||
  !isLoopbackBaseUrl(CONFIGURED_BASE_URL) ||
  isLoopbackBaseUrl(WINDOW_ORIGIN);
const BASE_URL =
  canUseConfiguredBaseUrl && CONFIGURED_BASE_URL ? CONFIGURED_BASE_URL : WINDOW_ORIGIN;
const API_BASE_URL =
  CONFIGURED_API_BASE_URL || (canUseConfiguredBaseUrl ? CONFIGURED_BASE_URL : "");
const COMMUNITY_REPO_URL = String(import.meta.env.VITE_COMMUNITY_REPO_URL || "");

export const PLUGIN_CATEGORY_LABELS = Object.freeze({
  ai_tools: "AI 增强",
  entertainment: "娱乐",
  integrations: "外部集成",
  productivity: "效率",
  utilities: "生活实用",
  other: "其他",
}) as Record<PluginCategory, string>;

export const PLUGIN_CATEGORY_VALUES = Object.freeze([
  "ai_tools",
  "entertainment",
  "integrations",
  "productivity",
  "utilities",
]) as PluginCategory[];

export const PLUGIN_CATEGORY_OPTIONS: { label: string; value: PluginCategory }[] =
  PLUGIN_CATEGORY_VALUES.map((value) => ({
    label: PLUGIN_CATEGORY_LABELS[value],
    value,
  }));

export const normalizePluginCategory = (value: unknown): PluginCategory => {
  const category =
    typeof value === "string" || typeof value === "number" || typeof value === "boolean"
      ? String(value)
          .trim()
          .toLowerCase()
          .replace(/[\s-]+/g, "_")
      : "";
  if (!category) return "other";
  return (PLUGIN_CATEGORY_VALUES as readonly string[]).includes(category)
    ? (category as PluginCategory)
    : "other";
};

export const normalizePluginTags = (value: unknown): string[] => {
  const rawTags: unknown[] = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(/[,，、;\n]+/)
      : [];
  const tags = rawTags
    .map((tag) =>
      typeof tag === "string" || typeof tag === "number" || typeof tag === "boolean"
        ? String(tag).trim()
        : "",
    )
    .filter(Boolean);
  return Array.from(new Set(tags));
};

const normalizePluginText = (value: unknown): string => {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value).trim();
  }
  return "";
};

export const getPluginCategoryLabel = (value: unknown): string => {
  const category = normalizePluginCategory(value);
  return PLUGIN_CATEGORY_LABELS[category] || category;
};

const DEFAULT_SITE_CONFIG: SiteConfig = Object.freeze({
  name: "AstrBot Community Plugins",
  icon_url: "/logo.webp",
  web_url: BASE_URL,
  subtitle: "全新社区插件市场",
  description: "发现、评价和提交 AstrBot 插件。",
  contact_email: "",
  docs_url: "https://docs.astrbot.app/dev/star/plugin-new.html",
  auth: {
    github_login_enabled: false,
    public_login_enabled: true,
    login_agreement_enabled: false,
    login_agreement_text: "",
    service_terms_enabled: false,
    service_terms_text: "",
    terms_revision: "",
  },
  market: {
    submissions_enabled: true,
    comments_enabled: true,
    likes_enabled: true,
    max_plugin_tags: 8,
  },
});

const createDefaultSetupConfig = (): SetupConfig => ({
  site: {
    name: DEFAULT_SITE_CONFIG.name,
    icon_url: DEFAULT_SITE_CONFIG.icon_url,
    web_url: DEFAULT_SITE_CONFIG.web_url,
    subtitle: DEFAULT_SITE_CONFIG.subtitle,
    description: DEFAULT_SITE_CONFIG.description,
    contact_email: DEFAULT_SITE_CONFIG.contact_email,
    docs_url: DEFAULT_SITE_CONFIG.docs_url,
  },
  admin: {
    username: "admin",
    password: "",
  },
  auth: { ...DEFAULT_SITE_CONFIG.auth },
  github: {
    client_id: "",
    client_secret: "",
    callback_url: `${BASE_URL}/v1/auth/github/callback`,
    scope: "read:user user:email read:org",
    admin_org: "",
  },
  market: {
    submissions_enabled: true,
    comments_enabled: true,
    likes_enabled: true,
    plugin_auto_approve_enabled: false,
    max_plugin_tags: 8,
    api_token: "",
    api_token_configured: false,
    api_token_previews: [],
    api_token_remove_indexes: [],
    metadata_sync_enabled: true,
    metadata_sync_interval_seconds: 3600,
  },
  email: {
    provider: "disabled",
    smtp: {
      host: "",
      port: 587,
      username: "",
      password: "",
      from_address: "",
      from_name: "Astrhub Plugins Market",
      ssl: false,
      encryption: "auto",
      auth_method: "auto",
      validate_certs: true,
    },
    cloudflare: {
      account_id: "",
      api_token: "",
      from_address: "",
      from_name: "Astrhub Plugins Market",
    },
    daily_limit: 0,
    verification_daily_limit_per_user: 5,
  },
  postgres: {
    host: "127.0.0.1",
    port: 5432,
    database: "",
    username: "",
    password: "",
    ssl: false,
  },
  redis: {
    host: "127.0.0.1",
    port: 6379,
    database: 0,
    username: "",
    password: "",
    ssl: false,
  },
});

export const usePluginStore = defineStore("plugins", () => {
  const plugins = ref<Plugin[]>([]);
  const announcements = ref<Announcement[]>([]);
  const currentUser = ref<User | null>(null);
  const unreadNotificationCount = shallowRef(0);
  const setupStatus = ref<SetupStatus>({
    required: false,
    missing: [],
    database_configured: true,
    redis_configured: true,
    saved_setup: createDefaultSetupConfig(),
    restart_required: false,
  });
  const siteConfig = ref<SiteConfig>({ ...DEFAULT_SITE_CONFIG });
  const searchQuery = shallowRef("");
  const selectedTag = shallowRef<string | null>(null);
  const selectedCategory = shallowRef("all");
  const currentPage = shallowRef(1);
  const pageSize = shallowRef(12);
  const isDarkMode = shallowRef(false);
  const themeMode = shallowRef<ThemeMode>("system");
  const isLoading = shallowRef(true);
  const sortBy = shallowRef<PluginSortBy>("default");
  const sortDirection = shallowRef<SortDirection>("asc");
  const fuzzySearchEnabled = shallowRef(false);
  const randomSeed = shallowRef(0);
  const irisMaskActive = shallowRef(false);
  const irisMaskPosition = ref({ x: window.innerWidth / 2, y: window.innerHeight / 2 });

  const apiBaseUrl = API_BASE_URL;
  const pluginSourceUrl = `${BASE_URL}/plugins.json`;
  const communityRepoUrl = COMMUNITY_REPO_URL;
  let mediaQuery: MediaQueryList | null = null;
  let pluginsLoaded = false;
  let announcementsLoaded = false;
  let currentUserLoaded = false;
  let loadPluginsPromise: Promise<Plugin[]> | null = null;
  let loadAnnouncementsPromise: Promise<Announcement[]> | null = null;
  let loadCurrentUserPromise: Promise<void> | null = null;

  function prefersDark(): boolean {
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches || false;
  }

  function applyThemeFromSystem(): void {
    if (themeMode.value === "system") {
      isDarkMode.value = prefersDark();
    }
  }

  function initTheme(): void {
    themeMode.value = (localStorage.getItem("theme-mode") as ThemeMode) || "system";
    if (themeMode.value === "dark") {
      isDarkMode.value = true;
    } else if (themeMode.value === "light") {
      isDarkMode.value = false;
    } else {
      applyThemeFromSystem();
    }

    if (!mediaQuery && window.matchMedia) {
      mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
      mediaQuery.addEventListener?.("change", applyThemeFromSystem);
    }
  }

  function setThemeMode(value: string): void {
    themeMode.value = (["system", "light", "dark"] as ThemeMode[]).includes(value as ThemeMode)
      ? (value as ThemeMode)
      : "system";
    localStorage.setItem("theme-mode", themeMode.value);
    if (themeMode.value === "system") {
      applyThemeFromSystem();
      return;
    }
    isDarkMode.value = themeMode.value === "dark";
  }

  function normalizeSiteConfig(value: Partial<SiteConfig> = {}): SiteConfig {
    return {
      name: String(value.name || DEFAULT_SITE_CONFIG.name).trim() || DEFAULT_SITE_CONFIG.name,
      icon_url:
        String(value.icon_url || DEFAULT_SITE_CONFIG.icon_url).trim() ||
        DEFAULT_SITE_CONFIG.icon_url,
      web_url:
        String(value.web_url || DEFAULT_SITE_CONFIG.web_url).trim() || DEFAULT_SITE_CONFIG.web_url,
      subtitle: String(value.subtitle ?? DEFAULT_SITE_CONFIG.subtitle).trim(),
      description: String(value.description ?? DEFAULT_SITE_CONFIG.description).trim(),
      contact_email: String(value.contact_email || "").trim(),
      docs_url: String(value.docs_url ?? DEFAULT_SITE_CONFIG.docs_url).trim(),
      auth: { ...DEFAULT_SITE_CONFIG.auth, ...value.auth },
      market: { ...DEFAULT_SITE_CONFIG.market, ...value.market } as MarketConfig,
    };
  }

  function setMetaContent(
    name: string,
    content: string,
    attribute: "name" | "property" = "name",
  ): void {
    if (typeof document === "undefined") return;
    const element = document.querySelector(`meta[${attribute}="${name}"]`);
    if (element) element.setAttribute("content", content);
  }

  function updateLink(rel: string, href: string): void {
    if (typeof document === "undefined") return;
    const element = document.querySelector(`link[rel="${rel}"]`);
    if (element) element.setAttribute("href", href);
  }

  function applySiteMetadata(config: SiteConfig): void {
    if (typeof document === "undefined") return;
    document.title = config.name;
    setMetaContent("application-name", config.name);
    setMetaContent("og:title", config.name, "property");
    setMetaContent("og:image", config.icon_url, "property");
    updateLink("icon", config.icon_url);
    updateLink("shortcut icon", config.icon_url);
    updateLink("preload", config.icon_url);
  }

  function applySiteConfig(value: Partial<SiteConfig>): SiteConfig {
    const config = normalizeSiteConfig(value);
    siteConfig.value = config;
    applySiteMetadata(config);
    return config;
  }

  watch(sortBy, (value) => {
    if (value === "random") randomSeed.value = Math.random();
  });

  function stableHash(input: string, seedNumber: number): number {
    let h = (Math.floor(seedNumber * 1e9) ^ 5381) >>> 0;
    for (let i = 0; i < input.length; i += 1) {
      h = ((h << 5) + h + input.charCodeAt(i)) >>> 0;
    }
    return h >>> 0;
  }

  function normalizeSearchValue(value: string): string {
    return String(value || "")
      .trim()
      .toLowerCase();
  }

  function pluginSearchText(plugin: Plugin): string {
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
      plugin.category,
      getPluginCategoryLabel(plugin.category),
      ...normalizePluginTags(plugin.tags),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function fuzzyIncludes(text: string, query: string): boolean {
    let index = 0;
    for (const char of query) {
      index = text.indexOf(char, index);
      if (index === -1) return false;
      index += 1;
    }
    return true;
  }

  function pluginMatchesSearch(plugin: Plugin, searchValue: string): boolean {
    const text = pluginSearchText(plugin);
    if (!fuzzySearchEnabled.value) return text.includes(searchValue);
    return fuzzyIncludes(text, searchValue);
  }

  function compareValues(a: number, b: number): number {
    if (a > b) return 1;
    if (a < b) return -1;
    return 0;
  }

  function getPluginSortValue(plugin: Plugin): number {
    if (sortBy.value === "stars") return Number(plugin.stars || 0);
    if (sortBy.value === "likes") return Number(plugin.likes || 0);
    if (sortBy.value === "comments") return Number(plugin.comments_count || 0);
    return Number(plugin.list_index || 0);
  }

  function getPluginTime(plugin: Plugin): number {
    return new Date(plugin.updated_at || plugin.created_at || 0).getTime() || 0;
  }

  function compareRandomPlugins(a: Plugin, b: Plugin): number {
    const ha = stableHash(String(a.id || a.name || ""), randomSeed.value);
    const hb = stableHash(String(b.id || b.name || ""), randomSeed.value);
    return ha - hb;
  }

  function comparePlugins(a: Plugin, b: Plugin): number {
    const direction = sortDirection.value === "asc" ? 1 : -1;
    if (sortBy.value === "random") {
      return direction * compareRandomPlugins(a, b);
    }
    if (sortBy.value === "updated") {
      return direction * compareValues(getPluginTime(a), getPluginTime(b));
    }
    return direction * compareValues(getPluginSortValue(a), getPluginSortValue(b));
  }

  const allTags = computed(() => {
    const tags = new Set<string>();
    plugins.value.forEach((plugin) => {
      normalizePluginTags(plugin.tags).forEach((tag) => tags.add(tag));
    });
    return Array.from(tags).sort();
  });

  const tagOptions = computed(() => allTags.value.map((tag) => ({ label: tag, value: tag })));

  const categoryOptions = computed(() => {
    const counts = plugins.value.reduce<Record<string, number>>(
      (acc, plugin) => {
        const category = normalizePluginCategory(plugin.category);
        acc[category] = (acc[category] || 0) + 1;
        return acc;
      },
      { all: plugins.value.length },
    );
    const options: { label: string; value: string }[] = [
      { label: `全部 (${counts.all || 0})`, value: "all" },
      ...PLUGIN_CATEGORY_OPTIONS.filter((option) => counts[option.value]).map((option) => ({
        label: `${option.label} (${counts[option.value] || 0})`,
        value: option.value,
      })),
    ];
    if (counts.other) {
      options.push({ label: `${PLUGIN_CATEGORY_LABELS.other} (${counts.other})`, value: "other" });
    }
    return options;
  });

  const filteredPlugins = computed(() => {
    const searchValue = normalizeSearchValue(searchQuery.value);
    const filtered = plugins.value.filter((plugin) => {
      const category = selectedCategory.value || "all";
      const matchesCategory =
        category === "all" || normalizePluginCategory(plugin.category) === category;
      if (!matchesCategory) return false;
      if (!searchValue && !selectedTag.value) return true;
      const matchesSearch = !searchValue || pluginMatchesSearch(plugin, searchValue);
      const matchesTag =
        !selectedTag.value || normalizePluginTags(plugin.tags).includes(selectedTag.value);
      return matchesSearch && matchesTag;
    });

    filtered.sort(comparePlugins);

    return filtered;
  });

  const totalPages = computed(() => {
    return Math.ceil(filteredPlugins.value.length / pageSize.value);
  });

  const paginatedPlugins = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value;
    return filteredPlugins.value.slice(start, start + pageSize.value);
  });

  function normalizePluginItem(plugin: RawPlugin, index: number): Plugin {
    const id = plugin.id || plugin.name || `plugin-${index}`;
    const name = normalizePluginText(plugin.name) || String(id);
    const displayName = normalizePluginText(plugin.display_name);
    const desc = normalizePluginText(plugin.desc);
    return {
      ...plugin,
      id,
      name,
      display_name: displayName && displayName !== desc ? displayName : name,
      version: plugin.version || "1.0.0",
      logo: plugin.logo || "",
      tags: normalizePluginTags(plugin.tags),
      category: normalizePluginCategory(plugin.category),
      stars: Number(plugin.stars || 0),
      likes: Number(plugin.likes || 0),
      comments_count: Number(plugin.comments_count || 0),
      list_index: index,
    };
  }

  async function loadPlugins(options: { force?: boolean } = {}): Promise<Plugin[]> {
    const { force = false } = options;
    if (loadPluginsPromise) return loadPluginsPromise;
    if (pluginsLoaded && !force) return plugins.value;
    isLoading.value = true;
    loadPluginsPromise = (async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/v1/plugins`, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const items: unknown[] = Array.isArray(data) ? data : data.items || [];
        plugins.value = items.map((plugin, index) =>
          normalizePluginItem(plugin as RawPlugin, index),
        );
        pluginsLoaded = true;
        return plugins.value;
      } catch (error) {
        console.error("Error loading plugins:", error);
        plugins.value = [];
        pluginsLoaded = false;
        return plugins.value;
      } finally {
        isLoading.value = false;
        loadPluginsPromise = null;
      }
    })();
    return loadPluginsPromise;
  }

  async function loadAnnouncements(options: { force?: boolean } = {}): Promise<Announcement[]> {
    const { force = false } = options;
    if (loadAnnouncementsPromise) return loadAnnouncementsPromise;
    if (announcementsLoaded && !force) return announcements.value;
    loadAnnouncementsPromise = (async () => {
      const response = await fetch(`${apiBaseUrl}/v1/announcements`, { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "加载公告失败");
      announcements.value = Array.isArray(data.items) ? data.items : [];
      announcementsLoaded = true;
      return announcements.value;
    })();
    try {
      return await loadAnnouncementsPromise;
    } finally {
      loadAnnouncementsPromise = null;
    }
  }

  async function loadSetupStatus(path = "/v1/setup/status"): Promise<SetupStatus> {
    const response = await fetch(`${apiBaseUrl}${path}`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 404) {
        setupStatus.value = {
          ...setupStatus.value,
          required: false,
          missing: [],
          saved_setup: createDefaultSetupConfig(),
        };
        return setupStatus.value;
      }
      throw new Error(data.error || "加载安装状态失败");
    }
    const setupConfig = mergeSetupConfig(
      data.saved_setup,
      data.saved_setup?.site || data.site || {},
    );
    setupStatus.value = {
      required: Boolean(data.required),
      missing: Array.isArray(data.missing) ? data.missing : [],
      database_configured: Boolean(data.database_configured),
      redis_configured: Boolean(data.redis_configured),
      saved_setup: setupConfig,
      restart_required: Boolean(data.restart_required),
    };
    return setupStatus.value;
  }

  function loadAdminSetupStatus(): Promise<SetupStatus> {
    return loadSetupStatus("/v1/admin/setup/status");
  }

  async function loadSiteConfig(): Promise<SiteConfig> {
    try {
      const response = await fetch(`${apiBaseUrl}/v1/site`, { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      return applySiteConfig(response.ok ? data : siteConfig.value);
    } catch {
      return applySiteConfig(siteConfig.value);
    }
  }

  function mergeSetupConfig(
    value: Partial<SetupConfig> = {},
    site: Partial<SiteConfig> = siteConfig.value,
  ): SetupConfig {
    const defaults = createDefaultSetupConfig();
    return {
      site: normalizeSiteConfig({ ...value.site, ...(site as object) }),
      admin: { ...defaults.admin, ...value.admin },
      auth: { ...defaults.auth, ...value.auth },
      github: { ...defaults.github, ...value.github },
      market: { ...defaults.market, ...value.market } as SetupConfig["market"],
      email: {
        ...defaults.email,
        ...value.email,
        smtp: { ...defaults.email.smtp, ...value.email?.smtp },
        cloudflare: { ...defaults.email.cloudflare, ...value.email?.cloudflare },
      },
      postgres: { ...defaults.postgres, ...value.postgres },
      redis: { ...defaults.redis, ...value.redis },
    };
  }

  async function saveSetupConfig(payload: SetupConfig): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/setup`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "保存失败");
    await loadSetupStatus();
    return data;
  }

  async function loadSystemSettings(): Promise<SetupConfig> {
    const response = await fetch(`${apiBaseUrl}/v1/admin/settings`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载设置失败");
    return mergeSetupConfig(data, data.site);
  }

  async function loadAdminPlugins(): Promise<Plugin[]> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/submissions`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载插件审核列表失败");
    const items: Submission[] = data.items || [];
    return items.map((submission) => {
      const payload = submission.payload || {};
      return normalizePluginItem(
        {
          ...payload,
          id: submission.plugin_id,
          submission_id: submission.id,
          status: submission.status || "pending",
        },
        0,
      );
    });
  }

  async function loadAdminUsers(): Promise<AdminUser[]> {
    const response = await fetch(`${apiBaseUrl}/v1/admin/users`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载用户列表失败");
    return data.items || [];
  }

  async function createInternalUser(
    payload: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/core/users`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "创建用户失败");
    return data;
  }

  async function updateAdminUserRole(
    userId: number | string,
    role: string,
  ): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/core/admins/${userId}`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ role }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "更新用户角色失败");
    return data;
  }

  async function muteAdminUser(
    userId: number | string,
    payload: string | Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const body = typeof payload === "string" ? { muted_until: payload } : payload;
    const response = await fetch(`${apiBaseUrl}/v1/admin/users/${userId}/mute`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "封禁用户失败");
    return data;
  }

  async function unmuteAdminUser(userId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/admin/users/${userId}/unmute`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "解除封禁失败");
    return data;
  }

  async function deleteAdminUser(userId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/core/users/${userId}`, {
      method: "DELETE",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "删除用户失败");
    return data;
  }

  async function loadMyPlugins(): Promise<Plugin[]> {
    const response = await fetch(`${apiBaseUrl}/v1/me/plugins`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载我的插件失败");
    const items: unknown[] = data.items || [];
    return items.map((plugin, index) => normalizePluginItem(plugin as RawPlugin, index));
  }

  async function loadMyApiKeys(): Promise<ApiKey[]> {
    const response = await fetch(`${apiBaseUrl}/v1/me/api-keys`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载访问密钥失败");
    return data.items || [];
  }

  async function createMyApiKey(
    payload: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/me/api-keys`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "生成访问密钥失败");
    return data;
  }

  async function deleteMyApiKey(keyId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/me/api-keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "删除访问密钥失败");
    return data;
  }

  function updatePluginInList(plugin: Plugin): void {
    const pluginId = plugin?.id;
    if (!pluginId) return;
    plugins.value = plugins.value.map((item) =>
      item.id === pluginId ? { ...item, ...normalizePluginItem(plugin, item.list_index) } : item,
    );
  }

  async function updatePluginMetadata(
    pluginId: number | string,
    payload: Partial<Plugin> & Record<string, unknown>,
  ): Promise<Plugin> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "保存插件信息失败");
    updatePluginInList(data);
    return normalizePluginItem(data, 0);
  }

  async function requestPluginListing(pluginId: number | string): Promise<Plugin> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/request-list`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "申请上架失败");
    updatePluginInList(data);
    return normalizePluginItem(data, 0);
  }

  async function unlistOwnPlugin(
    pluginId: number | string,
    payload: Record<string, unknown> | null = null,
  ): Promise<Plugin> {
    const options: RequestInit = {
      method: "POST",
      credentials: "include",
    };
    if (payload && Object.keys(payload).length > 0) {
      options.headers = { "content-type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/unlist`, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "下架插件失败");
    updatePluginInList(data);
    return normalizePluginItem(data, 0);
  }

  async function loadPluginDetail(pluginId: number | string): Promise<PluginDetail> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载插件详情失败");
    return data;
  }

  async function likePlugin(pluginId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/like`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "点赞失败");
    return data;
  }

  async function unlikePlugin(pluginId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/unlike`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "取消点赞失败");
    return data;
  }

  async function addPluginComment(
    pluginId: number | string,
    payload: Record<string, unknown>,
  ): Promise<Comment> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/comments`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "评论失败");
    return data;
  }

  async function deletePluginComment(commentId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/comments/${commentId}`, {
      method: "DELETE",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "删除评论失败");
    return data;
  }

  async function likePluginComment(commentId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/comments/${commentId}/like`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "点赞评论失败");
    return data;
  }

  async function unlikePluginComment(commentId: number | string): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/comments/${commentId}/unlike`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "取消评论点赞失败");
    return data;
  }

  async function updatePluginListing(
    pluginId: number | string,
    action: string,
    payload: Record<string, unknown> | null = null,
  ): Promise<Record<string, unknown>> {
    const options: RequestInit = {
      method: "POST",
      credentials: "include",
    };
    if (payload && Object.keys(payload).length > 0) {
      options.headers = { "content-type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(`${apiBaseUrl}/v1/admin/plugins/${pluginId}/${action}`, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "更新插件状态失败");
    return data;
  }

  async function refreshPluginGithubMetadata(
    pluginId: number | string,
    payload: Record<string, unknown> | null = null,
  ): Promise<Record<string, unknown>> {
    const options: RequestInit = {
      method: "POST",
      credentials: "include",
    };
    if (payload && Object.keys(payload).length > 0) {
      options.headers = { "content-type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(`${apiBaseUrl}/v1/plugins/${pluginId}/refresh-github`, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "刷新 GitHub 数据失败");
    return data;
  }

  async function refreshAdminPluginGithubMetadata(
    pluginId: number | string,
    payload: Record<string, unknown> | null = null,
  ): Promise<Record<string, unknown>> {
    const options: RequestInit = {
      method: "POST",
      credentials: "include",
    };
    if (payload && Object.keys(payload).length > 0) {
      options.headers = { "content-type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(
      `${apiBaseUrl}/v1/admin/plugins/${pluginId}/refresh-github`,
      options,
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "刷新 GitHub 数据失败");
    return data;
  }

  async function loadNotifications(
    options: PageOptions = {},
  ): Promise<PaginatedResult<AppNotification>> {
    const page = Number(options.page || 1);
    const requestedPageSize = Number(options.pageSize || 20);
    const params = new URLSearchParams({
      limit: String(requestedPageSize),
      offset: String(Math.max(0, page - 1) * requestedPageSize),
    });
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications?${params}`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载消息失败");
    const items: AppNotification[] = data.items || [];
    unreadNotificationCount.value = Number(
      data.unread_count ?? items.filter((item) => !item.read).length,
    );
    return {
      items,
      total: Number(data.total || 0),
      page,
      page_size: Number(data.limit || requestedPageSize),
      limit: Number(data.limit || requestedPageSize),
      offset: Number(data.offset || 0),
      unread_count: unreadNotificationCount.value,
    };
  }

  async function loadUnreadNotificationCount(): Promise<number> {
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications/unread-count`, {
      credentials: "include",
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "加载未读消息失败");
    unreadNotificationCount.value = Number(data.count || 0);
    return unreadNotificationCount.value;
  }

  async function markNotificationsRead(): Promise<number> {
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications/read`, {
      method: "POST",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "标记消息已读失败");
    unreadNotificationCount.value = 0;
    return data.updated || 0;
  }

  async function deleteNotification(notificationId: number | string): Promise<number> {
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications/${notificationId}`, {
      method: "DELETE",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "删除消息失败");
    await loadUnreadNotificationCount().catch(() => {
      unreadNotificationCount.value = 0;
    });
    return Number(data.deleted || 0);
  }

  async function deleteNotifications(notificationIds: Array<number | string>): Promise<number> {
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications/delete`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ids: notificationIds }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "删除消息失败");
    await loadUnreadNotificationCount().catch(() => {
      unreadNotificationCount.value = 0;
    });
    return Number(data.deleted || 0);
  }

  async function clearNotifications(): Promise<number> {
    const response = await fetch(`${apiBaseUrl}/v1/me/notifications`, {
      method: "DELETE",
      credentials: "include",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "清空消息失败");
    unreadNotificationCount.value = 0;
    return Number(data.deleted || 0);
  }

  function apiErrorMessage(data: unknown, fallback: string): string {
    const payload = (data || {}) as Record<string, unknown>;
    for (const key of ["error", "detail", "message"]) {
      const value = payload[key];
      if (typeof value === "string" && value.trim()) return value;
      if (Array.isArray(value) && value.length) return JSON.stringify(value);
      if (value && typeof value === "object") return JSON.stringify(value);
    }
    return fallback;
  }

  async function saveSystemSettings(
    payload: Partial<SetupConfig>,
  ): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/admin/settings`, {
      method: "PUT",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(apiErrorMessage(data, "保存设置失败"));
    if (data.settings?.site) {
      applySiteConfig({
        ...data.settings.site,
        auth: data.settings.auth,
        market: data.settings.market,
      });
    }
    return data;
  }

  async function sendTestEmail(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/admin/settings/email/test`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(apiErrorMessage(data, "测试邮件发送失败"));
    return data;
  }

  async function publishAnnouncement(payload: Record<string, unknown>): Promise<Announcement> {
    const response = await fetch(`${apiBaseUrl}/v1/core/announcements`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "发布公告失败");
    announcements.value = [data, ...announcements.value];
    announcementsLoaded = true;
    return data;
  }

  async function loadCurrentUser(options: { force?: boolean } = {}): Promise<void> {
    const { force = false } = options;
    if (loadCurrentUserPromise) return loadCurrentUserPromise;
    if (currentUserLoaded && !force) return;
    loadCurrentUserPromise = (async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/v1/me`, { credentials: "include" });
        if (!response.ok) {
          currentUser.value = null;
          unreadNotificationCount.value = 0;
          currentUserLoaded = true;
          return;
        }
        currentUser.value = await response.json();
        currentUserLoaded = true;
        await loadUnreadNotificationCount().catch(() => {
          unreadNotificationCount.value = 0;
        });
      } catch {
        currentUser.value = null;
        unreadNotificationCount.value = 0;
        currentUserLoaded = true;
      }
    })();
    try {
      await loadCurrentUserPromise;
    } finally {
      loadCurrentUserPromise = null;
    }
  }

  function loginWithGithub(): void {
    window.location.href = `${apiBaseUrl}/v1/auth/github/login`;
  }

  async function loginWithPassword(payload: { username: string; password: string }): Promise<User> {
    const response = await fetch(`${apiBaseUrl}/v1/auth/internal/login`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "登录失败");
    currentUser.value = data.user;
    currentUserLoaded = true;
    await loadUnreadNotificationCount().catch(() => {
      unreadNotificationCount.value = 0;
    });
    return data.user;
  }

  async function logout(): Promise<void> {
    await fetch(`${apiBaseUrl}/v1/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
    currentUser.value = null;
    currentUserLoaded = true;
    unreadNotificationCount.value = 0;
  }

  async function updateProfile(payload: Partial<User> & Record<string, unknown>): Promise<User> {
    const response = await fetch(`${apiBaseUrl}/v1/me/profile`, {
      method: "PATCH",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || "更新失败");
    currentUser.value = data;
    currentUserLoaded = true;
    return data;
  }

  async function submitPlugin(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/submissions`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "提交失败");
    return data;
  }

  async function fetchPluginSubmissionMetadata(
    repo: string,
  ): Promise<PluginSubmissionMetadataPreview> {
    const response = await fetch(`${apiBaseUrl}/v1/plugins/submissions/metadata-preview`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ repo }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(apiErrorMessage(data, "仓库信息拉取失败"));
    return data as PluginSubmissionMetadataPreview;
  }

  function setSearchQuery(query: string): void {
    searchQuery.value = query;
  }

  function setSelectedTag(tag: string | null): void {
    selectedTag.value = tag;
  }

  function setSelectedCategory(category: string): void {
    selectedCategory.value = category || "all";
  }

  function setCurrentPage(page: number): void {
    currentPage.value = page;
  }

  function setSortBy(value: PluginSortBy): void {
    sortBy.value = value;
    if (value === "random") randomSeed.value = Math.random();
    currentPage.value = 1;
  }

  function setSortDirection(value: SortDirection): void {
    sortDirection.value = value === "asc" ? "asc" : "desc";
    currentPage.value = 1;
  }

  function setFuzzySearchEnabled(value: boolean): void {
    fuzzySearchEnabled.value = Boolean(value);
    currentPage.value = 1;
  }

  function resetPluginFilters(): void {
    searchQuery.value = "";
    selectedTag.value = null;
    selectedCategory.value = "all";
    currentPage.value = 1;
    sortBy.value = "updated";
    sortDirection.value = "desc";
    fuzzySearchEnabled.value = false;
  }

  function refreshRandomOrder(): void {
    if (sortBy.value === "random") randomSeed.value = Math.random();
  }

  function triggerIrisAnimation(
    position: { x: number; y: number } | null = null,
    callback: (() => void) | null = null,
  ): void {
    irisMaskPosition.value = position || { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    irisMaskActive.value = true;
    setTimeout(() => {
      if (callback) callback();
      setTimeout(() => {
        irisMaskActive.value = false;
      }, 400);
    }, 800);
  }

  return {
    plugins,
    announcements,
    currentUser,
    unreadNotificationCount,
    setupStatus,
    siteConfig,
    searchQuery,
    selectedTag,
    selectedCategory,
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
    categoryOptions,
    filteredPlugins,
    totalPages,
    paginatedPlugins,
    initTheme,
    loadAnnouncements,
    loadSiteConfig,
    loadSetupStatus,
    loadAdminSetupStatus,
    loadPlugins,
    loadCurrentUser,
    loginWithGithub,
    loginWithPassword,
    logout,
    updateProfile,
    saveSetupConfig,
    loadSystemSettings,
    loadAdminPlugins,
    loadAdminUsers,
    createInternalUser,
    updateAdminUserRole,
    muteAdminUser,
    unmuteAdminUser,
    deleteAdminUser,
    loadMyPlugins,
    loadMyApiKeys,
    createMyApiKey,
    deleteMyApiKey,
    updatePluginMetadata,
    requestPluginListing,
    unlistOwnPlugin,
    loadPluginDetail,
    likePlugin,
    unlikePlugin,
    addPluginComment,
    deletePluginComment,
    likePluginComment,
    unlikePluginComment,
    updatePluginListing,
    refreshPluginGithubMetadata,
    refreshAdminPluginGithubMetadata,
    loadNotifications,
    loadUnreadNotificationCount,
    markNotificationsRead,
    deleteNotification,
    deleteNotifications,
    clearNotifications,
    saveSystemSettings,
    sendTestEmail,
    publishAnnouncement,
    submitPlugin,
    fetchPluginSubmissionMetadata,
    setSearchQuery,
    setSelectedTag,
    setSelectedCategory,
    setCurrentPage,
    setSortBy,
    setSortDirection,
    setFuzzySearchEnabled,
    updatePluginInList,
    resetPluginFilters,
    setThemeMode,
    refreshRandomOrder,
    triggerIrisAnimation,
  };
});

export type { HelpSection };
