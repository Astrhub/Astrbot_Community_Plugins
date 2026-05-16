<template>
  <div class="setup-page">
    <n-layout class="setup-layout">
      <header class="setup-header">
        <div>
          <p class="eyebrow">首次配置</p>
          <h1>{{ formData.site.name || siteConfig.name }}</h1>
          <p class="subtitle">填写站点信息、PostgreSQL 和 Redis 连接参数，保存后重启服务再进入市场。</p>
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
            PostgreSQL 保存市场数据，Redis 保存会话令牌。默认使用无 SSL 本地连接，需要远程 TLS 时再打开 SSL。
          </n-alert>
          <n-alert v-if="setupStatus.restart_required" type="success" :bordered="false" class="setup-alert">
            配置已写入运行时文件，请重启 API 服务后继续。重启前不会连接 PostgreSQL 或 Redis。
          </n-alert>

          <n-form ref="formRef" :model="formData" :rules="rules" label-placement="top">
            <section class="form-section">
              <h3>站点</h3>
              <div class="form-grid">
                <n-form-item label="站点名称" path="site.name">
                  <n-input v-model:value="formData.site.name" placeholder="AstrBot Community Plugins" />
                </n-form-item>
                <n-form-item label="站点图标 URL" path="site.icon_url">
                  <n-input v-model:value="formData.site.icon_url" placeholder="/logo.webp 或 https://..." />
                </n-form-item>
              </div>
            </section>

            <section class="form-section">
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

            <section class="form-section">
              <h3>登录与条款</h3>
              <div class="form-grid">
                <n-form-item label="开放登录" path="auth.public_login_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.auth.public_login_enabled" />
                    <span>{{ formData.auth.public_login_enabled ? '允许登录' : '关闭所有登录' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="GitHub OAuth 登录" path="auth.github_login_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.auth.github_login_enabled" />
                    <span>{{ formData.auth.github_login_enabled ? '启用' : '暂不启用' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="登录条款" path="auth.login_agreement_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.auth.login_agreement_enabled" />
                    <span>{{ formData.auth.login_agreement_enabled ? '登录前确认' : '不显示' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="服务条款" path="auth.service_terms_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.auth.service_terms_enabled" />
                    <span>{{ formData.auth.service_terms_enabled ? '显示服务条款' : '不显示' }}</span>
                  </div>
                </n-form-item>
              </div>
              <n-form-item v-if="formData.auth.login_agreement_enabled" label="登录条款内容" path="auth.login_agreement_text">
                <n-input
                  v-model:value="formData.auth.login_agreement_text"
                  type="textarea"
                  :autosize="{ minRows: 3, maxRows: 8 }"
                  placeholder="用户登录前需要同意的条款"
                />
              </n-form-item>
              <n-form-item v-if="formData.auth.service_terms_enabled" label="服务条款内容" path="auth.service_terms_text">
                <n-input
                  v-model:value="formData.auth.service_terms_text"
                  type="textarea"
                  :autosize="{ minRows: 3, maxRows: 8 }"
                  placeholder="市场服务条款、社区规则或免责声明"
                />
              </n-form-item>
            </section>

            <section class="form-section">
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

            <section class="form-section">
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
import {
  NAlert,
  NButton,
  NCard,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NLayout,
  NSwitch,
  NTag,
  useMessage
} from 'naive-ui'
import { Moon, Sunny } from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const message = useMessage()
const store = usePluginStore()
const { isDarkMode, setupStatus, siteConfig } = storeToRefs(store)
const { toggleTheme, loadSetupStatus, saveSetupConfig } = store

const formRef = ref(null)
const saving = ref(false)
const formData = reactive({
  site: {
    name: '',
    icon_url: ''
  },
  admin: {
    username: 'admin',
    password: ''
  },
  auth: {
    github_login_enabled: false,
    public_login_enabled: true,
    login_agreement_enabled: false,
    login_agreement_text: '',
    service_terms_enabled: false,
    service_terms_text: ''
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

const requiredText = (message) => ({ required: true, message, trigger: 'blur' })
const rules = {
  'site.name': [requiredText('请输入站点名称')],
  'site.icon_url': [
    requiredText('请输入站点图标 URL'),
    {
      validator: (_, value) => String(value || '').startsWith('/') || /^https?:\/\//.test(value),
      message: '请输入 / 开头路径或 http(s) URL',
      trigger: 'blur'
    }
  ],
  'admin.username': [requiredText('请输入内部管理员账号')],
  'admin.password': [
    requiredText('请输入内部管理员密码'),
    { min: 8, message: '密码至少 8 位', trigger: 'blur' }
  ],
  'auth.login_agreement_text': [
    {
      validator: () => !formData.auth.login_agreement_enabled ||
        Boolean(formData.auth.login_agreement_text.trim()),
      message: '启用登录条款后必须填写内容',
      trigger: 'blur'
    }
  ],
  'auth.service_terms_text': [
    {
      validator: () => !formData.auth.service_terms_enabled ||
        Boolean(formData.auth.service_terms_text.trim()),
      message: '启用服务条款后必须填写内容',
      trigger: 'blur'
    }
  ],
  'postgres.host': [requiredText('请输入 PostgreSQL 主机名')],
  'postgres.port': [
    { type: 'number', required: true, message: '请输入 PostgreSQL 端口', trigger: 'blur' }
  ],
  'postgres.database': [requiredText('请输入数据库名')],
  'postgres.username': [requiredText('请输入 PostgreSQL 账号')],
  'postgres.password': [requiredText('请输入 PostgreSQL 密码')],
  'redis.host': [requiredText('请输入 Redis 主机名')],
  'redis.port': [
    { type: 'number', required: true, message: '请输入 Redis 端口', trigger: 'blur' }
  ],
  'redis.database': [
    { type: 'number', required: true, message: '请输入 Redis 数据库编号', trigger: 'blur' }
  ]
}

function normalizeNumberFields() {
  formData.postgres.port = Number(formData.postgres.port || 5432)
  formData.redis.port = Number(formData.redis.port || 6379)
  formData.redis.database = Number(formData.redis.database || 0)
}

async function reloadStatus() {
  const status = await loadSetupStatus()
  applySetupConfig(status.saved_setup)
}

function applySetupConfig(config = {}) {
  Object.assign(formData.site, config.site || {})
  Object.assign(formData.auth, config.auth || {})
  Object.assign(formData.postgres, config.postgres || {})
  Object.assign(formData.redis, config.redis || {})
}

function setupPayload() {
  normalizeNumberFields()
  return JSON.parse(JSON.stringify(formData))
}

async function save() {
  formRef.value?.validate(async (errors) => {
    if (errors) {
      message.error('请完善连接信息')
      return
    }
    saving.value = true
    try {
      await saveSetupConfig(setupPayload())
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

.form-section {
  padding-top: 4px;
}

.form-section + .form-section {
  margin-top: 10px;
  padding-top: 18px;
  border-top: 1px solid var(--border-color);
}

.form-section h3 {
  margin: 0 0 14px;
  color: var(--text-color-1);
  font-size: 15px;
}

.form-grid {
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

  .form-grid {
    grid-template-columns: 1fr;
  }

  .actions {
    flex-direction: column-reverse;
  }
}
</style>
