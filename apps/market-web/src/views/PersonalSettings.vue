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
              <h2>GitHub API Token</h2>
              <p>仅需要只读权限，用于读取公开仓库信息、metadata.yaml 和 logo.png；插件作者会优先使用自己的 Token 刷新元数据。</p>
            </div>
            <n-form-item label="Token" path="github_token">
              <n-input
                v-model:value="formData.github_token"
                type="password"
                show-password-on="click"
                :placeholder="currentUser?.has_github_token ? '已配置，留空保持不变' : 'ghp_... 或 fine-grained token'"
              />
            </n-form-item>
            <div class="token-state">
              {{ currentUser?.has_github_token ? '当前已配置 Token' : '当前未配置 Token' }}
            </div>
            <n-form-item label="自动刷新间隔（秒）" path="github_refresh_interval_seconds">
              <n-input-number
                v-model:value="formData.github_refresh_interval_seconds"
                :min="300"
                :max="86400"
                :step="300"
              />
            </n-form-item>
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
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import {
  NButton,
  NEmpty,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
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
  github_name: '',
  github_token: '',
  github_refresh_interval_seconds: 3600
})

function applyCurrentUser() {
  formData.github_name = currentUser.value?.github_name || ''
  formData.github_token = ''
  formData.github_refresh_interval_seconds = currentUser.value?.github_refresh_interval_seconds || 3600
}

async function saveProfile() {
  saving.value = true
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
.notification-item p,
.token-state {
  color: var(--text-color-2);
}

.notification-item p {
  margin: 8px 0 0;
}

@media (max-width: 640px) {
  .actions {
    justify-content: stretch;
  }

  .actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
