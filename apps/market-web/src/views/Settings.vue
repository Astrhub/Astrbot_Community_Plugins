<template>
  <div class="settings-page">
    <n-layout-header class="settings-header">
      <div class="header-content">
        <div class="header-left">
          <n-button quaternary circle @click="goBack" aria-label="返回">
            <template #icon>
              <n-icon><arrow-back /></n-icon>
            </template>
          </n-button>
          <div>
            <p class="eyebrow">核心管理员</p>
            <h1>系统设置</h1>
          </div>
        </div>
        <div class="header-actions">
          <theme-mode-button circle />
          <n-button tertiary @click="loadSettings">刷新</n-button>
          <n-button type="primary" :loading="saving" @click="save">保存</n-button>
        </div>
      </div>
    </n-layout-header>

    <main class="settings-content">
      <n-alert v-if="!isCoreAdmin && !loading" type="warning" :bordered="false">
        只有核心管理员可以管理系统设置。
      </n-alert>

      <n-spin :show="loading">
        <n-form
          v-if="isCoreAdmin"
          ref="formRef"
          :model="formData"
          :rules="rules"
          label-placement="top"
          class="settings-form"
        >
          <n-tabs type="line" animated class="settings-tabs">
            <n-tab-pane name="site" tab="站点与公告" display-directive="show">
              <div class="settings-tab-content">
                <section class="settings-section">
                  <div class="section-title">
                    <h2>站点展示</h2>
                    <p>首页、标题、访问地址和公共联系信息。</p>
                  </div>
                  <div class="form-grid">
                    <n-form-item label="站点名称" path="site.name">
                      <n-input v-model:value="formData.site.name" />
                    </n-form-item>
                    <n-form-item label="站点图标 URL" path="site.icon_url">
                      <n-input
                        v-model:value="formData.site.icon_url"
                        placeholder="/logo.webp 或 https://..."
                      />
                    </n-form-item>
                    <n-form-item label="站点访问地址" path="site.web_url">
                      <n-input
                        v-model:value="formData.site.web_url"
                        placeholder="https://plugins.example.com"
                      />
                    </n-form-item>
                    <n-form-item label="副标题" path="site.subtitle">
                      <n-input v-model:value="formData.site.subtitle" />
                    </n-form-item>
                    <n-form-item label="站点描述" path="site.description">
                      <n-input v-model:value="formData.site.description" />
                    </n-form-item>
                    <n-form-item label="联系邮箱" path="site.contact_email">
                      <n-input v-model:value="formData.site.contact_email" placeholder="可选" />
                    </n-form-item>
                    <n-form-item label="文档地址" path="site.docs_url">
                      <n-input
                        v-model:value="formData.site.docs_url"
                        placeholder="https://docs.astrbot.app/..."
                      />
                    </n-form-item>
                  </div>
                </section>

                <section class="settings-section">
                  <div class="section-title">
                    <h2>站点公告</h2>
                    <p>发布后会显示在市场首页。</p>
                  </div>
                  <div class="form-grid">
                    <n-form-item label="公告标题">
                      <n-input
                        v-model:value="announcementForm.title"
                        :maxlength="80"
                        show-count
                        placeholder="例如：维护通知"
                      />
                    </n-form-item>
                    <n-form-item label="公告内容" class="form-row-full">
                      <n-input
                        v-model:value="announcementForm.body"
                        type="textarea"
                        :maxlength="1000"
                        show-count
                        :autosize="{ minRows: 3, maxRows: 6 }"
                        placeholder="输入需要展示给用户的公告内容"
                      />
                    </n-form-item>
                  </div>
                  <div class="announcement-actions">
                    <n-button
                      type="primary"
                      :loading="publishingAnnouncement"
                      @click="publishSiteAnnouncement"
                    >
                      发布公告
                    </n-button>
                  </div>
                </section>
              </div>
            </n-tab-pane>

            <n-tab-pane name="auth" tab="登录与 OAuth" display-directive="show">
              <div class="settings-tab-content">
                <section class="settings-section">
                  <div class="section-title">
                    <h2>登录与条款</h2>
                    <p>控制后台内部登录、GitHub OAuth，以及登录前必须确认的条款。</p>
                  </div>
                  <div class="switch-grid">
                    <setting-switch
                      v-model="formData.auth.public_login_enabled"
                      label="内部账号登录"
                      enabled="允许 /admin 内部账号登录"
                      disabled="仅保留已登录会话"
                    />
                    <setting-switch
                      v-model="formData.auth.github_login_enabled"
                      label="GitHub OAuth"
                      enabled="允许 GitHub 登录 / 注册"
                      disabled="关闭 GitHub 登录 / 注册"
                    />
                    <setting-switch
                      v-model="formData.auth.login_agreement_enabled"
                      label="登录条款"
                      enabled="登录前确认"
                      disabled="不显示"
                    />
                    <setting-switch
                      v-model="formData.auth.service_terms_enabled"
                      label="服务条款"
                      enabled="显示服务条款"
                      disabled="不显示"
                    />
                  </div>
                  <n-form-item
                    v-if="formData.auth.login_agreement_enabled"
                    label="登录条款内容"
                    path="auth.login_agreement_text"
                  >
                    <n-input
                      v-model:value="formData.auth.login_agreement_text"
                      type="textarea"
                      :autosize="{ minRows: 3, maxRows: 8 }"
                    />
                  </n-form-item>
                  <n-form-item
                    v-if="formData.auth.service_terms_enabled"
                    label="服务条款内容"
                    path="auth.service_terms_text"
                  >
                    <n-input
                      v-model:value="formData.auth.service_terms_text"
                      type="textarea"
                      :autosize="{ minRows: 3, maxRows: 8 }"
                    />
                  </n-form-item>
                </section>

                <section class="settings-section">
                  <div class="section-title">
                    <h2>GitHub OAuth</h2>
                    <p>用于第三方登录、插件仓库归属识别和指定组织管理员。</p>
                  </div>
                  <div class="form-grid">
                    <n-form-item label="Client ID" path="github.client_id">
                      <n-input v-model:value="formData.github.client_id" />
                    </n-form-item>
                    <n-form-item label="Client Secret">
                      <div class="secret-status">
                        <n-tag
                          :type="formData.github.client_secret_configured ? 'success' : 'warning'"
                          :bordered="false"
                        >
                          {{ formData.github.client_secret_configured ? "已配置" : "未配置" }}
                        </n-tag>
                        <span class="secret-status-text"
                          >由部署环境变量 GITHUB_CLIENT_SECRET 管理</span
                        >
                      </div>
                    </n-form-item>
                    <n-form-item label="回调地址" path="github.callback_url">
                      <n-input
                        v-model:value="formData.github.callback_url"
                        placeholder="https://your-domain/v1/auth/github/callback"
                      />
                    </n-form-item>
                    <n-form-item label="管理员组织" path="github.admin_org">
                      <n-input
                        v-model:value="formData.github.admin_org"
                        placeholder="可选，GitHub 组织名"
                      />
                    </n-form-item>
                    <n-form-item label="授权范围" path="github.scope">
                      <n-input
                        v-model:value="formData.github.scope"
                        placeholder="read:user user:email read:org"
                      />
                    </n-form-item>
                  </div>
                </section>
              </div>
            </n-tab-pane>

            <n-tab-pane name="market" tab="市场策略" display-directive="show">
              <div class="settings-tab-content">
                <section class="settings-section">
                  <div class="section-title">
                    <h2>市场策略</h2>
                    <p>控制插件提交、互动功能和审核策略。</p>
                  </div>
                  <div class="switch-grid">
                    <setting-switch
                      v-model="formData.market.submissions_enabled"
                      label="插件提交"
                      enabled="允许提交"
                      disabled="暂停提交"
                    />
                    <setting-switch
                      v-model="formData.market.comments_enabled"
                      label="评论"
                      enabled="允许评论"
                      disabled="关闭评论"
                    />
                    <setting-switch
                      v-model="formData.market.likes_enabled"
                      label="点赞"
                      enabled="允许点赞"
                      disabled="关闭点赞"
                    />
                    <setting-switch
                      v-model="formData.market.plugin_auto_approve_enabled"
                      label="自动上架"
                      enabled="提交后上架"
                      disabled="管理员审核"
                    />
                  </div>
                  <div class="form-grid compact-grid">
                    <n-form-item label="最多标签数" path="market.max_plugin_tags">
                      <n-input-number
                        v-model:value="formData.market.max_plugin_tags"
                        :min="0"
                        :max="50"
                      />
                    </n-form-item>
                  </div>
                </section>

                <section class="settings-section">
                  <div class="section-title">
                    <h2>GitHub 元数据同步</h2>
                    <p>
                      用于插件 Star、更新时间、metadata.yml 和 logo 的后台同步。多个 Token
                      会轮询使用。
                    </p>
                  </div>
                  <div class="switch-grid">
                    <setting-switch
                      v-model="formData.market.metadata_sync_enabled"
                      label="元数据自动同步"
                      enabled="按间隔同步"
                      disabled="暂停同步"
                    />
                  </div>
                  <div class="form-grid">
                    <n-form-item path="market.api_token" class="form-row-full">
                      <template #label>
                        <span class="field-label">
                          新增 GitHub API Token
                          <field-hint
                            content="每行一个只读 Token，也可用逗号或分号分隔；保存时追加到现有池，不会覆盖未移除的 Token。"
                          />
                        </span>
                      </template>
                      <n-input
                        v-model:value="formData.market.api_token"
                        type="textarea"
                        :autosize="{ minRows: 3, maxRows: 7 }"
                        placeholder="ghp_..."
                      />
                      <template #feedback>
                        <div class="token-feedback">
                          <span>{{
                            formData.market.api_token_configured
                              ? "当前已配置系统 Token 池"
                              : "当前未配置系统 Token，未登录用户同步会使用 GitHub 公共限额"
                          }}</span>
                          <div v-if="marketTokenPreviewRows.length" class="token-preview-list">
                            <div
                              v-for="item in marketTokenPreviewRows"
                              :key="`${item.index}-${item.token}`"
                              class="token-preview-item"
                              :class="{
                                'token-preview-item--removing': item.removing,
                                'token-preview-item--disabled': item.disabled,
                              }"
                            >
                              <div class="token-preview-main">
                                <span>Token {{ item.index + 1 }}: {{ item.token }}</span>
                                <div
                                  v-if="
                                    item.errorCode || item.status !== 'active' || item.checkedAt
                                  "
                                  class="token-status-line"
                                >
                                  <n-tag
                                    size="small"
                                    :type="
                                      item.disabled
                                        ? 'error'
                                        : item.status === 'active'
                                          ? 'success'
                                          : 'warning'
                                    "
                                    :bordered="false"
                                  >
                                    {{ tokenStatusLabels[item.status] || item.status }}
                                  </n-tag>
                                  <span v-if="item.errorCode">HTTP {{ item.errorCode }}</span>
                                  <span v-if="item.retryAfterSeconds">
                                    等待 {{ item.retryAfterSeconds }} 秒
                                  </span>
                                  <span v-else-if="item.resetAt">重置于 {{ item.resetAt }}</span>
                                  <span v-if="item.errorMessage">{{ item.errorMessage }}</span>
                                  <span v-if="item.checkedAt">
                                    验证于 {{ formatSettingsTime(item.checkedAt) }}
                                  </span>
                                </div>
                              </div>
                              <div class="token-preview-actions">
                                <n-button
                                  tertiary
                                  size="tiny"
                                  :loading="verifyingTokenIndex === item.index"
                                  :disabled="item.removing"
                                  @click="verifyMarketToken(item.index)"
                                >
                                  <template #icon>
                                    <n-icon><refresh-outline /></n-icon>
                                  </template>
                                  验证
                                </n-button>
                                <n-button
                                  tertiary
                                  size="tiny"
                                  :type="item.removing ? 'default' : 'error'"
                                  @click="toggleMarketTokenRemoval(item.index)"
                                >
                                  {{ item.removing ? "撤销" : "移除" }}
                                </n-button>
                              </div>
                            </div>
                          </div>
                        </div>
                      </template>
                    </n-form-item>
                    <n-form-item
                      label="默认同步间隔（秒）"
                      path="market.metadata_sync_interval_seconds"
                    >
                      <n-input-number
                        v-model:value="formData.market.metadata_sync_interval_seconds"
                        :min="300"
                        :max="86400"
                        :step="300"
                      />
                    </n-form-item>
                  </div>
                </section>
              </div>
            </n-tab-pane>

            <n-tab-pane name="email" tab="邮件服务" display-directive="show">
              <div class="settings-tab-content">
                <section class="settings-section">
                  <div class="section-title">
                    <h2>邮件服务</h2>
                    <p>支持 SMTP 或 Cloudflare Email Service，密钥保存后只显示遮蔽状态。</p>
                  </div>
                  <div class="form-grid compact-grid">
                    <n-form-item label="邮件服务" path="email.provider">
                      <n-select
                        v-model:value="formData.email.provider"
                        :options="emailProviderOptions"
                      />
                    </n-form-item>
                    <n-form-item label="每日发送上限" path="email.daily_limit">
                      <n-input-number v-model:value="formData.email.daily_limit" :min="0" />
                    </n-form-item>
                    <n-form-item
                      label="单邮箱每日验证码上限"
                      path="email.verification_daily_limit_per_user"
                    >
                      <n-input-number
                        v-model:value="formData.email.verification_daily_limit_per_user"
                        :min="0"
                      />
                    </n-form-item>
                  </div>
                  <div v-if="formData.email.provider === 'smtp'" class="form-grid">
                    <n-form-item label="SMTP 主机" path="email.smtp.host">
                      <n-input
                        v-model:value="formData.email.smtp.host"
                        placeholder="smtp.example.com"
                      />
                    </n-form-item>
                    <n-form-item label="SMTP 端口" path="email.smtp.port">
                      <n-input-number
                        v-model:value="formData.email.smtp.port"
                        :min="1"
                        :max="65535"
                      />
                    </n-form-item>
                    <n-form-item label="连接加密" path="email.smtp.encryption">
                      <n-select
                        v-model:value="formData.email.smtp.encryption"
                        :options="smtpEncryptionOptions"
                      />
                      <template #feedback>{{ smtpEncryptionFeedback }}</template>
                    </n-form-item>
                    <n-form-item label="认证方式" path="email.smtp.auth_method">
                      <n-select
                        v-model:value="formData.email.smtp.auth_method"
                        :options="smtpAuthMethodOptions"
                      />
                      <template #feedback>{{ smtpAuthMethodFeedback }}</template>
                    </n-form-item>
                    <n-form-item label="SMTP 账号" path="email.smtp.username">
                      <n-input v-model:value="formData.email.smtp.username" />
                    </n-form-item>
                    <n-form-item path="email.smtp.password">
                      <template #label>
                        <span class="field-label">
                          SMTP 密码
                          <field-hint content="已配置时留空会保持原值；输入新值才会更新。" />
                        </span>
                      </template>
                      <n-input
                        v-model:value="formData.email.smtp.password"
                        type="password"
                        show-password-on="click"
                      />
                      <template #feedback>
                        {{
                          formData.email.smtp.password_configured
                            ? "当前已配置，输入新值后更新"
                            : "当前未配置"
                        }}
                      </template>
                    </n-form-item>
                    <n-form-item label="发件邮箱" path="email.smtp.from_address">
                      <n-input
                        v-model:value="formData.email.smtp.from_address"
                        placeholder="noreply@example.com"
                      />
                    </n-form-item>
                    <n-form-item label="发件名称" path="email.smtp.from_name">
                      <n-input
                        v-model:value="formData.email.smtp.from_name"
                        placeholder="Astrhub Plugins Market"
                      />
                    </n-form-item>
                    <n-form-item
                      v-if="formData.email.smtp.encryption !== 'none'"
                      label="证书校验"
                      path="email.smtp.validate_certs"
                    >
                      <div class="switch-row">
                        <n-switch v-model:value="formData.email.smtp.validate_certs" />
                        <span>{{
                          formData.email.smtp.validate_certs ? "验证 TLS 证书" : "不验证证书"
                        }}</span>
                      </div>
                    </n-form-item>
                  </div>
                  <div v-if="formData.email.provider === 'cloudflare'" class="form-grid">
                    <n-form-item label="Cloudflare Account ID" path="email.cloudflare.account_id">
                      <n-input v-model:value="formData.email.cloudflare.account_id" />
                    </n-form-item>
                    <n-form-item path="email.cloudflare.api_token">
                      <template #label>
                        <span class="field-label">
                          Cloudflare API Token
                          <field-hint content="已配置时留空会保持原值；输入新值才会更新。" />
                        </span>
                      </template>
                      <n-input
                        v-model:value="formData.email.cloudflare.api_token"
                        type="password"
                        show-password-on="click"
                      />
                      <template #feedback>
                        {{
                          formData.email.cloudflare.api_token_configured
                            ? "当前已配置，输入新值后更新"
                            : "当前未配置"
                        }}
                      </template>
                    </n-form-item>
                    <n-form-item label="发件邮箱" path="email.cloudflare.from_address">
                      <n-input
                        v-model:value="formData.email.cloudflare.from_address"
                        placeholder="noreply@mail.example.com"
                      />
                    </n-form-item>
                    <n-form-item label="发件名称" path="email.cloudflare.from_name">
                      <n-input
                        v-model:value="formData.email.cloudflare.from_name"
                        placeholder="Astrhub Plugins Market"
                      />
                    </n-form-item>
                  </div>

                  <div class="test-email-row">
                    <n-input v-model:value="testEmail.to" placeholder="测试收件邮箱" />
                    <n-button
                      :loading="testingEmail"
                      :disabled="formData.email.provider === 'disabled'"
                      @click="sendEmailTest"
                    >
                      发送测试邮件
                    </n-button>
                  </div>
                </section>
              </div>
            </n-tab-pane>

            <n-tab-pane name="users" tab="用户管理" display-directive="show">
              <div class="settings-tab-content">
                <admin-user-management
                  :users="adminUsers"
                  :current-user="currentUser"
                  :loading="loadingUsers"
                  :creating="creatingUser"
                  :busy-ids="userBusyIds"
                  @refresh="refreshAdminUsers"
                  @create-user="createUser"
                  @update-role="updateUserRole"
                  @mute-user="muteUser"
                  @unmute-user="unmuteUser"
                  @delete-user="deleteUser"
                />
              </div>
            </n-tab-pane>

            <n-tab-pane name="infra" tab="基础设施" display-directive="show">
              <div class="settings-tab-content">
                <section class="settings-section">
                  <div class="section-title">
                    <h2>基础设施</h2>
                    <p>
                      PostgreSQL 和 Redis 只在首次初始化向导中通过 Web 配置，后续变更请修改 .env
                      并重启 API。
                    </p>
                  </div>
                  <div class="infra-grid">
                    <div class="infra-item">
                      <span>PostgreSQL</span>
                      <n-tag
                        :type="setupStatus.database_configured ? 'success' : 'warning'"
                        :bordered="false"
                      >
                        {{ setupStatus.database_configured ? "已配置" : "未配置" }}
                      </n-tag>
                    </div>
                    <div class="infra-item">
                      <span>Redis</span>
                      <n-tag
                        :type="setupStatus.redis_configured ? 'success' : 'warning'"
                        :bordered="false"
                      >
                        {{ setupStatus.redis_configured ? "已配置" : "未配置" }}
                      </n-tag>
                    </div>
                    <div class="infra-item">
                      <span>重启状态</span>
                      <n-tag
                        :type="setupStatus.restart_required ? 'warning' : 'success'"
                        :bordered="false"
                      >
                        {{ setupStatus.restart_required ? "需要重启" : "当前生效" }}
                      </n-tag>
                    </div>
                  </div>
                  <p class="infra-note">
                    初始化完成后 `/setup` 会关闭；数据库或 Redis 地址、账号、密码和 SSL
                    配置请在部署环境的 .env 中调整。
                  </p>
                </section>
              </div>
            </n-tab-pane>
          </n-tabs>
        </n-form>
      </n-spin>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, reactive, ref, shallowRef } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import {
  NAlert,
  NButton,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NLayoutHeader,
  NSelect,
  NSpin,
  NSwitch,
  NTabPane,
  NTabs,
  NTag,
  useMessage,
} from "naive-ui";
import { ArrowBack, RefreshOutline } from "@vicons/ionicons5";
import { usePluginStore } from "@/stores/plugins";
import FieldHint from "@/components/FieldHint.vue";
import ThemeModeButton from "@/components/ThemeModeButton.vue";
import AdminUserManagement from "@/components/settings/AdminUserManagement.vue";

const SettingSwitch = {
  props: {
    modelValue: Boolean,
    label: String,
    enabled: String,
    disabled: String,
  },
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    return () =>
      h("div", { class: "switch-card" }, [
        h("span", { class: "switch-label" }, props.label),
        h("div", { class: "switch-row" }, [
          h(NSwitch, {
            value: props.modelValue,
            "onUpdate:value": (value) => emit("update:modelValue", value),
          }),
          h("span", props.modelValue ? props.enabled : props.disabled),
        ]),
      ]);
  },
};

const router = useRouter();
const message = useMessage();
const store = usePluginStore();
const { currentUser, setupStatus } = storeToRefs(store);
const {
  loadCurrentUser,
  loadAdminSetupStatus,
  loadAdminUsers,
  loadSystemSettings,
  saveSystemSettings,
  verifySystemGithubToken,
  sendTestEmail,
  publishAnnouncement,
  createInternalUser,
  updateAdminUserRole,
  muteAdminUser,
  unmuteAdminUser,
  deleteAdminUser,
} = store;

const formRef = ref(null);
const loading = ref(true);
const saving = ref(false);
const testingEmail = ref(false);
const publishingAnnouncement = ref(false);
const verifyingTokenIndex = shallowRef<number | null>(null);
const loadingUsers = shallowRef(false);
const creatingUser = shallowRef(false);
const adminUsers = shallowRef([]);
const userBusyIds = reactive({});
const isCoreAdmin = computed(() => currentUser.value?.role === "core_admin");
const testEmail = reactive({ to: "" });
const announcementForm = reactive({ title: "", body: "" });

const formData = reactive(createSettingsForm());
const marketTokenPreviewRows = computed(() => {
  const removeIndexes = new Set(formData.market.api_token_remove_indexes || []);
  const statuses = formData.market.api_token_statuses || [];
  const previews = formData.market.api_token_previews || [];
  const rows = statuses.length ? statuses : previews.map((token) => ({ token }));
  return rows.map((item, index) => ({
    token: item.token,
    index,
    removing: removeIndexes.has(index),
    disabled: Boolean(item.disabled),
    status: item.status || "active",
    errorCode: item.error_code,
    errorMessage: item.error_message || "",
    retryAfterSeconds: Number(item.retry_after_seconds || 0),
    resetAt: item.reset_at || "",
    checkedAt: item.checked_at || "",
  }));
});
const tokenStatusLabels = {
  active: "正常",
  disabled: "已停用",
  rate_limited: "限流",
  error: "验证失败",
};
const smtpEncryptionFeedback = computed(() => {
  const messages: Record<string, string> = {
    auto: "自动：服务器支持 STARTTLS 时自动升级，否则保持原连接。",
    starttls: "常用于 587 端口，连接后升级到 TLS。",
    ssl_tls: "常用于 465 端口，建立连接时直接使用 TLS。",
    none: "仅用于本地或受信任内网 SMTP，不建议公网使用。",
  };
  return messages[formData.email.smtp.encryption] || messages.auto;
});
const smtpAuthMethodFeedback = computed(() => {
  const messages: Record<string, string> = {
    auto: "自动选择服务器支持的认证方式。",
    login: "强制使用 AUTH LOGIN，适合部分 Outlook、SendCloud、Azure 通道。",
    plain: "强制使用 AUTH PLAIN。",
    none: "不发送认证命令，适合免认证内网 SMTP。",
  };
  return messages[formData.email.smtp.auth_method] || messages.auto;
});

const emailProviderOptions = [
  { label: "关闭邮件", value: "disabled" },
  { label: "SMTP", value: "smtp" },
  { label: "Cloudflare Email Service", value: "cloudflare" },
];
const smtpEncryptionOptions = [
  { label: "自动", value: "auto" },
  { label: "STARTTLS", value: "starttls" },
  { label: "SSL/TLS", value: "ssl_tls" },
  { label: "不加密", value: "none" },
];
const smtpAuthMethodOptions = [
  { label: "自动", value: "auto" },
  { label: "AUTH LOGIN", value: "login" },
  { label: "AUTH PLAIN", value: "plain" },
  { label: "不认证", value: "none" },
];

const MASKED_SECRET = "********";
const DEFAULT_EMAIL_FROM_NAME = "Astrhub Plugins Market";
const requiredText = (text) => ({ required: true, message: text, trigger: "blur" });
const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function isPublicHttpUrl(value) {
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    return (
      ["http:", "https:"].includes(url.protocol) &&
      !["localhost", "0.0.0.0", "::1"].includes(hostname) &&
      !hostname.startsWith("127.")
    );
  } catch {
    return false;
  }
}
const rules = {
  "site.name": [requiredText("请输入站点名称")],
  "site.icon_url": [
    requiredText("请输入站点图标 URL"),
    {
      validator: (_, value) => String(value || "").startsWith("/") || /^https?:\/\//.test(value),
      message: "请输入 / 开头路径或 http(s) URL",
      trigger: "blur",
    },
  ],
  "site.web_url": [
    {
      validator: (_, value) => /^https?:\/\//.test(value || ""),
      message: "请输入 http(s) 站点访问地址",
      trigger: "blur",
    },
    {
      validator: (_, value) => !formData.auth.github_login_enabled || isPublicHttpUrl(value),
      message: "启用 GitHub 登录后必须填写公网站点访问地址",
      trigger: "blur",
    },
  ],
  "site.contact_email": [
    {
      validator: (_, value) => !value || emailPattern.test(value),
      message: "请输入有效邮箱",
      trigger: "blur",
    },
  ],
  "site.docs_url": [
    {
      validator: (_, value) => !value || /^https?:\/\//.test(value),
      message: "请输入 http(s) URL",
      trigger: "blur",
    },
  ],
  "auth.login_agreement_text": [
    {
      validator: () =>
        !formData.auth.login_agreement_enabled ||
        Boolean(formData.auth.login_agreement_text.trim()),
      message: "启用登录条款后必须填写内容",
      trigger: "blur",
    },
  ],
  "auth.service_terms_text": [
    {
      validator: () =>
        !formData.auth.service_terms_enabled || Boolean(formData.auth.service_terms_text.trim()),
      message: "启用服务条款后必须填写内容",
      trigger: "blur",
    },
  ],
  "github.client_id": [
    {
      validator: () =>
        !formData.auth.github_login_enabled || Boolean(formData.github.client_id.trim()),
      message: "启用 GitHub 登录后必须填写 Client ID",
      trigger: "blur",
    },
  ],
  "github.callback_url": [
    {
      validator: (_, value) => {
        if (!formData.auth.github_login_enabled) return true;
        return isPublicHttpUrl(value);
      },
      message: "启用 GitHub 登录后必须填写公网 http(s) 回调地址",
      trigger: "blur",
    },
  ],
  "email.smtp.host": [
    {
      validator: () =>
        formData.email.provider !== "smtp" || Boolean(formData.email.smtp.host.trim()),
      message: "启用 SMTP 后必须填写主机",
      trigger: "blur",
    },
  ],
  "email.smtp.from_address": [
    {
      validator: () =>
        formData.email.provider !== "smtp" || emailPattern.test(formData.email.smtp.from_address),
      message: "请输入有效发件邮箱",
      trigger: "blur",
    },
  ],
  "email.cloudflare.account_id": [
    {
      validator: () =>
        formData.email.provider !== "cloudflare" ||
        Boolean(formData.email.cloudflare.account_id.trim()),
      message: "启用 Cloudflare 后必须填写 Account ID",
      trigger: "blur",
    },
  ],
  "email.cloudflare.api_token": [
    {
      validator: () =>
        formData.email.provider !== "cloudflare" ||
        Boolean(formData.email.cloudflare.api_token.trim()) ||
        formData.email.cloudflare.api_token_configured,
      message: "启用 Cloudflare 后必须填写 API Token",
      trigger: "blur",
    },
  ],
  "email.cloudflare.from_address": [
    {
      validator: () =>
        formData.email.provider !== "cloudflare" ||
        emailPattern.test(formData.email.cloudflare.from_address),
      message: "请输入有效发件邮箱",
      trigger: "blur",
    },
  ],
};

function createSettingsForm() {
  return {
    site: {
      name: "",
      icon_url: "",
      web_url: window.location.origin,
      subtitle: "",
      description: "",
      contact_email: "",
      docs_url: "",
    },
    auth: {
      github_login_enabled: false,
      public_login_enabled: true,
      login_agreement_enabled: false,
      login_agreement_text: "",
      service_terms_enabled: false,
      service_terms_text: "",
    },
    github: {
      client_id: "",
      client_secret: "",
      client_secret_configured: false,
      callback_url: "",
      scope: "read:user user:email read:org",
      admin_org: "",
    },
    market: {
      submissions_enabled: true,
      comments_enabled: true,
      likes_enabled: true,
      plugin_auto_approve_enabled: false,
      max_plugin_tags: 8,
      api_token: "",
      api_token_configured: false,
      api_token_previews: [],
      api_token_statuses: [],
      api_token_remove_indexes: [],
      metadata_sync_enabled: true,
      metadata_sync_interval_seconds: 3600,
    },
    email: {
      provider: "disabled",
      smtp: {
        host: "",
        port: 587,
        username: "",
        password: "",
        password_configured: false,
        from_address: "",
        from_name: DEFAULT_EMAIL_FROM_NAME,
        ssl: false,
        encryption: "auto",
        auth_method: "auto",
        validate_certs: true,
      },
      cloudflare: {
        account_id: "",
        api_token: "",
        api_token_configured: false,
        from_address: "",
        from_name: DEFAULT_EMAIL_FROM_NAME,
      },
      daily_limit: 0,
      verification_daily_limit_per_user: 5,
    },
  };
}

function applySettings(config = {}) {
  Object.assign(formData.site, config.site || {});
  Object.assign(formData.auth, config.auth || {});
  Object.assign(formData.github, config.github || {});
  Object.assign(formData.market, config.market || {});
  formData.market.api_token = "";
  formData.market.api_token_remove_indexes = [];
  if (!config.market?.api_token && config.github?.api_token) {
    formData.market.api_token_configured = Boolean(config.github.api_token_configured);
  }
  if (
    !config.market?.metadata_sync_interval_seconds &&
    config.github?.metadata_sync_interval_seconds
  ) {
    formData.market.metadata_sync_interval_seconds = config.github.metadata_sync_interval_seconds;
  }
  if (
    config.market?.metadata_sync_enabled === undefined &&
    config.github?.metadata_sync_enabled !== undefined
  ) {
    formData.market.metadata_sync_enabled = config.github.metadata_sync_enabled;
  }
  Object.assign(formData.email, config.email || {});
  Object.assign(formData.email.smtp, config.email?.smtp || {});
  Object.assign(formData.email.cloudflare, config.email?.cloudflare || {});
  normalizeSmtpOptions();
  formData.email.smtp.password = "";
  formData.email.cloudflare.api_token = "";
}

function normalizeNumberFields() {
  formData.market.max_plugin_tags = Number(formData.market.max_plugin_tags || 0);
  formData.market.metadata_sync_interval_seconds = Number(
    formData.market.metadata_sync_interval_seconds || 3600,
  );
  formData.email.smtp.port = Number(formData.email.smtp.port || 587);
  formData.email.daily_limit = Number(formData.email.daily_limit || 0);
  formData.email.verification_daily_limit_per_user = Number(
    formData.email.verification_daily_limit_per_user || 0,
  );
  normalizeSmtpOptions();
}

function settingsPayload() {
  normalizeNumberFields();
  const payload = JSON.parse(JSON.stringify(formData));
  delete payload.github.client_secret;
  delete payload.github.client_secret_configured;
  payload.market.api_token = secretInputPayload(payload.market.api_token);
  delete payload.market.api_token_configured;
  delete payload.market.api_token_previews;
  delete payload.market.api_token_statuses;
  delete payload.market.api_token_status;
  payload.email.smtp.password = secretInputPayload(payload.email.smtp.password);
  payload.email.smtp.ssl = payload.email.smtp.encryption === "ssl_tls";
  delete payload.email.smtp.password_configured;
  payload.email.cloudflare.api_token = secretInputPayload(payload.email.cloudflare.api_token);
  delete payload.email.cloudflare.api_token_configured;
  delete payload.email.cloudflare.api_token_previews;
  return payload;
}

function secretInputPayload(value) {
  const text = String(value || "").trim();
  return text === MASKED_SECRET ? "" : text;
}

function normalizeSmtpOptions() {
  formData.email.smtp.encryption = normalizeSmtpEncryption(formData.email.smtp.encryption);
  formData.email.smtp.auth_method = normalizeSmtpAuthMethod(formData.email.smtp.auth_method);
  formData.email.smtp.validate_certs = formData.email.smtp.validate_certs !== false;
  formData.email.smtp.ssl = formData.email.smtp.encryption === "ssl_tls";
}

function normalizeSmtpEncryption(value) {
  const encryption = String(value || "")
    .trim()
    .toLowerCase()
    .replace("-", "_");
  if (["auto", "none", "starttls", "ssl_tls"].includes(encryption)) return encryption;
  if (["ssl", "tls"].includes(encryption)) return "ssl_tls";
  return "auto";
}

function normalizeSmtpAuthMethod(value) {
  const method = String(value || "")
    .trim()
    .toLowerCase()
    .replace("-", "_");
  return ["auto", "login", "plain", "none"].includes(method) ? method : "auto";
}

function toggleMarketTokenRemoval(index) {
  const indexes = new Set(formData.market.api_token_remove_indexes || []);
  if (indexes.has(index)) {
    indexes.delete(index);
  } else {
    indexes.add(index);
  }
  formData.market.api_token_remove_indexes = Array.from(indexes).sort((a, b) => a - b);
}

function formatSettingsTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

async function verifyMarketToken(index) {
  verifyingTokenIndex.value = index;
  try {
    await verifySystemGithubToken(index);
    await loadSettings();
    message.success("GitHub Token 验证完成");
  } catch (error) {
    message.error(error.message || "GitHub Token 验证失败");
  } finally {
    verifyingTokenIndex.value = null;
  }
}

async function loadSettings() {
  loading.value = true;
  try {
    await loadCurrentUser();
    if (!isCoreAdmin.value) {
      router.replace("/admin");
      return;
    }
    await loadAdminSetupStatus();
    applySettings(await loadSystemSettings());
    await refreshAdminUsers();
  } catch (error) {
    message.error(error.message || "加载设置失败");
  } finally {
    loading.value = false;
  }
}

async function refreshAdminUsers() {
  if (!isCoreAdmin.value) return;
  loadingUsers.value = true;
  try {
    adminUsers.value = await loadAdminUsers();
  } catch (error) {
    message.error(error.message || "加载用户失败");
  } finally {
    loadingUsers.value = false;
  }
}

function replaceUser(updatedUser) {
  adminUsers.value = adminUsers.value.map((user) =>
    user.id === updatedUser.id ? { ...user, ...updatedUser } : user,
  );
}

async function withUserBusy(user, action, task) {
  userBusyIds[user.id] = action;
  try {
    return await task();
  } finally {
    delete userBusyIds[user.id];
  }
}

async function createUser(payload) {
  creatingUser.value = true;
  try {
    const created = await createInternalUser(payload);
    adminUsers.value = [created, ...adminUsers.value];
    message.success("用户已添加");
  } catch (error) {
    message.error(error.message || "创建用户失败");
  } finally {
    creatingUser.value = false;
  }
}

async function updateUserRole({ user, role }) {
  try {
    const updated = await withUserBusy(user, "role", () => updateAdminUserRole(user.id, role));
    replaceUser(updated);
    message.success("用户角色已更新");
  } catch (error) {
    message.error(error.message || "更新角色失败");
  }
}

async function muteUser({ user, muted_until, reason }) {
  try {
    const updated = await withUserBusy(user, "mute", () =>
      muteAdminUser(user.id, { muted_until, reason }),
    );
    replaceUser(updated);
    message.success("用户已封禁");
  } catch (error) {
    message.error(error.message || "封禁失败");
  }
}

async function unmuteUser(user) {
  try {
    const updated = await withUserBusy(user, "unmute", () => unmuteAdminUser(user.id));
    replaceUser(updated);
    message.success("封禁已解除");
  } catch (error) {
    message.error(error.message || "解除封禁失败");
  }
}

async function deleteUser(user) {
  try {
    await withUserBusy(user, "delete", () => deleteAdminUser(user.id));
    adminUsers.value = adminUsers.value.filter((item) => item.id !== user.id);
    message.success("用户已删除");
  } catch (error) {
    message.error(error.message || "删除用户失败");
  }
}

function save() {
  if (!isCoreAdmin.value) {
    message.warning("只有核心管理员可以保存设置");
    return;
  }
  formRef.value?.validate(async (errors) => {
    if (errors) {
      message.error("请完善设置项");
      return;
    }
    saving.value = true;
    try {
      const result = await saveSystemSettings(settingsPayload());
      applySettings(result.settings);
      message.success(
        result.restart_required ? "设置已保存，数据库或 Redis 变更需重启生效" : "设置已保存",
      );
    } catch (error) {
      message.error(error.message || "保存失败");
    } finally {
      saving.value = false;
    }
  });
}

async function publishSiteAnnouncement() {
  if (!isCoreAdmin.value) {
    message.warning("只有核心管理员可以发布公告");
    return;
  }
  const title = announcementForm.title.trim();
  const body = announcementForm.body.trim();
  if (!title || !body) {
    message.warning("请填写公告标题和内容");
    return;
  }
  publishingAnnouncement.value = true;
  try {
    await publishAnnouncement({ title, body });
    announcementForm.title = "";
    announcementForm.body = "";
    message.success("公告已发布");
  } catch (error) {
    message.error(error.message || "发布公告失败");
  } finally {
    publishingAnnouncement.value = false;
  }
}

async function sendEmailTest() {
  if (!emailPattern.test(testEmail.to)) {
    message.warning("请输入有效的测试收件邮箱");
    return;
  }
  testingEmail.value = true;
  try {
    await sendTestEmail({
      to: testEmail.to,
      subject: `${formData.site.name || "AstrBot Community Plugins"} 测试邮件`,
      body: "这是一封来自 AstrBot 社区插件市场的测试邮件。",
    });
    message.success("测试邮件已发送");
  } catch (error) {
    message.error(error.message || "测试邮件发送失败");
  } finally {
    testingEmail.value = false;
  }
}

function goBack() {
  router.push("/");
}

onMounted(loadSettings);
</script>

<style scoped>
.settings-page {
  min-height: 100vh;
  background: var(--bg-base);
}

.settings-header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: var(--bg-header);
  border-bottom: 1px solid var(--border-base);
  backdrop-filter: blur(18px);
  box-shadow: var(--shadow-sm);
}

.header-content {
  max-width: 1180px;
  margin: 0 auto;
  padding: 18px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.header-left,
.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.eyebrow {
  margin: 0 0 4px;
  color: var(--primary-color);
  font-size: 12px;
  font-weight: 700;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  color: var(--text-primary);
  font-size: 22px;
}

.settings-content {
  max-width: 1040px;
  margin: 0 auto;
  padding: 28px 20px 44px;
}

.settings-form {
  display: block;
}

.settings-tabs :deep(.n-tabs-nav) {
  margin-bottom: 18px;
  padding: 0 12px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

.settings-tabs :deep(.n-tabs-tab) {
  min-height: 46px;
}

.settings-tab-content {
  display: grid;
  gap: 18px;
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

.section-title h2 {
  color: var(--text-primary);
  font-size: 18px;
}

.section-title p {
  margin-top: 6px;
  color: var(--text-tertiary);
  font-size: 14px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 16px;
}

.form-row-full {
  grid-column: 1 / -1;
}

.field-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.infra-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.infra-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px;
  color: var(--text-primary);
  background: var(--bg-hover);
  border: 1px solid var(--border-base);
  border-radius: 8px;
}

.infra-note {
  margin-top: 14px;
  color: var(--text-tertiary);
  font-size: 14px;
  line-height: 1.7;
}

.compact-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.switch-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 10px;
}

.switch-card {
  padding: 12px;
  border: 1px solid var(--border-base);
  border-radius: 8px;
  background: var(--bg-hover);
}

.switch-label {
  display: block;
  margin-bottom: 10px;
  color: var(--text-primary);
  font-weight: 600;
}

.switch-row {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-height: 34px;
  color: var(--text-secondary);
}

.test-email-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  margin-top: 10px;
}

.announcement-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

.token-feedback {
  display: grid;
  gap: 8px;
}

.token-preview-list {
  display: grid;
  gap: 6px;
}

.token-preview-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  color: var(--text-secondary);
  background: var(--bg-hover);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  font-family: var(--font-family-mono);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.token-preview-main {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.token-preview-actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
}

.token-status-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-family: var(--font-family);
}

.token-preview-item--disabled {
  border-color: var(--error-color);
}

.token-preview-item--removing {
  color: var(--text-tertiary);
  opacity: 0.7;
}

.token-preview-item--removing span {
  text-decoration: line-through;
}

.secret-status {
  min-height: 34px;
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.secret-status-text {
  overflow-wrap: anywhere;
}

@media (max-width: 760px) {
  .header-content {
    align-items: flex-start;
    flex-direction: column;
  }

  .header-actions {
    width: 100%;
    justify-content: space-between;
    overflow-x: auto;
    padding-bottom: 2px;
  }

  .token-preview-item {
    align-items: flex-start;
    flex-direction: column;
  }

  .form-grid,
  .infra-grid,
  .compact-grid,
  .switch-grid,
  .test-email-row {
    grid-template-columns: 1fr;
  }

  .settings-content {
    padding: 20px 14px 34px;
  }

  .announcement-actions {
    justify-content: stretch;
  }

  .announcement-actions :deep(.n-button) {
    width: 100%;
  }
}
</style>
