<template>
  <div class="submit-plugin-page">
    <n-layout-header class="page-header">
      <div class="header-content">
        <div class="header-left">
          <n-button quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <h1>提交插件</h1>
            <p>插件将提交到社区市场审核队列，数据保存在市场服务器。</p>
          </div>
        </div>
        <div class="header-right">
          <n-button quaternary circle @click="toggleTheme" :aria-label="isDarkMode ? '切换浅色模式' : '切换深色模式'">
            <template #icon>
              <n-icon>
                <sunny v-if="isDarkMode" />
                <moon v-else />
              </n-icon>
            </template>
          </n-button>
          <n-button
            v-if="!currentUser && siteConfig.auth.github_login_enabled"
            type="primary"
            @click="loginWithGithub"
          >
            <template #icon>
              <n-icon><logo-github /></n-icon>
            </template>
            GitHub 登录
          </n-button>
        </div>
      </div>
    </n-layout-header>

    <main class="submit-content">
      <section class="submit-aside">
        <n-card :bordered="false" class="info-panel">
          <h2>上架规则</h2>
          <p>提交插件需要使用 GitHub OAuth 登录，用于校验仓库归属。</p>
          <ul>
            <li>仓库必须是公开 GitHub 仓库。</li>
            <li>插件名建议使用 `astrbot_plugin_` 前缀。</li>
            <li>核心管理员拥有公告和管理员任免权限。</li>
            <li>普通管理员只处理插件审核、上下架、评论治理和用户禁言。</li>
          </ul>
        </n-card>
      </section>

      <section class="submit-main">
        <n-card :bordered="false" class="form-card">
          <template #header>
            <div class="form-title">
              <h2>插件信息</h2>
              <n-tag v-if="currentUser" type="success" :bordered="false">
                已登录：{{ currentUser.github_login || currentUser.login }}
              </n-tag>
              <n-tag v-else-if="!siteConfig.auth.github_login_enabled" type="warning" :bordered="false">GitHub 登录未开启</n-tag>
              <n-tag v-else type="warning" :bordered="false">需要 GitHub 登录</n-tag>
            </div>
          </template>

          <n-form ref="formRef" :model="formData" :rules="rules" label-placement="top">
            <n-grid :x-gap="16" :y-gap="10" :cols="2" responsive="screen">
              <n-grid-item span="2 m:1">
                <n-form-item label="插件名" path="name">
                  <n-input v-model:value="formData.name" placeholder="astrbot_plugin_example" />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="展示名称" path="display_name">
                  <n-input v-model:value="formData.display_name" placeholder="给用户看的名称" />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="GitHub 仓库地址" path="repo">
                  <n-input v-model:value="formData.repo" placeholder="https://github.com/owner/repository" />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="插件简介" path="desc">
                  <n-input
                    v-model:value="formData.desc"
                    type="textarea"
                    placeholder="一句话说明插件能做什么"
                    :maxlength="120"
                    :show-count="true"
                    :rows="4"
                    :resizable="false"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="作者显示名" path="author">
                  <n-input v-model:value="formData.author" placeholder="默认建议与 GitHub 用户名一致" />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="社交链接" path="social_link">
                  <n-input v-model:value="formData.social_link" placeholder="可选，个人主页或 GitHub 主页" />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="标签" path="tags">
                  <n-dynamic-tags v-model:value="formData.tags" :max="6" />
                </n-form-item>
              </n-grid-item>
            </n-grid>
          </n-form>

          <template #footer>
            <div class="form-actions">
              <n-button quaternary @click="goBack">取消</n-button>
              <n-button type="primary" :loading="submitting" :disabled="!currentUser" @click="handleSubmit">
                提交审核
              </n-button>
            </div>
          </template>
        </n-card>
      </section>
    </main>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import {
  NButton,
  NCard,
  NDynamicTags,
  NForm,
  NFormItem,
  NGrid,
  NGridItem,
  NIcon,
  NInput,
  NLayoutHeader,
  NTag,
  useMessage
} from 'naive-ui'
import { ArrowBack, LogoGithub, Moon, Sunny } from '@vicons/ionicons5'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { isDarkMode, currentUser, siteConfig } = storeToRefs(store)
const { loginWithGithub, toggleTheme } = store
const formRef = ref(null)
const submitting = ref(false)

const formData = reactive({
  name: '',
  display_name: '',
  desc: '',
  author: '',
  repo: '',
  tags: [],
  social_link: ''
})

const rules = {
  name: [
    { required: true, message: '请输入插件名', trigger: 'blur' },
    { pattern: /^astrbot_plugin_[a-z0-9_-]+$/i, message: '建议以 astrbot_plugin_ 开头，仅含字母、数字、下划线、短横线', trigger: 'blur' }
  ],
  display_name: {
    required: true,
    message: '请输入展示名称',
    trigger: 'blur'
  },
  desc: [
    { required: true, message: '请输入插件简介', trigger: 'blur' },
    {
      validator: (_, value) => Array.from((value || '').toString()).length <= 120,
      message: '插件简介最多 120 字',
      trigger: ['input', 'blur']
    }
  ],
  author: {
    required: true,
    message: '请输入作者显示名',
    trigger: 'blur'
  },
  repo: [
    { required: true, message: '请输入 GitHub 仓库地址', trigger: 'blur' },
    { pattern: /^https:\/\/github\.com\/[\w-]+\/[\w.-]+\/?$/, message: '请输入有效的 GitHub 仓库地址', trigger: 'blur' }
  ],
  tags: [
    {
      validator: (_, value) => !Array.isArray(value) || value.length <= 6,
      message: '标签最多 6 个',
      trigger: ['change', 'blur']
    }
  ]
}

const goBack = () => {
  router.back()
}

const handleSubmit = () => {
  formRef.value?.validate(async (errors) => {
    if (errors) {
      message.error('请完善必填信息')
      return
    }

    submitting.value = true
    try {
      await store.submitPlugin({ ...formData })
      message.success('已提交审核')
      router.push('/')
    } catch (error) {
      message.error(error.message || '提交失败')
    } finally {
      submitting.value = false
    }
  })
}
</script>

<style scoped>
.submit-plugin-page {
  min-height: 100vh;
  background: var(--bg-base);
}

.page-header {
  background: var(--bg-header);
  backdrop-filter: blur(18px);
  border-bottom: 1px solid var(--border-base);
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
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-left h1 {
  margin: 0;
  font-size: 22px;
  color: var(--text-primary);
}

.header-left p {
  margin: 4px 0 0;
  color: var(--text-tertiary);
  font-size: 14px;
}

.submit-content {
  max-width: 1180px;
  margin: 0 auto;
  padding: 32px 20px;
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  gap: 24px;
}

.info-panel,
.form-card {
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  box-shadow: var(--shadow-sm);
}

.info-panel h2,
.form-title h2 {
  margin: 0;
  color: var(--text-primary);
}

.info-panel p,
.info-panel li {
  color: var(--text-secondary);
  line-height: 1.7;
}

.info-panel ul {
  margin: 16px 0 0;
  padding-left: 18px;
}

.form-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

@media (max-width: 860px) {
  .submit-content {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .header-content {
    align-items: flex-start;
    flex-direction: column;
  }

  .header-right {
    width: 100%;
    justify-content: space-between;
  }

  .submit-content {
    padding: 20px 14px;
  }
}
</style>
