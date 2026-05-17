<template>
  <div class="setup-page">
    <n-layout class="setup-layout">
      <header class="setup-header">
        <div>
          <p class="eyebrow">首次配置</p>
          <h1>{{ formData.site.name || siteConfig.name }}</h1>
          <p class="subtitle">完成必要连接后，GitHub OAuth、条款、邮件和市场策略可在后台继续配置。</p>
        </div>
        <theme-mode-button circle />
      </header>

      <main class="setup-main">
        <n-card :bordered="false" class="setup-card">
          <template #header>
            <div class="card-title">
              <h2>安装向导</h2>
              <n-tag v-if="activated" type="success" :bordered="false">已启用</n-tag>
              <n-tag v-else-if="setupStatus.required" type="warning" :bordered="false">未完成</n-tag>
              <n-tag v-else type="success" :bordered="false">已完成</n-tag>
            </div>
          </template>

          <n-alert v-if="activated" type="success" :bordered="false" class="setup-alert">
            配置已保存，PostgreSQL 和 Redis 已在当前服务中启用。
          </n-alert>
          <n-alert v-else type="info" :bordered="false" class="setup-alert">
            PostgreSQL 用于持久化市场数据，Redis 用于登录会话。保存前会验证连接并初始化数据表。
          </n-alert>

          <n-steps :current="activeStep + 1" class="setup-steps">
            <n-step v-for="step in steps" :key="step.id" :title="step.title" />
          </n-steps>

          <n-form :model="formData" label-placement="top">
            <section v-if="currentStep.id === 'site'" class="form-section">
              <h3>站点基础</h3>
              <div class="form-grid">
                <n-form-item label="站点名称" path="site.name">
                  <n-input v-model:value="formData.site.name" placeholder="AstrBot Community Plugins" />
                </n-form-item>
                <n-form-item label="站点图标 URL" path="site.icon_url">
                  <n-input v-model:value="formData.site.icon_url" placeholder="/logo.webp 或 https://..." />
                </n-form-item>
              </div>
            </section>

            <section v-if="currentStep.id === 'admin'" class="form-section">
              <h3>核心管理员</h3>
              <div class="form-grid">
                <n-form-item label="内部账号" path="admin.username">
                  <n-input v-model:value="formData.admin.username" placeholder="admin" />
                </n-form-item>
                <n-form-item label="内部密码" path="admin.password">
                  <n-input
                    v-model:value="formData.admin.password"
                    type="password"
                    show-password-on="click"
                    placeholder="至少 8 位"
                  />
                </n-form-item>
              </div>
            </section>

            <section v-if="currentStep.id === 'postgres'" class="form-section">
              <h3>PostgreSQL</h3>
              <div class="form-grid">
                <n-form-item label="主机名" path="postgres.host">
                  <n-input v-model:value="formData.postgres.host" placeholder="127.0.0.1" />
                </n-form-item>
                <n-form-item label="端口" path="postgres.port">
                  <n-input-number v-model:value="formData.postgres.port" :min="1" :max="65535" />
                </n-form-item>
                <n-form-item label="数据库名" path="postgres.database">
                  <n-input v-model:value="formData.postgres.database" placeholder="market" />
                </n-form-item>
                <n-form-item label="账号" path="postgres.username">
                  <n-input v-model:value="formData.postgres.username" placeholder="market" />
                </n-form-item>
                <n-form-item label="密码" path="postgres.password">
                  <n-input
                    v-model:value="formData.postgres.password"
                    type="password"
                    show-password-on="click"
                    placeholder="PostgreSQL 密码"
                  />
                </n-form-item>
                <n-form-item label="SSL" path="postgres.ssl">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.postgres.ssl" />
                    <span>{{ formData.postgres.ssl ? '启用 SSL' : '无 SSL 本地连接' }}</span>
                  </div>
                </n-form-item>
              </div>
            </section>

            <section v-if="currentStep.id === 'redis'" class="form-section">
              <h3>Redis</h3>
              <div class="form-grid">
                <n-form-item label="主机名" path="redis.host">
                  <n-input v-model:value="formData.redis.host" placeholder="127.0.0.1" />
                </n-form-item>
                <n-form-item label="端口" path="redis.port">
                  <n-input-number v-model:value="formData.redis.port" :min="1" :max="65535" />
                </n-form-item>
                <n-form-item label="数据库编号" path="redis.database">
                  <n-input-number v-model:value="formData.redis.database" :min="0" />
                </n-form-item>
                <n-form-item label="密码" path="redis.password">
                  <n-input
                    v-model:value="formData.redis.password"
                    type="password"
                    show-password-on="click"
                    placeholder="无密码可留空"
                  />
                </n-form-item>
                <n-form-item label="SSL" path="redis.ssl">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.redis.ssl" />
                    <span>{{ formData.redis.ssl ? '启用 SSL' : '无 SSL 本地连接' }}</span>
                  </div>
                </n-form-item>
              </div>
            </section>

            <section v-if="currentStep.id === 'review'" class="form-section">
              <h3>确认配置</h3>
              <div class="review-grid">
                <div class="review-item">
                  <span>站点</span>
                  <strong>{{ formData.site.name }}</strong>
                </div>
                <div class="review-item">
                  <span>核心管理员</span>
                  <strong>{{ formData.admin.username }}</strong>
                </div>
                <div class="review-item">
                  <span>PostgreSQL</span>
                  <strong>{{ formData.postgres.host }}:{{ formData.postgres.port }}/{{ formData.postgres.database }}</strong>
                </div>
                <div class="review-item">
                  <span>Redis</span>
                  <strong>{{ formData.redis.host }}:{{ formData.redis.port }}/{{ formData.redis.database }}</strong>
                </div>
              </div>
            </section>
          </n-form>

          <template #footer>
            <div class="actions">
              <n-button tertiary :disabled="saving || activated" @click="reloadStatus">刷新状态</n-button>
              <div class="step-actions">
                <n-button v-if="!isFirstStep" :disabled="saving || activated" @click="previousStep">
                  上一步
                </n-button>
                <n-button v-if="!isLastStep" type="primary" :disabled="activated" @click="nextStep">
                  下一步
                </n-button>
                <n-button
                  v-else
                  type="primary"
                  :loading="saving"
                  :disabled="activated"
                  @click="save"
                >
                  保存并启用
                </n-button>
              </div>
            </div>
          </template>
        </n-card>
      </main>
    </n-layout>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NCard,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NLayout,
  NStep,
  NSteps,
  NSwitch,
  NTag,
  useMessage
} from 'naive-ui'
import { usePluginStore } from '@/stores/plugins'
import ThemeModeButton from '@/components/ThemeModeButton.vue'

const message = useMessage()
const router = useRouter()
const store = usePluginStore()
const { setupStatus, siteConfig } = storeToRefs(store)
const { loadSetupStatus, saveSetupConfig } = store

const steps = [
  { id: 'site', title: '站点' },
  { id: 'admin', title: '管理员' },
  { id: 'postgres', title: 'PostgreSQL' },
  { id: 'redis', title: 'Redis' },
  { id: 'review', title: '确认' }
]

const activeStep = ref(0)
const saving = ref(false)
const activated = ref(false)

const formData = reactive({
  site: {
    name: 'AstrBot Community Plugins',
    icon_url: '/logo.webp'
  },
  admin: {
    username: 'admin',
    password: ''
  },
  postgres: {
    host: '127.0.0.1',
    port: 5432,
    database: '',
    username: '',
    password: '',
    ssl: false
  },
  redis: {
    host: '127.0.0.1',
    port: 6379,
    database: 0,
    password: '',
    ssl: false
  }
})

const currentStep = computed(() => steps[activeStep.value])
const isFirstStep = computed(() => activeStep.value === 0)
const isLastStep = computed(() => activeStep.value === steps.length - 1)

function normalizeNumberFields() {
  formData.postgres.port = Number(formData.postgres.port || 5432)
  formData.redis.port = Number(formData.redis.port || 6379)
  formData.redis.database = Number(formData.redis.database || 0)
}

function applySetupConfig(config = {}) {
  Object.assign(formData.site, config.site || {})
  Object.assign(formData.postgres, config.postgres || {})
  Object.assign(formData.redis, config.redis || {})
  if (config.admin?.username) formData.admin.username = config.admin.username
}

async function reloadStatus() {
  const status = await loadSetupStatus()
  if (!status.required) {
    router.replace('/')
    return
  }
  applySetupConfig(status.saved_setup)
}

function setupPayload() {
  normalizeNumberFields()
  return {
    site: { ...formData.site },
    admin: { ...formData.admin },
    postgres: { ...formData.postgres },
    redis: { ...formData.redis }
  }
}

function requireText(value, errorMessage) {
  if (!String(value || '').trim()) {
    message.error(errorMessage)
    return false
  }
  return true
}

function isRootOrHttpUrl(value) {
  return String(value || '').startsWith('/') || /^https?:\/\//.test(String(value || ''))
}

function validateStep(index = activeStep.value) {
  normalizeNumberFields()
  const stepId = steps[index].id
  if (stepId === 'site') {
    if (!requireText(formData.site.name, '请输入站点名称')) return false
    if (!requireText(formData.site.icon_url, '请输入站点图标 URL')) return false
    if (!isRootOrHttpUrl(formData.site.icon_url)) {
      message.error('站点图标必须是 / 开头路径或 http(s) URL')
      return false
    }
  }
  if (stepId === 'admin') {
    if (!requireText(formData.admin.username, '请输入核心管理员账号')) return false
    if (String(formData.admin.password || '').length < 8) {
      message.error('核心管理员密码至少 8 位')
      return false
    }
  }
  if (stepId === 'postgres') {
    if (!requireText(formData.postgres.host, '请输入 PostgreSQL 主机名')) return false
    if (!formData.postgres.port) {
      message.error('请输入 PostgreSQL 端口')
      return false
    }
    if (!requireText(formData.postgres.database, '请输入 PostgreSQL 数据库名')) return false
    if (!requireText(formData.postgres.username, '请输入 PostgreSQL 账号')) return false
    if (!requireText(formData.postgres.password, '请输入 PostgreSQL 密码')) return false
  }
  if (stepId === 'redis') {
    if (!requireText(formData.redis.host, '请输入 Redis 主机名')) return false
    if (!formData.redis.port) {
      message.error('请输入 Redis 端口')
      return false
    }
  }
  return true
}

function validateAll() {
  return steps.every((_, index) => validateStep(index))
}

function nextStep() {
  if (!validateStep()) return
  activeStep.value += 1
}

function previousStep() {
  activeStep.value = Math.max(0, activeStep.value - 1)
}

async function save() {
  if (!validateAll()) return
  saving.value = true
  try {
    await saveSetupConfig(setupPayload())
    activated.value = true
    message.success('配置已保存并已启用')
    await store.loadPlugins()
    await store.loadCurrentUser()
    window.setTimeout(() => {
      window.location.assign('/')
    }, 600)
  } catch (error) {
    message.error(error.message || '保存失败')
  } finally {
    saving.value = false
  }
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
  max-width: 760px;
  margin: 0 auto;
  padding: 20px;
}

.setup-card {
  border: 1px solid var(--border-color);
  border-radius: 8px;
}

.card-title,
.actions,
.step-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.card-title,
.actions {
  justify-content: space-between;
}

.setup-alert {
  margin-bottom: 18px;
}

.setup-steps {
  margin-bottom: 24px;
}

.form-section {
  min-height: 260px;
}

.form-section h3 {
  margin: 0 0 14px;
  color: var(--text-color-1);
  font-size: 15px;
}

.form-grid,
.review-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 16px;
}

.switch-row {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 34px;
  color: var(--text-color-2);
}

.review-grid {
  gap: 14px;
}

.review-item {
  padding: 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--card-color);
}

.review-item span,
.review-item strong {
  display: block;
}

.review-item span {
  color: var(--text-color-3);
  font-size: 12px;
}

.review-item strong {
  margin-top: 6px;
  overflow-wrap: anywhere;
  color: var(--text-color-1);
  font-size: 14px;
  font-weight: 700;
}

@media (max-width: 640px) {
  .setup-header {
    padding-top: 24px;
  }

  h1 {
    font-size: 26px;
  }

  .form-grid,
  .review-grid {
    grid-template-columns: 1fr;
  }

  .actions {
    align-items: stretch;
    flex-direction: column-reverse;
  }

  .step-actions {
    justify-content: flex-end;
  }
}
</style>
