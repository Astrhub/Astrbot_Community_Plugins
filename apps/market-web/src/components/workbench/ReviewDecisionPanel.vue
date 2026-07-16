<script setup lang="ts">
import { computed, shallowRef } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NIcon,
  NInput,
  NModal,
  NSelect,
  NSpace,
  NTooltip,
} from "naive-ui";
import {
  CheckmarkCircleOutline,
  CloseCircleOutline,
  CreateOutline,
  RefreshOutline,
  ShieldCheckmarkOutline,
  TrashOutline,
} from "@vicons/ionicons5";
import type { ArtifactFinding, PluginArtifact } from "@/types/artifacts";

type DecisionAction = "approve" | "reject" | "requestChanges" | "revoke" | "stableRisk";

const props = defineProps<{
  artifact: PluginArtifact | null;
  findings: ArtifactFinding[];
  isAdmin: boolean;
  busy: boolean;
}>();

const emit = defineEmits<{
  approve: [reason: string];
  reject: [reason: string];
  requestChanges: [reason: string];
  retryPublish: [];
  revoke: [reason: string];
  stableRisk: [
    payload: {
      findingId: string;
      expectedVersion: number;
      reason: string;
      confirmAffectsCurrentRelease: boolean;
    },
  ];
  resubmit: [];
}>();

const modalAction = shallowRef<DecisionAction | null>(null);
const reason = shallowRef("");
const selectedFindingId = shallowRef("");
const confirmStableRisk = shallowRef(false);

const pending = computed(() => props.artifact?.review_status === "pending_review");
const canRetryPublish = computed(
  () =>
    props.artifact?.review_status === "approved" &&
    props.artifact?.publication_status === "publish_failed",
);
const canRevoke = computed(() =>
  ["published", "revoke_failed"].includes(props.artifact?.publication_status ?? ""),
);
const isRetryRevoke = computed(() => props.artifact?.publication_status === "revoke_failed");
const criticalFindings = computed(() =>
  props.findings.filter(
    (finding) =>
      finding.severity === "critical" &&
      ["open", "accepted"].includes(finding.status) &&
      finding.version >= 1,
  ),
);
const criticalOptions = computed(() =>
  criticalFindings.value.map((finding) => ({
    label:
      (finding.rule_id || finding.category || "critical") +
      " · " +
      (finding.file_path || "包级发现") +
      (finding.line_start ? ":" + finding.line_start : ""),
    value: finding.id,
  })),
);
const canStableRisk = computed(
  () =>
    criticalFindings.value.length > 0 &&
    Boolean(props.artifact?.published_version) &&
    !["revoking", "revoked"].includes(props.artifact?.publication_status || ""),
);
const modalTitle = computed(() => {
  const titles: Record<DecisionAction, string> = {
    approve: "批准候选版本",
    reject: "拒绝候选版本",
    requestChanges: "要求作者修改",
    revoke: isRetryRevoke.value ? "重试下架当前 CDN 版本" : "下架当前 CDN 版本",
    stableRisk: "确认严重风险影响稳定版本",
  };
  return modalAction.value ? titles[modalAction.value] : "";
});
const reasonRequired = computed(() => modalAction.value !== "approve");
const canConfirm = computed(() => {
  if (!modalAction.value || props.busy) return false;
  if (reasonRequired.value && !reason.value.trim()) return false;
  if (modalAction.value === "stableRisk") {
    return Boolean(selectedFindingId.value && confirmStableRisk.value);
  }
  return true;
});

function openModal(action: DecisionAction): void {
  modalAction.value = action;
  reason.value = "";
  selectedFindingId.value = action === "stableRisk" ? criticalFindings.value[0]?.id || "" : "";
  confirmStableRisk.value = false;
}

function closeModal(): void {
  if (props.busy) return;
  modalAction.value = null;
}

function confirmDecision(): void {
  if (!canConfirm.value || !modalAction.value) return;
  const normalizedReason = reason.value.trim();
  if (modalAction.value === "approve") emit("approve", normalizedReason);
  if (modalAction.value === "reject") emit("reject", normalizedReason);
  if (modalAction.value === "requestChanges") emit("requestChanges", normalizedReason);
  if (modalAction.value === "revoke") emit("revoke", normalizedReason);
  if (modalAction.value === "stableRisk") {
    const finding = criticalFindings.value.find((item) => item.id === selectedFindingId.value);
    if (!finding) return;
    emit("stableRisk", {
      findingId: finding.id,
      expectedVersion: finding.version,
      reason: normalizedReason,
      confirmAffectsCurrentRelease: confirmStableRisk.value,
    });
  }
  modalAction.value = null;
}
</script>

<template>
  <NCard v-if="artifact" title="审查决策" size="small" :aria-busy="busy" aria-live="polite">
    <NAlert v-if="!isAdmin" type="info" :bordered="false">
      自动审查只提供建议，最终发布由管理员人工复核。未通过版本不会获得插件源 CDN 链接；用户仍可选择
      GitHub 直连。
    </NAlert>
    <NButton
      v-if="!isAdmin && artifact.review_status === 'changes_requested'"
      class="author-resubmit"
      type="primary"
      @click="$emit('resubmit')"
    >
      <template #icon
        ><NIcon><CreateOutline /></NIcon
      ></template>
      重新提交修订版
    </NButton>

    <NSpace v-if="isAdmin" class="decision-actions" vertical>
      <NButton
        v-if="canStableRisk"
        type="error"
        secondary
        :disabled="busy"
        aria-label="确认 critical finding 影响稳定版本并紧急下架"
        @click="openModal('stableRisk')"
      >
        <template #icon
          ><NIcon><ShieldCheckmarkOutline /></NIcon
        ></template>
        严重风险关联稳定版
      </NButton>
      <NTooltip v-else-if="criticalFindings.length">
        <template #trigger>
          <NButton secondary disabled>
            <template #icon
              ><NIcon><ShieldCheckmarkOutline /></NIcon
            ></template>
            严重风险关联稳定版
          </NButton>
        </template>
        当前没有可关联的稳定 CDN 版本，候选风险只影响候选版本。
      </NTooltip>
      <NButton
        v-if="canRevoke"
        type="error"
        secondary
        :disabled="busy"
        :aria-label="isRetryRevoke ? '重试下架当前 CDN 版本' : '手动下架当前 CDN 版本'"
        @click="openModal('revoke')"
      >
        <template #icon
          ><NIcon><TrashOutline /></NIcon
        ></template>
        {{ isRetryRevoke ? "重试下架" : "手动下架当前 CDN 版本" }}
      </NButton>
      <NButton
        v-if="canRetryPublish"
        :loading="busy"
        aria-label="重试发布 CDN 包"
        @click="$emit('retryPublish')"
      >
        <template #icon
          ><NIcon><RefreshOutline /></NIcon
        ></template>
        重试发布
      </NButton>
      <NButton
        v-if="pending"
        type="warning"
        secondary
        :disabled="busy"
        aria-label="要求作者修改候选版本"
        @click="openModal('requestChanges')"
      >
        <template #icon
          ><NIcon><CreateOutline /></NIcon
        ></template>
        要求修改
      </NButton>
      <NButton
        v-if="pending"
        type="error"
        secondary
        :disabled="busy"
        aria-label="拒绝候选版本"
        @click="openModal('reject')"
      >
        <template #icon
          ><NIcon><CloseCircleOutline /></NIcon
        ></template>
        拒绝
      </NButton>
      <NButton
        v-if="pending"
        type="primary"
        :disabled="busy"
        aria-label="批准并发布候选版本"
        @click="openModal('approve')"
      >
        <template #icon
          ><NIcon><CheckmarkCircleOutline /></NIcon
        ></template>
        批准并发布
      </NButton>
    </NSpace>

    <NModal
      :show="Boolean(modalAction)"
      :mask-closable="!busy"
      @update:show="!$event && closeModal()"
    >
      <NCard
        class="decision-modal"
        :title="modalTitle"
        size="small"
        role="dialog"
        aria-modal="true"
      >
        <NAlert v-if="modalAction === 'stableRisk'" type="error" :bordered="false">
          该命令会先把当前稳定插件移出插件源，再排队撤回 CDN 对象。自动审查或 LLM
          结论本身不能触发此操作。
        </NAlert>
        <NSelect
          v-if="modalAction === 'stableRisk'"
          v-model:value="selectedFindingId"
          class="decision-modal__field"
          :options="criticalOptions"
          placeholder="选择 critical finding"
          aria-label="选择影响稳定版本的 critical finding"
        />
        <NInput
          v-model:value="reason"
          class="decision-modal__field"
          type="textarea"
          :rows="4"
          maxlength="2000"
          show-count
          :placeholder="reasonRequired ? '填写审查或处置依据' : '可选：填写批准说明'"
          aria-label="审查决定理由"
        />
        <NCheckbox
          v-if="modalAction === 'stableRisk'"
          v-model:checked="confirmStableRisk"
          class="decision-modal__field"
        >
          我已核对证据，并确认该 finding 影响当前稳定版本
        </NCheckbox>
        <template #footer>
          <NSpace justify="end">
            <NButton :disabled="busy" @click="closeModal">取消</NButton>
            <NButton
              :type="
                modalAction === 'approve'
                  ? 'primary'
                  : modalAction === 'requestChanges'
                    ? 'warning'
                    : 'error'
              "
              :loading="busy"
              :disabled="!canConfirm"
              @click="confirmDecision"
            >
              确认执行
            </NButton>
          </NSpace>
        </template>
      </NCard>
    </NModal>
  </NCard>
</template>

<style scoped>
.decision-actions,
.author-resubmit {
  margin-top: 14px;
}

.decision-actions :deep(.n-button) {
  width: 100%;
  justify-content: flex-start;
}

.decision-modal {
  width: min(92vw, 560px);
  border-radius: 8px;
}

.decision-modal__field {
  margin-top: 14px;
}
</style>
