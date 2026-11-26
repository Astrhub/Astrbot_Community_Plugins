<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { NAlert, NButton, NIcon, NModal, NSlider, NSpin, useMessage } from "naive-ui";
import { CloseOutline } from "@vicons/ionicons5";

const MAX_SOURCE_BYTES = 5 * 1024 * 1024;
const ALLOWED_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const OUTPUT_EDGE = 512;
const PREVIEW_EDGE = 160;
const MIN_SCALE = 1;
const MAX_SCALE = 6;

const props = defineProps<{
  show: boolean;
  file: File | null;
  uploading?: boolean;
}>();

const emit = defineEmits<{
  "update:show": [value: boolean];
  cancel: [];
  confirm: [payload: { file: File; width: number; height: number }];
}>();

const message = useMessage();

const canvasRef = shallowRef<HTMLCanvasElement | null>(null);
const previewRef = shallowRef<HTMLCanvasElement | null>(null);
const image = shallowRef<HTMLImageElement | null>(null);
const loading = shallowRef(false);
const scale = ref(1);
const offset = ref({ x: 0, y: 0 });
const dragging = ref(false);
const dragStart = shallowRef({ x: 0, y: 0, ox: 0, oy: 0 });
const isTouch = shallowRef(false);
let cleanupObjectUrl = "";

const canvasEdge = computed(() => {
  if (typeof window === "undefined") return PREVIEW_EDGE;
  const available = Math.min(window.innerWidth - 64, window.innerHeight - 240, 480);
  return Math.max(240, Math.floor(available));
});

const sourceError = computed(() => {
  const file = props.file;
  if (!file) return "";
  if (file.size > MAX_SOURCE_BYTES) return "图片不能超过 5 MB";
  if (!ALLOWED_TYPES.has(file.type)) return "仅支持 PNG、JPEG 或 WebP 图片";
  return "";
});

function releaseObjectUrl() {
  if (cleanupObjectUrl) URL.revokeObjectURL(cleanupObjectUrl);
  cleanupObjectUrl = "";
}

async function loadImage(file: File) {
  releaseObjectUrl();
  image.value = null;
  loading.value = true;
  const url = URL.createObjectURL(file);
  cleanupObjectUrl = url;
  try {
    const decoded = new Image();
    decoded.decoding = "async";
    await new Promise<void>((resolve, reject) => {
      decoded.onload = () => resolve();
      decoded.onerror = () => reject(new Error("图片解码失败"));
      decoded.src = url;
    });
    image.value = decoded;
    resetTransform();
  } catch (error) {
    message.error(error instanceof Error ? error.message : "图片解码失败");
    emit("update:show", false);
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.show, props.file] as const,
  ([show, file]) => {
    if (show && file && !sourceError.value) void loadImage(file);
    if (!show) releaseObjectUrl();
  },
  { immediate: true },
);

function resetTransform() {
  const img = image.value;
  if (!img) return;
  const edge = Math.min(img.naturalWidth, img.naturalHeight);
  // Fit the short edge of the image to the crop box so the box is always fully covered.
  scale.value = 1;
  offset.value = {
    x: (canvasEdge.value - img.naturalWidth * (canvasEdge.value / edge)) / 2,
    y: (canvasEdge.value - img.naturalHeight * (canvasEdge.value / edge)) / 2,
  };
}

function baseSize() {
  const img = image.value;
  if (!img) return { w: 0, h: 0 };
  const edge = Math.min(img.naturalWidth, img.naturalHeight);
  const factor = canvasEdge.value / edge;
  return { w: img.naturalWidth * factor, h: img.naturalHeight * factor };
}

function clampOffset() {
  const { w, h } = baseSize();
  const edge = canvasEdge.value;
  const minX = edge - w * scale.value;
  const minY = edge - h * scale.value;
  offset.value = {
    x: Math.min(0, Math.max(minX, offset.value.x)),
    y: Math.min(0, Math.max(minY, offset.value.y)),
  };
}

function render() {
  const canvas = canvasRef.value;
  const preview = previewRef.value;
  const img = image.value;
  if (!canvas || !img) return;
  const edge = canvasEdge.value;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(edge * dpr);
  canvas.height = Math.round(edge * dpr);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, edge, edge);
  const { w, h } = baseSize();
  ctx.drawImage(img, offset.value.x, offset.value.y, w * scale.value, h * scale.value);
  ctx.save();
  ctx.globalCompositeOperation = "destination-in";
  ctx.beginPath();
  ctx.arc(edge / 2, edge / 2, edge / 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  if (preview) {
    preview.width = PREVIEW_EDGE * dpr;
    preview.height = PREVIEW_EDGE * dpr;
    const pctx = preview.getContext("2d");
    if (!pctx) return;
    pctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    pctx.clearRect(0, 0, PREVIEW_EDGE, PREVIEW_EDGE);
    pctx.drawImage(canvas, 0, 0, PREVIEW_EDGE, PREVIEW_EDGE);
  }
}

watch([scale, offset, canvasRef, previewRef, image, canvasEdge], () => {
  requestAnimationFrame(render);
});

function setScale(next: number) {
  const edge = canvasEdge.value;
  const previous = scale.value;
  scale.value = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
  // Zoom around the crop box centre so the point under the cursor stays put.
  const ratio = scale.value / previous;
  offset.value = {
    x: edge / 2 - (edge / 2 - offset.value.x) * ratio,
    y: edge / 2 - (edge / 2 - offset.value.y) * ratio,
  };
  clampOffset();
}

function onWheel(event: WheelEvent) {
  event.preventDefault();
  setScale(scale.value * (event.deltaY < 0 ? 1.08 : 1 / 1.08));
}

function beginDrag(event: PointerEvent) {
  if (!image.value) return;
  dragging.value = true;
  isTouch.value = event.pointerType !== "mouse";
  dragStart.value = { x: event.clientX, y: event.clientY, ox: offset.value.x, oy: offset.value.y };
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
}

function moveDrag(event: PointerEvent) {
  if (!dragging.value) return;
  offset.value = {
    x: dragStart.value.ox + (event.clientX - dragStart.value.x),
    y: dragStart.value.oy + (event.clientY - dragStart.value.y),
  };
  clampOffset();
}

function endDrag() {
  dragging.value = false;
}

function exportBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("图片导出失败"))),
      "image/webp",
      0.9,
    );
  });
}

async function confirm() {
  const source = canvasRef.value;
  if (!source) return;
  const out = document.createElement("canvas");
  const edge = Math.min(OUTPUT_EDGE, canvasEdge.value * Math.min(window.devicePixelRatio || 1, 2));
  out.width = edge;
  out.height = edge;
  const ctx = out.getContext("2d");
  if (!ctx) {
    message.error("当前浏览器不支持图片裁剪");
    return;
  }
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(source, 0, 0, edge, edge);
  let blob: Blob;
  let type = "image/webp";
  try {
    blob = await exportBlob(out);
  } catch {
    type = "image/png";
    blob = await new Promise<Blob>((resolve, reject) =>
      out.toBlob(
        (value) => (value ? resolve(value) : reject(new Error("图片导出失败"))),
        "image/png",
      ),
    );
  }
  emit("confirm", {
    file: new File([blob], `avatar.${type === "image/webp" ? "webp" : "png"}`, { type }),
    width: edge,
    height: edge,
  });
}

function close() {
  if (props.uploading) return;
  emit("update:show", false);
  emit("cancel");
}

onBeforeUnmount(releaseObjectUrl);
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    title="调整头像"
    :bordered="false"
    :mask-closable="!uploading"
    :closable="!uploading"
    :close-on-esc="!uploading"
    class="avatar-cropper-modal"
    @update:show="(value: boolean) => emit('update:show', value)"
    @after-leave="releaseObjectUrl"
  >
    <NAlert v-if="sourceError" type="error" :bordered="false">{{ sourceError }}</NAlert>
    <div v-else class="cropper-body">
      <p class="cropper-hint">拖动图片调整位置，滚轮或滑块缩放；头像会裁剪为正方形。</p>
      <div class="cropper-stage">
        <NSpin :show="loading">
          <canvas
            ref="canvasRef"
            class="cropper-canvas"
            :style="{ width: `${canvasEdge}px`, height: `${canvasEdge}px` }"
            :class="{ 'is-dragging': dragging, 'is-touch': isTouch }"
            @pointerdown="beginDrag"
            @pointermove="moveDrag"
            @pointerup="endDrag"
            @pointercancel="endDrag"
            @wheel="onWheel"
          />
        </NSpin>
        <div class="cropper-preview">
          <canvas ref="previewRef" class="cropper-preview__canvas" />
          <span class="cropper-preview__label">预览</span>
          <NSlider
            v-model:value="scale"
            class="cropper-slider"
            :min="MIN_SCALE"
            :max="MAX_SCALE"
            :step="0.01"
            :disabled="loading || uploading"
            :tooltip="false"
            @update:value="clampOffset"
          />
        </div>
      </div>
    </div>
    <template #footer>
      <div class="cropper-actions">
        <NButton :disabled="uploading" @click="close">
          <template #icon>
            <NIcon><CloseOutline /></NIcon>
          </template>
          取消
        </NButton>
        <NButton type="primary" :loading="uploading" :disabled="loading || !image" @click="confirm">
          {{ uploading ? "正在上传…" : "使用这张图片" }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.cropper-body {
  display: grid;
  gap: 12px;
}

.cropper-hint {
  margin: 0;
  color: var(--text-color-3);
  font-size: 12px;
}

.cropper-stage {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 20px;
  justify-content: center;
}

.cropper-canvas {
  display: block;
  touch-action: none;
  cursor: grab;
  user-select: none;
  border-radius: 50%;
  background: var(--body-color);
}

.cropper-canvas.is-dragging {
  cursor: grabbing;
}

.cropper-preview {
  display: grid;
  justify-items: center;
  gap: 8px;
  width: 180px;
}

.cropper-preview__canvas {
  display: block;
  width: 160px;
  height: 160px;
  border-radius: 50%;
  background: var(--body-color);
}

.cropper-preview__label {
  color: var(--text-color-3);
  font-size: 12px;
}

.cropper-slider {
  width: 100%;
}

.cropper-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>

<!-- The modal is teleported to <body>, so its shell has to be styled globally. -->
<style>
.avatar-cropper-modal {
  width: min(480px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  display: flex;
  flex-direction: column;
}

.avatar-cropper-modal > .n-card__content {
  overflow-y: auto;
  min-height: 0;
}

@media (max-width: 560px) {
  .avatar-cropper-modal > .n-card__content {
    padding: 12px 14px;
  }
}
</style>
