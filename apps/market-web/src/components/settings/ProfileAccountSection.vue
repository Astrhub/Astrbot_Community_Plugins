<script setup lang="ts">
import { computed, shallowRef, watch } from "vue";
import { NButton, NForm, NFormItem, NIcon, NInput, NInputNumber, NTag } from "naive-ui";
import { KeyOutline, LogoGithub, PersonCircleOutline, SaveOutline } from "@vicons/ionicons5";
import { githubRawUrl } from "@/utils/github";

const githubName = defineModel("githubName", { type: String, default: "" });
const githubToken = defineModel("githubToken", { type: String, default: "" });
const refreshInterval = defineModel("refreshInterval", { type: Number, default: 3600 });

const props = defineProps({
  currentUser: {
    type: Object,
    default: null,
  },
  saving: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["save"]);

const githubLogin = computed(() => props.currentUser?.github_login || "");
const hasGithubToken = computed(() => Boolean(props.currentUser?.has_github_token));
const avatarLoadFailed = shallowRef(false);
const profileAvatarUrl = computed(() =>
  githubRawUrl(String(props.currentUser?.avatar_url || props.currentUser?.avatar || "").trim()),
);
const showProfileAvatar = computed(
  () => Boolean(profileAvatarUrl.value) && !avatarLoadFailed.value,
);

watch(profileAvatarUrl, () => {
  avatarLoadFailed.value = false;
});
</script>

<template>
  <section class="settings-section">
    <div class="section-heading">
      <div class="section-icon">
        <NIcon><PersonCircleOutline /></NIcon>
      </div>
      <div>
        <p class="section-kicker">账号资料</p>
        <h2 class="section-title">个人信息</h2>
      </div>
    </div>

    <div class="github-summary">
      <div class="github-main">
        <span class="profile-avatar">
          <img
            v-if="showProfileAvatar"
            :src="profileAvatarUrl"
            :alt="`${githubLogin || '当前用户'}的头像`"
            class="profile-avatar__image"
            @error="avatarLoadFailed = true"
          />
          <NIcon v-else><PersonCircleOutline /></NIcon>
        </span>
        <div class="github-copy">
          <span class="summary-label">GitHub 登录账号</span>
          <strong
            ><NIcon class="github-icon"><LogoGithub /></NIcon>{{ githubLogin || "未连接" }}</strong
          >
        </div>
      </div>
      <NTag :type="githubLogin ? 'success' : 'warning'" size="small" round>
        {{ githubLogin ? "已连接" : "未连接" }}
      </NTag>
    </div>

    <NForm label-placement="top" class="settings-form">
      <NFormItem label="显示名称">
        <NInput v-model:value="githubName" placeholder="你的显示名称" />
      </NFormItem>

      <div class="token-row">
        <NFormItem label="GitHub API Token">
          <NInput
            v-model:value="githubToken"
            type="password"
            show-password-on="click"
            :placeholder="hasGithubToken ? '已配置，留空保持不变' : 'ghp_... 或 fine-grained token'"
          >
            <template #prefix>
              <NIcon><KeyOutline /></NIcon>
            </template>
          </NInput>
          <template #feedback>
            {{ hasGithubToken ? "当前已配置 Token" : "当前未配置 Token" }}
          </template>
        </NFormItem>

        <NFormItem label="自动刷新间隔（秒）">
          <NInputNumber
            v-model:value="refreshInterval"
            :min="300"
            :max="86400"
            :step="300"
            class="interval-input"
          />
        </NFormItem>
      </div>

      <div class="section-actions">
        <NButton type="primary" :loading="saving" @click="emit('save')">
          <template #icon>
            <NIcon><SaveOutline /></NIcon>
          </template>
          保存资料
        </NButton>
      </div>
    </NForm>
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
.github-summary,
.github-main,
.section-actions {
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

.section-kicker,
.section-title,
.summary-label {
  margin: 0;
}

.section-kicker,
.summary-label {
  color: var(--text-color-3);
  font-size: 12px;
}

.section-title {
  font-size: 18px;
  line-height: 1.3;
}

.github-summary {
  justify-content: space-between;
  gap: 14px;
  padding: 14px;
  margin-bottom: 18px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.github-main {
  min-width: 0;
  gap: 10px;
}

.github-icon {
  color: var(--text-color-2);
  font-size: 14px;
  vertical-align: -2px;
}

.profile-avatar {
  width: 44px;
  height: 44px;
  display: inline-grid;
  flex: none;
  overflow: hidden;
  place-items: center;
  color: var(--text-color-3);
  background: var(--card-color);
  border: 1px solid var(--border-color);
  border-radius: 50%;
  font-size: 25px;
}

.profile-avatar__image {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.github-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.github-copy strong {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  overflow-wrap: anywhere;
}

.settings-form {
  display: grid;
  gap: 4px;
}

.token-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, 240px);
  gap: 14px;
}

.interval-input {
  width: 100%;
}

.section-actions {
  justify-content: flex-end;
}

@media (max-width: 720px) {
  .github-summary {
    align-items: flex-start;
    flex-direction: column;
  }

  .token-row {
    grid-template-columns: 1fr;
  }

  .section-actions {
    justify-content: stretch;
  }

  .section-actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
