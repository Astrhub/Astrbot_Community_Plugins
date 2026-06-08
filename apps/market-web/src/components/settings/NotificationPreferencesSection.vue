<script setup>
import {
  NButton,
  NIcon,
  NSwitch
} from 'naive-ui'
import {
  ChatbubbleEllipsesOutline,
  HeartOutline,
  NotificationsOutline,
  SaveOutline
} from '@vicons/ionicons5'

const notifyReplies = defineModel('notifyReplies', { type: Boolean, default: true })
const notifyLikes = defineModel('notifyLikes', { type: Boolean, default: true })

defineProps({
  saving: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['save'])
</script>

<template>
  <section class="settings-section">
    <div class="section-heading">
      <div class="section-icon">
        <NIcon><NotificationsOutline /></NIcon>
      </div>
      <div>
        <p class="section-kicker">站内信</p>
        <h2 class="section-title">通知偏好</h2>
      </div>
    </div>

    <div class="preference-list">
      <div class="preference-item">
        <div class="preference-icon">
          <NIcon><ChatbubbleEllipsesOutline /></NIcon>
        </div>
        <div class="preference-copy">
          <strong>评论回复</strong>
          <span>有人回复你的评论时提醒</span>
        </div>
        <NSwitch v-model:value="notifyReplies" />
      </div>

      <div class="preference-item">
        <div class="preference-icon">
          <NIcon><HeartOutline /></NIcon>
        </div>
        <div class="preference-copy">
          <strong>点赞</strong>
          <span>插件或评论收到点赞时提醒</span>
        </div>
        <NSwitch v-model:value="notifyLikes" />
      </div>
    </div>

    <div class="section-actions">
      <NButton type="primary" :loading="saving" @click="emit('save')">
        <template #icon>
          <NIcon><SaveOutline /></NIcon>
        </template>
        保存通知设置
      </NButton>
    </div>
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
.preference-item,
.section-actions {
  display: flex;
  align-items: center;
}

.section-heading {
  gap: 12px;
  margin-bottom: 18px;
}

.section-icon,
.preference-icon {
  display: grid;
  place-items: center;
}

.section-icon {
  width: 34px;
  height: 34px;
  color: #0e74e4;
  background: rgba(14, 116, 228, 0.12);
  border-radius: 8px;
}

.section-kicker,
.section-title {
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

.preference-list {
  display: grid;
  gap: 10px;
}

.preference-item {
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.preference-icon {
  width: 32px;
  height: 32px;
  flex: none;
  color: var(--text-color-2);
  border: 1px solid var(--border-color);
  border-radius: 8px;
}

.preference-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
  flex: 1;
}

.preference-copy span {
  color: var(--text-color-3);
  font-size: 12px;
}

.section-actions {
  justify-content: flex-end;
  margin-top: 18px;
}

@media (max-width: 640px) {
  .preference-item {
    align-items: flex-start;
  }

  .section-actions {
    justify-content: stretch;
  }

  .section-actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
