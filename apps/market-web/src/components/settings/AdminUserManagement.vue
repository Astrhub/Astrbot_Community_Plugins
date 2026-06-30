<script setup lang="ts">
import { computed, reactive, shallowRef } from "vue";
import {
  NButton,
  NEmpty,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  NSpace,
  NSpin,
  NTag,
  useDialog,
} from "naive-ui";
import {
  BanOutline,
  PersonAddOutline,
  SearchOutline,
  ShieldCheckmarkOutline,
  TrashOutline,
} from "@vicons/ionicons5";

const props = defineProps({
  users: {
    type: Array,
    default: () => [],
  },
  currentUser: {
    type: Object,
    default: null,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  creating: {
    type: Boolean,
    default: false,
  },
  busyIds: {
    type: Object,
    default: () => ({}),
  },
});

const emit = defineEmits([
  "refresh",
  "create-user",
  "update-role",
  "mute-user",
  "unmute-user",
  "delete-user",
]);

const dialog = useDialog();
const searchQuery = shallowRef("");
const showCreateModal = shallowRef(false);
const showMuteModal = shallowRef(false);
const muteTarget = shallowRef(null);
const createForm = reactive({
  username: "",
  password: "",
  role: "user",
});
const muteForm = reactive({
  days: 7,
  reason: "",
});

const roleOptions = Object.freeze([
  { label: "普通用户", value: "user" },
  { label: "管理员", value: "admin" },
]);

const filteredUsers = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  if (!query) return props.users;
  return props.users.filter((user) => {
    const searchText = [
      user.id,
      user.github_login,
      user.internal_username,
      user.github_name,
      user.auth_source,
      user.role,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return searchText.includes(query);
  });
});

const createDisabled = computed(
  () => !createForm.username.trim() || createForm.password.length < 8,
);

const muteDisabled = computed(() => {
  const days = Number(muteForm.days);
  return !muteTarget.value || !Number.isInteger(days) || days < 1;
});

function displayName(user) {
  return user.github_name || user.github_login || user.internal_username || user.id;
}

function username(user) {
  return user.github_login || user.internal_username || "-";
}

function roleLabel(role) {
  if (role === "core_admin") return "核心管理员";
  if (role === "admin") return "管理员";
  return "普通用户";
}

function roleTagType(role) {
  if (role === "core_admin") return "error";
  if (role === "admin") return "warning";
  return "default";
}

function sourceLabel(source) {
  return source === "internal" ? "内部账号" : "GitHub";
}

function isCurrentUser(user) {
  return user.id === props.currentUser?.id;
}

function isCoreAdmin(user) {
  return user.role === "core_admin";
}

function isMuted(user) {
  if (!user.muted_until) return false;
  return new Date(user.muted_until).getTime() > Date.now();
}

function formatTime(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

function busyState(user) {
  return props.busyIds?.[user.id] || "";
}

function resetCreateForm() {
  createForm.username = "";
  createForm.password = "";
  createForm.role = "user";
}

function openCreateModal() {
  resetCreateForm();
  showCreateModal.value = true;
}

function submitCreateUser() {
  const usernameValue = createForm.username.trim();
  const passwordValue = createForm.password;
  if (createDisabled.value) return;
  emit("create-user", {
    username: usernameValue,
    password: passwordValue,
    role: createForm.role,
  });
  showCreateModal.value = false;
  resetCreateForm();
}

function confirmRoleChange(user, role) {
  if (role === user.role) return;
  emit("update-role", { user, role });
}

function muteUntilDays(days) {
  return new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
}

function resetMuteForm() {
  muteForm.days = 7;
  muteForm.reason = "";
  muteTarget.value = null;
}

function openMuteModal(user) {
  muteTarget.value = user;
  muteForm.days = 7;
  muteForm.reason = "";
  showMuteModal.value = true;
}

function submitMuteUser() {
  if (muteDisabled.value) return;
  const user = muteTarget.value;
  const days = Number(muteForm.days);
  emit("mute-user", {
    user,
    muted_until: muteUntilDays(days),
    reason: muteForm.reason.trim(),
  });
  showMuteModal.value = false;
  resetMuteForm();
}

function confirmUnmute(user) {
  dialog.info({
    title: "解除封禁",
    content: `确认解除 ${displayName(user)} 的封禁？`,
    positiveText: "解除",
    negativeText: "取消",
    onPositiveClick: () => emit("unmute-user", user),
  });
}

function confirmDelete(user) {
  dialog.warning({
    title: "删除用户",
    content: `${displayName(user)} 的账号将被删除，已提交插件会转给当前核心管理员保留。`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: () => emit("delete-user", user),
  });
}
</script>

<template>
  <div class="user-management">
    <section class="settings-section">
      <div class="section-title row-title">
        <div>
          <h2>添加内部用户</h2>
          <p>用于后台登录和协作管理；GitHub 用户仍通过 OAuth 自动创建。</p>
        </div>
        <NButton type="primary" :loading="creating" @click="openCreateModal">
          <template #icon>
            <NIcon><PersonAddOutline /></NIcon>
          </template>
          添加用户
        </NButton>
      </div>
    </section>

    <section class="settings-section">
      <div class="section-title row-title">
        <div>
          <h2>用户列表</h2>
          <p>搜索用户名、GitHub 登录名或显示名称，并执行封禁、解封、删除和角色调整。</p>
        </div>
        <NButton tertiary :loading="loading" @click="emit('refresh')">刷新</NButton>
      </div>

      <div class="user-toolbar">
        <NInput v-model:value="searchQuery" clearable placeholder="搜索用户名 / GitHub / 显示名称">
          <template #prefix>
            <NIcon><SearchOutline /></NIcon>
          </template>
        </NInput>
        <span class="result-count">{{ filteredUsers.length }} / {{ users.length }} 个用户</span>
      </div>

      <NSpin :show="loading">
        <NEmpty v-if="filteredUsers.length === 0" description="没有匹配用户" />

        <div v-else class="user-list">
          <article v-for="user in filteredUsers" :key="user.id" class="user-row">
            <div class="user-main">
              <div class="user-title">
                <strong>{{ displayName(user) }}</strong>
                <NTag :type="roleTagType(user.role)" size="small" round>
                  {{ roleLabel(user.role) }}
                </NTag>
                <NTag v-if="isMuted(user)" type="error" size="small" round>已封禁</NTag>
              </div>
              <div class="user-meta">
                <span>{{ sourceLabel(user.auth_source) }}：{{ username(user) }}</span>
                <span>ID：{{ user.id }}</span>
                <span>注册：{{ formatTime(user.created_at) }}</span>
                <span v-if="isMuted(user)">封禁至：{{ formatTime(user.muted_until) }}</span>
                <span v-if="isMuted(user) && user.muted_reason">理由：{{ user.muted_reason }}</span>
              </div>
            </div>

            <NSpace class="user-actions" :size="8">
              <NSelect
                v-if="!isCoreAdmin(user)"
                class="role-select"
                :value="user.role"
                :options="roleOptions"
                :disabled="isCurrentUser(user)"
                :loading="busyState(user) === 'role'"
                @update:value="(role) => confirmRoleChange(user, role)"
              />
              <NButton v-else secondary disabled>
                <template #icon>
                  <NIcon><ShieldCheckmarkOutline /></NIcon>
                </template>
                受保护
              </NButton>

              <NButton
                v-if="isMuted(user)"
                secondary
                :disabled="isCurrentUser(user)"
                :loading="busyState(user) === 'unmute'"
                @click="confirmUnmute(user)"
              >
                解除封禁
              </NButton>
              <NButton
                v-else
                secondary
                type="warning"
                :disabled="isCurrentUser(user)"
                :loading="busyState(user) === 'mute'"
                @click="openMuteModal(user)"
              >
                <template #icon>
                  <NIcon><BanOutline /></NIcon>
                </template>
                封禁
              </NButton>

              <NButton
                secondary
                type="error"
                :disabled="isCurrentUser(user) || isCoreAdmin(user)"
                :loading="busyState(user) === 'delete'"
                @click="confirmDelete(user)"
              >
                <template #icon>
                  <NIcon><TrashOutline /></NIcon>
                </template>
                删除
              </NButton>
            </NSpace>
          </article>
        </div>
      </NSpin>
    </section>

    <NModal
      v-model:show="showCreateModal"
      preset="card"
      title="添加内部用户"
      :bordered="false"
      :mask-closable="!creating"
      class="management-modal"
    >
      <div class="modal-form">
        <NFormItem label="用户名">
          <NInput v-model:value="createForm.username" placeholder="至少 3 个字符" />
        </NFormItem>
        <NFormItem label="密码">
          <NInput
            v-model:value="createForm.password"
            type="password"
            show-password-on="click"
            placeholder="至少 8 个字符"
          />
        </NFormItem>
        <NFormItem label="角色">
          <NSelect v-model:value="createForm.role" :options="roleOptions" />
        </NFormItem>
      </div>
      <template #footer>
        <div class="modal-actions">
          <NButton :disabled="creating" @click="showCreateModal = false">取消</NButton>
          <NButton
            type="primary"
            :loading="creating"
            :disabled="createDisabled"
            @click="submitCreateUser"
          >
            添加用户
          </NButton>
        </div>
      </template>
    </NModal>

    <NModal
      v-model:show="showMuteModal"
      preset="card"
      :title="muteTarget ? `封禁 ${displayName(muteTarget)}` : '封禁用户'"
      :bordered="false"
      :mask-closable="!muteTarget || busyState(muteTarget) !== 'mute'"
      class="management-modal"
      @after-leave="resetMuteForm"
    >
      <div class="modal-form">
        <NFormItem label="封禁天数">
          <NInputNumber
            v-model:value="muteForm.days"
            class="full-width"
            :min="1"
            :precision="0"
            placeholder="输入封禁天数"
          />
        </NFormItem>
        <NFormItem label="封禁理由">
          <NInput
            v-model:value="muteForm.reason"
            type="textarea"
            :maxlength="500"
            show-count
            :autosize="{ minRows: 3, maxRows: 5 }"
            placeholder="可选，填写后会记录在用户封禁信息中"
          />
        </NFormItem>
      </div>
      <template #footer>
        <div class="modal-actions">
          <NButton
            :disabled="muteTarget && busyState(muteTarget) === 'mute'"
            @click="showMuteModal = false"
          >
            取消
          </NButton>
          <NButton
            type="warning"
            :loading="muteTarget && busyState(muteTarget) === 'mute'"
            :disabled="muteDisabled"
            @click="submitMuteUser"
          >
            确认封禁
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.user-management,
.user-list {
  display: grid;
  gap: 16px;
}

.settings-section {
  padding: 22px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

.section-title {
  margin-bottom: 18px;
}

.section-title:last-child {
  margin-bottom: 0;
}

.section-title h2,
.section-title p {
  margin: 0;
}

.section-title h2 {
  color: var(--text-primary);
  font-size: 18px;
}

.section-title p {
  margin-top: 6px;
  color: var(--text-tertiary);
  font-size: 14px;
}

.row-title,
.user-toolbar,
.user-title {
  display: flex;
  align-items: center;
}

.row-title,
.user-toolbar {
  justify-content: space-between;
  gap: 12px;
}

.user-toolbar {
  margin-bottom: 14px;
}

.result-count {
  flex: none;
  color: var(--text-tertiary);
  font-size: 13px;
}

.user-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: flex-start;
  padding: 16px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-hover);
}

.user-main {
  min-width: 0;
  display: grid;
  gap: 8px;
}

.user-title {
  min-width: 0;
  flex-wrap: wrap;
  gap: 8px;
}

.user-title strong {
  overflow-wrap: anywhere;
}

.user-meta {
  display: grid;
  gap: 3px;
  color: var(--text-tertiary);
  font-size: 12px;
}

.user-meta span {
  overflow-wrap: anywhere;
}

.user-actions {
  justify-content: flex-end;
}

.role-select {
  width: 132px;
}

.management-modal {
  width: min(520px, calc(100vw - 32px));
}

.modal-form {
  display: grid;
  gap: 14px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.full-width {
  width: 100%;
}

@media (max-width: 960px) {
  .user-row {
    grid-template-columns: 1fr;
  }

  .user-actions {
    justify-content: flex-start;
  }
}

@media (max-width: 680px) {
  .row-title,
  .user-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .user-actions {
    width: 100%;
  }

  .user-actions :deep(.n-button),
  .role-select {
    flex: 1 1 132px;
  }
}
</style>
