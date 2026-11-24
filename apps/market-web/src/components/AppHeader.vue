<script setup lang="ts">
import { computed, h, onUnmounted, reactive, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import {
  NAlert,
  NButton,
  NCheckbox,
  NDivider,
  NDropdown,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NModal,
  NTabPane,
  NTabs,
  useMessage,
} from "naive-ui";
import {
  GitBranchOutline,
  LogOutOutline,
  LogoGithub,
  NotificationsOutline,
  PersonOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
} from "@vicons/ionicons5";
import ThemeModeButton from "./ThemeModeButton.vue";
import AnnouncementButton from "./AnnouncementButton.vue";
import { usePluginStore } from "../stores/plugins";
import { getGitProviderLabel, githubRawUrl } from "../utils/github";
import type { OAuthProviderKey, PublicOAuthProviderInfo } from "../types";

const router = useRouter();
const message = useMessage();
const store = usePluginStore();
const { currentUser, currentUserPreview, siteConfig, unreadNotificationCount } = storeToRefs(store);
const {
  loginWithGithub,
  loginWithOAuth,
  loginWithPassword,
  registerWithEmailDirect,
  requestEmailRegisterCode,
  verifyEmailRegisterCode,
  requestPasswordResetCode,
  verifyPasswordResetCode,
  logout,
} = store;

const isLoginModalOpen = shallowRef(false);
const agreementAccepted = shallowRef(false);
const avatarLoadFailed = shallowRef(false);
const authTab = shallowRef<"login" | "register">("login");
const showResetPassword = shallowRef(false);
const authSubmitting = shallowRef(false);

const loginForm = reactive({
  username: "",
  password: "",
});

const registerForm = reactive({
  username: "",
  email: "",
  password: "",
  confirmPassword: "",
  code: "",
  registrationId: "",
});

const resetForm = reactive({
  email: "",
  code: "",
  newPassword: "",
  confirmPassword: "",
  resetId: "",
});

const registerCooldown = shallowRef(0);
const resetCooldown = shallowRef(0);
let cooldownTimer: number | undefined;

function startCooldownTimer(): void {
  if (cooldownTimer !== undefined) return;
  cooldownTimer = window.setInterval(() => {
    if (registerCooldown.value > 0) registerCooldown.value -= 1;
    if (resetCooldown.value > 0) resetCooldown.value -= 1;
    if (registerCooldown.value <= 0 && resetCooldown.value <= 0) {
      window.clearInterval(cooldownTimer);
      cooldownTimer = undefined;
    }
  }, 1000);
}

onUnmounted(() => {
  if (cooldownTimer !== undefined) window.clearInterval(cooldownTimer);
});

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
  githubRawUrl(String(headerUser.value?.avatar_url || headerUser.value?.avatar || "").trim()),
);
const showUserAvatar = computed(() => Boolean(userAvatarUrl.value) && !avatarLoadFailed.value);
const authConfig = computed(() => siteConfig.value.auth);
const passwordLoginEnabled = computed(() => authConfig.value.public_login_enabled !== false);
const emailRegisterEnabled = computed(() => Boolean(authConfig.value.email_registration_enabled));
const emailVerificationRequired = computed(
  () => authConfig.value.email_verification_required !== false,
);
const emailServiceEnabled = computed(() => Boolean(authConfig.value.email_service_enabled));
const githubLoginEnabled = computed(() => Boolean(authConfig.value.github_login_enabled));
const enabledOAuthProviders = computed<PublicOAuthProviderInfo[]>(() => {
  const configured = authConfig.value.oauth_providers;
  if (Array.isArray(configured) && configured.length > 0) {
    return configured.filter((item) => item.enabled);
  }
  const fallbackKeys: Array<{ provider: OAuthProviderKey; flag: boolean }> = [
    { provider: "github", flag: Boolean(authConfig.value.github_login_enabled) },
    { provider: "gitlab", flag: Boolean(authConfig.value.gitlab_login_enabled) },
    { provider: "gitee", flag: Boolean(authConfig.value.gitee_login_enabled) },
    { provider: "atomgit", flag: Boolean(authConfig.value.atomgit_login_enabled) },
    { provider: "gitea", flag: Boolean(authConfig.value.gitea_login_enabled) },
  ];
  return fallbackKeys
    .filter((item) => item.flag)
    .map((item) => ({
      provider: item.provider,
      label: getGitProviderLabel(item.provider),
      enabled: true,
      login_path:
        item.provider === "github"
          ? "/v1/auth/github/login"
          : `/v1/auth/oauth/${item.provider}/login`,
    }));
});
const hasAnyAuthMethod = computed(
  () =>
    passwordLoginEnabled.value ||
    emailRegisterEnabled.value ||
    githubLoginEnabled.value ||
    enabledOAuthProviders.value.length > 0,
);
function handleOAuthLogin(provider: OAuthProviderKey): void {
  if (provider === "github") {
    loginWithGithub();
    return;
  }
  loginWithOAuth(provider);
}
const agreementText = computed(() => {
  const auth = authConfig.value || {};
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

watch(userAvatarUrl, () => {
  avatarLoadFailed.value = false;
});

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

function resetModalForms(): void {
  showResetPassword.value = false;
  authTab.value = passwordLoginEnabled.value
    ? "login"
    : emailRegisterEnabled.value
      ? "register"
      : "login";
  loginForm.username = "";
  loginForm.password = "";
  registerForm.username = "";
  registerForm.email = "";
  registerForm.password = "";
  registerForm.confirmPassword = "";
  registerForm.code = "";
  registerForm.registrationId = "";
  resetForm.email = "";
  resetForm.code = "";
  resetForm.newPassword = "";
  resetForm.confirmPassword = "";
  resetForm.resetId = "";
}

function openLoginModal(): void {
  agreementAccepted.value = false;
  resetModalForms();
  isLoginModalOpen.value = true;
}

async function handlePasswordLogin(): Promise<void> {
  if (!canSubmitLogin.value) return;
  if (!loginForm.username.trim() || !loginForm.password) {
    message.warning("请输入用户名或邮箱以及密码");
    return;
  }
  authSubmitting.value = true;
  try {
    await loginWithPassword({
      username: loginForm.username.trim(),
      password: loginForm.password,
    });
    isLoginModalOpen.value = false;
    message.success("登录成功");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "登录失败");
  } finally {
    authSubmitting.value = false;
  }
}

async function handleDirectRegister(): Promise<void> {
  if (!canSubmitLogin.value) return;
  if (!registerForm.username.trim() || !registerForm.email.trim() || !registerForm.password) {
    message.warning("请完整填写用户名、邮箱和密码");
    return;
  }
  if (registerForm.password !== registerForm.confirmPassword) {
    message.warning("两次输入的密码不一致");
    return;
  }
  authSubmitting.value = true;
  try {
    await registerWithEmailDirect({
      username: registerForm.username.trim(),
      email: registerForm.email.trim(),
      password: registerForm.password,
    });
    isLoginModalOpen.value = false;
    message.success("注册成功并已自动登录");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "注册失败");
  } finally {
    authSubmitting.value = false;
  }
}

async function handleRequestRegisterCode(): Promise<void> {
  if (!canSubmitLogin.value) return;
  if (!registerForm.username.trim() || !registerForm.email.trim() || !registerForm.password) {
    message.warning("请先填写用户名、邮箱和密码");
    return;
  }
  if (registerForm.password !== registerForm.confirmPassword) {
    message.warning("两次输入的密码不一致");
    return;
  }
  authSubmitting.value = true;
  try {
    const result = await requestEmailRegisterCode({
      username: registerForm.username.trim(),
      email: registerForm.email.trim(),
      password: registerForm.password,
    });
    registerForm.registrationId = result.registration_id;
    registerCooldown.value = 60;
    startCooldownTimer();
    message.success("验证码已发送，请查收邮件");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "发送验证码失败");
  } finally {
    authSubmitting.value = false;
  }
}

async function handleVerifyRegisterCode(): Promise<void> {
  if (!canSubmitLogin.value) return;
  if (!registerForm.registrationId || !registerForm.code.trim()) {
    message.warning("请先获取并输入邮箱验证码");
    return;
  }
  authSubmitting.value = true;
  try {
    await verifyEmailRegisterCode({
      registration_id: registerForm.registrationId,
      code: registerForm.code.trim(),
    });
    isLoginModalOpen.value = false;
    message.success("注册成功并已自动登录");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "验证码校验失败");
  } finally {
    authSubmitting.value = false;
  }
}

async function handleRequestPasswordReset(): Promise<void> {
  if (!resetForm.email.trim()) {
    message.warning("请输入账号邮箱");
    return;
  }
  authSubmitting.value = true;
  try {
    const result = await requestPasswordResetCode({ email: resetForm.email.trim() });
    resetForm.resetId = result.reset_id;
    resetCooldown.value = 60;
    startCooldownTimer();
    message.success("如果该邮箱已关联账号，重置验证码已发送，请查收邮件");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "发送重置验证码失败");
  } finally {
    authSubmitting.value = false;
  }
}

async function handleVerifyPasswordReset(): Promise<void> {
  if (!resetForm.resetId || !resetForm.code.trim() || !resetForm.newPassword) {
    message.warning("请输入验证码和新密码");
    return;
  }
  if (resetForm.newPassword !== resetForm.confirmPassword) {
    message.warning("两次输入的新密码不一致");
    return;
  }
  authSubmitting.value = true;
  try {
    await verifyPasswordResetCode({
      reset_id: resetForm.resetId,
      code: resetForm.code.trim(),
      new_password: resetForm.newPassword,
    });
    message.success("密码已重置，请使用新密码登录");
    loginForm.username = resetForm.email.trim();
    loginForm.password = "";
    showResetPassword.value = false;
  } catch (error) {
    message.error(error instanceof Error ? error.message : "密码重置失败");
  } finally {
    authSubmitting.value = false;
  }
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
          :options="userMenuOptions"
          trigger="click"
          @select="handleUserMenuSelect"
        >
          <button
            type="button"
            class="nav-link user-trigger auth-session-control"
            :aria-label="`账户：${displayUserName}`"
            :aria-busy="!currentUser"
            :disabled="!currentUser"
            :title="currentUser ? undefined : '正在验证登录状态'"
          >
            <span class="user-avatar" aria-hidden="true">
              <img
                v-if="showUserAvatar"
                :src="userAvatarUrl"
                alt=""
                class="user-avatar__image"
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

  <n-modal v-model:show="isLoginModalOpen" preset="card" title="登录 / 注册" class="login-modal">
    <div class="login-methods">
      <template v-if="showResetPassword">
        <div class="auth-subhead">
          <strong>重置密码</strong>
          <n-button text type="primary" size="small" @click="showResetPassword = false">
            返回登录
          </n-button>
        </div>
        <n-alert v-if="!emailServiceEnabled" type="warning" :bordered="false">
          当前站点未启用邮件服务，暂无法发送重置密码验证码，请联系管理员处理。
        </n-alert>
        <n-form v-else label-placement="top" size="small">
          <n-form-item label="账号邮箱">
            <div class="inline-code-row">
              <n-input
                v-model:value="resetForm.email"
                placeholder="name@example.com"
                inputmode="email"
              />
              <n-button
                :loading="authSubmitting"
                :disabled="resetCooldown > 0"
                @click="handleRequestPasswordReset"
              >
                {{ resetCooldown > 0 ? `${resetCooldown}s 后重发` : "发送验证码" }}
              </n-button>
            </div>
          </n-form-item>
          <n-form-item label="邮箱验证码">
            <n-input v-model:value="resetForm.code" placeholder="输入 6 位验证码" maxlength="6" />
          </n-form-item>
          <n-form-item label="新密码">
            <n-input
              v-model:value="resetForm.newPassword"
              type="password"
              show-password-on="click"
              placeholder="至少 8 个字符"
            />
          </n-form-item>
          <n-form-item label="确认新密码">
            <n-input
              v-model:value="resetForm.confirmPassword"
              type="password"
              show-password-on="click"
              placeholder="再次输入新密码"
              @keyup.enter="handleVerifyPasswordReset"
            />
          </n-form-item>
          <n-button
            type="primary"
            block
            :loading="authSubmitting"
            :disabled="!resetForm.resetId || !resetForm.code.trim() || !resetForm.newPassword"
            @click="handleVerifyPasswordReset"
          >
            确认重置密码
          </n-button>
        </n-form>
      </template>

      <template v-else-if="hasAnyAuthMethod">
        <n-tabs
          v-if="passwordLoginEnabled || emailRegisterEnabled"
          v-model:value="authTab"
          type="segment"
          animated
        >
          <n-tab-pane v-if="passwordLoginEnabled" name="login" tab="密码登录">
            <n-form label-placement="top" size="small" class="auth-form">
              <n-form-item label="用户名或邮箱">
                <n-input
                  v-model:value="loginForm.username"
                  placeholder="输入用户名或邮箱"
                  autocomplete="username"
                />
              </n-form-item>
              <n-form-item label="密码">
                <n-input
                  v-model:value="loginForm.password"
                  type="password"
                  show-password-on="click"
                  placeholder="输入登录密码"
                  autocomplete="current-password"
                  @keyup.enter="handlePasswordLogin"
                />
              </n-form-item>
              <div class="auth-helper-row">
                <n-button
                  v-if="emailServiceEnabled"
                  text
                  size="small"
                  type="primary"
                  @click="showResetPassword = true"
                >
                  忘记密码？
                </n-button>
              </div>
              <n-button
                type="primary"
                block
                :loading="authSubmitting"
                :disabled="!canSubmitLogin"
                @click="handlePasswordLogin"
              >
                登录
              </n-button>
            </n-form>
          </n-tab-pane>

          <n-tab-pane v-if="emailRegisterEnabled" name="register" tab="邮箱注册">
            <n-alert
              v-if="emailVerificationRequired && !emailServiceEnabled"
              type="warning"
              :bordered="false"
            >
              当前站点开启了邮箱验证但未配置邮件服务，暂无法进行邮箱注册。
            </n-alert>
            <n-form v-else label-placement="top" size="small" class="auth-form">
              <n-form-item label="用户名">
                <n-input
                  v-model:value="registerForm.username"
                  placeholder="3-39 位字母、数字、点、下划线或短横线"
                  autocomplete="username"
                />
              </n-form-item>
              <n-form-item label="邮箱">
                <n-input
                  v-model:value="registerForm.email"
                  placeholder="name@example.com"
                  inputmode="email"
                  autocomplete="email"
                />
              </n-form-item>
              <n-form-item label="密码">
                <n-input
                  v-model:value="registerForm.password"
                  type="password"
                  show-password-on="click"
                  placeholder="至少 8 个字符"
                  autocomplete="new-password"
                />
              </n-form-item>
              <n-form-item label="确认密码">
                <n-input
                  v-model:value="registerForm.confirmPassword"
                  type="password"
                  show-password-on="click"
                  placeholder="再次输入密码"
                  autocomplete="new-password"
                />
              </n-form-item>

              <template v-if="emailVerificationRequired">
                <n-form-item label="邮箱验证码">
                  <div class="inline-code-row">
                    <n-input
                      v-model:value="registerForm.code"
                      placeholder="输入 6 位验证码"
                      maxlength="6"
                      @keyup.enter="handleVerifyRegisterCode"
                    />
                    <n-button
                      :loading="authSubmitting"
                      :disabled="!canSubmitLogin || registerCooldown > 0"
                      @click="handleRequestRegisterCode"
                    >
                      {{ registerCooldown > 0 ? `${registerCooldown}s 后重发` : "发送验证码" }}
                    </n-button>
                  </div>
                </n-form-item>
                <n-button
                  type="primary"
                  block
                  :loading="authSubmitting"
                  :disabled="
                    !canSubmitLogin || !registerForm.registrationId || !registerForm.code.trim()
                  "
                  @click="handleVerifyRegisterCode"
                >
                  验证并注册
                </n-button>
              </template>
              <n-button
                v-else
                type="primary"
                block
                :loading="authSubmitting"
                :disabled="!canSubmitLogin"
                @click="handleDirectRegister"
              >
                立即注册
              </n-button>
            </n-form>
          </n-tab-pane>
        </n-tabs>

        <n-divider
          v-if="(passwordLoginEnabled || emailRegisterEnabled) && enabledOAuthProviders.length"
        >
          或
        </n-divider>

        <div v-if="enabledOAuthProviders.length" class="oauth-login-list">
          <n-button
            v-for="(item, index) in enabledOAuthProviders"
            :key="item.provider"
            :type="
              !passwordLoginEnabled && !emailRegisterEnabled && index === 0 ? 'primary' : 'default'
            "
            block
            :disabled="!canSubmitLogin"
            @click="handleOAuthLogin(item.provider)"
          >
            <template #icon>
              <n-icon>
                <logo-github v-if="item.provider === 'github'" />
                <git-branch-outline v-else />
              </n-icon>
            </template>
            {{ item.label }} 登录 / 注册
          </n-button>
        </div>
      </template>

      <n-alert v-else type="warning" :bordered="false">
        当前站点已关闭公开登录与注册，仅核心管理员可通过后台入口登录。
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

.user-trigger {
  display: inline-flex;
  align-items: center;
  gap: 7px;
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

:global(.login-modal) {
  width: min(440px, calc(100vw - 32px));
  border-radius: 8px;
}

.login-methods {
  display: grid;
  gap: 12px;
}

.auth-form {
  margin-top: 12px;
}

.auth-subhead,
.auth-helper-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.auth-helper-row {
  justify-content: flex-end;
  margin: -8px 0 10px;
}

.inline-code-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  width: 100%;
}

.agreement-box {
  margin-bottom: 4px;
}

.oauth-login-list {
  display: grid;
  gap: 10px;
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
