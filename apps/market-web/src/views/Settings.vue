<template>
  <div class="settings-page">
    <n-layout-header class="settings-header">
      <div class="header-content">
        <div class="header-left">
          <n-button quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <p class="eyebrow">核心管理员</p>
            <h1>系统设置</h1>
          </div>
        </div>
        <div class="header-actions">
          <theme-mode-button circle />
          <n-button tertiary @click="loadSettings">刷新</n-button>
          <n-button type="primary" :loading="saving" @click="save">保存</n-button>
        </div>
      </div>
    </n-layout-header>

    <main class="settings-content">
      <n-alert v-if="!isCoreAdmin && !loading" type="warning" :bordered="false">
        只有核心管理员可以管理系统设置。
      </n-alert>

      <n-spin :show="loading">
        <n-form
          v-if="isCoreAdmin"
          ref="formRef"
          :model="formData"
          :rules="rules"
          label-placement="top"
          class="settings-form"
        >
          <section class="settings-section">
            <div class="section-title">
              <h2>站点展示</h2>
              <p>首页、标题、图标和公共联系信息。</p>
            </div>
            <div class="form-grid">
              <n-form-item label="站点名称" path="site.name">
                <n-input v-model:value="formData.site.name" />
              </n-form-item>
              <n-form-item label="站点图标 URL" path="site.icon_url">
                <n-input v-model:value="formData.site.icon_url" placeholder="/logo.webp 或 https://..." />
              </n-form-item>
              <n-form-item label="副标题" path="site.subtitle">
                <n-input v-model:value="formData.site.subtitle" />
              </n-form-item>
              <n-form-item label="站点描述" path="site.description">
                <n-input v-model:value="formData.site.description" />
              </n-form-item>
              <n-form-item label="联系邮箱" path="site.contact_email">
                <n-input v-model:value="formData.site.contact_email" placeholder="可选" />
              </n-form-item>
              <n-form-item label="文档地址" path="site.docs_url">
                <n-input v-model:value="formData.site.docs_url" placeholder="https://docs.astrbot.app/..." />
              </n-form-item>
            </div>
          </section>

          <section class="settings-section">
            <div class="section-title">
              <h2>站点公告</h2>
              <p>发布后会显示在市场首页。</p>
            </div>
            <div class="form-grid">
              <n-form-item label="公告标题">
                <n-input
                  v-model:value="announcementForm.title"
                  :maxlength="80"
                  show-count
                  placeholder="例如：维护通知"
                />
              </n-form-item>
              <n-form-item label="公告内容" class="form-row-full">
                <n-input
                  v-model:value="announcementForm.body"
                  type="textarea"
                  :maxlength="1000"
                  show-count
                  :autosize="{ minRows: 3, maxRows: 6 }"
                  placeholder="输入需要展示给用户的公告内容"
                />
              </n-form-item>
            </div>
            <div class="announcement-actions">
              <n-button type="primary" :loading="publishingAnnouncement" @click="publishSiteAnnouncement">
                发布公告
              </n-button>
            </div>
          </section>

          <section class="settings-section">
            <div class="section-title">
              <h2>基础设施</h2>
              <p>PostgreSQL 和 Redis 连接变更需要写入运行时配置并重启 API。</p>
            </div>
            <div class="infra-grid">
              <div class="infra-item">
                <span>PostgreSQL</span>
                <n-tag :type="setupStatus.database_configured ? 'success' : 'warning'" :bordered="false">
                  {{ setupStatus.database_configured ? '已配置' : '未配置' }}
                </n-tag>
              </div>
              <div class="infra-item">
                <span>Redis</span>
                <n-tag :type="setupStatus.redis_configured ? 'success' : 'warning'" :bordered="false">
                  {{ setupStatus.redis_configured ? '已配置' : '未配置' }}
                </n-tag>
              </div>
              <div class="infra-item">
                <span>重启状态</span>
                <n-tag :type="setupStatus.restart_required ? 'warning' : 'success'" :bordered="false">
                  {{ setupStatus.restart_required ? '需要重启' : '当前生效' }}
                </n-tag>
              </div>
            </div>
            <n-button secondary @click="goSetup">打开基础设施配置</n-button>
          </section>

          <section class="settings-section">
            <div class="section-title">
              <h2>登录与条款</h2>
              <p>控制内部账号、GitHub OAuth，以及登录前必须确认的条款。</p>
            </div>
            <div class="switch-grid">
              <setting-switch v-model="formData.auth.public_login_enabled" label="内部账号登录" enabled="允许内部账号登录" disabled="仅核心管理员可登录后台" />
              <setting-switch v-model="formData.auth.github_login_enabled" label="GitHub OAuth" enabled="允许 GitHub 登录 / 注册" disabled="关闭 GitHub 登录 / 注册" />
              <setting-switch v-model="formData.auth.login_agreement_enabled" label="登录条款" enabled="登录前确认" disabled="不显示" />
              <setting-switch v-model="formData.auth.service_terms_enabled" label="服务条款" enabled="显示服务条款" disabled="不显示" />
            </div>
            <n-form-item v-if="formData.auth.login_agreement_enabled" label="登录条款内容" path="auth.login_agreement_text">
              <n-input v-model:value="formData.auth.login_agreement_text" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" />
            </n-form-item>
            <n-form-item v-if="formData.auth.service_terms_enabled" label="服务条款内容" path="auth.service_terms_text">
              <n-input v-model:value="formData.auth.service_terms_text" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" />
            </n-form-item>
          </section>

          <section class="settings-section">
            <div class="section-title">
              <h2>GitHub OAuth</h2>
              <p>用于识别插件仓库归属和指定组织管理员。</p>
            </div>
            <div class="form-grid">
              <n-form-item label="Client ID" path="github.client_id">
                <n-input v-model:value="formData.github.client_id" />
              </n-form-item>
              <n-form-item label="Client Secret" path="github.client_secret">
                <n-input
                  v-model:value="formData.github.client_secret"
                  type="password"
                  show-password-on="click"
                  :placeholder="formData.github.client_secret_configured ? '留空或保持遮蔽值表示不更新' : 'GitHub OAuth App Secret'"
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
              <n-form-item label="GitHub API Token（兜底）" path="github.api_token">
                <n-input
                  v-model:value="formData.github.api_token"
                  type="password"
                  show-password-on="click"
                  :placeholder="formData.github.api_token_configured ? '留空或保持遮蔽值表示不更新' : '只读 Token，用于服务端同步兜底'"
                />
              </n-form-item>
            </div>
            <div class="switch-grid">
              <setting-switch
                v-model="formData.github.metadata_sync_enabled"
                label="元数据自动同步"
                enabled="按间隔同步"
                disabled="暂停同步"
              />
            </div>
            <div class="form-grid compact-grid">
              <n-form-item label="默认同步间隔（秒）" path="github.metadata_sync_interval_seconds">
                <n-input-number
                  v-model:value="formData.github.metadata_sync_interval_seconds"
                  :min="300"
                  :max="86400"
                  :step="300"
                />
              </n-form-item>
            </div>
          </section>

          <section class="settings-section">
            <div class="section-title">
              <h2>市场策略</h2>
              <p>控制插件提交、互动功能和审核策略。</p>
            </div>
            <div class="switch-grid">
              <setting-switch v-model="formData.market.submissions_enabled" label="插件提交" enabled="允许提交" disabled="暂停提交" />
              <setting-switch v-model="formData.market.comments_enabled" label="评论" enabled="允许评论" disabled="关闭评论" />
              <setting-switch v-model="formData.market.likes_enabled" label="点赞" enabled="允许点赞" disabled="关闭点赞" />
              <setting-switch v-model="formData.market.plugin_auto_approve_enabled" label="自动上架" enabled="提交后上架" disabled="管理员审核" />
            </div>
            <div class="form-grid compact-grid">
              <n-form-item label="最多标签数" path="market.max_plugin_tags">
                <n-input-number v-model:value="formData.market.max_plugin_tags" :min="0" :max="50" />
              </n-form-item>
            </div>
          </section>

          <section class="settings-section">
            <div class="section-title">
              <h2>邮件服务</h2>
              <p>支持 SMTP 或 Cloudflare Email Service，密钥保存后只显示遮蔽状态。</p>
            </div>
            <div class="form-grid compact-grid">
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
                <n-input v-model:value="formData.email.smtp.username" />
              </n-form-item>
              <n-form-item label="SMTP 密码" path="email.smtp.password">
                <n-input v-model:value="formData.email.smtp.password" type="password" show-password-on="click" placeholder="留空或保持遮蔽值表示不更新" />
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
                <n-input v-model:value="formData.email.cloudflare.account_id" />
              </n-form-item>
              <n-form-item label="Cloudflare API Token" path="email.cloudflare.api_token">
                <n-input v-model:value="formData.email.cloudflare.api_token" type="password" show-password-on="click" placeholder="留空或保持遮蔽值表示不更新" />
              </n-form-item>
              <n-form-item label="发件邮箱" path="email.cloudflare.from_address">
                <n-input v-model:value="formData.email.cloudflare.from_address" placeholder="noreply@mail.example.com" />
              </n-form-item>
            </div>

            <div class="test-email-row">
              <n-input v-model:value="testEmail.to" placeholder="测试收件邮箱" />
              <n-button :loading="testingEmail" :disabled="formData.email.provider === 'disabled'" @click="sendEmailTest">
                发送测试邮件
              </n-button>
            </div>
          </section>
        </n-form>
      </n-spin>
    </main>
  </div>
</template>

<script setup>
import { computed, h, onMounted, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NLayoutHeader,
  NSelect,
  NSpin,
  NSwitch,
  NTag,
  useMessage
} from 'naive-ui'
import { ArrowBack } from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'
import ThemeModeButton from '@/components/ThemeModeButton.vue'

const SettingSwitch = {
  props: {
    modelValue: Boolean,
    label: String,
    enabled: String,
    disabled: String
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    return () => h('div', { class: 'switch-card' }, [
      h('span', { class: 'switch-label' }, props.label),
      h('div', { class: 'switch-row' }, [
        h(NSwitch, {
          value: props.modelValue,
          'onUpdate:value': (value) => emit('update:modelValue', value)
        }),
        h('span', props.modelValue ? props.enabled : props.disabled)
      ])
    ])
  }
}

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { currentUser, setupStatus } = storeToRefs(store)
const {
  loadCurrentUser,
  loadSetupStatus,
  loadSystemSettings,
  saveSystemSettings,
  sendTestEmail,
  publishAnnouncement
} = store

const formRef = ref(null)
const loading = ref(true)
const saving = ref(false)
const testingEmail = ref(false)
const publishingAnnouncement = ref(false)
const isCoreAdmin = computed(() => currentUser.value?.role === 'core_admin')
const testEmail = reactive({ to: '' })
const announcementForm = reactive({ title: '', body: '' })

const formData = reactive(createSettingsForm())

const emailProviderOptions = [
  { label: '关闭邮件', value: 'disabled' },
  { label: 'SMTP', value: 'smtp' },
  { label: 'Cloudflare Email Service', value: 'cloudflare' }
]

const requiredText = (text) => ({ required: true, message: text, trigger: 'blur' })
const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
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
      validator: (_, value) => !value || emailPattern.test(value),
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
  'auth.login_agreement_text': [
    {
      validator: () => !formData.auth.login_agreement_enabled || Boolean(formData.auth.login_agreement_text.trim()),
      message: '启用登录条款后必须填写内容',
      trigger: 'blur'
    }
  ],
  'auth.service_terms_text': [
    {
      validator: () => !formData.auth.service_terms_enabled || Boolean(formData.auth.service_terms_text.trim()),
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
      validator: () => formData.email.provider !== 'smtp' || emailPattern.test(formData.email.smtp.from_address),
      message: '请输入有效发件邮箱',
      trigger: 'blur'
    }
  ],
  'email.cloudflare.account_id': [
    {
      validator: () => formData.email.provider !== 'cloudflare' || Boolean(formData.email.cloudflare.account_id.trim()),
      message: '启用 Cloudflare 后必须填写 Account ID',
      trigger: 'blur'
    }
  ],
  'email.cloudflare.api_token': [
    {
      validator: () => formData.email.provider !== 'cloudflare' || Boolean(formData.email.cloudflare.api_token.trim()),
      message: '启用 Cloudflare 后必须填写 API Token',
      trigger: 'blur'
    }
  ],
  'email.cloudflare.from_address': [
    {
      validator: () => formData.email.provider !== 'cloudflare' || emailPattern.test(formData.email.cloudflare.from_address),
      message: '请输入有效发件邮箱',
      trigger: 'blur'
    }
  ]
}

function createSettingsForm() {
  return {
    site: {
      name: '',
      icon_url: '',
      subtitle: '',
      description: '',
      contact_email: '',
      docs_url: ''
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
      client_secret_configured: false,
      callback_url: '',
      scope: 'read:user user:email read:org',
      admin_org: '',
      api_token: '',
      api_token_configured: false,
      metadata_sync_enabled: true,
      metadata_sync_interval_seconds: 3600
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
        password_configured: false,
        from_address: '',
        ssl: false
      },
      cloudflare: {
        account_id: '',
        api_token: '',
        api_token_configured: false,
        from_address: ''
      },
      daily_limit: 0,
      verification_daily_limit_per_user: 5
    }
  }
}

function applySettings(config = {}) {
  Object.assign(formData.site, config.site || {})
  Object.assign(formData.auth, config.auth || {})
  Object.assign(formData.github, config.github || {})
  Object.assign(formData.market, config.market || {})
  Object.assign(formData.email, config.email || {})
  Object.assign(formData.email.smtp, config.email?.smtp || {})
  Object.assign(formData.email.cloudflare, config.email?.cloudflare || {})
}

function normalizeNumberFields() {
  formData.market.max_plugin_tags = Number(formData.market.max_plugin_tags || 0)
  formData.github.metadata_sync_interval_seconds = Number(formData.github.metadata_sync_interval_seconds || 3600)
  formData.email.smtp.port = Number(formData.email.smtp.port || 587)
  formData.email.daily_limit = Number(formData.email.daily_limit || 0)
  formData.email.verification_daily_limit_per_user = Number(formData.email.verification_daily_limit_per_user || 0)
}

function settingsPayload() {
  normalizeNumberFields()
  return JSON.parse(JSON.stringify(formData))
}

async function loadSettings() {
  loading.value = true
  try {
    await loadCurrentUser()
    if (!isCoreAdmin.value) {
      router.replace('/admin')
      return
    }
    await loadSetupStatus()
    applySettings(await loadSystemSettings())
  } catch (error) {
    message.error(error.message || '加载设置失败')
  } finally {
    loading.value = false
  }
}

function save() {
  if (!isCoreAdmin.value) {
    message.warning('只有核心管理员可以保存设置')
    return
  }
  formRef.value?.validate(async (errors) => {
    if (errors) {
      message.error('请完善设置项')
      return
    }
    saving.value = true
    try {
      const result = await saveSystemSettings(settingsPayload())
      applySettings(result.settings)
      message.success(result.restart_required ? '设置已保存，数据库或 Redis 变更需重启生效' : '设置已保存')
    } catch (error) {
      message.error(error.message || '保存失败')
    } finally {
      saving.value = false
    }
  })
}

async function publishSiteAnnouncement() {
  if (!isCoreAdmin.value) {
    message.warning('只有核心管理员可以发布公告')
    return
  }
  const title = announcementForm.title.trim()
  const body = announcementForm.body.trim()
  if (!title || !body) {
    message.warning('请填写公告标题和内容')
    return
  }
  publishingAnnouncement.value = true
  try {
    await publishAnnouncement({ title, body })
    announcementForm.title = ''
    announcementForm.body = ''
    message.success('公告已发布')
  } catch (error) {
    message.error(error.message || '发布公告失败')
  } finally {
    publishingAnnouncement.value = false
  }
}

async function sendEmailTest() {
  if (!emailPattern.test(testEmail.to)) {
    message.warning('请输入有效的测试收件邮箱')
    return
  }
  testingEmail.value = true
  try {
    await sendTestEmail({
      to: testEmail.to,
      subject: `${formData.site.name || 'AstrBot Community Plugins'} 测试邮件`,
      body: '这是一封来自 AstrBot 社区插件市场的测试邮件。'
    })
    message.success('测试邮件已发送')
  } catch (error) {
    message.error(error.message || '测试邮件发送失败')
  } finally {
    testingEmail.value = false
  }
}

function goBack() {
  router.push('/')
}

function goSetup() {
  router.push('/setup')
}

onMounted(loadSettings)
</script>

<style scoped>
.settings-page {
  min-height: 100vh;
  background: var(--bg-base);
}

.settings-header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: var(--bg-header);
  border-bottom: 1px solid var(--border-base);
  backdrop-filter: blur(18px);
  box-shadow: var(--shadow-sm);
}

.header-content {
  max-width: 1180px;
  margin: 0 auto;
  padding: 18px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.header-left,
.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.eyebrow {
  margin: 0 0 4px;
  color: var(--primary-color);
  font-size: 12px;
  font-weight: 700;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  color: var(--text-primary);
  font-size: 22px;
}

.settings-content {
  max-width: 1040px;
  margin: 0 auto;
  padding: 28px 20px 44px;
}

.settings-form {
  display: grid;
  gap: 18px;
}

.settings-section {
  padding: 22px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

.section-title {
  margin-bottom: 18px;
}

.section-title h2 {
  color: var(--text-primary);
  font-size: 18px;
}

.section-title p {
  margin-top: 6px;
  color: var(--text-tertiary);
  font-size: 14px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 16px;
}

.form-row-full {
  grid-column: 1 / -1;
}

.infra-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.infra-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px;
  color: var(--text-primary);
  background: var(--bg-hover);
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.compact-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.switch-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 10px;
}

.switch-card {
  padding: 12px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-hover);
}

.switch-label {
  display: block;
  margin-bottom: 10px;
  color: var(--text-primary);
  font-weight: 600;
}

.switch-row {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 34px;
  color: var(--text-secondary);
}

.test-email-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  margin-top: 10px;
}

.announcement-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

@media (max-width: 760px) {
  .header-content {
    align-items: flex-start;
    flex-direction: column;
  }

  .header-actions {
    width: 100%;
    justify-content: space-between;
    overflow-x: auto;
    padding-bottom: 2px;
  }

  .form-grid,
  .infra-grid,
  .compact-grid,
  .switch-grid,
  .test-email-row {
    grid-template-columns: 1fr;
  }

  .settings-content {
    padding: 20px 14px 34px;
  }

  .announcement-actions {
    justify-content: stretch;
  }

  .announcement-actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
