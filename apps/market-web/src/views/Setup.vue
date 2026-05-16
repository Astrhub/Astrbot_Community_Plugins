<template>
  <div class="setup-page">
    <n-layout class="setup-layout">
      <header class="setup-header">
        <div>
          <p class="eyebrow">首次配置</p>
          <h1>AstrBot Community Plugins</h1>
          <p class="subtitle">先填数据库和 Redis 连接，保存后重启服务再进入市场。</p>
        </div>
        <n-button quaternary circle @click="toggleTheme" :aria-label="isDarkMode ? '切换浅色模式' : '切换深色模式'">
          <template #icon>
            <n-icon>
              <sunny v-if="isDarkMode" />
              <moon v-else />
            </n-icon>
          </template>
        </n-button>
      </header>

      <main class="setup-main">
        <n-card :bordered="false" class="setup-card">
          <template #header>
            <div class="card-title">
              <h2>运行时配置</h2>
              <n-tag v-if="setupStatus.required" type="warning" :bordered="false">未完成</n-tag>
              <n-tag v-else type="success" :bordered="false">已完成</n-tag>
            </div>
          </template>

          <n-alert type="info" :bordered="false" class="setup-alert">
            PostgreSQL 保存市场数据，Redis 保存会话、OAuth state、缓存和限流状态。
          </n-alert>
          <n-alert v-if="setupStatus.restart_required" type="success" :bordered="false" class="setup-alert">
            配置已写入运行时文件，请重启 API 服务后继续。重启前不会连接 PostgreSQL 或 Redis。
          </n-alert>

          <n-form ref="formRef" :model="formData" :rules="rules" label-placement="top">
            <n-form-item label="PostgreSQL 连接" path="database_url">
              <n-input v-model:value="formData.database_url" placeholder="postgresql://user:pass@host:5432/db" />
            </n-form-item>
            <n-form-item label="Redis 连接" path="redis_url">
              <n-input v-model:value="formData.redis_url" placeholder="redis://host:6379/0" />
            </n-form-item>
          </n-form>

          <template #footer>
            <div class="actions">
              <n-button tertiary @click="reloadStatus">刷新状态</n-button>
              <n-button
                type="primary"
                :loading="saving"
                :disabled="setupStatus.required && setupStatus.restart_required"
                @click="save"
              >
                保存配置
              </n-button>
            </div>
          </template>
        </n-card>
      </main>
    </n-layout>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { NAlert, NButton, NCard, NForm, NFormItem, NIcon, NInput, NLayout, NTag, useMessage } from 'naive-ui'
import { Moon, Sunny } from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const message = useMessage()
const store = usePluginStore()
const { isDarkMode, setupStatus } = storeToRefs(store)
const { toggleTheme, loadSetupStatus, saveSetupConfig } = store

const formRef = ref(null)
const saving = ref(false)
const formData = reactive({
  database_url: '',
  redis_url: ''
})

const rules = {
  database_url: [
    { required: true, message: '请输入 PostgreSQL 连接', trigger: 'blur' },
    { pattern: /^postgresql(\+asyncpg)?:\/\//, message: '请使用 PostgreSQL 连接串', trigger: 'blur' }
  ],
  redis_url: [
    { required: true, message: '请输入 Redis 连接', trigger: 'blur' },
    { pattern: /^rediss?:\/\//, message: '请使用 Redis 连接串', trigger: 'blur' }
  ]
}

async function reloadStatus() {
  const status = await loadSetupStatus()
  formData.database_url = status.saved_database_url || formData.database_url
  formData.redis_url = status.saved_redis_url || formData.redis_url
}

async function save() {
  formRef.value?.validate(async (errors) => {
    if (errors) {
      message.error('请完善连接信息')
      return
    }
    saving.value = true
    try {
      await saveSetupConfig({ ...formData })
      message.success('配置已保存，请重启后继续')
      await loadSetupStatus()
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      saving.value = false
    }
  })
}

onMounted(async () => {
  await reloadStatus()
})
</script>

<style scoped>
.setup-page {
  min-height: 100vh;
  color: var(--text-color-base);
  background: linear-gradient(180deg, rgba(14, 116, 228, 0.08), transparent 260px), var(--body-color);
}

.setup-layout {
  min-height: 100vh;
  background: transparent;
}

.setup-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  max-width: 920px;
  margin: 0 auto;
  padding: 36px 20px 20px;
  gap: 20px;
}

.eyebrow {
  margin: 0 0 8px;
  color: #0e74e4;
  font-size: 13px;
  font-weight: 700;
}

h1,
h2,
.subtitle {
  margin: 0;
}

h1 {
  font-size: 32px;
  line-height: 1.2;
}

h2 {
  font-size: 18px;
}

.subtitle {
  margin-top: 10px;
  color: var(--text-color-2);
  line-height: 1.6;
}

.setup-main {
  max-width: 720px;
  margin: 0 auto;
  padding: 20px;
}

.setup-card {
  border: 1px solid var(--border-color);
  border-radius: 8px;
}

.card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.setup-alert {
  margin-bottom: 18px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

@media (max-width: 640px) {
  .setup-header {
    padding-top: 24px;
  }

  h1 {
    font-size: 26px;
  }

  .actions {
    flex-direction: column-reverse;
  }
}
</style>
