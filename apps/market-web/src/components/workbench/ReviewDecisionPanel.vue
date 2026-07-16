<script setup lang="ts">
import { computed, shallowRef } from "vue";
import { NAlert, NButton, NCard, NIcon, NInput, NSpace } from "naive-ui";
import {
  CheckmarkCircleOutline,
  CloseCircleOutline,
  CreateOutline,
  RefreshOutline,
  TrashOutline,
} from "@vicons/ionicons5";
import type { PluginArtifact } from "@/types/artifacts";

const props = defineProps<{
  artifact: PluginArtifact | null;
  isAdmin: boolean;
  busy: boolean;
}>();

const emit = defineEmits<{
  approve: [reason: string];
  reject: [reason: string];
  requestChanges: [reason: string];
  retryPublish: [];
  revoke: [reason: string];
}>();

const reason = shallowRef("");
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

function reject(): void {
  if (!reason.value.trim()) return;
  emit("reject", reason.value.trim());
}

function revoke(): void {
  if (!reason.value.trim()) return;
  emit("revoke", reason.value.trim());
}

function requestChanges(): void {
  if (!reason.value.trim()) return;
  emit("requestChanges", reason.value.trim());
}
</script>

<template>
  <NCard v-if="artifact" title="审查决策" size="small" :aria-busy="busy" aria-live="polite">
    <NAlert v-if="!isAdmin" type="info" :bordered="false">
      自动审查只提供建议，最终发布由管理员人工复核。未通过版本不会获得插件源 CDN 链接；用户仍可选择
      GitHub 直连。
    </NAlert>
    <template v-else>
      <NInput
        v-model:value="reason"
        type="textarea"
        :rows="3"
        maxlength="2000"
        show-count
        placeholder="填写审查依据；拒绝或撤回时必填"
      />
      <NSpace class="decision-actions" justify="end">
        <NButton
          v-if="canRevoke"
          type="error"
          secondary
          :loading="busy"
          :disabled="!reason.trim()"
          :aria-label="isRetryRevoke ? '重试下架当前 CDN 版本' : '下架当前 CDN 版本'"
          @click="revoke"
        >
          <template #icon
            ><NIcon><TrashOutline /></NIcon
          ></template>
          {{ isRetryRevoke ? "重试下架" : "下架当前 CDN 版本" }}
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
          :loading="busy"
          :disabled="!reason.trim()"
          aria-label="要求作者修改候选版本"
          @click="requestChanges"
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
          :loading="busy"
          :disabled="!reason.trim()"
          aria-label="拒绝候选版本"
          @click="reject"
        >
          <template #icon
            ><NIcon><CloseCircleOutline /></NIcon
          ></template>
          拒绝
        </NButton>
        <NButton
          v-if="pending"
          type="primary"
          :loading="busy"
          aria-label="批准并发布候选版本"
          @click="$emit('approve', reason.trim())"
        >
          <template #icon
            ><NIcon><CheckmarkCircleOutline /></NIcon
          ></template>
          批准并发布
        </NButton>
      </NSpace>
    </template>
  </NCard>
</template>

<style scoped>
.decision-actions {
  margin-top: 14px;
}
</style>
