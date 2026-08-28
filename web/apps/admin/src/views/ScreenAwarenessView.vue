<script setup lang="ts">
import { inject, onMounted, ref, watch } from "vue";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "status" });

interface DisplayState {
  display: number;
  last_hash: string | null;
  last_analyzed_at: string | null;
  last_summary: string | null;
  consecutive_failures: number;
  cooldown_until: string | null;
}

interface StatusResponse {
  configured_enabled: boolean;
  interval_seconds: number;
  displays: number[];
  memory_enabled: boolean;
  proactive_enabled: boolean;
  loop_running: boolean;
  cycles: number;
  last_tick_at: string | null;
  last_error: string | null;
  display_states: DisplayState[];
}

interface ObservationItem {
  id: number;
  occurred_at: string;
  display: number;
  title: string;
  summary: string;
  importance: number;
  metadata: Record<string, unknown>;
}

const status = ref<StatusResponse | null>(null);
const observations = ref<ObservationItem[]>([]);
const loading = ref(false);
const observationLoading = ref(false);

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function loadStatus() {
  loading.value = true;
  try {
    status.value = await api.request<StatusResponse>("/api/v1/admin/screen-awareness/status");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "屏幕感知状态加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function loadObservations() {
  observationLoading.value = true;
  try {
    const result = await api.request<{ items: ObservationItem[]; total: number }>(
      "/api/v1/admin/screen-awareness/observations?limit=50",
    );
    observations.value = result.items;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "观察记录加载失败", true);
  } finally {
    observationLoading.value = false;
  }
}

async function refresh() {
  if (props.mode === "observations") await loadObservations();
  else await loadStatus();
  emit("status", "屏幕感知状态已刷新");
}

// KeepAlive 缓存组件：切 Tab 不重挂载，按 mode 变化拉取对应数据
watch(
  () => props.mode,
  async (mode) => {
    if (mode === "observations") await loadObservations();
    else await loadStatus();
  },
);

onMounted(refresh);
</script>

<template>
  <section class="content">
    <template v-if="props.mode !== 'observations'">
    <div class="hero panel">
      <div>
        <div class="eyebrow">屏幕感知 · SCREEN AWARENESS</div>
        <h2>周期屏幕观察</h2>
        <p>
          中枢按配置间隔截取各显示器，画面变化时用视觉模型生成摘要；全量进时间线，显著内容升级长期记忆并可主动发起话题。
          原图即焚不落盘；设备端 TCC / 锁屏 / 隐私暂停随时可停。
        </p>
      </div>
      <el-button :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <div class="stats">
      <article><span>配置开关</span><strong>{{ status?.configured_enabled ? "开启" : "关闭" }}</strong><small>config.screen_awareness</small></article>
      <article><span>采集间隔</span><strong>{{ status?.interval_seconds ?? "—" }}s</strong><small>显示器 {{ status?.displays?.join(" / ") ?? "—" }}</small></article>
      <article><span>循环状态</span><strong>{{ status?.loop_running ? "运行中" : "停止" }}</strong><small>已完成 {{ status?.cycles ?? 0 }} 轮</small></article>
      <article><span>记忆/主动</span><strong>{{ status?.memory_enabled ? "记忆" : "—" }}{{ status?.proactive_enabled ? " · 主动" : "" }}</strong><small>跟随配置</small></article>
    </div>

    <el-alert
      v-if="status?.last_error"
      :title="`最近一次循环错误：${status.last_error}`"
      type="warning"
      :closable="false"
      class="error-alert"
    />

    <div class="panel">
      <div class="panel-head">
        <div><h2>各显示器状态</h2><p>上次 tick：{{ fmt(status?.last_tick_at ?? null) }}</p></div>
      </div>
      <el-table v-loading="loading" :data="status?.display_states ?? []" empty-text="尚未采集（等待首个周期或开关未开启）" style="width:100%">
        <el-table-column label="显示器" width="90"><template #default="{ row }">{{ row.display }}</template></el-table-column>
        <el-table-column label="画面哈希" width="170"><template #default="{ row }"><code>{{ row.last_hash ?? "—" }}</code></template></el-table-column>
        <el-table-column label="最近分析" width="170"><template #default="{ row }">{{ fmt(row.last_analyzed_at) }}</template></el-table-column>
        <el-table-column label="最近摘要" min-width="300"><template #default="{ row }">{{ row.last_summary ?? "—" }}</template></el-table-column>
        <el-table-column label="连续失败" width="100"><template #default="{ row }">{{ row.consecutive_failures }}</template></el-table-column>
        <el-table-column label="冷却至" width="170"><template #default="{ row }">{{ fmt(row.cooldown_until) }}</template></el-table-column>
      </el-table>
    </div>
    </template>

    <template v-else>
    <div class="panel">
      <div class="panel-head">
        <div><h2>观察记录</h2><p>最近 50 条屏幕观察摘要（来源：时间线 screen.observed）。</p></div>
        <el-button size="small" :loading="observationLoading" @click="loadObservations">刷新记录</el-button>
      </div>
      <el-table v-loading="observationLoading" :data="observations" empty-text="还没有观察记录" style="width:100%">
        <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmt(row.occurred_at) }}</template></el-table-column>
        <el-table-column label="屏" width="70"><template #default="{ row }">{{ row.display }}</template></el-table-column>
        <el-table-column label="摘要" min-width="420"><template #default="{ row }">{{ row.summary }}</template></el-table-column>
        <el-table-column label="重要度" width="90"><template #default="{ row }">{{ row.importance.toFixed(2) }}</template></el-table-column>
      </el-table>
    </div>
    </template>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.stats { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 15px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.stats span, small { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 22px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.panel-head h2 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.error-alert { border-radius: 10px; }
@media (max-width: 1000px) {
  .stats { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
}
</style>
