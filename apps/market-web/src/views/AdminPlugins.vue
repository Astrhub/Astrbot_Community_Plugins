<template>
  <div class="admin-page">
    <n-layout-header class="admin-header">
      <div class="header-content">
        <div class="header-left">
          <n-button quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <p class="eyebrow">管理员</p>
            <h1>插件审核</h1>
          </div>
        </div>
        <n-button tertiary :loading="loading" @click="loadItems">刷新</n-button>
      </div>
    </n-layout-header>

    <main class="admin-content">
      <n-alert v-if="!isAdmin && !loading" type="warning" :bordered="false">
        只有管理员可以审核插件。
      </n-alert>

      <n-spin :show="loading">
        <div v-if="isAdmin" class="plugin-list">
          <n-empty v-if="items.length === 0" description="暂无插件提交" />
          <article v-for="plugin in items" :key="plugin.id" class="plugin-row">
            <div class="plugin-main">
              <div class="plugin-title">
                <h2>{{ plugin.display_name || plugin.name }}</h2>
                <n-tag :type="statusType(plugin.status)" :bordered="false">
                  {{ statusLabel(plugin.status) }}
                </n-tag>
              </div>
              <p class="plugin-name">{{ plugin.name }}</p>
              <p class="plugin-desc">{{ plugin.desc || '暂无描述' }}</p>
              <a :href="plugin.repo" target="_blank" rel="noreferrer" class="repo-link">
                {{ plugin.repo }}
              </a>
            </div>
            <div class="plugin-actions">
              <n-button
                type="primary"
                :disabled="plugin.status === 'listed'"
                :loading="busyId === plugin.id"
                @click="setListing(plugin, 'list')"
              >
                上架
              </n-button>
              <n-button
                secondary
                type="warning"
                :disabled="plugin.status === 'unlisted'"
                :loading="busyId === plugin.id"
                @click="setListing(plugin, 'unlist')"
              >
                下架
              </n-button>
            </div>
          </article>
        </div>
      </n-spin>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NEmpty,
  NIcon,
  NLayoutHeader,
  NSpin,
  NTag,
  useMessage
} from 'naive-ui'
import { ArrowBack } from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { currentUser } = storeToRefs(store)
const { loadAdminPlugins, loadCurrentUser, loadPlugins, updatePluginListing } = store

const loading = ref(true)
const busyId = ref('')
const items = ref([])
const isAdmin = computed(() => ['core_admin', 'admin'].includes(currentUser.value?.role))

const statusLabel = (status) => {
  if (status === 'listed') return '已上架'
  if (status === 'unlisted') return '已下架'
  return '待审核'
}

const statusType = (status) => {
  if (status === 'listed') return 'success'
  if (status === 'unlisted') return 'warning'
  return 'info'
}

async function loadItems() {
  loading.value = true
  try {
    await loadCurrentUser()
    if (!isAdmin.value) return
    items.value = await loadAdminPlugins()
  } catch (error) {
    message.error(error.message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function setListing(plugin, action) {
  busyId.value = plugin.id
  try {
    const updated = await updatePluginListing(plugin.id, action)
    items.value = items.value.map((item) => (item.id === updated.id ? updated : item))
    await loadPlugins()
    message.success(action === 'list' ? '插件已上架' : '插件已下架')
  } catch (error) {
    message.error(error.message || '操作失败')
  } finally {
    busyId.value = ''
  }
}

function goBack() {
  router.back()
}

onMounted(loadItems)
</script>

<style scoped>
.admin-page {
  min-height: 100vh;
  background: var(--bg-base);
}

.admin-header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: var(--bg-header);
  border-bottom: 1px solid var(--border-base);
  backdrop-filter: blur(18px);
  box-shadow: var(--shadow-sm);
}

.header-content {
  max-width: 1040px;
  margin: 0 auto;
  padding: 18px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.header-left,
.plugin-title,
.plugin-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.eyebrow,
h1,
h2,
p {
  margin: 0;
}

.eyebrow {
  margin-bottom: 4px;
  color: var(--primary-color);
  font-size: 12px;
  font-weight: 700;
}

h1 {
  color: var(--text-primary);
  font-size: 22px;
}

.admin-content {
  max-width: 1040px;
  margin: 0 auto;
  padding: 28px 20px 44px;
}

.plugin-list {
  display: grid;
  gap: 14px;
}

.plugin-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  padding: 18px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

.plugin-main {
  min-width: 0;
}

.plugin-title h2 {
  color: var(--text-primary);
  font-size: 18px;
}

.plugin-name,
.plugin-desc,
.repo-link {
  margin-top: 8px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.repo-link {
  display: inline-block;
  text-decoration: none;
}

.repo-link:hover {
  color: var(--primary-color);
}

@media (max-width: 720px) {
  .plugin-row {
    grid-template-columns: 1fr;
  }

  .plugin-actions {
    justify-content: flex-end;
  }
}
</style>
