<script setup lang="ts">
import { computed } from "vue";
import {
  NAlert,
  NButton,
  NEmpty,
  NIcon,
  NRadioButton,
  NRadioGroup,
  NSpin,
  NTag,
  NTooltip,
} from "naive-ui";
import { ChevronBackOutline, ChevronForwardOutline, GitCompareOutline } from "@vicons/ionicons5";
import type {
  ArtifactDiff,
  ArtifactDiffContentResponse,
  ArtifactDiffHunk,
  ArtifactDiffLine,
  ArtifactDiffListResponse,
  ReviewCommentSide,
} from "@/types/artifacts";

const props = defineProps<{
  diffs: ArtifactDiffListResponse | null;
  content: ArtifactDiffContentResponse | null;
  selectedDiffId: string;
  selectedSide: ReviewCommentSide;
  selectedHunkId: string;
  selectedLineStart: number | null;
  selectedLineEnd: number | null;
  loadingDiffs: boolean;
  loadingContent: boolean;
  diffsError?: string;
  contentError?: string;
}>();

const emit = defineEmits<{
  selectDiff: [diffId: string];
  sideChange: [side: ReviewCommentSide];
  selectLine: [
    payload: {
      fileId: string;
      side: ReviewCommentSide;
      lineStart: number;
      lineEnd: number;
      diffId: string;
      hunkId: string;
    },
  ];
  diffsPage: [offset: number];
}>();

const selectedDiff = computed<ArtifactDiff | null>(
  () => props.diffs?.items.find((item) => item.id === props.selectedDiffId) ?? null,
);
const visibleContent = computed(() =>
  props.content?.diff.id === props.selectedDiffId ? props.content : null,
);
const canPreviousDiffs = computed(() => (props.diffs?.offset || 0) > 0);
const canNextDiffs = computed(() =>
  Boolean(props.diffs && props.diffs.offset + props.diffs.items.length < props.diffs.total),
);

function changeType(diff: ArtifactDiff): "default" | "info" | "success" | "warning" | "error" {
  if (diff.change_type === "added") return "success";
  if (diff.change_type === "deleted") return "error";
  if (diff.change_type === "modified") return "warning";
  if (diff.change_type === "renamed") return "info";
  return "default";
}

function lineAnchor(
  line: ArtifactDiffLine,
  hunk: ArtifactDiffHunk,
): {
  fileId: string;
  side: ReviewCommentSide;
  lineNumber: number;
  hunkId: string;
} | null {
  const diff = selectedDiff.value;
  if (!diff) return null;
  const side: ReviewCommentSide =
    line.kind === "delete" ? "base" : line.kind === "add" ? "current" : props.selectedSide;
  const fileId = side === "base" ? diff.base_file_id : diff.current_file_id;
  const lineNumber = side === "base" ? line.old_line : line.new_line;
  if (!fileId || !lineNumber) return null;
  return { fileId, side, lineNumber, hunkId: hunk.id };
}

function selectLine(line: ArtifactDiffLine, hunk: ArtifactDiffHunk, event: MouseEvent): void {
  const anchor = lineAnchor(line, hunk);
  if (!anchor || !selectedDiff.value) return;
  const rangeStart =
    event.shiftKey &&
    props.selectedSide === anchor.side &&
    props.selectedHunkId === hunk.id &&
    props.selectedLineStart
      ? props.selectedLineStart
      : anchor.lineNumber;
  emit("selectLine", {
    fileId: anchor.fileId,
    side: anchor.side,
    lineStart: Math.min(rangeStart, anchor.lineNumber),
    lineEnd: Math.max(rangeStart, anchor.lineNumber),
    diffId: selectedDiff.value.id,
    hunkId: anchor.hunkId,
  });
}

function isSelectedLine(line: ArtifactDiffLine, hunk: ArtifactDiffHunk): boolean {
  const anchor = lineAnchor(line, hunk);
  return Boolean(
    anchor &&
    props.selectedHunkId === hunk.id &&
    props.selectedSide === anchor.side &&
    props.selectedLineStart &&
    anchor.lineNumber >= props.selectedLineStart &&
    anchor.lineNumber <= (props.selectedLineEnd || props.selectedLineStart),
  );
}

function lineClasses(line: ArtifactDiffLine, hunk: ArtifactDiffHunk) {
  return ["diff-line--" + line.kind, { "diff-line--selected": isSelectedLine(line, hunk) }];
}

function hunkLabel(hunk: ArtifactDiffHunk): string {
  return (selectedDiff.value?.path || "Diff") + " " + hunk.header;
}

function previousDiffs(): void {
  if (!props.diffs) return;
  emit("diffsPage", Math.max(0, props.diffs.offset - props.diffs.limit));
}

function nextDiffs(): void {
  if (!props.diffs) return;
  emit("diffsPage", props.diffs.offset + props.diffs.limit);
}
</script>

<template>
  <section class="diff-viewer" aria-label="Artifact 差异查看器">
    <aside class="diff-viewer__list">
      <div class="panel-heading">
        <div>
          <strong>变更文件</strong>
          <span>{{ diffs?.total || 0 }} 项</span>
        </div>
        <div class="pager-actions">
          <NTooltip>
            <template #trigger>
              <NButton
                quaternary
                circle
                size="small"
                :disabled="!canPreviousDiffs || loadingDiffs"
                aria-label="上一页变更"
                @click="previousDiffs"
              >
                <template #icon
                  ><NIcon><ChevronBackOutline /></NIcon
                ></template>
              </NButton>
            </template>
            上一页变更
          </NTooltip>
          <NTooltip>
            <template #trigger>
              <NButton
                quaternary
                circle
                size="small"
                :disabled="!canNextDiffs || loadingDiffs"
                aria-label="下一页变更"
                @click="nextDiffs"
              >
                <template #icon
                  ><NIcon><ChevronForwardOutline /></NIcon
                ></template>
              </NButton>
            </template>
            下一页变更
          </NTooltip>
        </div>
      </div>
      <NAlert v-if="diffsError" type="error" :bordered="false">{{ diffsError }}</NAlert>
      <NSpin :show="loadingDiffs">
        <div v-if="diffs?.items.length" class="diff-list" role="listbox" aria-label="变更列表">
          <button
            v-for="diff in diffs.items"
            :key="diff.id"
            type="button"
            role="option"
            class="diff-row"
            :class="{ 'diff-row--selected': diff.id === selectedDiffId }"
            :aria-selected="diff.id === selectedDiffId"
            :title="diff.path"
            @click="$emit('selectDiff', diff.id)"
          >
            <NIcon><GitCompareOutline /></NIcon>
            <span>{{ diff.path }}</span>
            <NTag size="tiny" :type="changeType(diff)">{{ diff.change_type }}</NTag>
            <small>+{{ diff.stats.added_lines }} / -{{ diff.stats.deleted_lines }}</small>
          </button>
        </div>
        <NEmpty v-else class="panel-empty" description="当前版本没有可用 diff" />
      </NSpin>
    </aside>

    <div class="diff-viewer__content">
      <div v-if="selectedDiff" class="panel-heading panel-heading--diff">
        <div class="selected-diff-title">
          <strong :title="selectedDiff.path">{{ selectedDiff.path }}</strong>
          <span v-if="selectedDiff.base_path && selectedDiff.base_path !== selectedDiff.path">
            {{ selectedDiff.base_path }} → {{ selectedDiff.path }}
          </span>
          <span v-else>
            {{ selectedDiff.stats.hunk_count }} hunks · +{{ selectedDiff.stats.added_lines }} / -{{
              selectedDiff.stats.deleted_lines
            }}
          </span>
        </div>
        <NRadioGroup
          size="small"
          :value="selectedSide"
          aria-label="选择评论行侧"
          @update:value="$emit('sideChange', $event as ReviewCommentSide)"
        >
          <NRadioButton value="base" :disabled="!selectedDiff.base_file_id">Base</NRadioButton>
          <NRadioButton value="current" :disabled="!selectedDiff.current_file_id">
            Current
          </NRadioButton>
        </NRadioGroup>
      </div>

      <NAlert v-if="contentError" type="error" :bordered="false">{{ contentError }}</NAlert>
      <NSpin :show="loadingContent">
        <template v-if="visibleContent">
          <NAlert v-if="!visibleContent.hunks_available" type="info" :bordered="false">
            {{ visibleContent.unavailable_reason || "该变更没有可浏览的文本 hunks" }}
          </NAlert>
          <NAlert v-if="visibleContent.truncated" type="warning" :bordered="false">
            Diff 已按安全上限截断，省略 {{ visibleContent.omitted_hunks }} 个
            hunks；必须继续人工复核。
          </NAlert>
          <div v-if="visibleContent.hunks.length" class="diff-hunks">
            <section v-for="hunk in visibleContent.hunks" :key="hunk.id" class="diff-hunk">
              <header>{{ hunk.header }}</header>
              <div class="diff-lines" role="list" :aria-label="hunkLabel(hunk)">
                <button
                  v-for="(line, index) in hunk.lines"
                  :key="hunk.id + ':' + index"
                  type="button"
                  role="listitem"
                  class="diff-line"
                  :class="lineClasses(line, hunk)"
                  :disabled="!lineAnchor(line, hunk)"
                  @click="selectLine(line, hunk, $event)"
                >
                  <span class="diff-line__number" aria-hidden="true">{{
                    line.old_line || ""
                  }}</span>
                  <span class="diff-line__number" aria-hidden="true">{{
                    line.new_line || ""
                  }}</span>
                  <span class="diff-line__prefix" aria-hidden="true">{{ line.prefix }}</span>
                  <code>{{ line.text || " " }}</code>
                </button>
              </div>
            </section>
          </div>
          <NEmpty
            v-else
            class="panel-empty panel-empty--content"
            description="没有可显示的文本差异"
          />
        </template>
        <NEmpty v-else class="panel-empty panel-empty--content" description="选择一个变更文件" />
      </NSpin>
    </div>
  </section>
</template>

<style scoped>
.diff-viewer {
  display: grid;
  grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
  min-height: 560px;
  overflow: hidden;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--card-color);
}

.diff-viewer__list {
  min-width: 0;
  border-right: 1px solid var(--border-base);
}

.diff-viewer__content {
  min-width: 0;
  overflow: hidden;
}

.panel-heading {
  display: flex;
  min-height: 48px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-base);
}

.panel-heading > div:first-child,
.selected-diff-title {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.panel-heading span,
.panel-heading small {
  color: var(--text-secondary);
  font-size: 12px;
}

.selected-diff-title strong,
.diff-row span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pager-actions {
  display: flex;
  flex-shrink: 0;
  gap: 4px;
}

.diff-list {
  max-height: 650px;
  overflow: auto;
}

.diff-row {
  display: grid;
  width: 100%;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 6px 8px;
  padding: 10px 12px;
  border: 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border-base) 70%, transparent);
  background: transparent;
  color: var(--text-primary);
  text-align: left;
  cursor: pointer;
}

.diff-row small {
  grid-column: 2 / 4;
  color: var(--text-secondary);
  font-size: 11px;
}

.diff-row:hover,
.diff-row:focus-visible,
.diff-row--selected {
  background: var(--hover-color);
}

.diff-row--selected {
  box-shadow: inset 3px 0 var(--primary-color);
}

.diff-hunks {
  max-height: 650px;
  overflow: auto;
  background: var(--code-color);
}

.diff-hunk header {
  position: sticky;
  z-index: 2;
  top: 0;
  padding: 6px 12px;
  border-top: 1px solid var(--border-base);
  border-bottom: 1px solid var(--border-base);
  background: color-mix(in srgb, var(--primary-color) 12%, var(--card-color));
  color: var(--text-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
}

.diff-lines {
  overflow-x: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
}

.diff-line {
  display: grid;
  min-width: 100%;
  width: max-content;
  grid-template-columns: 54px 54px 22px minmax(max-content, 1fr);
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-primary);
  text-align: left;
}

.diff-line:not(:disabled) {
  cursor: text;
}

.diff-line--add {
  background: color-mix(in srgb, #16a34a 12%, transparent);
}

.diff-line--delete {
  background: color-mix(in srgb, #dc2626 12%, transparent);
}

.diff-line:hover:not(:disabled),
.diff-line:focus-visible,
.diff-line--selected {
  box-shadow: inset 3px 0 var(--primary-color);
  filter: brightness(0.98);
}

.diff-line__number {
  padding: 2px 8px;
  border-right: 1px solid var(--border-base);
  color: var(--text-secondary);
  text-align: right;
  user-select: none;
}

.diff-line__prefix {
  padding: 2px 6px;
  text-align: center;
  user-select: none;
}

.diff-line code {
  padding: 2px 12px 2px 0;
  white-space: pre;
}

.panel-empty {
  padding: 52px 12px;
}

.panel-empty--content {
  padding-top: 160px;
}

@media (max-width: 720px) {
  .diff-viewer {
    grid-template-columns: 1fr;
    min-height: 0;
  }

  .diff-viewer__list {
    border-right: 0;
    border-bottom: 1px solid var(--border-base);
  }

  .diff-list {
    max-height: 220px;
  }

  .diff-hunks {
    max-height: 58vh;
  }
}
</style>
