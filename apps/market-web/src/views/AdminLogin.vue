<template>
  <div class="admin-login-page">
    <n-layout-header class="admin-login-header">
      <div class="header-content">
        <n-button quaternary circle @click="goHome" aria-label="返回首页">
          <template #icon>
            <n-icon><arrow-back /></n-icon>
          </template>
        </n-button>
        <theme-mode-button circle />
      </div>
    </n-layout-header>

    <main class="admin-login-content">
      <section class="admin-login-panel">
        <div class="panel-title">
          <p class="eyebrow">核心管理员</p>
          <h1>后台登录</h1>
        </div>
        <n-form :model="loginForm" label-placement="top">
          <n-form-item label="内部账号">
            <n-input v-model:value="loginForm.username" placeholder="admin" />
          </n-form-item>
          <n-form-item label="密码">
            <n-input
              v-model:value="loginForm.password"
              type="password"
              show-password-on="click"
              placeholder="核心管理员密码"
              @keyup.enter="submitInternalLogin"
            />
          </n-form-item>
          <n-button
            type="primary"
            block
            :loading="isLoggingIn"
            :disabled="!canSubmit"
            @click="submitInternalLogin"
          >
            登录后台
          </n-button>
        </n-form>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import {
  NButton,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NLayoutHeader,
  useMessage
} from 'naive-ui'
import { ArrowBack } from '@vicons/ionicons5'
import ThemeModeButton from '@/components/ThemeModeButton.vue'
import { usePluginStore } from '@/stores/plugins'

const router = useRouter()
const message = useMessage()
const store = usePluginStore()
const { currentUser } = storeToRefs(store)
const { loadCurrentUser, loginWithPassword } = store

const isLoggingIn = ref(false)
const loginForm = ref({ username: 'admin', password: '' })
const canSubmit = computed(() => Boolean(loginForm.value.username.trim() && loginForm.value.password))

async function submitInternalLogin() {
  if (!canSubmit.value) return
  isLoggingIn.value = true
  try {
    const user = await loginWithPassword({
      username: loginForm.value.username.trim(),
      password: loginForm.value.password
    })
    if (user.role !== 'core_admin') {
      await store.logout()
      message.error('只有核心管理员可以登录后台')
      return
    }
    message.success('已登录后台')
    router.replace('/admin/settings')
  } catch (error) {
    message.error(error.message || '登录失败')
  } finally {
    isLoggingIn.value = false
  }
}

function goHome() {
  router.push('/')
}

onMounted(async () => {
  await loadCurrentUser()
  if (currentUser.value?.role === 'core_admin') {
    router.replace('/admin/settings')
  }
})
</script>

<style scoped>
.admin-login-page {
  min-height: 100vh;
  background: var(--body-color);
}

.admin-login-header {
  background: var(--card-color);
  border-bottom: 1px solid var(--border-base);
}

.header-content {
  max-width: 960px;
  margin: 0 auto;
  padding: 14px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.admin-login-content {
  min-height: calc(100vh - 61px);
  display: grid;
  place-items: center;
  padding: 24px;
}

.admin-login-panel {
  width: min(100%, 380px);
  padding: 28px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--card-color);
  box-shadow: var(--shadow-2);
}

.panel-title {
  margin-bottom: 22px;
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 700;
}

h1 {
  margin: 0;
  color: var(--text-primary);
  font-size: 26px;
}
</style>
