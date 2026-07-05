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
          <theme-mode-button circle />
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
          <p v-if="siteConfig.market.submissions_enabled">
            提交插件需要使用 GitHub OAuth 登录，用于校验仓库归属。
          </p>
          <p v-else>当前站点已暂停插件提交。</p>
          <ul>
            <li>仓库必须是公开 GitHub 仓库。</li>
            <li>插件名必须使用 `astrbot_plugin_` 前缀。</li>
          </ul>
          <p class="rule-tip">填写仓库地址后，可点击“拉取信息”自动填充，请确保仓库地址无误。</p>
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
              <n-tag
                v-else-if="!siteConfig.auth.github_login_enabled"
                type="warning"
                :bordered="false"
                >GitHub 登录未开启</n-tag
              >
              <n-tag v-else type="warning" :bordered="false">需要 GitHub 登录</n-tag>
            </div>
          </template>

          <n-form ref="formRef" :model="formData" :rules="rules" label-placement="top">
            <n-grid :x-gap="16" :y-gap="10" :cols="2" responsive="screen">
              <n-grid-item span="2">
                <n-form-item label="GitHub 仓库地址" path="repo">
                  <n-input-group>
                    <n-input
                      v-model:value="formData.repo"
                      type="url"
                      placeholder="例如：https://github.com/owner/repository…"
                      :input-props="{
                        name: 'plugin-repo',
                        autocomplete: 'off',
                        inputmode: 'url',
                        spellcheck: 'false',
                      }"
                      @update:value="handleRepoInput"
                    />
                    <n-button
                      type="primary"
                      ghost
                      :loading="metadataLoading"
                      :disabled="!canFetchMetadata"
                      @click="fetchMetadataFromRepo"
                    >
                      拉取信息
                    </n-button>
                  </n-input-group>
                  <p v-if="metadataStatus.text" :class="metadataFeedbackClass">
                    {{ metadataStatus.text }}
                  </p>
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
                <n-form-item label="展示名称" path="display_name">
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
                    placeholder="一句话说明插件能做什么…"
                    :input-props="{
                      name: 'plugin-description',
                      autocomplete: 'off',
                    }"
                    :maxlength="120"
                    :show-count="true"
                    :rows="4"
                    :resizable="false"
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
            </n-grid>
          </n-form>

          <template #footer>
            <div class="form-actions">
              <n-button quaternary @click="goBack">取消</n-button>
              <n-button
                type="primary"
                :loading="submitting"
                :disabled="!currentUser || !siteConfig.market.submissions_enabled"
                @click="handleSubmit"
              >
                提交审核
              </n-button>
            </div>
          </template>
        </n-card>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, shallowRef } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
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
  NInputGroup,
  NLayoutHeader,
  NSelect,
  NTag,
  useMessage,
} from "naive-ui";
import { ArrowBack, LogoGithub } from "@vicons/ionicons5";
import { PLUGIN_CATEGORY_OPTIONS, usePluginStore } from "@/stores/plugins";
import ThemeModeButton from "@/components/ThemeModeButton.vue";
import type { PluginSubmissionMetadataPreview } from "@/types";

const router = useRouter();
const message = useMessage();
const store = usePluginStore();
const { currentUser, siteConfig } = storeToRefs(store);
const { loginWithGithub } = store;
const formRef = ref(null);
const submitting = shallowRef(false);
const metadataLoading = shallowRef(false);
const lastPreviewRepo = shallowRef("");
const maxPluginTags = computed(() => siteConfig.value.market?.max_plugin_tags || 8);
const pluginCategoryOptions = PLUGIN_CATEGORY_OPTIONS;
const githubRepoPattern = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+(?:\.git)?\/?$/;

type SubmissionFormData = {
  name: string;
  display_name: string;
  desc: string;
  author: string;
  repo: string;
  category: string;
  tags: string[];
  social_link: string;
};

type TextAutofillField = Exclude<keyof SubmissionFormData, "repo" | "tags">;

const metadataStatus = reactive({
  type: "" as "" | "success" | "warning" | "error",
  text: "",
});

const formData = reactive<SubmissionFormData>({
  name: "",
  display_name: "",
  desc: "",
  author: "",
  repo: "",
  category: "",
  tags: [],
  social_link: "",
});

const autoFilledTextValues = reactive<Partial<Record<TextAutofillField, string>>>({});
const autoFilledTags = shallowRef<string[]>([]);

const canFetchMetadata = computed(() => {
  return (
    Boolean(currentUser.value) &&
    Boolean(siteConfig.value.market?.submissions_enabled) &&
    githubRepoPattern.test(formData.repo.trim()) &&
    !metadataLoading.value
  );
});

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
  display_name: {
    required: true,
    message: "请输入展示名称",
    trigger: "blur",
  },
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
    { required: true, message: "请输入 GitHub 仓库地址", trigger: "blur" },
    {
      pattern: githubRepoPattern,
      message: "请输入有效的 GitHub 仓库地址",
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

function handleRepoInput(value: string) {
  if (!cleanText(value)) {
    lastPreviewRepo.value = "";
    clearMetadataStatus();
    return;
  }
  if (cleanText(value) !== lastPreviewRepo.value) {
    clearMetadataStatus();
  }
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
  formData[field] = field === "desc" ? nextValue.slice(0, 120) : nextValue;
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

async function fetchMetadataFromRepo() {
  const repo = formData.repo.trim();
  if (!repo) {
    message.warning("请先填写 GitHub 仓库地址");
    return;
  }
  if (!githubRepoPattern.test(repo)) {
    message.warning("请输入有效的 GitHub 仓库地址");
    return;
  }
  if (!currentUser.value) {
    message.warning("请先使用 GitHub 登录");
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
    const preview = await store.fetchPluginSubmissionMetadata(repo);
    if (requestId !== metadataRequestId || repo !== formData.repo.trim()) return;
    const applied = applyMetadataPreview(preview);
    lastPreviewRepo.value = repo;
    metadataStatus.type = applied > 0 ? "success" : "warning";
    metadataStatus.text =
      applied > 0
        ? `已自动填充 ${applied} 项，可继续手动调整`
        : "仓库信息已拉取，没有可填充的新字段";
  } catch (error) {
    if (requestId !== metadataRequestId) return;
    metadataStatus.type = "error";
    metadataStatus.text = metadataErrorMessage(error);
    message.error(metadataStatus.text);
  } finally {
    if (requestId === metadataRequestId) metadataLoading.value = false;
  }
}

const goBack = () => {
  router.back();
};

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
      await store.submitPlugin({ ...formData });
      message.success("已提交审核");
      router.push("/");
    } catch (error) {
      message.error(errorMessage(error, "提交失败"));
    } finally {
      submitting.value = false;
    }
  });
};
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
