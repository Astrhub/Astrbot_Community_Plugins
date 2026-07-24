<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useHead } from "@unhead/vue";
import { useRoute, useRouter } from "vue-router";
import { storeToRefs } from "pinia";
import { darkTheme, NConfigProvider, NDialogProvider, NMessageProvider } from "naive-ui";
import BackToTop from "./components/BackToTop.vue";
import IrisMask from "./components/IrisMask.vue";
import { darkThemeOverrides, lightThemeOverrides } from "./config/darkTheme";
import { highlightConfig } from "./plugins/highlight";
import { usePluginStore } from "./stores/plugins";

const store = usePluginStore();
const { irisMaskActive, irisMaskPosition, isDarkMode } = storeToRefs(store);
const route = useRoute();
const router = useRouter();
const backToTopHiddenRoutes = new Set([
  "/submit",
  "/settings",
  "/admin",
  "/admin/settings",
  "/admin/plugins",
  "/plugin-workbench",
]);
const showBackToTop = computed(() => !backToTopHiddenRoutes.has(route.path));

useHead(() => ({
  meta: route.meta.noindex ? [{ name: "robots", content: "noindex,nofollow" }] : [],
}));

onMounted(async () => {
  store.initTheme();
  await store.loadSiteConfig();
  const status = await store.loadSetupStatus();
  if (status.required) {
    if (route.path !== "/setup") await router.replace("/setup");
    return;
  }
  await Promise.all([store.loadPlugins(), store.loadCurrentUser()]);
});
</script>

<template>
  <n-config-provider
    :theme="isDarkMode ? darkTheme : null"
    :theme-overrides="isDarkMode ? darkThemeOverrides : lightThemeOverrides"
    :hljs="highlightConfig.hljs"
  >
    <n-message-provider>
      <n-dialog-provider>
        <div class="app-container" :class="{ dark: isDarkMode }">
          <back-to-top v-if="showBackToTop" />
          <router-view />
        </div>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
  <iris-mask :is-active="irisMaskActive" :position="irisMaskPosition" />
</template>

<style>
body {
  margin: 0;
  font-family:
    -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

.app-container,
.main-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-base);
}
</style>
