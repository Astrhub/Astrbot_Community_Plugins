<template>
  <div class="author-page">
    <app-header />

    <main class="author-content">
      <section class="author-summary" aria-labelledby="author-title">
        <router-link to="/" class="back-link" aria-label="返回插件市场">
          <n-icon><arrow-back /></n-icon>
          返回插件市场
        </router-link>

        <div class="author-card" :class="{ 'author-card--owner': isOwnProfile }">
          <div class="author-card__main">
            <img
              v-if="authorAvatarUrl && !avatarLoadFailed"
              class="author-avatar"
              :src="authorAvatarUrl"
              :alt="`${authorDisplayName} 的头像`"
              width="64"
              height="64"
              referrerpolicy="no-referrer"
              @error="avatarLoadFailed = true"
            />
            <span v-else class="author-avatar author-avatar--fallback" aria-hidden="true">
              <n-icon size="30"><person-outline /></n-icon>
            </span>

            <div class="author-info">
              <div class="author-title-row">
                <h1 id="author-title">{{ authorDisplayName }}</h1>
                <span v-if="isOwnProfile" class="owner-badge">本人主页</span>
              </div>

              <p class="author-subtitle">
                <span v-if="authorDisplayName !== canonicalUsername" class="author-handle">
                  @{{ canonicalUsername }}
                </span>
                <span>{{ authorRoleLabel }}</span>
                <template v-if="isOwnProfile && ownerSummaryText">
                  <span aria-hidden="true">·</span>
                  <span class="author-stats">{{ ownerSummaryText }}</span>
                </template>
              </p>
            </div>
          </div>

          <div
            v-if="isOwnProfile || externalProfiles.length || authorSocialLinks.length"
            class="author-top-actions"
          >
            <template v-if="externalProfiles.length === 1 && singleExternalProfile">
              <a
                class="header-action-btn"
                :href="singleExternalProfile.url"
                target="_blank"
                rel="noopener noreferrer"
                @click.prevent="openExternalLink(singleExternalProfile.url)"
              >
                <git-provider-icon :provider="singleExternalProfile.provider" :size="15" />
                {{ `${getGitProviderLabel(singleExternalProfile.provider)} 主页` }}
              </a>
            </template>

            <n-dropdown
              v-else-if="externalProfiles.length > 1"
              :options="externalProfileDropdownOptions"
              trigger="click"
              @select="handleExternalProfileSelect"
            >
              <button type="button" class="header-action-btn">
                <n-icon size="15"><git-branch-outline /></n-icon>
                <span>Git 主页</span>
                <n-icon size="13" class="action-chevron"><chevron-down-outline /></n-icon>
              </button>
            </n-dropdown>

            <template v-if="authorSocialLinks.length === 1 && singleSocialLink">
              <a
                class="header-action-btn"
                :href="singleSocialLink.url"
                target="_blank"
                rel="noopener noreferrer"
                @click.prevent="openExternalLink(singleSocialLink.url)"
              >
                <n-icon size="15"><globe-outline /></n-icon>
                作者主页
              </a>
            </template>

            <n-dropdown
              v-else-if="authorSocialLinks.length > 1"
              :options="socialLinkDropdownOptions"
              trigger="click"
              @select="handleSocialLinkSelect"
            >
              <button type="button" class="header-action-btn">
                <n-icon size="15"><globe-outline /></n-icon>
                <span>作者主页</span>
                <n-icon size="13" class="action-chevron"><chevron-down-outline /></n-icon>
              </button>
            </n-dropdown>

            <template v-if="isOwnProfile">
              <router-link to="/settings/personal" class="header-action-btn">
                <n-icon size="15"><settings-outline /></n-icon>
                个人设置
              </router-link>
              <router-link
                v-if="canSubmitPlugin"
                to="/submit"
                class="header-action-btn header-action-btn--primary"
              >
                <n-icon size="15"><add-outline /></n-icon>
                提交插件
              </router-link>
            </template>
          </div>
        </div>
      </section>

      <section
        class="author-plugins"
        :aria-label="isOwnProfile ? '我的插件管理' : '作者已上架插件'"
      >
        <div class="section-heading" :class="{ 'section-heading--owner': isOwnProfile }">
          <div class="section-heading__left">
            <h2>{{ isOwnProfile ? "我的插件" : "已上架插件" }}</h2>
            <div
              v-if="isOwnProfile && (isDeveloper || ownerPlugins.length > 0)"
              class="status-filters"
              role="group"
              aria-label="按状态筛选插件"
            >
              <button
                v-for="option in statusFilterOptions"
                :key="option.value"
                type="button"
                class="status-filter-btn"
                :class="{ 'is-active': statusFilter === option.value }"
                :aria-pressed="statusFilter === option.value"
                @click="statusFilter = option.value"
              >
                <span>{{ option.label }}</span>
                <span class="status-filter-count">{{ option.count }}</span>
              </button>
            </div>
          </div>

          <div class="section-heading__right">
            <span v-if="!isOwnProfile" class="section-count">共 {{ publicPlugins.length }} 个</span>
            <n-button
              v-else-if="isDeveloper"
              quaternary
              circle
              size="small"
              :loading="loadingOwnerPlugins"
              aria-label="刷新插件列表"
              @click="refreshOwnerPlugins"
            >
              <template #icon>
                <n-icon><refresh-outline /></n-icon>
              </template>
            </n-button>
          </div>
        </div>

        <n-alert
          v-if="isOwnProfile && !isDeveloper && !ownerPlugins.length"
          type="info"
          :bordered="false"
          class="developer-onboarding-alert"
        >
          当前账号为注册用户，绑定至少一个 OAuth 账号成为开发者后即可提交与管理插件。
          <div class="onboarding-actions">
            <n-button
              v-if="enabledOAuthProviders.length === 1 && singleEnabledOAuthProvider"
              size="small"
              type="primary"
              @click="handleConnectOAuth(singleEnabledOAuthProvider.provider)"
            >
              <template #icon>
                <git-provider-icon :provider="singleEnabledOAuthProvider.provider" :size="14" />
              </template>
              绑定 {{ singleEnabledOAuthProvider.label }}
            </n-button>
            <n-dropdown
              v-else-if="enabledOAuthProviders.length > 1"
              :options="oauthBindDropdownOptions"
              trigger="click"
              @select="handleOAuthBindSelect"
            >
              <n-button size="small" type="primary">
                <template #icon>
                  <n-icon><git-branch-outline /></n-icon>
                </template>
                绑定 Git 账号
                <n-icon size="13" style="margin-left: 4px"><chevron-down-outline /></n-icon>
              </n-button>
            </n-dropdown>
            <router-link to="/settings/personal" class="onboarding-settings-link">
              前往个人设置
            </router-link>
          </div>
        </n-alert>

        <div
          v-if="isLoading || (isOwnProfile && loadingOwnerPlugins && !ownerPlugins.length)"
          class="loading-container"
        >
          <div class="loading-dots" aria-label="正在加载作者插件">
            <span></span><span></span><span></span>
          </div>
        </div>

        <div v-else-if="notFound" class="empty-state">
          <n-icon size="40"><alert-circle-outline /></n-icon>
          <h2>作者不存在</h2>
          <p>未找到该作者或公开账号。</p>
        </div>

        <div v-else-if="errorMessage" class="empty-state">
          <n-icon size="40"><alert-circle-outline /></n-icon>
          <h2>加载失败</h2>
          <p>{{ errorMessage }}</p>
        </div>

        <template v-else-if="isOwnProfile">
          <div v-if="ownerPluginsError && !ownerPlugins.length" class="empty-state">
            <n-icon size="40"><alert-circle-outline /></n-icon>
            <h2>我的插件加载失败</h2>
            <p>{{ ownerPluginsError }}</p>
            <n-button size="small" class="empty-action-btn" @click="refreshOwnerPlugins">
              重试
            </n-button>
          </div>

          <div v-else-if="!ownerPlugins.length" class="empty-state">
            <n-icon size="40"><file-tray-outline /></n-icon>
            <h2>还没有提交过插件</h2>
            <p>
              {{
                isDeveloper
                  ? "提交你的第一个社区插件，即可在此统一编辑与管理。"
                  : "绑定 OAuth 账号成为开发者后，即可在这里提交并管理插件。"
              }}
            </p>
            <router-link v-if="canSubmitPlugin" to="/submit" class="empty-cta-link">
              提交插件
            </router-link>
          </div>

          <div v-else-if="!displayedOwnerPlugins.length" class="empty-state">
            <n-icon size="40"><file-tray-outline /></n-icon>
            <h2>当前状态下暂无插件</h2>
            <p>切换到「全部」查看你的所有插件。</p>
            <n-button size="small" class="empty-action-btn" @click="statusFilter = 'all'">
              查看全部
            </n-button>
          </div>

          <div v-else class="plugins-grid">
            <plugin-card
              v-for="(plugin, index) in displayedOwnerPlugins"
              :key="plugin.id"
              :plugin="plugin"
              :index="index"
              :masonry="true"
              :show-admin-actions="false"
              :show-social-link="false"
              :disable-detail-link="plugin.status !== 'listed'"
            >
              <template #title-extra>
                <span class="owner-status-tag" :class="pluginStatusMeta(plugin).className">
                  {{ pluginStatusMeta(plugin).label }}
                </span>
              </template>

              <template #owner-actions>
                <div class="owner-card-meta">
                  <span
                    class="cdn-chip"
                    :class="plugin.cdn_enabled === true ? 'cdn-chip--on' : 'cdn-chip--off'"
                  >
                    {{ plugin.cdn_enabled === true ? "CDN 已开启" : "CDN 未开启" }}
                  </span>
                  <span
                    v-if="
                      isAdminUser && plugin.cdn_enabled === true && plugin.cdn_whitelist === true
                    "
                    class="cdn-chip cdn-chip--whitelist"
                  >
                    白名单
                  </span>
                </div>

                <div class="owner-card-actions">
                  <n-button
                    secondary
                    size="tiny"
                    :disabled="isPluginBusy(plugin)"
                    @click="openEditModal(plugin)"
                  >
                    <template #icon>
                      <n-icon><create-outline /></n-icon>
                    </template>
                    编辑
                  </n-button>

                  <n-button
                    v-if="plugin.status === 'listed'"
                    secondary
                    size="tiny"
                    type="warning"
                    :loading="pluginBusyAction(plugin) === 'unlist'"
                    :disabled="isPluginBusy(plugin) && pluginBusyAction(plugin) !== 'unlist'"
                    @click="unlistPlugin(plugin)"
                  >
                    <template #icon>
                      <n-icon><archive-outline /></n-icon>
                    </template>
                    下架
                  </n-button>

                  <n-button v-else-if="plugin.status === 'pending'" secondary size="tiny" disabled>
                    审查中
                  </n-button>

                  <n-button
                    v-else
                    secondary
                    size="tiny"
                    type="primary"
                    :loading="pluginBusyAction(plugin) === 'request'"
                    :disabled="isPluginBusy(plugin) && pluginBusyAction(plugin) !== 'request'"
                    @click="requestListPlugin(plugin)"
                  >
                    <template #icon>
                      <n-icon><cloud-upload-outline /></n-icon>
                    </template>
                    申请上架
                  </n-button>
                </div>
              </template>
            </plugin-card>
          </div>
        </template>

        <template v-else>
          <div v-if="!publicPlugins.length" class="empty-state">
            <n-icon size="40"><file-tray-outline /></n-icon>
            <h2>暂无已上架插件</h2>
            <p>该作者目前还没有已上架的插件。</p>
          </div>

          <div v-else class="plugins-grid">
            <plugin-card
              v-for="(plugin, index) in publicPlugins"
              :key="plugin.id"
              :plugin="plugin"
              :index="index"
              :masonry="true"
              :show-social-link="false"
            />
          </div>
        </template>
      </section>
    </main>

    <plugin-edit-modal
      v-if="editModalLoaded"
      v-model:show="editModalOpen"
      :plugin="editingPlugin"
      :available-tags="allOwnerTags"
      :max-tags="maxPluginTags"
      :busy-action="editingPlugin ? pluginBusyAction(editingPlugin) : ''"
      :is-admin-user="isAdminUser"
      :api-base-url="pluginStore.apiBaseUrl"
      @save="savePluginMetadata"
      @toggle-cdn="togglePluginCdn"
      @unlist="unlistPlugin"
      @request-list="requestListPlugin"
    />

    <app-footer />
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, h, onMounted, reactive, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { useRoute } from "vue-router";
import { NAlert, NButton, NDropdown, NIcon, useDialog, useMessage } from "naive-ui";
import {
  AddOutline,
  AlertCircleOutline,
  ArchiveOutline,
  ArrowBack,
  ChevronDownOutline,
  CloudUploadOutline,
  CreateOutline,
  FileTrayOutline,
  GitBranchOutline,
  GlobeOutline,
  PersonOutline,
  RefreshOutline,
  SettingsOutline,
} from "@vicons/ionicons5";
import AppHeader from "@/components/AppHeader.vue";
import AppFooter from "@/components/AppFooter.vue";
import GitProviderIcon from "@/components/GitProviderIcon.vue";
import PluginCard from "@/components/PluginCard.vue";
import type { SavePluginPayload } from "@/components/PluginEditModal.vue";
import { useExternalOpenConfirm } from "@/composables/useExternalOpenConfirm";
import { useSeo, useSeoContext } from "@/composables/useSeo";
import {
  avatarVariantUrl,
  getGitProviderLabel,
  resolveAuthorAvatarUrl,
  resolveMyAvatarUrl,
} from "@/utils/github";
import { usePluginStore } from "@/stores/plugins";
import type { AuthorOAuthProfile, OAuthProviderKey, Plugin, PluginStatus } from "@/types";

const PluginEditModal = defineAsyncComponent(() => import("@/components/PluginEditModal.vue"));

type OwnerStatusFilter = "all" | PluginStatus;

interface StatusMeta {
  label: string;
  className: string;
}

interface AggregatedSocialLink {
  url: string;
  label: string;
}

const STATUS_META: Record<PluginStatus, StatusMeta> = {
  listed: { label: "已上架", className: "owner-status-tag--listed" },
  pending: { label: "待审查", className: "owner-status-tag--pending" },
  unlisted: { label: "已下架", className: "owner-status-tag--unlisted" },
};

const route = useRoute();
const message = useMessage();
const dialog = useDialog();
const { confirmExternalOpen } = useExternalOpenConfirm();
const pluginStore = usePluginStore();
const { currentUser, siteConfig } = storeToRefs(pluginStore);
const { brandName, absoluteSiteUrl } = useSeoContext();

const username = computed(() => String(route.params.username || "").trim());
const canonicalUsername = shallowRef("");
const displayName = shallowRef("");
const avatarUrl = shallowRef("");
const githubUrl = shallowRef("");
const oauthProfiles = shallowRef<AuthorOAuthProfile[]>([]);
const avatarLoadFailed = shallowRef(false);
const publicPlugins = shallowRef<Plugin[]>([]);
const isLoading = shallowRef(true);
const notFound = shallowRef(false);
const errorMessage = shallowRef("");

const ownerPlugins = shallowRef<Plugin[]>([]);
const loadingOwnerPlugins = shallowRef(false);
const ownerPluginsError = shallowRef("");
const statusFilter = shallowRef<OwnerStatusFilter>("all");
const pluginBusyIds = reactive<Record<string, string>>({});
const editingPluginId = shallowRef("");
const editModalLoaded = shallowRef(false);
const editModalOpen = shallowRef(false);

let authorRequestId = 0;
let ownerRequestId = 0;

function parseHttpUrl(rawUrl: unknown): URL | null {
  const value = String(rawUrl || "").trim();
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed;
  } catch {
    return null;
  }
}

function normalizeUrlKey(parsed: URL): string {
  const pathname = parsed.pathname.replace(/\/+$/, "");
  return `${parsed.origin.toLowerCase()}${pathname.toLowerCase()}${parsed.search}`;
}

function formatExternalLinkLabel(parsed: URL): string {
  const pathname = parsed.pathname.replace(/\/+$/, "");
  const combined = pathname ? `${parsed.host}${pathname}` : parsed.host;
  return combined.length > 44 ? `${combined.slice(0, 41)}...` : combined;
}

const myPublicUsername = computed(() =>
  String(
    currentUser.value?.public_username ||
      currentUser.value?.github_login ||
      currentUser.value?.internal_username ||
      "",
  ).trim(),
);

const isOwnProfile = computed(() => {
  if (notFound.value || !currentUser.value || !myPublicUsername.value || !canonicalUsername.value) {
    return false;
  }
  return myPublicUsername.value.toLowerCase() === canonicalUsername.value.toLowerCase();
});

const isAdminUser = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);

const isDeveloper = computed(() => {
  if (typeof currentUser.value?.is_developer === "boolean") return currentUser.value.is_developer;
  return Boolean(currentUser.value?.github_login || isAdminUser.value);
});

const canSubmitPlugin = computed(
  () =>
    Boolean(siteConfig.value.market?.submissions_enabled) &&
    (isDeveloper.value || isAdminUser.value),
);

const maxPluginTags = computed(() => Number(siteConfig.value.market?.max_plugin_tags || 8));

const authorDisplayName = computed(() => {
  if (isOwnProfile.value && currentUser.value?.github_name) {
    return (
      String(currentUser.value.github_name).trim() || canonicalUsername.value || username.value
    );
  }
  return displayName.value || canonicalUsername.value || username.value;
});

const authorAvatarUrl = computed(() => {
  if (isOwnProfile.value && currentUser.value) {
    const liveAvatar = String(
      currentUser.value.effective_avatar_url || currentUser.value.avatar_url || avatarUrl.value,
    ).trim();
    if (liveAvatar) {
      return avatarVariantUrl(resolveMyAvatarUrl(liveAvatar, pluginStore.apiBaseUrl), 128);
    }
  }
  return avatarVariantUrl(
    resolveAuthorAvatarUrl(
      avatarUrl.value,
      canonicalUsername.value || username.value,
      pluginStore.apiBaseUrl,
    ),
    128,
  );
});

watch(authorAvatarUrl, () => {
  avatarLoadFailed.value = false;
});

const externalProfiles = computed<AuthorOAuthProfile[]>(() => {
  const candidates: AuthorOAuthProfile[] = oauthProfiles.value.length
    ? oauthProfiles.value
    : githubUrl.value
      ? [
          {
            provider: "github",
            login: canonicalUsername.value || username.value,
            url: githubUrl.value,
          },
        ]
      : [];
  const seen = new Set<string>();
  const valid: AuthorOAuthProfile[] = [];
  for (const item of candidates) {
    const parsed = parseHttpUrl(item.url);
    if (!parsed) continue;
    const key = normalizeUrlKey(parsed);
    if (seen.has(key)) continue;
    seen.add(key);
    valid.push({ ...item, url: parsed.toString() });
  }
  return valid;
});

const singleExternalProfile = computed<AuthorOAuthProfile | null>(
  () => externalProfiles.value[0] || null,
);

const externalProfileDropdownOptions = computed(() =>
  externalProfiles.value.map((profile) => ({
    key: profile.url,
    label: profile.login
      ? `${getGitProviderLabel(profile.provider)} · @${profile.login}`
      : `${getGitProviderLabel(profile.provider)} 主页`,
    icon: () => h(GitProviderIcon, { provider: profile.provider, size: 15 }),
  })),
);

const authorSocialLinks = computed<AggregatedSocialLink[]>(() => {
  const sourcePlugins =
    isOwnProfile.value && ownerPlugins.value.length ? ownerPlugins.value : publicPlugins.value;
  const seen = new Set<string>();
  for (const profile of externalProfiles.value) {
    const parsed = parseHttpUrl(profile.url);
    if (parsed) seen.add(normalizeUrlKey(parsed));
  }

  const selfPaths = new Set<string>();
  for (const handle of [canonicalUsername.value, username.value]) {
    const clean = handle.trim();
    if (!clean) continue;
    selfPaths.add(`/author/${clean.toLowerCase()}`);
    selfPaths.add(`/author/${encodeURIComponent(clean).toLowerCase()}`);
  }
  const siteOrigins = new Set<string>();
  if (typeof window !== "undefined" && window.location.origin) {
    siteOrigins.add(window.location.origin.toLowerCase());
  }
  const configuredOrigin = parseHttpUrl(siteConfig.value.web_url);
  if (configuredOrigin) siteOrigins.add(configuredOrigin.origin.toLowerCase());

  const links: AggregatedSocialLink[] = [];
  for (const plugin of sourcePlugins) {
    const parsed = parseHttpUrl(plugin.social_link);
    if (!parsed) continue;
    const normalizedPath = parsed.pathname.replace(/\/+$/, "").toLowerCase();
    if (siteOrigins.has(parsed.origin.toLowerCase()) && selfPaths.has(normalizedPath)) {
      continue;
    }
    const key = normalizeUrlKey(parsed);
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({
      url: parsed.toString(),
      label: formatExternalLinkLabel(parsed),
    });
  }
  return links;
});

const singleSocialLink = computed<AggregatedSocialLink | null>(
  () => authorSocialLinks.value[0] || null,
);

const socialLinkDropdownOptions = computed(() =>
  authorSocialLinks.value.map((item) => ({
    key: item.url,
    label: item.label,
    icon: () => h(NIcon, null, { default: () => h(GlobeOutline) }),
  })),
);

const authorRoleLabel = computed(() => {
  if (isOwnProfile.value) {
    return isDeveloper.value || ownerPlugins.value.length > 0 ? "社区插件开发者" : "社区成员";
  }
  return publicPlugins.value.length ? "社区插件开发者" : "社区成员";
});

const statusCounts = computed(() => {
  const counts = { all: ownerPlugins.value.length, listed: 0, pending: 0, unlisted: 0 };
  for (const plugin of ownerPlugins.value) {
    const status = plugin.status || "pending";
    if (status === "listed") counts.listed += 1;
    else if (status === "unlisted") counts.unlisted += 1;
    else counts.pending += 1;
  }
  return counts;
});

const ownerSummaryText = computed(() => {
  if (!isDeveloper.value && ownerPlugins.value.length === 0) return "";
  const { all, listed, pending } = statusCounts.value;
  const parts = [`共 ${all} 个插件`, `${listed} 已上架`];
  if (pending > 0) parts.push(`${pending} 待审查`);
  return parts.join(" · ");
});

const statusFilterOptions = computed<
  Array<{ label: string; value: OwnerStatusFilter; count: number }>
>(() => [
  { label: "全部", value: "all", count: statusCounts.value.all },
  { label: "已上架", value: "listed", count: statusCounts.value.listed },
  { label: "待审查", value: "pending", count: statusCounts.value.pending },
  { label: "已下架", value: "unlisted", count: statusCounts.value.unlisted },
]);

const displayedOwnerPlugins = computed(() => {
  if (statusFilter.value === "all") return ownerPlugins.value;
  return ownerPlugins.value.filter((plugin) => (plugin.status || "pending") === statusFilter.value);
});

const allOwnerTags = computed(() => {
  const tags = new Set<string>();
  for (const plugin of ownerPlugins.value) {
    for (const tag of plugin.tags || []) tags.add(tag);
  }
  return Array.from(tags);
});

const editingPlugin = computed<Plugin | null>(() => {
  if (!editingPluginId.value) return null;
  return ownerPlugins.value.find((item) => String(item.id) === editingPluginId.value) ?? null;
});

const enabledOAuthProviders = computed(() => {
  const configured = siteConfig.value.auth.oauth_providers;
  if (Array.isArray(configured) && configured.length > 0) {
    return configured.filter((item) => item.enabled);
  }
  const fallback: Array<{ provider: OAuthProviderKey; enabled: boolean }> = [
    { provider: "github", enabled: Boolean(siteConfig.value.auth.github_login_enabled) },
    { provider: "gitlab", enabled: Boolean(siteConfig.value.auth.gitlab_login_enabled) },
    { provider: "gitee", enabled: Boolean(siteConfig.value.auth.gitee_login_enabled) },
    { provider: "atomgit", enabled: Boolean(siteConfig.value.auth.atomgit_login_enabled) },
    { provider: "gitea", enabled: Boolean(siteConfig.value.auth.gitea_login_enabled) },
  ];
  return fallback
    .filter((item) => item.enabled)
    .map((item) => ({ provider: item.provider, label: getGitProviderLabel(item.provider) }));
});

const singleEnabledOAuthProvider = computed(() => enabledOAuthProviders.value[0] || null);

const oauthBindDropdownOptions = computed(() =>
  enabledOAuthProviders.value.map((item) => ({
    key: item.provider,
    label: `绑定 ${item.label}`,
    icon: () => h(GitProviderIcon, { provider: item.provider, size: 15 }),
  })),
);

const authorTitle = computed(() =>
  notFound.value ? "作者不存在" : `${authorDisplayName.value || username.value} 的插件`,
);
const authorDescription = computed(() =>
  notFound.value
    ? "未找到该作者或公开账号。"
    : `查看 ${authorDisplayName.value || username.value} 在 ${brandName.value} 发布的所有插件。`,
);
const authorPath = computed(
  () => `/author/${encodeURIComponent(canonicalUsername.value || username.value)}`,
);
const authorRobots = computed(() => (notFound.value ? "noindex,nofollow" : undefined));
const authorJsonLd = computed(() => {
  if (notFound.value) return undefined;
  const authorPageUrl = absoluteSiteUrl(authorPath.value);
  const sameAs = externalProfiles.value.map((item) => item.url).filter(Boolean);
  return {
    "@context": "https://schema.org",
    "@type": "ProfilePage",
    mainEntity: {
      "@type": "Person",
      name: authorDisplayName.value || username.value,
      url: externalProfiles.value[0]?.url || authorPageUrl,
      ...(sameAs.length ? { sameAs } : {}),
    },
    url: authorPageUrl,
  };
});

useSeo({
  title: authorTitle,
  description: authorDescription,
  path: authorPath,
  robots: authorRobots,
  jsonLd: authorJsonLd,
});

function openExternalLink(url: string): void {
  confirmExternalOpen(url);
}

function handleExternalProfileSelect(key: string): void {
  confirmExternalOpen(String(key || ""));
}

function handleSocialLinkSelect(key: string): void {
  confirmExternalOpen(String(key || ""));
}

function handleOAuthBindSelect(key: string): void {
  handleConnectOAuth(key as OAuthProviderKey);
}

function pluginStatusMeta(plugin: Plugin): StatusMeta {
  const status = plugin.status || "pending";
  return STATUS_META[status] || { label: status, className: "owner-status-tag--pending" };
}

function pluginBusyAction(plugin: Plugin): string {
  return pluginBusyIds[String(plugin.id)] || "";
}

function isPluginBusy(plugin: Plugin): boolean {
  return Boolean(pluginBusyAction(plugin));
}

function openEditModal(plugin: Plugin): void {
  editingPluginId.value = String(plugin.id);
  editModalLoaded.value = true;
  editModalOpen.value = true;
}

function replacePlugin(updatedPlugin: Plugin): void {
  const targetId = String(updatedPlugin.id);
  ownerPlugins.value = ownerPlugins.value.map((plugin) =>
    String(plugin.id) === targetId ? { ...plugin, ...updatedPlugin } : plugin,
  );
  if (updatedPlugin.status === "listed") {
    const exists = publicPlugins.value.some((plugin) => String(plugin.id) === targetId);
    publicPlugins.value = exists
      ? publicPlugins.value.map((plugin) =>
          String(plugin.id) === targetId ? { ...plugin, ...updatedPlugin } : plugin,
        )
      : [updatedPlugin, ...publicPlugins.value];
  } else {
    publicPlugins.value = publicPlugins.value.filter((plugin) => String(plugin.id) !== targetId);
  }
}

async function withPluginBusy(
  plugin: Plugin,
  action: string,
  task: () => Promise<Plugin>,
): Promise<Plugin> {
  const key = String(plugin.id);
  pluginBusyIds[key] = action;
  try {
    const updated = await task();
    replacePlugin(updated);
    return updated;
  } finally {
    delete pluginBusyIds[key];
  }
}

async function refreshOwnerPlugins(): Promise<void> {
  if (!isOwnProfile.value || !isDeveloper.value) return;
  const reqId = ++ownerRequestId;
  loadingOwnerPlugins.value = true;
  ownerPluginsError.value = "";
  try {
    const items = await pluginStore.loadMyPlugins();
    if (reqId !== ownerRequestId) return;
    ownerPlugins.value = items;
  } catch (error) {
    if (reqId !== ownerRequestId) return;
    const msg = error instanceof Error ? error.message : "插件加载失败";
    ownerPluginsError.value = msg;
    message.error(msg);
  } finally {
    if (reqId === ownerRequestId) {
      loadingOwnerPlugins.value = false;
    }
  }
}

async function savePluginMetadata({ plugin, changes }: SavePluginPayload): Promise<void> {
  try {
    await withPluginBusy(plugin, "save", () =>
      pluginStore.updatePluginMetadata(plugin.id, changes),
    );
    message.success("插件信息已保存");
    editModalOpen.value = false;
  } catch (error) {
    message.error(error instanceof Error ? error.message : "保存插件信息失败");
  }
}

async function togglePluginCdn({
  plugin,
  enabled,
  whitelist,
}: {
  plugin: Plugin;
  enabled: boolean;
  whitelist?: boolean;
}): Promise<void> {
  if (isPluginBusy(plugin)) return;
  try {
    const updated = await withPluginBusy(plugin, "cdn", () =>
      pluginStore.updatePluginCdn(plugin.id, enabled, whitelist),
    );
    if (whitelist !== undefined && updated.cdn_enabled) {
      message.success(
        updated.cdn_whitelist
          ? "已开启管理员 CDN 白名单直发，后续发版将跳过审查直接发布"
          : "已关闭管理员 CDN 白名单直发，后续发版将按正常流程审查",
      );
      return;
    }
    message.success(
      updated.cdn_enabled
        ? "已开启 CDN，未审查的新版本会自动送审"
        : "已关闭 CDN，插件仍可通过原 Git 仓库安装",
    );
  } catch (error) {
    message.error(error instanceof Error ? error.message : "更新 CDN 设置失败");
  }
}

function unlistPlugin(plugin: Plugin): void {
  dialog.warning({
    title: "下架插件",
    content: `${plugin.display_name || plugin.name} 下架后将从公开市场隐藏。`,
    positiveText: "下架",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        await withPluginBusy(plugin, "unlist", () =>
          pluginStore.unlistOwnPlugin(plugin.id, { reason: "作者主动下架" }),
        );
        message.success("插件已下架");
      } catch (error) {
        message.error(error instanceof Error ? error.message : "下架失败");
      }
    },
  });
}

async function requestListPlugin(plugin: Plugin): Promise<void> {
  try {
    const updated = await withPluginBusy(plugin, "request", () =>
      pluginStore.requestPluginListing(plugin.id),
    );
    message.success(updated.status === "listed" ? "插件已重新上架" : "已提交上架申请");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "申请上架失败");
  }
}

function handleConnectOAuth(provider: OAuthProviderKey): void {
  pluginStore.loginWithOAuth(provider, "/settings/personal");
}

function resetOwnerState(): void {
  ownerRequestId += 1;
  ownerPlugins.value = [];
  ownerPluginsError.value = "";
  loadingOwnerPlugins.value = false;
  statusFilter.value = "all";
  editModalOpen.value = false;
  editingPluginId.value = "";
}

async function loadAuthorPage(): Promise<void> {
  if (!username.value) return;
  const reqId = ++authorRequestId;
  isLoading.value = true;
  notFound.value = false;
  errorMessage.value = "";
  avatarLoadFailed.value = false;
  canonicalUsername.value = username.value;
  displayName.value = "";
  avatarUrl.value = "";
  githubUrl.value = "";
  oauthProfiles.value = [];
  publicPlugins.value = [];
  resetOwnerState();

  try {
    const [response] = await Promise.all([
      fetch(`${pluginStore.apiBaseUrl}/v1/authors/${encodeURIComponent(username.value)}/plugins`),
      pluginStore.loadCurrentUser().catch(() => undefined),
    ]);
    if (reqId !== authorRequestId) return;
    const data = await response.json().catch(() => ({}));
    if (response.status === 404) {
      notFound.value = true;
      publicPlugins.value = [];
      return;
    }
    if (!response.ok) throw new Error(data.detail || data.error || "作者插件加载失败");
    canonicalUsername.value = String(data.username || username.value).trim();
    displayName.value = String(data.display_name || "").trim();
    avatarUrl.value = String(data.avatar_url || "").trim();
    githubUrl.value = String(data.github_url || "").trim();
    oauthProfiles.value = Array.isArray(data.oauth_profiles) ? data.oauth_profiles : [];
    publicPlugins.value = data.items || [];
  } catch (error) {
    if (reqId !== authorRequestId) return;
    errorMessage.value = error instanceof Error ? error.message : "作者插件加载失败";
  } finally {
    if (reqId === authorRequestId) {
      isLoading.value = false;
    }
  }

  if (reqId === authorRequestId && isOwnProfile.value && isDeveloper.value) {
    await refreshOwnerPlugins();
  }
}

watch([isOwnProfile, isDeveloper], ([own, dev], [prevOwn, prevDev]) => {
  if (isLoading.value) return;
  if (!own) {
    if (prevOwn) resetOwnerState();
    return;
  }
  if (dev && (!prevOwn || !prevDev)) {
    void refreshOwnerPlugins();
  }
});

onMounted(() => {
  void loadAuthorPage();
});

watch(username, () => {
  void loadAuthorPage();
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
  justify-content: space-between;
  gap: 20px;
  padding: 24px 28px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 10px;
}

.author-card--owner {
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--primary-color) 5%, var(--bg-card)) 0%,
    var(--bg-card) 60%
  );
  border-color: color-mix(in srgb, var(--primary-color) 28%, var(--border-base));
}

.author-card__main {
  min-width: 0;
  flex: 1;
  display: flex;
  align-items: center;
  gap: 20px;
}

.author-avatar {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  border: 1px solid var(--border-base);
  flex-shrink: 0;
  object-fit: cover;
}

.author-avatar--fallback {
  display: inline-grid;
  place-items: center;
  color: var(--text-tertiary);
  background: var(--bg-base);
}

.author-info {
  flex: 1;
  min-width: 0;
}

.author-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.author-info h1 {
  margin: 0;
  font-size: 22px;
  color: var(--text-primary);
  line-height: 1.3;
}

.owner-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 9px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--primary-color);
  background: var(--primary-light);
  border: 1px solid color-mix(in srgb, var(--primary-color) 32%, transparent);
  border-radius: 999px;
}

.author-subtitle {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 5px 0 0;
  font-size: 13px;
  color: var(--text-tertiary);
}

.author-handle {
  font-family: var(--font-family-mono, monospace);
}

.author-stats {
  color: var(--text-secondary);
}

.author-top-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
  flex-shrink: 0;
}

.header-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 14px;
  font: inherit;
  font-size: 13.5px;
  font-weight: 500;
  line-height: 1.2;
  color: var(--text-primary);
  background: var(--bg-base);
  border: 1px solid var(--border-base);
  border-radius: 7px;
  text-decoration: none;
  cursor: pointer;
  transition: all 0.2s ease;
}

.header-action-btn:hover {
  border-color: var(--primary-color);
  color: var(--primary-color);
}

.header-action-btn--primary {
  color: #ffffff;
  background: var(--primary-color);
  border-color: var(--primary-color);
}

.header-action-btn--primary:hover {
  color: #ffffff;
  background: var(--primary-hover);
  border-color: var(--primary-hover);
}

.action-chevron {
  opacity: 0.72;
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
  padding: 0 4px;
}

.section-heading__left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
}

.section-heading h2 {
  margin: 0;
  font-size: 18px;
  color: var(--text-primary);
}

.section-count {
  font-size: 13px;
  color: var(--text-tertiary);
}

.status-filters {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.status-filter-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  font: inherit;
  font-size: 12.5px;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 999px;
  cursor: pointer;
  transition: all 0.16s ease;
}

.status-filter-btn:hover {
  color: var(--primary-color);
  border-color: var(--border-hover);
}

.status-filter-btn.is-active {
  color: var(--primary-color);
  background: var(--primary-light);
  border-color: color-mix(in srgb, var(--primary-color) 42%, transparent);
  font-weight: 600;
}

.status-filter-count {
  font-family: var(--font-family-mono, monospace);
  font-size: 11px;
  opacity: 0.85;
}

.developer-onboarding-alert {
  margin-bottom: 20px;
}

.onboarding-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.onboarding-settings-link {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  font-size: 12.5px;
  color: var(--primary-color);
  text-decoration: none;
}

.onboarding-settings-link:hover {
  text-decoration: underline;
}

.plugins-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 20px;
}

.owner-status-tag {
  flex: none;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.3;
  border: 1px solid currentColor;
  border-radius: 999px;
}

.owner-status-tag--listed {
  color: var(--success-color);
}

.owner-status-tag--pending {
  color: var(--metric-comment);
}

.owner-status-tag--unlisted {
  color: var(--accent-amber);
}

.owner-card-meta,
.owner-card-actions {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.cdn-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  font-size: 11px;
  line-height: 1.35;
  border-radius: 999px;
  border: 1px solid var(--border-base);
}

.cdn-chip--on {
  color: var(--success-color);
  background: color-mix(in srgb, var(--success-color) 10%, transparent);
  border-color: color-mix(in srgb, var(--success-color) 32%, transparent);
}

.cdn-chip--off {
  color: var(--text-tertiary);
  background: var(--bg-hover);
}

.cdn-chip--whitelist {
  color: var(--primary-color);
  background: var(--primary-light);
  border-color: color-mix(in srgb, var(--primary-color) 32%, transparent);
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

.empty-action-btn {
  margin-top: 14px;
}

.empty-cta-link {
  margin-top: 16px;
  display: inline-flex;
  align-items: center;
  padding: 8px 18px;
  font-size: 13.5px;
  font-weight: 500;
  color: #ffffff;
  background: var(--primary-color);
  border-radius: 7px;
  text-decoration: none;
  transition: background-color 0.2s ease;
}

.empty-cta-link:hover {
  background: var(--primary-hover);
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
    flex-direction: column;
    align-items: stretch;
    gap: 16px;
    padding: 20px;
  }

  .author-card__main {
    align-items: flex-start;
    gap: 14px;
  }

  .author-top-actions {
    width: 100%;
    justify-content: flex-start;
  }

  .header-action-btn {
    flex: 1 1 auto;
  }

  .plugins-grid {
    grid-template-columns: 1fr;
  }
}
</style>
