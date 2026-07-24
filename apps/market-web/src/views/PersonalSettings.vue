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
import PersonalPluginManager from "@/components/settings/PersonalPluginManager.vue";
import ProfileAccountSection from "@/components/settings/ProfileAccountSection.vue";
import { usePluginStore } from "@/stores/plugins";

const router = useRouter();
const message = useMessage();
const dialog = useDialog();
const store = usePluginStore();
const { currentUser, siteConfig } = storeToRefs(store);
const {
  createMyApiKey,
  deleteMyApiKey,
  loadCurrentUser,
  loadMyApiKeys,
  loadMyPlugins,
  requestPluginListing,
  setSearchQuery,
  unlistOwnPlugin,
  updatePluginMetadata,
  updateProfile,
} = store;

const loading = shallowRef(true);
const savingProfile = shallowRef(false);
const savingNotifications = shallowRef(false);
const loadingPlugins = shallowRef(false);
const loadingAccessKeys = shallowRef(false);
const creatingAccessKey = shallowRef(false);
const myPlugins = shallowRef([]);
const accessKeys = shallowRef([]);
const newAccessKey = shallowRef("");
const pluginBusyIds = reactive({});
const accessKeyBusyIds = reactive({});
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

const maxPluginTags = computed(() => Number(siteConfig.value.market?.max_plugin_tags || 8));
const isAdminUser = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);

function applyCurrentUser() {
  formData.github_name = currentUser.value?.github_name || "";
  formData.github_token = "";
  formData.github_refresh_interval_seconds =
    currentUser.value?.github_refresh_interval_seconds || 3600;
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

async function saveProfile() {
  savingProfile.value = true;
  try {
    const payload = {
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
    message.error(error.message || "保存失败");
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
    message.error(error.message || "保存失败");
  } finally {
    savingNotifications.value = false;
  }
}

async function refreshMyPlugins() {
  loadingPlugins.value = true;
  try {
    myPlugins.value = await loadMyPlugins();
  } catch (error) {
    message.error(error.message || "插件加载失败");
  } finally {
    loadingPlugins.value = false;
  }
}

async function refreshAccessKeys() {
  loadingAccessKeys.value = true;
  try {
    accessKeys.value = await loadMyApiKeys();
  } catch (error) {
    message.error(error.message || "访问密钥加载失败");
  } finally {
    loadingAccessKeys.value = false;
  }
}

async function createAccessKey(payload) {
  creatingAccessKey.value = true;
  newAccessKey.value = "";
  try {
    const apiKey = await createMyApiKey(payload);
    newAccessKey.value = apiKey.key || "";
    accessKeys.value = [apiKey, ...accessKeys.value.filter((item) => item.id !== apiKey.id)];
    message.success("访问密钥已生成");
  } catch (error) {
    message.error(error.message || "生成访问密钥失败");
  } finally {
    creatingAccessKey.value = false;
  }
}

function deleteAccessKey(apiKey) {
  dialog.warning({
    title: "删除访问密钥",
    content: `${apiKey.name || "这个访问密钥"} 删除后，正在使用它的插件或脚本会立刻失效。`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      accessKeyBusyIds[apiKey.id] = "delete";
      try {
        await deleteMyApiKey(apiKey.id);
        accessKeys.value = accessKeys.value.filter((item) => item.id !== apiKey.id);
        message.success("访问密钥已删除");
      } catch (error) {
        message.error(error.message || "删除访问密钥失败");
      } finally {
        delete accessKeyBusyIds[apiKey.id];
      }
    },
  });
}

async function copyAccessKey(key) {
  try {
    await navigator.clipboard.writeText(key);
    message.success("访问密钥已复制");
  } catch {
    message.error("复制失败，请手动复制");
  }
}

function replacePlugin(updatedPlugin) {
  myPlugins.value = myPlugins.value.map((plugin) =>
    plugin.id === updatedPlugin.id ? { ...plugin, ...updatedPlugin } : plugin,
  );
}

async function withPluginBusy(plugin, action, task) {
  pluginBusyIds[plugin.id] = action;
  try {
    const updated = await task();
    replacePlugin(updated);
    return updated;
  } finally {
    delete pluginBusyIds[plugin.id];
  }
}

async function savePluginTags({ plugin, tags }) {
  try {
    await withPluginBusy(plugin, "tags", () => updatePluginMetadata(plugin.id, { tags }));
    message.success("标签已保存");
  } catch (error) {
    message.error(error.message || "保存标签失败");
  }
}

function unlistPlugin(plugin) {
  dialog.warning({
    title: "下架插件",
    content: `${plugin.display_name || plugin.name} 下架后将从公开市场隐藏。`,
    positiveText: "下架",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        await withPluginBusy(plugin, "unlist", () =>
          unlistOwnPlugin(plugin.id, { reason: "作者主动下架" }),
        );
        message.success("插件已下架");
      } catch (error) {
        message.error(error.message || "下架失败");
      }
    },
  });
}

async function requestListPlugin(plugin) {
  try {
    await withPluginBusy(plugin, "request", () => requestPluginListing(plugin.id));
    message.success("已提交上架申请");
  } catch (error) {
    message.error(error.message || "申请上架失败");
  }
}

function openPlugin(plugin) {
  setSearchQuery(plugin.name || plugin.id);
  router.push("/");
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
  await Promise.all([refreshMyPlugins(), refreshAccessKeys()]);
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
            <h1 class="page-title">账号与插件</h1>
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
                :saving="savingProfile"
                @save="saveProfile"
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
                :fallback-email="currentUser?.github_email || ''"
                :show-pending-review-email="isAdminUser"
                :saving="savingNotifications"
                @save="saveNotificationPreferences"
              />
            </div>
          </NTabPane>

          <NTabPane name="plugins" tab="我的插件" display-directive="show">
            <div class="profile-tab-content">
              <PersonalPluginManager
                :plugins="myPlugins"
                :loading="loadingPlugins"
                :busy-ids="pluginBusyIds"
                :max-tags="maxPluginTags"
                @refresh="refreshMyPlugins"
                @save-tags="savePluginTags"
                @request-list="requestListPlugin"
                @unlist="unlistPlugin"
                @open-plugin="openPlugin"
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

.profile-tab-content {
  display: grid;
  gap: 18px;
}

@media (max-width: 640px) {
  .profile-content {
    padding: 20px 14px 38px;
  }
}
</style>
