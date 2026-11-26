<script setup lang="ts">
import { computed, onMounted, reactive, shallowRef } from "vue";
import { useRouter } from "vue-router";
import { storeToRefs } from "pinia";
import {
  NButton,
  NIcon,
  NLayoutHeader,
  NSpin,
  NTabPane,
  NTabs,
  useDialog,
  useMessage,
} from "naive-ui";
import { ArrowBack } from "@vicons/ionicons5";
import AccessKeyManager from "@/components/settings/AccessKeyManager.vue";
import NotificationPreferencesSection from "@/components/settings/NotificationPreferencesSection.vue";
import ProfileAccountSection from "@/components/settings/ProfileAccountSection.vue";
import { usePluginStore } from "@/stores/plugins";
import type { ApiKey, OAuthIdentity, OAuthProviderKey } from "@/types";

const router = useRouter();
const message = useMessage();
const dialog = useDialog();
const store = usePluginStore();
const { currentUser, siteConfig } = storeToRefs(store);
const {
  createMyApiKey,
  deleteMyApiKey,
  uploadMyAvatar,
  deleteMyAvatar,
  loadCurrentUser,
  loadMyApiKeys,
  loginWithOAuth,
  requestEmailChange,
  unbindMyOAuthIdentity,
  updateMyPassword,
  updateProfile,
  verifyEmailChange,
} = store;

const loading = shallowRef(true);
const savingProfile = shallowRef(false);
const savingPassword = shallowRef(false);
const savingEmail = shallowRef(false);
const oauthBusyId = shallowRef("");
const savingNotifications = shallowRef(false);
const loadingAccessKeys = shallowRef(false);
const creatingAccessKey = shallowRef(false);
const avatarBusy = shallowRef(false);
const accessKeys = shallowRef<ApiKey[]>([]);
const newAccessKey = shallowRef("");
const accessKeyBusyIds = reactive<Record<string, string>>({});
const formData = reactive({
  github_name: "",
  github_token: "",
  github_refresh_interval_seconds: 3600,
  notification_email: "",
  notify_plugin_review: true,
  notify_comments: true,
  notify_replies: true,
  notify_likes: true,
  notify_unlist: true,
  email_notify_plugin_review: true,
  email_notify_pending_review: true,
  email_notify_comments: false,
  email_notify_replies: false,
  email_notify_likes: false,
  email_notify_unlist: true,
});

const isAdminUser = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);
const isDeveloper = computed(() => {
  if (typeof currentUser.value?.is_developer === "boolean") return currentUser.value.is_developer;
  return Boolean(currentUser.value?.github_login || isAdminUser.value);
});
const fallbackNotificationEmail = computed(() => {
  if (currentUser.value?.email_verified && currentUser.value?.email) {
    return currentUser.value.email;
  }
  return currentUser.value?.github_email || "";
});

function applyCurrentUser() {
  formData.github_name = currentUser.value?.github_name || "";
  formData.github_token = "";
  formData.github_refresh_interval_seconds = Number(
    currentUser.value?.github_refresh_interval_seconds || 3600,
  );
  formData.notification_email = currentUser.value?.notification_email || "";
  formData.notify_plugin_review = currentUser.value?.notify_plugin_review !== false;
  formData.notify_comments = currentUser.value?.notify_comments !== false;
  formData.notify_replies = currentUser.value?.notify_replies !== false;
  formData.notify_likes = currentUser.value?.notify_likes !== false;
  formData.notify_unlist = currentUser.value?.notify_unlist !== false;
  formData.email_notify_plugin_review = currentUser.value?.email_notify_plugin_review !== false;
  formData.email_notify_pending_review = currentUser.value?.email_notify_pending_review !== false;
  formData.email_notify_comments = currentUser.value?.email_notify_comments === true;
  formData.email_notify_replies = currentUser.value?.email_notify_replies === true;
  formData.email_notify_likes = currentUser.value?.email_notify_likes === true;
  formData.email_notify_unlist = currentUser.value?.email_notify_unlist !== false;
}

async function handleUploadAvatar(payload: { file: File; onCompleted?: () => void }) {
  avatarBusy.value = true;
  try {
    await uploadMyAvatar(payload.file);
    payload.onCompleted?.();
    message.success("头像已更新");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "上传头像失败");
  } finally {
    avatarBusy.value = false;
  }
}

async function handleRemoveAvatar() {
  avatarBusy.value = true;
  try {
    await deleteMyAvatar();
    message.success("已清除上传的头像");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "清除头像失败");
  } finally {
    avatarBusy.value = false;
  }
}

async function saveProfile() {
  savingProfile.value = true;
  try {
    const payload: Record<string, unknown> = {
      github_name: formData.github_name.trim(),
      github_refresh_interval_seconds: Number(formData.github_refresh_interval_seconds || 3600),
    };
    if (formData.github_token.trim()) {
      payload.github_token = formData.github_token.trim();
    }
    await updateProfile(payload);
    applyCurrentUser();
    message.success("个人资料已保存");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    savingProfile.value = false;
  }
}

async function saveNotificationPreferences() {
  savingNotifications.value = true;
  try {
    await updateProfile({
      notification_email: formData.notification_email.trim(),
      notify_plugin_review: formData.notify_plugin_review,
      notify_comments: formData.notify_comments,
      notify_replies: formData.notify_replies,
      notify_likes: formData.notify_likes,
      notify_unlist: formData.notify_unlist,
      email_notify_plugin_review: formData.email_notify_plugin_review,
      email_notify_pending_review: formData.email_notify_pending_review,
      email_notify_comments: formData.email_notify_comments,
      email_notify_replies: formData.email_notify_replies,
      email_notify_likes: formData.email_notify_likes,
      email_notify_unlist: formData.email_notify_unlist,
    });
    applyCurrentUser();
    message.success("通知设置已保存");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    savingNotifications.value = false;
  }
}

async function refreshAccessKeys() {
  loadingAccessKeys.value = true;
  try {
    accessKeys.value = await loadMyApiKeys();
  } catch (error) {
    message.error(error instanceof Error ? error.message : "访问密钥加载失败");
  } finally {
    loadingAccessKeys.value = false;
  }
}

async function createAccessKey(payload: Record<string, unknown>) {
  creatingAccessKey.value = true;
  newAccessKey.value = "";
  try {
    const apiKey = (await createMyApiKey(payload)) as ApiKey;
    newAccessKey.value = apiKey.key || "";
    accessKeys.value = [apiKey, ...accessKeys.value.filter((item) => item.id !== apiKey.id)];
    message.success("访问密钥已生成");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "生成访问密钥失败");
  } finally {
    creatingAccessKey.value = false;
  }
}

function deleteAccessKey(apiKey: ApiKey) {
  dialog.warning({
    title: "删除访问密钥",
    content: `${apiKey.name || "这个访问密钥"} 删除后，正在使用它的插件或脚本会立刻失效。`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      const keyId = String(apiKey.id);
      accessKeyBusyIds[keyId] = "delete";
      try {
        await deleteMyApiKey(apiKey.id);
        accessKeys.value = accessKeys.value.filter((item) => item.id !== apiKey.id);
        message.success("访问密钥已删除");
      } catch (error) {
        message.error(error instanceof Error ? error.message : "删除访问密钥失败");
      } finally {
        delete accessKeyBusyIds[keyId];
      }
    },
  });
}

async function copyAccessKey(key: string) {
  try {
    await navigator.clipboard.writeText(key);
    message.success("访问密钥已复制");
  } catch {
    message.error("复制失败，请手动复制");
  }
}

async function handleUpdatePassword(payload: {
  current_password?: string;
  new_password: string;
  email?: string;
}) {
  savingPassword.value = true;
  try {
    await updateMyPassword(payload);
    message.success("登录密码已更新");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "更新密码失败");
  } finally {
    savingPassword.value = false;
  }
}

async function handleRequestEmailChange(payload: {
  current_password: string;
  new_email: string;
  onPending?: (changeId: string) => void;
  onCompleted?: () => void;
}) {
  savingEmail.value = true;
  try {
    const result = await requestEmailChange({
      current_password: payload.current_password,
      new_email: payload.new_email,
    });
    if (result.completed || result.verification_required === false) {
      message.success("登录邮箱已更新");
      payload.onCompleted?.();
    } else if (result.change_id) {
      message.success("验证码已发送至新邮箱，请查收");
      payload.onPending?.(result.change_id);
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : "修改邮箱失败");
  } finally {
    savingEmail.value = false;
  }
}

async function handleVerifyEmailChange(payload: {
  change_id: string;
  code: string;
  onCompleted?: () => void;
}) {
  savingEmail.value = true;
  try {
    await verifyEmailChange({ change_id: payload.change_id, code: payload.code });
    message.success("登录邮箱已验证并更新");
    payload.onCompleted?.();
  } catch (error) {
    message.error(error instanceof Error ? error.message : "验证新邮箱失败");
  } finally {
    savingEmail.value = false;
  }
}

function handleConnectOAuth(provider: OAuthProviderKey) {
  loginWithOAuth(provider, "/settings/personal");
}

async function handleUnbindOAuth(identity: OAuthIdentity) {
  oauthBusyId.value = identity.id;
  try {
    await unbindMyOAuthIdentity(identity.id);
    message.success("OAuth 账号已解绑");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "解绑失败");
  } finally {
    oauthBusyId.value = "";
  }
}

function goBack() {
  router.back();
}

onMounted(async () => {
  await loadCurrentUser();
  if (!currentUser.value) {
    message.warning("请先登录");
    router.replace("/");
    return;
  }
  applyCurrentUser();
  await refreshAccessKeys();
  loading.value = false;
});
</script>

<template>
  <div class="profile-page">
    <NLayoutHeader class="profile-header">
      <div class="header-content">
        <div class="header-left">
          <NButton quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <NIcon><ArrowBack /></NIcon>
            </template>
          </NButton>
          <div class="header-copy">
            <p class="eyebrow">个人设置</p>
            <h1 class="page-title">账号设置</h1>
          </div>
        </div>
      </div>
    </NLayoutHeader>

    <main class="profile-content">
      <NSpin :show="loading">
        <NTabs type="line" animated class="profile-tabs">
          <NTabPane name="account" tab="账号资料" display-directive="show">
            <div class="profile-tab-content">
              <ProfileAccountSection
                v-model:github-name="formData.github_name"
                v-model:github-token="formData.github_token"
                v-model:refresh-interval="formData.github_refresh_interval_seconds"
                :current-user="currentUser"
                :auth-config="siteConfig.auth"
                :saving="savingProfile"
                :password-saving="savingPassword"
                :email-saving="savingEmail"
                :oauth-busy-id="oauthBusyId"
                :avatar-busy="avatarBusy"
                @upload-avatar="handleUploadAvatar"
                @remove-avatar="handleRemoveAvatar"
                @save="saveProfile"
                @connect-oauth="handleConnectOAuth"
                @unbind-oauth="handleUnbindOAuth"
                @update-password="handleUpdatePassword"
                @request-email-change="handleRequestEmailChange"
                @verify-email-change="handleVerifyEmailChange"
              />
            </div>
          </NTabPane>

          <NTabPane name="notifications" tab="通知偏好" display-directive="show">
            <div class="profile-tab-content">
              <NotificationPreferencesSection
                v-model:notification-email="formData.notification_email"
                v-model:notify-plugin-review="formData.notify_plugin_review"
                v-model:notify-comments="formData.notify_comments"
                v-model:notify-replies="formData.notify_replies"
                v-model:notify-likes="formData.notify_likes"
                v-model:notify-unlist="formData.notify_unlist"
                v-model:email-notify-plugin-review="formData.email_notify_plugin_review"
                v-model:email-notify-pending-review="formData.email_notify_pending_review"
                v-model:email-notify-comments="formData.email_notify_comments"
                v-model:email-notify-replies="formData.email_notify_replies"
                v-model:email-notify-likes="formData.email_notify_likes"
                v-model:email-notify-unlist="formData.email_notify_unlist"
                :fallback-email="fallbackNotificationEmail"
                :show-pending-review-email="isAdminUser"
                :saving="savingNotifications"
                @save="saveNotificationPreferences"
              />
            </div>
          </NTabPane>

          <NTabPane name="access-keys" tab="访问密钥" display-directive="show">
            <div class="profile-tab-content">
              <AccessKeyManager
                :keys="accessKeys"
                :loading="loadingAccessKeys"
                :creating="creatingAccessKey"
                :busy-ids="accessKeyBusyIds"
                :new-key="newAccessKey"
                :can-create-write-key="isDeveloper"
                @refresh="refreshAccessKeys"
                @create="createAccessKey"
                @delete="deleteAccessKey"
                @copy-key="copyAccessKey"
                @clear-new-key="newAccessKey = ''"
              />
            </div>
          </NTabPane>
        </NTabs>
      </NSpin>
    </main>
  </div>
</template>

<style scoped>
.profile-page {
  min-height: 100vh;
  background: var(--bg-base, var(--body-color));
  color: var(--text-primary, var(--text-color-base));
}

.profile-header {
  position: sticky;
  top: 0;
  z-index: 20;
  border-bottom: 1px solid var(--border-base, var(--border-color));
  background: var(--bg-header, var(--card-color));
  backdrop-filter: blur(18px);
  box-shadow: var(--shadow-sm);
}

.header-content {
  max-width: 1080px;
  margin: 0 auto;
  padding: 20px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 14px;
}

.header-copy {
  min-width: 0;
}

.eyebrow,
.page-title {
  margin: 0;
}

.eyebrow {
  color: var(--primary-color);
  font-size: 12px;
  font-weight: 700;
}

.page-title {
  color: var(--text-primary, var(--text-color-base));
  font-size: 24px;
  line-height: 1.25;
}

.profile-content {
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px 20px 48px;
}

.profile-tabs :deep(.n-tabs-nav) {
  margin-bottom: 18px;
  padding: 0 12px;
  background: var(--bg-card, var(--card-color));
  border: 1px solid var(--border-base, var(--border-color));
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

.profile-tabs :deep(.n-tabs-tab) {
  min-height: 46px;
}

.profile-tabs :deep(.n-tabs-pane-wrapper),
.profile-tabs :deep(.n-tab-pane) {
  min-width: 0;
}

.profile-tab-content {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 18px;
  min-width: 0;
}

@media (max-width: 640px) {
  .profile-content {
    padding: 20px 14px 38px;
  }
}
</style>
