<script setup lang="ts">
import { computed, nextTick, onMounted, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { useRoute, useRouter } from "vue-router";
import { NTabPane, NTabs, useMessage } from "naive-ui";
import ArtifactSubmissionPanel from "@/components/workbench/ArtifactSubmissionPanel.vue";
import PluginReviewHeader from "@/components/workbench/PluginReviewHeader.vue";
import PluginReviewSidebar from "@/components/workbench/PluginReviewSidebar.vue";
import PluginReviewWorkspace from "@/components/workbench/PluginReviewWorkspace.vue";
import ReviewCommentThread from "@/components/workbench/ReviewCommentThread.vue";
import ReviewDecisionPanel from "@/components/workbench/ReviewDecisionPanel.vue";
import ReviewDiffViewer from "@/components/workbench/ReviewDiffViewer.vue";
import ReviewFileBrowser from "@/components/workbench/ReviewFileBrowser.vue";
import ReviewHistoryTimeline from "@/components/workbench/ReviewHistoryTimeline.vue";
import ReviewPolicyPanel from "@/components/workbench/ReviewPolicyPanel.vue";
import ReviewSummaryPanel from "@/components/workbench/ReviewSummaryPanel.vue";
import { useReviewSelection } from "@/composables/useReviewSelection";
import { useArtifactStore } from "@/stores/artifacts";
import { useReviewWorkspaceStore, WorkspaceApiError } from "@/stores/reviewWorkspace";
import { usePluginStore } from "@/stores/plugins";
import { useReviewPolicyStore } from "@/stores/reviewPolicy";
import type { Plugin } from "@/types";
import type {
  ArtifactFinding,
  ArtifactReviewStatus,
  ArtifactRiskLevel,
  ReviewAnchor,
  ReviewCommentCreateInput,
  ReviewCommentSide,
  ReviewPolicyDocument,
  ReviewWorkspaceView,
} from "@/types/artifacts";

const route = useRoute();
const router = useRouter();
const message = useMessage();
const pluginStore = usePluginStore();
const artifactStore = useArtifactStore();
const workspaceStore = useReviewWorkspaceStore();
const policyStore = useReviewPolicyStore();
const reviewRoute = useReviewSelection(route, router);

const { currentUser } = storeToRefs(pluginStore);
const { items, detail, loadingList, loadingDetail, submitting, deciding } =
  storeToRefs(artifactStore);
const {
  activeArtifactId,
  files,
  fileContent,
  diffs,
  diffContent,
  comments,
  historyItems,
  historyHasMore,
  historyLoaded,
  loadingFiles,
  loadingFileContent,
  loadingDiffs,
  loadingDiffContent,
  loadingComments,
  loadingHistory,
  mutating,
  filesError,
  fileContentError,
  diffsError,
  diffContentError,
  commentsError,
  historyError,
} = storeToRefs(workspaceStore);
const {
  policies,
  operations: policyOperations,
  lastDiff: policyDiff,
  loading: loadingPolicy,
  mutating: mutatingPolicy,
  error: policyError,
} = storeToRefs(policyStore);

const myPlugins = shallowRef<Plugin[]>([]);
const initialized = shallowRef(false);
const drawerOpen = shallowRef(false);
const submissionSection = shallowRef<HTMLElement | null>(null);

const selection = reviewRoute.selection;
const selectedId = computed(() => selection.value.artifactId);
const isAdmin = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);
const isCoreAdmin = computed(() => String(currentUser.value?.role || "") === "core_admin");
const currentNickname = computed(
  () =>
    String(
      currentUser.value?.github_login ||
        currentUser.value?.username ||
        currentUser.value?.internal_username ||
        "",
    ) || "当前用户",
);
const visibleItems = computed(() =>
  items.value.filter(
    (item) =>
      (!selection.value.status || item.review_status === selection.value.status) &&
      (!selection.value.risk || item.risk_level === selection.value.risk),
  ),
);
const commandBusy = computed(() => deciding.value || mutating.value);
const supersedesArtifact = computed(() => {
  const artifact = detail.value?.artifact;
  return !isAdmin.value && artifact?.review_status === "changes_requested" ? artifact : null;
});
const canCreateComments = computed(() => {
  if (!isAdmin.value || !detail.value?.artifact) return false;
  return !["changes_requested", "approved", "rejected", "withdrawn"].includes(
    detail.value.artifact.review_status,
  );
});
const selectedAnchor = computed<ReviewAnchor | null>(() => {
  const state = selection.value;
  if (!state.fileId || !state.lineStart) return null;
  const selectedDiff = diffs.value?.items.find((item) => item.id === state.diffId);
  const selectedFile = files.value?.items.find((item) => item.id === state.fileId);
  const filePath = selectedDiff
    ? state.side === "base"
      ? selectedDiff.base_path || selectedDiff.path
      : selectedDiff.path
    : selectedFile?.path || "受限文件";
  return {
    fileId: state.fileId,
    filePath,
    side: state.side,
    lineStart: state.lineStart,
    lineEnd: state.lineEnd || state.lineStart,
    ...(state.diffId && state.hunkId ? { diffId: state.diffId, hunkId: state.hunkId } : {}),
  };
});

async function refreshList(): Promise<void> {
  if (!currentUser.value) return;
  try {
    if (isAdmin.value) {
      await artifactStore.loadQueue({
        reviewStatus: selection.value.status,
        riskLevel: selection.value.risk,
      });
    } else {
      await artifactStore.loadMine();
    }
    if (selection.value.view !== "policy" && !selectedId.value && visibleItems.value[0]) {
      await reviewRoute.setArtifact(visibleItems.value[0].id);
    }
  } catch (error) {
    message.error(errorMessage(error, "版本列表加载失败"));
  }
}

async function refreshAll(): Promise<void> {
  if (selection.value.view === "policy" && isAdmin.value) {
    await loadPolicyWorkspace();
    return;
  }
  await refreshList();
  if (selectedId.value) await loadSelected(selectedId.value);
}

async function selectArtifact(artifactId: string): Promise<void> {
  drawerOpen.value = false;
  await reviewRoute.setArtifact(artifactId);
}

async function loadSelected(artifactId: string): Promise<void> {
  if (!artifactId) {
    artifactStore.clearDetail();
    workspaceStore.resetForArtifact("");
    return;
  }
  workspaceStore.resetForArtifact(artifactId);
  const results = await Promise.allSettled([
    artifactStore.loadDetail(artifactId),
    workspaceStore.loadFiles(artifactId),
    workspaceStore.loadDiffs(artifactId),
    workspaceStore.loadComments(artifactId),
  ]);
  if (selectedId.value !== artifactId) return;
  if (results[0]?.status === "rejected") {
    message.error(errorMessage(results[0].reason, "审查详情加载失败"));
    return;
  }
  await ensureViewData();
}

async function ensureViewData(): Promise<void> {
  const state = selection.value;
  if (state.view === "policy") {
    if (isAdmin.value) await loadPolicyWorkspace();
    return;
  }
  const artifactId = state.artifactId;
  if (!artifactId || activeArtifactId.value !== artifactId) return;
  try {
    if (state.view === "files") {
      const page = files.value || (await workspaceStore.loadFiles(artifactId));
      const selected = page?.items.find((item) => item.id === state.fileId);
      if (!state.fileId && page?.items[0]) {
        await reviewRoute.selectFile(page.items[0].id);
        return;
      }
      if (state.fileId && !selected) return;
      if (selected?.content_available) {
        const startLine = state.lineStart ? Math.floor((state.lineStart - 1) / 200) * 200 + 1 : 1;
        await workspaceStore.loadFileContent(artifactId, selected.id, { startLine });
      }
    }
    if (state.view === "diff") {
      const page = diffs.value || (await workspaceStore.loadDiffs(artifactId));
      if (!state.diffId && page?.items[0]) {
        await selectDiff(page.items[0].id);
        return;
      }
      if (state.diffId && page?.items.some((item) => item.id === state.diffId)) {
        await workspaceStore.loadDiffContent(artifactId, state.diffId, state.hunkId);
      }
    }
    if (state.view === "comments" && !comments.value) {
      await workspaceStore.loadComments(artifactId);
    }
    if (state.view === "history" && !historyLoaded.value) {
      await workspaceStore.loadHistory(artifactId, { reset: true });
    }
  } catch {
    // Component error panels expose bounded read failures without duplicate toasts.
  }
}

async function loadPolicyWorkspace(): Promise<void> {
  try {
    await policyStore.load(isCoreAdmin.value);
  } catch (error) {
    message.error(errorMessage(error, "策略工作台加载失败"));
  }
}

function changeStatus(status: string): void {
  void reviewRoute.setStatus(status as ArtifactReviewStatus | "");
}

function changeRisk(risk: ArtifactRiskLevel | ""): void {
  void reviewRoute.setRisk(risk);
}

function changeView(view: string | number): void {
  void reviewRoute.setView(String(view) as ReviewWorkspaceView);
}

function selectFile(fileId: string): void {
  void reviewRoute.selectFile(fileId);
}

function selectFileLine(payload: { fileId: string; lineStart: number; lineEnd: number }): void {
  void reviewRoute.selectLine({ ...payload, side: "current" });
}

function selectDiff(diffId: string): Promise<void> {
  const diff = diffs.value?.items.find((item) => item.id === diffId);
  const side: ReviewCommentSide = diff?.current_file_id ? "current" : "base";
  return reviewRoute.replace({
    view: "diff",
    diff: diffId,
    side,
    file: side === "current" ? diff?.current_file_id || "" : diff?.base_file_id || "",
    hunk: "",
    line: undefined,
    line_end: undefined,
  });
}

function changeDiffSide(side: ReviewCommentSide): void {
  const diff = diffs.value?.items.find((item) => item.id === selection.value.diffId);
  const fileId = side === "base" ? diff?.base_file_id : diff?.current_file_id;
  void reviewRoute.replace({
    side,
    file: fileId || "",
    hunk: "",
    line: undefined,
    line_end: undefined,
  });
}

function selectDiffLine(payload: {
  fileId: string;
  side: ReviewCommentSide;
  lineStart: number;
  lineEnd: number;
  diffId: string;
  hunkId: string;
}): void {
  void reviewRoute.selectLine(payload);
}

async function openFinding(finding: ArtifactFinding): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId || !finding.file_path) return;
  const page = files.value || (await workspaceStore.loadFiles(artifactId).catch(() => null));
  const file = page?.items.find((item) => item.path === finding.file_path);
  if (!file) {
    message.warning("该 finding 的文件不在当前受限文件页中");
    return;
  }
  await reviewRoute.selectFile(
    file.id,
    finding.line_start || undefined,
    finding.line_end || finding.line_start || undefined,
  );
}

function loadFilePage(offset: number): void {
  if (selectedId.value) void workspaceStore.loadFiles(selectedId.value, { offset });
}

function loadContentPage(payload: { fileId: string; startLine: number }): void {
  if (selectedId.value) {
    void workspaceStore.loadFileContent(selectedId.value, payload.fileId, {
      startLine: payload.startLine,
    });
  }
}

function loadDiffPage(offset: number): void {
  if (selectedId.value) void workspaceStore.loadDiffs(selectedId.value, { offset });
}

function loadCommentPage(offset: number): void {
  if (selectedId.value) void workspaceStore.loadComments(selectedId.value, { offset });
}

async function runCommentMutation(
  action: () => Promise<unknown>,
  successMessage: string,
): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId) return;
  try {
    await action();
    message.success(successMessage);
    if (selectedId.value !== artifactId) return;
    if (historyLoaded.value) {
      await workspaceStore.loadHistory(artifactId, { reset: true }).catch(() => null);
    }
  } catch (error) {
    if (error instanceof WorkspaceApiError && error.status === 409) {
      message.warning(error.message + "，已重新加载线程");
      if (selectedId.value === artifactId) {
        await workspaceStore.loadComments(artifactId).catch(() => null);
      }
      return;
    }
    message.error(errorMessage(error, "评论操作失败"));
  }
}

function createComment(input: ReviewCommentCreateInput): void {
  if (!selectedId.value) return;
  void runCommentMutation(
    () => workspaceStore.createComment(selectedId.value, input),
    "行级审查评论已发布",
  );
}

function replyComment(payload: { threadId: string; version: number; body: string }): void {
  void runCommentMutation(
    () =>
      workspaceStore.replyComment(
        selectedId.value,
        payload.threadId,
        payload.version,
        payload.body,
      ),
    "回复已保存",
  );
}

function addressComment(payload: { threadId: string; version: number; body: string }): void {
  void runCommentMutation(
    () =>
      workspaceStore.addressComment(
        selectedId.value,
        payload.threadId,
        payload.version,
        payload.body,
      ),
    "已标记为作者已处理",
  );
}

function editComment(payload: { threadId: string; version: number; body: string }): void {
  void runCommentMutation(
    () =>
      workspaceStore.editComment(selectedId.value, payload.threadId, payload.version, payload.body),
    "评论已更新",
  );
}

function resolveComment(payload: { threadId: string; version: number }): void {
  void runCommentMutation(
    () => workspaceStore.resolveComment(selectedId.value, payload.threadId, payload.version),
    "线程已解决",
  );
}

function reopenComment(payload: { threadId: string; version: number }): void {
  void runCommentMutation(
    () => workspaceStore.reopenComment(selectedId.value, payload.threadId, payload.version),
    "线程已重新打开",
  );
}

async function submitUpload(payload: {
  pluginId: string;
  file: File;
  supersedesArtifactId: string;
}): Promise<void> {
  try {
    const artifact = await artifactStore.submitUpload(
      payload.pluginId,
      payload.file,
      payload.supersedesArtifactId,
    );
    message.success(
      payload.supersedesArtifactId ? "修订版已创建并进入隔离队列" : "插件包已进入隔离队列",
    );
    await refreshList();
    await reviewRoute.setArtifact(artifact.id);
  } catch (error) {
    message.error(errorMessage(error, "插件包上传失败"));
  }
}

async function submitGithub(payload: {
  pluginId: string;
  sourceRef: string;
  supersedesArtifactId: string;
}): Promise<void> {
  try {
    const artifact = await artifactStore.submitGithub(
      payload.pluginId,
      payload.sourceRef,
      payload.supersedesArtifactId,
    );
    message.success(
      payload.supersedesArtifactId
        ? "修订版 commit 已固定并进入隔离队列"
        : "GitHub commit 已固定并进入隔离队列",
    );
    await refreshList();
    await reviewRoute.setArtifact(artifact.id);
  } catch (error) {
    message.error(errorMessage(error, "GitHub 版本提交失败"));
  }
}

async function refreshAfterDecision(artifactId: string): Promise<void> {
  await refreshList();
  if (!artifactId || selectedId.value !== artifactId) return;
  await Promise.allSettled([
    artifactStore.loadDetail(artifactId),
    workspaceStore.loadComments(artifactId),
    workspaceStore.loadHistory(artifactId, { reset: true }),
  ]);
}

async function approve(reason: string): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId) return;
  try {
    await artifactStore.approve(artifactId, reason);
    message.success("版本已批准，正在排队发布 CDN 包");
    await refreshAfterDecision(artifactId);
  } catch (error) {
    message.error(errorMessage(error, "批准失败"));
  }
}

async function reject(reason: string): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId) return;
  try {
    await artifactStore.reject(artifactId, reason);
    message.success("版本已拒绝，不会提供 CDN 下载链接");
    await refreshAfterDecision(artifactId);
  } catch (error) {
    message.error(errorMessage(error, "拒绝失败"));
  }
}

async function requestChanges(reason: string): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId) return;
  try {
    await artifactStore.requestChanges(artifactId, reason);
    message.success("已要求作者修改，该版本不会发布 CDN 包");
    await refreshAfterDecision(artifactId);
  } catch (error) {
    message.error(errorMessage(error, "要求修改失败"));
  }
}

async function retryPublish(): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId) return;
  try {
    await artifactStore.retryPublish(artifactId);
    message.success("已重新加入发布队列");
    await refreshAfterDecision(artifactId);
  } catch (error) {
    message.error(errorMessage(error, "重试发布失败"));
  }
}

async function revoke(reason: string): Promise<void> {
  const artifact = detail.value?.artifact;
  if (!artifact) return;
  try {
    await artifactStore.revokeRelease(artifact.plugin_id, reason);
    message.success("当前 CDN 版本正在撤回");
    await refreshAfterDecision(artifact.id);
  } catch (error) {
    message.error(errorMessage(error, "撤回失败"));
  }
}

async function stableRisk(payload: {
  findingId: string;
  expectedVersion: number;
  reason: string;
  confirmAffectsCurrentRelease: boolean;
}): Promise<void> {
  const artifactId = selectedId.value;
  if (!artifactId) return;
  try {
    await workspaceStore.requestStableRisk(
      artifactId,
      payload.findingId,
      payload.expectedVersion,
      payload.reason,
      payload.confirmAffectsCurrentRelease,
    );
    message.success("稳定版本已先移出插件源，CDN 对象正在撤回");
    await refreshAfterDecision(artifactId);
  } catch (error) {
    message.error(errorMessage(error, "稳定版本风险处置失败"));
  }
}

async function createPolicy(input: {
  version: string;
  policy: ReviewPolicyDocument;
  reason: string;
  basePolicyId?: string;
}): Promise<void> {
  try {
    await policyStore.createDraft(input);
    message.success("策略草稿已创建");
    await policyStore.load(true);
  } catch (error) {
    message.error(errorMessage(error, "策略草稿创建失败"));
  }
}

async function validatePolicy(input: { policyId: string; reason: string }): Promise<void> {
  try {
    const policy = await policyStore.validatePolicy(input.policyId, input.reason);
    if (policy.validation_summary.valid) message.success("策略校验通过");
    else message.warning("策略校验未通过");
    await policyStore.load(true);
  } catch (error) {
    message.error(errorMessage(error, "策略校验失败"));
  }
}

async function transitionPolicy(
  action: "activate" | "retire" | "rollback",
  input: { policyId: string; reason: string },
): Promise<void> {
  try {
    await policyStore.transitionPolicy(input.policyId, action, input.reason);
    message.success(
      { activate: "策略已激活", retire: "策略已退役", rollback: "策略已回滚" }[action],
    );
    await policyStore.load(true);
  } catch (error) {
    message.error(errorMessage(error, "策略状态变更失败"));
  }
}

async function focusResubmission(): Promise<void> {
  await reviewRoute.setView("summary");
  await nextTick();
  submissionSection.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

watch(selectedId, (artifactId) => {
  if (initialized.value) void loadSelected(artifactId);
});
watch(
  () => [selection.value.status, selection.value.risk, isAdmin.value] as const,
  () => {
    if (initialized.value) void refreshList();
  },
);
watch(
  () =>
    [
      selection.value.view,
      selection.value.fileId,
      selection.value.diffId,
      selection.value.hunkId,
      selection.value.side,
      selection.value.lineStart,
    ] as const,
  () => {
    if (
      initialized.value &&
      (selection.value.view === "policy" || activeArtifactId.value === selectedId.value)
    ) {
      void ensureViewData();
    }
  },
);

onMounted(async () => {
  await pluginStore.loadCurrentUser();
  if (!currentUser.value) return;
  if (selection.value.view === "policy" && !isAdmin.value) {
    await reviewRoute.setView("summary");
  }
  if (!isAdmin.value) {
    myPlugins.value = await pluginStore.loadMyPlugins().catch(() => []);
  }
  initialized.value = true;
  await refreshList();
  if (selection.value.view === "policy") {
    await loadPolicyWorkspace();
  } else if (selectedId.value) {
    await loadSelected(selectedId.value);
  }
});
</script>

<template>
  <PluginReviewWorkspace
    v-model:drawer-open="drawerOpen"
    :active-view="selection.view"
    :policy-mode="selection.view === 'policy'"
  >
    <template #header>
      <PluginReviewHeader
        :is-admin="isAdmin"
        :item-count="selection.view === 'policy' ? policies.length : visibleItems.length"
        :refreshing="loadingList || loadingDetail || loadingPolicy"
        :artifact="detail?.artifact || null"
        :policy-mode="selection.view === 'policy'"
        @back="router.back()"
        @refresh="refreshAll"
      />
    </template>

    <template #sidebar>
      <PluginReviewSidebar
        :artifacts="visibleItems"
        :selected-id="selectedId"
        :status-filter="selection.status"
        :risk-filter="selection.risk"
        :loading="loadingList"
        :is-admin="isAdmin"
        @select="selectArtifact"
        @status-change="changeStatus"
        @risk-change="changeRisk"
      />
    </template>

    <template v-if="!isAdmin && selection.view === 'summary'" #submission>
      <div ref="submissionSection">
        <ArtifactSubmissionPanel
          :plugins="myPlugins"
          :submitting="submitting"
          :supersedes-artifact="supersedesArtifact"
          @upload="submitUpload"
          @github="submitGithub"
        />
      </div>
    </template>

    <NTabs
      class="review-tabs"
      type="line"
      :value="selection.view"
      :animated="false"
      @update:value="changeView"
    >
      <NTabPane name="summary" tab="摘要">
        <ReviewSummaryPanel :detail="detail" :loading="loadingDetail" @open-finding="openFinding" />
      </NTabPane>
      <NTabPane name="files" tab="文件">
        <ReviewFileBrowser
          :files="files"
          :content="fileContent"
          :selected-file-id="selection.fileId"
          :selected-line-start="selection.lineStart"
          :selected-line-end="selection.lineEnd"
          :loading-files="loadingFiles"
          :loading-content="loadingFileContent"
          :files-error="filesError"
          :content-error="fileContentError"
          @select-file="selectFile"
          @select-line="selectFileLine"
          @files-page="loadFilePage"
          @content-page="loadContentPage"
        />
      </NTabPane>
      <NTabPane name="diff" tab="Diff">
        <ReviewDiffViewer
          :diffs="diffs"
          :content="diffContent"
          :selected-diff-id="selection.diffId"
          :selected-side="selection.side"
          :selected-hunk-id="selection.hunkId"
          :selected-line-start="selection.lineStart"
          :selected-line-end="selection.lineEnd"
          :loading-diffs="loadingDiffs"
          :loading-content="loadingDiffContent"
          :diffs-error="diffsError"
          :content-error="diffContentError"
          @select-diff="selectDiff"
          @side-change="changeDiffSide"
          @select-line="selectDiffLine"
          @diffs-page="loadDiffPage"
        />
      </NTabPane>
      <NTabPane name="comments" tab="评论" />
      <NTabPane name="history" tab="历史">
        <ReviewHistoryTimeline
          :items="historyItems"
          :loading="loadingHistory"
          :has-more="historyHasMore"
          :error="historyError"
          @load-more="workspaceStore.loadHistory(selectedId)"
        />
      </NTabPane>
      <NTabPane v-if="isAdmin" name="policy" tab="策略">
        <ReviewPolicyPanel
          :policies="policies"
          :operations="policyOperations"
          :last-diff="policyDiff"
          :loading="loadingPolicy"
          :busy="mutatingPolicy"
          :is-core-admin="isCoreAdmin"
          :error="policyError"
          @refresh="loadPolicyWorkspace"
          @create="createPolicy"
          @validate="validatePolicy"
          @activate="transitionPolicy('activate', $event)"
          @retire="transitionPolicy('retire', $event)"
          @rollback="transitionPolicy('rollback', $event)"
        />
      </NTabPane>
    </NTabs>

    <template v-if="selection.view !== 'policy'" #thread>
      <ReviewCommentThread
        :comments="comments"
        :anchor="selectedAnchor"
        :is-admin="isAdmin"
        :current-nickname="currentNickname"
        :can-create="canCreateComments"
        :loading="loadingComments"
        :busy="mutating"
        :error="commentsError"
        @create="createComment"
        @reply="replyComment"
        @address="addressComment"
        @edit="editComment"
        @resolve="resolveComment"
        @reopen="reopenComment"
        @page="loadCommentPage"
      />
    </template>

    <template v-if="selection.view !== 'policy'" #decision>
      <ReviewDecisionPanel
        :artifact="detail?.artifact || null"
        :findings="detail?.findings || []"
        :is-admin="isAdmin"
        :busy="commandBusy"
        @approve="approve"
        @reject="reject"
        @request-changes="requestChanges"
        @retry-publish="retryPublish"
        @revoke="revoke"
        @stable-risk="stableRisk"
        @resubmit="focusResubmission"
      />
    </template>
  </PluginReviewWorkspace>
</template>

<style scoped>
.review-tabs {
  min-width: 0;
}

.review-tabs :deep(.n-tabs-nav) {
  padding: 0 4px;
  background: color-mix(in srgb, var(--card-color) 96%, transparent);
}

.review-tabs :deep(.n-tab-pane) {
  padding-top: 14px;
}

@media (max-width: 860px) {
  .review-tabs :deep(.n-tabs-nav) {
    overflow-x: auto;
  }
}
</style>
