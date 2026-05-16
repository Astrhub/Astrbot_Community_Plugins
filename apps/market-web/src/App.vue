<template>
  <n-config-provider
    :theme="isDarkMode ? darkTheme : null"
    :theme-overrides="isDarkMode ? darkThemeOverrides : lightThemeOverrides"
    :hljs="highlightConfig.hljs"
  >
    <n-message-provider>
      <div class="app-container" :class="{ dark: isDarkMode }">
        <back-to-top v-if="!isFormPage" />
        <router-view />
      </div>
    </n-message-provider>
  </n-config-provider>
  <iris-mask :is-active="irisMaskActive" :position="irisMaskPosition" />
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { darkTheme, NConfigProvider, NMessageProvider } from 'naive-ui'
import { highlightConfig } from './plugins/highlight'

import IrisMask from './components/IrisMask.vue'
import BackToTop from './components/BackToTop.vue'

import { darkThemeOverrides, lightThemeOverrides } from './config/darkTheme'
import { usePluginStore } from './stores/plugins'

const store = usePluginStore()
const {
  irisMaskActive,
  irisMaskPosition,
  isDarkMode,
} = storeToRefs(store)

const route = useRoute()
const router = useRouter()
const isFormPage = computed(() => ['/submit', '/settings'].includes(route.path))

onMounted(async () => {
  store.initTheme()
  await store.loadSiteConfig()
  const status = await store.loadSetupStatus()
  if (status.required) {
    if (route.path !== '/setup') await router.replace('/setup')
    return
  }
  await Promise.all([store.loadPlugins(), store.loadCurrentUser()])
})
</script>

<style>
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

.app-container {
  min-height: 100vh;
  background: var(--body-color, #f5f5f5);
  display: flex;
  flex-direction: column;
}

.main-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

@keyframes gridAppear {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

.plugins-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 28px;
  padding: 20px;
  max-width: 100%;
  margin: 0 auto;
  animation: gridAppear 0.3s ease-out;
  animation-delay: 0.7s;
  animation-fill-mode: backwards;
}

@media (max-width: 768px) {
  .app-container {
    padding: 0;
  }
  
  .plugins-grid {
    grid-template-columns: 1fr;
    gap: 16px;
    padding: 16px;
  }
}
</style>
