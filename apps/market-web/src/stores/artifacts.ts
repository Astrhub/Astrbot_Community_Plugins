import { computed, shallowRef } from "vue";
import { defineStore } from "pinia";
import type {
  ArtifactDecision,
  ArtifactDetail,
  ArtifactRiskLevel,
  PluginArtifact,
} from "@/types/artifacts";
import { usePluginStore } from "./plugins";

type QueueFilters = {
  reviewStatus?: string;
  riskLevel?: ArtifactRiskLevel | "";
};

function errorText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

export const useArtifactStore = defineStore("artifacts", () => {
  const items = shallowRef<PluginArtifact[]>([]);
  const detail = shallowRef<ArtifactDetail | null>(null);
  const reviewContext = shallowRef<{
    artifact: PluginArtifact;
    decisions: ArtifactDecision[];
  } | null>(null);
  const contextError = shallowRef("");
  const loadingContext = shallowRef(false);
  let contextRequest = 0;
  let contextTargetId = "";
  const actionKeys = new Map<string, string>();
  const loadingList = shallowRef(false);
  const loadingDetail = shallowRef(false);
  const submitting = shallowRef(false);
  const deciding = shallowRef(false);
  const selectedArtifact = computed(
    () => reviewContext.value?.artifact ?? detail.value?.artifact ?? null,
  );
  let listRequest = 0;
  let detailRequest = 0;
  let detailTargetId = "";

  function apiBaseUrl(): string {
    return usePluginStore().apiBaseUrl;
  }

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      credentials: "include",
      cache: "no-store",
      ...init,
    });
    const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) {
      const detailValue = data.detail;
      const detailMessage =
        typeof detailValue === "object" && detailValue
          ? errorText((detailValue as Record<string, unknown>).message)
          : errorText(detailValue);
      const message = detailMessage || errorText(data.error) || errorText(data.message);
      throw new Error(message || `请求失败（HTTP ${response.status}）`);
    }
    return data as T;
  }

  async function loadMine(): Promise<PluginArtifact[]> {
    const requestId = ++listRequest;
    loadingList.value = true;
    try {
      const payload = await request<{ items: PluginArtifact[] }>("/v1/me/artifacts");
      const result = payload.items || [];
      if (requestId === listRequest) items.value = result;
      return result;
    } finally {
      if (requestId === listRequest) loadingList.value = false;
    }
  }

  async function loadQueue(filters: QueueFilters = {}): Promise<PluginArtifact[]> {
    const requestId = ++listRequest;
    loadingList.value = true;
    try {
      const query = new URLSearchParams();
      if (filters.reviewStatus) query.set("review_status", filters.reviewStatus);
      if (filters.riskLevel) query.set("risk_level", filters.riskLevel);
      const suffix = query.size ? `?${query.toString()}` : "";
      const payload = await request<{ items: PluginArtifact[] }>(`/v1/admin/artifacts${suffix}`);
      const result = payload.items || [];
      if (requestId === listRequest) items.value = result;
      return result;
    } finally {
      if (requestId === listRequest) loadingList.value = false;
    }
  }

  async function loadDetail(artifactId: string): Promise<ArtifactDetail> {
    const requestId = ++detailRequest;
    detailTargetId = artifactId;
    if (detail.value?.artifact.id !== artifactId) detail.value = null;
    loadingDetail.value = true;
    try {
      const payload = await request<ArtifactDetail>(
        "/v1/artifacts/" + encodeURIComponent(artifactId),
      );
      if (requestId === detailRequest && detailTargetId === artifactId) detail.value = payload;
      return payload;
    } finally {
      if (requestId === detailRequest) loadingDetail.value = false;
    }
  }

  async function loadContext(artifactId: string): Promise<void> {
    const requestId = ++contextRequest;
    contextTargetId = artifactId;
    if (reviewContext.value?.artifact.id !== artifactId) reviewContext.value = null;
    contextError.value = "";
    loadingContext.value = true;
    try {
      const payload = await request<{ artifact: PluginArtifact; decisions: ArtifactDecision[] }>(
        `/v1/artifacts/${encodeURIComponent(artifactId)}/review-context`,
      );
      if (requestId === contextRequest) reviewContext.value = payload;
    } catch (error) {
      if (requestId === contextRequest)
        contextError.value = error instanceof Error ? error.message : "版本信息加载失败";
      throw error;
    } finally {
      if (requestId === contextRequest) loadingContext.value = false;
    }
  }

  async function reviewAction(
    artifact: PluginArtifact,
    action: "comment" | "manual_approve" | "retry_review",
    reason: string,
  ): Promise<void> {
    const key = JSON.stringify([artifact.id, artifact.archive_sha256, action, reason]);
    if (!actionKeys.has(key)) actionKeys.set(key, crypto.randomUUID());
    deciding.value = true;
    try {
      if (action === "retry_review") {
        const preview = await request<{ running: boolean; archive_sha256: string }>(
          `/v1/admin/artifacts/${encodeURIComponent(artifact.id)}/retry-review-preview`,
        );
        if (preview.running) throw new Error("仍有任务运行，请等待结束后重跑；人工放行不受此限制");
        if (preview.archive_sha256 !== artifact.archive_sha256)
          throw new Error("提交包已变化，请刷新");
      }
      const payload = await request<{ artifact: PluginArtifact }>(
        `/v1/admin/artifacts/${encodeURIComponent(artifact.id)}/review-action`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action,
            reason,
            confirmed: action !== "comment",
            archive_sha256: artifact.archive_sha256,
            idempotency_key: actionKeys.get(key),
          }),
        },
      );
      if (contextTargetId === artifact.id) {
        reviewContext.value = {
          artifact: { ...artifact, ...payload.artifact },
          decisions: reviewContext.value?.decisions || [],
        };
        await Promise.allSettled([loadContext(artifact.id), loadDetail(artifact.id)]);
      }
    } finally {
      deciding.value = false;
    }
  }

  async function submitUpload(
    pluginId: string,
    file: File,
    supersedesArtifactId = "",
  ): Promise<PluginArtifact> {
    submitting.value = true;
    try {
      const body = new FormData();
      body.set("file", file);
      if (supersedesArtifactId) body.set("supersedes_artifact_id", supersedesArtifactId);
      const payload = await request<{ artifact: PluginArtifact }>(
        `/v1/plugins/${encodeURIComponent(pluginId)}/artifacts/upload`,
        { method: "POST", body },
      );
      return payload.artifact;
    } finally {
      submitting.value = false;
    }
  }

  async function submitGithub(
    pluginId: string,
    sourceRef = "",
    supersedesArtifactId = "",
  ): Promise<PluginArtifact> {
    submitting.value = true;
    try {
      const payload = await request<{ artifact: PluginArtifact }>(
        `/v1/plugins/${encodeURIComponent(pluginId)}/artifacts/github`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source_ref: sourceRef,
            ...(supersedesArtifactId ? { supersedes_artifact_id: supersedesArtifactId } : {}),
          }),
        },
      );
      return payload.artifact;
    } finally {
      submitting.value = false;
    }
  }

  async function approve(artifactId: string, reason: string): Promise<PluginArtifact> {
    return decide(artifactId, "approve", reason);
  }

  async function reject(artifactId: string, reason: string): Promise<PluginArtifact> {
    return decide(artifactId, "reject", reason);
  }

  async function requestChanges(artifactId: string, reason: string): Promise<PluginArtifact> {
    return decide(artifactId, "request-changes", reason);
  }

  async function decide(
    artifactId: string,
    action: "approve" | "reject" | "request-changes",
    reason: string,
  ): Promise<PluginArtifact> {
    deciding.value = true;
    try {
      const payload = await request<{ artifact: PluginArtifact }>(
        `/v1/admin/artifacts/${encodeURIComponent(artifactId)}/${action}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ reason }),
        },
      );
      if (!detailTargetId || detailTargetId === artifactId) await loadDetail(artifactId);
      return payload.artifact;
    } finally {
      deciding.value = false;
    }
  }

  async function retryPublish(artifactId: string): Promise<void> {
    deciding.value = true;
    try {
      await request(`/v1/admin/artifacts/${encodeURIComponent(artifactId)}/retry-publish`, {
        method: "POST",
      });
      if (!detailTargetId || detailTargetId === artifactId) await loadDetail(artifactId);
    } finally {
      deciding.value = false;
    }
  }

  async function revokeRelease(pluginId: string, reason: string): Promise<void> {
    deciding.value = true;
    try {
      await request(`/v1/admin/plugins/${encodeURIComponent(pluginId)}/revoke-release`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ reason }),
      });
    } finally {
      deciding.value = false;
    }
  }

  function clearDetail(): void {
    detailRequest += 1;
    contextRequest += 1;
    contextTargetId = "";
    reviewContext.value = null;
    contextError.value = "";
    loadingContext.value = false;
    detailTargetId = "";
    detail.value = null;
    loadingDetail.value = false;
  }

  return {
    items,
    detail,
    reviewContext,
    contextError,
    loadingContext,
    loadContext,
    reviewAction,
    loadingList,
    loadingDetail,
    submitting,
    deciding,
    selectedArtifact,
    loadMine,
    loadQueue,
    loadDetail,
    submitUpload,
    submitGithub,
    approve,
    reject,
    requestChanges,
    retryPublish,
    revokeRelease,
    clearDetail,
  };
});
