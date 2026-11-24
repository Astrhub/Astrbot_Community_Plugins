<template>
  <div class="submit-plugin-page">
    <n-layout-header class="page-header">
      <div class="header-content">
        <div class="header-left">
          <n-button tag="a" href="/" quaternary circle aria-label="返回插件市场">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <h1>提交插件</h1>
            <p>提交到社区市场，可选择需要逐版本审查的 CDN 加速服务。</p>
          </div>
        </div>
        <div class="header-right">
          <theme-mode-button circle />
          <n-button
            v-if="!currentUser && isLocalMode && !hasEnabledOAuthLogin"
            tag="a"
            href="/admin"
            type="primary"
          >
            管理员登录
          </n-button>
          <n-button
            v-else-if="!currentUser && hasEnabledOAuthLogin"
            type="primary"
            @click="connectSelectedProvider"
          >
            <template #icon>
              <n-icon>
                <logo-github v-if="selectedProvider === 'github'" />
                <git-branch-outline v-else />
              </n-icon>
            </template>
            {{ selectedProviderLabel }} 登录
          </n-button>
        </div>
      </div>
    </n-layout-header>

    <main class="submit-content">
      <section class="submit-aside">
        <n-card :bordered="false" class="info-panel">
          <h2>上架规则</h2>
          <p v-if="!siteConfig.market.submissions_enabled">当前站点已暂停插件提交。</p>
          <p v-else-if="isLocalMode">
            当前为本地自用模式，管理员登录后可直接填写受支持平台的公开 Git
            仓库地址并自动读取元数据。
          </p>
          <p v-else>
            普通邮箱注册用户可先浏览、点赞与评论；提交或上架插件前需绑定至少一个 OAuth
            账号成为开发者，用于校验仓库归属。
          </p>
          <ul>
            <li>仓库必须是 GitHub、GitLab、Gitee、AtomGit 或 Gitea 上的公开 HTTPS 仓库。</li>
            <li>插件名必须使用 `astrbot_plugin_` 前缀。</li>
          </ul>
          <p class="rule-tip">
            {{
              isLocalMode
                ? "输入仓库地址后点击“读取信息”可自动填充插件字段，确认后即可提交。"
                : "连接受支持的 Git 平台后选择你可管理的插件仓库，信息会自动填充，确认后即可提交。"
            }}
          </p>
        </n-card>
      </section>

      <section class="submit-main">
        <n-card :bordered="false" class="form-card">
          <template #header>
            <div class="form-title">
              <h2>插件信息</h2>
              <n-tag v-if="currentUser" type="success" :bordered="false">
                已登录：{{
                  currentUser.public_username ||
                  currentUser.github_login ||
                  currentUser.internal_username ||
                  currentUser.login
                }}
              </n-tag>
              <n-tag v-else-if="isLocalMode" type="warning" :bordered="false">
                请先登录管理员账号
              </n-tag>
              <n-tag v-else-if="!hasEnabledOAuthLogin" type="warning" :bordered="false"
                >OAuth 登录未开启</n-tag
              >
              <n-tag v-else type="warning" :bordered="false">需要开发者登录</n-tag>
            </div>
          </template>

          <n-form ref="formRef" :model="formData" :rules="rules" label-placement="top">
            <n-grid :x-gap="16" :y-gap="10" :cols="2" responsive="screen">
              <n-grid-item span="2">
                <n-form-item
                  :label="
                    isLocalAdminDirectSubmit
                      ? 'Git 插件仓库地址'
                      : `选择 ${selectedProviderLabel} 插件仓库`
                  "
                  path="repo"
                >
                  <div class="repository-section">
                    <n-input-group v-if="isLocalAdminDirectSubmit">
                      <n-input
                        v-model:value="formData.repo"
                        placeholder="https://github.com/owner/astrbot_plugin_example 或 gitlab/gitee/atomgit/gitea"
                        :disabled="submitting"
                        :input-props="{
                          name: 'plugin-repo',
                          autocomplete: 'off',
                          spellcheck: 'false',
                        }"
                      />
                      <n-button
                        :loading="metadataLoading"
                        :disabled="submitting || !formData.repo.trim()"
                        @click="() => fetchMetadataFromRepo(false)"
                      >
                        读取信息
                      </n-button>
                      <n-button
                        type="primary"
                        secondary
                        :loading="metadataLoading"
                        :disabled="submitting || !formData.repo.trim()"
                        @click="() => fetchMetadataFromRepo(true)"
                      >
                        一键导入
                      </n-button>
                    </n-input-group>
                    <template v-else>
                      <div
                        v-if="availableProviders.length > 1"
                        class="provider-switcher"
                        role="group"
                        aria-label="选择代码托管平台"
                      >
                        <n-button
                          v-for="item in availableProviders"
                          :key="item.provider"
                          size="small"
                          :type="selectedProvider === item.provider ? 'primary' : 'default'"
                          :secondary="selectedProvider !== item.provider"
                          :disabled="submitting"
                          @click="switchProvider(item.provider)"
                        >
                          {{ item.label }}
                        </n-button>
                      </div>
                      <div class="repository-picker">
                        <n-select
                          :value="selectedRepositoryId"
                          :options="repositoryOptions"
                          :loading="repositoriesLoading"
                          :disabled="
                            !currentUser ||
                            needsOAuthBinding ||
                            needsProviderConnection ||
                            submitting
                          "
                          filterable
                          placeholder="搜索或选择 astrbot_plugin_ 开头的仓库"
                          :aria-label="`选择 ${selectedProviderLabel} 插件仓库`"
                          @update:value="selectRepository"
                          @update:show="refreshRepositoriesOnOpen"
                        />
                      </div>
                      <n-alert v-if="needsOAuthBinding" type="warning" :bordered="false">
                        当前账号尚未绑定 OAuth 身份。绑定至少一个 OAuth
                        账号成为开发者后即可提交与上架插件。
                        <div class="connect-actions">
                          <n-button
                            v-for="item in enabledOAuthProviders"
                            :key="item.provider"
                            class="connect-github"
                            :type="
                              item.provider === enabledOAuthProviders[0]?.provider
                                ? 'primary'
                                : 'default'
                            "
                            @click="connectProvider(item.provider)"
                            >连接 {{ item.label }}</n-button
                          >
                          <n-button
                            class="connect-github"
                            tertiary
                            tag="a"
                            href="/settings/personal"
                            >前往个人设置</n-button
                          >
                        </div>
                      </n-alert>
                      <n-alert v-else-if="needsProviderConnection" type="info" :bordered="false">
                        连接
                        {{ selectedProviderLabel }}
                        后即可选择你可管理的公开插件仓库，无需填写访问令牌。
                        <n-button
                          class="connect-github"
                          type="primary"
                          @click="connectSelectedProvider"
                          >连接 {{ selectedProviderLabel }}</n-button
                        >
                      </n-alert>
                      <p
                        v-else-if="repositoriesError"
                        class="metadata-feedback is-error"
                        role="alert"
                      >
                        {{ repositoriesError }}
                      </p>
                      <p
                        v-else-if="currentUser && !repositoriesLoading && !repositories.length"
                        class="repository-hint"
                      >
                        没有找到可管理的公开 {{ selectedProviderLabel }} 插件仓库。仓库名需以
                        astrbot_plugin_ 开头。
                      </p>
                      <n-button
                        v-if="nextRepositoryPage"
                        size="small"
                        :loading="repositoriesLoading"
                        @click="loadRepositories(true)"
                        >继续加载仓库</n-button
                      >
                    </template>
                    <a
                      v-if="formData.repo"
                      :href="formData.repo"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="repository-url"
                      >{{ formData.repo }}</a
                    >
                    <n-alert
                      v-if="showNonGithubDirectCloneHint"
                      type="info"
                      :bordered="false"
                      style="margin-top: 8px"
                    >
                      非 GitHub 仓库在未启用 CDN 时，AstrBot 端需升级至 v4.27.0
                      及以上版本才能直接从仓库安装；如需兼容旧版 AstrBot，建议开启社区源 CDN。
                    </n-alert>
                    <p v-if="metadataLoading" class="repository-hint" role="status">
                      正在读取插件信息…
                    </p>
                    <p v-if="metadataStatus.text" :class="metadataFeedbackClass">
                      {{ metadataStatus.text }}
                    </p>
                    <n-button
                      v-if="metadataStatus.type === 'error'"
                      size="small"
                      @click="() => fetchMetadataFromRepo()"
                      >重试读取信息</n-button
                    >
                    <n-button
                      v-if="
                        currentUser &&
                        selectedRepositoryId &&
                        !repositories.find((repo) => repo.id === selectedRepositoryId)?.fork &&
                        !repositories.find((repo) => repo.id === selectedRepositoryId)?.submitted
                      "
                      class="import-metadata-btn"
                      size="small"
                      type="primary"
                      secondary
                      :loading="metadataLoading"
                      @click="() => fetchMetadataFromRepo(true)"
                      >一键导入已有插件</n-button
                    >
                  </div>
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="插件名" path="name">
                  <n-input
                    v-model:value="formData.name"
                    placeholder="例如：astrbot_plugin_example…"
                    :input-props="{
                      name: 'plugin-name',
                      autocomplete: 'off',
                      spellcheck: 'false',
                    }"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="展示名称（可选）" path="display_name">
                  <n-input
                    v-model:value="formData.display_name"
                    placeholder="给用户看的名称…"
                    :input-props="{
                      name: 'plugin-display-name',
                      autocomplete: 'off',
                    }"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="插件简介" path="desc">
                  <n-input
                    v-model:value="formData.desc"
                    type="textarea"
                    placeholder="填写插件描述…"
                    :input-props="{
                      name: 'plugin-description',
                      autocomplete: 'off',
                    }"
                    :show-count="true"
                    :rows="4"
                    :resizable="false"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="卡片短描述（可选）" path="short_desc">
                  <n-input
                    v-model:value="formData.short_desc"
                    placeholder="可选，一句话介绍插件…"
                    :input-props="{ name: 'plugin-short-description', autocomplete: 'off' }"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="作者显示名" path="author">
                  <n-input
                    v-model:value="formData.author"
                    placeholder="默认建议与 GitHub 用户名一致…"
                    :input-props="{
                      name: 'plugin-author',
                      autocomplete: 'off',
                      spellcheck: 'false',
                    }"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2 m:1">
                <n-form-item label="官方分类（可选）" path="category">
                  <n-select
                    v-model:value="formData.category"
                    :options="pluginCategoryOptions"
                    placeholder="不选择则归为其他…"
                    aria-label="官方分类"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="社交链接" path="social_link">
                  <n-input
                    v-model:value="formData.social_link"
                    type="url"
                    placeholder="可选，例如：https://github.com/owner…"
                    :input-props="{
                      name: 'plugin-social-link',
                      autocomplete: 'off',
                      inputmode: 'url',
                      spellcheck: 'false',
                    }"
                  />
                </n-form-item>
              </n-grid-item>
              <n-grid-item span="2">
                <n-form-item label="标签" path="tags">
                  <n-dynamic-tags v-model:value="formData.tags" :max="maxPluginTags" />
                </n-form-item>
              </n-grid-item>
              <n-grid-item v-if="!isLocalMode" span="2">
                <n-form-item label="社区源 CDN">
                  <div>
                    <n-checkbox v-model:checked="formData.cdn_enabled" :disabled="submitting">
                      使用社区源 CDN（需审查）
                    </n-checkbox>
                    <p class="repository-hint">
                      开启后自动跟踪新版本，每次版本更新都需要审查。通过前不提供该版本的 CDN
                      资源链接，仍可通过原 Git 仓库安装。可在个人界面关闭 CDN。
                    </p>
                    <p v-if="!formData.cdn_enabled" class="repository-hint">
                      {{
                        siteConfig.market.plugin_auto_approve_enabled
                          ? "当前站点已开启自动通过，不使用 CDN 可直接上架。"
                          : "不使用 CDN，仅按站点规则进行上架审核，不进入包审查。"
                      }}
                    </p>
                  </div>
                </n-form-item>
              </n-grid-item>
            </n-grid>
          </n-form>

          <template #footer>
            <div class="form-actions">
              <n-button quaternary @click="goBack">取消</n-button>
              <n-button
                type="primary"
                :loading="submitting"
                :disabled="
                  !currentUser ||
                  (!isLocalAdminDirectSubmit && !selectedRepositoryId) ||
                  (isLocalAdminDirectSubmit && !formData.repo.trim()) ||
                  metadataLoading ||
                  repositoriesLoading ||
                  (!isLocalAdminDirectSubmit && needsOAuthBinding) ||
                  (!isLocalAdminDirectSubmit && needsProviderConnection) ||
                  !siteConfig.market.submissions_enabled
                "
                @click="handleSubmit"
              >
                {{
                  formData.cdn_enabled
                    ? "提交 CDN 审查"
                    : siteConfig.market.plugin_auto_approve_enabled
                      ? "提交并上架"
                      : "提交审核"
                }}
              </n-button>
            </div>
          </template>
        </n-card>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import { useSeo, useSeoContext } from "../composables/useSeo";
import {
  NButton,
  NAlert,
  NCheckbox,
  NCard,
  NDynamicTags,
  NForm,
  NFormItem,
  NGrid,
  NGridItem,
  NIcon,
  NInput,
  NInputGroup,
  NLayoutHeader,
  NSelect,
  NTag,
  useMessage,
} from "naive-ui";
import { ArrowBack, GitBranchOutline, LogoGithub } from "@vicons/ionicons5";
import { PLUGIN_CATEGORY_OPTIONS, usePluginStore } from "@/stores/plugins";
import ThemeModeButton from "@/components/ThemeModeButton.vue";
import {
  detectGitProvider,
  getGitProviderLabel,
  isGithubRepoUrl,
  isSupportedGitRepoUrl,
} from "@/utils/github";
import type {
  GithubPluginRepository,
  OAuthProviderKey,
  PluginSubmissionMetadataPreview,
} from "@/types";

const router = useRouter();
const { brandName } = useSeoContext();
useSeo({
  title: "提交插件",
  description: computed(() => `向 ${brandName.value} 提交 AstrBot 社区插件。`),
  path: "/submit",
});
const message = useMessage();
const store = usePluginStore();
const { currentUser, siteConfig } = storeToRefs(store);
const isLocalMode = computed(() => siteConfig.value.storage_mode === "local");
const isLocalAdminDirectSubmit = computed(
  () =>
    isLocalMode.value &&
    (currentUser.value?.role === "core_admin" || currentUser.value?.role === "admin"),
);
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
    .map((item) => ({
      provider: item.provider,
      label: getGitProviderLabel(item.provider),
      enabled: true,
      login_path:
        item.provider === "github"
          ? "/v1/auth/github/login"
          : `/v1/auth/oauth/${item.provider}/login`,
    }));
});
const hasEnabledOAuthLogin = computed(() => enabledOAuthProviders.value.length > 0);
const availableProviders = computed(() =>
  enabledOAuthProviders.value.length > 0
    ? enabledOAuthProviders.value
    : [
        {
          provider: "github" as OAuthProviderKey,
          label: "GitHub",
          enabled: true,
          login_path: "/v1/auth/github/login",
        },
      ],
);
const selectedProvider = shallowRef<OAuthProviderKey>("github");
const selectedProviderLabel = computed(() => getGitProviderLabel(selectedProvider.value));
const showNonGithubDirectCloneHint = computed(() => {
  if (formData.cdn_enabled) return false;
  const repo = formData.repo.trim();
  if (repo) return isSupportedGitRepoUrl(repo) && !isGithubRepoUrl(repo);
  return !isLocalAdminDirectSubmit.value && selectedProvider.value !== "github";
});

function connectProvider(provider: OAuthProviderKey): void {
  if (provider === "github") {
    store.loginWithGithub("/submit");
    return;
  }
  store.loginWithOAuth(provider, "/submit");
}
const connectSelectedProvider = () => connectProvider(selectedProvider.value);

const formRef = ref(null);
const submitting = shallowRef(false);
const metadataLoading = shallowRef(false);
const lastPreviewRepo = shallowRef("");
const maxPluginTags = computed(() => siteConfig.value.market?.max_plugin_tags || 8);
const pluginCategoryOptions = PLUGIN_CATEGORY_OPTIONS;

type SubmissionFormData = {
  cdn_enabled: boolean;
  repository_id: string;
  name: string;
  display_name: string;
  desc: string;
  short_desc: string;
  author: string;
  repo: string;
  category: string;
  tags: string[];
  social_link: string;
};

type TextAutofillField = Exclude<
  keyof SubmissionFormData,
  "repo" | "repository_id" | "tags" | "cdn_enabled"
>;

const metadataStatus = reactive({
  type: "" as "" | "success" | "warning" | "error",
  text: "",
});

const formData = reactive<SubmissionFormData>({
  cdn_enabled: false,
  repository_id: "",
  name: "",
  display_name: "",
  desc: "",
  short_desc: "",
  author: "",
  repo: "",
  category: "",
  tags: [],
  social_link: "",
});

const autoFilledTextValues = reactive<Partial<Record<TextAutofillField, string>>>({});
const autoFilledTags = shallowRef<string[]>([]);

const repositories = shallowRef<GithubPluginRepository[]>([]);
const repositoriesLoading = shallowRef(false);
const repositoriesRefreshing = shallowRef(false);
const repositoryMenuOpen = shallowRef(false);
const repositoriesError = shallowRef("");
const needsProviderConnection = shallowRef(false);
const oauthBindingBlocked = shallowRef(false);
const needsOAuthBinding = computed(
  () =>
    Boolean(currentUser.value) &&
    !isLocalAdminDirectSubmit.value &&
    (currentUser.value?.has_oauth_binding === false || oauthBindingBlocked.value),
);
const nextRepositoryPage = shallowRef<number | null>(null);
const selectedRepositoryId = shallowRef<string | null>(null);
const repositoryOptions = computed(() =>
  repositories.value.map((repo) => ({
    label: `${repo.full_name}${repo.owner_type === "Organization" ? " · 组织" : ""}${repo.fork ? " · Fork" : ""}${repo.submitted ? " · 已提交" : ""}`,
    value: repo.id,
    disabled: !!repo.submitted,
  })),
);
let repositoryRequestId = 0;
let lastRepositoryRefreshAt = 0;
let repositoryRefreshTimer: number | undefined;
onMounted(() => {
  void store.loadCurrentUser();
  window.addEventListener("focus", refreshRepositoriesAutomatically);
  window.addEventListener("online", refreshRepositoriesAutomatically);
  document.addEventListener("visibilitychange", refreshRepositoriesAutomatically);
  repositoryRefreshTimer = window.setInterval(refreshRepositoriesAutomatically, 60_000);
});
onUnmounted(() => {
  repositoryRequestId++;
  metadataRequestId++;
  window.removeEventListener("focus", refreshRepositoriesAutomatically);
  window.removeEventListener("online", refreshRepositoriesAutomatically);
  document.removeEventListener("visibilitychange", refreshRepositoriesAutomatically);
  window.clearInterval(repositoryRefreshTimer);
});

function switchProvider(provider: OAuthProviderKey): void {
  if (selectedProvider.value === provider) return;
  selectedProvider.value = provider;
  selectedRepositoryId.value = null;
  repositories.value = [];
  nextRepositoryPage.value = null;
  formData.repo = "";
  formData.repository_id = "";
  metadataRequestId++;
  metadataLoading.value = false;
  clearMetadataStatus();
  void loadRepositories(false);
}

function isProviderAuthErrorCode(code: string | undefined): boolean {
  if (!code) return false;
  return (
    code === "github_authorization_required" ||
    code === "github_authorization_denied" ||
    code.endsWith("_authorization_required") ||
    code.endsWith("_authorization_denied") ||
    code.endsWith("_token_expired")
  );
}

function refreshRepositoriesAutomatically(): void {
  if (
    document.visibilityState === "hidden" ||
    !currentUser.value ||
    needsOAuthBinding.value ||
    needsProviderConnection.value ||
    repositoriesLoading.value ||
    repositoriesRefreshing.value ||
    repositoryMenuOpen.value ||
    metadataLoading.value ||
    submitting.value ||
    Date.now() - lastRepositoryRefreshAt < 45_000
  )
    return;
  void loadRepositories(false, true);
}

function refreshRepositoriesOnOpen(show: boolean): void {
  repositoryMenuOpen.value = show;
  if (!show) refreshRepositoriesAutomatically();
}
watch(
  [
    () => currentUser.value?.id,
    () => currentUser.value?.has_oauth_binding,
    () => currentUser.value?.oauth_identities,
    isLocalAdminDirectSubmit,
  ],
  () => {
    oauthBindingBlocked.value = false;
    const identities = currentUser.value?.oauth_identities || [];
    const allowed = new Set(availableProviders.value.map((item) => item.provider));
    if (identities.length > 0) {
      const boundSelected = identities.some((item) => item.provider === selectedProvider.value);
      if (!boundSelected) {
        const firstBound = identities.find((item) => allowed.has(item.provider));
        if (firstBound) selectedProvider.value = firstBound.provider;
      }
    } else if (!allowed.has(selectedProvider.value) && availableProviders.value[0]) {
      selectedProvider.value = availableProviders.value[0].provider;
    }
    void loadRepositories(false);
  },
  { immediate: true },
);

async function loadRepositories(append: boolean, silent = false): Promise<void> {
  const requestId = ++repositoryRequestId;
  if (!currentUser.value || isLocalAdminDirectSubmit.value || needsOAuthBinding.value) {
    repositories.value = [];
    repositoriesLoading.value = false;
    repositoriesRefreshing.value = false;
    needsProviderConnection.value = false;
    repositoriesError.value = "";
    return;
  }
  repositoriesLoading.value = !silent;
  repositoriesRefreshing.value = silent;
  lastRepositoryRefreshAt = Date.now();
  if (!silent) {
    repositoriesError.value = "";
    needsProviderConnection.value = false;
  }
  let page = append ? nextRepositoryPage.value : 1;
  const found = new Map((append ? repositories.value : []).map((repo) => [repo.id, repo]));
  const provider = selectedProvider.value;
  try {
    // Keep each interaction bounded while retaining an explicit continuation for large accounts.
    for (let count = 0; page && count < 10; count++) {
      const result =
        provider === "github"
          ? await store.loadGithubRepositories(page)
          : await store.loadProviderRepositories(provider, page);
      if (requestId !== repositoryRequestId) return;
      result.items.forEach((repo) => found.set(repo.id, repo));
      page = result.next_page;
    }
    // Passive updates and partial pages must not replace the current selection or draft.
    // Submission and publication still verify current access on the server.
    if ((silent || page) && selectedRepositoryId.value && !found.has(selectedRepositoryId.value)) {
      const selected = repositories.value.find((repo) => repo.id === selectedRepositoryId.value);
      if (selected) found.set(selected.id, selected);
    }
    repositories.value = Array.from(found.values());
    repositoriesError.value = "";
    nextRepositoryPage.value = page;
    if (!page && selectedRepositoryId.value && !found.has(selectedRepositoryId.value)) {
      selectedRepositoryId.value = null;
      formData.repo = "";
      formData.repository_id = "";
      formData.social_link = "";
      metadataRequestId++;
      metadataLoading.value = false;
      clearMetadataStatus();
    }
  } catch (error) {
    if (requestId !== repositoryRequestId) return;
    const code = (error as { code?: string }).code;
    if (code === "oauth_binding_required") oauthBindingBlocked.value = true;
    needsProviderConnection.value = isProviderAuthErrorCode(code);
    if (!silent || needsProviderConnection.value || oauthBindingBlocked.value)
      repositoriesError.value = errorMessage(error, "仓库列表加载失败");
  } finally {
    if (requestId === repositoryRequestId) {
      repositoriesLoading.value = false;
      repositoriesRefreshing.value = false;
    }
  }
}

async function selectRepository(id: string): Promise<void> {
  const repo = repositories.value.find((item) => item.id === id);
  if (!repo) return;
  metadataRequestId++;
  selectedRepositoryId.value = id;
  const ownerHandle =
    currentUser.value?.public_username || currentUser.value?.github_login || repo.owner;
  const defaultAuthorPage = `${window.location.origin}/author/${encodeURIComponent(ownerHandle)}`;
  Object.assign(formData, {
    repository_id: id,
    repo: repo.repo,
    name: "",
    display_name: "",
    desc: "",
    short_desc: "",
    author: "",
    social_link: defaultAuthorPage,
    category: "",
    tags: [],
  });
  Object.keys(autoFilledTextValues).forEach(
    (key) => delete autoFilledTextValues[key as TextAutofillField],
  );
  for (const key of [
    "name",
    "display_name",
    "desc",
    "short_desc",
    "author",
    "social_link",
  ] as const)
    autoFilledTextValues[key] = formData[key];
  autoFilledTags.value = [];
  await fetchMetadataFromRepo();
}

const metadataFeedbackClass = computed(() => ({
  "metadata-feedback": true,
  "is-success": metadataStatus.type === "success",
  "is-warning": metadataStatus.type === "warning",
  "is-error": metadataStatus.type === "error",
}));

const rules = {
  name: [
    { required: true, message: "请输入插件名", trigger: "blur" },
    {
      pattern: /^astrbot_plugin_[a-z0-9_-]+$/i,
      message: "插件名必须以 astrbot_plugin_ 开头，仅含字母、数字、下划线、短横线",
      trigger: "blur",
    },
  ],
  desc: [
    { required: true, message: "请输入插件简介", trigger: "blur" },
    {
      validator: (_, value) => Array.from((value || "").toString()).length <= 120,
      message: "插件简介最多 120 字",
      trigger: ["input", "blur"],
    },
  ],
  author: {
    required: true,
    message: "请输入作者显示名",
    trigger: "blur",
  },
  repo: [
    { required: true, message: "请输入受支持平台的 Git 仓库地址", trigger: "blur" },
    {
      validator: (_, value) => isSupportedGitRepoUrl(String(value || "").trim()),
      message: "请输入有效的 GitHub / GitLab / Gitee / AtomGit / Gitea 公开 HTTPS 仓库地址",
      trigger: "blur",
    },
  ],
  tags: [
    {
      validator: (_, value) => !Array.isArray(value) || value.length <= maxPluginTags.value,
      message: () => `标签最多 ${maxPluginTags.value} 个`,
      trigger: ["change", "blur"],
    },
  ],
};

let metadataRequestId = 0;

function cleanText(value: unknown): string {
  return String(value || "").trim();
}

function clearMetadataStatus() {
  metadataStatus.type = "";
  metadataStatus.text = "";
}

function cleanTags(tags: unknown): string[] {
  const rawTags = Array.isArray(tags) ? tags : [];
  return Array.from(new Set(rawTags.map((tag) => cleanText(tag)).filter(Boolean))).slice(
    0,
    maxPluginTags.value,
  );
}

function applyTextField(field: TextAutofillField, value: unknown): boolean {
  const nextValue = cleanText(value);
  if (!nextValue) return false;
  const currentValue = cleanText(formData[field]);
  const previousAutoValue = autoFilledTextValues[field] || "";
  if (currentValue && currentValue !== previousAutoValue) return false;
  if (field === "category" && !pluginCategoryOptions.some((option) => option.value === nextValue)) {
    return false;
  }
  formData[field] = nextValue;
  autoFilledTextValues[field] = formData[field];
  return currentValue !== formData[field];
}

function applyTags(value: unknown): boolean {
  const nextTags = cleanTags(value);
  if (!nextTags.length) return false;
  const currentTags = cleanTags(formData.tags);
  if (currentTags.length && currentTags.join("\n") !== autoFilledTags.value.join("\n")) {
    return false;
  }
  formData.tags = nextTags;
  autoFilledTags.value = [...nextTags];
  return currentTags.join("\n") !== nextTags.join("\n");
}

function applyMetadataPreview(preview: PluginSubmissionMetadataPreview): number {
  let applied = 0;
  const fields: TextAutofillField[] = [
    "name",
    "display_name",
    "desc",
    "short_desc",
    "author",
    "social_link",
    "category",
  ];
  fields.forEach((field) => {
    if (applyTextField(field, preview[field])) applied += 1;
  });
  if (applyTags(preview.tags)) applied += 1;
  return applied;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function metadataErrorMessage(error: unknown): string {
  const rawMessage = errorMessage(error, "仓库信息拉取失败");
  const retrySeconds = rawMessage.match(/(\d+)\s+seconds?/i)?.[1];
  if (/rate limit|too many requests/i.test(rawMessage)) {
    return retrySeconds
      ? `自动填充请求过于频繁，请 ${retrySeconds} 秒后再试`
      : "自动填充请求过于频繁，请稍后再试";
  }
  return rawMessage;
}

async function fetchMetadataFromRepo(strict = false) {
  const repo = formData.repo.trim();
  if (!repo) {
    message.warning("请先填写 Git 仓库地址");
    return;
  }
  if (!isSupportedGitRepoUrl(repo)) {
    message.warning(
      "请输入受支持平台（GitHub / GitLab / Gitee / AtomGit / Gitea）的 HTTPS 仓库地址",
    );
    return;
  }
  if (!currentUser.value) {
    message.warning("请先登录后再读取仓库信息");
    return;
  }
  if (!siteConfig.value.market?.submissions_enabled) {
    message.warning("当前站点已暂停插件提交");
    return;
  }

  const requestId = metadataRequestId + 1;
  metadataRequestId = requestId;
  metadataLoading.value = true;
  metadataStatus.type = "";
  metadataStatus.text = "";
  try {
    const preview = await store.fetchPluginSubmissionMetadata(repo, formData.repository_id, strict);
    if (requestId !== metadataRequestId || repo !== formData.repo.trim()) return;
    const applied = applyMetadataPreview(preview);
    lastPreviewRepo.value = repo;
    metadataStatus.type = applied > 0 ? "success" : "warning";
    metadataStatus.text =
      strict && applied > 0
        ? "已从元数据一键导入插件信息，确认后即可提交"
        : applied > 0
          ? `已从元数据填充 ${applied} 项，缺失字段留空，可手动调整`
          : "元数据中没有可填充的新字段，缺失字段保持为空";
  } catch (error) {
    if (requestId !== metadataRequestId) return;
    const code = (error as { code?: string }).code;
    if (code === "oauth_binding_required") oauthBindingBlocked.value = true;
    if (isProviderAuthErrorCode(code)) needsProviderConnection.value = true;
    metadataStatus.type = "error";
    metadataStatus.text = metadataErrorMessage(error);
    message.error(metadataStatus.text);
  } finally {
    if (requestId === metadataRequestId) metadataLoading.value = false;
  }
}

const handleSubmit = () => {
  if (!siteConfig.value.market?.submissions_enabled) {
    message.warning("当前站点已暂停插件提交");
    return;
  }
  formRef.value?.validate(async (errors) => {
    if (errors) {
      message.error("请完善必填信息");
      return;
    }

    submitting.value = true;
    try {
      const result = await store.submitPlugin({ ...formData });
      message.success(
        result.cdn_enabled
          ? "已加入 CDN 审查队列，通过后提供资源链接"
          : result.status === "listed"
            ? "插件已上架"
            : "已提交上架审核",
      );
      router.push("/");
    } catch (error) {
      const code = (error as { code?: string }).code;
      if (code === "oauth_binding_required") oauthBindingBlocked.value = true;
      if (code === "github_authorization_required") needsGithubConnection.value = true;
      message.error(errorMessage(error, "提交失败"));
    } finally {
      submitting.value = false;
    }
  });
};
</script>

<style scoped>
.repository-section {
  width: 100%;
  min-width: 0;
}

.repository-picker {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
}

.provider-switcher {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.repository-hint,
.repository-url {
  display: block;
  margin: 10px 0 0;
  color: var(--text-secondary);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.connect-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.connect-github {
  margin-top: 8px;
}

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

.rule-tip {
  margin: 14px 0 0;
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

.metadata-feedback {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.5;
}

.metadata-feedback.is-success {
  color: var(--success-color);
}

.metadata-feedback.is-warning {
  color: var(--warning-color);
}

.metadata-feedback.is-error {
  color: var(--error-color);
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

  :deep(.n-input-group) {
    display: grid;
    grid-template-columns: 1fr;
  }

  :deep(.n-input-group .n-button) {
    width: 100%;
  }
}
</style>
