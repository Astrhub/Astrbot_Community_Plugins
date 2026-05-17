<template>
  <n-modal
    v-model:show="show"
    :mask-closable="true"
    preset="card"
    class="plugin-details"
    style="max-width: 900px; width: 90%"
    :bordered="false"
  >
    <template #header>
      <div class="plugin-details__header">
        <n-h2 class="plugin-details__title">
          <n-space align="center" :size="12">
            <n-icon size="24">
              <extension-puzzle-outline />
            </n-icon>
            {{ plugin?.name }}
            <n-tag type="success" size="small" :bordered="false">
              {{ plugin?.version?.startsWith('v') ? plugin?.version : 'v' + plugin?.version }}
            </n-tag>
          </n-space>
        </n-h2>
      </div>
    </template>

    <div class="plugin-details__content">
      <n-space vertical size="large">
        <div class="plugin-actions">
          <n-button
            v-if="likesEnabled"
            secondary
            type="primary"
            :loading="liking"
            @click="toggleLike"
          >
            <template #icon>
              <n-icon><heart-outline /></n-icon>
            </template>
            {{ liked ? '取消点赞' : '点赞' }} {{ detail?.likes ?? plugin?.likes ?? 0 }}
          </n-button>
          <n-button
            v-if="canManagePlugin"
            secondary
            :loading="refreshing"
            @click="openRefreshModal"
          >
            刷新 GitHub
          </n-button>
          <span v-if="!likesEnabled" class="muted-text">点赞已关闭</span>
        </div>

        <div v-if="loading" class="readme-loading">
          <n-spin size="medium">
            <template #description>
              正在加载 README...
            </template>
          </n-spin>
        </div>
        <div v-else-if="error" class="readme-error">
          <n-empty description="加载 README 失败">
            <template #extra>
              <n-button size="small" @click="fetchReadme">
                重试
              </n-button>
            </template>
          </n-empty>
        </div>
        <div v-else class="markdown-content" v-html="readmeHtml"></div>

        <plugin-comment
          :comments="comments"
          :comments-enabled="commentsEnabled"
          @submit="submitComment"
        />
      </n-space>
    </div>

    <template #footer>
      <div class="plugin-details__footer">
        <n-space justify="end" :size="12">
          <n-button
            secondary
            type="primary"
            @click="openUrl(plugin?.repo)"
          >
            <template #icon>
              <n-icon><logo-github /></n-icon>
            </template>
            查看仓库
          </n-button>
          <n-button
            type="primary"
            @click="show = false"
          >
            关闭
          </n-button>
        </n-space>
      </div>
    </template>
  </n-modal>

  <n-modal
    v-model:show="showRefreshModal"
    preset="card"
    title="刷新 GitHub 元数据"
    style="max-width: 520px"
  >
    <n-space vertical size="medium">
      <n-alert type="info" :bordered="false">
        Token 只需要公开仓库读取权限。留空会使用已保存的个人 Token 或站点兜底 Token。
      </n-alert>
      <n-form label-placement="top">
        <n-form-item label="临时 GitHub Token" path="github_token">
          <n-input
            v-model:value="refreshForm.github_token"
            type="password"
            show-password-on="click"
            placeholder="可选，ghp_... 或 fine-grained token"
          />
        </n-form-item>
        <n-checkbox v-model:checked="refreshForm.save_token">
          保存到个人设置，后续自动同步优先使用
        </n-checkbox>
        <n-form-item label="刷新间隔（秒）" path="refresh_interval_seconds">
          <n-input-number
            v-model:value="refreshForm.refresh_interval_seconds"
            :min="300"
            :max="86400"
            :step="300"
          />
        </n-form-item>
      </n-form>
    </n-space>
    <template #footer>
      <div class="refresh-actions">
        <n-button tertiary @click="showRefreshModal = false">取消</n-button>
        <n-button type="primary" :loading="refreshing" @click="confirmRefreshGithub">
          刷新
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { marked } from 'marked'
import {
  NModal,
  NSpace,
  NH2,
  NIcon,
  NTag,
  NButton,
  NAlert,
  NCheckbox,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSpin,
  NEmpty,
  useMessage
} from 'naive-ui'
import { storeToRefs } from 'pinia'
import { usePluginStore } from '../stores/plugins'
import PluginComment from './PluginComment.vue'
import {
  ExtensionPuzzleOutline,
  HeartOutline,
  LogoGithub
} from '@vicons/ionicons5'

const props = defineProps({
  show: Boolean,
  plugin: Object
})

const emit = defineEmits(['update:show'])

const show = ref(props.show)
const loading = ref(false)
const error = ref(false)
const readmeHtml = ref('')
const detail = ref(null)
const liking = ref(false)
const liked = ref(false)
const refreshing = ref(false)
const showRefreshModal = ref(false)
const refreshForm = ref({
  github_token: '',
  save_token: false,
  refresh_interval_seconds: 3600
})
const comments = computed(() => detail.value?.comments || [])

const store = usePluginStore()
const message = useMessage()
const { siteConfig, currentUser } = storeToRefs(store)
const {
  addPluginComment,
  likePlugin,
  loadPluginDetail,
  loadPlugins,
  loadCurrentUser,
  refreshPluginGithubMetadata,
  unlikePlugin
} = store
const commentsEnabled = computed(() => Boolean(siteConfig.value.market?.comments_enabled))
const likesEnabled = computed(() => Boolean(siteConfig.value.market?.likes_enabled))
const activePlugin = computed(() => detail.value || props.plugin || {})
const canManagePlugin = computed(() => {
  const user = currentUser.value
  const plugin = activePlugin.value
  if (!user || !plugin?.id) return false
  if (['core_admin', 'admin'].includes(user.role)) return true
  return Boolean(
    plugin.owner_user_id === user.id ||
    (plugin.owner_github_login && plugin.owner_github_login === user.github_login)
  )
})

watch(() => props.show, (newVal) => {
  show.value = newVal
})

watch(show, (newVal) => {
  emit('update:show', newVal)
  if (newVal) {
    loadCurrentUser()
    fetchDetail()
    fetchReadme()
  }
})

const openUrl = (url) => {
  if (url) {
    window.open(url, '_blank')
  }
}

async function fetchReadme() {
  if (!props.plugin?.repo) return
  
  loading.value = true
  error.value = false
  
  try {
    const [owner, repo] = props.plugin.repo.split('/').slice(-2)

    let readmeText = ''

    try {
      const apiResp = await fetch(`https://api.github.com/repos/${owner}/${repo}/readme`, {
        method: 'GET',
        headers: {
          'Accept': 'application/vnd.github.v3.raw'
        },
        timeout: 10000
      })

      if (apiResp.ok) {
        readmeText = await apiResp.text()
      } else {
        throw new Error(`GitHub API /readme returned ${apiResp.status}`)
      }
    } catch (apiErr) {
      const branches = ['main', 'master']
      const candidates = ['README.md', 'Readme.md', 'readme.md', 'README.MD', 'README']

      let found = false
      for (const branch of branches) {
        for (const filename of candidates) {
          try {
            const resp = await fetch(`https://raw.githubusercontent.com/${owner}/${repo}/${branch}/${filename}`, {
              method: 'GET',
              headers: {
                'Accept': 'text/plain'
              },
              timeout: 10000
            })
            if (resp.ok) {
              readmeText = await resp.text()
              found = true
              break
            }
          } catch (_) {
            // 忽略，尝试下一个候选
          }
        }
        if (found) break
      }

      if (!found) {
        throw new Error('无法获取 README（API 与镜像均失败）')
      }
    }

    if (!readmeText) {
      throw new Error('README 内容为空')
    }

    readmeHtml.value = marked(readmeText)
  } catch (err) {
    console.error('Error fetching README:', err)
    error.value = true
  } finally {
    loading.value = false
  }
}

async function fetchDetail() {
  if (!props.plugin?.id) return
  try {
    detail.value = await loadPluginDetail(props.plugin.id)
  } catch (err) {
    message.error(err.message || '加载互动信息失败')
  }
}

async function toggleLike() {
  if (!props.plugin?.id) return
  liking.value = true
  try {
    detail.value = liked.value
      ? await unlikePlugin(props.plugin.id)
      : await likePlugin(props.plugin.id)
    liked.value = !liked.value
    await loadPlugins()
  } catch (err) {
    message.error(err.message || '操作失败')
  } finally {
    liking.value = false
  }
}

function openRefreshModal() {
  refreshForm.value = {
    github_token: '',
    save_token: false,
    refresh_interval_seconds: currentUser.value?.github_refresh_interval_seconds || 3600
  }
  showRefreshModal.value = true
}

async function confirmRefreshGithub() {
  if (!props.plugin?.id) return
  refreshing.value = true
  try {
    const payload = {
      github_token: refreshForm.value.github_token.trim(),
      save_token: refreshForm.value.save_token,
      refresh_interval_seconds: Number(refreshForm.value.refresh_interval_seconds || 3600)
    }
    detail.value = await refreshPluginGithubMetadata(props.plugin.id, payload)
    await loadCurrentUser()
    await loadPlugins()
    showRefreshModal.value = false
    message.success('GitHub 数据已刷新')
  } catch (err) {
    message.error(err.message || '刷新失败，请填写只读 GitHub Token 后重试')
  } finally {
    refreshing.value = false
  }
}

async function submitComment(payload) {
  if (!props.plugin?.id) return
  try {
    await addPluginComment(props.plugin.id, payload)
    await fetchDetail()
    await loadPlugins()
    message.success(payload.parent_id ? '回复已发布' : '评价已发布')
    payload.done?.()
  } catch (err) {
    message.error(err.message || '发布失败')
    payload.fail?.()
  }
}
</script>

<style scoped>
.plugin-details {
  --modal-padding: 24px !important;
}

.plugin-details :deep(.n-modal) {
  max-height: 90vh !important;
}

.plugin-details__header {
  padding: 0 var(--modal-padding);
  margin: calc(-1 * var(--modal-padding)) calc(-1 * var(--modal-padding)) 0;
  padding-top: var(--modal-padding);
  padding-bottom: 16px;
  border-bottom: 1px solid var(--n-border-color);
}

.plugin-details__title {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.plugin-details__content {
  padding: var(--modal-padding) 0;
  padding-right: 16px;
  margin-right: 4px;
  overflow-y: auto;
  max-height: calc(80vh - 180px);
}

.plugin-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
}

.muted-text {
  color: var(--n-text-color-3);
  font-size: 13px;
}

.markdown-content {
  color: var(--n-text-color-2);
  line-height: 1.6;
}

.markdown-content :deep(h1),
.markdown-content :deep(h2),
.markdown-content :deep(h3),
.markdown-content :deep(h4),
.markdown-content :deep(h5),
.markdown-content :deep(h6) {
  margin: 1.5em 0 0.5em;
  color: var(--n-text-color);
}

.markdown-content :deep(h1:first-child),
.markdown-content :deep(h2:first-child),
.markdown-content :deep(h3:first-child) {
  margin-top: 0;
}

.markdown-content :deep(p) {
  margin: 1em 0;
}

.markdown-content :deep(img) {
  max-width: 100%;
  border-radius: 8px;
}

.markdown-content :deep(code) {
  background: var(--n-code-color);
  padding: 0.2em 0.4em;
  border-radius: 3px;
  font-size: 0.9em;
  font-family: monospace;
}

.markdown-content :deep(pre) {
  background: var(--n-code-color);
  padding: 16px;
  border-radius: 8px;
  overflow-x: auto;
}

.markdown-content :deep(pre code) {
  background: none;
  padding: 0;
  border-radius: 0;
}

.markdown-content :deep(blockquote) {
  margin: 1em 0;
  padding-left: 1em;
  border-left: 4px solid var(--n-border-color);
  color: var(--n-text-color-3);
}

.markdown-content :deep(ul),
.markdown-content :deep(ol) {
  padding-left: 2em;
  margin: 1em 0;
}

.markdown-content :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 1em 0;
}

.markdown-content :deep(th),
.markdown-content :deep(td) {
  border: 1px solid var(--n-border-color);
  padding: 8px;
  text-align: left;
}

.markdown-content :deep(th) {
  background: var(--n-color-hover);
}

.plugin-details__footer {
  padding: var(--modal-padding);
  margin: 0 calc(-1 * var(--modal-padding));
  margin-top: calc(-1 * var(--modal-padding));
  border-top: 0px solid var(--n-border-color);
}

.readme-loading,
.readme-error {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 200px;
}

.refresh-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* 响应式设计 */
@media (max-width: 768px) {
  .plugin-details__content {
    max-height: calc(70vh - 140px);
  }
}

@media (max-width: 480px) {
  .plugin-details {
    --modal-padding: 16px;
  }
  
  .plugin-details__title {
    font-size: 1.2em;
  }
}
</style>
