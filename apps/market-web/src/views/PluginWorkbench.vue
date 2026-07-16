<script setup lang="ts">
import { computed, onMounted, shallowRef, watch } from "vue";
import { storeToRefs } from "pinia";
import { useRoute, useRouter } from "vue-router";
import { useMessage } from "naive-ui";
import ArtifactSubmissionPanel from "@/components/workbench/ArtifactSubmissionPanel.vue";
import PluginReviewHeader from "@/components/workbench/PluginReviewHeader.vue";
import PluginReviewSidebar from "@/components/workbench/PluginReviewSidebar.vue";
import PluginReviewWorkspace from "@/components/workbench/PluginReviewWorkspace.vue";
import ReviewDecisionPanel from "@/components/workbench/ReviewDecisionPanel.vue";
import ReviewSummaryPanel from "@/components/workbench/ReviewSummaryPanel.vue";
import { useArtifactStore } from "@/stores/artifacts";
import { usePluginStore } from "@/stores/plugins";
import type { Plugin } from "@/types";

const route = useRoute();
const router = useRouter();
const message = useMessage();
const pluginStore = usePluginStore();
const artifactStore = useArtifactStore();
const { currentUser } = storeToRefs(pluginStore);
const { items, detail, loadingList, loadingDetail, submitting, deciding } =
  storeToRefs(artifactStore);
const myPlugins = shallowRef<Plugin[]>([]);

const isAdmin = computed(() =>
  ["core_admin", "admin"].includes(String(currentUser.value?.role || "")),
);
const selectedId = computed(() =>
  typeof route.query.artifact === "string" ? route.query.artifact : "",
);
const statusFilter = computed({
  get: () => (typeof route.query.status === "string" ? route.query.status : ""),
  set: (value: string) => {
    void router.replace({
      query: { ...route.query, status: value || undefined, artifact: undefined },
    });
  },
});
const visibleItems = computed(() => {
  if (isAdmin.value || !statusFilter.value) return items.value;
  return items.value.filter((item) => item.review_status === statusFilter.value);
});

async function refreshList(): Promise<void> {
  try {
    if (isAdmin.value) {
      await artifactStore.loadQueue({ reviewStatus: statusFilter.value });
    } else {
      await artifactStore.loadMine();
    }
    if (!selectedId.value && visibleItems.value[0]) {
      await selectArtifact(visibleItems.value[0].id);
    }
  } catch (error) {
    message.error(errorMessage(error, "版本列表加载失败"));
  }
}

async function selectArtifact(artifactId: string): Promise<void> {
  await router.replace({ query: { ...route.query, artifact: artifactId } });
}

async function loadSelected(artifactId: string): Promise<void> {
  if (!artifactId) {
    artifactStore.clearDetail();
    return;
  }
  try {
    await artifactStore.loadDetail(artifactId);
  } catch (error) {
    message.error(errorMessage(error, "审查详情加载失败"));
  }
}

async function submitUpload(payload: { pluginId: string; file: File }): Promise<void> {
  try {
    const artifact = await artifactStore.submitUpload(payload.pluginId, payload.file);
    message.success("插件包已进入隔离队列");
    await refreshList();
    await selectArtifact(artifact.id);
  } catch (error) {
    message.error(errorMessage(error, "插件包上传失败"));
  }
}

async function submitGithub(payload: { pluginId: string; sourceRef: string }): Promise<void> {
  try {
    const artifact = await artifactStore.submitGithub(payload.pluginId, payload.sourceRef);
    message.success("GitHub commit 已固定并进入隔离队列");
    await refreshList();
    await selectArtifact(artifact.id);
  } catch (error) {
    message.error(errorMessage(error, "GitHub 版本提交失败"));
  }
}

async function approve(reason: string): Promise<void> {
  if (!selectedId.value) return;
  try {
    await artifactStore.approve(selectedId.value, reason);
    message.success("版本已批准，正在排队发布 CDN 包");
    await refreshList();
  } catch (error) {
    message.error(errorMessage(error, "批准失败"));
  }
}

async function reject(reason: string): Promise<void> {
  if (!selectedId.value) return;
  try {
    await artifactStore.reject(selectedId.value, reason);
    message.success("版本已拒绝，不会提供 CDN 下载链接");
    await refreshList();
  } catch (error) {
    message.error(errorMessage(error, "拒绝失败"));
  }
}

async function requestChanges(reason: string): Promise<void> {
  if (!selectedId.value) return;
  try {
    await artifactStore.requestChanges(selectedId.value, reason);
    message.success("已要求作者修改，该版本不会发布 CDN 包");
    await refreshList();
  } catch (error) {
    message.error(errorMessage(error, "要求修改失败"));
  }
}

async function retryPublish(): Promise<void> {
  if (!selectedId.value) return;
  try {
    await artifactStore.retryPublish(selectedId.value);
    message.success("已重新加入发布队列");
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
    await artifactStore.loadDetail(artifact.id);
  } catch (error) {
    message.error(errorMessage(error, "撤回失败"));
  }
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

watch(selectedId, (artifactId) => void loadSelected(artifactId));
watch(statusFilter, () => void refreshList());

onMounted(async () => {
  await pluginStore.loadCurrentUser();
  if (!currentUser.value) return;
  if (!isAdmin.value) {
    myPlugins.value = await pluginStore.loadMyPlugins().catch(() => []);
  }
  await refreshList();
  await loadSelected(selectedId.value);
});
</script>

<template>
  <PluginReviewWorkspace>
    <template #header>
      <PluginReviewHeader
        :is-admin="isAdmin"
        :item-count="visibleItems.length"
        :refreshing="loadingList"
        :artifact="detail?.artifact || null"
        @back="router.back()"
        @refresh="refreshList"
      />
    </template>

    <template #sidebar>
      <PluginReviewSidebar
        :artifacts="visibleItems"
        :selected-id="selectedId"
        :status-filter="statusFilter"
        :loading="loadingList"
        :is-admin="isAdmin"
        @select="selectArtifact"
        @status-change="statusFilter = $event"
      />
    </template>

    <template v-if="!isAdmin" #submission>
      <ArtifactSubmissionPanel
        :plugins="myPlugins"
        :submitting="submitting"
        @upload="submitUpload"
        @github="submitGithub"
      />
    </template>

    <ReviewSummaryPanel :detail="detail" :loading="loadingDetail" />
    <ReviewDecisionPanel
      :artifact="detail?.artifact || null"
      :is-admin="isAdmin"
      :busy="deciding"
      @approve="approve"
      @reject="reject"
      @request-changes="requestChanges"
      @retry-publish="retryPublish"
      @revoke="revoke"
    />
  </PluginReviewWorkspace>
</template>
