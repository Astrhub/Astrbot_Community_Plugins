<script setup lang="ts">
import { computed, defineAsyncComponent, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { storeToRefs } from "pinia";
import { NButton, NCard, NIcon, NResult, NSpin, NTag, useMessage } from "naive-ui";
import {
  ArrowBackOutline,
  ChatbubbleEllipsesOutline,
  CopyOutline,
  HeartOutline,
  LogoGithub,
  StarSharp,
} from "@vicons/ionicons5";
import { PLUGIN_CATEGORY_LABELS, usePluginStore } from "../stores/plugins";
import type { PluginDetail } from "../types";
import { resolvePluginLogoUrl } from "../utils/github";
import { DEFAULT_OG_IMAGE, useSeo } from "../composables/useSeo";
import { useExternalOpenConfirm } from "../composables/useExternalOpenConfirm";
import AppFooter from "../components/AppFooter.vue";
import AppHeader from "../components/AppHeader.vue";

const PluginDetails = defineAsyncComponent(() => import("../components/PluginDetails.vue"));
const route = useRoute();
const store = usePluginStore();
const { currentUser, siteConfig } = storeToRefs(store);
const message = useMessage();
const { confirmExternalOpen } = useExternalOpenConfirm();
const plugin = shallowRef<PluginDetail | null>(null);
const loading = shallowRef(true);
const notFound = shallowRef(false);
const liking = shallowRef(false);
const copied = shallowRef(false);

const pluginName = computed(() => String(route.params.name || ""));
const displayName = computed(() => plugin.value?.display_name || plugin.value?.name || "插件详情");
const description = computed(
  () => plugin.value?.desc || plugin.value?.short_desc || "查看 AstrBot 社区插件详情。",
);
const canonicalPath = computed(() => `/plugin/${encodeURIComponent(pluginName.value)}`);
const categoryLabel = computed(() =>
  plugin.value ? PLUGIN_CATEGORY_LABELS[plugin.value.category] || "其他" : "其他",
);
const likesEnabled = computed(() => Boolean(siteConfig.value.market?.likes_enabled));
const jsonLd = computed(() => {
  if (!plugin.value) return null;
  return {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: displayName.value,
    applicationCategory: "UtilitiesApplication",
    operatingSystem: "AstrBot",
    author: { "@type": "Person", name: plugin.value.author || "AstrBot 社区开发者" },
    softwareVersion: plugin.value.version || undefined,
    description: description.value,
    dateModified: plugin.value.updated_at || plugin.value.created_at || undefined,
    url: `https://plugins.eloina.cn${canonicalPath.value}`,
    interactionStatistic: [
      {
        "@type": "InteractionCounter",
        interactionType: "https://schema.org/LikeAction",
        userInteractionCount: plugin.value.likes || 0,
      },
      {
        "@type": "InteractionCounter",
        interactionType: "https://schema.org/CommentAction",
        userInteractionCount: plugin.value.comments_count || 0,
      },
    ],
  };
});

useSeo({
  title: displayName,
  description,
  path: canonicalPath,
  image: DEFAULT_OG_IMAGE,
  type: "article",
  robots: computed(() => (notFound.value ? "noindex,nofollow" : "index,follow")),
  jsonLdId: "software-application-jsonld",
  jsonLd,
});

watch(
  pluginName,
  async (name) => {
    loading.value = true;
    notFound.value = false;
    plugin.value = null;
    try {
      plugin.value = await store.loadPluginDetail(name);
    } catch {
      notFound.value = true;
    } finally {
      loading.value = false;
    }
  },
  { immediate: true },
);

async function toggleLike(): Promise<void> {
  if (!plugin.value?.id) return;
  if (!currentUser.value) {
    message.warning("请先登录后再点赞");
    return;
  }
  liking.value = true;
  try {
    const updated = await (plugin.value.liked
      ? store.unlikePlugin(plugin.value.id)
      : store.likePlugin(plugin.value.id));
    plugin.value = { ...plugin.value, ...updated } as PluginDetail;
    store.updatePluginInList(plugin.value);
  } catch (error) {
    message.error(error instanceof Error ? error.message : "点赞操作失败");
  } finally {
    liking.value = false;
  }
}

async function copyRepo(): Promise<void> {
  const repo = String(plugin.value?.repo || "");
  if (!repo) return;
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(repo);
    } else {
      const input = document.createElement("textarea");
      input.value = repo;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    copied.value = true;
    window.setTimeout(() => (copied.value = false), 1600);
  } catch {
    message.error("复制仓库地址失败");
  }
}

function updatePlugin(updated: PluginDetail): void {
  plugin.value = updated;
}
</script>

<template>
  <div class="plugin-page-shell">
    <app-header />
    <main class="plugin-page">
      <router-link class="back-link" to="/">
        <n-icon><arrow-back-outline /></n-icon>
        返回插件墙
      </router-link>

      <div v-if="loading" class="plugin-state"><n-spin size="large" /></div>
      <n-result
        v-else-if="notFound"
        status="404"
        title="插件不存在"
        description="该插件未上架、已下架或名称有误。"
      >
        <template #footer><router-link to="/">浏览其他插件</router-link></template>
      </n-result>
      <div v-else-if="plugin" class="plugin-layout">
        <main class="plugin-main">
          <section class="plugin-intro" aria-labelledby="plugin-title">
            <div class="plugin-intro__identity">
              <img
                :src="resolvePluginLogoUrl(plugin)"
                :alt="`${displayName} 插件图标`"
                width="88"
                height="88"
              />
              <div class="plugin-intro__copy">
                <p class="plugin-kicker">AstrBot 社区插件</p>
                <h1 id="plugin-title">{{ displayName }}</h1>
                <p class="plugin-id">{{ plugin.id }}</p>
              </div>
            </div>
            <p class="plugin-description">{{ description }}</p>
            <div class="plugin-tags">
              <n-tag v-for="tag in plugin.tags" :key="tag" size="small" :bordered="false">
                {{ tag }}
              </n-tag>
            </div>
          </section>

          <section class="plugin-readme-section" aria-label="README 与评论">
            <plugin-details embedded :plugin="plugin" @updated="updatePlugin" />
          </section>
        </main>

        <aside class="plugin-sidebar" aria-label="插件信息">
          <n-card class="plugin-info-card" :bordered="false">
            <dl class="plugin-facts">
              <div>
                <dt>版本</dt>
                <dd>{{ plugin.version || "未知" }}</dd>
              </div>
              <div>
                <dt>作者</dt>
                <dd>{{ plugin.author || "社区开发者" }}</dd>
              </div>
              <div>
                <dt>分类</dt>
                <dd>{{ categoryLabel }}</dd>
              </div>
            </dl>

            <div class="plugin-metrics" aria-label="插件互动数据">
              <span class="plugin-metric plugin-metric--star">
                <n-icon><star-sharp /></n-icon>{{ plugin.stars || 0 }}
              </span>
              <span class="plugin-metric plugin-metric--like">
                <n-icon><heart-outline /></n-icon>{{ plugin.likes || 0 }}
              </span>
              <span class="plugin-metric plugin-metric--comment">
                <n-icon><chatbubble-ellipses-outline /></n-icon>{{ plugin.comments_count || 0 }}
              </span>
            </div>

            <div class="plugin-sidebar-actions">
              <n-button v-if="likesEnabled" type="primary" :loading="liking" @click="toggleLike">
                <template #icon
                  ><n-icon><heart-outline /></n-icon
                ></template>
                {{ plugin.liked ? "取消喜欢" : "喜欢" }}
              </n-button>
              <span v-else class="sidebar-muted">点赞已关闭</span>
              <n-button v-if="plugin.repo" secondary @click="confirmExternalOpen(plugin.repo)">
                <template #icon
                  ><n-icon><logo-github /></n-icon
                ></template>
                查看仓库
              </n-button>
            </div>

            <div v-if="plugin.repo" class="repo-copy">
              <code>{{ plugin.repo }}</code>
              <n-button
                circle
                quaternary
                :aria-label="copied ? '已复制' : '复制仓库地址'"
                @click="copyRepo"
              >
                <template #icon
                  ><n-icon><copy-outline /></n-icon
                ></template>
              </n-button>
            </div>
          </n-card>
        </aside>
      </div>
    </main>
    <app-footer />
  </div>
</template>

<style scoped>
.plugin-page {
  width: min(1256px, calc(100% - 48px));
  margin: 0 auto;
  padding: 42px 0 24px;
}
.back-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-tertiary);
  font-size: 12px;
  text-decoration: none;
  margin-bottom: 30px;
}
.plugin-state {
  min-height: 60vh;
  display: grid;
  place-items: center;
}
.plugin-layout {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 46px;
  align-items: start;
}

.plugin-main {
  min-width: 0;
}

.plugin-intro {
  padding-bottom: 22px;
  border-bottom: 1px solid var(--border-base);
}

.plugin-intro__identity {
  display: flex;
  align-items: center;
  gap: 16px;
}

.plugin-intro__identity img {
  border-radius: 8px;
  object-fit: cover;
  border: 1px solid var(--border-base);
  background: var(--logo-bg);
}

.plugin-intro__copy {
  min-width: 0;
}

.plugin-kicker,
.plugin-id {
  color: var(--text-tertiary);
  margin: 0;
  overflow-wrap: anywhere;
}

.plugin-intro h1 {
  margin: 4px 0;
  color: var(--text-primary);
  font-size: 32px;
  line-height: 1.2;
}

.plugin-description {
  margin: 20px 0 0;
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.8;
}

.plugin-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}

.plugin-readme-section {
  min-width: 0;
  padding-top: 24px;
}

.plugin-sidebar {
  position: sticky;
  top: 84px;
  min-width: 0;
}

.plugin-info-card {
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: none;
}

.plugin-facts {
  display: grid;
  gap: 14px;
  margin: 0;
}

.plugin-facts div {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 12px;
  align-items: baseline;
}

.plugin-facts dt {
  color: var(--text-tertiary);
  font-size: 13px;
}

.plugin-facts dd {
  margin: 0;
  color: var(--text-primary);
  font-weight: 700;
  overflow-wrap: anywhere;
}

.plugin-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin: 22px 0;
  padding: 16px 0;
  border-top: 1px solid var(--border-base);
  border-bottom: 1px solid var(--border-base);
}

.plugin-metric {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 700;
}

.plugin-metric--star {
  color: var(--metric-star);
}
.plugin-metric--like {
  color: var(--metric-like);
}
.plugin-metric--comment {
  color: var(--metric-comment);
}

.plugin-sidebar-actions {
  display: grid;
  gap: 10px;
}

.sidebar-muted {
  color: var(--text-tertiary);
  font-size: 13px;
}

.repo-copy {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 34px;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
  padding: 8px 8px 8px 10px;
  border: 1px solid var(--border-base);
  border-radius: 6px;
  background: var(--bg-hover);
}

.repo-copy code {
  min-width: 0;
  color: var(--text-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 900px) {
  .plugin-layout {
    grid-template-columns: 1fr;
  }

  .plugin-sidebar {
    position: static;
    order: -1;
  }
}

@media (max-width: 640px) {
  .plugin-page {
    width: min(100% - 28px, 1256px);
    padding-top: 18px;
  }

  .plugin-intro__identity img {
    width: 72px;
    height: 72px;
  }

  .plugin-intro h1 {
    font-size: 26px;
  }

  .plugin-description {
    font-size: 15px;
  }
}
</style>
