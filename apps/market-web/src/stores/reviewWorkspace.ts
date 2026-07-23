import { shallowRef, type ShallowRef } from "vue";
import { defineStore } from "pinia";
import type {
  ArtifactDiffContentResponse,
  ArtifactDiffListResponse,
  ArtifactFileContentResponse,
  ArtifactFileListResponse,
  ReviewComment,
  ReviewCommentCreateInput,
  ReviewCommentListResponse,
  ReviewHistoryEvent,
  ReviewHistoryResponse,
  StableRiskResponse,
} from "@/types/artifacts";
import { usePluginStore } from "./plugins";

type ReadResource = "files" | "fileContent" | "diffs" | "diffContent" | "comments" | "history";

interface RequestToken {
  artifactId: string;
  generation: number;
  sequence: number;
  signal: AbortSignal;
}

interface CommentMutationPayload {
  expected_version: number;
  body?: string;
}

export class WorkspaceApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code = "") {
    super(message);
    this.name = "WorkspaceApiError";
    this.status = status;
    this.code = code;
  }
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function textValue(value: unknown): string {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    ? String(value)
    : "";
}

function requestKey(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  return `${prefix}:${uuid || `${Date.now()}:${Math.random().toString(16).slice(2)}`}`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function cachePut<T>(cache: Map<string, T>, key: string, value: T, limit: number): void {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > limit) {
    const oldest = cache.keys().next().value as string | undefined;
    if (!oldest) break;
    cache.delete(oldest);
  }
}

function cacheGet<T>(cache: Map<string, T>, key: string): T | undefined {
  const value = cache.get(key);
  if (value === undefined) return undefined;
  cache.delete(key);
  cache.set(key, value);
  return value;
}

export const useReviewWorkspaceStore = defineStore("review-workspace", () => {
  const activeArtifactId = shallowRef("");
  const files = shallowRef<ArtifactFileListResponse | null>(null);
  const fileContent = shallowRef<ArtifactFileContentResponse | null>(null);
  const diffs = shallowRef<ArtifactDiffListResponse | null>(null);
  const diffContent = shallowRef<ArtifactDiffContentResponse | null>(null);
  const comments = shallowRef<ReviewCommentListResponse | null>(null);
  const historyItems = shallowRef<ReviewHistoryEvent[]>([]);
  const historyHasMore = shallowRef(false);
  const historyCursor = shallowRef<string | null>(null);
  const historyLoaded = shallowRef(false);

  const loadingFiles = shallowRef(false);
  const loadingFileContent = shallowRef(false);
  const loadingDiffs = shallowRef(false);
  const loadingDiffContent = shallowRef(false);
  const loadingComments = shallowRef(false);
  const loadingHistory = shallowRef(false);
  const mutating = shallowRef(false);

  const filesError = shallowRef("");
  const fileContentError = shallowRef("");
  const diffsError = shallowRef("");
  const diffContentError = shallowRef("");
  const commentsError = shallowRef("");
  const historyError = shallowRef("");

  const loadingRefs: Record<ReadResource, ShallowRef<boolean>> = {
    files: loadingFiles,
    fileContent: loadingFileContent,
    diffs: loadingDiffs,
    diffContent: loadingDiffContent,
    comments: loadingComments,
    history: loadingHistory,
  };
  const errorRefs: Record<ReadResource, ShallowRef<string>> = {
    files: filesError,
    fileContent: fileContentError,
    diffs: diffsError,
    diffContent: diffContentError,
    comments: commentsError,
    history: historyError,
  };
  const sequences: Record<ReadResource, number> = {
    files: 0,
    fileContent: 0,
    diffs: 0,
    diffContent: 0,
    comments: 0,
    history: 0,
  };
  const controllers = new Map<ReadResource, AbortController>();
  const contentCache = new Map<string, ArtifactFileContentResponse>();
  const diffCache = new Map<string, ArtifactDiffContentResponse>();
  let generation = 0;
  let pendingMutations = 0;

  function apiBaseUrl(): string {
    return usePluginStore().apiBaseUrl;
  }

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      credentials: "include",
      cache: "no-store",
      ...init,
    });
    const payload = recordValue(await response.json().catch(() => ({})));
    if (!response.ok) {
      const detail = recordValue(payload.detail);
      const message =
        textValue(detail.message) ||
        textValue(payload.message) ||
        textValue(payload.error) ||
        textValue(payload.detail) ||
        `请求失败（HTTP ${response.status}）`;
      throw new WorkspaceApiError(
        message,
        response.status,
        textValue(detail.code) || textValue(payload.code),
      );
    }
    return payload as T;
  }

  function resetForArtifact(artifactId: string): void {
    generation += 1;
    for (const controller of controllers.values()) controller.abort();
    controllers.clear();
    for (const key of Object.keys(sequences) as ReadResource[]) sequences[key] += 1;
    activeArtifactId.value = artifactId;
    files.value = null;
    fileContent.value = null;
    diffs.value = null;
    diffContent.value = null;
    comments.value = null;
    historyItems.value = [];
    historyHasMore.value = false;
    historyCursor.value = null;
    historyLoaded.value = false;
    contentCache.clear();
    diffCache.clear();
    for (const key of Object.keys(loadingRefs) as ReadResource[]) {
      loadingRefs[key].value = false;
      errorRefs[key].value = "";
    }
  }

  function prepareArtifact(artifactId: string): void {
    if (activeArtifactId.value !== artifactId) resetForArtifact(artifactId);
  }

  function begin(resource: ReadResource, artifactId: string): RequestToken {
    prepareArtifact(artifactId);
    controllers.get(resource)?.abort();
    const controller = new AbortController();
    controllers.set(resource, controller);
    sequences[resource] += 1;
    loadingRefs[resource].value = true;
    errorRefs[resource].value = "";
    return {
      artifactId,
      generation,
      sequence: sequences[resource],
      signal: controller.signal,
    };
  }

  function isCurrent(resource: ReadResource, token: RequestToken): boolean {
    return (
      token.generation === generation &&
      token.sequence === sequences[resource] &&
      token.artifactId === activeArtifactId.value
    );
  }

  function useCached(resource: ReadResource): void {
    controllers.get(resource)?.abort();
    controllers.delete(resource);
    sequences[resource] += 1;
    loadingRefs[resource].value = false;
    errorRefs[resource].value = "";
  }

  function beginMutation(): void {
    pendingMutations += 1;
    mutating.value = true;
  }

  function endMutation(): void {
    pendingMutations = Math.max(0, pendingMutations - 1);
    mutating.value = pendingMutations > 0;
  }

  async function runRead<T>(
    resource: ReadResource,
    artifactId: string,
    path: string,
    commit: (payload: T) => void,
  ): Promise<T | null> {
    const token = begin(resource, artifactId);
    try {
      const payload = await request<T>(path, { signal: token.signal });
      if (!isCurrent(resource, token)) return null;
      commit(payload);
      return payload;
    } catch (error) {
      if (isAbortError(error) || !isCurrent(resource, token)) return null;
      errorRefs[resource].value = error instanceof Error ? error.message : "请求失败";
      throw error;
    } finally {
      if (isCurrent(resource, token)) loadingRefs[resource].value = false;
    }
  }

  async function loadFiles(
    artifactId: string,
    { limit = 200, offset = 0 }: { limit?: number; offset?: number } = {},
  ): Promise<ArtifactFileListResponse | null> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return runRead<ArtifactFileListResponse>(
      "files",
      artifactId,
      `/v1/artifacts/${encodeURIComponent(artifactId)}/files?${query}`,
      (payload) => {
        if (files.value && files.value.tree_sha256 !== payload.tree_sha256) contentCache.clear();
        files.value = payload;
      },
    );
  }

  async function loadFileContent(
    artifactId: string,
    fileId: string,
    { startLine = 1, lineLimit = 200 }: { startLine?: number; lineLimit?: number } = {},
  ): Promise<ArtifactFileContentResponse | null> {
    prepareArtifact(artifactId);
    const key = `${artifactId}:${fileId}:${startLine}:${lineLimit}`;
    const cached = cacheGet(contentCache, key);
    if (cached) {
      useCached("fileContent");
      fileContent.value = cached;
      return cached;
    }
    const query = new URLSearchParams({
      start_line: String(startLine),
      line_limit: String(lineLimit),
    });
    return runRead<ArtifactFileContentResponse>(
      "fileContent",
      artifactId,
      `/v1/artifacts/${encodeURIComponent(artifactId)}/files/${encodeURIComponent(fileId)}/content?${query}`,
      (payload) => {
        if (files.value && payload.tree_sha256 !== files.value.tree_sha256) {
          throw new WorkspaceApiError("文件树已变化，请刷新版本", 409, "artifact_tree_changed");
        }
        cachePut(contentCache, key, payload, 3);
        fileContent.value = payload;
      },
    );
  }

  async function loadDiffs(
    artifactId: string,
    { limit = 200, offset = 0 }: { limit?: number; offset?: number } = {},
  ): Promise<ArtifactDiffListResponse | null> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return runRead<ArtifactDiffListResponse>(
      "diffs",
      artifactId,
      `/v1/artifacts/${encodeURIComponent(artifactId)}/diff?${query}`,
      (payload) => {
        if (diffs.value && diffs.value.tree_sha256 !== payload.tree_sha256) diffCache.clear();
        diffs.value = payload;
      },
    );
  }

  async function loadDiffContent(
    artifactId: string,
    diffId: string,
    hunkId = "",
  ): Promise<ArtifactDiffContentResponse | null> {
    prepareArtifact(artifactId);
    const key = `${artifactId}:${diffId}:${hunkId}`;
    const cached = cacheGet(diffCache, key);
    if (cached) {
      useCached("diffContent");
      diffContent.value = cached;
      return cached;
    }
    const query = new URLSearchParams();
    if (hunkId) query.set("hunk_id", hunkId);
    const suffix = query.size ? `?${query}` : "";
    return runRead<ArtifactDiffContentResponse>(
      "diffContent",
      artifactId,
      `/v1/artifacts/${encodeURIComponent(artifactId)}/diff/${encodeURIComponent(diffId)}${suffix}`,
      (payload) => {
        if (diffs.value && payload.tree_sha256 !== diffs.value.tree_sha256) {
          throw new WorkspaceApiError(
            "Diff 文件树已变化，请刷新版本",
            409,
            "artifact_tree_changed",
          );
        }
        cachePut(diffCache, key, payload, 2);
        diffContent.value = payload;
      },
    );
  }

  async function loadComments(
    artifactId: string,
    { limit = 20, offset = 0 }: { limit?: number; offset?: number } = {},
  ): Promise<ReviewCommentListResponse | null> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return runRead<ReviewCommentListResponse>(
      "comments",
      artifactId,
      `/v1/artifacts/${encodeURIComponent(artifactId)}/comments?${query}`,
      (payload) => {
        comments.value = payload;
      },
    );
  }

  async function loadHistory(
    artifactId: string,
    { reset = false, limit = 30 }: { reset?: boolean; limit?: number } = {},
  ): Promise<ReviewHistoryResponse | null> {
    prepareArtifact(artifactId);
    if (reset) {
      historyItems.value = [];
      historyHasMore.value = false;
      historyCursor.value = null;
      historyLoaded.value = false;
    } else if (loadingHistory.value || (historyItems.value.length && !historyHasMore.value)) {
      return null;
    }
    const query = new URLSearchParams({ limit: String(limit) });
    if (!reset && historyCursor.value) query.set("cursor", historyCursor.value);
    return runRead<ReviewHistoryResponse>(
      "history",
      artifactId,
      `/v1/artifacts/${encodeURIComponent(artifactId)}/history?${query}`,
      (payload) => {
        const existing = reset ? [] : historyItems.value;
        const seen = new Set(existing.map((item) => `${item.type}:${item.id}`));
        historyItems.value = [
          ...existing,
          ...payload.items.filter((item) => !seen.has(`${item.type}:${item.id}`)),
        ];
        historyHasMore.value = payload.has_more;
        historyCursor.value = payload.next_cursor;
        historyLoaded.value = true;
      },
    );
  }

  function upsertComment(comment: ReviewComment, isNew = false): void {
    if (!comments.value) {
      comments.value = {
        artifact_id: comment.artifact_id,
        items: [comment],
        total: 1,
        limit: 20,
        offset: 0,
      };
      return;
    }
    const index = comments.value.items.findIndex((item) => item.id === comment.id);
    const items = [...comments.value.items];
    if (index >= 0) items.splice(index, 1, comment);
    else items.push(comment);
    comments.value = {
      ...comments.value,
      items,
      total: comments.value.total + (isNew && index < 0 ? 1 : 0),
    };
  }

  async function commentRequest(
    artifactId: string,
    path: string,
    body: Record<string, unknown>,
    isNew = false,
  ): Promise<ReviewComment> {
    prepareArtifact(artifactId);
    const requestGeneration = generation;
    beginMutation();
    try {
      const payload = await request<{ comment: ReviewComment }>(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (activeArtifactId.value === artifactId && generation === requestGeneration) {
        useCached("comments");
        upsertComment(payload.comment, isNew);
      }
      return payload.comment;
    } finally {
      endMutation();
    }
  }

  function createComment(
    artifactId: string,
    input: ReviewCommentCreateInput,
  ): Promise<ReviewComment> {
    return commentRequest(
      artifactId,
      `/v1/admin/artifacts/${encodeURIComponent(artifactId)}/comments`,
      { ...input, idempotency_key: requestKey("review-comment") },
      true,
    );
  }

  function mutateComment(
    artifactId: string,
    threadId: string,
    action: "replies" | "author-addressed" | "edit" | "resolve" | "reopen",
    payload: CommentMutationPayload,
  ): Promise<ReviewComment> {
    const adminAction = ["edit", "resolve", "reopen"].includes(action);
    const prefix = adminAction ? "/v1/admin" : "/v1";
    const body: Record<string, unknown> = {
      expected_version: payload.expected_version,
      idempotency_key: requestKey(`review-comment-${action}`),
    };
    if (payload.body !== undefined) body.body = payload.body;
    return commentRequest(
      artifactId,
      `${prefix}/artifacts/${encodeURIComponent(artifactId)}/comments/${encodeURIComponent(threadId)}/${action}`,
      body,
    );
  }

  async function requestStableRisk(
    artifactId: string,
    findingId: string,
    expectedVersion: number,
    reason: string,
    confirmAffectsCurrentRelease: boolean,
  ): Promise<StableRiskResponse> {
    prepareArtifact(artifactId);
    beginMutation();
    try {
      return await request<StableRiskResponse>(
        `/v1/admin/artifacts/${encodeURIComponent(artifactId)}/findings/${encodeURIComponent(findingId)}/stable-risk`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            expected_version: expectedVersion,
            reason,
            confirm_affects_current_release: confirmAffectsCurrentRelease,
            idempotency_key: requestKey("stable-risk"),
          }),
        },
      );
    } finally {
      endMutation();
    }
  }

  return {
    activeArtifactId,
    files,
    fileContent,
    diffs,
    diffContent,
    comments,
    historyItems,
    historyHasMore,
    historyCursor,
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
    resetForArtifact,
    loadFiles,
    loadFileContent,
    loadDiffs,
    loadDiffContent,
    loadComments,
    loadHistory,
    createComment,
    replyComment: (artifactId: string, threadId: string, version: number, body: string) =>
      mutateComment(artifactId, threadId, "replies", {
        expected_version: version,
        body,
      }),
    addressComment: (artifactId: string, threadId: string, version: number, body = "") =>
      mutateComment(artifactId, threadId, "author-addressed", {
        expected_version: version,
        body,
      }),
    editComment: (artifactId: string, threadId: string, version: number, body: string) =>
      mutateComment(artifactId, threadId, "edit", {
        expected_version: version,
        body,
      }),
    resolveComment: (artifactId: string, threadId: string, version: number) =>
      mutateComment(artifactId, threadId, "resolve", { expected_version: version }),
    reopenComment: (artifactId: string, threadId: string, version: number) =>
      mutateComment(artifactId, threadId, "reopen", { expected_version: version }),
    requestStableRisk,
  };
});
