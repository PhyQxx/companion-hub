<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

interface JobItem {
  id: string;
  kind: string;
  owner: string;
  status: string;
  priority: number;
  progress: number;
  current_step: string | null;
  resource_class: string;
  attempts: number;
  max_attempts: number;
  error_code: string | null;
  lease_owner: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const jobs = ref<JobItem[]>([]);
const loading = ref(false);
const filterStatus = ref("");
const filterKind = ref("");

const statusOptions = [
  { value: "", label: "全部状态" },
  { value: "queued", label: "队列中" },
  { value: "admitted", label: "已准入" },
  { value: "running", label: "运行中" },
  { value: "waiting_user", label: "等待用户" },
  { value: "retry_wait", label: "重试等待" },
  { value: "cancelling", label: "取消中" },
  { value: "succeeded", label: "成功" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
];

const statusTagType = (s: string): any => {
  const map: Record<string, any> = {
    queued: "info",
    admitted: "warning",
    running: "success",
    waiting_user: "warning",
    retry_wait: "warning",
    cancelling: "danger",
    succeeded: "success",
    failed: "danger",
    cancelled: "info",
  };
  return map[s] || "info";
};

const statusLabel = (s: string): string => {
  const opt = statusOptions.find((o) => o.value === s);
  return opt ? opt.label : s;
};

const stats = computed(() => {
  const total = jobs.value.length;
  const running = jobs.value.filter((j) => j.status === "running").length;
  const queued = jobs.value.filter((j) => j.status === "queued").length;
  const failed = jobs.value.filter((j) => j.status === "failed").length;
  const succeeded = jobs.value.filter((j) => j.status === "succeeded").length;
  return { total, running, queued, failed, succeeded };
});

async function loadJobs() {
  loading.value = true;
  try {
    const params = new URLSearchParams();
    if (filterStatus.value) params.set("status", filterStatus.value);
    if (filterKind.value) params.set("kind", filterKind.value);
    const res = await api.request<{ jobs: JobItem[]; total: number }>(
      `/api/v1/admin/jobs?${params.toString()}`
    );
    jobs.value = res.jobs;
  } catch (e) {
    emit("status", String(e), true);
  } finally {
    loading.value = false;
  }
}

async function cancelJob(id: string) {
  try {
    await ElMessageBox.confirm("确认取消该任务？", "取消任务", { type: "warning" });
    await api.request(`/api/v1/admin/jobs/${id}/cancel`, { method: "POST" });
    ElMessage.success("已请求取消");
    await loadJobs();
  } catch (e: any) {
    if (e !== "cancel") emit("status", String(e), true);
  }
}

async function expireLeases() {
  try {
    const res = await api.request<{ expired: number }>("/api/v1/admin/jobs/system/expire-leases", {
      method: "POST",
    });
    ElMessage.success(`已清理 ${res.expired} 条过期租约`);
    await loadJobs();
  } catch (e) {
    emit("status", String(e), true);
  }
}

onMounted(loadJobs);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <div class="stats-row">
        <div class="stat-card"><span class="stat-num">{{ stats.total }}</span><span class="stat-label">总任务</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#67c23a">{{ stats.running }}</span><span class="stat-label">运行中</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#409eff">{{ stats.queued }}</span><span class="stat-label">队列中</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#f56c6c">{{ stats.failed }}</span><span class="stat-label">失败</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#14c8c8">{{ stats.succeeded }}</span><span class="stat-label">成功</span></div>
      </div>
      <div class="filters">
        <el-select v-model="filterStatus" placeholder="状态筛选" clearable style="width:140px" @change="loadJobs">
          <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-input v-model="filterKind" placeholder="类型筛选" clearable style="width:160px" @keyup.enter="loadJobs" />
        <el-button @click="loadJobs">搜索</el-button>
        <el-button type="primary" @click="loadJobs">刷新</el-button>
        <el-button @click="expireLeases">清理过期租约</el-button>
      </div>
    </div>

    <el-table :data="jobs" v-loading="loading" stripe>
      <el-table-column prop="id" label="ID" width="200">
        <template #default="{ row }"><code class="id-code">{{ row.id.slice(0, 8) }}…</code></template>
      </el-table-column>
      <el-table-column prop="kind" label="类型" width="140" />
      <el-table-column prop="status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="priority" label="优先级" width="80" />
      <el-table-column prop="progress" label="进度" width="140">
        <template #default="{ row }">
          <el-progress :percentage="Math.round(row.progress * 100)" :status="row.status === 'failed' ? 'exception' : undefined" />
        </template>
      </el-table-column>
      <el-table-column prop="current_step" label="当前步骤" width="120">
        <template #default="{ row }">{{ row.current_step || "—" }}</template>
      </el-table-column>
      <el-table-column prop="attempts" label="尝试" width="80">
        <template #default="{ row }">{{ row.attempts }}/{{ row.max_attempts }}</template>
      </el-table-column>
      <el-table-column prop="lease_owner" label="持有者" width="120">
        <template #default="{ row }">{{ row.lease_owner || "—" }}</template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="160">
        <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button v-if="['queued','admitted','running','waiting_user','retry_wait','cancelling'].includes(row.status)"
            link type="danger" size="small" @click="cancelJob(row.id)">取消</el-button>
          <span v-else>—</span>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: grid; gap: 14px; margin-bottom: 16px; }
.stats-row { display: flex; gap: 12px; flex-wrap: wrap; }
.stat-card { min-width: 110px; padding: 12px 16px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); display: grid; gap: 4px; }
.stat-num { font-size: 22px; font-weight: 700; color: var(--text); }
.stat-label { font-size: 12px; color: var(--muted); }
.filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.id-code { font-size: 11px; color: #606c80; background: #f4f6f9; padding: 2px 6px; border-radius: 4px; }
</style>
