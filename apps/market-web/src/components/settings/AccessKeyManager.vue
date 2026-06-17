<script setup>
import { computed, reactive } from 'vue'
import {
  NAlert,
  NButton,
  NCheckbox,
  NCheckboxGroup,
  NEmpty,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NSpace,
  NSpin,
  NTag
} from 'naive-ui'
import {
  CopyOutline,
  KeyOutline,
  RefreshOutline,
  ShieldCheckmarkOutline,
  TrashOutline
} from '@vicons/ionicons5'

const props = defineProps({
  keys: {
    type: Array,
    default: () => []
  },
  loading: {
    type: Boolean,
    default: false
  },
  creating: {
    type: Boolean,
    default: false
  },
  busyIds: {
    type: Object,
    default: () => ({})
  },
  newKey: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['refresh', 'create', 'delete', 'copy-key', 'clear-new-key'])

const form = reactive({
  name: 'AstrBot 插件访问密钥',
  scopes: ['market:read']
})

const scopeOptions = Object.freeze([
  {
    label: '读取市场数据',
    value: 'market:read',
    description: '读取插件、评论、公告和公开市场元数据'
  },
  {
    label: '写入市场数据',
    value: 'market:write',
    description: '为后续插件提交、同步和管理接口预留'
  }
])

const canCreate = computed(() => form.scopes.length > 0 && !props.creating)

function createKey() {
  emit('create', {
    name: form.name.trim() || 'AstrBot 插件访问密钥',
    scopes: [...form.scopes]
  })
}

function scopeLabel(scope) {
  return scopeOptions.find((option) => option.value === scope)?.label || scope
}

function formatTime(value) {
  if (!value) return '未知时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未知时间'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date)
}

function isBusy(key) {
  return Boolean(props.busyIds?.[key.id])
}
</script>

<template>
  <section class="settings-section">
    <div class="section-heading">
      <div class="section-icon">
        <NIcon><KeyOutline /></NIcon>
      </div>
      <div class="section-copy">
        <p class="section-kicker">OpenAPI 凭证</p>
        <h2 class="section-title">访问密钥</h2>
      </div>
      <NButton tertiary :loading="loading" class="refresh-button" @click="emit('refresh')">
        <template #icon>
          <NIcon><RefreshOutline /></NIcon>
        </template>
        刷新
      </NButton>
    </div>

    <NAlert
      v-if="newKey"
      type="success"
      title="访问密钥已生成"
      closable
      class="new-key-alert"
      @close="emit('clear-new-key')"
    >
      <div class="new-key-content">
        <code>{{ newKey }}</code>
        <NButton secondary type="primary" @click="emit('copy-key', newKey)">
          <template #icon>
            <NIcon><CopyOutline /></NIcon>
          </template>
          复制
        </NButton>
      </div>
    </NAlert>

    <NForm label-placement="top" class="key-form">
      <NFormItem label="密钥名称">
        <NInput v-model:value="form.name" placeholder="例如：插件同步脚本" />
      </NFormItem>

      <NFormItem label="权限">
        <NCheckboxGroup v-model:value="form.scopes" class="scope-group">
          <NCheckbox
            v-for="scope in scopeOptions"
            :key="scope.value"
            :value="scope.value"
            class="scope-option"
          >
            <div class="scope-copy">
              <strong>{{ scope.label }}</strong>
              <span>{{ scope.description }}</span>
            </div>
          </NCheckbox>
        </NCheckboxGroup>
      </NFormItem>

      <div class="section-actions">
        <NButton type="primary" :loading="creating" :disabled="!canCreate" @click="createKey">
          <template #icon>
            <NIcon><ShieldCheckmarkOutline /></NIcon>
          </template>
          生成访问密钥
        </NButton>
      </div>
    </NForm>

    <div class="key-list-heading">
      <h3>已创建密钥</h3>
    </div>

    <NSpin :show="loading">
      <NEmpty v-if="keys.length === 0" description="暂无访问密钥" />

      <div v-else class="key-list">
        <article v-for="key in keys" :key="key.id" class="key-row">
          <div class="key-main">
            <div class="key-title-row">
              <strong>{{ key.name || '未命名密钥' }}</strong>
              <span>{{ formatTime(key.created_at) }}</span>
            </div>
            <NSpace :size="6" class="scope-tags">
              <NTag v-for="scope in key.scopes || []" :key="scope" size="small" round>
                {{ scopeLabel(scope) }}
              </NTag>
            </NSpace>
          </div>

          <NButton
            secondary
            type="error"
            :loading="isBusy(key)"
            @click="emit('delete', key)"
          >
            <template #icon>
              <NIcon><TrashOutline /></NIcon>
            </template>
            删除
          </NButton>
        </article>
      </div>
    </NSpin>
  </section>
</template>

<style scoped>
.settings-section {
  padding: 22px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--card-color);
}

.section-heading,
.section-actions,
.new-key-content,
.key-title-row {
  display: flex;
  align-items: center;
}

.section-heading {
  gap: 12px;
  margin-bottom: 18px;
}

.section-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  color: #0e74e4;
  background: rgba(14, 116, 228, 0.12);
  border-radius: 8px;
}

.section-copy {
  min-width: 0;
  flex: 1;
}

.section-kicker,
.section-title,
.key-list-heading h3 {
  margin: 0;
}

.section-kicker {
  color: var(--text-color-3);
  font-size: 12px;
}

.section-title {
  font-size: 18px;
  line-height: 1.3;
}

.refresh-button {
  flex: none;
}

.new-key-alert {
  margin-bottom: 18px;
}

.new-key-content {
  gap: 10px;
  flex-wrap: wrap;
}

.new-key-content code {
  min-width: 0;
  flex: 1;
  padding: 8px 10px;
  overflow-wrap: anywhere;
  color: var(--text-color-base);
  background: var(--body-color);
  border: 1px solid var(--border-color);
  border-radius: 8px;
}

.key-form {
  margin-bottom: 18px;
}

.scope-group {
  display: grid;
  gap: 10px;
  width: 100%;
}

.scope-option {
  width: 100%;
  align-items: flex-start;
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.scope-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.scope-copy span {
  color: var(--text-color-3);
  font-size: 12px;
}

.section-actions {
  justify-content: flex-end;
}

.key-list-heading {
  margin: 6px 0 12px;
}

.key-list-heading h3 {
  font-size: 15px;
}

.key-list {
  display: grid;
  gap: 12px;
}

.key-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: center;
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.key-main {
  min-width: 0;
  display: grid;
  gap: 8px;
}

.key-title-row {
  gap: 8px;
  flex-wrap: wrap;
}

.key-title-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 15px;
}

.key-title-row span {
  color: var(--text-color-3);
  font-size: 12px;
}

.scope-tags {
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .section-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .refresh-button,
  .section-actions :deep(.n-button),
  .new-key-content :deep(.n-button) {
    width: 100%;
  }

  .key-row {
    grid-template-columns: 1fr;
  }

  .key-row :deep(.n-button) {
    width: 100%;
  }
}
</style>
