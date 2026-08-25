<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi, type VoiceLatencySummary } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface ConversationItem {
  id: string;
  user_id: string;
  status: string;
  created_at: string;
  last_active_at: string;
}

interface MessageItem {
  id: string;
  role: string;
  privacy_level: string;
  created_at: string;
}

interface AmapMetrics {
  total_calls: number;
  success_rate: number | null;
  p50_latency_ms: number | null;
  p90_latency_ms: number | null;
  cache_hit_rate: number | null;
  failures: Record<string, number>;
}

const conversations = ref<ConversationItem[]>([]);
const messages = ref<MessageItem[]>([]);
const voiceLatency = ref<VoiceLatencySummary | null>(null);
const amapMetrics = ref<AmapMetrics | null>(null);
const haLedger = ref<unknown[]>([]);
const loading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    const [c, m, v, a, h] = await Promise.allSettled([
      api.request<ConversationItem[]>("/api/v1/chat/conversations"),
      api.request<MessageItem[]>("/api/v1/admin/memories?status=active&limit=1").then(() =>
        api.request<MessageItem[]>("/api/v1/chat/conversations/00000000-0000-0000-0000-000000000000/messages").catch(() => []),
      ),
      api.request<VoiceLatencySummary>("/api/v1/meta/voice/latency"),
      api.request<AmapMetrics>("/api/v1/admin/config/tools/amap/metrics"),
      api.request<unknown[]>("/api/v1/admin/config/integrations/home-assistant/ledger?limit=50"),
    ]);
    if (c.status === "fulfilled") conversations.value = c.value;
    if (m.status === "fulfilled") messages.value = m.value;
    if (v.status === "fulfilled") voiceLatency.value = v.value;
    if (a.status === "fulfilled") amapMetrics.value = a.value;
    if (h.status === "fulfilled") haLedger.value = h.value;
  } catch {
    // Promise.allSettled 不抛错
  } finally {
    loading.value = false;
  }
}

const activeConversations = computed(() => conversations.value.filter((c) => c.status === "active"));
const messageCounts = computed(() => {
  const counts: Record<string, number> = {};
  for (const m of messages.value) {
    counts[m.role] = (counts[m.role] ?? 0) + 1;
  }
  return counts;
});
const privacyCounts = computed(() => {
  const counts: Record<string, number> = {};
  for (const m of messages.value) {
    counts[m.privacy_level] = (counts[m.privacy_level] ?? 0) + 1;
  }
  return counts;
});

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
        <strong>{{ conversations.length }}</strong>
        <small>{{ activeConversations.length }} 个活跃</small>
      </el-card>
      <el-card shadow="never">
        <span>语音样本</span>
        <strong>{{ voiceLatency?.count ?? "—" }}</strong>
        <small>完成回合</small>
      </el-card>
      <el-card shadow="never">
        <span>高德调用</span>
        <strong>{{ amapMetrics?.total_calls ?? "—" }}</strong>
        <small>{{ amapMetrics?.success_rate !== null ? (amapMetrics.success_rate * 100).toFixed(0) + "% 成功" : "" }}</small>
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
