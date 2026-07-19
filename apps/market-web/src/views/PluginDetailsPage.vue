<script setup lang="ts">
import { computed, defineAsyncComponent, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { NButton, NIcon, NResult, NSpin, NTag } from "naive-ui";
import { ArrowBackOutline, LogoGithub } from "@vicons/ionicons5";
import { usePluginStore } from "../stores/plugins";
import type { PluginDetail } from "../types";
import { resolvePluginLogoUrl } from "../utils/github";
import { DEFAULT_OG_IMAGE, useSeo } from "../composables/useSeo";

const PluginDetails = defineAsyncComponent(() => import("../components/PluginDetails.vue"));
const route = useRoute();
const store = usePluginStore();
const plugin = shallowRef<PluginDetail | null>(null);
const loading = shallowRef(true);
const notFound = shallowRef(false);
const showInteractiveDetails = shallowRef(false);

const pluginName = computed(() => String(route.params.name || ""));
const displayName = computed(() => plugin.value?.display_name || plugin.value?.name || "插件详情");
const description = computed(
  () => plugin.value?.desc || plugin.value?.short_desc || "查看 AstrBot 社区插件详情。",
);
const canonicalPath = computed(() => `/plugin/${encodeURIComponent(pluginName.value)}`);
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
</script>

<template>
  <main class="plugin-page">
    <router-link class="back-link" to="/">
      <n-icon><arrow-back-outline /></n-icon>
      返回插件市场
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
    <article v-else-if="plugin" class="plugin-profile">
      <header class="plugin-profile__header">
        <img
          :src="resolvePluginLogoUrl(plugin)"
          :alt="`${displayName} 插件图标`"
          width="96"
          height="96"
        />
        <div>
          <p class="plugin-kicker">AstrBot 社区插件</p>
          <h1>{{ displayName }}</h1>
          <p class="plugin-id">{{ plugin.id }}</p>
        </div>
        <n-tag type="success" :bordered="false">{{ plugin.version || "未知版本" }}</n-tag>
      </header>
      <p class="plugin-description">{{ description }}</p>
      <dl class="plugin-facts">
        <div>
          <dt>作者</dt>
          <dd>{{ plugin.author || "社区开发者" }}</dd>
        </div>
        <div>
          <dt>Stars</dt>
          <dd>{{ plugin.stars || 0 }}</dd>
        </div>
        <div>
          <dt>点赞</dt>
          <dd>{{ plugin.likes || 0 }}</dd>
        </div>
        <div>
          <dt>评论</dt>
          <dd>{{ plugin.comments_count || 0 }}</dd>
        </div>
      </dl>
      <div class="plugin-tags">
        <n-tag v-for="tag in plugin.tags" :key="tag" size="small" :bordered="false">
          {{ tag }}
        </n-tag>
      </div>
      <div class="plugin-actions">
        <n-button type="primary" @click="showInteractiveDetails = true"
          >查看 README 与评论</n-button
        >
        <n-button
          v-if="plugin.repo"
          tag="a"
          :href="plugin.repo"
          target="_blank"
          rel="noopener noreferrer"
          secondary
        >
          <template #icon
            ><n-icon><logo-github /></n-icon
          ></template>
          GitHub 仓库
        </n-button>
      </div>
    </article>
    <plugin-details v-if="plugin" v-model:show="showInteractiveDetails" :plugin="plugin" />
  </main>
</template>

<style scoped>
.plugin-page {
  width: min(960px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 64px;
}
.back-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--primary-color);
  text-decoration: none;
  margin-bottom: 20px;
}
.plugin-state {
  min-height: 60vh;
  display: grid;
  place-items: center;
}
.plugin-profile {
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  padding: clamp(20px, 4vw, 40px);
  box-shadow: var(--shadow-sm);
}
.plugin-profile__header {
  display: grid;
  grid-template-columns: 96px 1fr auto;
  gap: 20px;
  align-items: center;
}
.plugin-profile__header img {
  border-radius: 8px;
  object-fit: cover;
}
.plugin-kicker,
.plugin-id {
  color: var(--text-tertiary);
  margin: 0;
}
.plugin-profile h1 {
  margin: 4px 0;
  font-size: clamp(28px, 5vw, 44px);
}
.plugin-description {
  margin: 28px 0;
  color: var(--text-secondary);
  font-size: 17px;
  line-height: 1.8;
}
.plugin-facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.plugin-facts div {
  border-top: 1px solid var(--border-base);
  padding-top: 12px;
}
.plugin-facts dt {
  color: var(--text-tertiary);
  font-size: 13px;
}
.plugin-facts dd {
  margin: 4px 0 0;
  font-weight: 700;
}
.plugin-tags,
.plugin-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 24px;
}
.plugin-actions a {
  text-decoration: none;
}
@media (max-width: 640px) {
  .plugin-profile__header {
    grid-template-columns: 72px 1fr;
  }
  .plugin-profile__header img {
    width: 72px;
    height: 72px;
  }
  .plugin-profile__header > .n-tag {
    grid-column: 1 / -1;
    justify-self: start;
  }
  .plugin-facts {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
