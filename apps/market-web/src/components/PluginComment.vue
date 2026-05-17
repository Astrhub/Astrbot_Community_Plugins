<template>
  <section class="plugin-comments">
    <div class="comments-header">
      <h3>评价</h3>
      <span>{{ comments.length }} 条</span>
    </div>

    <n-alert v-if="!commentsEnabled" type="warning" :bordered="false">
      当前站点已关闭评论。
    </n-alert>

    <div v-else class="comment-editor">
      <n-input
        v-model:value="draft"
        type="textarea"
        :autosize="{ minRows: 3, maxRows: 6 }"
        placeholder="写下你的评价"
      />
      <div class="editor-actions">
        <n-button type="primary" :loading="submitting" @click="submitComment(null)">
          发布评价
        </n-button>
      </div>
    </div>

    <n-empty v-if="comments.length === 0" description="还没有评价" />
    <div v-else class="comment-list">
      <article v-for="comment in rootComments" :key="comment.id" class="comment-item">
        <div class="comment-body">
          <div class="comment-meta">
            <div class="comment-user">
              <span class="comment-floor">#{{ comment.floor || '-' }}</span>
              <span>{{ displayUser(comment) }}</span>
              <span v-if="comment.is_admin" class="comment-badge comment-badge--admin">管理</span>
              <span v-if="comment.is_plugin_author" class="comment-badge comment-badge--author">作者</span>
            </div>
            <time>{{ formatTime(comment.created_at) }}</time>
          </div>
          <p>{{ comment.body }}</p>
          <div class="comment-actions">
            <n-button v-if="likesEnabled" text size="small" @click="toggleLike(comment)">
              {{ comment.liked ? '取消点赞' : '点赞' }} {{ comment.likes || 0 }}
            </n-button>
            <n-button text size="small" :disabled="!commentsEnabled" @click="toggleReply(comment.id)">
              回复
            </n-button>
            <n-button v-if="comment.can_delete" text size="small" type="error" @click="deleteComment(comment)">
              删除
            </n-button>
          </div>
        </div>

        <div v-if="replyingTo === comment.id" class="reply-editor">
          <n-input
            v-model:value="replyDraft"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 4 }"
            placeholder="回复这条评价"
          />
          <div class="editor-actions">
            <n-button tertiary @click="cancelReply">取消</n-button>
            <n-button type="primary" :loading="submitting" @click="submitComment(comment.id)">
              回复
            </n-button>
          </div>
        </div>

        <div v-if="repliesByParent[comment.id]?.length" class="reply-list">
          <article v-for="reply in repliesByParent[comment.id]" :key="reply.id" class="reply-item">
            <div class="comment-meta">
              <div class="comment-user">
                <span class="comment-floor">#{{ reply.floor || '-' }}</span>
                <span>{{ displayUser(reply) }}</span>
                <span v-if="reply.is_admin" class="comment-badge comment-badge--admin">管理</span>
                <span v-if="reply.is_plugin_author" class="comment-badge comment-badge--author">作者</span>
              </div>
              <time>{{ formatTime(reply.created_at) }}</time>
            </div>
            <p>{{ reply.body }}</p>
            <div class="comment-actions">
              <n-button v-if="likesEnabled" text size="small" @click="toggleLike(reply)">
                {{ reply.liked ? '取消点赞' : '点赞' }} {{ reply.likes || 0 }}
              </n-button>
              <n-button v-if="reply.can_delete" text size="small" type="error" @click="deleteComment(reply)">
                删除
              </n-button>
            </div>
          </article>
        </div>
      </article>
    </div>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import { NAlert, NButton, NEmpty, NInput, useMessage } from 'naive-ui'

const props = defineProps({
  comments: {
    type: Array,
    default: () => []
  },
  commentsEnabled: {
    type: Boolean,
    default: true
  },
  likesEnabled: {
    type: Boolean,
    default: true
  }
})

const emit = defineEmits(['submit', 'delete', 'like'])
const message = useMessage()
const draft = ref('')
const replyDraft = ref('')
const replyingTo = ref('')
const submitting = ref(false)

const rootComments = computed(() => props.comments.filter((comment) => !comment.parent_id))
const repliesByParent = computed(() => props.comments.reduce((groups, comment) => {
  if (comment.parent_id) {
    groups[comment.parent_id] ||= []
    groups[comment.parent_id].push(comment)
  }
  return groups
}, {}))

function displayUser(comment) {
  return comment.github_login || comment.github_name || comment.user_id || '用户'
}

function formatTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString()
}

function toggleReply(commentId) {
  replyingTo.value = replyingTo.value === commentId ? '' : commentId
  replyDraft.value = ''
}

function cancelReply() {
  replyingTo.value = ''
  replyDraft.value = ''
}

function toggleLike(comment) {
  emit('like', {
    comment,
    liked: !comment.liked
  })
}

function deleteComment(comment) {
  emit('delete', comment)
}

async function submitComment(parentId) {
  const body = (parentId ? replyDraft.value : draft.value).trim()
  if (!body) {
    message.warning('请输入内容')
    return
  }
  submitting.value = true
  emit('submit', {
    body,
    parent_id: parentId,
    done: () => {
      if (parentId) {
        cancelReply()
      } else {
        draft.value = ''
      }
      submitting.value = false
    },
    fail: () => {
      submitting.value = false
    }
  })
  setTimeout(() => {
    submitting.value = false
  }, 10000)
}
</script>

<style scoped>
.plugin-comments {
  display: grid;
  gap: 16px;
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--n-border-color);
}

.comments-header,
.comment-meta,
.comment-user,
.comment-actions,
.editor-actions {
  display: flex;
  align-items: center;
}

.comments-header {
  justify-content: space-between;
}

.comments-header h3,
.comment-body p,
.reply-item p {
  margin: 0;
}

.comments-header span,
.comment-meta time {
  color: var(--n-text-color-3);
  font-size: 13px;
}

.comment-editor,
.reply-editor,
.comment-list {
  display: grid;
  gap: 12px;
}

.editor-actions {
  justify-content: flex-end;
  gap: 10px;
}

.comment-item,
.reply-item {
  padding: 14px;
  background: var(--n-color);
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
}

.comment-body {
  display: grid;
  gap: 8px;
}

.comment-actions {
  flex-wrap: wrap;
  gap: 10px;
}

.comment-meta {
  justify-content: space-between;
  gap: 12px;
  color: var(--n-text-color);
  font-weight: 600;
}

.comment-user {
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.comment-floor {
  color: var(--n-text-color-3);
  font-weight: 500;
}

.comment-badge {
  border: 1px solid currentColor;
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
}

.comment-badge--admin {
  color: #2563eb;
  background: rgba(37, 99, 235, 0.08);
}

.comment-badge--author {
  color: #16a34a;
  background: rgba(22, 163, 74, 0.08);
}

.reply-list {
  display: grid;
  gap: 10px;
  margin-top: 12px;
  padding-left: 18px;
  border-left: 2px solid var(--n-border-color);
}

.reply-editor {
  margin-top: 12px;
}
</style>
