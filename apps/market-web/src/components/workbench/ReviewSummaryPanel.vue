<script setup lang="ts">
import { computed } from "vue";
import { NAlert, NCard, NDescriptions, NDescriptionsItem, NEmpty, NSpin, NTag } from "naive-ui";
import type { ArtifactDetail } from "@/types/artifacts";
import {
  PUBLICATION_STATUS_LABELS,
  REVIEW_STATUS_LABELS,
  RISK_LABELS,
  formatArtifactTime,
  formatBytes,
  reviewTagType,
  riskTagType,
} from "@/utils/artifacts";

const props = defineProps<{
  detail: ArtifactDetail | null;
  loading: boolean;
}>();

const sortedFindings = computed(() => {
  const order = { critical: 5, high: 4, medium: 3, low: 2, info: 1 };
  return [...(props.detail?.findings || [])].sort(
    (left, right) => order[right.severity] - order[left.severity],
  );
});
</script>

<template>
  <NSpin :show="loading">
    <NEmpty v-if="!detail" class="summary-empty" description="选择一个版本查看审查结果" />
    <div v-else class="summary-grid">
      <NCard title="版本摘要" size="small">
        <template #header-extra>
          <div class="summary-tags">
            <NTag :type="reviewTagType(detail.artifact.review_status)">
              {{ REVIEW_STATUS_LABELS[detail.artifact.review_status] }}
            </NTag>
            <NTag :type="riskTagType(detail.artifact.risk_level)">
              {{ RISK_LABELS[detail.artifact.risk_level] }}
            </NTag>
          </div>
        </template>
        <NDescriptions label-placement="left" :column="2" bordered size="small">
          <NDescriptionsItem label="插件">
            {{ detail.artifact.plugin_name || detail.artifact.plugin_id }}
          </NDescriptionsItem>
          <NDescriptionsItem label="候选版本">
            {{ detail.artifact.version || "待解析" }}
          </NDescriptionsItem>
          <NDescriptionsItem label="仓库版本">
            {{ detail.artifact.repo_version || "尚未同步" }}
          </NDescriptionsItem>
          <NDescriptionsItem label="CDN 版本">
            {{ detail.artifact.published_version || "未发布" }}
          </NDescriptionsItem>
          <NDescriptionsItem label="来源">
            {{ detail.artifact.source_type }}
            <span v-if="detail.artifact.source_ref"> · {{ detail.artifact.source_ref }}</span>
          </NDescriptionsItem>
          <NDescriptionsItem label="包大小">
            {{ formatBytes(detail.artifact.size_bytes) }}
          </NDescriptionsItem>
          <NDescriptionsItem label="发布状态">
            {{ PUBLICATION_STATUS_LABELS[detail.artifact.publication_status] }}
          </NDescriptionsItem>
          <NDescriptionsItem label="提交时间">
            {{ formatArtifactTime(detail.artifact.created_at) }}
          </NDescriptionsItem>
        </NDescriptions>
        <NAlert
          v-if="
            detail.artifact.repo_version && detail.artifact.version !== detail.artifact.repo_version
          "
          class="version-warning"
          type="warning"
          :bordered="false"
        >
          仓库版本与候选包版本不同。该版本即使完成审查，也不会覆盖稳定 CDN 链接。
        </NAlert>
      </NCard>

      <NCard title="自动审查运行" size="small">
        <div v-if="detail.runs.length" class="run-list">
          <article v-for="run in detail.runs" :key="run.id" class="run-item">
            <div class="run-item__title">
              <strong>{{ run.type }}</strong>
              <NTag
                size="small"
                :type="
                  run.status === 'succeeded'
                    ? 'success'
                    : run.status === 'failed'
                      ? 'error'
                      : 'info'
                "
              >
                {{ run.status }}
              </NTag>
            </div>
            <p>{{ run.summary || "暂无摘要" }}</p>
            <small>{{ formatArtifactTime(run.completed_at || run.created_at) }}</small>
          </article>
        </div>
        <NEmpty v-else description="自动审查尚未开始" />
      </NCard>

      <NCard class="findings-card" title="结构化风险发现" size="small">
        <div v-if="sortedFindings.length" class="finding-list">
          <article v-for="finding in sortedFindings" :key="finding.id" class="finding-item">
            <div class="finding-item__title">
              <NTag
                size="small"
                :type="riskTagType(finding.severity === 'info' ? 'none' : finding.severity)"
              >
                {{ finding.severity }}
              </NTag>
              <code>{{ finding.rule_id || finding.category || "RULE" }}</code>
              <span
                >{{ finding.file_path || "包级发现"
                }}<template v-if="finding.line_start">:{{ finding.line_start }}</template></span
              >
            </div>
            <p>{{ finding.message }}</p>
            <p v-if="finding.suggestion" class="finding-item__suggestion">
              建议：{{ finding.suggestion }}
            </p>
            <code v-if="finding.evidence_excerpt" class="finding-item__evidence">
              {{ finding.evidence_excerpt }}
            </code>
          </article>
        </div>
        <NEmpty v-else description="未发现静态风险命中" />
      </NCard>
    </div>
  </NSpin>
</template>

<style scoped>
.summary-empty {
  padding: 88px 16px;
}

.summary-grid {
  display: grid;
  gap: 18px;
}

.summary-tags,
.run-item__title,
.finding-item__title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.version-warning {
  margin-top: 14px;
}

.run-list,
.finding-list {
  display: grid;
  gap: 10px;
}

.run-item,
.finding-item {
  padding: 12px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.run-item p,
.finding-item p {
  margin: 8px 0 0;
}

.run-item small,
.finding-item__suggestion {
  color: var(--text-secondary);
}

.finding-item__evidence {
  display: block;
  margin-top: 10px;
  padding: 8px;
  overflow-wrap: anywhere;
  border-radius: 6px;
  background: var(--code-color);
  white-space: pre-wrap;
}

@media (max-width: 720px) {
  :deep(.n-descriptions-table-wrapper) {
    overflow-x: auto;
  }
}
</style>
