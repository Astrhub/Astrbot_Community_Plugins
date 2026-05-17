<template>
  <div class="profile-page">
    <n-layout-header class="profile-header">
      <div class="header-content">
        <div class="header-left">
          <n-button quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <p class="eyebrow">个人设置</p>
            <h1>个人资料</h1>
          </div>
        </div>
      </div>
    </n-layout-header>

    <main class="profile-content">
      <n-spin :show="loading">
        <n-form :model="formData" label-placement="top" class="profile-form">
          <section class="profile-section">
            <div class="section-title">
              <h2>基本资料</h2>
              <p>资料会显示在评论、插件提交和管理记录中。</p>
            </div>
            <div class="avatar-row">
              <n-avatar :size="72" :src="formData.avatar_url || undefined">
                {{ avatarFallback }}
              </n-avatar>
              <n-form-item label="头像 URL" path="avatar_url">
                <n-input v-model:value="formData.avatar_url" placeholder="https://..." />
              </n-form-item>
            </div>
            <n-form-item label="显示名称" path="github_name">
              <n-input v-model:value="formData.github_name" placeholder="你的显示名称" />
            </n-form-item>
            <div class="actions">
              <n-button type="primary" :loading="saving" @click="saveProfile">
                保存资料
              </n-button>
            </div>
          </section>

          <section class="profile-section">
            <div class="section-title">
              <h2>GitHub 绑定</h2>
              <p>绑定后可以用 GitHub 登录，并用仓库所有权编辑自己的插件。</p>
            </div>
            <div class="github-state" :class="{ linked: Boolean(currentUser?.github_login) }">
              <n-icon>
                <checkmark-circle-outline v-if="currentUser?.github_login" />
                <close-circle-outline v-else />
              </n-icon>
              <span v-if="currentUser?.github_login">
                已绑定 <strong>{{ currentUser.github_login }}</strong>
              </span>
              <span v-else>未绑定 GitHub 账号</span>
            </div>
            <n-button type="primary" tertiary @click="bindGithub">
              <template #icon>
                <n-icon><logo-github /></n-icon>
              </template>
              {{ currentUser?.github_login ? '重新绑定 GitHub' : '绑定 GitHub' }}
            </n-button>
          </section>

          <section class="profile-section">
            <div class="section-title">
              <h2>消息</h2>
              <p>插件审核、下架等站内通知会显示在这里。</p>
            </div>
            <n-empty v-if="notifications.length === 0" description="暂无消息" />
            <div v-else class="notification-list">
              <article
                v-for="notification in notifications"
                :key="notification.id"
                class="notification-item"
              >
                <div class="notification-title">
                  <strong>{{ notification.title }}</strong>
                  <time>{{ formatTime(notification.created_at) }}</time>
                </div>
                <p>{{ notification.body }}</p>
              </article>
            </div>
          </section>
        </n-form>
      </n-spin>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import {
  NAvatar,
  NButton,
  NEmpty,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NLayoutHeader,
  NSpin,
  useMessage
} from 'naive-ui'
import {
  ArrowBack,
  CheckmarkCircleOutline,
  CloseCircleOutline,
  LogoGithub
} from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { currentUser } = storeToRefs(store)
const { loadCurrentUser, loadNotifications, loginWithGithub, updateProfile } = store

const loading = ref(true)
const saving = ref(false)
const notifications = ref([])
const formData = reactive({
  avatar_url: '',
  github_name: ''
})

const avatarFallback = computed(() => {
  const value = currentUser.value?.github_login || currentUser.value?.internal_username || '?'
  return value.slice(0, 1).toUpperCase()
})

function applyCurrentUser() {
  formData.avatar_url = currentUser.value?.avatar_url || ''
  formData.github_name = currentUser.value?.github_name || ''
}

async function saveProfile() {
  saving.value = true
  try {
    await updateProfile({
      avatar_url: formData.avatar_url.trim(),
      github_name: formData.github_name.trim()
    })
    applyCurrentUser()
    message.success('个人资料已保存')
  } catch (error) {
    message.error(error.message || '保存失败')
  } finally {
    saving.value = false
  }
}

function bindGithub() {
  if (!store.siteConfig.auth?.github_login_enabled) {
    message.warning('GitHub 登录尚未开启，请先由核心管理员配置 OAuth')
    return
  }
  loginWithGithub()
}

function goBack() {
  router.back()
}

function formatTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString()
}

onMounted(async () => {
  await loadCurrentUser()
  if (!currentUser.value) {
    message.warning('请先登录')
    router.replace('/')
    return
  }
  applyCurrentUser()
  try {
    notifications.value = await loadNotifications()
  } catch (error) {
    message.error(error.message || '消息加载失败')
  }
  loading.value = false
})
</script>

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
  max-width: 880px;
  margin: 0 auto;
  padding: 20px;
}

.header-left,
.avatar-row,
.actions,
.github-state,
.notification-title {
  display: flex;
  align-items: center;
  gap: 14px;
}

.eyebrow,
h1,
h2,
.section-title p {
  margin: 0;
}

.eyebrow {
  color: #0e74e4;
  font-size: 12px;
  font-weight: 700;
}

h1 {
  font-size: 24px;
}

h2 {
  font-size: 18px;
}

.profile-content {
  max-width: 760px;
  margin: 0 auto;
  padding: 24px 20px 48px;
}

.profile-form {
  display: grid;
  gap: 18px;
}

.profile-section {
  padding: 22px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--card-color);
}

.section-title {
  margin-bottom: 18px;
}

.section-title p {
  margin-top: 6px;
  color: var(--text-color-2);
}

.avatar-row {
  align-items: flex-end;
}

.avatar-row :deep(.n-form-item) {
  flex: 1;
}

.actions {
  justify-content: flex-end;
}

.github-state {
  margin-bottom: 14px;
  color: var(--text-color-2);
}

.github-state.linked {
  color: #18a058;
}

.notification-list {
  display: grid;
  gap: 12px;
}

.notification-item {
  padding: 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.notification-title {
  justify-content: space-between;
}

.notification-title time,
.notification-item p {
  color: var(--text-color-2);
}

.notification-item p {
  margin: 8px 0 0;
}

@media (max-width: 640px) {
  .avatar-row {
    align-items: stretch;
    flex-direction: column;
  }

  .actions {
    justify-content: stretch;
  }

  .actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
