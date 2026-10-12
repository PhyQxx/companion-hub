<script setup lang="ts">
import { computed, inject, onActivated, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface HealthItem {
  status: string;
  version: string | null;
  reason_code: string | null;
  checked_at: string | null;
  latency_ms: number | null;
}

interface UsageItem {
  user_id: string;
  daily_count: number;
  active_count: number;
}

interface RecentRunItem {
  run_id: string | null;
  user_id: string | null;
  status: string;
  reason_code?: string | null;
  instruction_chars?: number | null;
  output_chars?: number | null;
  waited_for_approval?: boolean | null;
  timeout_seconds?: number | null;
}

interface StatusResponse {
  enabled: boolean;
  health: HealthItem;
  limits: Record<string, number>;
  usage: UsageItem[];
  recent_runs: RecentRunItem[];
}

interface HermesConfigShape {
  enabled: boolean;
  base_url: string | null;
  secret_ref: string | null;
  secret_configured?: boolean;
  allow_insecure_local_http: boolean;
  run_max_seconds: number;
  approval_wait_seconds: number;
  daily_run_limit: number;
  max_concurrent_runs: number;
  instruction_max_chars: number;
}

const loading = ref(false);
const status = ref<StatusResponse | null>(null);
const config = ref<HermesConfigShape | null>(null);
const form = reactive({ showSecret: false });

const healthTag = computed(() => {
  if (!status.value?.enabled) return "info";
  if (status.value.health.status === "ok") return "success";
  return "danger";
});

const healthText = computed(() => {
  if (!status.value) return "未加载";
  if (!status.value.enabled) return "未启用";
  if (status.value.health.status === "ok")
    return `正常${status.value.health.version ? ` · v${status.value.health.version}` : ""}`;
  return `不可达（${status.value.health.reason_code ?? "unknown"}）`;
});

const statusText = computed(() => {
  const map: Record<string, string> = {
    submitted: "已提交",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    stopped: "已停止",
    approval_timeout: "审批超时",
    submit_failed: "提交失败",
    waiting_for_approval: "等待审批",
  };
  return (raw: string) => map[raw] ?? raw;
});

async function load() {
  loading.value = true;
  try {
    const [statusData, configData] = await Promise.all([
      api.request<StatusResponse>("/api/v1/admin/hermes/status"),
      api.request<{ config: { hermes?: HermesConfigShape } }>(
        "/api/v1/admin/config/current",
      ),
    ]);
    status.value = statusData;
    config.value = configData.config.hermes ?? null;
  } catch (error) {
    emit("status", `加载 hermes 状态失败：${String(error)}`, true);
    ElMessage.error("加载 hermes 状态失败");
  } finally {
    loading.value = false;
  }
}

onActivated(load);
</script>

<template>
  <div class="hermes-view" v-loading="loading">
    <el-alert
      v-if="status && !status.enabled"
      title="Hermes 服务器代理未启用"
      description="在配置中开启 hermes.enabled 并提供转发地址与密钥后，Aria 可经确认的行动计划委派 NAS 上的 hermes 执行服务器任务。"
      type="info"
      :closable="false"
      class="block"
    />

    <el-row :gutter="12" class="block">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>连接健康</template>
          <el-tag :type="healthTag" effect="dark">{{ healthText }}</el-tag>
          <dl class="kv" v-if="status?.enabled">
            <div>
              <dt>最近探测</dt>
              <dd>{{ status.health.checked_at ?? "—" }}</dd>
            </div>
            <div>
              <dt>探测延迟</dt>
              <dd>{{ status.health.latency_ms != null ? `${Math.round(status.health.latency_ms)}ms` : "—" }}</dd>
            </div>
          </dl>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>额度与限流</template>
          <dl class="kv">
            <div>
              <dt>每日任务上限</dt>
              <dd>{{ status?.limits.daily_run_limit ?? "—" }}</dd>
            </div>
            <div>
              <dt>并发上限</dt>
              <dd>{{ status?.limits.max_concurrent_runs ?? "—" }}</dd>
            </div>
            <div>
              <dt>单任务超时</dt>
              <dd>{{ status?.limits.run_max_seconds ? `${status.limits.run_max_seconds}s` : "—" }}</dd>
            </div>
            <div>
              <dt>审批等待</dt>
              <dd>{{ status?.limits.approval_wait_seconds != null ? `${status.limits.approval_wait_seconds}s` : "—" }}</dd>
            </div>
          </dl>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>配置（只读）</template>
          <dl class="kv">
            <div>
              <dt>转发地址</dt>
              <dd>{{ config?.base_url ?? "—" }}</dd>
            </div>
            <div>
              <dt>密钥引用</dt>
              <dd>{{ config?.secret_ref ?? "内联注入" }}</dd>
            </div>
            <div>
              <dt>本地明文 HTTP</dt>
              <dd>{{ config?.allow_insecure_local_http ? "已开启（限私有地址）" : "关闭" }}</dd>
            </div>
            <div>
              <dt>指令长度上限</dt>
              <dd>{{ config?.instruction_max_chars ?? "—" }}</dd>
            </div>
          </dl>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="block" v-if="status?.usage?.length">
      <template #header>今日用量（按用户）</template>
      <el-table :data="status.usage" size="small">
        <el-table-column prop="user_id" label="用户" min-width="220" />
        <el-table-column prop="daily_count" label="今日任务数" width="110" />
        <el-table-column prop="active_count" label="进行中" width="90" />
      </el-table>
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>最近任务（进程内存，重启后为空；完整历史见「任务 → 后台任务」）</template>
      <el-table :data="status?.recent_runs ?? []" size="small" empty-text="暂无任务">
        <el-table-column label="run" min-width="140">
          <template #default="{ row }">{{ row.run_id ?? "—" }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="
                row.status === 'completed'
                  ? 'success'
                  : row.status === 'failed' || row.status === 'submit_failed'
                    ? 'danger'
                    : 'info'
              "
            >
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="审批" width="90">
          <template #default="{ row }">{{ row.waited_for_approval ? "曾挂起" : "—" }}</template>
        </el-table-column>
        <el-table-column prop="reason_code" label="原因" min-width="160" />
        <el-table-column prop="instruction_chars" label="指令长度" width="90" />
        <el-table-column prop="output_chars" label="结果长度" width="90" />
      </el-table>
    </el-card>

    <div class="actions">
      <el-button @click="load" :loading="loading">刷新</el-button>
    </div>
  </div>
</template>

<style scoped>
.hermes-view .block {
  margin-bottom: 12px;
}
.kv {
  margin: 0;
  display: grid;
  gap: 6px;
}
.kv div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
}
.kv dt {
  color: var(--el-text-color-secondary);
}
.kv dd {
  margin: 0;
  word-break: break-all;
  text-align: right;
}
.actions {
  display: flex;
  justify-content: flex-end;
}
</style>
