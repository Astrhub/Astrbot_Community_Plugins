<script setup lang="ts">
import { computed, onUnmounted, reactive, shallowRef, watch } from "vue";
import {
  NAlert,
  NButton,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NSpace,
  NTag,
  useDialog,
  useMessage,
} from "naive-ui";
import {
  KeyOutline,
  LinkOutline,
  LockClosedOutline,
  LogoGithub,
  MailOutline,
  PersonCircleOutline,
  SaveOutline,
  UnlinkOutline,
} from "@vicons/ionicons5";
import { githubRawUrl } from "@/utils/github";
import type { AuthConfig, OAuthIdentity, User } from "@/types";

const githubName = defineModel("githubName", { type: String, default: "" });
const githubToken = defineModel("githubToken", { type: String, default: "" });
const refreshInterval = defineModel("refreshInterval", { type: Number, default: 3600 });

const props = withDefaults(
  defineProps<{
    currentUser: User | null;
    authConfig?: Partial<AuthConfig>;
    saving?: boolean;
    passwordSaving?: boolean;
    emailSaving?: boolean;
    oauthBusyId?: string;
  }>(),
  {
    currentUser: null,
    authConfig: () => ({}),
    saving: false,
    passwordSaving: false,
    emailSaving: false,
    oauthBusyId: "",
  },
);

const emit = defineEmits<{
  save: [];
  "connect-github": [];
  "unbind-oauth": [identity: OAuthIdentity];
  "update-password": [payload: { current_password?: string; new_password: string; email?: string }];
  "request-email-change": [
    payload: {
      current_password: string;
      new_email: string;
      onPending?: (changeId: string) => void;
      onCompleted?: () => void;
    },
  ];
  "verify-email-change": [payload: { change_id: string; code: string; onCompleted?: () => void }];
}>();

const message = useMessage();
const dialog = useDialog();

const publicUsername = computed(
  () =>
    props.currentUser?.public_username ||
    props.currentUser?.github_login ||
    props.currentUser?.internal_username ||
    "",
);
const hasGithubToken = computed(() => Boolean(props.currentUser?.has_github_token));
const hasPassword = computed(() => Boolean(props.currentUser?.has_password));
const userEmail = computed(() => String(props.currentUser?.email || "").trim());
const emailVerified = computed(() => Boolean(props.currentUser?.email_verified));
const oauthIdentities = computed<OAuthIdentity[]>(() => props.currentUser?.oauth_identities || []);
const githubIdentity = computed(() =>
  oauthIdentities.value.find((item) => item.provider === "github"),
);
const githubLoginEnabled = computed(() => Boolean(props.authConfig?.github_login_enabled));
const emailVerificationRequired = computed(
  () => props.authConfig?.email_verification_required !== false,
);
const emailServiceEnabled = computed(() => Boolean(props.authConfig?.email_service_enabled));

const tierInfo = computed(() => {
  const tier = props.currentUser?.account_tier || props.currentUser?.role || "user";
  if (tier === "core_admin") return { label: "核心管理员", type: "error" as const };
  if (tier === "admin") return { label: "管理员", type: "warning" as const };
  if (tier === "developer") return { label: "开发者", type: "info" as const };
  return { label: "注册用户", type: "default" as const };
});

const avatarLoadFailed = shallowRef(false);
const profileAvatarUrl = computed(() =>
  githubRawUrl(String(props.currentUser?.avatar_url || props.currentUser?.avatar || "").trim()),
);
const showProfileAvatar = computed(
  () => Boolean(profileAvatarUrl.value) && !avatarLoadFailed.value,
);

watch(profileAvatarUrl, () => {
  avatarLoadFailed.value = false;
});

const passwordForm = reactive({
  currentPassword: "",
  newPassword: "",
  confirmPassword: "",
  email: "",
});

const emailForm = reactive({
  currentPassword: "",
  newEmail: "",
  changeId: "",
  code: "",
});

const emailChangeCooldown = shallowRef(0);
let cooldownTimer: number | undefined;

function startCooldown(): void {
  emailChangeCooldown.value = 60;
  if (cooldownTimer !== undefined) return;
  cooldownTimer = window.setInterval(() => {
    emailChangeCooldown.value -= 1;
    if (emailChangeCooldown.value <= 0) {
      window.clearInterval(cooldownTimer);
      cooldownTimer = undefined;
    }
  }, 1000);
}

onUnmounted(() => {
  if (cooldownTimer !== undefined) window.clearInterval(cooldownTimer);
});

function canUnbind(identity: OAuthIdentity): boolean {
  if (oauthIdentities.value.length > 1) return true;
  return hasPassword.value;
}

function submitPassword(): void {
  if (passwordForm.newPassword.length < 8) {
    message.warning("新密码长度不能少于 8 个字符");
    return;
  }
  if (passwordForm.newPassword !== passwordForm.confirmPassword) {
    message.warning("两次输入的密码不一致");
    return;
  }
  if (hasPassword.value && !passwordForm.currentPassword) {
    message.warning("请输入当前密码");
    return;
  }
  emit("update-password", {
    ...(hasPassword.value ? { current_password: passwordForm.currentPassword } : {}),
    new_password: passwordForm.newPassword,
    ...(!hasPassword.value && !userEmail.value && passwordForm.email.trim()
      ? { email: passwordForm.email.trim() }
      : {}),
  });
  passwordForm.currentPassword = "";
  passwordForm.newPassword = "";
  passwordForm.confirmPassword = "";
  passwordForm.email = "";
}

function submitEmailChangeRequest(): void {
  if (!emailForm.currentPassword) {
    message.warning("请先输入当前登录密码");
    return;
  }
  if (!emailForm.newEmail.trim()) {
    message.warning("请输入新的邮箱地址");
    return;
  }
  emit("request-email-change", {
    current_password: emailForm.currentPassword,
    new_email: emailForm.newEmail.trim(),
    onPending: (changeId: string) => {
      emailForm.changeId = changeId;
      startCooldown();
    },
    onCompleted: () => {
      emailForm.currentPassword = "";
      emailForm.newEmail = "";
      emailForm.changeId = "";
      emailForm.code = "";
    },
  });
}

function submitEmailChangeVerify(): void {
  if (!emailForm.changeId || !emailForm.code.trim()) {
    message.warning("请输入收到的 6 位验证码");
    return;
  }
  emit("verify-email-change", {
    change_id: emailForm.changeId,
    code: emailForm.code.trim(),
    onCompleted: () => {
      emailForm.currentPassword = "";
      emailForm.newEmail = "";
      emailForm.changeId = "";
      emailForm.code = "";
    },
  });
}

function confirmUnbind(identity: OAuthIdentity): void {
  dialog.warning({
    title: "解绑 OAuth 账号",
    content:
      "解绑后，你仍可使用本地密码或其他已绑定的 OAuth 登录。已上架或审核中的插件会继续保留在当前账号名下；但在重新绑定 OAuth 前将无法提交新插件、重新申请上架或修改插件仓库。",
    positiveText: "确认解绑",
    negativeText: "取消",
    onPositiveClick: () => emit("unbind-oauth", identity),
  });
}
</script>

<template>
  <div class="account-stack">
    <section class="settings-section">
      <div class="section-heading">
        <div class="section-icon">
          <NIcon><PersonCircleOutline /></NIcon>
        </div>
        <div>
          <p class="section-kicker">账号资料</p>
          <h2 class="section-title">个人信息与身份</h2>
        </div>
      </div>

      <div class="github-summary">
        <div class="github-main">
          <span class="profile-avatar">
            <img
              v-if="showProfileAvatar"
              :src="profileAvatarUrl"
              :alt="`${publicUsername || '当前用户'}的头像`"
              class="profile-avatar__image"
              @error="avatarLoadFailed = true"
            />
            <NIcon v-else><PersonCircleOutline /></NIcon>
          </span>
          <div class="github-copy">
            <span class="summary-label">公开用户名</span>
            <strong>@{{ publicUsername || "未设置" }}</strong>
            <span v-if="userEmail" class="summary-meta">
              登录邮箱：{{ userEmail }}（{{ emailVerified ? "已验证" : "未验证" }}）
            </span>
          </div>
        </div>
        <NSpace :size="8">
          <NTag :type="tierInfo.type" size="small" round>{{ tierInfo.label }}</NTag>
          <NTag :type="oauthIdentities.length ? 'success' : 'warning'" size="small" round>
            {{
              oauthIdentities.length ? `已绑定 ${oauthIdentities.length} 个 OAuth` : "未绑定 OAuth"
            }}
          </NTag>
        </NSpace>
      </div>

      <NForm label-placement="top" class="settings-form">
        <NFormItem label="显示名称">
          <NInput v-model:value="githubName" placeholder="你的显示名称" />
        </NFormItem>

        <div class="token-row">
          <NFormItem label="GitHub API Token">
            <NInput
              v-model:value="githubToken"
              type="password"
              show-password-on="click"
              :placeholder="
                hasGithubToken ? '已配置，留空保持不变' : 'ghp_... 或 fine-grained token'
              "
            >
              <template #prefix>
                <NIcon><KeyOutline /></NIcon>
              </template>
            </NInput>
            <template #feedback>
              {{ hasGithubToken ? "当前已配置 Token" : "当前未配置 Token" }}
            </template>
          </NFormItem>

          <NFormItem label="自动刷新间隔（秒）">
            <NInputNumber
              v-model:value="refreshInterval"
              :min="300"
              :max="86400"
              :step="300"
              class="interval-input"
            />
          </NFormItem>
        </div>

        <div class="section-actions">
          <NButton type="primary" :loading="saving" @click="emit('save')">
            <template #icon>
              <NIcon><SaveOutline /></NIcon>
            </template>
            保存资料
          </NButton>
        </div>
      </NForm>
    </section>

    <section class="settings-section">
      <div class="section-heading">
        <div class="section-icon">
          <NIcon><LinkOutline /></NIcon>
        </div>
        <div>
          <p class="section-kicker">开发者身份与第三方授权</p>
          <h2 class="section-title">OAuth 账号绑定</h2>
        </div>
      </div>

      <NAlert v-if="!oauthIdentities.length" type="info" :bordered="false" class="section-alert">
        绑定至少一个 OAuth 账号即可自动升级为开发者，获得提交插件、管理插件及创建写入权限 API Key
        的能力。
      </NAlert>

      <div class="oauth-list">
        <div v-if="githubIdentity" class="oauth-row">
          <div class="oauth-info">
            <NIcon class="oauth-provider-icon"><LogoGithub /></NIcon>
            <div>
              <strong>GitHub · @{{ githubIdentity.provider_login }}</strong>
              <span class="summary-meta">{{ githubIdentity.profile_url }}</span>
            </div>
          </div>
          <NSpace :size="8">
            <NButton
              v-if="githubLoginEnabled"
              size="small"
              secondary
              @click="emit('connect-github')"
            >
              重新授权
            </NButton>
            <NButton
              size="small"
              secondary
              type="error"
              :disabled="!canUnbind(githubIdentity)"
              :loading="oauthBusyId === githubIdentity.id"
              :title="
                canUnbind(githubIdentity) ? undefined : '请先设置本地登录密码后再解绑最后一个 OAuth'
              "
              @click="confirmUnbind(githubIdentity)"
            >
              <template #icon>
                <NIcon><UnlinkOutline /></NIcon>
              </template>
              解绑
            </NButton>
          </NSpace>
        </div>

        <div v-else class="oauth-row">
          <div class="oauth-info">
            <NIcon class="oauth-provider-icon"><LogoGithub /></NIcon>
            <div>
              <strong>GitHub</strong>
              <span class="summary-meta">未绑定</span>
            </div>
          </div>
          <NButton
            type="primary"
            size="small"
            :disabled="!githubLoginEnabled"
            @click="emit('connect-github')"
          >
            绑定 GitHub
          </NButton>
        </div>
      </div>
      <p v-if="oauthIdentities.length === 1 && !hasPassword" class="lockout-hint">
        当前账号尚未设置本地密码。如需解绑最后一个 OAuth
        账号，请先在下方设置登录密码，以免无法登录。
      </p>
    </section>

    <section class="settings-section">
      <div class="section-heading">
        <div class="section-icon">
          <NIcon><LockClosedOutline /></NIcon>
        </div>
        <div>
          <p class="section-kicker">账号安全</p>
          <h2 class="section-title">{{ hasPassword ? "修改登录密码" : "设置本地登录密码" }}</h2>
        </div>
      </div>

      <NForm label-placement="top" class="settings-form">
        <NFormItem v-if="hasPassword" label="当前密码">
          <NInput
            v-model:value="passwordForm.currentPassword"
            type="password"
            show-password-on="click"
            placeholder="输入当前登录密码"
          />
        </NFormItem>
        <NFormItem v-if="!hasPassword && !userEmail" label="登录邮箱（可选）">
          <NInput
            v-model:value="passwordForm.email"
            placeholder="可选，绑定后可用邮箱登录与找回密码"
            inputmode="email"
          />
        </NFormItem>
        <div class="password-grid">
          <NFormItem label="新密码">
            <NInput
              v-model:value="passwordForm.newPassword"
              type="password"
              show-password-on="click"
              placeholder="至少 8 个字符"
            />
          </NFormItem>
          <NFormItem label="确认新密码">
            <NInput
              v-model:value="passwordForm.confirmPassword"
              type="password"
              show-password-on="click"
              placeholder="再次输入新密码"
            />
          </NFormItem>
        </div>
        <div class="section-actions">
          <NButton type="primary" :loading="passwordSaving" @click="submitPassword">
            {{ hasPassword ? "更新密码" : "设置密码" }}
          </NButton>
        </div>
      </NForm>
    </section>

    <section v-if="hasPassword" class="settings-section">
      <div class="section-heading">
        <div class="section-icon">
          <NIcon><MailOutline /></NIcon>
        </div>
        <div>
          <p class="section-kicker">登录邮箱</p>
          <h2 class="section-title">{{ userEmail ? "更换登录邮箱" : "绑定登录邮箱" }}</h2>
        </div>
      </div>

      <NAlert
        v-if="emailVerificationRequired && !emailServiceEnabled"
        type="warning"
        :bordered="false"
        class="section-alert"
      >
        当前站点开启了邮箱验证但未启用邮件服务，暂无法发送换绑验证码。
      </NAlert>

      <NForm v-else label-placement="top" class="settings-form">
        <div class="password-grid">
          <NFormItem label="当前密码">
            <NInput
              v-model:value="emailForm.currentPassword"
              type="password"
              show-password-on="click"
              placeholder="输入当前密码验证身份"
            />
          </NFormItem>
          <NFormItem label="新邮箱地址">
            <NInput
              v-model:value="emailForm.newEmail"
              placeholder="new-email@example.com"
              inputmode="email"
            />
          </NFormItem>
        </div>

        <template v-if="emailVerificationRequired">
          <NFormItem label="新邮箱验证码">
            <div class="inline-code-row">
              <NInput v-model:value="emailForm.code" placeholder="输入 6 位验证码" maxlength="6" />
              <NButton
                :loading="emailSaving"
                :disabled="emailChangeCooldown > 0"
                @click="submitEmailChangeRequest"
              >
                {{ emailChangeCooldown > 0 ? `${emailChangeCooldown}s 后重发` : "发送验证码" }}
              </NButton>
            </div>
          </NFormItem>
          <div class="section-actions">
            <NButton
              type="primary"
              :loading="emailSaving"
              :disabled="!emailForm.changeId || !emailForm.code.trim()"
              @click="submitEmailChangeVerify"
            >
              验证并保存邮箱
            </NButton>
          </div>
        </template>
        <div v-else class="section-actions">
          <NButton type="primary" :loading="emailSaving" @click="submitEmailChangeRequest">
            保存新邮箱
          </NButton>
        </div>
      </NForm>
    </section>
  </div>
</template>

<style scoped>
.account-stack {
  display: grid;
  gap: 18px;
}

.settings-section {
  padding: 22px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--card-color);
}

.section-heading,
.github-summary,
.github-main,
.oauth-row,
.oauth-info,
.section-actions {
  display: flex;
  align-items: center;
}

.section-heading {
  gap: 12px;
  margin-bottom: 18px;
}

.section-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  color: #0e74e4;
  background: rgba(14, 116, 228, 0.12);
  border-radius: 8px;
}

.section-kicker,
.section-title,
.summary-label {
  margin: 0;
}

.section-kicker,
.summary-label,
.summary-meta,
.lockout-hint {
  color: var(--text-color-3);
  font-size: 12px;
}

.section-title {
  font-size: 18px;
  line-height: 1.3;
}

.section-alert {
  margin-bottom: 16px;
}

.github-summary,
.oauth-row {
  justify-content: space-between;
  gap: 14px;
  padding: 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.github-summary {
  margin-bottom: 18px;
}

.github-main,
.oauth-info {
  min-width: 0;
  gap: 10px;
}

.oauth-list {
  display: grid;
  gap: 10px;
}

.oauth-provider-icon {
  font-size: 20px;
  color: var(--text-color-2);
}

.oauth-info > div {
  display: grid;
  gap: 2px;
}

.lockout-hint {
  margin: 12px 0 0;
}

.profile-avatar {
  width: 44px;
  height: 44px;
  display: inline-grid;
  flex: none;
  overflow: hidden;
  place-items: center;
  color: var(--text-color-3);
  background: var(--card-color);
  border: 1px solid var(--border-color);
  border-radius: 50%;
  font-size: 25px;
}

.profile-avatar__image {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.github-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.github-copy strong {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  overflow-wrap: anywhere;
}

.settings-form {
  display: grid;
  gap: 4px;
}

.token-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, 240px);
  gap: 14px;
}

.password-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.inline-code-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  width: 100%;
}

.interval-input {
  width: 100%;
}

.section-actions {
  justify-content: flex-end;
}

@media (max-width: 720px) {
  .github-summary,
  .oauth-row {
    align-items: flex-start;
    flex-direction: column;
  }

  .token-row,
  .password-grid {
    grid-template-columns: 1fr;
  }

  .section-actions {
    justify-content: stretch;
  }

  .section-actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
