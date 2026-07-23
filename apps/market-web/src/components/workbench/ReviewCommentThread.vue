<script setup lang="ts">
import { computed, reactive, shallowRef } from "vue";
import { NAlert, NButton, NEmpty, NIcon, NInput, NSpin, NTag, NTooltip } from "naive-ui";
import {
  CheckmarkDoneOutline,
  LockClosedOutline,
  PencilOutline,
  RefreshOutline,
  SendOutline,
} from "@vicons/ionicons5";
import type {
  ReviewAnchor,
  ReviewComment,
  ReviewCommentCreateInput,
  ReviewCommentEvent,
  ReviewCommentListResponse,
} from "@/types/artifacts";
import { formatArtifactTime } from "@/utils/artifacts";

const props = defineProps<{
  comments: ReviewCommentListResponse | null;
  anchor: ReviewAnchor | null;
  isAdmin: boolean;
  currentNickname: string;
  canCreate: boolean;
  loading: boolean;
  busy: boolean;
  error?: string;
}>();

const emit = defineEmits<{
  create: [input: ReviewCommentCreateInput];
  reply: [payload: { threadId: string; version: number; body: string }];
  address: [payload: { threadId: string; version: number; body: string }];
  edit: [payload: { threadId: string; version: number; body: string }];
  resolve: [payload: { threadId: string; version: number }];
  reopen: [payload: { threadId: string; version: number }];
  page: [offset: number];
}>();

const newBody = shallowRef("");
const replyBodies = reactive<Record<string, string>>({});
const addressedBodies = reactive<Record<string, string>>({});
const editBodies = reactive<Record<string, string>>({});
const editingId = shallowRef("");

const orderedThreads = computed(() => {
  const anchor = props.anchor;
  return [...(props.comments?.items || [])].sort((left, right) => {
    const leftSelected = anchor && matchesAnchor(left, anchor) ? 1 : 0;
    const rightSelected = anchor && matchesAnchor(right, anchor) ? 1 : 0;
    if (leftSelected !== rightSelected) return rightSelected - leftSelected;
    if (left.resolved !== right.resolved) return Number(left.resolved) - Number(right.resolved);
    return left.created_at.localeCompare(right.created_at);
  });
});
const canPrevious = computed(() => (props.comments?.offset || 0) > 0);
const canNext = computed(() =>
  Boolean(
    props.comments && props.comments.offset + props.comments.items.length < props.comments.total,
  ),
);

function matchesAnchor(thread: ReviewComment, anchor: ReviewAnchor): boolean {
  return (
    thread.file_id === anchor.fileId &&
    thread.side === anchor.side &&
    thread.line_start <= anchor.lineEnd &&
    thread.line_end >= anchor.lineStart
  );
}

function visibleEvents(thread: ReviewComment): ReviewCommentEvent[] {
  return thread.events.filter((event) => event.type !== "create");
}

function eventLabel(event: ReviewCommentEvent): string {
  const labels: Record<ReviewCommentEvent["type"], string> = {
    create: "创建评论",
    edit: "编辑评论",
    reply: "回复",
    resolve: "标记已解决",
    reopen: "重新打开",
    author_addressed: "作者标记已处理",
  };
  return labels[event.type];
}

function submitCreate(): void {
  if (!props.anchor || !newBody.value.trim()) return;
  const anchor = props.anchor;
  emit("create", {
    file_id: anchor.fileId,
    side: anchor.side,
    line_start: anchor.lineStart,
    line_end: anchor.lineEnd,
    body: newBody.value.trim(),
    ...(anchor.diffId ? { diff_id: anchor.diffId } : {}),
    ...(anchor.hunkId ? { hunk_id: anchor.hunkId } : {}),
  });
  newBody.value = "";
}

function submitReply(thread: ReviewComment): void {
  const body = (replyBodies[thread.id] || "").trim();
  if (!body) return;
  emit("reply", { threadId: thread.id, version: thread.version, body });
  replyBodies[thread.id] = "";
}

function submitAddressed(thread: ReviewComment): void {
  emit("address", {
    threadId: thread.id,
    version: thread.version,
    body: (addressedBodies[thread.id] || "").trim(),
  });
  addressedBodies[thread.id] = "";
}

function startEdit(thread: ReviewComment): void {
  editingId.value = thread.id;
  editBodies[thread.id] = thread.body;
}

function submitEdit(thread: ReviewComment): void {
  const body = (editBodies[thread.id] || "").trim();
  if (!body) return;
  emit("edit", { threadId: thread.id, version: thread.version, body });
  editingId.value = "";
}

function previousPage(): void {
  if (!props.comments) return;
  emit("page", Math.max(0, props.comments.offset - props.comments.limit));
}

function nextPage(): void {
  if (!props.comments) return;
  emit("page", props.comments.offset + props.comments.limit);
}
</script>

<template>
  <section class="comment-panel" aria-label="行级审查评论">
    <header class="comment-panel__header">
      <div>
        <strong>审查评论</strong>
        <span>{{ comments?.total || 0 }} 个线程</span>
      </div>
      <div class="comment-panel__pager">
        <NButton quaternary size="tiny" :disabled="!canPrevious || loading" @click="previousPage">
          上一页
        </NButton>
        <NButton quaternary size="tiny" :disabled="!canNext || loading" @click="nextPage">
          下一页
        </NButton>
      </div>
    </header>

    <NAlert v-if="error" type="error" :bordered="false">{{ error }}</NAlert>
    <div v-if="anchor" class="comment-anchor" aria-live="polite">
      <span :title="anchor.filePath">{{ anchor.filePath }}</span>
      <NTag size="small">{{ anchor.side }}</NTag>
      <strong>第 {{ anchor.lineStart }}–{{ anchor.lineEnd }} 行</strong>
    </div>
    <NAlert v-else type="info" :bordered="false"> 未选择行锚点，暂不能创建行级评论。 </NAlert>

    <div v-if="isAdmin && anchor" class="comment-compose">
      <NInput
        v-model:value="newBody"
        data-testid="new-comment"
        type="textarea"
        :rows="3"
        maxlength="10000"
        show-count
        placeholder="输入行级审查意见"
        :disabled="!canCreate || busy"
        aria-label="新建行级审查评论"
      />
      <NButton
        type="primary"
        size="small"
        :loading="busy"
        :disabled="!canCreate || !newBody.trim()"
        @click="submitCreate"
      >
        <template #icon
          ><NIcon><SendOutline /></NIcon
        ></template>
        发布评论
      </NButton>
      <small v-if="!canCreate">当前版本已进入只读终态，不能创建新线程。</small>
    </div>

    <NSpin :show="loading">
      <div v-if="orderedThreads.length" class="comment-threads">
        <article
          v-for="thread in orderedThreads"
          :key="thread.id"
          class="comment-thread"
          :class="{
            'comment-thread--resolved': thread.resolved,
            'comment-thread--selected': anchor && matchesAnchor(thread, anchor),
          }"
        >
          <header class="comment-thread__header">
            <div>
              <strong>{{ thread.reviewer_nickname || "管理员" }}</strong>
              <NTag size="tiny">{{ thread.reviewer_role }}</NTag>
              <NTag v-if="thread.resolved" size="tiny" type="success">已解决</NTag>
              <NTag v-else size="tiny" type="warning">待处理</NTag>
              <NTooltip v-if="thread.locked_at">
                <template #trigger>
                  <NTag size="tiny">
                    <template #icon
                      ><NIcon><LockClosedOutline /></NIcon
                    ></template>
                    已锁定
                  </NTag>
                </template>
                Artifact 已产生终态决定，线程只读
              </NTooltip>
            </div>
            <time>{{ formatArtifactTime(thread.created_at) }}</time>
          </header>
          <div class="comment-thread__location">
            <span :title="thread.file_path">{{ thread.file_path }}</span>
            <span>{{ thread.side }} · {{ thread.line_start }}–{{ thread.line_end }}</span>
          </div>

          <template v-if="editingId === thread.id">
            <NInput
              v-model:value="editBodies[thread.id]"
              type="textarea"
              :rows="3"
              maxlength="10000"
              aria-label="编辑审查评论"
            />
            <div class="thread-actions">
              <NButton size="tiny" @click="editingId = ''">取消</NButton>
              <NButton
                size="tiny"
                type="primary"
                :disabled="!editBodies[thread.id]?.trim() || busy"
                @click="submitEdit(thread)"
              >
                保存
              </NButton>
            </div>
          </template>
          <p v-else class="comment-thread__body">{{ thread.body }}</p>

          <ol v-if="visibleEvents(thread).length" class="comment-events">
            <li v-for="event in visibleEvents(thread)" :key="event.id">
              <div>
                <strong>{{ event.actor_nickname || event.actor_role }}</strong>
                <NTag size="tiny">{{ event.actor_role }}</NTag>
                <span>{{ eventLabel(event) }}</span>
              </div>
              <p v-if="event.body">{{ event.body }}</p>
              <time>{{ formatArtifactTime(event.created_at) }}</time>
            </li>
          </ol>
          <NAlert v-if="thread.events_truncated" type="info" :bordered="false">
            仅显示最近事件；完整事实保留在审查历史。
          </NAlert>

          <div v-if="!thread.locked_at" class="comment-reply">
            <NInput
              v-model:value="replyBodies[thread.id]"
              type="textarea"
              :rows="2"
              maxlength="10000"
              placeholder="回复线程"
              :aria-label="'回复 ' + (thread.reviewer_nickname || '审查线程')"
            />
            <div class="thread-actions">
              <NButton
                size="tiny"
                :loading="busy"
                :disabled="!replyBodies[thread.id]?.trim()"
                @click="submitReply(thread)"
              >
                <template #icon
                  ><NIcon><SendOutline /></NIcon
                ></template>
                回复
              </NButton>
              <NButton
                v-if="isAdmin && currentNickname === thread.reviewer_nickname"
                size="tiny"
                quaternary
                :disabled="busy"
                @click="startEdit(thread)"
              >
                <template #icon
                  ><NIcon><PencilOutline /></NIcon
                ></template>
                编辑
              </NButton>
              <NButton
                v-if="isAdmin && !thread.resolved"
                size="tiny"
                type="success"
                secondary
                :disabled="busy"
                @click="$emit('resolve', { threadId: thread.id, version: thread.version })"
              >
                <template #icon
                  ><NIcon><CheckmarkDoneOutline /></NIcon
                ></template>
                解决
              </NButton>
              <NButton
                v-if="isAdmin && thread.resolved"
                size="tiny"
                :disabled="busy"
                @click="$emit('reopen', { threadId: thread.id, version: thread.version })"
              >
                <template #icon
                  ><NIcon><RefreshOutline /></NIcon
                ></template>
                重开
              </NButton>
            </div>
            <div v-if="!isAdmin && !thread.resolved" class="author-addressed">
              <NInput
                v-model:value="addressedBodies[thread.id]"
                size="small"
                maxlength="10000"
                placeholder="可选：说明修改位置"
                aria-label="作者处理说明"
              />
              <NButton
                size="tiny"
                type="primary"
                secondary
                :disabled="busy"
                @click="submitAddressed(thread)"
              >
                <template #icon
                  ><NIcon><CheckmarkDoneOutline /></NIcon
                ></template>
                标记已处理
              </NButton>
            </div>
          </div>
        </article>
      </div>
      <NEmpty v-else class="comment-empty" description="当前还没有行级评论" />
    </NSpin>
  </section>
</template>

<style scoped>
.comment-panel {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--card-color);
}

.comment-panel__header,
.comment-thread__header,
.comment-thread__header > div,
.thread-actions,
.author-addressed {
  display: flex;
  align-items: center;
}

.comment-panel__header {
  min-height: 48px;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-base);
}

.comment-panel__header > div:first-child {
  display: grid;
  gap: 2px;
}

.comment-panel__header span,
.comment-thread time,
.comment-thread__location,
.comment-compose small {
  color: var(--text-secondary);
  font-size: 12px;
}

.comment-panel__pager,
.thread-actions {
  display: flex;
  gap: 6px;
}

.comment-anchor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-base);
  background: var(--hover-color);
  font-size: 12px;
}

.comment-anchor > span:first-child,
.comment-thread__location span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.comment-compose {
  display: grid;
  justify-items: end;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--border-base);
}

.comment-threads {
  display: grid;
  max-height: calc(100vh - 210px);
  overflow-y: auto;
}

.comment-thread {
  display: grid;
  gap: 10px;
  padding: 12px;
  border-bottom: 1px solid var(--border-base);
}

.comment-thread--selected {
  box-shadow: inset 3px 0 var(--primary-color);
}

.comment-thread--resolved {
  background: color-mix(in srgb, var(--success-color) 5%, transparent);
}

.comment-thread__header {
  justify-content: space-between;
  gap: 8px;
}

.comment-thread__header > div {
  min-width: 0;
  flex-wrap: wrap;
  gap: 6px;
}

.comment-thread__location {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.comment-thread__body,
.comment-events p {
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.comment-events {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 18px;
}

.comment-events li {
  padding-left: 2px;
}

.comment-events li > div {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.comment-events time {
  display: block;
  margin-top: 3px;
}

.comment-reply {
  display: grid;
  gap: 8px;
}

.thread-actions {
  justify-content: flex-end;
  flex-wrap: wrap;
}

.author-addressed {
  gap: 8px;
}

.author-addressed :deep(.n-input) {
  min-width: 0;
  flex: 1;
}

.comment-empty {
  padding: 64px 12px;
}

@media (max-width: 640px) {
  .comment-threads {
    max-height: none;
  }

  .comment-anchor,
  .comment-thread__location {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .comment-anchor strong {
    grid-column: 1 / -1;
  }

  .author-addressed {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
