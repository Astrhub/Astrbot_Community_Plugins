<script setup lang="ts">
import { computed } from "vue";
import { NAlert, NButton, NEmpty, NIcon, NSpin, NTag, NTooltip } from "naive-ui";
import {
  ChevronBackOutline,
  ChevronForwardOutline,
  CodeSlashOutline,
  DocumentOutline,
} from "@vicons/ionicons5";
import type {
  ArtifactFile,
  ArtifactFileContentResponse,
  ArtifactFileListResponse,
} from "@/types/artifacts";
import { formatBytes } from "@/utils/artifacts";

const props = defineProps<{
  files: ArtifactFileListResponse | null;
  content: ArtifactFileContentResponse | null;
  selectedFileId: string;
  selectedLineStart: number | null;
  selectedLineEnd: number | null;
  loadingFiles: boolean;
  loadingContent: boolean;
  filesError?: string;
  contentError?: string;
}>();

const emit = defineEmits<{
  selectFile: [fileId: string];
  selectLine: [payload: { fileId: string; lineStart: number; lineEnd: number }];
  contentPage: [payload: { fileId: string; startLine: number }];
  filesPage: [offset: number];
}>();

const selectedFile = computed<ArtifactFile | null>(
  () => props.files?.items.find((item) => item.id === props.selectedFileId) ?? null,
);
const visibleContent = computed(() =>
  props.content?.file.id === props.selectedFileId ? props.content : null,
);
const canPreviousFiles = computed(() => (props.files?.offset || 0) > 0);
const canNextFiles = computed(() =>
  Boolean(props.files && props.files.offset + props.files.items.length < props.files.total),
);
const canPreviousLines = computed(() => (visibleContent.value?.start_line || 1) > 1);
const canNextLines = computed(() => {
  const content = visibleContent.value;
  return Boolean(content?.end_line && content.end_line < content.total_lines);
});

function fileIcon(file: ArtifactFile) {
  return file.is_text ? CodeSlashOutline : DocumentOutline;
}

function selectFile(file: ArtifactFile): void {
  emit("selectFile", file.id);
}

function selectLine(lineNumber: number, event: MouseEvent): void {
  const anchor = event.shiftKey && props.selectedLineStart ? props.selectedLineStart : lineNumber;
  emit("selectLine", {
    fileId: props.selectedFileId,
    lineStart: Math.min(anchor, lineNumber),
    lineEnd: Math.max(anchor, lineNumber),
  });
}

function isSelectedLine(lineNumber: number): boolean {
  return Boolean(
    props.selectedLineStart &&
    lineNumber >= props.selectedLineStart &&
    lineNumber <= (props.selectedLineEnd || props.selectedLineStart),
  );
}

function previousFiles(): void {
  if (!props.files) return;
  emit("filesPage", Math.max(0, props.files.offset - props.files.limit));
}

function nextFiles(): void {
  if (!props.files) return;
  emit("filesPage", props.files.offset + props.files.limit);
}

function previousLines(): void {
  const content = visibleContent.value;
  if (!content) return;
  emit("contentPage", {
    fileId: content.file.id,
    startLine: Math.max(1, content.start_line - Math.max(content.lines.length, 1)),
  });
}

function nextLines(): void {
  const content = visibleContent.value;
  if (!content?.end_line) return;
  emit("contentPage", { fileId: content.file.id, startLine: content.end_line + 1 });
}
</script>

<template>
  <section class="file-browser" aria-label="Artifact 文件浏览器">
    <aside class="file-browser__tree">
      <div class="panel-heading">
        <div>
          <strong>文件</strong>
          <span>{{ files?.total || 0 }} 项</span>
        </div>
        <div class="pager-actions">
          <NTooltip>
            <template #trigger>
              <NButton
                quaternary
                circle
                size="small"
                :disabled="!canPreviousFiles || loadingFiles"
                aria-label="上一页文件"
                @click="previousFiles"
              >
                <template #icon
                  ><NIcon><ChevronBackOutline /></NIcon
                ></template>
              </NButton>
            </template>
            上一页文件
          </NTooltip>
          <NTooltip>
            <template #trigger>
              <NButton
                quaternary
                circle
                size="small"
                :disabled="!canNextFiles || loadingFiles"
                aria-label="下一页文件"
                @click="nextFiles"
              >
                <template #icon
                  ><NIcon><ChevronForwardOutline /></NIcon
                ></template>
              </NButton>
            </template>
            下一页文件
          </NTooltip>
        </div>
      </div>
      <NAlert v-if="filesError" type="error" :bordered="false">{{ filesError }}</NAlert>
      <NSpin :show="loadingFiles">
        <div v-if="files?.items.length" class="file-tree" role="listbox" aria-label="文件列表">
          <button
            v-for="file in files.items"
            :key="file.id"
            type="button"
            role="option"
            class="file-row"
            :class="{ 'file-row--selected': file.id === selectedFileId }"
            :aria-selected="file.id === selectedFileId"
            :title="file.path"
            @click="selectFile(file)"
          >
            <NIcon :component="fileIcon(file)" />
            <span>{{ file.path }}</span>
            <small>{{ formatBytes(file.size_bytes) }}</small>
          </button>
        </div>
        <NEmpty v-else class="panel-empty" description="没有可浏览文件" />
      </NSpin>
    </aside>

    <div class="file-browser__content">
      <div v-if="selectedFile" class="panel-heading panel-heading--content">
        <div class="selected-file-title">
          <strong :title="selectedFile.path">{{ selectedFile.path }}</strong>
          <span>{{ selectedFile.mime_type }} · {{ formatBytes(selectedFile.size_bytes) }}</span>
        </div>
        <div class="file-badges">
          <NTag v-if="selectedFile.is_entrypoint" size="small" type="info">入口</NTag>
          <NTag v-if="selectedFile.is_reachable" size="small" type="success">入口可达</NTag>
          <NTag v-if="!selectedFile.is_text" size="small">binary</NTag>
        </div>
      </div>

      <NAlert v-if="contentError" type="error" :bordered="false">{{ contentError }}</NAlert>
      <NAlert
        v-else-if="selectedFile && !selectedFile.content_available"
        type="info"
        :bordered="false"
      >
        该文件只提供元数据，二进制或不可用正文不会发送到浏览器。
      </NAlert>
      <NSpin v-else :show="loadingContent">
        <div v-if="visibleContent" class="code-panel">
          <div class="code-toolbar">
            <span>
              第 {{ visibleContent.start_line }}–{{ visibleContent.end_line || 0 }} 行 / 共
              {{ visibleContent.total_lines }} 行
            </span>
            <div class="pager-actions">
              <NButton
                quaternary
                size="tiny"
                :disabled="!canPreviousLines || loadingContent"
                @click="previousLines"
              >
                上一页
              </NButton>
              <NButton
                quaternary
                size="tiny"
                :disabled="!canNextLines || loadingContent"
                @click="nextLines"
              >
                下一页
              </NButton>
            </div>
          </div>
          <NAlert v-if="visibleContent.truncated" type="warning" :bordered="false">
            响应已按安全上限截断，请继续分页查看。
          </NAlert>
          <div class="code-lines" role="list" aria-label="文本文件内容">
            <button
              v-for="line in visibleContent.lines"
              :key="line.number"
              type="button"
              role="listitem"
              class="code-line"
              :class="{ 'code-line--selected': isSelectedLine(line.number) }"
              :aria-label="'选择第 ' + line.number + ' 行'"
              @click="selectLine(line.number, $event)"
            >
              <span class="code-line__number" aria-hidden="true">{{ line.number }}</span>
              <code>{{ line.text || " " }}</code>
            </button>
          </div>
        </div>
        <NEmpty
          v-else-if="selectedFile"
          class="panel-empty panel-empty--content"
          description="选择文本文件后加载受限正文"
        />
        <NEmpty v-else class="panel-empty panel-empty--content" description="选择一个文件" />
      </NSpin>
    </div>
  </section>
</template>

<style scoped>
.file-browser {
  display: grid;
  grid-template-columns: minmax(210px, 260px) minmax(0, 1fr);
  min-height: 560px;
  overflow: hidden;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--card-color);
}

.file-browser__tree {
  min-width: 0;
  border-right: 1px solid var(--border-base);
}

.file-browser__content {
  min-width: 0;
  overflow: hidden;
}

.panel-heading,
.code-toolbar {
  display: flex;
  min-height: 48px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-base);
}

.panel-heading > div:first-child,
.selected-file-title {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.panel-heading span,
.panel-heading small,
.code-toolbar {
  color: var(--text-secondary);
  font-size: 12px;
}

.selected-file-title strong,
.file-row span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pager-actions,
.file-badges {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 4px;
}

.file-tree {
  max-height: 650px;
  overflow: auto;
}

.file-row {
  display: grid;
  width: 100%;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border: 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border-base) 70%, transparent);
  background: transparent;
  color: var(--text-primary);
  text-align: left;
  cursor: pointer;
}

.file-row:hover,
.file-row:focus-visible,
.file-row--selected {
  background: var(--hover-color);
}

.file-row--selected {
  box-shadow: inset 3px 0 var(--primary-color);
}

.file-row small {
  color: var(--text-secondary);
  font-size: 11px;
}

.code-panel {
  min-width: 0;
}

.code-lines {
  max-height: 650px;
  overflow: auto;
  background: var(--code-color);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
}

.code-line {
  display: grid;
  min-width: 100%;
  width: max-content;
  grid-template-columns: 64px minmax(max-content, 1fr);
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-primary);
  text-align: left;
  cursor: text;
}

.code-line:hover,
.code-line:focus-visible,
.code-line--selected {
  background: color-mix(in srgb, var(--primary-color) 14%, transparent);
}

.code-line__number {
  position: sticky;
  left: 0;
  padding: 2px 12px 2px 6px;
  border-right: 1px solid var(--border-base);
  background: color-mix(in srgb, var(--code-color) 94%, var(--card-color));
  color: var(--text-secondary);
  text-align: right;
  user-select: none;
}

.code-line code {
  padding: 2px 12px;
  white-space: pre;
}

.panel-empty {
  padding: 52px 12px;
}

.panel-empty--content {
  padding-top: 160px;
}

@media (max-width: 720px) {
  .file-browser {
    grid-template-columns: 1fr;
    min-height: 0;
  }

  .file-browser__tree {
    border-right: 0;
    border-bottom: 1px solid var(--border-base);
  }

  .file-tree {
    max-height: 220px;
  }

  .code-lines {
    max-height: 58vh;
  }
}
</style>
