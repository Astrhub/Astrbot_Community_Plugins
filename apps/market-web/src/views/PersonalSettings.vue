<script setup>
import { computed, onMounted, reactive, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import {
  NButton,
  NIcon,
  NLayoutHeader,
  NSpin,
  useDialog,
  useMessage
} from 'naive-ui'
import { ArrowBack } from '@vicons/ionicons5'
import NotificationPreferencesSection from '@/components/settings/NotificationPreferencesSection.vue'
import PersonalPluginManager from '@/components/settings/PersonalPluginManager.vue'
import ProfileAccountSection from '@/components/settings/ProfileAccountSection.vue'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const store = usePluginStore()
const { currentUser, siteConfig } = storeToRefs(store)
const {
  loadCurrentUser,
  loadMyPlugins,
  requestPluginListing,
  setSearchQuery,
  unlistOwnPlugin,
  updatePluginMetadata,
  updateProfile
} = store

const loading = shallowRef(true)
const savingProfile = shallowRef(false)
const savingNotifications = shallowRef(false)
const loadingPlugins = shallowRef(false)
const myPlugins = shallowRef([])
const pluginBusyIds = reactive({})
const formData = reactive({
  github_name: '',
  github_token: '',
  github_refresh_interval_seconds: 3600,
  notify_replies: true,
  notify_likes: true
})

const maxPluginTags = computed(() => Number(siteConfig.value.market?.max_plugin_tags || 8))

function applyCurrentUser() {
  formData.github_name = currentUser.value?.github_name || ''
  formData.github_token = ''
  formData.github_refresh_interval_seconds =
    currentUser.value?.github_refresh_interval_seconds || 3600
  formData.notify_replies = currentUser.value?.notify_replies !== false
  formData.notify_likes = currentUser.value?.notify_likes !== false
}

async function saveProfile() {
  savingProfile.value = true
  try {
    const payload = {
      github_name: formData.github_name.trim(),
      github_refresh_interval_seconds: Number(formData.github_refresh_interval_seconds || 3600)
    }
    if (formData.github_token.trim()) {
      payload.github_token = formData.github_token.trim()
    }
    await updateProfile(payload)
    applyCurrentUser()
    message.success('个人资料已保存')
  } catch (error) {
    message.error(error.message || '保存失败')
  } finally {
    savingProfile.value = false
  }
}

async function saveNotificationPreferences() {
  savingNotifications.value = true
  try {
    await updateProfile({
      notify_replies: formData.notify_replies,
      notify_likes: formData.notify_likes
    })
    applyCurrentUser()
    message.success('通知设置已保存')
  } catch (error) {
    message.error(error.message || '保存失败')
  } finally {
    savingNotifications.value = false
  }
}

async function refreshMyPlugins() {
  loadingPlugins.value = true
  try {
    myPlugins.value = await loadMyPlugins()
  } catch (error) {
    message.error(error.message || '插件加载失败')
  } finally {
    loadingPlugins.value = false
  }
}

function replacePlugin(updatedPlugin) {
  myPlugins.value = myPlugins.value.map((plugin) =>
    plugin.id === updatedPlugin.id ? { ...plugin, ...updatedPlugin } : plugin
  )
}

async function withPluginBusy(plugin, action, task) {
  pluginBusyIds[plugin.id] = action
  try {
    const updated = await task()
    replacePlugin(updated)
    return updated
  } finally {
    delete pluginBusyIds[plugin.id]
  }
}

async function savePluginTags({ plugin, tags }) {
  try {
    await withPluginBusy(plugin, 'tags', () => updatePluginMetadata(plugin.id, { tags }))
    message.success('标签已保存')
  } catch (error) {
    message.error(error.message || '保存标签失败')
  }
}

function unlistPlugin(plugin) {
  dialog.warning({
    title: '下架插件',
    content: `${plugin.display_name || plugin.name} 下架后将从公开市场隐藏。`,
    positiveText: '下架',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await withPluginBusy(plugin, 'unlist', () =>
          unlistOwnPlugin(plugin.id, { reason: '作者主动下架' })
        )
        message.success('插件已下架')
      } catch (error) {
        message.error(error.message || '下架失败')
      }
    }
  })
}

async function requestListPlugin(plugin) {
  try {
    await withPluginBusy(plugin, 'request', () => requestPluginListing(plugin.id))
    message.success('已提交上架申请')
  } catch (error) {
    message.error(error.message || '申请上架失败')
  }
}

function openPlugin(plugin) {
  setSearchQuery(plugin.name || plugin.id)
  router.push('/')
}

function goBack() {
  router.back()
}

onMounted(async () => {
  await loadCurrentUser()
  if (!currentUser.value) {
    message.warning('请先登录')
    router.replace('/')
    return
  }
  applyCurrentUser()
  await refreshMyPlugins()
  loading.value = false
})
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
        <div class="settings-grid">
          <ProfileAccountSection
            v-model:github-name="formData.github_name"
            v-model:github-token="formData.github_token"
            v-model:refresh-interval="formData.github_refresh_interval_seconds"
            :current-user="currentUser"
            :saving="savingProfile"
            @save="saveProfile"
          />

          <NotificationPreferencesSection
            v-model:notify-replies="formData.notify_replies"
            v-model:notify-likes="formData.notify_likes"
            :saving="savingNotifications"
            @save="saveNotificationPreferences"
          />

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
      </NSpin>
    </main>
  </div>
</template>

<style scoped>
.profile-page {
  min-height: 100vh;
  background: var(--body-color);
  color: var(--text-color-base);
}

.profile-header {
  border-bottom: 1px solid var(--border-color);
  background: var(--card-color);
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
  color: #0e74e4;
  font-size: 12px;
  font-weight: 700;
}

.page-title {
  font-size: 24px;
  line-height: 1.25;
}

.profile-content {
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px 20px 48px;
}

.settings-grid {
  display: grid;
  gap: 18px;
}

@media (max-width: 640px) {
  .profile-content {
    padding: 20px 14px 38px;
  }
}
</style>
