<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  NAlert,
  NButton,
  NCheckbox,
  NCheckboxGroup,
  NDynamicTags,
  NEmpty,
  NFormItem,
  NIcon,
  NInput,
  NInputNumber,
  NSelect,
  NSpin,
  NSwitch,
  NTable,
  NTag,
} from "naive-ui";
import {
  AddOutline,
  ArchiveOutline,
  CheckmarkCircleOutline,
  RefreshOutline,
  ReturnUpBackOutline,
  TrashOutline,
} from "@vicons/ionicons5";
import type {
  ReviewOperationsResponse,
  ReviewPluginCategory,
  ReviewPolicyDiff,
  ReviewPolicyDocument,
  ReviewPolicyRecord,
  ReviewPolicySeverity,
  ReviewPolicyStage,
  ReviewToolFailureAction,
} from "@/types/artifacts";
import { cloneReviewPolicy, createDefaultReviewPolicy } from "@/utils/reviewPolicy";

const props = defineProps<{
  policies: ReviewPolicyRecord[];
  operations: ReviewOperationsResponse | null;
  lastDiff: ReviewPolicyDiff | null;
  loading: boolean;
  busy: boolean;
  isCoreAdmin: boolean;
  error?: string;
}>();

const emit = defineEmits<{
  refresh: [];
  create: [
    input: {
      version: string;
      policy: ReviewPolicyDocument;
      reason: string;
      basePolicyId?: string;
    },
  ];
  validate: [input: { policyId: string; reason: string }];
  activate: [input: { policyId: string; reason: string }];
  retire: [input: { policyId: string; reason: string }];
  rollback: [input: { policyId: string; reason: string }];
}>();

const selectedPolicyId = ref("");
const editor = ref<ReviewPolicyDocument>(createDefaultReviewPolicy());
const draftVersion = ref("");
const draftReason = ref("");
const transitionReason = ref("");

const selectedPolicy = computed(
  () => props.policies.find((item) => item.id === selectedPolicyId.value) ?? null,
);
const policyOptions = computed(() =>
  props.policies.map((item) => ({
    label: `${item.version} · ${statusLabel(item.status)}`,
    value: item.id,
  })),
);
const validationIssues = computed(() => selectedPolicy.value?.validation_summary.issues || []);

const stageOptions: Array<{ value: ReviewPolicyStage; label: string }> = [
  { value: "static", label: "静态扫描" },
  { value: "diff", label: "版本 Diff" },
  { value: "import_graph", label: "依赖图" },
  { value: "runtime", label: "Runtime" },
  { value: "category", label: "分类补全" },
  { value: "clamav", label: "ClamAV" },
  { value: "yara", label: "YARA" },
  { value: "dependency", label: "依赖风险" },
  { value: "llm_package", label: "LLM 包级" },
  { value: "llm_file", label: "LLM 文件级" },
  { value: "llm_summary", label: "LLM 汇总" },
];
const severityOptions: Array<{ value: ReviewPolicySeverity; label: string }> = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "critical", label: "Critical" },
];
const failureOptions: Array<{ value: ReviewToolFailureAction; label: string }> = [
  { value: "manual_review", label: "进入人工审查" },
  { value: "fail_closed", label: "阻断" },
];
const categoryOptions: Array<{ value: ReviewPluginCategory; label: string }> = [
  { value: "ai_tools", label: "AI 工具" },
  { value: "entertainment", label: "娱乐" },
  { value: "integrations", label: "集成" },
  { value: "productivity", label: "效率" },
  { value: "utilities", label: "实用工具" },
  { value: "other", label: "其他" },
];

watch(
  () => props.policies,
  (items) => {
    const current = items.find((item) => item.id === selectedPolicyId.value);
    const preferred = current || items.find((item) => item.status === "active") || items[0];
    selectedPolicyId.value = preferred?.id || "";
    loadEditor(preferred || null);
  },
  { immediate: true },
);

watch(selectedPolicyId, (policyId) => {
  loadEditor(props.policies.find((item) => item.id === policyId) || null);
});

function loadEditor(policy: ReviewPolicyRecord | null): void {
  editor.value = policy ? cloneReviewPolicy(policy.policy) : createDefaultReviewPolicy();
  if (props.isCoreAdmin) {
    draftVersion.value = policy ? `${policy.version}-next` : "";
  }
}

function addRuntimeTarget(): void {
  editor.value.runtime_targets.push({ astrbot: "4.26.6", python: "3.12" });
}

function removeRuntimeTarget(index: number): void {
  editor.value.runtime_targets.splice(index, 1);
}

function setLlmEnabled(enabled: boolean): void {
  editor.value.llm.enabled = enabled;
  if (enabled) {
    editor.value.llm.max_tokens ||= 24_000;
    editor.value.llm.max_cost_microusd ||= 100_000;
    editor.value.llm.input_cost_microusd_per_million_tokens ||= 1_000_000;
    editor.value.llm.output_cost_microusd_per_million_tokens ||= 4_000_000;
  } else {
    editor.value.llm.model = "";
    editor.value.llm.max_tokens = 0;
    editor.value.llm.max_cost_microusd = 0;
    editor.value.llm.input_cost_microusd_per_million_tokens = 0;
    editor.value.llm.output_cost_microusd_per_million_tokens = 0;
  }
}

function setCategoryEnabled(enabled: boolean): void {
  editor.value.category.enabled = enabled;
  if (!enabled) editor.value.category.model = "";
}

function createDraft(): void {
  const version = draftVersion.value.trim();
  if (!version) return;
  emit("create", {
    version,
    policy: cloneReviewPolicy(editor.value),
    reason: draftReason.value.trim(),
    ...(selectedPolicy.value ? { basePolicyId: selectedPolicy.value.id } : {}),
  });
}

function emitPolicyCommand(action: "validate" | "activate" | "retire" | "rollback"): void {
  const policyId = selectedPolicy.value?.id;
  if (!policyId) return;
  const reason = transitionReason.value.trim();
  if (action !== "validate" && !reason) return;
  emit(action, { policyId, reason });
}

function statusLabel(status: ReviewPolicyRecord["status"]): string {
  return { draft: "草稿", active: "生效", retired: "已退役" }[status];
}

function statusType(status: ReviewPolicyRecord["status"]): "default" | "success" | "warning" {
  return status === "active" ? "success" : status === "draft" ? "warning" : "default";
}

function healthType(status: string): "default" | "success" | "error" | "warning" {
  if (status === "ready" || status === "current") return "success";
  if (status === "degraded" || status === "stale") return "error";
  return "default";
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "-";
}

function formatDuration(milliseconds: number): string {
  return milliseconds >= 1000
    ? `${(milliseconds / 1000).toFixed(1)} s`
    : `${milliseconds.toFixed(0)} ms`;
}
</script>

<template>
  <section class="policy-panel" aria-labelledby="policy-panel-title">
    <header class="policy-panel__toolbar">
      <div>
        <p class="policy-panel__eyebrow">审查治理</p>
        <h2 id="policy-panel-title">策略与工具状态</h2>
      </div>
      <div class="policy-panel__toolbar-actions">
        <NSelect
          v-if="policyOptions.length"
          v-model:value="selectedPolicyId"
          class="policy-panel__selector"
          :options="policyOptions"
          aria-label="选择策略版本"
        />
        <NTag v-if="selectedPolicy" :type="statusType(selectedPolicy.status)" :bordered="false">
          {{ statusLabel(selectedPolicy.status) }}
        </NTag>
        <NButton
          quaternary
          circle
          :loading="loading"
          aria-label="刷新策略"
          @click="$emit('refresh')"
        >
          <template #icon
            ><NIcon><RefreshOutline /></NIcon
          ></template>
        </NButton>
      </div>
    </header>

    <NAlert v-if="error" type="error" :show-icon="false">{{ error }}</NAlert>

    <NSpin :show="loading">
      <template v-if="operations">
        <section class="policy-section" aria-labelledby="worker-health-title">
          <h3 id="worker-health-title">Worker</h3>
          <div class="worker-grid">
            <div v-for="worker in operations.health.workers" :key="worker.kind" class="worker-row">
              <div>
                <strong>{{
                  worker.kind === "artifact_worker" ? "Artifact Worker" : "Runtime Runner"
                }}</strong>
                <span>{{ worker.active_count }} / {{ worker.capacity }} 活跃</span>
              </div>
              <div class="worker-row__state">
                <NTag size="small" :type="healthType(worker.status)" :bordered="false">
                  {{ worker.status }}
                </NTag>
                <span>{{ worker.live_instances }} live · {{ worker.stale_instances }} stale</span>
                <span>{{ formatTime(worker.last_observed_at) }}</span>
              </div>
            </div>
          </div>
        </section>

        <section class="policy-section" aria-labelledby="tool-health-title">
          <h3 id="tool-health-title">工具健康</h3>
          <div class="table-scroll">
            <NTable size="small" :single-line="false">
              <thead>
                <tr>
                  <th>组件</th>
                  <th>状态</th>
                  <th>版本</th>
                  <th>数据新鲜度</th>
                  <th>观测时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="tool in operations.health.tools" :key="tool.name">
                  <td>{{ tool.name }}</td>
                  <td>
                    <NTag size="small" :type="healthType(tool.status)" :bordered="false">
                      {{ tool.status }}
                    </NTag>
                    <span v-if="tool.reasons[0]" class="cell-note">{{ tool.reasons[0] }}</span>
                  </td>
                  <td class="mono-cell">{{ tool.version || "-" }}</td>
                  <td>
                    <NTag size="small" :type="healthType(tool.freshness)" :bordered="false">
                      {{ tool.freshness }}
                    </NTag>
                    <span class="cell-note">{{ formatTime(tool.data_updated_at) }}</span>
                  </td>
                  <td>{{ formatTime(tool.observed_at) }}</td>
                </tr>
              </tbody>
            </NTable>
          </div>
        </section>

        <section class="policy-section" aria-labelledby="metrics-title">
          <h3 id="metrics-title">24 小时指标</h3>
          <div class="metrics-strip">
            <div>
              <span>人工等待</span
              ><strong>{{ operations.metrics.manual_wait.waiting_count }}</strong>
            </div>
            <div>
              <span>平均等待</span
              ><strong
                >{{
                  Math.round(operations.metrics.manual_wait.average_wait_seconds / 60)
                }}
                min</strong
              >
            </div>
            <div>
              <span>队列项</span
              ><strong>{{
                operations.metrics.queue.reduce((sum, item) => sum + item.count, 0)
              }}</strong>
            </div>
            <div>
              <span>路由决定</span
              ><strong>{{
                operations.metrics.routing.reduce((sum, item) => sum + item.count, 0)
              }}</strong>
            </div>
          </div>
          <div v-if="operations.metrics.stages.length" class="table-scroll">
            <NTable size="small">
              <thead>
                <tr>
                  <th>阶段</th>
                  <th>样本</th>
                  <th>失败</th>
                  <th>超时</th>
                  <th>平均</th>
                  <th>P95</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="stage in operations.metrics.stages" :key="stage.run_type">
                  <td>{{ stage.run_type }}</td>
                  <td>{{ stage.sample_count }}</td>
                  <td>{{ stage.failure_count }}</td>
                  <td>{{ stage.timeout_count }}</td>
                  <td>{{ formatDuration(stage.average_duration_ms) }}</td>
                  <td>{{ formatDuration(stage.p95_duration_ms) }}</td>
                </tr>
              </tbody>
            </NTable>
          </div>
        </section>
      </template>

      <section v-if="selectedPolicy" class="policy-section" aria-labelledby="validation-title">
        <div class="section-heading-row">
          <h3 id="validation-title">校验结果</h3>
          <NTag
            size="small"
            :type="selectedPolicy.validation_summary.valid ? 'success' : 'warning'"
            :bordered="false"
          >
            {{ selectedPolicy.validation_summary.valid ? "有效" : "待修正" }}
          </NTag>
        </div>
        <div v-if="validationIssues.length" class="validation-list">
          <div v-for="issue in validationIssues" :key="`${issue.path}:${issue.code}`">
            <code>{{ issue.path }}</code
            ><strong>{{ issue.code }}</strong
            ><span>{{ issue.message }}</span>
          </div>
        </div>
        <div v-if="lastDiff" class="diff-summary">
          <span>{{ lastDiff.path_count }} 项变更</span>
          <NTag size="small" :bordered="false">+{{ lastDiff.added_paths.length }}</NTag>
          <NTag size="small" :bordered="false">~{{ lastDiff.changed_paths.length }}</NTag>
          <NTag size="small" :bordered="false">-{{ lastDiff.removed_paths.length }}</NTag>
        </div>
      </section>

      <NEmpty v-if="!selectedPolicy && !isCoreAdmin" description="暂无生效策略" />

      <section class="policy-section policy-editor" aria-labelledby="policy-editor-title">
        <div class="section-heading-row">
          <h3 id="policy-editor-title">{{ isCoreAdmin ? "新策略草稿" : "生效策略快照" }}</h3>
          <NTag v-if="!isCoreAdmin" size="small" :bordered="false">只读</NTag>
        </div>

        <div v-if="isCoreAdmin" class="form-grid form-grid--wide">
          <NFormItem label="版本号">
            <NInput v-model:value="draftVersion" :disabled="busy" placeholder="policy-2026-07-v2" />
          </NFormItem>
          <NFormItem label="草稿原因">
            <NInput v-model:value="draftReason" :disabled="busy" maxlength="2000" />
          </NFormItem>
        </div>

        <div class="editor-band">
          <h4>强制阶段</h4>
          <NCheckboxGroup v-model:value="editor.required_stages" :disabled="!isCoreAdmin || busy">
            <div class="checkbox-grid">
              <NCheckbox v-for="stage in stageOptions" :key="stage.value" :value="stage.value">
                {{ stage.label }}
              </NCheckbox>
            </div>
          </NCheckboxGroup>
        </div>

        <div class="editor-band">
          <div class="section-heading-row">
            <h4>Runtime targets</h4>
            <NButton
              v-if="isCoreAdmin"
              quaternary
              circle
              :disabled="busy"
              aria-label="添加 Runtime target"
              @click="addRuntimeTarget"
            >
              <template #icon
                ><NIcon><AddOutline /></NIcon
              ></template>
            </NButton>
          </div>
          <div v-if="editor.runtime_targets.length" class="runtime-targets">
            <div
              v-for="(target, index) in editor.runtime_targets"
              :key="index"
              class="runtime-target-row"
            >
              <NInput
                v-model:value="target.astrbot"
                :disabled="!isCoreAdmin || busy"
                placeholder="AstrBot 4.26.6"
              />
              <NInput
                v-model:value="target.python"
                :disabled="!isCoreAdmin || busy"
                placeholder="Python 3.12"
              />
              <NButton
                v-if="isCoreAdmin"
                quaternary
                circle
                :disabled="busy"
                aria-label="删除 Runtime target"
                @click="removeRuntimeTarget(index)"
              >
                <template #icon
                  ><NIcon><TrashOutline /></NIcon
                ></template>
              </NButton>
            </div>
          </div>
        </div>

        <div class="editor-band">
          <h4>资源限制</h4>
          <div class="form-grid">
            <NFormItem label="CPU"
              ><NInputNumber
                v-model:value="editor.limits.cpu"
                :disabled="!isCoreAdmin || busy"
                :min="0.1"
                :max="16"
            /></NFormItem>
            <NFormItem label="内存 MB"
              ><NInputNumber
                v-model:value="editor.limits.memory_mb"
                :disabled="!isCoreAdmin || busy"
                :min="128"
            /></NFormItem>
            <NFormItem label="PIDs"
              ><NInputNumber
                v-model:value="editor.limits.pids"
                :disabled="!isCoreAdmin || busy"
                :min="16"
            /></NFormItem>
            <NFormItem label="超时秒"
              ><NInputNumber
                v-model:value="editor.limits.timeout_seconds"
                :disabled="!isCoreAdmin || busy"
                :min="10"
            /></NFormItem>
            <NFormItem label="磁盘 MB"
              ><NInputNumber
                v-model:value="editor.limits.disk_mb"
                :disabled="!isCoreAdmin || busy"
                :min="128"
            /></NFormItem>
            <NFormItem label="Tmpfs MB"
              ><NInputNumber
                v-model:value="editor.limits.tmpfs_mb"
                :disabled="!isCoreAdmin || busy"
                :min="16"
            /></NFormItem>
            <NFormItem label="日志字节"
              ><NInputNumber
                v-model:value="editor.limits.max_log_bytes"
                :disabled="!isCoreAdmin || busy"
                :min="1024"
            /></NFormItem>
          </div>
        </div>

        <div class="editor-band">
          <h4>网络 profile</h4>
          <div class="form-grid">
            <NFormItem label="Install"
              ><NInput
                v-model:value="editor.network_profiles.install"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
            <NFormItem label="Smoke"
              ><NInput
                v-model:value="editor.network_profiles.smoke"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
            <NFormItem label="无法验证">
              <NSelect
                v-model:value="editor.network_profiles.on_unverified"
                :disabled="!isCoreAdmin || busy"
                :options="failureOptions"
              />
            </NFormItem>
          </div>
        </div>

        <div class="editor-band">
          <div class="section-heading-row">
            <h4>LLM 审查</h4>
            <NSwitch
              :value="editor.llm.enabled"
              :disabled="!isCoreAdmin || busy"
              @update:value="setLlmEnabled"
            />
          </div>
          <div class="form-grid">
            <NFormItem label="模型"
              ><NInput
                v-model:value="editor.llm.model"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
            /></NFormItem>
            <NFormItem label="Prompt 版本"
              ><NInput
                v-model:value="editor.llm.prompt_version"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
            /></NFormItem>
            <NFormItem label="Token 预算"
              ><NInputNumber
                v-model:value="editor.llm.max_tokens"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="0"
            /></NFormItem>
            <NFormItem label="费用预算 µUSD"
              ><NInputNumber
                v-model:value="editor.llm.max_cost_microusd"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="0"
            /></NFormItem>
            <NFormItem label="输入 µUSD/M token"
              ><NInputNumber
                v-model:value="editor.llm.input_cost_microusd_per_million_tokens"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="0"
            /></NFormItem>
            <NFormItem label="输出 µUSD/M token"
              ><NInputNumber
                v-model:value="editor.llm.output_cost_microusd_per_million_tokens"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="0"
            /></NFormItem>
            <NFormItem label="最大文件"
              ><NInputNumber
                v-model:value="editor.llm.max_files"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="1"
            /></NFormItem>
            <NFormItem label="单文件字节"
              ><NInputNumber
                v-model:value="editor.llm.max_file_bytes"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="1024"
            /></NFormItem>
            <NFormItem label="超时秒"
              ><NInputNumber
                v-model:value="editor.llm.timeout_seconds"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="5"
            /></NFormItem>
            <NFormItem label="最大重试"
              ><NInputNumber
                v-model:value="editor.llm.max_retries"
                :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
                :min="0"
                :max="5"
            /></NFormItem>
          </div>
          <NFormItem label="强制文件"
            ><NDynamicTags
              v-model:value="editor.llm.required_files"
              :disabled="!isCoreAdmin || busy || !editor.llm.enabled"
          /></NFormItem>
        </div>

        <div class="editor-band">
          <h4>恶意软件扫描</h4>
          <div class="toggle-row">
            <label
              >ClamAV
              <NSwitch v-model:value="editor.malware.clamav" :disabled="!isCoreAdmin || busy"
            /></label>
          </div>
          <div class="form-grid">
            <NFormItem label="YARA ruleset"
              ><NInput
                :value="editor.malware.yara_ruleset || ''"
                :disabled="!isCoreAdmin || busy"
                @update:value="editor.malware.yara_ruleset = $event || null"
            /></NFormItem>
            <NFormItem label="病毒库最大小时"
              ><NInputNumber
                v-model:value="editor.malware.max_database_age_hours"
                :disabled="!isCoreAdmin || busy"
                :min="1"
            /></NFormItem>
            <NFormItem label="未知结果"
              ><NSelect
                v-model:value="editor.malware.on_unknown"
                :disabled="!isCoreAdmin || busy"
                :options="failureOptions"
            /></NFormItem>
            <NFormItem label="最大文件"
              ><NInputNumber
                v-model:value="editor.malware.max_files"
                :disabled="!isCoreAdmin || busy"
                :min="1"
            /></NFormItem>
            <NFormItem label="单文件字节"
              ><NInputNumber
                v-model:value="editor.malware.max_file_bytes"
                :disabled="!isCoreAdmin || busy"
                :min="1024"
            /></NFormItem>
            <NFormItem label="总字节"
              ><NInputNumber
                v-model:value="editor.malware.max_total_bytes"
                :disabled="!isCoreAdmin || busy"
                :min="1024"
            /></NFormItem>
            <NFormItem label="扫描超时秒"
              ><NInputNumber
                v-model:value="editor.malware.timeout_seconds"
                :disabled="!isCoreAdmin || busy"
                :min="5"
            /></NFormItem>
            <NFormItem label="单文件超时"
              ><NInputNumber
                v-model:value="editor.malware.per_file_timeout_seconds"
                :disabled="!isCoreAdmin || busy"
                :min="1"
            /></NFormItem>
            <NFormItem label="最大命中"
              ><NInputNumber
                v-model:value="editor.malware.max_matches"
                :disabled="!isCoreAdmin || busy"
                :min="1"
            /></NFormItem>
            <NFormItem label="每命中 offsets"
              ><NInputNumber
                v-model:value="editor.malware.max_offsets_per_match"
                :disabled="!isCoreAdmin || busy"
                :min="1"
            /></NFormItem>
            <NFormItem label="输出字节"
              ><NInputNumber
                v-model:value="editor.malware.max_output_bytes"
                :disabled="!isCoreAdmin || busy"
                :min="1024"
            /></NFormItem>
            <NFormItem label="子进程内存 MB"
              ><NInputNumber
                v-model:value="editor.malware.subprocess_memory_mb"
                :disabled="!isCoreAdmin || busy"
                :min="64"
            /></NFormItem>
          </div>
        </div>

        <div class="editor-band">
          <div class="section-heading-row">
            <h4>依赖风险</h4>
            <NSwitch v-model:value="editor.dependency.enabled" :disabled="!isCoreAdmin || busy" />
          </div>
          <div class="form-grid">
            <NFormItem label="阻断严重度"
              ><NSelect
                v-model:value="editor.dependency.max_severity"
                :disabled="!isCoreAdmin || busy"
                :options="severityOptions"
            /></NFormItem>
            <NFormItem label="数据最大小时"
              ><NInputNumber
                v-model:value="editor.dependency.max_data_age_hours"
                :disabled="!isCoreAdmin || busy"
                :min="1"
            /></NFormItem>
            <NFormItem label="不可用"
              ><NSelect
                v-model:value="editor.dependency.on_unavailable"
                :disabled="!isCoreAdmin || busy"
                :options="failureOptions"
            /></NFormItem>
            <NFormItem label="允许直链"
              ><NSwitch
                v-model:value="editor.dependency.allow_direct_urls"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
            <NFormItem label="允许 VCS"
              ><NSwitch
                v-model:value="editor.dependency.allow_vcs"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
          </div>
          <NFormItem label="禁止许可证"
            ><NDynamicTags
              v-model:value="editor.dependency.denied_licenses"
              :disabled="!isCoreAdmin || busy"
          /></NFormItem>
          <NFormItem label="私有包前缀"
            ><NDynamicTags
              v-model:value="editor.dependency.private_package_prefixes"
              :disabled="!isCoreAdmin || busy"
          /></NFormItem>
        </div>

        <div class="editor-band">
          <div class="section-heading-row">
            <h4>分类补全</h4>
            <NSwitch
              :value="editor.category.enabled"
              :disabled="!isCoreAdmin || busy"
              @update:value="setCategoryEnabled"
            />
          </div>
          <div class="form-grid">
            <NFormItem label="模型"
              ><NInput
                v-model:value="editor.category.model"
                :disabled="!isCoreAdmin || busy || !editor.category.enabled"
            /></NFormItem>
            <NFormItem label="最低置信度"
              ><NInputNumber
                v-model:value="editor.category.minimum_confidence"
                :disabled="!isCoreAdmin || busy"
                :min="0"
                :max="1"
                :step="0.05"
            /></NFormItem>
            <NFormItem label="默认分类"
              ><NSelect
                v-model:value="editor.category.default_category"
                :disabled="!isCoreAdmin || busy"
                :options="categoryOptions"
            /></NFormItem>
            <NFormItem label="输出 Token"
              ><NInputNumber
                v-model:value="editor.category.max_output_tokens"
                :disabled="!isCoreAdmin || busy"
                :min="64"
            /></NFormItem>
            <NFormItem label="最大输入字符"
              ><NInputNumber
                v-model:value="editor.category.max_input_chars"
                :disabled="!isCoreAdmin || busy"
                :min="1024"
            /></NFormItem>
            <NFormItem label="Prompt 版本"
              ><NInput
                v-model:value="editor.category.prompt_version"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
          </div>
          <NFormItem label="允许分类"
            ><NSelect
              v-model:value="editor.category.allowed_categories"
              multiple
              :disabled="!isCoreAdmin || busy"
              :options="categoryOptions"
          /></NFormItem>
        </div>

        <div class="editor-band">
          <h4>自动路由</h4>
          <div class="form-grid">
            <NFormItem label="自动通过"
              ><NSwitch
                v-model:value="editor.routing.auto_approve"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
            <NFormItem label="人工阈值"
              ><NSelect
                v-model:value="editor.routing.manual_review_at"
                :disabled="!isCoreAdmin || busy"
                :options="severityOptions"
            /></NFormItem>
            <NFormItem label="拒绝阈值"
              ><NSelect
                v-model:value="editor.routing.deterministic_reject_at"
                :disabled="!isCoreAdmin || busy"
                :options="severityOptions"
            /></NFormItem>
            <NFormItem label="工具降级"
              ><NSelect
                v-model:value="editor.routing.degraded_action"
                :disabled="!isCoreAdmin || busy"
                :options="failureOptions"
            /></NFormItem>
            <NFormItem label="完整覆盖"
              ><NSwitch
                v-model:value="editor.routing.require_complete_coverage"
                :disabled="!isCoreAdmin || busy"
            /></NFormItem>
          </div>
        </div>

        <div v-if="isCoreAdmin" class="policy-actions">
          <NButton
            type="primary"
            :disabled="!draftVersion.trim()"
            :loading="busy"
            @click="createDraft"
          >
            创建草稿
          </NButton>
          <NInput
            v-model:value="transitionReason"
            class="policy-actions__reason"
            :disabled="busy"
            maxlength="2000"
            placeholder="变更原因"
          />
          <NButton
            v-if="selectedPolicy?.status !== 'active'"
            :disabled="!selectedPolicy"
            :loading="busy"
            @click="emitPolicyCommand('validate')"
          >
            <template #icon
              ><NIcon><CheckmarkCircleOutline /></NIcon></template
            >校验
          </NButton>
          <NButton
            v-if="selectedPolicy?.status === 'draft'"
            type="success"
            :disabled="!transitionReason.trim()"
            :loading="busy"
            @click="emitPolicyCommand('activate')"
          >
            激活
          </NButton>
          <NButton
            v-if="selectedPolicy?.status === 'active'"
            type="warning"
            :disabled="!transitionReason.trim()"
            :loading="busy"
            @click="emitPolicyCommand('retire')"
          >
            <template #icon
              ><NIcon><ArchiveOutline /></NIcon></template
            >退役
          </NButton>
          <NButton
            v-if="selectedPolicy?.status === 'retired'"
            :disabled="!transitionReason.trim()"
            :loading="busy"
            @click="emitPolicyCommand('rollback')"
          >
            <template #icon
              ><NIcon><ReturnUpBackOutline /></NIcon></template
            >回滚
          </NButton>
        </div>
      </section>
    </NSpin>
  </section>
</template>

<style scoped>
.policy-panel {
  display: grid;
  gap: 18px;
  min-width: 0;
}
.policy-panel__toolbar,
.section-heading-row,
.policy-actions,
.toggle-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.policy-panel__toolbar {
  justify-content: space-between;
  min-height: 52px;
}
.policy-panel__toolbar h2,
.policy-panel__toolbar p,
.policy-section h3,
.editor-band h4 {
  margin: 0;
}
.policy-panel__eyebrow {
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 700;
}
.policy-panel__toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.policy-panel__selector {
  width: min(360px, 42vw);
}
.policy-section {
  display: grid;
  gap: 14px;
  padding: 18px 0;
  border-top: 1px solid var(--border-base);
}
.section-heading-row {
  justify-content: space-between;
}
.worker-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border: 1px solid var(--border-base);
  border-radius: 6px;
}
.worker-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
}
.worker-row + .worker-row {
  border-left: 1px solid var(--border-base);
}
.worker-row > div,
.worker-row__state {
  display: grid;
  gap: 4px;
}
.worker-row span,
.cell-note {
  color: var(--text-secondary);
  font-size: 12px;
}
.worker-row__state {
  justify-items: end;
  text-align: right;
}
.table-scroll {
  min-width: 0;
  overflow-x: auto;
}
.table-scroll table {
  min-width: 720px;
}
.cell-note {
  display: block;
  margin-top: 3px;
}
.mono-cell,
.validation-list code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.metrics-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  border: 1px solid var(--border-base);
  border-radius: 6px;
}
.metrics-strip > div {
  display: grid;
  gap: 4px;
  padding: 14px;
}
.metrics-strip > div + div {
  border-left: 1px solid var(--border-base);
}
.metrics-strip span {
  color: var(--text-secondary);
  font-size: 12px;
}
.metrics-strip strong {
  font-size: 22px;
}
.validation-list {
  display: grid;
  gap: 8px;
}
.validation-list > div {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) minmax(140px, 1fr) minmax(220px, 2fr);
  gap: 10px;
  padding: 10px 12px;
  border-left: 3px solid var(--warning-color);
  background: color-mix(in srgb, var(--warning-color) 7%, transparent);
}
.diff-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.policy-editor {
  padding-bottom: 40px;
}
.editor-band {
  display: grid;
  gap: 12px;
  padding: 16px 0;
  border-top: 1px dashed var(--border-base);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(150px, 1fr));
  gap: 0 14px;
}
.form-grid--wide {
  grid-template-columns: minmax(220px, 1fr) minmax(280px, 2fr);
}
.checkbox-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 10px;
}
.runtime-targets {
  display: grid;
  gap: 8px;
}
.runtime-target-row {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(140px, 1fr) 34px;
  gap: 8px;
}
.policy-actions {
  position: sticky;
  bottom: 0;
  z-index: 4;
  flex-wrap: wrap;
  padding: 12px;
  border: 1px solid var(--border-base);
  border-radius: 6px;
  background: color-mix(in srgb, var(--card-color) 96%, transparent);
}
.policy-actions__reason {
  min-width: 240px;
  flex: 1;
}
@media (max-width: 900px) {
  .worker-grid,
  .metrics-strip,
  .form-grid,
  .checkbox-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .worker-row + .worker-row {
    border-top: 1px solid var(--border-base);
    border-left: 0;
  }
  .validation-list > div {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 600px) {
  .policy-panel__toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
  }
  .policy-panel__toolbar-actions {
    display: grid;
    width: 100%;
    grid-template-columns: minmax(0, 1fr) auto auto;
    justify-content: stretch;
  }
  .policy-panel__selector {
    width: 100%;
    min-width: 0;
  }
  .worker-grid,
  .metrics-strip,
  .form-grid,
  .form-grid--wide,
  .checkbox-grid {
    grid-template-columns: 1fr;
  }
  .metrics-strip > div + div {
    border-top: 1px solid var(--border-base);
    border-left: 0;
  }
  .runtime-target-row {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 34px;
  }
  .worker-row {
    display: grid;
  }
  .worker-row__state {
    justify-items: start;
    text-align: left;
  }
}
</style>
