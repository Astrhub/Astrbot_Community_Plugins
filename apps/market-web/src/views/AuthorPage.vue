<template>
  <div class="author-page">
    <app-header />

    <main class="author-content">
      <section class="author-summary" aria-labelledby="author-title">
        <router-link to="/" class="back-link" aria-label="返回插件市场">
          <n-icon><arrow-back /></n-icon>
          返回插件市场
        </router-link>
        <div class="author-card">
          <img
            class="author-avatar"
            :src="`https://github.com/${username}.png?size=96`"
            :alt="`${username} 的 GitHub 头像`"
            width="64"
            height="64"
          />
          <div class="author-info">
            <h1 id="author-title">{{ username }}</h1>
            <p>社区插件开发者</p>
          </div>
          <a
            class="github-home-btn"
            :href="`https://github.com/${username}`"
            target="_blank"
            rel="noopener noreferrer"
          >
            <n-icon><logo-github /></n-icon>
            GitHub 主页
          </a>
        </div>
      </section>

      <section class="author-plugins" aria-label="作者已上架插件">
        <div class="section-heading">
          <h2>已上架插件</h2>
          <span>共 {{ plugins.length }} 个</span>
        </div>

        <div v-if="isLoading" class="loading-container">
          <div class="loading-dots" aria-label="正在加载作者插件">
            <span></span><span></span><span></span>
          </div>
        </div>

        <div v-else-if="errorMessage" class="empty-state">
          <n-icon size="40"><alert-circle-outline /></n-icon>
          <h2>加载失败</h2>
          <p>{{ errorMessage }}</p>
        </div>

        <div v-else-if="!plugins.length" class="empty-state">
          <n-icon size="40"><file-tray-outline /></n-icon>
          <h2>暂无已上架插件</h2>
          <p>该作者目前还没有已上架的插件。</p>
        </div>

        <div v-else class="plugins-grid">
          <plugin-card
            v-for="(plugin, index) in plugins"
            :key="plugin.id"
            :plugin="plugin"
            :index="index"
          />
        </div>
      </section>
    </main>

    <app-footer />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { NIcon } from "naive-ui";
import { ArrowBack, LogoGithub, AlertCircleOutline, FileTrayOutline } from "@vicons/ionicons5";
import AppHeader from "@/components/AppHeader.vue";
import AppFooter from "@/components/AppFooter.vue";
import PluginCard from "@/components/PluginCard.vue";
import { useSeo, absoluteSiteUrl } from "@/composables/useSeo";
import type { Plugin } from "@/types";

const route = useRoute();
const username = computed(() => String(route.params.username || "").trim());
const plugins = shallowRef<Plugin[]>([]);
const isLoading = shallowRef(true);
const errorMessage = shallowRef("");

useSeo({
  title: computed(() => `${username.value} 的插件`),
  description: computed(() => `查看 ${username.value} 在 Astrhub 插件市场发布的所有插件。`),
  path: computed(() => `/author/${encodeURIComponent(username.value)}`),
  jsonLd: computed(() => ({
    "@context": "https://schema.org",
    "@type": "ProfilePage",
    mainEntity: {
      "@type": "Person",
      name: username.value,
      url: `https://github.com/${username.value}`,
    },
    url: absoluteSiteUrl(`/author/${encodeURIComponent(username.value)}`),
  })),
});

async function loadAuthorPlugins() {
  if (!username.value) return;
  isLoading.value = true;
  errorMessage.value = "";
  try {
    const response = await fetch(`/v1/authors/${encodeURIComponent(username.value)}/plugins`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || "作者插件加载失败");
    plugins.value = data.items || [];
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "作者插件加载失败";
  } finally {
    isLoading.value = false;
  }
}

onMounted(() => {
  void loadAuthorPlugins();
});

watch(username, () => {
  void loadAuthorPlugins();
});
</script>

<style scoped>
.author-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-base);
  color: var(--text-primary);
}

.author-content {
  width: min(1824px, calc(100% - 96px));
  margin: 0 auto;
  flex: 1;
  padding: 24px 0 48px;
}

.author-summary {
  margin-bottom: 24px;
}

.back-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 13px;
  text-decoration: none;
  margin-bottom: 16px;
}

.back-link:hover {
  color: var(--primary-color);
}

.author-card {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 24px 28px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.author-avatar {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  border: 1px solid var(--border-base);
  flex-shrink: 0;
}

.author-info {
  flex: 1;
  min-width: 0;
}

.author-info h1 {
  margin: 0;
  font-size: 22px;
  color: var(--text-primary);
  line-height: 1.3;
}

.author-info p {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--text-tertiary);
}

.github-home-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  font-size: 14px;
  color: var(--text-primary);
  background: var(--bg-base);
  border: 1px solid var(--border-base);
  border-radius: 6px;
  text-decoration: none;
  transition: all 0.2s ease;
}

.github-home-btn:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  padding: 0 4px;
}

.section-heading h2 {
  margin: 0;
  font-size: 18px;
  color: var(--text-primary);
}

.section-heading span {
  font-size: 13px;
  color: var(--text-tertiary);
}

.plugins-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 20px;
}

.loading-container,
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 64px 0;
  color: var(--text-tertiary);
  text-align: center;
}

.empty-state h2 {
  margin: 16px 0 8px;
  font-size: 18px;
  color: var(--text-primary);
}

.empty-state p {
  margin: 0;
  font-size: 14px;
}

.loading-dots {
  display: flex;
  gap: 8px;
}

.loading-dots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--primary-color);
  animation: bounce 1.4s infinite ease-in-out both;
}

.loading-dots span:nth-child(1) {
  animation-delay: -0.32s;
}
.loading-dots span:nth-child(2) {
  animation-delay: -0.16s;
}

@keyframes bounce {
  0%,
  80%,
  100% {
    transform: scale(0);
  }
  40% {
    transform: scale(1);
  }
}

@media (max-width: 768px) {
  .author-content {
    width: calc(100% - 32px);
    padding-top: 16px;
  }

  .author-card {
    flex-wrap: wrap;
    gap: 16px;
    padding: 20px;
  }

  .github-home-btn {
    width: 100%;
    justify-content: center;
  }

  .plugins-grid {
    grid-template-columns: 1fr;
  }
}
</style>
