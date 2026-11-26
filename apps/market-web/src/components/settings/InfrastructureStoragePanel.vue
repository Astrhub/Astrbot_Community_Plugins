<template>
  <section class="settings-section storage-panel">
    <div class="storage-header">
      <div class="section-title">
        <h2>对象存储与迁移</h2>
        <p>
          按用途管理独立存储配置、连接探活、用途切换与后台对象迁移；编辑配置时，密钥类字段留空会保留现有加密凭据。
        </p>
      </div>
      <div class="storage-header-actions">
        <n-button secondary :loading="loading" @click="loadAll">
          <template #icon>
            <n-icon><RefreshOutline /></n-icon>
          </template>
          刷新
        </n-button>
        <n-button type="primary" @click="openCreateModal">
          <template #icon>
            <n-icon><AddOutline /></n-icon>
          </template>
          新增存储配置
        </n-button>
      </div>
    </div>

    <n-alert
      v-if="feedback.message"
      :type="feedback.type"
      closable
      class="storage-feedback"
      @close="feedback.message = ''"
    >
      {{ feedback.message }}
    </n-alert>

    <n-spin :show="loading">
      <div class="storage-subsection">
        <h3>用途绑定</h3>
        <div class="binding-grid">
          <div v-for="binding in bindings" :key="binding.purpose" class="binding-card">
            <div class="binding-meta">
              <span class="binding-title">{{ purposeLabel(binding.purpose) }}</span>
              <span class="binding-desc">{{ purposeDescription(binding.purpose) }}</span>
            </div>
            <div class="binding-controls">
              <n-select
                v-model:value="bindingDrafts[binding.purpose]"
                :options="profileOptionsForPurpose(binding.purpose)"
                placeholder="选择活动存储配置"
                :aria-label="`${purposeLabel(binding.purpose)}存储配置`"
              />
              <n-button
                type="primary"
                secondary
                :disabled="
                  !bindingDrafts[binding.purpose] ||
                  bindingDrafts[binding.purpose] === binding.profile_id ||
                  busyAction === `bind:${binding.purpose}`
                "
                :loading="busyAction === `bind:${binding.purpose}`"
                @click="openBindConfirm(binding.purpose)"
              >
                应用绑定
              </n-button>
            </div>
            <div class="binding-foot">
              <span>当前绑定：{{ binding.profile_id || "未绑定" }}</span>
              <span v-if="binding.generation > 0">代数 #{{ binding.generation }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="storage-subsection">
        <h3>存储配置</h3>
        <div v-if="groupedProfiles.length === 0" class="storage-empty">
          <n-empty description="尚未配置任何对象存储 Profile" />
        </div>
        <div v-else class="backend-groups">
          <div v-for="group in groupedProfiles" :key="group.backend" class="backend-group">
            <div class="backend-group-header">
              <h4>{{ group.label }}</h4>
              <span class="backend-group-count">{{ group.items.length }} 项</span>
            </div>
            <div class="profile-list">
              <article v-for="profile in group.items" :key="profile.id" class="profile-card">
                <div class="profile-main">
                  <div class="profile-title-row">
                    <strong>{{ profile.name }}</strong>
                    <code class="profile-id">{{ profile.id }}</code>
                    <n-tag size="small" :type="statusTagType(profile.status)" :bordered="false">
                      {{ statusLabel(profile.status) }}
                    </n-tag>
                    <n-tag
                      size="small"
                      :type="healthTagType(profile.health_status)"
                      :bordered="false"
                    >
                      {{ healthLabel(profile.health_status) }}
                    </n-tag>
                    <n-tag v-if="profile.legacy" size="small" type="info" :bordered="false">
                      兼容配置
                    </n-tag>
                  </div>
                  <p class="profile-summary">{{ summarizeProfileConfig(profile) }}</p>
                  <div class="profile-meta-row">
                    <span>活动对象：{{ profile.active_object_count }}</span>
                    <span v-if="profile.pending_object_count > 0">
                      待完成对象：{{ profile.pending_object_count }}
                    </span>
                    <span v-if="profile.bound_purposes.length > 0">
                      已绑定用途：{{ profile.bound_purposes.map(purposeLabel).join("、") }}
                    </span>
                    <span v-else>未绑定用途</span>
                    <span v-if="profile.last_checked_at">
                      最近检查：{{ formatTime(profile.last_checked_at) }}
                    </span>
                  </div>
                  <p v-if="profile.health_message" class="profile-health-message">
                    {{ profile.health_message }}
                  </p>
                </div>

                <div class="profile-actions">
                  <n-button
                    size="small"
                    secondary
                    :disabled="profile.status === 'retired' || profile.status === 'removed'"
                    :loading="busyAction === `test:${profile.id}`"
                    @click="testProfile(profile)"
                  >
                    <template #icon>
                      <n-icon><CheckmarkCircleOutline /></n-icon>
                    </template>
                    测试连接
                  </n-button>
                  <n-button
                    size="small"
                    secondary
                    :disabled="profile.status === 'retired' || profile.status === 'removed'"
                    @click="openEditModal(profile)"
                  >
                    <template #icon>
                      <n-icon><CreateOutline /></n-icon>
                    </template>
                    编辑
                  </n-button>
                  <n-button
                    size="small"
                    secondary
                    type="warning"
                    :disabled="!canDecommission(profile) || profile.status === 'retired'"
                    :loading="busyAction === `retire:${profile.id}`"
                    @click="openDecommissionConfirm(profile, 'retire')"
                  >
                    退役
                  </n-button>
                  <n-button
                    size="small"
                    secondary
                    type="error"
                    :disabled="!canDecommission(profile)"
                    :loading="busyAction === `delete:${profile.id}`"
                    @click="openDecommissionConfirm(profile, 'delete')"
                  >
                    <template #icon>
                      <n-icon><TrashOutline /></n-icon>
                    </template>
                    移除
                  </n-button>
                </div>
              </article>
            </div>
          </div>
        </div>
      </div>

      <div class="storage-subsection">
        <h3>迁移任务</h3>
        <div v-if="migrations.length === 0" class="storage-empty-inline">
          当前没有历史或进行中的存储迁移任务。切换用途绑定后会自动生成迁移记录。
        </div>
        <div v-else class="migration-list">
          <article v-for="migration in migrations" :key="migration.id" class="migration-card">
            <div class="migration-head">
              <div class="migration-title">
                <strong>{{ purposeLabel(migration.purpose) }}</strong>
                <span class="migration-route">
                  {{ migration.source_profile_id }} → {{ migration.target_profile_id }}
                </span>
                <n-tag size="small" :type="migrationTagType(migration.status)" :bordered="false">
                  {{ migrationStatusLabel(migration.status) }}
                </n-tag>
                <n-tag
                  v-if="migration.unknown_objects.length > 0"
                  size="small"
                  type="warning"
                  :bordered="false"
                >
                  未登记对象 {{ migration.unknown_objects.length }}
                </n-tag>
              </div>
              <div class="migration-actions">
                <n-button
                  v-if="migration.status === 'queued' || migration.status === 'running'"
                  size="small"
                  secondary
                  :loading="busyAction === `pause:${migration.id}`"
                  @click="runMigrationAction(migration.id, 'pause')"
                >
                  <template #icon>
                    <n-icon><PauseOutline /></n-icon>
                  </template>
                  暂停
                </n-button>
                <n-button
                  v-if="migration.status === 'paused'"
                  size="small"
                  secondary
                  type="primary"
                  :loading="busyAction === `resume:${migration.id}`"
                  @click="runMigrationAction(migration.id, 'resume')"
                >
                  <template #icon>
                    <n-icon><PlayOutline /></n-icon>
                  </template>
                  继续
                </n-button>
                <n-button
                  v-if="migration.failed_count > 0"
                  size="small"
                  secondary
                  type="warning"
                  :loading="busyAction === `retry:${migration.id}`"
                  @click="runMigrationAction(migration.id, 'retry-failed')"
                >
                  重试失败项
                </n-button>
                <n-button
                  v-if="migration.unknown_objects.length > 0 && migration.status !== 'cancelled'"
                  size="small"
                  secondary
                  type="info"
                  :loading="busyAction === `import-unknown:${migration.id}`"
                  @click="runMigrationAction(migration.id, 'import-unknown')"
                >
                  导入未登记对象
                </n-button>
                <n-button
                  size="small"
                  quaternary
                  :loading="busyAction === `detail:${migration.id}`"
                  @click="toggleMigrationDetail(migration.id)"
                >
                  {{ expandedMigrationId === migration.id ? "收起详情" : "查看详情" }}
                </n-button>
              </div>
            </div>
            <div class="migration-stats">
              <span>进度：{{ migration.completed_count }} / {{ migration.total_count }}</span>
              <span>跳过：{{ migration.skipped_count }}</span>
              <span>失败：{{ migration.failed_count }}</span>
              <span>创建时间：{{ formatTime(migration.created_at) }}</span>
            </div>
            <p v-if="migration.last_error" class="migration-error">{{ migration.last_error }}</p>
            <div
              v-if="expandedMigrationId === migration.id && expandedMigrationDetail"
              class="migration-detail"
            >
              <div v-if="expandedMigrationDetail.unknown_objects.length > 0" class="detail-block">
                <strong>未登记对象（阻止源配置直接退役）：</strong>
                <ul>
                  <li
                    v-for="item in expandedMigrationDetail.unknown_objects"
                    :key="`${item.namespace}:${item.key}`"
                  >
                    <code>{{ item.namespace ? `${item.namespace}/` : "" }}{{ item.key }}</code>
                    ({{ item.size_bytes }} B)
                  </li>
                </ul>
              </div>
              <div v-if="expandedMigrationDetail.items.length > 0" class="detail-block">
                <strong>对象迁移条目：</strong>
                <ul>
                  <li v-for="entry in expandedMigrationDetail.items" :key="entry.id">
                    <code>{{ entry.source_key }}</code>
                    <span> — {{ entry.status }}</span>
                    <span v-if="entry.last_error">（{{ entry.last_error }}）</span>
                  </li>
                </ul>
              </div>
            </div>
          </article>
        </div>
      </div>
    </n-spin>

    <n-modal
      v-model:show="profileModalOpen"
      preset="card"
      :title="editingProfile ? `编辑存储配置：${editingProfile.id}` : '新增存储配置'"
      class="storage-modal"
    >
      <div class="modal-form">
        <n-form-item v-if="!editingProfile" label="配置 ID">
          <n-input
            v-model:value="profileForm.id"
            placeholder="例如：local-sqlite-main 或 r2-published"
          />
        </n-form-item>
        <n-form-item label="显示名称">
          <n-input v-model:value="profileForm.name" placeholder="例如：本地 SQLite 归档库" />
        </n-form-item>
        <n-form-item v-if="!editingProfile" label="后端类型">
          <n-select v-model:value="profileForm.backend" :options="backendOptions" />
        </n-form-item>

        <template v-if="profileForm.backend === 'local'">
          <n-form-item label="本地存储模式">
            <n-select v-model:value="profileForm.localMode" :options="localModeOptions" />
          </n-form-item>
          <n-form-item v-if="profileForm.localMode === 'filesystem'" label="存储根目录">
            <n-input v-model:value="profileForm.localRoot" placeholder="/var/lib/astrhub/objects" />
          </n-form-item>
          <n-form-item v-if="profileForm.localMode === 'sqlite'" label="SQLite 数据库文件路径">
            <n-input
              v-model:value="profileForm.sqlitePath"
              placeholder="/var/lib/astrhub/storage_blobs.sqlite3"
            />
          </n-form-item>
          <template v-if="profileForm.localMode === 'postgresql'">
            <n-form-item label="复用主市场 PostgreSQL">
              <n-switch v-model:value="profileForm.pgUseMarketDb" />
            </n-form-item>
            <n-form-item label="Schema 名称">
              <n-input v-model:value="profileForm.pgSchema" placeholder="public" />
            </n-form-item>
            <n-form-item v-if="!profileForm.pgUseMarketDb" label="独立 PostgreSQL DSN">
              <n-input
                v-model:value="profileForm.pgDsn"
                type="password"
                show-password-on="click"
                :placeholder="
                  editingProfile?.has_secret
                    ? '留空保留已加密保存的 DSN'
                    : 'postgresql://user:pass@host:5432/db'
                "
              />
            </n-form-item>
          </template>
        </template>

        <template v-if="profileForm.backend === 's3'">
          <n-form-item label="Endpoint URL">
            <n-input
              v-model:value="profileForm.s3EndpointUrl"
              placeholder="https://<account>.r2.cloudflarestorage.com"
            />
          </n-form-item>
          <n-form-item label="Region">
            <n-input v-model:value="profileForm.s3Region" placeholder="auto" />
          </n-form-item>
          <n-form-item label="Bucket">
            <n-input v-model:value="profileForm.s3Bucket" placeholder="astrhub-published" />
          </n-form-item>
          <n-form-item label="Access Key ID">
            <n-input
              v-model:value="profileForm.s3AccessKeyId"
              type="password"
              show-password-on="click"
              :placeholder="
                editingProfile?.has_secret ? '留空保留现有 Access Key' : '输入 Access Key ID'
              "
            />
          </n-form-item>
          <n-form-item label="Secret Access Key">
            <n-input
              v-model:value="profileForm.s3SecretAccessKey"
              type="password"
              show-password-on="click"
              :placeholder="
                editingProfile?.has_secret ? '留空保留现有 Secret Key' : '输入 Secret Access Key'
              "
            />
          </n-form-item>
        </template>

        <template v-if="profileForm.backend === 'webdav'">
          <n-form-item label="WebDAV 服务地址">
            <n-input
              v-model:value="profileForm.webdavBaseUrl"
              placeholder="https://dav.example.com/dav"
            />
          </n-form-item>
          <n-form-item label="专用根目录（可选）">
            <n-input v-model:value="profileForm.webdavRootPath" placeholder="astrhub/objects" />
          </n-form-item>
          <n-form-item label="认证方式">
            <n-select v-model:value="profileForm.webdavAuthType" :options="webdavAuthOptions" />
          </n-form-item>
          <template v-if="profileForm.webdavAuthType === 'basic'">
            <n-form-item label="用户名">
              <n-input
                v-model:value="profileForm.webdavUsername"
                :placeholder="
                  editingProfile?.has_secret ? '留空保留现有用户名' : '输入 WebDAV 用户名'
                "
              />
            </n-form-item>
            <n-form-item label="密码">
              <n-input
                v-model:value="profileForm.webdavPassword"
                type="password"
                show-password-on="click"
                :placeholder="editingProfile?.has_secret ? '留空保留现有密码' : '输入 WebDAV 密码'"
              />
            </n-form-item>
          </template>
          <n-form-item v-if="profileForm.webdavAuthType === 'bearer'" label="Bearer Token">
            <n-input
              v-model:value="profileForm.webdavToken"
              type="password"
              show-password-on="click"
              :placeholder="editingProfile?.has_secret ? '留空保留现有 Token' : '输入 Bearer Token'"
            />
          </n-form-item>
          <n-form-item label="允许内网地址或明文 HTTP">
            <n-switch v-model:value="profileForm.webdavAllowPrivateNetwork" />
          </n-form-item>
          <n-form-item label="校验证书（HTTPS）">
            <n-switch v-model:value="profileForm.webdavVerifyTls" />
          </n-form-item>
        </template>

        <template v-if="profileForm.backend === 'imgbed'">
          <n-form-item label="ImgBed 实例地址">
            <n-input
              v-model:value="profileForm.imgbedBaseUrl"
              placeholder="https://imgbed.example.com"
            />
          </n-form-item>
          <n-form-item label="专用上传目录（uploadFolder）">
            <n-input
              v-model:value="profileForm.imgbedUploadFolder"
              placeholder="astrhub/avatars 或 astrhub/plugins"
            />
          </n-form-item>
          <n-form-item label="文件有效期（expire，可选）">
            <n-input
              v-model:value="profileForm.imgbedExpire"
              placeholder="留空使用图床默认，如 12h、7d、forever"
            />
          </n-form-item>
          <n-form-item label="Token 权限范围">
            <n-select
              v-model:value="profileForm.imgbedScopes"
              multiple
              :options="imgbedScopeOptions"
              placeholder="头像仅需 upload；插件公开包需 upload + list + delete"
            />
          </n-form-item>
          <n-form-item label="Bearer Token">
            <n-input
              v-model:value="profileForm.imgbedToken"
              type="password"
              show-password-on="click"
              :placeholder="editingProfile?.has_secret ? '留空保留现有 Token' : '输入 ImgBed Token'"
            />
          </n-form-item>
          <n-form-item label="启用图片尺寸参数（64/128/256）">
            <n-switch v-model:value="profileForm.imgbedImageVariants" />
          </n-form-item>
          <n-form-item label="允许内网地址或明文 HTTP">
            <n-switch v-model:value="profileForm.imgbedAllowPrivateNetwork" />
          </n-form-item>
          <n-form-item label="校验证书（HTTPS）">
            <n-switch v-model:value="profileForm.imgbedVerifyTls" />
          </n-form-item>
        </template>

        <n-form-item v-if="profileForm.backend !== 'imgbed'" label="公开访问基址（可选）">
          <n-input
            v-model:value="profileForm.publicBaseUrl"
            placeholder="留空时由市场同源接口 /v1/storage/public/{id} 提供下载"
          />
        </n-form-item>
      </div>
      <template #footer>
        <div class="modal-footer-actions">
          <n-button @click="profileModalOpen = false">取消</n-button>
          <n-button
            type="primary"
            :loading="busyAction === 'save-profile'"
            :disabled="!canSubmitProfileForm"
            @click="saveProfile"
          >
            {{ editingProfile ? "保存修改" : "创建配置" }}
          </n-button>
        </div>
      </template>
    </n-modal>

    <n-modal
      v-model:show="bindConfirmOpen"
      preset="card"
      title="确认切换存储用途绑定"
      class="storage-modal storage-modal--compact"
    >
      <div v-if="pendingBindPurpose" class="confirm-body">
        <p>
          即将把 <strong>{{ purposeLabel(pendingBindPurpose) }}</strong> 的存储配置从
          <code>{{ currentBindingProfileId(pendingBindPurpose) || "未绑定" }}</code>
          切换为
          <code>{{ bindingDrafts[pendingBindPurpose] }}</code
          >。
        </p>
        <p>
          切换后，新的写入会立即路由到目标配置；源配置上已有的活动对象将自动进入后台迁移队列，并保留历史公开链接的兼容访问。
        </p>
      </div>
      <template #footer>
        <div class="modal-footer-actions">
          <n-button @click="bindConfirmOpen = false">取消</n-button>
          <n-button
            type="primary"
            :loading="Boolean(pendingBindPurpose && busyAction === `bind:${pendingBindPurpose}`)"
            @click="confirmBindPurpose"
          >
            确认切换
          </n-button>
        </div>
      </template>
    </n-modal>

    <n-modal
      v-model:show="decommissionConfirmOpen"
      preset="card"
      :title="decommissionAction === 'retire' ? '确认退役存储配置' : '确认移除存储配置'"
      class="storage-modal storage-modal--compact"
    >
      <div v-if="decommissionTarget" class="confirm-body">
        <p v-if="decommissionAction === 'retire'">
          退役后，市场将停止访问
          <code>{{ decommissionTarget.id }}</code>
          并立即撤销其本地加密凭据，但不会自动删除远端历史文件。
        </p>
        <p v-else>
          移除后，配置
          <code>{{ decommissionTarget.id }}</code> 的连接参数与凭据将被清除，仅保留审计记录。
        </p>
      </div>
      <template #footer>
        <div class="modal-footer-actions">
          <n-button @click="decommissionConfirmOpen = false">取消</n-button>
          <n-button
            :type="decommissionAction === 'retire' ? 'warning' : 'error'"
            :loading="
              Boolean(
                decommissionTarget &&
                busyAction === `${decommissionAction}:${decommissionTarget.id}`,
              )
            "
            @click="confirmDecommission"
          >
            {{ decommissionAction === "retire" ? "确认退役" : "确认移除" }}
          </n-button>
        </div>
      </template>
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef } from "vue";
import {
  NAlert,
  NButton,
  NEmpty,
  NFormItem,
  NIcon,
  NInput,
  NModal,
  NSelect,
  NSpin,
  NSwitch,
  NTag,
} from "naive-ui";
import {
  AddOutline,
  CheckmarkCircleOutline,
  CreateOutline,
  PauseOutline,
  PlayOutline,
  RefreshOutline,
  TrashOutline,
} from "@vicons/ionicons5";
import { usePluginStore } from "@/stores/plugins";
import type {
  LocalStorageMode,
  StorageBackend,
  StorageBindingItem,
  StorageMigrationDetail,
  StorageMigrationItem,
  StorageProfileCreatePayload,
  StorageProfileItem,
  StorageProfileTestResult,
  StorageProfileUpdatePayload,
  StoragePurpose,
} from "@/types/storage";

const props = defineProps<{
  apiBaseUrl?: string;
}>();

const pluginStore = usePluginStore();
const resolvedApiBase = computed(() =>
  props.apiBaseUrl !== undefined ? props.apiBaseUrl : pluginStore.apiBaseUrl,
);

const PURPOSE_META: Record<
  StoragePurpose,
  { label: string; description: string; allowedBackends: StorageBackend[] }
> = {
  "artifact-quarantine": {
    label: "隔离区与扫描内容",
    description: "保存上传待检插件包与解包扫描文本，禁止对外公开访问。",
    allowedBackends: ["local", "s3", "webdav"],
  },
  "artifact-published": {
    label: "已发布插件包",
    description: "保存通过审核的插件归档包，支持 CDN 或市场同源 Range 下载。",
    allowedBackends: ["local", "s3", "webdav", "imgbed"],
  },
  avatar: {
    label: "用户头像",
    description: "保存市场用户头像文件，后续支持接入图床后端。",
    allowedBackends: ["local", "s3", "webdav", "imgbed"],
  },
};

const BACKEND_LABELS: Record<StorageBackend, string> = {
  local: "本地与数据库（Local）",
  s3: "S3 / Cloudflare R2",
  webdav: "WebDAV",
  imgbed: "图床（ImgBed）",
};

const backendOptions = [
  { label: "本地文件系统 / SQLite / PostgreSQL", value: "local" },
  { label: "S3 / Cloudflare R2", value: "s3" },
  { label: "WebDAV 服务器", value: "webdav" },
  { label: "CloudFlare ImgBed 图床", value: "imgbed" },
];

const imgbedScopeOptions = [
  { label: "upload（上传）", value: "upload" },
  { label: "list（列举对账）", value: "list" },
  { label: "delete（撤回清理）", value: "delete" },
];

const webdavAuthOptions = [
  { label: "Basic 账号密码", value: "basic" },
  { label: "Bearer Token", value: "bearer" },
  { label: "无认证", value: "none" },
];

const localModeOptions = [
  { label: "本地文件系统目录", value: "filesystem" },
  { label: "独立 SQLite 数据库", value: "sqlite" },
  { label: "PostgreSQL 分块表", value: "postgresql" },
];

const loading = ref(false);
const busyAction = ref("");
const profiles = shallowRef<StorageProfileItem[]>([]);
const bindings = shallowRef<StorageBindingItem[]>([]);
const migrations = shallowRef<StorageMigrationItem[]>([]);
const bindingDrafts = reactive<Record<StoragePurpose, string>>({
  "artifact-quarantine": "",
  "artifact-published": "",
  avatar: "",
});
const feedback = reactive<{ type: "success" | "error" | "info" | "warning"; message: string }>({
  type: "info",
  message: "",
});

const expandedMigrationId = ref("");
const expandedMigrationDetail = shallowRef<StorageMigrationDetail | null>(null);

const profileModalOpen = ref(false);
const editingProfile = shallowRef<StorageProfileItem | null>(null);
const profileForm = reactive({
  id: "",
  name: "",
  backend: "local" as StorageBackend,
  localMode: "filesystem" as LocalStorageMode,
  localRoot: "",
  sqlitePath: "",
  pgUseMarketDb: true,
  pgSchema: "public",
  pgDsn: "",
  s3EndpointUrl: "",
  s3Region: "auto",
  s3Bucket: "",
  s3AccessKeyId: "",
  s3SecretAccessKey: "",
  webdavBaseUrl: "",
  webdavRootPath: "",
  webdavAuthType: "basic" as "basic" | "bearer" | "none",
  webdavUsername: "",
  webdavPassword: "",
  webdavToken: "",
  webdavAllowPrivateNetwork: false,
  webdavVerifyTls: true,
  imgbedBaseUrl: "",
  imgbedUploadFolder: "",
  imgbedExpire: "",
  imgbedScopes: ["upload"] as string[],
  imgbedToken: "",
  imgbedImageVariants: true,
  imgbedAllowPrivateNetwork: false,
  imgbedVerifyTls: true,
  publicBaseUrl: "",
});

const bindConfirmOpen = ref(false);
const pendingBindPurpose = ref<StoragePurpose | null>(null);

const decommissionConfirmOpen = ref(false);
const decommissionAction = ref<"retire" | "delete">("retire");
const decommissionTarget = shallowRef<StorageProfileItem | null>(null);

const groupedProfiles = computed(() => {
  const order: StorageBackend[] = ["local", "s3", "webdav", "imgbed"];
  return order
    .map((backend) => ({
      backend,
      label: BACKEND_LABELS[backend],
      items: profiles.value.filter((item) => item.backend === backend),
    }))
    .filter((group) => group.items.length > 0);
});

const canSubmitProfileForm = computed(() => {
  if (!editingProfile.value && !profileForm.id.trim()) return false;
  if (!profileForm.name.trim()) return false;
  if (profileForm.backend === "local") {
    if (profileForm.localMode === "filesystem") return Boolean(profileForm.localRoot.trim());
    if (profileForm.localMode === "sqlite") return Boolean(profileForm.sqlitePath.trim());
    if (profileForm.localMode === "postgresql") {
      if (!profileForm.pgUseMarketDb && !editingProfile.value?.has_secret) {
        return Boolean(profileForm.pgDsn.trim());
      }
      return Boolean(profileForm.pgSchema.trim());
    }
  }
  if (profileForm.backend === "s3") {
    const baseReady = Boolean(profileForm.s3EndpointUrl.trim() && profileForm.s3Bucket.trim());
    if (!baseReady) return false;
    if (!editingProfile.value?.has_secret) {
      return Boolean(profileForm.s3AccessKeyId.trim() && profileForm.s3SecretAccessKey.trim());
    }
    return true;
  }
  if (profileForm.backend === "webdav") {
    if (!profileForm.webdavBaseUrl.trim()) return false;
    if (profileForm.webdavAuthType === "basic" && !editingProfile.value?.has_secret) {
      return Boolean(profileForm.webdavUsername.trim() && profileForm.webdavPassword);
    }
    if (profileForm.webdavAuthType === "bearer" && !editingProfile.value?.has_secret) {
      return Boolean(profileForm.webdavToken.trim());
    }
    return true;
  }
  if (profileForm.backend === "imgbed") {
    if (!profileForm.imgbedBaseUrl.trim()) return false;
    if (!profileForm.imgbedScopes.includes("upload")) return false;
    if (!editingProfile.value?.has_secret) {
      return Boolean(profileForm.imgbedToken.trim());
    }
    return true;
  }
  return false;
});

function setFeedback(type: "success" | "error" | "info" | "warning", message: string) {
  feedback.type = type;
  feedback.message = message;
}

async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${resolvedApiBase.value}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const msg =
      (payload as { error?: string; detail?: string }).error ||
      (payload as { detail?: string }).detail ||
      `请求失败 (${response.status})`;
    throw new Error(msg);
  }
  return payload as T;
}

async function loadAll() {
  loading.value = true;
  try {
    const [profileRes, bindingRes, migrationRes] = await Promise.all([
      apiRequest<{ items: StorageProfileItem[] }>("/v1/admin/storage/profiles"),
      apiRequest<{ items: StorageBindingItem[] }>("/v1/admin/storage/bindings"),
      apiRequest<{ items: StorageMigrationItem[] }>("/v1/admin/storage/migrations"),
    ]);
    profiles.value = profileRes.items || [];
    bindings.value = bindingRes.items || [];
    migrations.value = migrationRes.items || [];
    for (const binding of bindings.value) {
      bindingDrafts[binding.purpose] = binding.profile_id || "";
    }
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "加载存储配置失败");
  } finally {
    loading.value = false;
  }
}

function purposeLabel(purpose: StoragePurpose): string {
  return PURPOSE_META[purpose]?.label || purpose;
}

function purposeDescription(purpose: StoragePurpose): string {
  return PURPOSE_META[purpose]?.description || "";
}

function profileOptionsForPurpose(purpose: StoragePurpose) {
  const allowed = PURPOSE_META[purpose]?.allowedBackends || [];
  return profiles.value
    .filter((item) => item.status === "active" && allowed.includes(item.backend))
    .map((item) => ({
      label: `${item.name} (${item.id})`,
      value: item.id,
    }));
}

function currentBindingProfileId(purpose: StoragePurpose): string {
  return bindings.value.find((item) => item.purpose === purpose)?.profile_id || "";
}

function statusLabel(status: StorageProfileItem["status"]): string {
  const map: Record<StorageProfileItem["status"], string> = {
    active: "活动中",
    draining: "迁移排空中",
    retired: "已退役",
    removed: "已移除",
  };
  return map[status] || status;
}

function statusTagType(status: StorageProfileItem["status"]) {
  if (status === "active") return "success";
  if (status === "draining") return "warning";
  return "default";
}

function healthLabel(health: string): string {
  if (health === "healthy") return "连接正常";
  if (health === "unhealthy") return "连接异常";
  if (health === "retired") return "已停止";
  if (health === "removed") return "已清除";
  return "未测试";
}

function healthTagType(health: string) {
  if (health === "healthy") return "success";
  if (health === "unhealthy") return "error";
  return "default";
}

function migrationStatusLabel(status: StorageMigrationItem["status"]): string {
  const map: Record<StorageMigrationItem["status"], string> = {
    queued: "排队中",
    running: "迁移中",
    paused: "已暂停",
    completed: "已完成",
    completed_with_errors: "部分失败",
    failed: "失败",
    cancelled: "已取消",
  };
  return map[status] || status;
}

function migrationTagType(status: StorageMigrationItem["status"]) {
  if (status === "completed") return "success";
  if (status === "running" || status === "queued") return "info";
  if (status === "paused" || status === "completed_with_errors") return "warning";
  return "error";
}

function summarizeProfileConfig(profile: StorageProfileItem): string {
  const cfg = profile.config || {};
  if (profile.backend === "local") {
    const mode = String(cfg.mode || "filesystem");
    if (mode === "sqlite") return `SQLite · ${String(cfg.db_path || "-")}`;
    if (mode === "postgresql") {
      const target = cfg.use_market_db ? "复用主库" : "独立实例";
      return `PostgreSQL (${target}) · schema=${String(cfg.schema || "public")}`;
    }
    return `目录 · ${String(cfg.root || "-")}`;
  }
  if (profile.backend === "s3") {
    return `Bucket=${String(cfg.bucket || "-")} · ${String(cfg.endpoint_url || "-")}`;
  }
  if (profile.backend === "webdav") {
    const root = String(cfg.root_path || "").trim();
    const base = String(cfg.base_url || "-");
    return root ? `WebDAV · ${base}/${root}` : `WebDAV · ${base}`;
  }
  if (profile.backend === "imgbed") {
    const folder = String(cfg.upload_folder || "").trim();
    const expire = String(cfg.expire || "").trim();
    const base = String(cfg.base_url || "-");
    const scopes = Array.isArray(cfg.scopes) ? cfg.scopes.join("+") : "upload";
    const loc = folder ? `${base}/file/${folder}` : `${base}/file`;
    const expiry = expire ? ` · expire=${expire}` : "";
    return `ImgBed · ${loc} · scopes=${scopes}${expiry}`;
  }
  return profile.backend;
}

function canDecommission(profile: StorageProfileItem): boolean {
  if (profile.status === "removed") return false;
  return (
    profile.bound_purposes.length === 0 &&
    profile.active_object_count === 0 &&
    profile.pending_object_count === 0 &&
    !profile.has_active_migrations
  );
}

function formatTime(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function resetProfileForm() {
  profileForm.id = "";
  profileForm.name = "";
  profileForm.backend = "local";
  profileForm.localMode = "filesystem";
  profileForm.localRoot = "";
  profileForm.sqlitePath = "";
  profileForm.pgUseMarketDb = true;
  profileForm.pgSchema = "public";
  profileForm.pgDsn = "";
  profileForm.s3EndpointUrl = "";
  profileForm.s3Region = "auto";
  profileForm.s3Bucket = "";
  profileForm.s3AccessKeyId = "";
  profileForm.s3SecretAccessKey = "";
  profileForm.webdavBaseUrl = "";
  profileForm.webdavRootPath = "";
  profileForm.webdavAuthType = "basic";
  profileForm.webdavUsername = "";
  profileForm.webdavPassword = "";
  profileForm.webdavToken = "";
  profileForm.webdavAllowPrivateNetwork = false;
  profileForm.webdavVerifyTls = true;
  profileForm.imgbedBaseUrl = "";
  profileForm.imgbedUploadFolder = "";
  profileForm.imgbedExpire = "";
  profileForm.imgbedScopes = ["upload"];
  profileForm.imgbedToken = "";
  profileForm.imgbedImageVariants = true;
  profileForm.imgbedAllowPrivateNetwork = false;
  profileForm.imgbedVerifyTls = true;
  profileForm.publicBaseUrl = "";
}

function openCreateModal() {
  editingProfile.value = null;
  resetProfileForm();
  profileModalOpen.value = true;
}

function openEditModal(profile: StorageProfileItem) {
  editingProfile.value = profile;
  resetProfileForm();
  const cfg = profile.config || {};
  profileForm.id = profile.id;
  profileForm.name = profile.name;
  profileForm.backend = profile.backend;
  profileForm.publicBaseUrl = String(cfg.public_base_url || "");
  if (profile.backend === "local") {
    profileForm.localMode = (String(cfg.mode || "filesystem") as LocalStorageMode) || "filesystem";
    profileForm.localRoot = String(cfg.root || "");
    profileForm.sqlitePath = String(cfg.db_path || "");
    profileForm.pgUseMarketDb = cfg.use_market_db !== false;
    profileForm.pgSchema = String(cfg.schema || "public");
  } else if (profile.backend === "s3") {
    profileForm.s3EndpointUrl = String(cfg.endpoint_url || "");
    profileForm.s3Region = String(cfg.region || "auto");
    profileForm.s3Bucket = String(cfg.bucket || "");
  } else if (profile.backend === "webdav") {
    profileForm.webdavBaseUrl = String(cfg.base_url || "");
    profileForm.webdavRootPath = String(cfg.root_path || "");
    const rawAuth = String(cfg.auth_type || "basic");
    profileForm.webdavAuthType = rawAuth === "bearer" || rawAuth === "none" ? rawAuth : "basic";
    profileForm.webdavAllowPrivateNetwork = Boolean(cfg.allow_private_network);
    profileForm.webdavVerifyTls = cfg.verify_tls !== false;
  } else if (profile.backend === "imgbed") {
    profileForm.imgbedBaseUrl = String(cfg.base_url || "");
    profileForm.imgbedUploadFolder = String(cfg.upload_folder || "");
    profileForm.imgbedExpire = String(cfg.expire || "");
    profileForm.imgbedScopes = Array.isArray(cfg.scopes)
      ? cfg.scopes.map((item) => String(item))
      : ["upload"];
    profileForm.imgbedImageVariants = cfg.image_variants_enabled !== false;
    profileForm.imgbedAllowPrivateNetwork = Boolean(cfg.allow_private_network);
    profileForm.imgbedVerifyTls = cfg.verify_tls !== false;
  }
  profileModalOpen.value = true;
}

function buildConfigAndSecret(): {
  config: Record<string, unknown>;
  secret: Record<string, string>;
} {
  const config: Record<string, unknown> = {
    public_base_url: profileForm.publicBaseUrl.trim(),
  };
  const secret: Record<string, string> = {};
  if (profileForm.backend === "local") {
    config.mode = profileForm.localMode;
    if (profileForm.localMode === "filesystem") {
      config.root = profileForm.localRoot.trim();
    } else if (profileForm.localMode === "sqlite") {
      config.db_path = profileForm.sqlitePath.trim();
    } else if (profileForm.localMode === "postgresql") {
      config.use_market_db = profileForm.pgUseMarketDb;
      config.schema = profileForm.pgSchema.trim() || "public";
      if (!profileForm.pgUseMarketDb && profileForm.pgDsn.trim()) {
        secret.dsn = profileForm.pgDsn.trim();
      }
    }
  } else if (profileForm.backend === "s3") {
    config.endpoint_url = profileForm.s3EndpointUrl.trim();
    config.region = profileForm.s3Region.trim() || "auto";
    config.bucket = profileForm.s3Bucket.trim();
    if (profileForm.s3AccessKeyId.trim()) {
      secret.access_key_id = profileForm.s3AccessKeyId.trim();
    }
    if (profileForm.s3SecretAccessKey.trim()) {
      secret.secret_access_key = profileForm.s3SecretAccessKey.trim();
    }
  } else if (profileForm.backend === "webdav") {
    config.base_url = profileForm.webdavBaseUrl.trim();
    config.root_path = profileForm.webdavRootPath.trim();
    config.auth_type = profileForm.webdavAuthType;
    config.allow_private_network = profileForm.webdavAllowPrivateNetwork;
    config.verify_tls = profileForm.webdavVerifyTls;
    if (profileForm.webdavAuthType === "basic") {
      if (profileForm.webdavUsername.trim()) {
        secret.username = profileForm.webdavUsername.trim();
      }
      if (profileForm.webdavPassword) {
        secret.password = profileForm.webdavPassword;
      }
    } else if (profileForm.webdavAuthType === "bearer" && profileForm.webdavToken.trim()) {
      secret.token = profileForm.webdavToken.trim();
    }
  } else if (profileForm.backend === "imgbed") {
    delete config.public_base_url;
    config.base_url = profileForm.imgbedBaseUrl.trim();
    config.upload_folder = profileForm.imgbedUploadFolder.trim();
    config.expire = profileForm.imgbedExpire.trim();
    config.scopes = [...profileForm.imgbedScopes];
    config.image_variants_enabled = profileForm.imgbedImageVariants;
    config.allow_private_network = profileForm.imgbedAllowPrivateNetwork;
    config.verify_tls = profileForm.imgbedVerifyTls;
    if (profileForm.imgbedToken.trim()) {
      secret.token = profileForm.imgbedToken.trim();
    }
  }
  return { config, secret };
}

async function saveProfile() {
  busyAction.value = "save-profile";
  try {
    const { config, secret } = buildConfigAndSecret();
    if (editingProfile.value) {
      const payload: StorageProfileUpdatePayload = {
        name: profileForm.name.trim(),
        config,
        ...(Object.keys(secret).length > 0 ? { secret } : {}),
      };
      await apiRequest(
        `/v1/admin/storage/profiles/${encodeURIComponent(editingProfile.value.id)}`,
        {
          method: "PATCH",
          body: JSON.stringify(payload),
        },
      );
      setFeedback("success", `已更新存储配置 ${editingProfile.value.id}。`);
    } else {
      const payload: StorageProfileCreatePayload = {
        id: profileForm.id.trim(),
        name: profileForm.name.trim(),
        backend: profileForm.backend,
        config,
        secret,
      };
      await apiRequest("/v1/admin/storage/profiles", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setFeedback("success", `已创建存储配置 ${payload.id}。`);
    }
    profileModalOpen.value = false;
    await loadAll();
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "保存存储配置失败");
  } finally {
    busyAction.value = "";
  }
}

async function testProfile(profile: StorageProfileItem) {
  busyAction.value = `test:${profile.id}`;
  try {
    const result = await apiRequest<StorageProfileTestResult>(
      `/v1/admin/storage/profiles/${encodeURIComponent(profile.id)}/test`,
      { method: "POST" },
    );
    setFeedback(
      result.ok ? "success" : "warning",
      `${profile.id} 测试${result.ok ? "通过" : "未通过"}：${result.health_message}`,
    );
    await loadAll();
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "存储连接测试失败");
  } finally {
    busyAction.value = "";
  }
}

function openBindConfirm(purpose: StoragePurpose) {
  pendingBindPurpose.value = purpose;
  bindConfirmOpen.value = true;
}

async function confirmBindPurpose() {
  const purpose = pendingBindPurpose.value;
  if (!purpose) return;
  const targetProfileId = bindingDrafts[purpose];
  busyAction.value = `bind:${purpose}`;
  try {
    const result = await apiRequest<{
      binding: StorageBindingItem;
      migration: StorageMigrationItem | null;
    }>(`/v1/admin/storage/bindings/${encodeURIComponent(purpose)}`, {
      method: "PUT",
      body: JSON.stringify({ profile_id: targetProfileId }),
    });
    bindConfirmOpen.value = false;
    pendingBindPurpose.value = null;
    if (result.migration) {
      setFeedback(
        "success",
        `已切换 ${purposeLabel(purpose)} 到 ${targetProfileId}，并创建迁移任务 ${result.migration.id}。`,
      );
    } else {
      setFeedback("success", `已将 ${purposeLabel(purpose)} 绑定到 ${targetProfileId}。`);
    }
    await loadAll();
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "切换存储用途绑定失败");
  } finally {
    busyAction.value = "";
  }
}

function openDecommissionConfirm(profile: StorageProfileItem, action: "retire" | "delete") {
  decommissionTarget.value = profile;
  decommissionAction.value = action;
  decommissionConfirmOpen.value = true;
}

async function confirmDecommission() {
  const target = decommissionTarget.value;
  if (!target) return;
  const action = decommissionAction.value;
  busyAction.value = `${action}:${target.id}`;
  try {
    if (action === "retire") {
      await apiRequest(`/v1/admin/storage/profiles/${encodeURIComponent(target.id)}/retire`, {
        method: "POST",
      });
      setFeedback("success", `存储配置 ${target.id} 已退役并撤销凭据。`);
    } else {
      await apiRequest(`/v1/admin/storage/profiles/${encodeURIComponent(target.id)}`, {
        method: "DELETE",
      });
      setFeedback("success", `存储配置 ${target.id} 已移除并保留审计记录。`);
    }
    decommissionConfirmOpen.value = false;
    decommissionTarget.value = null;
    await loadAll();
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "下线存储配置失败");
  } finally {
    busyAction.value = "";
  }
}

async function runMigrationAction(
  migrationId: string,
  action: "pause" | "resume" | "retry-failed" | "import-unknown",
) {
  const key = action === "retry-failed" ? `retry:${migrationId}` : `${action}:${migrationId}`;
  busyAction.value = key;
  try {
    await apiRequest(`/v1/admin/storage/migrations/${encodeURIComponent(migrationId)}/${action}`, {
      method: "POST",
      ...(action === "import-unknown" ? { body: JSON.stringify({}) } : {}),
    });
    const labels = {
      pause: "已暂停迁移任务",
      resume: "已恢复迁移任务",
      "retry-failed": "已将失败项重新入队",
      "import-unknown": "已将未登记对象纳入迁移队列",
    };
    setFeedback("success", `${labels[action]} ${migrationId}。`);
    await loadAll();
    if (expandedMigrationId.value === migrationId) {
      await fetchMigrationDetail(migrationId);
    }
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "迁移操作失败");
  } finally {
    busyAction.value = "";
  }
}

async function fetchMigrationDetail(migrationId: string) {
  busyAction.value = `detail:${migrationId}`;
  try {
    expandedMigrationDetail.value = await apiRequest<StorageMigrationDetail>(
      `/v1/admin/storage/migrations/${encodeURIComponent(migrationId)}`,
    );
  } catch (err) {
    setFeedback("error", err instanceof Error ? err.message : "加载迁移详情失败");
  } finally {
    busyAction.value = "";
  }
}

async function toggleMigrationDetail(migrationId: string) {
  if (expandedMigrationId.value === migrationId) {
    expandedMigrationId.value = "";
    expandedMigrationDetail.value = null;
    return;
  }
  expandedMigrationId.value = migrationId;
  await fetchMigrationDetail(migrationId);
}

onMounted(() => {
  void loadAll();
});
</script>

<style scoped>
.settings-section {
  padding: 22px;
  background: var(--bg-card);
  border: 1px solid var(--border-base);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
  min-width: 0;
}

.storage-panel {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 20px;
  min-width: 0;
}

.storage-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.storage-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.storage-feedback {
  margin-top: -4px;
}

.storage-subsection {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  margin-top: 12px;
  min-width: 0;
}

.storage-subsection h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.binding-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
  gap: 12px;
}

.binding-card,
.profile-card,
.migration-card {
  border: 1px solid var(--border-base);
  border-radius: 10px;
  padding: 14px;
  background: var(--bg-surface);
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  min-width: 0;
}

.binding-meta {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 4px;
  min-width: 0;
}

.binding-title {
  font-weight: 600;
  font-size: 14px;
}

.binding-desc,
.binding-foot,
.profile-summary,
.profile-meta-row,
.profile-health-message,
.migration-stats,
.storage-empty-inline {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.binding-controls {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.binding-foot,
.profile-meta-row,
.migration-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.backend-groups {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  min-width: 0;
}

.backend-group {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  min-width: 0;
}

.backend-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.backend-group-header h4 {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
}

.backend-group-count {
  font-size: 12px;
  color: var(--text-tertiary);
}

.profile-list,
.migration-list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  min-width: 0;
}

.profile-card {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 14px;
}

.profile-main {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
  min-width: 0;
}

.profile-title-row,
.migration-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.profile-title-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

.profile-id {
  font-family: var(--font-family-mono);
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.profile-actions,
.migration-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.migration-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  min-width: 0;
}

.migration-route {
  font-family: var(--font-family-mono);
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.migration-error {
  margin: 0;
  font-size: 12px;
  color: var(--error-color);
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.migration-detail {
  border-top: 1px solid var(--border-base);
  padding-top: 10px;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  font-size: 12px;
  min-width: 0;
  max-height: 360px;
  overflow-y: auto;
}

.detail-block ul {
  margin: 4px 0 0;
  padding-left: 18px;
}

.detail-block {
  min-width: 0;
}

.detail-block code,
.detail-block li {
  overflow-wrap: anywhere;
  word-break: break-word;
}

:global(.storage-modal) {
  width: min(560px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  display: flex;
  flex-direction: column;
}

:global(.storage-modal--compact) {
  width: min(480px, calc(100vw - 32px));
}

:global(.storage-modal > .n-card-header),
:global(.storage-modal > .n-card__footer) {
  flex-shrink: 0;
}

:global(.storage-modal > .n-card__content) {
  overflow-y: auto;
  min-height: 0;
}

:global(.storage-modal .modal-form) {
  display: grid;
  gap: 4px;
  min-width: 0;
  overflow-wrap: anywhere;
}

:global(.storage-modal .modal-footer-actions) {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  min-width: 0;
  overflow-wrap: anywhere;
}

:global(.storage-modal .confirm-body) {
  display: grid;
  gap: 8px;
  line-height: 1.6;
  min-width: 0;
  overflow-wrap: anywhere;
}

:global(.storage-modal .confirm-body p) {
  margin: 0;
}

@media (max-width: 760px) {
  .settings-section {
    padding: 16px;
  }
  .profile-card {
    grid-template-columns: minmax(0, 1fr);
  }

  .binding-controls {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
