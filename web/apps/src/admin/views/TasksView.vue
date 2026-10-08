<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

interface TaskTrigger {
  type: "time" | "event";
  at: string | null;
  repeat_kind: string;
  interval_minutes: number | null;
  weekdays: number[] | null;
  event_type: string | null;
  cooldown_seconds: number | null;
}

interface TaskItem {
  id: string;
  user_id: string;
  kind: string;
  title: string;
  notes: string | null;
  status: string;
  trigger: TaskTrigger;
  next_fire_at: string | null;
  last_fired_at: string | null;
  fire_count: number;
  last_delivery: Record<string, unknown> | null;
  privacy_level: string;
  source: string;
  source_ref: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const tasks = ref<TaskItem[]>([]);
const loading = ref(false);
const counts = ref<Record<string, number>>({});
const filterStatus = ref("");
const filterKind = ref("");
const filterSource = ref("");
const filterQuery = ref("");

const statusOptions = [
  { value: "", label: "全部状态" },
  { value: "active", label: "待触发" },
  { value: "firing", label: "投递中" },
  { value: "done", label: "已完成" },
  { value: "cancelled", label: "已取消" },
];

const kindOptions = [
  { value: "", label: "全部类型" },
  { value: "reminder", label: "提醒" },
  { value: "task", label: "任务" },
];

const sourceOptions = [
  { value: "", label: "全部来源" },
  { value: "manual", label: "手动" },
  { value: "chat", label: "聊天" },
  { value: "calendar", label: "日历" },
  { value: "commute", label: "通勤" },
  { value: "meeting", label: "会议" },
  { value: "pnkx", label: "pnkx 镜像" },
];

const repeatLabels: Record<string, string> = {
  once: "单次",
  daily: "每天",
  weekdays: "工作日",
  weekly: "每周",
  interval_minutes: "周期",
};

const statusTagType = (s: string): any => {
  const map: Record<string, any> = {
    active: "primary",
    firing: "warning",
    done: "success",
    cancelled: "info",
  };
  return map[s] || "info";
};

const statusLabel = (s: string): string => {
  const opt = statusOptions.find((o) => o.value === s);
  return opt ? opt.label : s;
};

const kindLabel = (k: string): string => {
  const opt = kindOptions.find((o) => o.value === k);
  return opt ? opt.label : k;
};

const stats = computed(() => ({
  total: Object.values(counts.value).reduce((acc, n) => acc + n, 0),
  active: counts.value["active"] ?? 0,
  firing: counts.value["firing"] ?? 0,
  done: counts.value["done"] ?? 0,
  cancelled: counts.value["cancelled"] ?? 0,
}));

function triggerSummary(t: TaskTrigger): string {
  if (t.type === "event") {
    return `事件 · ${t.event_type ?? "?"}`;
  }
  const label = repeatLabels[t.repeat_kind] ?? t.repeat_kind;
  if (t.repeat_kind === "interval_minutes" && t.interval_minutes) {
    return `${label} ${t.interval_minutes} 分钟`;
  }
  if (t.repeat_kind === "weekly" && t.weekdays?.length) {
    const names = ["一", "二", "三", "四", "五", "六", "日"];
    return `${label} ${t.weekdays.map((d) => names[d]).join("/")}`;
  }
  return label;
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

const page = ref(1);
const pageSize = ref(20);
const total = ref(0);

async function loadTasks() {
  loading.value = true;
  try {
    const params = new URLSearchParams({
      limit: String(pageSize.value),
      offset: String((page.value - 1) * pageSize.value),
    });
    if (filterStatus.value) params.set("status", filterStatus.value);
    if (filterKind.value) params.set("kind", filterKind.value);
    if (filterSource.value) params.set("source", filterSource.value);
    if (filterQuery.value.trim()) params.set("query", filterQuery.value.trim());
    const res = await api.request<{ items: TaskItem[]; total: number; counts: Record<string, number> }>(
      `/api/v1/admin/tasks?${params.toString()}`
    );
    tasks.value = res.items;
    total.value = res.total;
    counts.value = res.counts;
  } catch (e) {
    emit("status", String(e), true);
  } finally {
    loading.value = false;
  }
}

function onPageChange() {
  void loadTasks();
}

function onFilterChange() {
  page.value = 1;
  void loadTasks();
}

async function cancelTask(row: TaskItem) {
  try {
    await ElMessageBox.confirm(`确认取消任务「${row.title}」？`, "取消任务", { type: "warning" });
    await api.request(`/api/v1/admin/tasks/${row.id}/cancel?user_id=${row.user_id}`, { method: "POST" });
    ElMessage.success("已取消");
    await loadTasks();
  } catch (e: any) {
    if (e !== "cancel") emit("status", String(e), true);
  }
}

onMounted(loadTasks);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <div class="stats-row">
        <div class="stat-card"><span class="stat-num">{{ stats.total }}</span><span class="stat-label">总任务</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#409eff">{{ stats.active }}</span><span class="stat-label">待触发</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#e6a23c">{{ stats.firing }}</span><span class="stat-label">投递中</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#67c23a">{{ stats.done }}</span><span class="stat-label">已完成</span></div>
        <div class="stat-card"><span class="stat-num" style="color:#909399">{{ stats.cancelled }}</span><span class="stat-label">已取消</span></div>
      </div>
      <div class="filters">
        <el-select v-model="filterStatus" placeholder="状态筛选" clearable style="width:130px" @change="onFilterChange">
          <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-select v-model="filterKind" placeholder="类型筛选" clearable style="width:110px" @change="onFilterChange">
          <el-option v-for="o in kindOptions" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-select v-model="filterSource" placeholder="来源筛选" clearable style="width:120px" @change="onFilterChange">
          <el-option v-for="o in sourceOptions" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-input v-model="filterQuery" placeholder="标题搜索" clearable style="width:180px" @keyup.enter="onFilterChange" />
        <el-button @click="onFilterChange">搜索</el-button>
        <el-button type="primary" @click="loadTasks">刷新</el-button>
      </div>
    </div>

    <el-table :data="tasks" v-loading="loading" stripe>
      <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
      <el-table-column label="类型" width="80">
        <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="96">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="触发" width="150">
        <template #default="{ row }">
          <span>{{ triggerSummary(row.trigger) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="next_fire_at" label="下次触发" width="160">
        <template #default="{ row }">{{ formatTime(row.next_fire_at) }}</template>
      </el-table-column>
      <el-table-column prop="last_fired_at" label="上次触发" width="160">
        <template #default="{ row }">{{ formatTime(row.last_fired_at) }}</template>
      </el-table-column>
      <el-table-column prop="fire_count" label="次数" width="70" />
      <el-table-column prop="source" label="来源" width="90">
        <template #default="{ row }">
          <el-tag size="small" type="info" effect="plain">{{ row.source }}{{ row.source_ref ? " ·" : "" }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button v-if="['active', 'firing'].includes(row.status)" link type="danger" size="small" @click="cancelTask(row as TaskItem)">取消</el-button>
          <span v-else>—</span>
        </template>
      </el-table-column>
    </el-table>
    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="onPageChange"
        @size-change="onPageChange"
      />
    </div>
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
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
