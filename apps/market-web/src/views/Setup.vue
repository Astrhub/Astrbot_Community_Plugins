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
                <n-form-item label="副标题" path="site.subtitle">
                  <n-input v-model:value="formData.site.subtitle" placeholder="全新社区插件市场" />
                </n-form-item>
                <n-form-item label="站点描述" path="site.description">
                  <n-input v-model:value="formData.site.description" placeholder="发现、评价和提交 AstrBot 插件。" />
                </n-form-item>
                <n-form-item label="联系邮箱" path="site.contact_email">
                  <n-input v-model:value="formData.site.contact_email" placeholder="可选" />
                </n-form-item>
                <n-form-item label="文档地址" path="site.docs_url">
                  <n-input v-model:value="formData.site.docs_url" placeholder="https://docs.astrbot.app/..." />
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
              <h3>GitHub OAuth</h3>
              <div class="form-grid">
                <n-form-item label="Client ID" path="github.client_id">
                  <n-input v-model:value="formData.github.client_id" placeholder="GitHub OAuth App Client ID" />
                </n-form-item>
                <n-form-item label="Client Secret" path="github.client_secret">
                  <n-input
                    v-model:value="formData.github.client_secret"
                    type="password"
                    show-password-on="click"
                    placeholder="留空可稍后配置"
                  />
                </n-form-item>
                <n-form-item label="回调地址" path="github.callback_url">
                  <n-input v-model:value="formData.github.callback_url" placeholder="https://your-domain/v1/auth/github/callback" />
                </n-form-item>
                <n-form-item label="管理员组织" path="github.admin_org">
                  <n-input v-model:value="formData.github.admin_org" placeholder="可选，GitHub 组织名" />
                </n-form-item>
                <n-form-item label="授权范围" path="github.scope">
                  <n-input v-model:value="formData.github.scope" placeholder="read:user user:email read:org" />
                </n-form-item>
              </div>
            </section>

            <section class="form-section">
              <h3>市场策略</h3>
              <div class="form-grid">
                <n-form-item label="开放插件提交" path="market.submissions_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.market.submissions_enabled" />
                    <span>{{ formData.market.submissions_enabled ? '允许提交' : '暂停提交' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="开放评论" path="market.comments_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.market.comments_enabled" />
                    <span>{{ formData.market.comments_enabled ? '允许评论' : '关闭评论' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="开放点赞" path="market.likes_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.market.likes_enabled" />
                    <span>{{ formData.market.likes_enabled ? '允许点赞' : '关闭点赞' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="自动上架" path="market.plugin_auto_approve_enabled">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.market.plugin_auto_approve_enabled" />
                    <span>{{ formData.market.plugin_auto_approve_enabled ? '提交后自动上架' : '需要管理员审核' }}</span>
                  </div>
                </n-form-item>
                <n-form-item label="最多标签数" path="market.max_plugin_tags">
                  <n-input-number v-model:value="formData.market.max_plugin_tags" :min="0" :max="50" />
                </n-form-item>
              </div>
            </section>

            <section class="form-section">
              <h3>邮件服务</h3>
              <div class="form-grid">
                <n-form-item label="邮件服务" path="email.provider">
                  <n-select v-model:value="formData.email.provider" :options="emailProviderOptions" />
                </n-form-item>
                <n-form-item label="每日发送上限" path="email.daily_limit">
                  <n-input-number v-model:value="formData.email.daily_limit" :min="0" />
                </n-form-item>
                <n-form-item label="单邮箱每日验证码上限" path="email.verification_daily_limit_per_user">
                  <n-input-number v-model:value="formData.email.verification_daily_limit_per_user" :min="0" />
                </n-form-item>
              </div>
              <div v-if="formData.email.provider === 'smtp'" class="form-grid">
                <n-form-item label="SMTP 主机" path="email.smtp.host">
                  <n-input v-model:value="formData.email.smtp.host" placeholder="smtp.example.com" />
                </n-form-item>
                <n-form-item label="SMTP 端口" path="email.smtp.port">
                  <n-input-number v-model:value="formData.email.smtp.port" :min="1" :max="65535" />
                </n-form-item>
                <n-form-item label="SMTP 账号" path="email.smtp.username">
                  <n-input v-model:value="formData.email.smtp.username" placeholder="noreply@example.com" />
                </n-form-item>
                <n-form-item label="SMTP 密码" path="email.smtp.password">
                  <n-input v-model:value="formData.email.smtp.password" type="password" show-password-on="click" placeholder="留空表示不更新" />
                </n-form-item>
                <n-form-item label="发件邮箱" path="email.smtp.from_address">
                  <n-input v-model:value="formData.email.smtp.from_address" placeholder="noreply@example.com" />
                </n-form-item>
                <n-form-item label="SMTP SSL" path="email.smtp.ssl">
                  <div class="switch-row">
                    <n-switch v-model:value="formData.email.smtp.ssl" />
                    <span>{{ formData.email.smtp.ssl ? '启用 SSL' : '不启用 SSL' }}</span>
                  </div>
                </n-form-item>
              </div>
              <div v-if="formData.email.provider === 'cloudflare'" class="form-grid">
                <n-form-item label="Cloudflare Account ID" path="email.cloudflare.account_id">
                  <n-input v-model:value="formData.email.cloudflare.account_id" placeholder="Cloudflare Account ID" />
                </n-form-item>
                <n-form-item label="Cloudflare API Token" path="email.cloudflare.api_token">
                  <n-input v-model:value="formData.email.cloudflare.api_token" type="password" show-password-on="click" placeholder="留空表示不更新" />
                </n-form-item>
                <n-form-item label="发件邮箱" path="email.cloudflare.from_address">
                  <n-input v-model:value="formData.email.cloudflare.from_address" placeholder="noreply@mail.example.com" />
                </n-form-item>
              </div>
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
  NSelect,
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
    icon_url: '',
    subtitle: '',
    description: '',
    contact_email: '',
    docs_url: ''
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
  github: {
    client_id: '',
    client_secret: '',
    callback_url: `${window.location.origin}/v1/auth/github/callback`,
    scope: 'read:user user:email read:org',
    admin_org: ''
  },
  market: {
    submissions_enabled: true,
    comments_enabled: true,
    likes_enabled: true,
    plugin_auto_approve_enabled: false,
    max_plugin_tags: 8
  },
  email: {
    provider: 'disabled',
    smtp: {
      host: '',
      port: 587,
      username: '',
      password: '',
      from_address: '',
      ssl: false
    },
    cloudflare: {
      account_id: '',
      api_token: '',
      from_address: ''
    },
    daily_limit: 0,
    verification_daily_limit_per_user: 5
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

const emailProviderOptions = [
  { label: '关闭邮件', value: 'disabled' },
  { label: 'SMTP', value: 'smtp' },
  { label: 'Cloudflare Email Service', value: 'cloudflare' }
]

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
  'site.contact_email': [
    {
      validator: (_, value) => !value || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value),
      message: '请输入有效邮箱',
      trigger: 'blur'
    }
  ],
  'site.docs_url': [
    {
      validator: (_, value) => !value || /^https?:\/\//.test(value),
      message: '请输入 http(s) URL',
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
  'github.client_id': [
    {
      validator: () => !formData.auth.github_login_enabled || Boolean(formData.github.client_id.trim()),
      message: '启用 GitHub 登录后必须填写 Client ID',
      trigger: 'blur'
    }
  ],
  'github.client_secret': [
    {
      validator: () => !formData.auth.github_login_enabled || Boolean(formData.github.client_secret.trim()),
      message: '启用 GitHub 登录后必须填写 Client Secret',
      trigger: 'blur'
    }
  ],
  'github.callback_url': [
    {
      validator: (_, value) => !formData.auth.github_login_enabled || /^https?:\/\//.test(value),
      message: '启用 GitHub 登录后必须填写 http(s) 回调地址',
      trigger: 'blur'
    }
  ],
  'email.smtp.host': [
    {
      validator: () => formData.email.provider !== 'smtp' || Boolean(formData.email.smtp.host.trim()),
      message: '启用 SMTP 后必须填写主机',
      trigger: 'blur'
    }
  ],
  'email.smtp.from_address': [
    {
      validator: () => formData.email.provider !== 'smtp' ||
        /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(formData.email.smtp.from_address),
      message: '请输入有效发件邮箱',
      trigger: 'blur'
    }
  ],
  'email.cloudflare.account_id': [
    {
      validator: () => formData.email.provider !== 'cloudflare' ||
        Boolean(formData.email.cloudflare.account_id.trim()),
      message: '启用 Cloudflare 后必须填写 Account ID',
      trigger: 'blur'
    }
  ],
  'email.cloudflare.api_token': [
    {
      validator: () => formData.email.provider !== 'cloudflare' ||
        Boolean(formData.email.cloudflare.api_token.trim()),
      message: '启用 Cloudflare 后必须填写 API Token',
      trigger: 'blur'
    }
  ],
  'email.cloudflare.from_address': [
    {
      validator: () => formData.email.provider !== 'cloudflare' ||
        /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(formData.email.cloudflare.from_address),
      message: '请输入有效发件邮箱',
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
  formData.market.max_plugin_tags = Number(formData.market.max_plugin_tags || 0)
  formData.email.smtp.port = Number(formData.email.smtp.port || 587)
  formData.email.daily_limit = Number(formData.email.daily_limit || 0)
  formData.email.verification_daily_limit_per_user = Number(formData.email.verification_daily_limit_per_user || 0)
}

async function reloadStatus() {
  const status = await loadSetupStatus()
  applySetupConfig(status.saved_setup)
}

function applySetupConfig(config = {}) {
  Object.assign(formData.site, config.site || {})
  Object.assign(formData.auth, config.auth || {})
  Object.assign(formData.github, config.github || {})
  Object.assign(formData.market, config.market || {})
  Object.assign(formData.email, config.email || {})
  Object.assign(formData.email.smtp, config.email?.smtp || {})
  Object.assign(formData.email.cloudflare, config.email?.cloudflare || {})
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
