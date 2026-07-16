<script setup lang="ts">
import { NAlert, NButton, NEmpty, NIcon, NSpin, NTag } from "naive-ui";
import { TimeOutline } from "@vicons/ionicons5";
import type { ReviewHistoryEvent } from "@/types/artifacts";
import { formatArtifactTime } from "@/utils/artifacts";

defineProps<{
  items: ReviewHistoryEvent[];
  loading: boolean;
  hasMore: boolean;
  error?: string;
}>();

defineEmits<{
  loadMore: [];
}>();

const EVENT_LABELS: Record<ReviewHistoryEvent["type"], string> = {
  artifact_submitted: "提交 Artifact",
  comment_event: "评论事件",
  decision: "审查决定",
  finding: "新增风险发现",
  finding_event: "风险状态事件",
  policy_event: "策略事件",
  publication_publish_failed: "CDN 发布失败",
  publication_published: "CDN 已发布",
  publication_revoke_failed: "CDN 撤回失败",
  publication_revoked: "CDN 已撤回",
  run: "审查运行",
};

function eventLabel(event: ReviewHistoryEvent): string {
  return EVENT_LABELS[event.type];
}

function payloadLines(event: ReviewHistoryEvent): string[] {
  const result: string[] = [];
  for (const [key, value] of Object.entries(event.payload)) {
    if (result.length >= 6) break;
    if (value == null || ["string", "number", "boolean"].includes(typeof value)) {
      result.push(key + ": " + String(value ?? ""));
      continue;
    }
    if (Array.isArray(value)) {
      result.push(key + ": " + value.slice(0, 5).map(String).join(", "));
    }
  }
  return result;
}
</script>

<template>
  <section class="history-panel" aria-label="Artifact 审查历史">
    <header>
      <div>
        <NIcon><TimeOutline /></NIcon>
        <strong>版本时间线</strong>
      </div>
      <span>{{ items.length }} 个事件</span>
    </header>
    <NAlert v-if="error" type="error" :bordered="false">{{ error }}</NAlert>
    <NSpin :show="loading && !items.length">
      <ol v-if="items.length" class="history-list">
        <li v-for="item in items" :key="item.type + ':' + item.id">
          <span class="history-list__marker" aria-hidden="true"></span>
          <article>
            <div class="history-list__title">
              <strong>{{ eventLabel(item) }}</strong>
              <NTag size="tiny">{{ item.source }}</NTag>
              <time>{{ formatArtifactTime(item.occurred_at) }}</time>
            </div>
            <p>
              {{ item.actor_nickname || "系统" }}
              <span>（{{ item.actor_role || "system" }}）</span>
            </p>
            <ul v-if="payloadLines(item).length">
              <li v-for="line in payloadLines(item)" :key="line">{{ line }}</li>
            </ul>
            <small v-if="item.policy_version_id" :title="item.policy_version_id">
              policy {{ item.policy_version_id.slice(0, 16) }}
            </small>
          </article>
        </li>
      </ol>
      <NEmpty v-else class="history-empty" description="暂无审查历史" />
    </NSpin>
    <div v-if="hasMore" class="history-panel__more">
      <NButton :loading="loading" @click="$emit('loadMore')">加载更多事件</NButton>
    </div>
  </section>
</template>

<style scoped>
.history-panel {
  overflow: hidden;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--card-color);
}

.history-panel > header {
  display: flex;
  min-height: 48px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-base);
}

.history-panel > header > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.history-panel > header > span,
.history-list__title time,
.history-list article > small,
.history-list article > p span {
  color: var(--text-secondary);
  font-size: 12px;
}

.history-list {
  display: grid;
  max-width: 900px;
  gap: 0;
  margin: 0;
  padding: 16px 20px;
  list-style: none;
}

.history-list > li {
  position: relative;
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr);
  gap: 10px;
  padding-bottom: 18px;
}

.history-list > li:not(:last-child)::before {
  position: absolute;
  top: 14px;
  bottom: -2px;
  left: 6px;
  width: 1px;
  background: var(--border-base);
  content: "";
}

.history-list__marker {
  position: relative;
  z-index: 1;
  width: 11px;
  height: 11px;
  margin-top: 4px;
  border: 2px solid var(--card-color);
  border-radius: 50%;
  background: var(--primary-color);
  box-shadow: 0 0 0 1px var(--primary-color);
}

.history-list article {
  min-width: 0;
}

.history-list__title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.history-list article > p {
  margin: 5px 0;
}

.history-list article > ul {
  display: grid;
  gap: 3px;
  margin: 6px 0;
  padding-left: 18px;
  color: var(--text-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.history-empty {
  padding: 80px 12px;
}

.history-panel__more {
  display: flex;
  justify-content: center;
  padding: 12px;
  border-top: 1px solid var(--border-base);
}
</style>
