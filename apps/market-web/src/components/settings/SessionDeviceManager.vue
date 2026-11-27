<script setup lang="ts">
import { computed } from "vue";
import { NButton, NEmpty, NIcon, NSpace, NSpin, NTag } from "naive-ui";
import {
  LaptopOutline,
  LogOutOutline,
  PhonePortraitOutline,
  RefreshOutline,
  ShieldCheckmarkOutline,
} from "@vicons/ionicons5";
import type { UserSession } from "@/types";

const props = withDefaults(
  defineProps<{
    sessions: UserSession[];
    loading?: boolean;
    revokingOthers?: boolean;
    busyIds?: Record<string, string>;
  }>(),
  {
    sessions: () => [],
    loading: false,
    revokingOthers: false,
    busyIds: () => ({}),
  },
);

const emit = defineEmits<{
  refresh: [];
  revoke: [session: UserSession];
  "revoke-others": [];
}>();

const otherSessionCount = computed(
  () => props.sessions.filter((session) => !session.is_current).length,
);

function isMobileDevice(label: string): boolean {
  const lower = String(label || "").toLowerCase();
  return lower.includes("iphone") || lower.includes("ipad") || lower.includes("android");
}

function isBusy(session: UserSession): boolean {
  return Boolean(props.busyIds?.[session.id]);
}

function formatTime(value: string | undefined): string {
  if (!value) return "未知时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
</script>

<template>
  <section class="settings-section">
    <div class="section-heading">
      <div class="section-icon">
        <NIcon><ShieldCheckmarkOutline /></NIcon>
      </div>
      <div class="section-copy">
        <p class="section-kicker">账号安全</p>
        <h2 class="section-title">登录设备</h2>
      </div>
      <div class="heading-actions">
        <NButton tertiary :loading="loading" @click="emit('refresh')">
          <template #icon>
            <NIcon><RefreshOutline /></NIcon>
          </template>
          刷新
        </NButton>
        <NButton
          secondary
          type="warning"
          :loading="revokingOthers"
          :disabled="otherSessionCount === 0"
          @click="emit('revoke-others')"
        >
          <template #icon>
            <NIcon><LogOutOutline /></NIcon>
          </template>
          注销其他设备
        </NButton>
      </div>
    </div>

    <p class="section-hint">
      登录时勾选“信任此设备”会在该设备保持登录 1 年，未勾选默认 7
      天过期。如果发现陌生设备，请立即下线并修改密码。
    </p>

    <NSpin :show="loading">
      <NEmpty v-if="sessions.length === 0" description="暂无活跃登录设备" />

      <div v-else class="device-list">
        <article
          v-for="session in sessions"
          :key="session.id"
          class="device-row"
          :class="{ 'device-row--current': session.is_current }"
        >
          <div class="device-icon" aria-hidden="true">
            <NIcon size="20">
              <PhonePortraitOutline v-if="isMobileDevice(session.device_label)" />
              <LaptopOutline v-else />
            </NIcon>
          </div>

          <div class="device-main">
            <div class="device-title-row">
              <strong>{{ session.device_label || "未知设备" }}</strong>
              <NSpace :size="6" class="device-tags">
                <NTag v-if="session.is_current" size="small" type="success" round>当前设备</NTag>
                <NTag size="small" :type="session.trusted ? 'info' : 'default'" round>
                  {{ session.trusted ? "信任设备 · 1 年" : "常规会话 · 7 天" }}
                </NTag>
              </NSpace>
            </div>

            <div class="device-meta">
              <span v-if="session.ip_address">IP：{{ session.ip_address }}</span>
              <span>登录时间：{{ formatTime(session.created_at) }}</span>
              <span>最近活跃：{{ formatTime(session.last_seen_at) }}</span>
              <span>过期时间：{{ formatTime(session.expires_at) }}</span>
            </div>
          </div>

          <NButton
            secondary
            :type="session.is_current ? 'default' : 'error'"
            :loading="isBusy(session)"
            @click="emit('revoke', session)"
          >
            <template #icon>
              <NIcon><LogOutOutline /></NIcon>
            </template>
            {{ session.is_current ? "退出当前设备" : "下线设备" }}
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
  box-shadow: var(--shadow-sm);
  min-width: 0;
}

.section-heading,
.heading-actions,
.device-title-row {
  display: flex;
  align-items: center;
}

.section-heading {
  gap: 12px;
  margin-bottom: 12px;
}

.heading-actions {
  gap: 8px;
  flex-wrap: wrap;
}

.section-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  color: #0e74e4;
  background: rgba(14, 116, 228, 0.12);
  border-radius: 8px;
  flex: none;
}

.section-copy {
  min-width: 0;
  flex: 1;
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

.section-hint {
  margin: 0 0 18px;
  color: var(--text-color-3);
  font-size: 13px;
  line-height: 1.5;
}

.device-list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  min-width: 0;
}

.device-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--body-color);
}

.device-row--current {
  border-color: rgba(24, 160, 88, 0.35);
}

.device-icon {
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  color: var(--text-color-2);
  background: var(--card-color);
  border: 1px solid var(--border-color);
}

.device-main {
  min-width: 0;
  display: grid;
  gap: 8px;
}

.device-title-row {
  gap: 8px;
  flex-wrap: wrap;
}

.device-title-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 15px;
}

.device-tags {
  flex-wrap: wrap;
}

.device-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  color: var(--text-color-3);
  font-size: 12px;
  line-height: 1.4;
}

@media (max-width: 640px) {
  .settings-section {
    padding: 16px;
  }

  .section-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .heading-actions {
    width: 100%;
  }

  .heading-actions :deep(.n-button) {
    flex: 1;
  }

  .device-row {
    grid-template-columns: 1fr;
  }

  .device-icon {
    display: none;
  }

  .device-row :deep(.n-button) {
    width: 100%;
  }
}
</style>
