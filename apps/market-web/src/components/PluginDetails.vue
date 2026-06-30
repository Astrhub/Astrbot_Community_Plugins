<template>
  <n-modal
    v-model:show="show"
    :mask-closable="true"
    preset="card"
    :class="['plugin-details', { 'plugin-details--dark': isDarkMode }]"
    style="max-width: 900px; width: 90%"
    :bordered="false"
  >
    <template #header>
      <div class="plugin-details__header">
        <n-h2 class="plugin-details__title">
          <n-space align="center" :size="12">
            <n-icon size="24">
              <extension-puzzle-outline />
            </n-icon>
            {{ plugin?.name }}
            <n-tag type="success" size="small" :bordered="false">
              {{ plugin?.version?.startsWith("v") ? plugin?.version : "v" + plugin?.version }}
            </n-tag>
          </n-space>
        </n-h2>
      </div>
    </template>

    <div class="plugin-details__content">
      <n-space vertical size="large">
        <div class="plugin-actions">
          <n-button
            v-if="likesEnabled"
            secondary
            type="primary"
            :loading="liking"
            @click="toggleLike"
          >
            <template #icon>
              <n-icon><heart-outline /></n-icon>
            </template>
            {{ liked ? "取消点赞" : "点赞" }} {{ detail?.likes ?? plugin?.likes ?? 0 }}
          </n-button>
          <n-button
            v-if="canManagePlugin"
            secondary
            :loading="refreshing"
            @click="openRefreshModal"
          >
            刷新 GitHub
          </n-button>
          <span v-if="!likesEnabled" class="muted-text">点赞已关闭</span>
        </div>

        <div v-if="loading" class="readme-loading">
          <n-spin size="medium">
            <template #description> 正在加载 README... </template>
          </n-spin>
        </div>
        <div v-else-if="error" class="readme-error">
          <n-empty description="加载 README 失败">
            <template #extra>
              <n-button size="small" @click="fetchReadme"> 重试 </n-button>
            </template>
          </n-empty>
        </div>
        <template v-else>
          <div v-if="readmeHtml && readmeNavigationVisible" class="readme-navigation">
            <div class="readme-navigation__main">
              <n-button v-if="canGoBackReadme" size="small" tertiary @click="goBackReadmeFile">
                <template #icon>
                  <n-icon><arrow-back-outline /></n-icon>
                </template>
                返回
              </n-button>
              <span class="readme-current-path">
                <n-icon size="16"><document-text-outline /></n-icon>
                {{ readmeViewTitle }}
              </span>
            </div>
            <n-button
              v-if="isViewingRepositoryFile"
              size="small"
              quaternary
              @click="restoreRootReadme"
            >
              README
            </n-button>
          </div>
          <div
            ref="markdownContentRef"
            class="markdown-content"
            @click="handleMarkdownClick"
            v-html="readmeHtml"
          ></div>
        </template>

        <plugin-comment
          :comments="comments"
          :comments-enabled="commentsEnabled"
          :likes-enabled="likesEnabled"
          @submit="submitComment"
          @delete="deleteComment"
          @like="toggleCommentLike"
        />
      </n-space>
    </div>

    <template #footer>
      <div class="plugin-details__footer">
        <n-space justify="end" :size="12">
          <n-button secondary type="primary" @click="openUrl(plugin?.repo)">
            <template #icon>
              <n-icon><logo-github /></n-icon>
            </template>
            查看仓库
          </n-button>
          <n-button type="primary" @click="show = false"> 关闭 </n-button>
        </n-space>
      </div>
    </template>
  </n-modal>

  <n-modal
    v-model:show="showRefreshModal"
    preset="card"
    title="刷新 GitHub 元数据"
    style="max-width: 520px"
  >
    <n-space vertical size="medium">
      <n-alert type="info" :bordered="false">
        Token 只需要公开仓库读取权限。留空会使用已保存的个人 Token 或站点兜底 Token。
      </n-alert>
      <n-form label-placement="top">
        <n-form-item label="临时 GitHub Token" path="github_token">
          <n-input
            v-model:value="refreshForm.github_token"
            type="password"
            show-password-on="click"
            placeholder="可选，ghp_… 或 fine-grained token…"
          />
        </n-form-item>
        <n-checkbox v-model:checked="refreshForm.save_token">
          保存到个人设置，后续自动同步优先使用
        </n-checkbox>
        <n-form-item label="刷新间隔（秒）" path="refresh_interval_seconds">
          <n-input-number
            v-model:value="refreshForm.refresh_interval_seconds"
            :min="300"
            :max="86400"
            :step="300"
          />
        </n-form-item>
      </n-form>
    </n-space>
    <template #footer>
      <div class="refresh-actions">
        <n-button tertiary @click="showRefreshModal = false">取消</n-button>
        <n-button type="primary" :loading="refreshing" @click="confirmRefreshGithub">
          刷新
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import {
  NModal,
  NSpace,
  NH2,
  NIcon,
  NTag,
  NButton,
  NAlert,
  NCheckbox,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSpin,
  NEmpty,
  useDialog,
  useMessage,
} from "naive-ui";
import { storeToRefs } from "pinia";
import { usePluginStore } from "../stores/plugins";
import PluginComment from "./PluginComment.vue";
import {
  ArrowBackOutline,
  DocumentTextOutline,
  ExtensionPuzzleOutline,
  HeartOutline,
  LogoGithub,
} from "@vicons/ionicons5";

const props = defineProps({
  show: Boolean,
  plugin: Object,
});

const emit = defineEmits(["update:show"]);

const show = ref(props.show);
const loading = ref(false);
const error = ref(false);
const readmeHtml = ref("");
const readmeContext = ref(null);
const rootReadmeView = ref(null);
const readmeHistory = ref([]);
const readmeView = ref({
  kind: "readme",
  path: "",
  browserUrl: "",
});
const markdownContentRef = ref(null);
const detail = ref(null);
const liking = ref(false);
const liked = ref(false);
const refreshing = ref(false);
const showRefreshModal = ref(false);
const refreshForm = ref({
  github_token: "",
  save_token: false,
  refresh_interval_seconds: 3600,
});
const comments = computed(() => detail.value?.comments || []);

const store = usePluginStore();
const message = useMessage();
const dialog = useDialog();
const { siteConfig, currentUser, isDarkMode } = storeToRefs(store);
const {
  addPluginComment,
  deletePluginComment,
  likePlugin,
  loadPluginDetail,
  loadCurrentUser,
  likePluginComment,
  refreshPluginGithubMetadata,
  updatePluginInList,
  unlikePluginComment,
  unlikePlugin,
} = store;
const MARKDOWN_FILE_EXTENSIONS = new Set([".md", ".markdown", ".mdown", ".mkd"]);
const IMAGE_FILE_EXTENSIONS = new Set([".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"]);
const TEXT_FILE_EXTENSIONS = new Set([
  ".cfg",
  ".conf",
  ".css",
  ".env",
  ".ini",
  ".js",
  ".json",
  ".log",
  ".mjs",
  ".py",
  ".sh",
  ".toml",
  ".ts",
  ".txt",
  ".vue",
  ".yaml",
  ".yml",
]);
const MAX_INLINE_FILE_SIZE = 300000;
const commentsEnabled = computed(() => Boolean(siteConfig.value.market?.comments_enabled));
const likesEnabled = computed(() => Boolean(siteConfig.value.market?.likes_enabled));
const activePlugin = computed(() => detail.value || props.plugin || {});
const canGoBackReadme = computed(() => readmeHistory.value.length > 0);
const isViewingRepositoryFile = computed(() => readmeView.value.kind !== "readme");
const readmeNavigationVisible = computed(
  () => canGoBackReadme.value || isViewingRepositoryFile.value,
);
const readmeViewTitle = computed(() => {
  if (readmeView.value.kind === "directory") return `${readmeView.value.path || "/"} /`;
  if (readmeView.value.path) return readmeView.value.path;
  return "README";
});
const canManagePlugin = computed(() => {
  const user = currentUser.value;
  const plugin = activePlugin.value;
  if (!user || !plugin?.id) return false;
  if (["core_admin", "admin"].includes(user.role)) return true;
  return Boolean(
    plugin.owner_user_id === user.id ||
    (plugin.owner_github_login && plugin.owner_github_login === user.github_login),
  );
});

watch(
  () => props.show,
  (newVal) => {
    show.value = newVal;
  },
);

watch(show, (newVal) => {
  emit("update:show", newVal);
  if (newVal) {
    loadCurrentUser();
    fetchDetail();
    fetchReadme();
  }
});

const openUrl = (url) => {
  if (url) {
    confirmExternalOpen(url);
  }
};

async function fetchReadme() {
  if (!props.plugin?.repo) return;

  loading.value = true;
  error.value = false;

  try {
    const repoInfo = parseGithubRepo(props.plugin.repo);
    if (!repoInfo) throw new Error("Invalid GitHub repo URL");
    const { owner, repo } = repoInfo;

    let readmeText = "";
    let readmeContext = {
      owner,
      repo,
      branch: "main",
      path: "README.md",
    };

    try {
      const apiResp = await fetchWithTimeout(
        `https://api.github.com/repos/${owner}/${repo}/readme`,
        {
          method: "GET",
          headers: {
            Accept: "application/vnd.github+json",
          },
        },
        10000,
      );

      if (apiResp.ok) {
        const data = await apiResp.json();
        readmeText = decodeBase64Content(data.content || "");
        readmeContext = {
          owner,
          repo,
          branch:
            data.download_url?.split("/")[5] ||
            data.html_url?.split("/blob/")[1]?.split("/")[0] ||
            "main",
          path: data.path || "README.md",
        };
      } else {
        throw new Error(`GitHub API /readme returned ${apiResp.status}`);
      }
    } catch (apiErr) {
      const branches = ["main", "master"];
      const candidates = ["README.md", "Readme.md", "readme.md", "README.MD", "README"];

      let found = false;
      for (const branch of branches) {
        for (const filename of candidates) {
          try {
            const resp = await fetchWithTimeout(
              `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${filename}`,
              {
                method: "GET",
                headers: {
                  Accept: "text/plain",
                },
              },
              10000,
            );
            if (resp.ok) {
              readmeText = await resp.text();
              readmeContext = { owner, repo, branch, path: filename };
              found = true;
              break;
            }
          } catch (_) {
            // 忽略，尝试下一个候选
          }
        }
        if (found) break;
      }

      if (!found) {
        throw new Error("无法获取 README（API 与镜像均失败）");
      }
    }

    if (!readmeText) {
      throw new Error("README 内容为空");
    }

    setReadmeDocument({
      html: renderReadmeHtml(readmeText, readmeContext),
      context: readmeContext,
      view: {
        kind: "readme",
        path: readmeContext.path,
        browserUrl: `https://github.com/${owner}/${repo}/blob/${readmeContext.branch}/${readmeContext.path}`,
      },
    });
    rootReadmeView.value = currentReadmeSnapshot();
    readmeHistory.value = [];
  } catch (err) {
    console.error("Error fetching README:", err);
    error.value = true;
  } finally {
    loading.value = false;
  }
}

async function fetchDetail() {
  if (!props.plugin?.id) return;
  try {
    detail.value = await loadPluginDetail(props.plugin.id);
    updatePluginInList(detail.value);
    liked.value = Boolean(detail.value?.liked);
  } catch (err) {
    message.error(err.message || "加载互动信息失败");
  }
}

async function toggleLike() {
  if (!props.plugin?.id) return;
  liking.value = true;
  try {
    detail.value = liked.value
      ? await unlikePlugin(props.plugin.id)
      : await likePlugin(props.plugin.id);
    updatePluginInList(detail.value);
    liked.value = Boolean(detail.value?.liked);
  } catch (err) {
    message.error(err.message || "操作失败");
  } finally {
    liking.value = false;
  }
}

function openRefreshModal() {
  refreshForm.value = {
    github_token: "",
    save_token: false,
    refresh_interval_seconds: currentUser.value?.github_refresh_interval_seconds || 3600,
  };
  showRefreshModal.value = true;
}

async function confirmRefreshGithub() {
  if (!props.plugin?.id) return;
  refreshing.value = true;
  try {
    const payload = {
      github_token: refreshForm.value.github_token.trim(),
      save_token: refreshForm.value.save_token,
      refresh_interval_seconds: Number(refreshForm.value.refresh_interval_seconds || 3600),
    };
    detail.value = await refreshPluginGithubMetadata(props.plugin.id, payload);
    updatePluginInList(detail.value);
    await loadCurrentUser();
    await fetchDetail();
    showRefreshModal.value = false;
    message.success("GitHub 数据已刷新");
  } catch (err) {
    message.error(err.message || "刷新失败，请填写只读 GitHub Token 后重试");
  } finally {
    refreshing.value = false;
  }
}

async function submitComment(payload) {
  if (!props.plugin?.id) return;
  try {
    await addPluginComment(props.plugin.id, payload);
    await fetchDetail();
    message.success(payload.parent_id ? "回复已发布" : "评价已发布");
    payload.done?.();
  } catch (err) {
    message.error(err.message || "发布失败");
    payload.fail?.();
  }
}

async function deleteComment(comment) {
  dialog.warning({
    title: "删除评论",
    content: "确认删除这条评论？",
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        await deletePluginComment(comment.id);
        await fetchDetail();
        message.success("评论已删除");
      } catch (err) {
        message.error(err.message || "删除失败");
      }
    },
  });
}

async function toggleCommentLike(payload) {
  try {
    const updated = await (payload.liked
      ? likePluginComment(payload.comment.id)
      : unlikePluginComment(payload.comment.id));
    updateCommentInDetail(updated);
  } catch (err) {
    message.error(err.message || "操作失败");
  }
}

function parseGithubRepo(repoUrl) {
  try {
    const url = new URL(repoUrl);
    if (url.hostname !== "github.com") return null;
    const [owner, repo] = url.pathname.split("/").filter(Boolean);
    if (!owner || !repo) return null;
    return { owner, repo: repo.replace(/\.git$/, "") };
  } catch (_) {
    return null;
  }
}

function decodeBase64Content(value) {
  const normalized = String(value || "").replace(/\s/g, "");
  if (!normalized) return "";
  try {
    return decodeURIComponent(escape(atob(normalized)));
  } catch (_) {
    return atob(normalized);
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function currentReadmeSnapshot() {
  return {
    html: readmeHtml.value,
    context: readmeContext.value ? { ...readmeContext.value } : null,
    view: { ...readmeView.value },
  };
}

function setReadmeDocument(snapshot) {
  readmeHtml.value = snapshot.html || "";
  readmeContext.value = snapshot.context ? { ...snapshot.context } : null;
  readmeView.value = {
    kind: "readme",
    path: "",
    browserUrl: "",
    ...snapshot.view,
  };
}

function restoreReadmeSnapshot(snapshot) {
  if (!snapshot) return;
  setReadmeDocument(snapshot);
  nextTick(() => {
    scrollMarkdownAnchor(snapshot.view?.hash || "");
  });
}

function goBackReadmeFile() {
  const snapshot = readmeHistory.value.pop();
  restoreReadmeSnapshot(snapshot);
}

function restoreRootReadme() {
  if (!rootReadmeView.value) return;
  readmeHistory.value = [];
  restoreReadmeSnapshot(rootReadmeView.value);
}

async function openReadmeTarget(target) {
  if (!target) return false;
  if (target.kind === "anchor") {
    scrollMarkdownAnchor(target.hash);
    return true;
  }
  if (target.kind === "root") {
    restoreRootReadme();
    return true;
  }
  if (isCurrentReadmeTarget(target)) {
    scrollMarkdownAnchor(target.hash);
    return true;
  }

  const previous = currentReadmeSnapshot();
  loading.value = true;
  error.value = false;
  try {
    await loadRepositoryResource(target);
    readmeHistory.value.push(previous);
    await nextTick();
    scrollMarkdownAnchor(target.hash);
    return true;
  } catch (err) {
    console.error("Error loading repository file:", err);
    message.warning(err.message || "无法在站内预览该文件");
    if (target.browserUrl) confirmExternalOpen(target.browserUrl);
    return true;
  } finally {
    loading.value = false;
  }
}

function isCurrentReadmeTarget(target) {
  const context = readmeContext.value;
  if (!context || !target?.path) return false;
  return (
    sameText(context.owner, target.owner) &&
    sameText(context.repo, target.repo) &&
    context.branch === target.branch &&
    normalizeReadmePath("", context.path) === normalizeReadmePath("", target.path)
  );
}

async function loadRepositoryResource(target) {
  const data = await fetchGithubContents(target);
  if (Array.isArray(data)) {
    setReadmeDocument({
      html: renderDirectoryListingHtml(data, target),
      context: {
        owner: target.owner,
        repo: target.repo,
        branch: target.branch,
        path: target.path || "",
      },
      view: {
        kind: "directory",
        path: target.path || "/",
        browserUrl: target.browserUrl,
      },
    });
    return;
  }

  const path = data.path || target.path || "";
  if (isImageFile(path)) {
    const imageUrl = data.download_url || target.rawUrl;
    if (!imageUrl) throw new Error("无法获取图片地址");
    setReadmeDocument({
      html: renderImageFileHtml(imageUrl, path),
      context: {
        owner: target.owner,
        repo: target.repo,
        branch: target.branch,
        path,
      },
      view: {
        kind: "file",
        path,
        browserUrl: data.html_url || target.browserUrl,
      },
    });
    return;
  }

  const text = await readGithubFileText(data, target);
  if (!isMarkdownFile(path) && !isPreviewableTextFile(path, text)) {
    throw new Error("此文件类型暂不支持站内预览");
  }

  const context = {
    owner: target.owner,
    repo: target.repo,
    branch: target.branch,
    path,
  };
  setReadmeDocument({
    html: isMarkdownFile(path) ? renderReadmeHtml(text, context) : renderTextFileHtml(text, path),
    context,
    view: {
      kind: isMarkdownFile(path) ? "markdown" : "file",
      path,
      browserUrl: data.html_url || target.browserUrl,
    },
  });
}

async function fetchGithubContents(target) {
  const path = encodeGithubPath(target.path);
  const pathSuffix = path ? `/${path}` : "";
  const response = await fetchWithTimeout(
    `https://api.github.com/repos/${target.owner}/${target.repo}/contents${pathSuffix}?ref=${encodeURIComponent(target.branch)}`,
    {
      method: "GET",
      headers: { Accept: "application/vnd.github+json" },
    },
    10000,
  );
  if (response.ok) return response.json();
  if (!target.rawUrl || target.kind === "directory") {
    throw new Error(`GitHub contents API 返回 ${response.status}`);
  }
  const rawResponse = await fetchWithTimeout(
    target.rawUrl,
    {
      method: "GET",
      headers: { Accept: "text/plain" },
    },
    10000,
  );
  if (!rawResponse.ok) throw new Error(`raw 文件返回 ${rawResponse.status}`);
  return {
    type: "file",
    path: target.path,
    html_url: target.browserUrl,
    rawText: await rawResponse.text(),
  };
}

async function readGithubFileText(data, target) {
  if (typeof data.rawText === "string") return data.rawText;
  if (data.size > MAX_INLINE_FILE_SIZE) {
    throw new Error("文件过大，已保留为外链打开");
  }
  if (data.encoding === "base64" && data.content) {
    return decodeBase64Content(data.content);
  }
  const downloadUrl = data.download_url || target.rawUrl;
  if (!downloadUrl) throw new Error("无法获取文件内容");
  const response = await fetchWithTimeout(
    downloadUrl,
    {
      method: "GET",
      headers: { Accept: "text/plain" },
    },
    10000,
  );
  if (!response.ok) throw new Error(`文件下载返回 ${response.status}`);
  const text = await response.text();
  if (text.length > MAX_INLINE_FILE_SIZE) {
    throw new Error("文件过大，已保留为外链打开");
  }
  return text;
}

function renderReadmeHtml(markdown, context) {
  const container = document.createElement("div");
  container.innerHTML = DOMPurify.sanitize(marked(markdown), {
    USE_PROFILES: { html: true },
  });
  const basePath = context.path.split("/").slice(0, -1).join("/");

  container.querySelectorAll("img[src]").forEach((image) => {
    image.src = resolveReadmeUrl(image.getAttribute("src"), context, basePath, true);
    image.loading = "lazy";
  });

  container.querySelectorAll("a[href]").forEach((link) => {
    const cleanHref = normalizeRenderedHref(link);
    const target = buildReadmeLinkTarget(cleanHref, context, basePath);
    if (target?.kind === "anchor") {
      link.href = target.hash;
      return;
    }
    if (target) {
      applyInternalReadmeLink(link, target);
      return;
    }
    const href = resolveReadmeUrl(cleanHref, context, basePath, false);
    link.href = href;
    if (!href.startsWith("#")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  });

  container.querySelectorAll("pre").forEach((pre) => {
    if (
      !pre.querySelector("code") ||
      pre.parentElement?.classList.contains("markdown-code-block")
    ) {
      return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "markdown-code-block";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "markdown-copy-code";
    button.dataset.copyCode = "true";
    button.textContent = "复制";
    pre.replaceWith(wrapper);
    wrapper.append(button, pre);
  });

  return container.innerHTML;
}

function renderDirectoryListingHtml(entries, target) {
  const container = document.createElement("div");
  const title = document.createElement("p");
  title.className = "markdown-file-note";
  title.textContent = "目录内容";
  const list = document.createElement("ul");
  list.className = "markdown-directory-list";
  entries
    .filter((entry) => entry?.name && ["dir", "file"].includes(entry.type))
    .sort((a, b) => {
      if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    })
    .forEach((entry) => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      const childTarget = createRepositoryTarget({
        owner: target.owner,
        repo: target.repo,
        branch: target.branch,
        path: entry.path,
        kind: entry.type === "dir" ? "directory" : "file",
      });
      link.textContent = `${entry.type === "dir" ? "目录" : "文件"} ${entry.name}`;
      applyInternalReadmeLink(link, childTarget);
      item.append(link);
      list.append(item);
    });
  container.append(title, list);
  return container.innerHTML;
}

function renderTextFileHtml(text, path) {
  const wrapper = document.createElement("div");
  const note = document.createElement("p");
  note.className = "markdown-file-note";
  note.textContent = "文件预览";
  const block = document.createElement("div");
  block.className = "markdown-code-block";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "markdown-copy-code";
  button.dataset.copyCode = "true";
  button.textContent = "复制";
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.className = languageClassFromPath(path);
  code.textContent = text || " ";
  pre.append(code);
  block.append(button, pre);
  wrapper.append(note, block);
  return wrapper.innerHTML;
}

function renderImageFileHtml(imageUrl, path) {
  const wrapper = document.createElement("div");
  const note = document.createElement("p");
  note.className = "markdown-file-note";
  note.textContent = "图片预览";
  const image = document.createElement("img");
  image.src = imageUrl;
  image.alt = path;
  image.loading = "lazy";
  wrapper.append(note, image);
  return wrapper.innerHTML;
}

function buildReadmeLinkTarget(url, context, basePath) {
  if (!url || /^mailto:/i.test(url)) return null;
  if (url.startsWith("#")) return { kind: "anchor", hash: url };
  const absoluteTarget = githubUrlToReadmeTarget(url, context);
  if (absoluteTarget) return absoluteTarget;
  if (/^[a-z][a-z\d+.-]*:/i.test(url) || url.startsWith("//")) return null;

  const [path, suffix = ""] = splitReadmeUrl(url);
  if (!path) return suffix.startsWith("#") ? { kind: "anchor", hash: suffix } : null;
  const cleanPath = normalizeReadmePath(path.startsWith("/") ? "" : basePath, path);
  if (!cleanPath) return { kind: "root" };
  return createRepositoryTarget({
    owner: context.owner,
    repo: context.repo,
    branch: context.branch,
    path: cleanPath,
    hash: extractUrlHash(suffix),
    kind: path.endsWith("/") ? "directory" : "file",
  });
}

function githubUrlToReadmeTarget(url, context) {
  try {
    const parsed = new URL(url.startsWith("//") ? `https:${url}` : url);
    if (parsed.hostname === "raw.githubusercontent.com") {
      const [owner, repo, branch, ...pathParts] = parsed.pathname.split("/").filter(Boolean);
      if (!isSameGithubRepo(owner, repo, context) || !branch || !pathParts.length) return null;
      return createRepositoryTarget({
        owner,
        repo,
        branch,
        path: decodeGithubPath(pathParts),
        hash: parsed.hash,
        kind: "file",
      });
    }
    if (parsed.hostname !== "github.com") return null;
    const [owner, repo, mode, branch, ...pathParts] = parsed.pathname.split("/").filter(Boolean);
    if (!isSameGithubRepo(owner, repo, context)) return null;
    if (!mode) return { kind: "root" };
    if (!["blob", "tree", "raw"].includes(mode) || !branch) return null;
    if (!pathParts.length) {
      return mode === "tree"
        ? createRepositoryTarget({
            owner,
            repo,
            branch,
            path: "",
            hash: parsed.hash,
            kind: "directory",
          })
        : { kind: "root" };
    }
    return createRepositoryTarget({
      owner,
      repo,
      branch,
      path: decodeGithubPath(pathParts),
      hash: parsed.hash,
      kind: mode === "tree" ? "directory" : "file",
    });
  } catch (_) {
    return null;
  }
}

function createRepositoryTarget({ owner, repo, branch, path, hash = "", kind = "file" }) {
  const cleanPath = normalizeReadmePath("", path);
  const encodedPath = encodeGithubPath(cleanPath);
  const browserMode = kind === "directory" ? "tree" : "blob";
  const browserPath = encodedPath ? `/${encodedPath}` : "";
  return {
    kind,
    owner,
    repo,
    branch,
    path: cleanPath,
    hash,
    browserUrl: `https://github.com/${owner}/${repo}/${browserMode}/${branch}${browserPath}${hash}`,
    rawUrl: cleanPath
      ? `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${encodedPath}${hash}`
      : "",
  };
}

function applyInternalReadmeLink(link, target) {
  link.href = target.browserUrl || "#";
  link.dataset.readmeInternal = "true";
  link.dataset.readmeKind = target.kind;
  link.dataset.readmeOwner = target.owner || "";
  link.dataset.readmeRepo = target.repo || "";
  link.dataset.readmeBranch = target.branch || "";
  link.dataset.readmePath = target.path || "";
  link.dataset.readmeHash = target.hash || "";
  link.dataset.readmeBrowserUrl = target.browserUrl || "";
  link.dataset.readmeRawUrl = target.rawUrl || "";
  link.classList.add("markdown-internal-link");
  link.removeAttribute("target");
  link.removeAttribute("rel");
}

function normalizeRenderedHref(link) {
  const href = link.getAttribute("href") || "";
  if (!isPlainUrlLink(link, href)) return href;
  const { cleanHref, suffix } = trimTrailingLinkSuffix(href);
  if (!suffix) return href;
  if (link.textContent.endsWith(suffix)) {
    link.textContent = link.textContent.slice(0, -suffix.length);
  }
  link.after(document.createTextNode(suffix));
  return cleanHref;
}

function isPlainUrlLink(link, href) {
  const text = link.textContent || "";
  return /^(https?:)?\/\//i.test(href) && (text === href || text === decodeUrlText(href));
}

function decodeUrlText(value) {
  try {
    return decodeURI(value);
  } catch (_) {
    return value;
  }
}

function trimTrailingLinkSuffix(href) {
  let cleanHref = href;
  let suffix = "";
  while (cleanHref) {
    const char = cleanHref.at(-1);
    if (!shouldTrimLinkSuffix(cleanHref, char)) break;
    suffix = char + suffix;
    cleanHref = cleanHref.slice(0, -1);
  }
  return { cleanHref, suffix };
}

function shouldTrimLinkSuffix(value, char) {
  const closers = {
    ")": "(",
    "]": "[",
    "}": "{",
    "）": "（",
    "】": "【",
    "》": "《",
    "」": "「",
    "』": "『",
  };
  if ("。，、；：！？!?;,".includes(char) || char === ".") return true;
  if (!closers[char]) return false;
  return countChars(value, char) > countChars(value, closers[char]);
}

function countChars(value, char) {
  return [...value].filter((item) => item === char).length;
}

function resolveReadmeUrl(url, context, basePath, raw) {
  if (!url || /^(mailto:|#)/i.test(url)) return url;
  const absoluteUrl = normalizeAbsoluteReadmeUrl(url, raw);
  if (absoluteUrl) return absoluteUrl;
  const [path, suffix = ""] = splitReadmeUrl(url);
  if (!path) return suffix || url;
  const cleanPath = normalizeReadmePath(path.startsWith("/") ? "" : basePath, path);
  const encodedPath = cleanPath.split("/").map(encodeURIComponent).join("/");
  const host = raw ? "raw.githubusercontent.com" : "github.com";
  const mode = raw ? "" : "/blob";
  return `https://${host}/${context.owner}/${context.repo}${mode}/${context.branch}/${encodedPath}${suffix}`;
}

function normalizeAbsoluteReadmeUrl(url, raw) {
  if (url.startsWith("//")) return `https:${url}`;
  if (!/^https?:/i.test(url)) return "";
  return raw ? githubBlobUrlToRaw(url) || url : url;
}

function githubBlobUrlToRaw(url) {
  try {
    const parsed = new URL(url);
    if (parsed.hostname !== "github.com") return "";
    const parts = parsed.pathname.split("/").filter(Boolean);
    if (parts.length < 5 || parts[2] !== "blob") return "";
    const [owner, repo, , branch, ...pathParts] = parts;
    if (!owner || !repo || !branch || !pathParts.length) return "";
    const path = pathParts.map(encodeURIComponent).join("/");
    return `https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${path}${parsed.search}${parsed.hash}`;
  } catch (_) {
    return "";
  }
}

function splitReadmeUrl(url) {
  const match = String(url).match(/^([^?#]*)([?#].*)?$/);
  return [match?.[1] || "", match?.[2] || ""];
}

function normalizeReadmePath(basePath, url) {
  const cleanUrl = String(url || "").replace(/^\/+/, "");
  const parts = `${basePath ? `${basePath}/` : ""}${cleanUrl}`.split("/");
  const normalized = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      normalized.pop();
    } else {
      normalized.push(part);
    }
  }
  return normalized.join("/");
}

function readmeTargetFromLink(link) {
  if (link.dataset.readmeInternal !== "true") return null;
  return {
    kind: link.dataset.readmeKind || "file",
    owner: link.dataset.readmeOwner || "",
    repo: link.dataset.readmeRepo || "",
    branch: link.dataset.readmeBranch || "main",
    path: link.dataset.readmePath || "",
    hash: link.dataset.readmeHash || "",
    browserUrl: link.dataset.readmeBrowserUrl || link.href,
    rawUrl: link.dataset.readmeRawUrl || "",
  };
}

function encodeGithubPath(path) {
  return String(path || "")
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent)
    .join("/");
}

function decodeGithubPath(pathParts) {
  return pathParts
    .map((part) => {
      try {
        return decodeURIComponent(part);
      } catch (_) {
        return part;
      }
    })
    .join("/");
}

function extractUrlHash(suffix) {
  const hashIndex = String(suffix || "").indexOf("#");
  return hashIndex === -1 ? "" : suffix.slice(hashIndex);
}

function isSameGithubRepo(owner, repo, context) {
  return sameText(owner, context?.owner) && sameText(repo, context?.repo);
}

function sameText(left, right) {
  return String(left || "").toLowerCase() === String(right || "").toLowerCase();
}

function fileExtension(path) {
  const filename =
    String(path || "")
      .split("/")
      .pop() || "";
  const dotIndex = filename.lastIndexOf(".");
  return dotIndex === -1 ? "" : filename.slice(dotIndex).toLowerCase();
}

function isMarkdownFile(path) {
  return MARKDOWN_FILE_EXTENSIONS.has(fileExtension(path));
}

function isImageFile(path) {
  return IMAGE_FILE_EXTENSIONS.has(fileExtension(path));
}

function isPreviewableTextFile(path, text) {
  if (String(text || "").length > MAX_INLINE_FILE_SIZE) return false;
  const extension = fileExtension(path);
  if (TEXT_FILE_EXTENSIONS.has(extension) || !extension) return isProbablyText(text);
  return isProbablyText(text);
}

function isProbablyText(text) {
  const value = String(text || "");
  if (!value) return true;
  const sample = value.slice(0, 4096);
  // eslint-disable-next-line no-control-regex -- intentional: detect control chars to classify README as binary vs text
  const controlChars = sample.match(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g) || [];
  return controlChars.length / sample.length < 0.02;
}

function languageClassFromPath(path) {
  const extension = fileExtension(path).slice(1);
  return extension ? `language-${extension}` : "";
}

function scrollMarkdownAnchor(hash) {
  if (!hash || !markdownContentRef.value) return;
  const id = decodeHash(hash);
  if (!id) return;
  const target = [...markdownContentRef.value.querySelectorAll("[id], a[name]")].find(
    (element) => element.id === id || element.getAttribute("name") === id,
  );
  target?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function decodeHash(hash) {
  try {
    return decodeURIComponent(String(hash || "").replace(/^#/, ""));
  } catch (_) {
    return String(hash || "").replace(/^#/, "");
  }
}

function handleMarkdownClick(event) {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  if (!target) return;
  const copyButton = target.closest("[data-copy-code]");
  if (copyButton) {
    event.preventDefault();
    copyMarkdownCode(copyButton);
    return;
  }
  const link = target.closest("a[href]");
  if (!link) return;
  const internalTarget = readmeTargetFromLink(link);
  if (internalTarget) {
    event.preventDefault();
    openReadmeTarget(internalTarget);
    return;
  }
  const href = link.getAttribute("href");
  if (!href) return;
  if (href.startsWith("#")) {
    event.preventDefault();
    scrollMarkdownAnchor(href);
    return;
  }
  event.preventDefault();
  confirmExternalOpen(link.href);
}

async function copyMarkdownCode(button) {
  const code = button.closest(".markdown-code-block")?.querySelector("code")?.textContent;
  if (!code) return;
  try {
    await copyText(code);
    button.textContent = "已复制";
    setTimeout(() => {
      button.textContent = "复制";
    }, 1600);
  } catch (_) {
    message.error("复制失败，请手动复制");
  }
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

function updateCommentInDetail(updated) {
  if (!detail.value?.comments || !updated?.id) return;
  detail.value = {
    ...detail.value,
    comments: detail.value.comments.map((comment) =>
      comment.id === updated.id ? { ...comment, ...updated } : comment,
    ),
  };
}

function confirmExternalOpen(url) {
  if (!url) return;
  dialog.info({
    title: "即将打开外链",
    content: `将跳转到：${url}`,
    positiveText: "继续打开",
    negativeText: "取消",
    onPositiveClick: () => {
      window.open(url, "_blank", "noopener,noreferrer");
    },
  });
}
</script>

<style scoped>
.plugin-details {
  --modal-padding: 24px !important;
}

.plugin-details :deep(.n-modal) {
  max-height: 90vh !important;
}

.plugin-details__header {
  padding: 0 var(--modal-padding);
  margin: calc(-1 * var(--modal-padding)) calc(-1 * var(--modal-padding)) 0;
  padding-top: var(--modal-padding);
  padding-bottom: 16px;
  border-bottom: 1px solid var(--n-border-color);
}

.plugin-details__title {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.plugin-details__content {
  padding: var(--modal-padding) 0;
  padding-right: 16px;
  margin-right: 4px;
  overflow-y: auto;
  max-height: calc(80vh - 180px);
}

.plugin-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
}

.readme-navigation {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--n-border-color);
}

.readme-navigation__main {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.readme-current-path {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--n-text-color-2);
  font-size: 13px;
  font-weight: 600;
  word-break: break-all;
}

.muted-text {
  color: var(--n-text-color-3);
  font-size: 13px;
}

.markdown-content {
  color: var(--n-text-color-2);
  line-height: 1.6;
  --markdown-link-color: #1d4ed8;
  --markdown-link-hover: #1e40af;
  --markdown-code-bg: #f1f5f9;
  --markdown-code-border: #cbd5e1;
  --markdown-table-border: #94a3b8;
  --markdown-table-header: #eaf2ff;
  --markdown-table-row: rgba(37, 99, 235, 0.04);
}

.plugin-details--dark .markdown-content {
  --markdown-link-color: #93c5fd;
  --markdown-link-hover: #bfdbfe;
  --markdown-code-bg: #020617;
  --markdown-code-border: #334155;
  --markdown-table-border: #475569;
  --markdown-table-header: #1e293b;
  --markdown-table-row: rgba(96, 165, 250, 0.08);
}

.markdown-content :deep(h1),
.markdown-content :deep(h2),
.markdown-content :deep(h3),
.markdown-content :deep(h4),
.markdown-content :deep(h5),
.markdown-content :deep(h6) {
  margin: 1.5em 0 0.5em;
  color: var(--n-text-color);
}

.markdown-content :deep(h1:first-child),
.markdown-content :deep(h2:first-child),
.markdown-content :deep(h3:first-child) {
  margin-top: 0;
}

.markdown-content :deep(p) {
  margin: 1em 0;
}

.markdown-content :deep(a) {
  color: var(--markdown-link-color);
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.markdown-content :deep(a:hover) {
  color: var(--markdown-link-hover);
}

.markdown-content :deep(img) {
  max-width: 100%;
  border-radius: 8px;
}

.markdown-content :deep(.markdown-file-note) {
  margin: 0 0 12px;
  color: var(--n-text-color-3);
  font-size: 13px;
  font-weight: 600;
}

.markdown-content :deep(.markdown-directory-list) {
  display: grid;
  gap: 8px;
  padding-left: 0;
  list-style: none;
}

.markdown-content :deep(.markdown-directory-list li) {
  padding: 8px 10px;
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
  background: var(--n-color);
}

.markdown-content :deep(:not(pre) > code) {
  background: var(--markdown-code-bg);
  border: 1px solid var(--markdown-code-border);
  padding: 0.2em 0.4em;
  border-radius: 3px;
  font-size: 0.9em;
  font-family: monospace;
}

.markdown-content :deep(.markdown-code-block) {
  position: relative;
  margin: 1em 0;
  border: 1px solid var(--markdown-code-border);
  border-radius: 8px;
  background: var(--markdown-code-bg);
}

.markdown-content :deep(.markdown-copy-code) {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 1;
  border: 1px solid var(--markdown-code-border);
  border-radius: 5px;
  padding: 4px 8px;
  color: var(--n-text-color);
  background: var(--n-color);
  cursor: pointer;
  font-size: 12px;
  line-height: 1.2;
}

.markdown-content :deep(.markdown-copy-code:hover) {
  border-color: var(--markdown-link-color);
  color: var(--markdown-link-color);
}

.markdown-content :deep(pre) {
  margin: 0;
  padding: 42px 16px 16px;
  background: transparent;
  overflow-x: auto;
}

.markdown-content :deep(pre code) {
  background: none;
  padding: 0;
  border: 0;
  border-radius: 0;
  font-family: monospace;
  font-size: 0.9em;
}

.markdown-content :deep(pre code *),
.markdown-content :deep(pre code .hljs) {
  border: 0;
  background: transparent;
  padding: 0;
  border-radius: 0;
}

.markdown-content :deep(blockquote) {
  margin: 1em 0;
  padding-left: 1em;
  border-left: 4px solid var(--n-border-color);
  color: var(--n-text-color-3);
}

.markdown-content :deep(ul),
.markdown-content :deep(ol) {
  padding-left: 2em;
  margin: 1em 0;
}

.markdown-content :deep(table) {
  border-collapse: collapse;
  min-width: 100%;
  margin: 1em 0;
  display: block;
  overflow-x: auto;
  white-space: nowrap;
}

.markdown-content :deep(th),
.markdown-content :deep(td) {
  border: 1px solid var(--markdown-table-border);
  padding: 8px;
  text-align: left;
}

.markdown-content :deep(th) {
  background: var(--markdown-table-header);
}

.markdown-content :deep(tr:nth-child(even) td) {
  background: var(--markdown-table-row);
}

.plugin-details__footer {
  padding: var(--modal-padding);
  margin: 0 calc(-1 * var(--modal-padding));
  margin-top: calc(-1 * var(--modal-padding));
  border-top: 0px solid var(--n-border-color);
}

.readme-loading,
.readme-error {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 200px;
}

.refresh-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* 响应式设计 */
@media (max-width: 768px) {
  .plugin-details__content {
    max-height: calc(70vh - 140px);
  }
}

@media (max-width: 480px) {
  .plugin-details {
    --modal-padding: 16px;
  }

  .plugin-details__title {
    font-size: 1.2em;
  }
}
</style>
