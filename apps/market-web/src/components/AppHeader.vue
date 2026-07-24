<script setup lang="ts">
import { computed, h, shallowRef } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import { NAlert, NButton, NCheckbox, NDropdown, NIcon, NModal, useMessage } from "naive-ui";
import {
  LogInOutline,
  LogOutOutline,
  LogoGithub,
  NotificationsOutline,
  PersonOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
} from "@vicons/ionicons5";
import ThemeModeButton from "./ThemeModeButton.vue";
import { usePluginStore } from "../stores/plugins";

const router = useRouter();
const message = useMessage();
const store = usePluginStore();
const { currentUser, siteConfig, unreadNotificationCount } = storeToRefs(store);
const { loginWithGithub, logout } = store;

const isLoginModalOpen = shallowRef(false);
const agreementAccepted = shallowRef(false);
const siteName = computed(() => siteConfig.value.name || "Astrhub Plugins Market");
const siteIconUrl = computed(() => {
  const configuredUrl = String(siteConfig.value.icon_url || "").trim();
  return !configuredUrl || configuredUrl === "/logo.webp"
    ? "/logo.webp?v=20260725"
    : configuredUrl;
});
const isCoreAdmin = computed(() => currentUser.value?.role === "core_admin");
const isAdminUser = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);
const hasUnreadNotifications = computed(() => unreadNotificationCount.value > 0);
const displayUserName = computed(
  () =>
    currentUser.value?.github_login ||
    currentUser.value?.internal_username ||
    currentUser.value?.login ||
    "已登录",
);
const agreementText = computed(() => {
  const auth = siteConfig.value.auth || {};
  const parts: string[] = [];
  if (auth.login_agreement_enabled && auth.login_agreement_text) {
    parts.push(auth.login_agreement_text);
  }
  if (auth.service_terms_enabled && auth.service_terms_text) {
    parts.push(auth.service_terms_text);
  }
  return parts.join("\n\n");
});
const canSubmitLogin = computed(() => !agreementText.value || agreementAccepted.value);
const userMenuOptions = computed(() => [
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

function renderIcon(icon: unknown) {
  return () => h(NIcon, null, { default: () => h(icon as never) });
}

function openLoginModal(): void {
  agreementAccepted.value = false;
  isLoginModalOpen.value = true;
}

async function handleUserMenuSelect(key: string): Promise<void> {
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
          v-if="currentUser"
          :options="userMenuOptions"
          trigger="click"
          @select="handleUserMenuSelect"
        >
          <button type="button" class="nav-link user-trigger">{{ displayUserName }}</button>
        </n-dropdown>

        <button v-else type="button" class="nav-link login-trigger" @click="openLoginModal">
          登录
        </button>
        <theme-mode-button circle class="theme-button" />
      </div>
    </nav>
  </header>

  <n-modal v-model:show="isLoginModalOpen" preset="card" title="登录 / 注册" class="login-modal">
    <div class="login-methods">
      <n-button
        v-if="siteConfig.auth.github_login_enabled"
        type="primary"
        block
        :disabled="!canSubmitLogin"
        @click="loginWithGithub"
      >
        <template #icon
          ><n-icon><logo-github /></n-icon
        ></template>
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

:global(.login-modal) {
  width: min(420px, calc(100vw - 32px));
  border-radius: 8px;
}

.login-methods {
  display: grid;
  gap: 12px;
}

.agreement-box {
  margin-bottom: 4px;
}

.agreement-text {
  max-height: 160px;
  margin-bottom: 12px;
  overflow: auto;
  white-space: pre-wrap;
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

  .nav-actions {
    gap: 10px;
  }

  .nav-link--optional {
    display: none;
  }

  .user-trigger {
    max-width: 92px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
