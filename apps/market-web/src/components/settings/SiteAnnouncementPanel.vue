<template>
  <section class="settings-section">
    <div class="section-title">
      <h2>站点公告</h2>
      <p>发布、编辑或删除市场首页公告，正文支持安全 Markdown。</p>
    </div>

    <n-spin :show="loading">
      <div class="announcement-form">
        <n-form-item :label="editingId === null ? '公告标题' : '编辑公告标题'">
          <n-input
            v-model:value="form.title"
            :maxlength="80"
            show-count
            placeholder="例如：维护通知"
          />
        </n-form-item>
        <n-form-item label="公告内容">
          <n-input
            v-model:value="form.body"
            type="textarea"
            :maxlength="10000"
            show-count
            :autosize="{ minRows: 5, maxRows: 12 }"
            placeholder="输入公告内容，支持 Markdown 和图片"
          />
        </n-form-item>
      </div>

      <n-form-item label="图片描述（替代文本）">
        <n-input
          v-model:value="imageAlt"
          :maxlength="100"
          show-count
          placeholder="例如：11 月维护时间安排截图"
        />
      </n-form-item>
      <div class="image-tools">
        <n-button :loading="uploading" @click="openImagePicker">
          <template #icon
            ><n-icon><ImageOutline /></n-icon
          ></template>
          上传图片
        </n-button>
        <n-input
          v-model:value="imageUrl"
          class="image-url-input"
          placeholder="或输入 HTTPS 图片链接"
          @keyup.enter="insertExternalImage"
        />
        <n-button @click="insertExternalImage">
          <template #icon
            ><n-icon><LinkOutline /></n-icon
          ></template>
          插入链接
        </n-button>
      </div>
      <input
        ref="imageFileInput"
        class="visually-hidden"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp,image/avif"
        @change="uploadSelectedImage"
      />
      <p class="form-hint">
        支持 PNG、JPEG、GIF、WebP、AVIF，单张不超过 5 MB。删除公告不会删除已上传图片。
      </p>

      <div class="announcement-actions">
        <n-button type="primary" :loading="saving" @click="saveAnnouncement">
          {{ editingId === null ? "发布公告" : "保存修改" }}
        </n-button>
        <n-button v-if="editingId !== null" @click="resetForm">取消编辑</n-button>
        <n-button quaternary @click="showPreview = !showPreview">
          {{ showPreview ? "隐藏预览" : "预览" }}
        </n-button>
      </div>

      <div v-if="showPreview" class="announcement-preview">
        <h3>预览</h3>
        <AnnouncementBody v-if="form.body" :body="form.body" />
        <p v-else class="form-hint">暂无内容</p>
      </div>

      <n-divider />

      <div class="announcement-list-header">
        <h3>已发布公告</h3>
        <span>{{ announcements.length }} 条</span>
      </div>
      <n-empty v-if="!announcements.length" description="暂无公告" />
      <div v-else class="announcement-list">
        <article
          v-for="announcement in announcements"
          :key="announcement.id"
          class="announcement-item"
        >
          <div class="announcement-item-header">
            <div>
              <h3>{{ announcement.title }}</h3>
              <time
                v-if="announcement.updated_at || announcement.created_at"
                :datetime="announcement.updated_at || announcement.created_at"
              >
                {{ isEdited(announcement) ? "编辑于" : "发布于" }}
                {{ formatTime(announcement.updated_at || announcement.created_at || "") }}
              </time>
            </div>
            <div class="announcement-item-actions">
              <n-button size="small" @click="startEdit(announcement)">
                <template #icon
                  ><n-icon><CreateOutline /></n-icon
                ></template>
                编辑
              </n-button>
              <n-button size="small" type="error" tertiary @click="confirmDelete(announcement)">
                <template #icon
                  ><n-icon><TrashOutline /></n-icon
                ></template>
                删除
              </n-button>
            </div>
          </div>
          <AnnouncementBody v-if="announcement.body" :body="announcement.body" />
        </article>
      </div>
    </n-spin>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, shallowRef, useTemplateRef } from "vue";
import { storeToRefs } from "pinia";
import {
  NButton,
  NDivider,
  NEmpty,
  NFormItem,
  NIcon,
  NInput,
  NSpin,
  useDialog,
  useMessage,
} from "naive-ui";
import { CreateOutline, ImageOutline, LinkOutline, TrashOutline } from "@vicons/ionicons5";
import AnnouncementBody from "@/components/AnnouncementBody.vue";
import { usePluginStore } from "@/stores/plugins";
import type { Announcement } from "@/types";
import {
  hasOnlyDescriptiveImages,
  isDescriptiveImageAlt,
  normalizeImageAlt,
} from "@/utils/announcement";

const store = usePluginStore();
const { announcements } = storeToRefs(store);
const message = useMessage();
const dialog = useDialog();

const form = reactive({ title: "", body: "" });
const editingId = shallowRef<Announcement["id"] | null>(null);
const editingUpdatedAt = shallowRef<string>();
const imageUrl = shallowRef("");
const imageAlt = shallowRef("");
const loading = shallowRef(false);
const saving = shallowRef(false);
const uploading = shallowRef(false);
const showPreview = shallowRef(false);
const imageFileInput = useTemplateRef<HTMLInputElement>("imageFileInput");

function isEdited(announcement: Announcement): boolean {
  return Boolean(announcement.updated_at && announcement.updated_at !== announcement.created_at);
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN");
}

function resetForm(): void {
  editingId.value = null;
  editingUpdatedAt.value = undefined;
  form.title = "";
  form.body = "";
  imageUrl.value = "";
  imageAlt.value = "";
  showPreview.value = false;
}

function startEdit(announcement: Announcement): void {
  editingId.value = announcement.id;
  editingUpdatedAt.value = announcement.updated_at || announcement.created_at;
  form.title = announcement.title;
  form.body = announcement.body || "";
  showPreview.value = false;
}

async function saveAnnouncement(): Promise<void> {
  if (store.currentUser?.role !== "core_admin") {
    message.warning("只有核心管理员可以管理公告");
    return;
  }
  const title = form.title.trim();
  const body = form.body.trim();
  if (!title || !body) {
    message.warning("请填写公告标题和内容");
    return;
  }
  if (!hasOnlyDescriptiveImages(body)) {
    message.warning("请为每张图片填写有描述性的替代文本");
    return;
  }

  saving.value = true;
  try {
    if (editingId.value === null) {
      await store.publishAnnouncement({ title, body });
      message.success("公告已发布");
    } else {
      await store.updateAnnouncement(editingId.value, {
        title,
        body,
        expected_updated_at: editingUpdatedAt.value,
      });
      message.success("公告已更新");
    }
    resetForm();
  } catch (error) {
    message.error(error instanceof Error ? error.message : "保存公告失败");
  } finally {
    saving.value = false;
  }
}

function validateImageAlt(value: string): boolean {
  if (isDescriptiveImageAlt(value)) return true;
  message.warning("请先填写有描述性的图片替代文本");
  return false;
}

function openImagePicker(): void {
  if (validateImageAlt(imageAlt.value)) imageFileInput.value?.click();
}

function appendImage(url: string, altValue: string): boolean {
  const alt = normalizeImageAlt(altValue);
  if (!validateImageAlt(alt)) return false;
  const markdown = `![${alt}](${url})`;
  form.body = form.body.trim() ? `${form.body.trimEnd()}\n\n${markdown}` : markdown;
  showPreview.value = true;
  if (normalizeImageAlt(imageAlt.value) === alt) imageAlt.value = "";
  return true;
}

function insertExternalImage(): void {
  const alt = imageAlt.value;
  if (!validateImageAlt(alt)) return;
  try {
    const parsed = new URL(imageUrl.value.trim());
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
      message.warning("图片链接必须是无账号信息的 HTTPS 地址");
      return;
    }
    const safeUrl = parsed.href.replaceAll("(", "%28").replaceAll(")", "%29");
    if (appendImage(safeUrl, alt)) imageUrl.value = "";
  } catch {
    message.warning("请输入有效的 HTTPS 图片链接");
  }
}

async function uploadSelectedImage(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const alt = imageAlt.value;
  if (!validateImageAlt(alt)) {
    input.value = "";
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    message.warning("图片不能超过 5 MB");
    input.value = "";
    return;
  }

  uploading.value = true;
  try {
    const uploaded = await store.uploadAnnouncementImage(file);
    appendImage(uploaded.url, alt);
    message.success("图片已上传并插入");
  } catch (error) {
    message.error(error instanceof Error ? error.message : "图片上传失败");
  } finally {
    uploading.value = false;
    input.value = "";
  }
}

function confirmDelete(announcement: Announcement): void {
  dialog.warning({
    title: "删除公告",
    content: `确定删除“${announcement.title}”吗？此操作不可撤销。`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      try {
        await store.deleteAnnouncement(announcement.id);
        if (editingId.value === announcement.id) resetForm();
        message.success("公告已删除");
      } catch (error) {
        message.error(error instanceof Error ? error.message : "删除公告失败");
        return false;
      }
    },
  });
}

onMounted(async () => {
  loading.value = true;
  try {
    await store.loadAnnouncements({ force: true });
  } catch (error) {
    message.error(error instanceof Error ? error.message : "加载公告失败");
  } finally {
    loading.value = false;
  }
});
</script>

<style scoped>
.settings-section {
  padding: 24px 0;
}

.section-title {
  margin-bottom: 20px;
}

.section-title h2,
.announcement-list-header h3,
.announcement-item h3,
.announcement-preview h3 {
  margin: 0;
  color: var(--text-primary);
}

.section-title p,
.form-hint,
.announcement-item time {
  color: var(--text-secondary);
}

.section-title p {
  margin: 8px 0 0;
}

.announcement-form {
  display: grid;
  gap: 4px;
}

.image-tools,
.announcement-actions,
.announcement-item-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.image-url-input {
  flex: 1;
  min-width: 180px;
}

.form-hint {
  margin: 10px 0 16px;
  font-size: 12px;
}

.announcement-actions {
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.announcement-preview {
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 12px;
}

.announcement-list-header,
.announcement-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.announcement-list-header {
  margin-bottom: 16px;
}

.announcement-list-header span,
.announcement-item time {
  font-size: 12px;
  color: var(--text-secondary);
}

.announcement-list {
  display: grid;
  gap: 16px;
}

.announcement-item {
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 12px;
}

.announcement-item time {
  display: block;
  margin-top: 6px;
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 640px) {
  .image-tools,
  .announcement-item-header {
    align-items: stretch;
    flex-direction: column;
  }

  .image-url-input {
    width: 100%;
  }
}
</style>
