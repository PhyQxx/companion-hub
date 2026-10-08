<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi, type VoiceLatencySummary } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface UsageSummary {
  total_conversations: number;
  active_conversations: number;
  total_messages: number;
  messages_by_role: Record<string, number>;
  messages_by_privacy_level: Record<string, number>;
}

interface AmapMetrics {
  total_calls: number;
  success_rate: number | null;
  p50_latency_ms: number | null;
  p90_latency_ms: number | null;
  cache_hit_rate: number | null;
  failures: Record<string, number>;
}

const usage = ref<UsageSummary | null>(null);
const voiceLatency = ref<VoiceLatencySummary | null>(null);
const amapMetrics = ref<AmapMetrics | null>(null);
const haLedger = ref<unknown[]>([]);
const loading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    const [u, v, a, h] = await Promise.allSettled([
      api.request<UsageSummary>("/api/v1/admin/dashboard/usage"),
      api.request<VoiceLatencySummary>("/api/v1/meta/voice/latency"),
      api.request<AmapMetrics>("/api/v1/admin/config/tools/amap/metrics"),
      api.request<unknown[]>("/api/v1/admin/config/integrations/home-assistant/ledger?limit=50"),
    ]);
    if (u.status === "fulfilled") usage.value = u.value;
    if (v.status === "fulfilled") voiceLatency.value = v.value;
    if (a.status === "fulfilled") amapMetrics.value = a.value;
    if (h.status === "fulfilled") haLedger.value = h.value;
  } catch {
    // Promise.allSettled 不抛错
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
      <el-tag type="info">部分数据来自分散端点，统一聚合 API 后续补充</el-tag>
    </div>

    <div class="stats">
      <el-card shadow="never">
        <span>会话总数</span>
        <strong>{{ usage?.total_conversations ?? "—" }}</strong>
        <small>{{ usage ? `${usage.active_conversations} 个活跃` : "" }}</small>
      </el-card>
      <el-card shadow="never">
        <span>消息总数</span>
        <strong>{{ usage?.total_messages ?? "—" }}</strong>
      </el-card>
      <el-card shadow="never">
        <span>语音样本</span>
        <strong>{{ voiceLatency?.count ?? "—" }}</strong>
        <small>完成回合</small>
      </el-card>
      <el-card shadow="never">
        <span>高德调用</span>
        <strong>{{ amapMetrics?.total_calls ?? "—" }}</strong>
        <small>{{ amapMetrics && amapMetrics.success_rate !== null ? (amapMetrics.success_rate * 100).toFixed(0) + "% 成功" : "" }}</small>
      </el-card>
      <el-card shadow="never">
        <span>HA 调用</span>
        <strong>{{ haLedger.length }}</strong>
        <small>最近 50 条</small>
      </el-card>
    </div>

    <div class="detail-grid">
      <el-card shadow="never">
        <template #header><span>语音延迟指标</span></template>
        <div class="meta">
          <div><dt>ASR P50</dt><dd>{{ voiceLatency?.asr_ms.p50 ?? "—" }} ms</dd></div>
          <div><dt>ASR P90</dt><dd>{{ voiceLatency?.asr_ms.p90 ?? "—" }} ms</dd></div>
          <div><dt>首音频 P90</dt><dd>{{ voiceLatency?.first_audio_ms.p90 ?? "—" }} ms</dd></div>
          <div><dt>打断 P90</dt><dd>{{ voiceLatency?.interrupt_ms.p90 ?? "—" }} ms</dd></div>
          <div><dt>总耗时 P90</dt><dd>{{ voiceLatency?.total_ms.p90 ?? "—" }} ms</dd></div>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header><span>消息角色分布</span></template>
        <div v-if="usage && Object.keys(usage.messages_by_role).length" class="meta">
          <div v-for="(count, role) in usage.messages_by_role" :key="role">
            <dt>{{ role }}</dt><dd>{{ count }}</dd>
          </div>
        </div>
        <div v-else class="meta"><dt>—</dt><dd>暂无数据</dd></div>
      </el-card>

      <el-card shadow="never">
        <template #header><span>消息隐私分布</span></template>
        <div v-if="usage && Object.keys(usage.messages_by_privacy_level).length" class="meta">
          <div v-for="(count, level) in usage.messages_by_privacy_level" :key="level">
            <dt>{{ level }}</dt><dd>{{ count }}</dd>
          </div>
        </div>
        <div v-else class="meta"><dt>—</dt><dd>暂无数据</dd></div>
      </el-card>

      <el-card v-if="amapMetrics" shadow="never">
        <template #header><span>高德工具指标</span></template>
        <div class="meta">
          <div><dt>总调用</dt><dd>{{ amapMetrics.total_calls }}</dd></div>
          <div><dt>成功率</dt><dd>{{ amapMetrics.success_rate !== null ? (amapMetrics.success_rate * 100).toFixed(1) + "%" : "—" }}</dd></div>
          <div><dt>P50 延迟</dt><dd>{{ amapMetrics.p50_latency_ms !== null ? amapMetrics.p50_latency_ms.toFixed(0) + " ms" : "—" }}</dd></div>
          <div><dt>P90 延迟</dt><dd>{{ amapMetrics.p90_latency_ms !== null ? amapMetrics.p90_latency_ms.toFixed(0) + " ms" : "—" }}</dd></div>
          <div><dt>缓存命中率</dt><dd>{{ amapMetrics.cache_hit_rate !== null ? (amapMetrics.cache_hit_rate * 100).toFixed(1) + "%" : "—" }}</dd></div>
        </div>
      </el-card>

      <el-card v-if="Object.keys(amapMetrics?.failures ?? {}).length" shadow="never">
        <template #header><span>高德失败分布</span></template>
        <div class="meta">
          <div v-for="(count, code) in (amapMetrics?.failures ?? {})" :key="code">
            <dt>{{ code }}</dt>
            <dd>{{ count }}</dd>
          </div>
        </div>
      </el-card>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; display: grid; gap: 14px; align-content: start; }
.toolbar { display: flex; gap: 8px; align-items: center; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.stats small { color: var(--muted); font-size: 11px; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.meta { display: grid; gap: 8px; }
.meta div { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-weight: 600; }
</style>
