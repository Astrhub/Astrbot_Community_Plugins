<script setup lang="ts">
import { computed, shallowRef, watch } from "vue";
import { NAlert, NButton, NCard, NCheckbox, NInput, NSpace, NTag } from "naive-ui";
import type { ArtifactDecision, PluginArtifact } from "@/types/artifacts";

const props = defineProps<{
  artifact: PluginArtifact | null;
  decisions: ArtifactDecision[];
  isAdmin: boolean;
  busy: boolean;
  error: string;
}>();
const emit = defineEmits<{
  action: [payload: { action: "comment" | "manual_approve" | "retry_review"; reason: string }];
  refresh: [];
}>();
const reason = shallowRef("");
const confirmed = shallowRef(false);
const reviewable = computed(() =>
  Boolean(
    props.artifact &&
    ["pending_review", "scanning", "processing_failed"].includes(props.artifact.review_status) &&
    props.artifact.publication_status === "unpublished",
  ),
);
const comments = computed(() =>
  props.decisions.filter((item) =>
    ["comment", "manual_approve", "retry_review"].includes(item.action),
  ),
);
watch(
  () => props.artifact?.id,
  () => {
    reason.value = "";
    confirmed.value = false;
  },
);
function submit(action: "comment" | "manual_approve" | "retry_review"): void {
  if (!props.artifact || props.busy || !reason.value.trim()) return;
  if (action !== "comment" && (!reviewable.value || !confirmed.value)) return;
  emit("action", { action, reason: reason.value.trim() });
}
</script>

<template>
  <NCard title="人工审查与版本评论" size="small" :aria-busy="busy">
    <NAlert v-if="error" type="error" :bordered="false">
      {{ error }} <NButton text @click="emit('refresh')">重新加载版本信息</NButton>
    </NAlert>
    <template v-if="artifact">
      <p>版本 {{ artifact.version || "待解析" }} · {{ artifact.plugin_id }}</p>
      <p v-if="reviewable">自动检查失败或未完成时，仍可评论并人工确认放行；原扫描结果会保留。</p>
      <p v-if="isAdmin && reviewable">
        人工放行意见会作为审查评价随通知邮件发送；请勿填写凭据或粘贴源码。
      </p>
      <div v-for="item in comments" :key="item.id" class="version-comment">
        <NTag size="small">{{
          item.action === "manual_approve"
            ? "人工批准"
            : item.action === "retry_review"
              ? "重新审查"
              : "版本评论"
        }}</NTag>
        <small>{{ item.reviewer_nickname }} · {{ item.created_at }}</small>
        <p>{{ item.reason }}</p>
      </div>
      <template v-if="isAdmin">
        <NInput
          v-model:value="reason"
          type="textarea"
          :maxlength="10000"
          :disabled="busy"
          placeholder="填写版本级审查意见，无需选择文件或行号"
          aria-label="版本级审查意见"
        />
        <NCheckbox
          v-if="reviewable"
          v-model:checked="confirmed"
          :disabled="busy"
          class="confirmation"
        >
          我已核对当前提交包与检查状态，确认执行所选操作
        </NCheckbox>
        <NSpace class="actions">
          <NButton :disabled="busy || !reason.trim()" @click="submit('comment')"
            >仅发表评论</NButton
          >
          <NButton
            v-if="reviewable"
            type="primary"
            :disabled="busy || !reason.trim() || !confirmed"
            @click="submit('manual_approve')"
            >评论并人工放行</NButton
          >
          <NButton
            v-if="reviewable"
            :disabled="busy || !reason.trim() || !confirmed"
            @click="submit('retry_review')"
            >重新运行自动审查</NButton
          >
        </NSpace>
      </template>
    </template>
  </NCard>
</template>

<style scoped>
.version-comment {
  border-top: 1px solid var(--border-color);
  margin-top: 12px;
  padding-top: 12px;
}
.version-comment p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.version-comment small {
  margin-left: 8px;
}
.confirmation,
.actions {
  margin-top: 12px;
}
</style>
