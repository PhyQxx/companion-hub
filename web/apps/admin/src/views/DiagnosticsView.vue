<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi, type VoiceLatencySummary } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface HealthData {
  status: string;
  version: string;
  dispatcher?: { running: boolean; cycles: number; last_error: string | null };
  configuration?: { version: number; content_hash: string; last_error: string | null };
  home_assistant?: { status: string; connected: boolean; cached_entities: number; reason_code: string | null };
}

interface DeviceItem {
  id: string;
  name: string;
  alias: string | null;
  client_type: string;
  online: boolean;
  revoked_at: string | null;
  last_seen_at: string;
}

const health = ref<HealthData | null>(null);
const devices = ref<DeviceItem[]>([]);
const voiceLatency = ref<VoiceLatencySummary | null>(null);
const loading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    const [h, d, v] = await Promise.all([
      api.request<HealthData>("/healthz"),
      api.request<DeviceItem[]>("/api/v1/admin/devices"),
      api.request<VoiceLatencySummary>("/api/v1/meta/voice/latency"),
    ]);
    health.value = h;
    devices.value = d;
    voiceLatency.value = v;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

const activeDevices = computed(() => devices.value.filter((d) => !d.revoked_at));
const onlineDevices = computed(() => activeDevices.value.filter((d) => d.online));
const offlineDevices = computed(() => activeDevices.value.filter((d) => !d.online));
const revokedDevices = computed(() => devices.value.filter((d) => d.revoked_at));

function statusType(status: string | undefined) {
  if (status === "ok") return "success";
  if (status === "degraded") return "warning";
  return "danger";
}

function latencyText(value: number | null) {
  return value === null ? "—" : `${value} ms`;
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <div class="stats">
      <el-card shadow="never">
        <span>整体状态</span>
        <strong>
          <el-tag :type="statusType(health?.status)">{{ health?.status === "ok" ? "正常" : health?.status === "degraded" ? "降级" : "—" }}</el-tag>
        </strong>
      </el-card>
      <el-card shadow="never">
        <span>在线设备</span>
        <strong>{{ onlineDevices.length }}</strong>
        <small>/ {{ activeDevices.length }} 台活跃</small>
      </el-card>
      <el-card shadow="never">
        <span>离线设备</span>
        <strong>{{ offlineDevices.length }}</strong>
      </el-card>
      <el-card shadow="never">
        <span>已撤销</span>
        <strong>{{ revokedDevices.length }}</strong>
      </el-card>
      <el-card shadow="never">
        <span>语音样本</span>
        <strong>{{ voiceLatency?.count ?? "—" }}</strong>
        <small>/ {{ voiceLatency?.targets.completed_turns ?? 20 }} 目标</small>
      </el-card>
    </div>

    <div class="diag-grid">
      <el-card shadow="never">
        <template #header><span>调度器诊断</span></template>
        <div class="meta">
          <div><dt>运行状态</dt><dd>{{ health?.dispatcher?.running ? "运行中" : "停止" }}</dd></div>
          <div><dt>运行周期</dt><dd>{{ health?.dispatcher?.cycles ?? "—" }}</dd></div>
          <div v-if="health?.dispatcher?.last_error"><dt>最近错误</dt><dd class="error">{{ health.dispatcher.last_error }}</dd></div>
          <div><dt>配置状态</dt><dd>{{ health?.configuration?.last_error ? "异常" : "正常" }}</dd></div>
          <div v-if="health?.configuration?.last_error"><dt>配置错误</dt><dd class="error">{{ health.configuration.last_error }}</dd></div>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header><span>Home Assistant 诊断</span></template>
        <div class="meta">
          <div><dt>连接状态</dt><dd>{{ health?.home_assistant?.connected ? "已连接" : "未连接" }}</dd></div>
          <div><dt>服务状态</dt><dd>{{ health?.home_assistant?.status ?? "—" }}</dd></div>
          <div><dt>缓存实体</dt><dd>{{ health?.home_assistant?.cached_entities ?? "—" }}</dd></div>
          <div v-if="health?.home_assistant?.reason_code"><dt>原因码</dt><dd>{{ health.home_assistant.reason_code }}</dd></div>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header><span>语音延迟诊断</span></template>
        <div class="meta">
          <div><dt>ASR P90</dt><dd>{{ latencyText(voiceLatency?.asr_ms.p90 ?? null) }}</dd></div>
          <div><dt>首 token P90</dt><dd>{{ latencyText(voiceLatency?.first_token_ms.p90 ?? null) }}</dd></div>
          <div><dt>首音频 P90</dt><dd>{{ latencyText(voiceLatency?.first_audio_ms.p90 ?? null) }}</dd></div>
          <div><dt>打断 P90</dt><dd>{{ latencyText(voiceLatency?.interrupt_ms.p90 ?? null) }}</dd></div>
          <div><dt>总耗时 P90</dt><dd>{{ latencyText(voiceLatency?.total_ms.p90 ?? null) }}</dd></div>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header><span>设备明细</span></template>
        <div class="device-list">
          <div v-for="d in activeDevices" :key="d.id" class="device-row">
            <span class="name">{{ d.alias ?? d.name }}</span>
            <el-tag :type="d.online ? 'success' : 'warning'" size="small">{{ d.online ? "在线" : "离线" }}</el-tag>
            <span class="time">{{ new Date(d.last_seen_at).toLocaleString() }}</span>
          </div>
          <el-empty v-if="!activeDevices.length" description="无活跃设备" />
        </div>
      </el-card>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; display: grid; gap: 14px; align-content: start; }
.toolbar { display: flex; gap: 8px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.stats small { color: var(--muted); font-size: 11px; }
.diag-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.meta { display: grid; gap: 8px; }
.meta div { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-weight: 600; }
.meta .error { color: #f56c6c; }
.device-list { display: grid; gap: 8px; }
.device-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 13px; }
.device-row .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.device-row .time { color: var(--muted); font-size: 11px; }
</style>
