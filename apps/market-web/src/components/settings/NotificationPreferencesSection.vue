<script setup lang="ts">
import { NButton, NIcon, NInput, NSwitch } from "naive-ui";
import {
  ChatbubbleEllipsesOutline,
  HeartOutline,
  MailOutline,
  NotificationsOutline,
  SaveOutline,
} from "@vicons/ionicons5";

const notificationEmail = defineModel("notificationEmail", { type: String, default: "" });
const notifyPluginReview = defineModel("notifyPluginReview", { type: Boolean, default: true });
const notifyComments = defineModel("notifyComments", { type: Boolean, default: true });
const notifyReplies = defineModel("notifyReplies", { type: Boolean, default: true });
const notifyLikes = defineModel("notifyLikes", { type: Boolean, default: true });
const notifyUnlist = defineModel("notifyUnlist", { type: Boolean, default: true });
const emailNotifyPluginReview = defineModel("emailNotifyPluginReview", {
  type: Boolean,
  default: false,
});
const emailNotifyComments = defineModel("emailNotifyComments", { type: Boolean, default: false });
const emailNotifyReplies = defineModel("emailNotifyReplies", { type: Boolean, default: false });
const emailNotifyLikes = defineModel("emailNotifyLikes", { type: Boolean, default: false });
const emailNotifyUnlist = defineModel("emailNotifyUnlist", { type: Boolean, default: false });

defineProps({
  fallbackEmail: {
    type: String,
    default: "",
  },
  saving: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["save"]);
</script>

<template>
  <section class="settings-section">
    <div class="section-heading">
      <div class="section-icon">
        <NIcon><NotificationsOutline /></NIcon>
      </div>
      <div>
        <p class="section-kicker">站内信 / 邮件</p>
        <h2 class="section-title">通知偏好</h2>
      </div>
    </div>

    <div class="preference-panel">
      <div class="email-target">
        <div class="preference-icon">
          <NIcon><MailOutline /></NIcon>
        </div>
        <div class="preference-copy">
          <strong>通知邮箱</strong>
          <span>
            留空使用 OAuth 邮箱
            <template v-if="fallbackEmail">：{{ fallbackEmail }}</template>
          </span>
        </div>
        <NInput
          v-model:value="notificationEmail"
          clearable
          placeholder="name@example.com"
          inputmode="email"
        />
      </div>

      <div class="preference-group">
        <div class="group-heading">
          <span>站内信</span>
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><NotificationsOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>审查结果</strong>
            <span>默认开启，插件审核通过或自动审核通过时提醒</span>
          </div>
          <NSwitch v-model:value="notifyPluginReview" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><ChatbubbleEllipsesOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>评论</strong>
            <span>插件收到新评论时提醒</span>
          </div>
          <NSwitch v-model:value="notifyComments" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><ChatbubbleEllipsesOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>回复</strong>
            <span>有人回复你的评论时提醒</span>
          </div>
          <NSwitch v-model:value="notifyReplies" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><HeartOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>点赞</strong>
            <span>插件或评论收到点赞时提醒</span>
          </div>
          <NSwitch v-model:value="notifyLikes" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><NotificationsOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>下架</strong>
            <span>默认开启，插件被管理员下架时提醒</span>
          </div>
          <NSwitch v-model:value="notifyUnlist" />
        </div>
      </div>

      <div class="preference-group">
        <div class="group-heading">
          <span>邮件通知</span>
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><MailOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>审查相关</strong>
            <span>审核结果和管理员待审查提醒发邮件</span>
          </div>
          <NSwitch v-model:value="emailNotifyPluginReview" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><MailOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>评论</strong>
            <span>插件收到新评论时发邮件</span>
          </div>
          <NSwitch v-model:value="emailNotifyComments" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><MailOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>回复</strong>
            <span>有人回复你的评论时发邮件</span>
          </div>
          <NSwitch v-model:value="emailNotifyReplies" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><MailOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>点赞</strong>
            <span>插件或评论收到点赞时发邮件</span>
          </div>
          <NSwitch v-model:value="emailNotifyLikes" />
        </div>
        <div class="preference-row">
          <div class="preference-icon">
            <NIcon><MailOutline /></NIcon>
          </div>
          <div class="preference-copy">
            <strong>下架</strong>
            <span>插件被管理员下架时发邮件</span>
          </div>
          <NSwitch v-model:value="emailNotifyUnlist" />
        </div>
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
.email-target,
.preference-row,
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

.preference-panel,
.preference-group {
  display: grid;
}

.preference-panel {
  gap: 18px;
}

.email-target {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) minmax(240px, 340px);
  gap: 12px;
  padding: 12px 0 16px;
  border-bottom: 1px solid var(--border-color);
}

.preference-group {
  gap: 0;
}

.group-heading {
  display: flex;
  align-items: center;
  height: 28px;
  color: var(--text-color-2);
  font-size: 13px;
  font-weight: 600;
}

.preference-row {
  gap: 12px;
  min-height: 60px;
  padding: 12px 0;
  border-top: 1px solid var(--border-color);
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

.preference-copy strong,
.preference-copy span {
  overflow-wrap: anywhere;
}

.preference-copy span {
  color: var(--text-color-3);
  font-size: 12px;
}

.preference-row :deep(.n-switch) {
  flex: none;
}

.section-actions {
  justify-content: flex-end;
  margin-top: 18px;
}

@media (max-width: 640px) {
  .email-target {
    grid-template-columns: 32px minmax(0, 1fr);
  }

  .email-target :deep(.n-input) {
    grid-column: 1 / -1;
  }

  .preference-row {
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
