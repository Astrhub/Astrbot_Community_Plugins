<template>
  <div class="notifications-page">
    <n-layout-header class="notifications-header">
      <div class="header-content">
        <div class="header-left">
          <n-button quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <p class="eyebrow">站内信</p>
            <h1>消息中心</h1>
          </div>
        </div>
        <n-button tertiary :loading="loading" @click="loadMessages">刷新</n-button>
      </div>
    </n-layout-header>

    <main class="notifications-content">
      <n-spin :show="loading">
        <n-empty v-if="notifications.length === 0" description="暂无消息" />
        <div v-else class="bubble-list">
          <article
            v-for="notification in notifications"
            :key="notification.id"
            class="bubble-message"
          >
            <div class="bubble-avatar">
              <n-icon><notifications-outline /></n-icon>
            </div>
            <div class="bubble-content">
              <div class="bubble-meta">
                <strong>{{ notification.title }}</strong>
                <time>{{ formatTime(notification.created_at) }}</time>
              </div>
              <p>{{ notification.body }}</p>
            </div>
          </article>
        </div>
      </n-spin>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import {
  NButton,
  NEmpty,
  NIcon,
  NLayoutHeader,
  NSpin,
  useMessage
} from 'naive-ui'
import { ArrowBack, NotificationsOutline } from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { currentUser } = storeToRefs(store)
const { loadCurrentUser, loadNotifications } = store

const loading = ref(true)
const notifications = ref([])

function goBack() {
  router.back()
}

function formatTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString()
}

async function loadMessages() {
  loading.value = true
  try {
    notifications.value = await loadNotifications()
  } catch (error) {
    message.error(error.message || '消息加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadCurrentUser()
  if (!currentUser.value) {
    message.warning('请先登录')
    router.replace('/')
    return
  }
  await loadMessages()
})
</script>

<style scoped>
.notifications-page {
  min-height: 100vh;
  background: var(--body-color);
  color: var(--text-color-base);
}

.notifications-header {
  border-bottom: 1px solid var(--border-color);
  background: var(--card-color);
}

.header-content {
  max-width: 880px;
  margin: 0 auto;
  padding: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 14px;
}

.eyebrow,
h1,
p {
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

.notifications-content {
  max-width: 760px;
  margin: 0 auto;
  padding: 24px 20px 48px;
}

.bubble-list {
  display: grid;
  gap: 16px;
}

.bubble-message {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr);
  gap: 12px;
  align-items: flex-start;
}

.bubble-avatar {
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  color: #0e74e4;
  background: rgba(14, 116, 228, 0.12);
  border: 1px solid rgba(14, 116, 228, 0.22);
  border-radius: 50%;
}

.bubble-content {
  position: relative;
  padding: 14px 16px;
  background: var(--card-color);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

.bubble-content::before {
  content: "";
  position: absolute;
  top: 14px;
  left: -7px;
  width: 12px;
  height: 12px;
  background: var(--card-color);
  border-left: 1px solid var(--border-color);
  border-bottom: 1px solid var(--border-color);
  transform: rotate(45deg);
}

.bubble-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--text-color-base);
}

.bubble-meta time {
  flex: none;
  color: var(--text-color-3);
  font-size: 12px;
}

.bubble-content p {
  margin-top: 8px;
  color: var(--text-color-2);
  line-height: 1.7;
  white-space: pre-wrap;
}

@media (max-width: 640px) {
  .header-content {
    align-items: flex-start;
    flex-direction: column;
  }

  .header-content :deep(.n-button) {
    align-self: flex-start;
  }

  .notifications-content {
    padding: 20px 14px 38px;
  }

  .bubble-message {
    grid-template-columns: 32px minmax(0, 1fr);
  }

  .bubble-avatar {
    width: 32px;
    height: 32px;
  }

  .bubble-meta {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
