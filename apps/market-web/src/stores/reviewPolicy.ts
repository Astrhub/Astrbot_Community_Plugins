import { computed, shallowRef } from "vue";
import { defineStore } from "pinia";
import type {
  ReviewOperationsResponse,
  ReviewPolicyDiff,
  ReviewPolicyDocument,
  ReviewPolicyRecord,
} from "@/types/artifacts";
import { usePluginStore } from "./plugins";

export class ReviewPolicyApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
    this.name = "ReviewPolicyApiError";
  }
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export const useReviewPolicyStore = defineStore("reviewPolicy", () => {
  const policies = shallowRef<ReviewPolicyRecord[]>([]);
  const operations = shallowRef<ReviewOperationsResponse | null>(null);
  const lastDiff = shallowRef<ReviewPolicyDiff | null>(null);
  const loading = shallowRef(false);
  const mutating = shallowRef(false);
  const error = shallowRef("");
  const activePolicy = computed(
    () => policies.value.find((item) => item.status === "active") ?? null,
  );
  let generation = 0;

  function apiBaseUrl(): string {
    return usePluginStore().apiBaseUrl;
  }

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      credentials: "include",
      cache: "no-store",
      ...init,
    });
    const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) {
      const detail = payload.detail;
      const detailRecord =
        detail && typeof detail === "object" ? (detail as Record<string, unknown>) : null;
      throw new ReviewPolicyApiError(
        text(detailRecord?.message) ||
          text(payload.message) ||
          `请求失败（HTTP ${response.status}）`,
        response.status,
        text(detailRecord?.code) || text(payload.code) || "request_failed",
      );
    }
    return payload as T;
  }

  async function load(isCoreAdmin: boolean): Promise<void> {
    const requestId = ++generation;
    loading.value = true;
    error.value = "";
    try {
      if (isCoreAdmin) {
        const [policyResult, healthResult] = await Promise.allSettled([
          request<{ items: ReviewPolicyRecord[] }>("/v1/core-admin/review-policies?limit=100"),
          request<ReviewOperationsResponse>("/v1/core-admin/review-tools/health"),
        ]);
        if (policyResult.status === "rejected") throw policyResult.reason;
        if (requestId === generation) {
          policies.value = policyResult.value.items || [];
          operations.value = healthResult.status === "fulfilled" ? healthResult.value : null;
          if (healthResult.status === "rejected") error.value = errorMessage(healthResult.reason);
        }
      } else {
        const payload = await request<{ policy: ReviewPolicyRecord | null }>(
          "/v1/admin/review-policies/active",
        );
        if (requestId === generation) {
          policies.value = payload.policy ? [payload.policy] : [];
          operations.value = null;
        }
      }
    } catch (caught) {
      if (requestId === generation) error.value = errorMessage(caught);
      throw caught;
    } finally {
      if (requestId === generation) loading.value = false;
    }
  }

  async function createDraft(input: {
    version: string;
    policy: ReviewPolicyDocument;
    reason: string;
    basePolicyId?: string;
  }): Promise<ReviewPolicyRecord> {
    return mutate("/v1/core-admin/review-policies", {
      version: input.version,
      policy: input.policy,
      reason: input.reason,
      base_policy_id: input.basePolicyId || undefined,
      idempotency_key: idempotencyKey("create"),
    });
  }

  async function validatePolicy(policyId: string, reason: string): Promise<ReviewPolicyRecord> {
    return mutate(`/v1/core-admin/review-policies/${encodeURIComponent(policyId)}/validate`, {
      reason,
      idempotency_key: idempotencyKey("validate"),
    });
  }

  async function transitionPolicy(
    policyId: string,
    action: "activate" | "retire" | "rollback",
    reason: string,
  ): Promise<ReviewPolicyRecord> {
    return mutate(`/v1/core-admin/review-policies/${encodeURIComponent(policyId)}/${action}`, {
      reason,
      idempotency_key: idempotencyKey(action),
    });
  }

  async function mutate(path: string, body: Record<string, unknown>): Promise<ReviewPolicyRecord> {
    mutating.value = true;
    error.value = "";
    try {
      const payload = await request<{ policy: ReviewPolicyRecord; diff: ReviewPolicyDiff }>(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      lastDiff.value = payload.diff;
      upsert(payload.policy);
      return payload.policy;
    } catch (caught) {
      error.value = errorMessage(caught);
      throw caught;
    } finally {
      mutating.value = false;
    }
  }

  function upsert(policy: ReviewPolicyRecord): void {
    const next = policies.value
      .filter((item) => item.id !== policy.id)
      .map((item) => {
        if (policy.status === "active" && item.status === "active") {
          return { ...item, status: "retired" as const };
        }
        return item;
      });
    policies.value = [policy, ...next].sort((left, right) =>
      right.created_at.localeCompare(left.created_at),
    );
  }

  return {
    policies,
    operations,
    lastDiff,
    loading,
    mutating,
    error,
    activePolicy,
    load,
    createDraft,
    validatePolicy,
    transitionPolicy,
  };
});

function idempotencyKey(action: string): string {
  const id =
    globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `policy-${action}-${id}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "策略请求失败";
}
