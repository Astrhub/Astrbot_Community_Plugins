<template>
  <header ref="fullHeader" class="app-header">
    <nav class="top-nav" aria-label="主导航">
      <div class="brand">
        <img :src="siteIconUrl" :alt="siteName" class="brand-logo" width="40" height="40" />
        <span class="brand-name">{{ siteName }}</span>
      </div>
      <div class="nav-actions">
        <theme-mode-button class="theme-button" />
        <n-button v-if="currentUser" secondary @click="goNotifications">
          <template #icon>
            <span class="notification-icon-wrapper">
              <n-icon><notifications-outline /></n-icon>
              <span
                v-if="hasUnreadNotifications"
                class="notification-dot"
                aria-hidden="true"
              ></span>
            </span>
          </template>
          消息
        </n-button>
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
          :selected-category="selectedCategory"
          :category-options="categoryOptions"
          :selected-tag="selectedTag"
          :tag-options="tagOptions"
          :on-header="true"
          @update:searchQuery="handleSearchQueryChange"
          @update:currentPage="handleCurrentPageChange"
          @update:sortBy="handleSortByChange"
          @update:sortDirection="handleSortDirectionChange"
          @update:fuzzySearchEnabled="handleFuzzySearchEnabledChange"
          @update:selectedCategory="handleSelectedCategoryChange"
          @update:selectedTag="handleSelectedTagChange"
        />
      </div>
    </section>
  </header>

  <header class="sticky-header" :class="{ 'sticky-header--visible': showStickyHeader }">
    <div class="sticky-header-content">
      <div class="sticky-header-left">
        <img :src="siteIconUrl" :alt="siteName" class="sticky-logo" width="32" height="32" />
        <h2 class="sticky-title">{{ siteName }}</h2>
      </div>
      <div class="sticky-header-center">
        <search-toolbar
          class="sticky-desktop-toolbar"
          :search-query="searchQuery"
          :current-page="currentPage"
          :sort-by="sortBy"
          :sort-direction="sortDirection"
          :fuzzy-search-enabled="fuzzySearchEnabled"
          :selected-category="selectedCategory"
          :category-options="categoryOptions"
          :selected-tag="selectedTag"
          :tag-options="tagOptions"
          :compact="true"
          @update:searchQuery="handleSearchQueryChange"
          @update:currentPage="handleCurrentPageChange"
          @update:sortBy="handleSortByChange"
          @update:sortDirection="handleSortDirectionChange"
          @update:fuzzySearchEnabled="handleFuzzySearchEnabledChange"
          @update:selectedCategory="handleSelectedCategoryChange"
          @update:selectedTag="handleSelectedTagChange"
        />
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
          v-if="currentUser"
          quaternary
          circle
          class="hide-on-mobile-search"
          @click="goNotifications"
          :aria-label="notificationButtonLabel"
        >
          <span class="notification-icon-wrapper">
            <n-icon><notifications-outline /></n-icon>
            <span v-if="hasUnreadNotifications" class="notification-dot" aria-hidden="true"></span>
          </span>
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
        <theme-mode-button circle class="hide-on-mobile-search" />
      </div>
    </div>
  </header>
  <div class="sticky-header-spacer" aria-hidden="true"></div>

  <n-modal v-model:show="isLoginModalOpen" preset="card" title="登录 / 注册" class="login-modal">
    <div class="login-methods">
      <n-button
        v-if="siteConfig.auth.github_login_enabled"
        type="primary"
        block
        :disabled="!canSubmitLogin"
        @click="loginWithGithub"
      >
        <template #icon>
          <n-icon><logo-github /></n-icon>
        </template>
        GitHub 登录 / 注册
      </n-button>
      <n-alert v-else type="warning" :bordered="false">
        GitHub OAuth 未开启，普通用户暂时无法登录或注册。
      </n-alert>
      <n-alert v-if="agreementText" type="info" :bordered="false" class="agreement-box">
        <div class="agreement-text">{{ agreementText }}</div>
        <n-checkbox v-model:checked="agreementAccepted">我已阅读并同意以上条款</n-checkbox>
      </n-alert>
    </div>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, h, onMounted, ref, onUnmounted } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import { NAlert, NCheckbox, NDropdown, NIcon, NButton, NModal, useMessage } from "naive-ui";
import {
  LinkOutline,
  LogInOutline,
  LogOutOutline,
  LogoGithub,
  NotificationsOutline,
  PersonOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
} from "@vicons/ionicons5";
import SearchToolbar from "./SearchToolbar.vue";
import ThemeModeButton from "./ThemeModeButton.vue";
import { usePluginStore } from "../stores/plugins";

defineProps({
  searchQuery: String,
  currentPage: Number,
  totalPages: Number,
  sortBy: String,
  sortDirection: String,
  fuzzySearchEnabled: Boolean,
  selectedCategory: String,
  categoryOptions: Array,
  selectedTag: String,
  tagOptions: Array,
});

const emit = defineEmits([
  "update:searchQuery",
  "update:currentPage",
  "update:sortBy",
  "update:sortDirection",
  "update:fuzzySearchEnabled",
  "update:selectedCategory",
  "update:selectedTag",
]);

const router = useRouter();
const message = useMessage();
const store = usePluginStore();
const { currentUser, siteConfig, unreadNotificationCount } = storeToRefs(store);
const { loginWithGithub, logout } = store;

const fullHeader = ref(null);
const showStickyHeader = ref(false);
const isLoginModalOpen = ref(false);
const agreementAccepted = ref(false);
const pluginSourceUrl = computed(() => store.pluginSourceUrl);
const siteName = computed(() => siteConfig.value.name);
const siteIconUrl = computed(() => siteConfig.value.icon_url);
const siteSubtitle = computed(() => siteConfig.value.subtitle);
const siteDescription = computed(() => siteConfig.value.description);
const isCoreAdmin = computed(() => currentUser.value?.role === "core_admin");
const isAdminUser = computed(() => ["core_admin", "admin"].includes(currentUser.value?.role));
const hasUnreadNotifications = computed(() => unreadNotificationCount.value > 0);
const notificationButtonLabel = computed(() =>
  hasUnreadNotifications.value ? `消息，${unreadNotificationCount.value} 条未读` : "消息",
);
const displayUserName = computed(
  () =>
    currentUser.value?.github_login ||
    currentUser.value?.internal_username ||
    currentUser.value?.login ||
    "已登录",
);
const userMenuOptions = computed(() => [
  {
    key: "profile",
    label: "个人设置",
    icon: renderIcon(PersonOutline),
  },
  {
    key: "settings",
    label: "系统设置",
    icon: renderIcon(SettingsOutline),
    disabled: !isCoreAdmin.value,
  },
  {
    key: "divider",
    type: "divider",
  },
  {
    key: "logout",
    label: "退出登录",
    icon: renderIcon(LogOutOutline),
  },
]);
const agreementText = computed(() => {
  const auth = siteConfig.value.auth || {};
  const parts = [];
  if (auth.login_agreement_enabled && auth.login_agreement_text) {
    parts.push(auth.login_agreement_text);
  }
  if (auth.service_terms_enabled && auth.service_terms_text) {
    parts.push(auth.service_terms_text);
  }
  return parts.join("\n\n");
});
const canSubmitLogin = computed(() => !agreementText.value || agreementAccepted.value);

const handleSearchQueryChange = (value) => {
  emit("update:searchQuery", value);
};

const handleCurrentPageChange = (value) => {
  emit("update:currentPage", value);
};

const handleSortByChange = (value) => {
  emit("update:sortBy", value);
};

const handleSortDirectionChange = (value) => {
  emit("update:sortDirection", value);
};

const handleFuzzySearchEnabledChange = (value) => {
  emit("update:fuzzySearchEnabled", value);
};

const handleSelectedCategoryChange = (value) => {
  emit("update:selectedCategory", value);
};

const handleSelectedTagChange = (value) => {
  emit("update:selectedTag", value);
};

const goSettings = () => {
  router.push("/admin/settings");
};

const goAdminPlugins = () => {
  router.push("/admin/plugins");
};

const goNotifications = () => {
  router.push("/notifications");
};

function renderIcon(icon) {
  return () => h(NIcon, null, { default: () => h(icon) });
}

async function handleUserMenuSelect(key) {
  if (key === "profile") {
    router.push("/settings/personal");
    return;
  }
  if (key === "settings") {
    goSettings();
    return;
  }
  if (key === "logout") {
    try {
      await logout();
      message.success("已退出登录");
      router.push("/");
    } catch (error) {
      message.error(error.message || "退出失败");
    }
  }
}

const openLoginModal = () => {
  isLoginModalOpen.value = true;
};

const copyPluginSource = async () => {
  try {
    await writeClipboard(pluginSourceUrl.value);
    message.success("插件源已复制");
  } catch {
    message.error(`复制失败，请手动复制：${pluginSourceUrl.value}`);
  }
};

const writeClipboard = async (value) => {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("copy failed");
};

const handleScroll = () => {
  if (window.matchMedia("(max-width: 768px)").matches) {
    showStickyHeader.value = true;
    return;
  }
  if (!fullHeader.value) return;
  showStickyHeader.value = fullHeader.value.getBoundingClientRect().bottom <= 0;
};

onMounted(() => {
  window.addEventListener("scroll", handleScroll, { passive: true });
  handleScroll();
});

onUnmounted(() => {
  window.removeEventListener("scroll", handleScroll);
});
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
  content: "";
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

.notification-icon-wrapper {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}

.notification-dot {
  position: absolute;
  top: -3px;
  right: -3px;
  width: 8px;
  height: 8px;
  border: 2px solid var(--bg-card);
  border-radius: 999px;
  background: #ef4444;
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
  transition:
    transform 0.22s ease,
    opacity 0.22s ease;
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
  grid-template-columns: auto minmax(240px, 640px) auto;
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

.login-methods {
  display: grid;
  gap: 12px;
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

  .nav-actions {
    display: none;
  }
}

@media (max-width: 480px) {
  .sticky-title {
    font-size: 14px;
  }
}
</style>
