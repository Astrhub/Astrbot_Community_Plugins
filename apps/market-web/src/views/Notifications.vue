<script setup>
import { computed, onMounted, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import {
  NButton,
  NCheckbox,
  NEmpty,
  NIcon,
  NLayoutHeader,
  NPagination,
  NSpin,
  NTag,
  useDialog,
  useMessage
} from 'naive-ui'
import {
  ArrowBack,
  NotificationsOutline,
  RefreshOutline,
  TrashOutline
} from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const store = usePluginStore()
const { currentUser, unreadNotificationCount } = storeToRefs(store)
const {
  clearNotifications,
  deleteNotification,
  deleteNotifications,
  loadCurrentUser,
  loadNotifications,
  markNotificationsRead
} = store

const loading = shallowRef(true)
const deleting = shallowRef(false)
const notifications = shallowRef([])
const selectedIds = shallowRef([])
const page = shallowRef(1)
const pageSize = shallowRef(20)
const total = shallowRef(0)

const selectedCount = computed(() => selectedIds.value.length)
const selectedIdSet = computed(() => new Set(selectedIds.value))
const currentPageIds = computed(() => notifications.value.map((notification) => notification.id))
const currentPageSelectedCount = computed(() =>
  currentPageIds.value.filter((id) => selectedIdSet.value.has(id)).length
)
const allCurrentSelected = computed(() =>
  currentPageIds.value.length > 0 &&
  currentPageSelectedCount.value === currentPageIds.value.length
)
const pageRangeText = computed(() => {
  if (total.value === 0) return '0 条'
  const start = (page.value - 1) * pageSize.value + 1
  const end = Math.min(total.value, page.value * pageSize.value)
  return `${start}-${end} / ${total.value} 条`
})

function goBack() {
  router.back()
}

function formatTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString()
}

function isSelected(notificationId) {
  return selectedIdSet.value.has(notificationId)
}

function setNotificationSelected(notificationId, checked) {
  if (checked) {
    selectedIds.value = Array.from(new Set([...selectedIds.value, notificationId]))
    return
  }
  selectedIds.value = selectedIds.value.filter((id) => id !== notificationId)
}

function setCurrentPageSelected(checked) {
  if (checked) {
    selectedIds.value = Array.from(new Set([...selectedIds.value, ...currentPageIds.value]))
    return
  }
  selectedIds.value = selectedIds.value.filter((id) => !currentPageIds.value.includes(id))
}

function clearSelection() {
  selectedIds.value = []
}

async function loadMessages() {
  loading.value = true
  try {
    const result = await loadNotifications({
      page: page.value,
      pageSize: pageSize.value
    })
    notifications.value = result.items || []
    total.value = Number(result.total || 0)
    pageSize.value = Number(result.page_size || pageSize.value)
    selectedIds.value = selectedIds.value.filter((id) => currentPageIds.value.includes(id))
    if (unreadNotificationCount.value > 0) {
      await markNotificationsRead()
      notifications.value = notifications.value.map((notification) => ({
        ...notification,
        read: true
      }))
    }
  } catch (error) {
    message.error(error.message || '消息加载失败')
  } finally {
    loading.value = false
  }
}

async function reloadAfterDeleting(deletedCount) {
  const nextTotal = Math.max(0, total.value - deletedCount)
  const maxPage = Math.max(1, Math.ceil(nextTotal / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
  clearSelection()
  await loadMessages()
}

function confirmDeleteNotification(notification) {
  dialog.warning({
    title: '删除消息',
    content: notification.title || '确认删除这条站内信？',
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      deleting.value = true
      try {
        const deleted = await deleteNotification(notification.id)
        await reloadAfterDeleting(deleted)
        message.success('消息已删除')
      } catch (error) {
        message.error(error.message || '删除失败')
      } finally {
        deleting.value = false
      }
    }
  })
}

function confirmDeleteSelected() {
  if (selectedCount.value === 0) return
  dialog.warning({
    title: '批量删除',
    content: `确认删除选中的 ${selectedCount.value} 条站内信？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      deleting.value = true
      try {
        const deleted = await deleteNotifications(selectedIds.value)
        await reloadAfterDeleting(deleted)
        message.success(`已删除 ${deleted} 条消息`)
      } catch (error) {
        message.error(error.message || '批量删除失败')
      } finally {
        deleting.value = false
      }
    }
  })
}

function confirmClearAll() {
  if (total.value === 0) return
  dialog.warning({
    title: '清空站内信',
    content: `确认清空全部 ${total.value} 条站内信？此操作不可撤销。`,
    positiveText: '清空',
    negativeText: '取消',
    onPositiveClick: async () => {
      deleting.value = true
      try {
        const deleted = await clearNotifications()
        page.value = 1
        clearSelection()
        await loadMessages()
        message.success(`已清空 ${deleted} 条消息`)
      } catch (error) {
        message.error(error.message || '清空失败')
      } finally {
        deleting.value = false
      }
    }
  })
}

async function changePage(nextPage) {
  page.value = nextPage
  clearSelection()
  await loadMessages()
}

async function changePageSize(nextPageSize) {
  pageSize.value = nextPageSize
  page.value = 1
  clearSelection()
  await loadMessages()
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

<template>
  <div class="notifications-page">
    <NLayoutHeader class="notifications-header">
      <div class="header-content">
        <div class="header-left">
          <NButton quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <NIcon><ArrowBack /></NIcon>
            </template>
          </NButton>
          <div class="header-copy">
            <p class="eyebrow">站内信</p>
            <h1 class="page-title">消息中心</h1>
          </div>
        </div>
        <NButton tertiary :loading="loading" @click="loadMessages">
          <template #icon>
            <NIcon><RefreshOutline /></NIcon>
          </template>
          刷新
        </NButton>
      </div>
    </NLayoutHeader>

    <main class="notifications-content">
      <div class="toolbar" v-if="total > 0">
        <NCheckbox
          :checked="allCurrentSelected"
          :indeterminate="currentPageSelectedCount > 0 && !allCurrentSelected"
          @update:checked="setCurrentPageSelected"
        >
          本页全选
        </NCheckbox>
        <span class="selection-state">已选 {{ selectedCount }} 条</span>
        <div class="toolbar-actions">
          <NButton
            secondary
            :disabled="selectedCount === 0"
            :loading="deleting"
            @click="confirmDeleteSelected"
          >
            <template #icon>
              <NIcon><TrashOutline /></NIcon>
            </template>
            删除所选
          </NButton>
          <NButton secondary type="error" :loading="deleting" @click="confirmClearAll">
            <template #icon>
              <NIcon><TrashOutline /></NIcon>
            </template>
            清空
          </NButton>
        </div>
      </div>

      <NSpin :show="loading">
        <NEmpty v-if="notifications.length === 0" description="暂无消息" />
        <div v-else class="bubble-list">
          <article
            v-for="notification in notifications"
            :key="notification.id"
            class="bubble-message"
          >
            <NCheckbox
              class="message-checkbox"
              :checked="isSelected(notification.id)"
              @update:checked="(checked) => setNotificationSelected(notification.id, checked)"
            />
            <div class="bubble-avatar">
              <NIcon><NotificationsOutline /></NIcon>
            </div>
            <div class="bubble-content">
              <div class="bubble-meta">
                <div class="title-row">
                  <strong>{{ notification.title }}</strong>
                  <NTag v-if="!notification.read" type="info" size="small" round>未读</NTag>
                </div>
                <time>{{ formatTime(notification.created_at) }}</time>
              </div>
              <p>{{ notification.body }}</p>
              <div class="message-actions">
                <NButton
                  quaternary
                  size="small"
                  :loading="deleting"
                  @click="confirmDeleteNotification(notification)"
                >
                  <template #icon>
                    <NIcon><TrashOutline /></NIcon>
                  </template>
                  删除
                </NButton>
              </div>
            </div>
          </article>
        </div>
      </NSpin>

      <div v-if="total > 0" class="pagination-bar">
        <span>{{ pageRangeText }}</span>
        <NPagination
          :page="page"
          :page-size="pageSize"
          :item-count="total"
          :page-sizes="[10, 20, 50]"
          show-size-picker
          @update:page="changePage"
          @update:page-size="changePageSize"
        />
      </div>
    </main>
  </div>
</template>

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
  max-width: 920px;
  margin: 0 auto;
  padding: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.header-left,
.toolbar,
.toolbar-actions,
.bubble-message,
.bubble-meta,
.title-row,
.message-actions,
.pagination-bar {
  display: flex;
  align-items: center;
}

.header-left {
  min-width: 0;
  gap: 14px;
}

.header-copy {
  min-width: 0;
}

.eyebrow,
.page-title,
.bubble-content p {
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

.notifications-content {
  max-width: 920px;
  margin: 0 auto;
  padding: 24px 20px 48px;
}

.toolbar {
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  margin-bottom: 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--card-color);
}

.selection-state {
  flex: 1;
  color: var(--text-color-3);
  font-size: 13px;
}

.toolbar-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.bubble-list {
  display: grid;
  gap: 14px;
}

.bubble-message {
  display: grid;
  grid-template-columns: 24px 38px minmax(0, 1fr);
  gap: 12px;
  align-items: flex-start;
}

.message-checkbox {
  margin-top: 8px;
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
  min-width: 0;
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
  justify-content: space-between;
  gap: 12px;
  color: var(--text-color-base);
}

.title-row {
  min-width: 0;
  gap: 8px;
}

.title-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
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

.message-actions {
  justify-content: flex-end;
  margin-top: 8px;
}

.pagination-bar {
  justify-content: space-between;
  gap: 14px;
  margin-top: 18px;
  color: var(--text-color-3);
  font-size: 13px;
}

@media (max-width: 720px) {
  .header-content,
  .toolbar,
  .pagination-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .toolbar-actions {
    width: 100%;
    justify-content: stretch;
  }

  .toolbar-actions :deep(.n-button) {
    flex: 1 1 120px;
  }

  .pagination-bar :deep(.n-pagination) {
    width: 100%;
  }
}

@media (max-width: 640px) {
  .notifications-content {
    padding: 20px 14px 38px;
  }

  .bubble-message {
    grid-template-columns: 22px 32px minmax(0, 1fr);
    gap: 9px;
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
