<script setup lang="ts">
import { computed, onUnmounted, reactive, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import {
  NAlert,
  NButton,
  NCheckbox,
  NDivider,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NTabPane,
  NTabs,
  useMessage,
} from "naive-ui";
import GitProviderIcon from "./GitProviderIcon.vue";
import { usePluginStore } from "../stores/plugins";
import { getGitProviderLabel } from "../utils/github";
import type { OAuthProviderKey, PublicOAuthProviderInfo } from "../types";

const props = defineProps<{
  show: boolean;
}>();

const emit = defineEmits<{
  "update:show": [value: boolean];
}>();

const message = useMessage();
const store = usePluginStore();
const { siteConfig } = storeToRefs(store);
const {
  loginWithGithub,
  loginWithOAuth,
  loginWithPassword,
  registerWithEmailDirect,
  requestEmailRegisterCode,
  verifyEmailRegisterCode,
  requestPasswordResetCode,
  verifyPasswordResetCode,
} = store;

const agreementAccepted = shallowRef(false);
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

watch(
  () => props.show,
  (visible) => {
    if (!visible) return;
    agreementAccepted.value = false;
    resetModalForms();
  },
  { immediate: true },
);

function handleOAuthLogin(provider: OAuthProviderKey): void {
  if (provider === "github") {
    loginWithGithub();
    return;
  }
  loginWithOAuth(provider);
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
    emit("update:show", false);
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
    emit("update:show", false);
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
    emit("update:show", false);
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
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="登录 / 注册"
    class="login-modal"
    @update:show="(value: boolean) => emit('update:show', value)"
  >
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
              <git-provider-icon :provider="item.provider" :size="16" />
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
:global(.login-modal) {
  width: min(440px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  display: flex;
  flex-direction: column;
  border-radius: 8px;
}

:global(.login-modal > .n-card-header),
:global(.login-modal > .n-card__footer) {
  flex-shrink: 0;
}

:global(.login-modal > .n-card__content) {
  overflow-y: auto;
  min-height: 0;
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
</style>
