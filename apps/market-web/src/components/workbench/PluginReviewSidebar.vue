<script setup lang="ts">
import { NEmpty, NSelect, NSpin, NTag } from "naive-ui";
import type { ArtifactRiskLevel, PluginArtifact } from "@/types/artifacts";
import {
  REVIEW_STATUS_LABELS,
  RISK_LABELS,
  formatArtifactTime,
  reviewTagType,
  riskTagType,
} from "@/utils/artifacts";

defineProps<{
  artifacts: PluginArtifact[];
  selectedId: string;
  statusFilter: string;
  riskFilter: ArtifactRiskLevel | "";
  loading: boolean;
  isAdmin: boolean;
}>();

defineEmits<{
  select: [artifactId: string];
  statusChange: [status: string];
  riskChange: [risk: ArtifactRiskLevel | ""];
}>();

const statusOptions = [
  { label: "全部状态", value: "" },
  { label: "隔离中", value: "quarantined" },
  { label: "基础校验", value: "prechecking" },
  { label: "待人工审查", value: "pending_review" },
  { label: "需要修改", value: "changes_requested" },
  { label: "处理中", value: "scanning" },
  { label: "已批准", value: "approved" },
  { label: "已拒绝", value: "rejected" },
  { label: "已撤回", value: "withdrawn" },
  { label: "处理失败", value: "processing_failed" },
];
const riskOptions = [
  { label: "全部风险", value: "" },
  { label: "严重风险", value: "critical" },
  { label: "高风险", value: "high" },
  { label: "中风险", value: "medium" },
  { label: "低风险", value: "low" },
  { label: "无命中", value: "none" },
];
</script>

<template>
  <section class="review-sidebar" aria-label="Artifact 列表">
    <div class="review-sidebar__toolbar">
      <strong>{{ isAdmin ? "待审队列" : "我的版本" }}</strong>
      <div class="review-sidebar__filters">
        <NSelect
          size="small"
          :value="statusFilter"
          :options="statusOptions"
          aria-label="筛选审查状态"
          @update:value="$emit('statusChange', String($event || ''))"
        />
        <NSelect
          v-if="isAdmin"
          size="small"
          :value="riskFilter"
          :options="riskOptions"
          aria-label="筛选风险等级"
          @update:value="$emit('riskChange', ($event || '') as ArtifactRiskLevel | '')"
        />
      </div>
    </div>
    <NSpin :show="loading">
      <div v-if="artifacts.length" class="review-sidebar__list">
        <button
          v-for="artifact in artifacts"
          :key="artifact.id"
          type="button"
          class="artifact-row"
          :class="{ 'artifact-row--selected': artifact.id === selectedId }"
          :aria-current="artifact.id === selectedId ? 'true' : undefined"
          @click="$emit('select', artifact.id)"
        >
          <span class="artifact-row__title">
            {{ artifact.plugin_name || artifact.plugin_id }}
          </span>
          <span class="artifact-row__version">
            {{ artifact.version || "版本待解析" }} · {{ artifact.source_type }}
          </span>
          <span class="artifact-row__tags">
            <NTag size="tiny" :type="reviewTagType(artifact.review_status)">
              {{ REVIEW_STATUS_LABELS[artifact.review_status] }}
            </NTag>
            <NTag size="tiny" :type="riskTagType(artifact.risk_level)">
              {{ RISK_LABELS[artifact.risk_level] }}
            </NTag>
          </span>
          <time class="artifact-row__time">{{ formatArtifactTime(artifact.created_at) }}</time>
        </button>
      </div>
      <NEmpty v-else class="review-sidebar__empty" description="暂无版本记录" />
    </NSpin>
  </section>
</template>

<style scoped>
.review-sidebar {
  position: sticky;
  top: 72px;
  max-height: calc(100vh - 72px);
  overflow-y: auto;
}

.review-sidebar__toolbar {
  display: grid;
  gap: 10px;
  padding: 18px;
  border-bottom: 1px solid var(--border-base);
}

.review-sidebar__filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px;
}

.review-sidebar__list {
  display: grid;
}

.artifact-row {
  display: grid;
  width: 100%;
  gap: 6px;
  padding: 16px 18px;
  border: 0;
  border-bottom: 1px solid var(--border-base);
  background: transparent;
  color: var(--text-primary);
  text-align: left;
  cursor: pointer;
}

.artifact-row:hover,
.artifact-row--selected {
  background: var(--hover-color);
}

.artifact-row--selected {
  box-shadow: inset 3px 0 var(--primary-color);
}

.artifact-row__title {
  overflow: hidden;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-row__version,
.artifact-row__time {
  color: var(--text-secondary);
  font-size: 12px;
}

.artifact-row__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.review-sidebar__empty {
  padding: 48px 16px;
}

@media (max-width: 860px) {
  .review-sidebar {
    position: static;
    max-height: none;
  }
}
</style>
