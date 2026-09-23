<script setup lang="ts">
import { computed, inject, onActivated, onDeactivated, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const route = useRoute();
const router = useRouter();
// 单 Tab 内的「运行状态 / 观察记录」切换，section 持久化在 URL 上
type ScreenSection = "status" | "records";
const section = ref<ScreenSection>(route.query.section === "records" ? "records" : "status");

function switchSection(next: string | number | boolean | undefined) {
  section.value = next === "records" ? "records" : "status";
}

watch(section, (value) => {
  void router.replace({ query: { ...route.query, section: value === "records" ? "records" : undefined } });
});

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

interface DeviceItem {
  id: string;
  name: string;
  alias: string | null;
  client_type: string;
  effective_capabilities: string[];
  revoked_at: string | null;
  online: boolean;
}

interface ScreenAwarenessConfig {
  enabled: boolean;
  interval_seconds: number;
  displays: number[];
  analysis_prompt: string;
  memory_enabled: boolean;
  proactive_enabled: boolean;
  unchanged_skip_threshold: number;
}

interface CurrentConfig {
  version: number;
  config: Record<string, unknown> & { screen_awareness?: ScreenAwarenessConfig };
}

const status = ref<StatusResponse | null>(null);
const observations = ref<ObservationItem[]>([]);
const devices = ref<DeviceItem[]>([]);
const observationTotal = ref(0);
const observationPage = ref(1);
const observationPageSize = ref(20);
const loading = ref(false);
const observationLoading = ref(false);
const deviceLoading = ref(false);
const savingConfig = ref(false);
let statusRefreshTimer: ReturnType<typeof window.setInterval> | null = null;
const displayOptions = Array.from({ length: 32 }, (_, index) => index + 1);

const onlineDesktopDevices = computed(() =>
  devices.value.filter(
    (device) => device.client_type === "desktop" && device.online && !device.revoked_at,
  ),
);
const screenMonitorDevices = computed(() =>
  onlineDesktopDevices.value.filter((device) =>
    device.effective_capabilities.includes("screen.monitor"),
  ),
);
const screenDeviceLabel = computed(() => {
  if (screenMonitorDevices.value.length) return `${screenMonitorDevices.value.length} 台就绪`;
  if (onlineDesktopDevices.value.length) return "在线但未就绪";
  return "未连接";
});
const screenDeviceHint = computed(() => {
  if (screenMonitorDevices.value.length) {
    return screenMonitorDevices.value
      .map((device) => device.alias || device.name)
      .join("、");
  }
  if (onlineDesktopDevices.value.length) {
    return "检查屏幕录制权限、锁屏或隐私暂停";
  }
  return "等待 Aria Desktop 连接 Hub";
});

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
      `/api/v1/admin/screen-awareness/observations?limit=${observationPageSize.value}`
        + `&offset=${(observationPage.value - 1) * observationPageSize.value}`,
    );
    observations.value = result.items;
    observationTotal.value = result.total;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "观察记录加载失败", true);
  } finally {
    observationLoading.value = false;
  }
}

function onObservationPageChange() {
  void loadObservations();
}

async function loadDevices() {
  deviceLoading.value = true;
  try {
    devices.value = await api.request<DeviceItem[]>("/api/v1/admin/devices");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "桌面设备状态加载失败", true);
  } finally {
    deviceLoading.value = false;
  }
}

async function updateScreenAwareness(
  patch: Partial<ScreenAwarenessConfig>,
  successMessage: string,
) {
  savingConfig.value = true;
  try {
    const current = await api.request<CurrentConfig>("/api/v1/admin/config/current");
    const config = structuredClone(current.config);
    const existing = config.screen_awareness;
    if (!existing) throw new Error("当前配置缺少 screen_awareness");
    config.screen_awareness = { ...existing, ...patch };
    const saved = await api.request<CurrentConfig>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    await loadStatus();
    emit("status", `${successMessage}，配置版本 ${saved.version}`);
    ElMessage.success(successMessage);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "屏幕感知配置保存失败", true);
    ElMessage.error("屏幕感知配置保存失败");
    await loadStatus();
  } finally {
    savingConfig.value = false;
  }
}

async function toggleEnabled(value: string | number | boolean) {
  const enabled = Boolean(value);
  await updateScreenAwareness(
    { enabled },
    enabled ? "屏幕感知已开启，最多等待一个采集周期" : "屏幕感知已关闭",
  );
}

async function updateDisplays(value: unknown) {
  const displays = Array.isArray(value)
    ? [...new Set(value.map(Number).filter((item) => Number.isInteger(item) && item >= 1 && item <= 32))]
    : [];
  if (!displays.length) {
    ElMessage.warning("至少选择一块显示器");
    await loadStatus();
    return;
  }
  if (displays.length > 8) {
    ElMessage.warning("最多选择 8 块显示器");
    await loadStatus();
    return;
  }
  await updateScreenAwareness(
    { displays: displays.sort((left, right) => left - right) },
    `采集显示器已更新为 ${displays.join("、")}`,
  );
}

async function refresh() {
  if (section.value === "records") {
    observationPage.value = 1;
    await loadObservations();
  } else {
    await Promise.all([loadStatus(), loadDevices()]);
  }
  emit("status", "屏幕感知状态已刷新");
}

// KeepAlive 缓存组件：切 Tab 不重挂载，按 section 变化拉取对应数据
watch(section, async () => {
  if (section.value === "records") {
    observationPage.value = 1;
    await loadObservations();
  } else {
    await Promise.all([loadStatus(), loadDevices()]);
  }
});

onActivated(() => {
  void refresh();
  statusRefreshTimer = window.setInterval(() => {
    if (section.value !== "records") void Promise.all([loadStatus(), loadDevices()]);
  }, 15_000);
});

onDeactivated(() => {
  if (statusRefreshTimer !== null) window.clearInterval(statusRefreshTimer);
  statusRefreshTimer = null;
});
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">屏幕感知 · SCREEN AWARENESS</div>
        <h2>周期屏幕观察</h2>
        <p>
          中枢按配置间隔截取各显示器，画面变化时用视觉模型生成摘要；全量进时间线，显著内容升级长期记忆并可主动发起话题。
          原图即焚不落盘；设备端 TCC / 锁屏 / 隐私暂停随时可停。
        </p>
      </div>
      <div class="hero-actions">
        <el-radio-group :model-value="section" size="small" @change="switchSection">
          <el-radio-button value="status">运行状态</el-radio-button>
          <el-radio-button value="records">观察记录</el-radio-button>
        </el-radio-group>
        <el-button :loading="loading || deviceLoading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <template v-if="section === 'status'">
    <div class="stats">
      <article class="switch-stat">
        <div><span>配置开关</span><el-switch :model-value="status?.configured_enabled ?? false" :loading="savingConfig" :disabled="!status || savingConfig" @change="toggleEnabled" /></div>
        <strong>{{ status?.configured_enabled ? "开启" : "关闭" }}</strong>
        <small>保存后立即热生效</small>
      </article>
      <article class="display-stat">
        <span>采集显示器</span>
        <el-select :model-value="status?.displays ?? []" multiple collapse-tags collapse-tags-tooltip :max-collapse-tags="3" placeholder="选择显示器" :disabled="!status || savingConfig" @change="updateDisplays">
          <el-option v-for="display in displayOptions" :key="display" :label="`显示器 ${display}`" :value="display" />
        </el-select>
        <small>最多选择 8 块 · 当前间隔 {{ status?.interval_seconds ?? "—" }}s</small>
      </article>
      <article><span>循环状态</span><strong>{{ status?.loop_running ? "运行中" : "停止" }}</strong><small>已完成 {{ status?.cycles ?? 0 }} 轮</small></article>
      <article><span>记忆/主动</span><strong>{{ status?.memory_enabled ? "记忆" : "—" }}{{ status?.proactive_enabled ? " · 主动" : "" }}</strong><small>跟随配置</small></article>
      <article><span>采集设备</span><strong>{{ screenDeviceLabel }}</strong><small>{{ screenDeviceHint }}</small></article>
    </div>

    <el-alert
      v-if="onlineDesktopDevices.length && !screenMonitorDevices.length"
      title="桌面端已连接，但尚未开放 screen.monitor；请检查 macOS 屏幕录制权限、锁屏状态和隐私暂停。"
      type="warning"
      :closable="false"
      class="error-alert"
    />
    <el-alert
      v-else-if="status?.configured_enabled && !onlineDesktopDevices.length"
      title="屏幕感知已开启，但当前没有在线的 Aria Desktop。"
      type="warning"
      :closable="false"
      class="error-alert"
    />

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
        <div><h2>观察记录</h2><p>屏幕观察摘要（来源：时间线 screen.observed）。</p></div>
        <el-button size="small" :loading="observationLoading" @click="loadObservations">刷新本页</el-button>
      </div>
      <el-table v-loading="observationLoading" :data="observations" empty-text="还没有观察记录" style="width:100%">
        <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmt(row.occurred_at) }}</template></el-table-column>
        <el-table-column label="屏" width="70"><template #default="{ row }">{{ row.display }}</template></el-table-column>
        <el-table-column label="摘要" min-width="420"><template #default="{ row }">{{ row.summary }}</template></el-table-column>
        <el-table-column label="重要度" width="90"><template #default="{ row }">{{ row.importance.toFixed(2) }}</template></el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="observationPage"
          v-model:page-size="observationPageSize"
          :total="observationTotal"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onObservationPageChange"
          @size-change="onObservationPageChange"
        />
      </div>
    </div>
    </template>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero-actions { display: flex; align-items: center; gap: 10px; flex: none; }
.hero h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 15px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.switch-stat > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.display-stat :deep(.el-select) { width: 100%; margin: 4px 0; }
.stats span, small { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 22px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.panel-head h2 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.error-alert { border-radius: 10px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
@media (max-width: 1000px) {
  .stats { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
}
</style>
