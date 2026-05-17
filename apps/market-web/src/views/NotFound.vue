<template>
  <main class="not-found-page">
    <n-result
      status="404"
      title="404"
      description="这个页面不存在或已被移除。"
    >
      <template #footer>
        <n-button type="primary" @click="goHome">
          <template #icon>
            <n-icon><home-outline /></n-icon>
          </template>
          返回首页
        </n-button>
      </template>
    </n-result>
  </main>
</template>

<script setup>
import { NButton, NIcon, NResult } from 'naive-ui'
import { HomeOutline } from '@vicons/ionicons5'
import { useRouter } from 'vue-router'

const router = useRouter()

function goHome() {
  router.push('/')
}
</script>

<style scoped>
.not-found-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--body-color);
}
</style>
