<script setup lang="ts">
import { computed, defineAsyncComponent, h, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import { NDropdown, NIcon, useMessage } from "naive-ui";
import {
  ExtensionPuzzleOutline,
  LogOutOutline,
  NotificationsOutline,
  PersonOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
} from "@vicons/ionicons5";
import ThemeModeButton from "./ThemeModeButton.vue";
import AnnouncementButton from "./AnnouncementButton.vue";
import { usePluginStore } from "../stores/plugins";
import { avatarVariantUrl, resolveMyAvatarUrl } from "../utils/github";

const AuthModal = defineAsyncComponent(() => import("./AuthModal.vue"));

const HOVER_CLOSE_DELAY_MS = 140;

const router = useRouter();
const message = useMessage();
const store = usePluginStore();
const { currentUser, currentUserPreview, siteConfig, unreadNotificationCount } = storeToRefs(store);
const { logout } = store;

const isLoginModalOpen = shallowRef(false);
const authModalLoaded = shallowRef(false);
const avatarLoadFailed = shallowRef(false);
const userTriggerRef = ref<HTMLButtonElement | null>(null);
const userMenuHovered = shallowRef(false);
const userMenuPinned = shallowRef(false);
let hoverCloseTimer: ReturnType<typeof window.setTimeout> | null = null;

const headerUser = computed(() => currentUser.value || currentUserPreview.value);
const siteName = computed(() => siteConfig.value.name || "Astrhub 插件市场");
const siteIconUrl = computed(() => {
  const configuredUrl = String(siteConfig.value.icon_url || "").trim();
  return !configuredUrl || configuredUrl === "/logo.webp" ? "/logo.webp?v=20260725" : configuredUrl;
});
const isCoreAdmin = computed(() => currentUser.value?.role === "core_admin");
const isAdminUser = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);
const hasUnreadNotifications = computed(() => unreadNotificationCount.value > 0);
const myPublicUsername = computed(() =>
  String(
    currentUser.value?.public_username ||
      currentUser.value?.github_login ||
      currentUser.value?.internal_username ||
      "",
  ).trim(),
);
const displayUserName = computed(
  () =>
    headerUser.value?.public_username ||
    headerUser.value?.github_login ||
    headerUser.value?.internal_username ||
    headerUser.value?.username ||
    headerUser.value?.login ||
    "已登录",
);
const userAvatarUrl = computed(() =>
  avatarVariantUrl(
    resolveMyAvatarUrl(
      String(
        headerUser.value?.effective_avatar_url ||
          headerUser.value?.avatar_url ||
          headerUser.value?.avatar ||
          "",
      ).trim(),
      store.apiBaseUrl,
    ),
    64,
  ),
);
const showUserAvatar = computed(() => Boolean(userAvatarUrl.value) && !avatarLoadFailed.value);
const userMenuOpen = computed(
  () => Boolean(currentUser.value) && (userMenuPinned.value || userMenuHovered.value),
);

watch(userAvatarUrl, () => {
  avatarLoadFailed.value = false;
});

watch(currentUser, (user) => {
  if (!user) closeUserMenu();
});

const userMenuOptions = computed(() => [
  ...(myPublicUsername.value
    ? [
        {
          key: "my-plugins",
          label: "我的插件",
          icon: renderIcon(ExtensionPuzzleOutline),
        },
      ]
    : []),
  {
    key: "profile",
    label: "个人设置",
    icon: renderIcon(PersonOutline),
  },
  ...(isAdminUser.value
    ? [
        {
          key: "workbench",
          label: "审查工作台",
          icon: renderIcon(ShieldCheckmarkOutline),
        },
      ]
    : []),
  ...(isCoreAdmin.value
    ? [
        {
          key: "settings",
          label: "系统设置",
          icon: renderIcon(SettingsOutline),
        },
      ]
    : []),
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

function renderIcon(icon: unknown) {
  return () => h(NIcon, null, { default: () => h(icon as never) });
}

function clearHoverCloseTimer(): void {
  if (hoverCloseTimer !== null) {
    window.clearTimeout(hoverCloseTimer);
    hoverCloseTimer = null;
  }
}

function closeUserMenu(): void {
  clearHoverCloseTimer();
  userMenuHovered.value = false;
  userMenuPinned.value = false;
}

function handleTriggerMouseEnter(): void {
  if (!currentUser.value) return;
  clearHoverCloseTimer();
  userMenuHovered.value = true;
}

function scheduleHoverClose(): void {
  clearHoverCloseTimer();
  hoverCloseTimer = window.setTimeout(() => {
    hoverCloseTimer = null;
    userMenuHovered.value = false;
  }, HOVER_CLOSE_DELAY_MS);
}

function handleTriggerMouseLeave(): void {
  if (!currentUser.value) return;
  scheduleHoverClose();
}

function handleMenuMouseEnter(): void {
  if (!currentUser.value) return;
  clearHoverCloseTimer();
  userMenuHovered.value = true;
}

function handleMenuMouseLeave(): void {
  if (!currentUser.value) return;
  scheduleHoverClose();
}

function handleTriggerClick(): void {
  if (!currentUser.value) return;
  clearHoverCloseTimer();
  if (userMenuPinned.value) {
    userMenuPinned.value = false;
    userMenuHovered.value = false;
    return;
  }
  userMenuPinned.value = true;
}

function handleUserMenuClickOutside(event: MouseEvent): void {
  const target = event.target as Node | null;
  if (target && userTriggerRef.value?.contains(target)) return;
  closeUserMenu();
}

const userMenuProps = () => ({
  onMouseenter: handleMenuMouseEnter,
  onMouseleave: handleMenuMouseLeave,
});

function openLoginModal(): void {
  authModalLoaded.value = true;
  isLoginModalOpen.value = true;
}

async function handleUserMenuSelect(key: string): Promise<void> {
  closeUserMenu();
  if (key === "my-plugins" && myPublicUsername.value) {
    await router.push(`/author/${encodeURIComponent(myPublicUsername.value)}`);
    return;
  }
  if (key === "profile") {
    await router.push("/settings/personal");
    return;
  }
  if (key === "workbench") {
    await router.push("/plugin-workbench");
    return;
  }
  if (key === "settings") {
    await router.push("/admin/settings");
    return;
  }
  if (key !== "logout") return;

  try {
    await logout();
    message.success("已退出登录");
    await router.push("/");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "退出失败");
  }
}

onBeforeUnmount(() => {
  clearHoverCloseTimer();
});
</script>

<template>
  <header class="app-header">
    <nav class="top-nav" aria-label="主导航">
      <router-link class="brand" to="/" aria-label="返回插件墙">
        <img
          :src="siteIconUrl"
          :alt="`${siteName} 标志`"
          class="brand-logo"
          width="22"
          height="22"
        />
        <span class="brand-name">{{ siteName }}</span>
      </router-link>

      <div class="nav-actions">
        <router-link class="nav-link nav-link--optional" to="/">插件墙</router-link>
        <router-link class="nav-link nav-link--optional" to="/docs/rest">文档</router-link>

        <announcement-button />

        <router-link
          v-if="currentUser"
          class="nav-icon-link"
          to="/notifications"
          :aria-label="hasUnreadNotifications ? `消息，${unreadNotificationCount} 条未读` : '消息'"
        >
          <n-icon><notifications-outline /></n-icon>
          <span v-if="hasUnreadNotifications" class="notification-dot" aria-hidden="true"></span>
        </router-link>

        <n-dropdown
          v-if="headerUser"
          :show="userMenuOpen"
          :options="userMenuOptions"
          :menu-props="userMenuProps"
          trigger="manual"
          @clickoutside="handleUserMenuClickOutside"
          @select="handleUserMenuSelect"
        >
          <button
            ref="userTriggerRef"
            type="button"
            class="nav-link user-trigger auth-session-control"
            :class="{ 'is-pinned': userMenuPinned }"
            :aria-label="`账户：${displayUserName}`"
            :aria-expanded="userMenuOpen"
            aria-haspopup="menu"
            :aria-busy="!currentUser"
            :disabled="!currentUser"
            :title="currentUser ? undefined : '正在验证登录状态'"
            @mouseenter="handleTriggerMouseEnter"
            @mouseleave="handleTriggerMouseLeave"
            @click="handleTriggerClick"
            @keydown.escape="closeUserMenu"
          >
            <span class="user-avatar" aria-hidden="true">
              <img
                v-if="showUserAvatar"
                :src="userAvatarUrl"
                alt=""
                class="user-avatar__image"
                referrerpolicy="no-referrer"
                @error="avatarLoadFailed = true"
              />
              <n-icon v-else><person-outline /></n-icon>
            </span>
            <span class="user-name">{{ displayUserName }}</span>
          </button>
        </n-dropdown>

        <button
          v-else
          type="button"
          class="nav-link login-trigger auth-session-control"
          @click="openLoginModal"
        >
          登录
        </button>
        <theme-mode-button circle class="theme-button" />
      </div>
    </nav>
  </header>

  <AuthModal v-if="authModalLoaded" v-model:show="isLoginModalOpen" />
</template>

<style scoped>
.app-header {
  position: sticky;
  top: 0;
  z-index: 900;
  width: 100%;
  color: var(--text-primary);
  background: color-mix(in srgb, var(--bg-card) 94%, transparent);
  border-bottom: 1px solid var(--border-base);
  backdrop-filter: blur(14px);
}

.top-nav {
  width: min(1824px, calc(100% - 96px));
  min-height: 60px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}

.brand {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: var(--text-primary);
  text-decoration: none;
}

.brand-logo {
  flex: 0 0 auto;
  object-fit: contain;
}

.brand-name {
  min-width: 0;
  overflow: hidden;
  color: var(--text-primary);
  font-size: 16px;
  font-weight: 750;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav-actions {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 18px;
}

.nav-link,
.nav-icon-link {
  position: relative;
  color: var(--text-secondary);
  font: inherit;
  font-size: 13px;
  line-height: 1;
  text-decoration: none;
}

.nav-link {
  padding: 4px 0;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.user-trigger {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.user-trigger.is-pinned {
  color: var(--primary-color);
}

.user-trigger:disabled {
  color: var(--text-secondary);
  cursor: default;
  opacity: 1;
}

.user-avatar {
  width: 26px;
  height: 26px;
  display: inline-grid;
  flex: 0 0 auto;
  overflow: hidden;
  place-items: center;
  color: var(--text-tertiary);
  background: var(--bg-hover);
  border: 1px solid var(--border-base);
  border-radius: 50%;
}

.user-avatar__image {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.user-name {
  min-width: 0;
}

.nav-link:hover,
.nav-link:focus-visible,
.nav-icon-link:hover,
.nav-icon-link:focus-visible,
.router-link-active.nav-link {
  color: var(--primary-color);
  outline: none;
}

.nav-icon-link {
  display: inline-grid;
  width: 28px;
  height: 28px;
  place-items: center;
  font-size: 17px;
}

.notification-dot {
  position: absolute;
  top: 3px;
  right: 2px;
  width: 6px;
  height: 6px;
  background: #ef4444;
  border: 1px solid var(--bg-card);
  border-radius: 50%;
}

.theme-button {
  width: 38px;
  height: 38px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

@media (max-width: 720px) {
  .top-nav {
    width: min(100% - 28px, 1824px);
    min-height: 56px;
  }

  .brand-name {
    max-width: 48vw;
    font-size: 14px;
  }

  .top-nav {
    gap: 12px;
  }

  .nav-actions {
    flex: 0 0 auto;
    gap: 8px;
  }

  .nav-link--optional {
    display: none;
  }

  .user-trigger {
    gap: 6px;
  }

  .user-name {
    max-width: 72px;
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
