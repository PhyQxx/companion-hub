<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { AdminApi, type VoiceLatencySummary } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const router = useRouter();
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const config = ref<{ version: number; content_hash: string; config?: unknown } | null>(null);
const personas = ref<{ version: number; persona: { name?: string } } | null>(null);
const memories = ref<number | null>(null);
const ledger = ref<unknown[] | null>(null);
const voiceLatency = ref<VoiceLatencySummary | null>(null);
const devices = ref<Array<{ online: boolean; revoked_at: string | null }> | null>(null);

function latencyText(value: number | null) {
  return value === null ? "—" : `${value} ms`;
}

async function refreshVoiceLatency() {
  voiceLatency.value = await api.request<VoiceLatencySummary>("/api/v1/meta/voice/latency");
}

async function resetVoiceLatency() {
  voiceLatency.value = await api.request<VoiceLatencySummary>("/api/v1/meta/voice/latency/reset", {
    method: "POST",
  });
}

onMounted(async () => {
  try {
    config.value = await api.request("/api/v1/admin/config/current");
    personas.value = await api.request("/api/v1/admin/personas/current");
    memories.value = (
      await api.request<unknown[]>("/api/v1/admin/memories?status=active&limit=200")
    ).length;
    ledger.value = await api.request<unknown[]>("/api/v1/admin/deletion-ledger?limit=200");
    devices.value = await api.request<Array<{ online: boolean; revoked_at: string | null }>>(
      "/api/v1/admin/devices",
    );
    await refreshVoiceLatency();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
});
</script>

<template>
  <section class="content">
    <div class="stats">
      <el-card shadow="never"><span>模型配置</span><strong>v{{ config?.version ?? "—" }}</strong><small>{{ config?.content_hash?.slice(0, 12) ?? "" }}</small></el-card>
      <el-card shadow="never"><span>当前人格</span><strong>{{ personas?.persona?.name ?? "—" }}</strong><small>v{{ personas?.version ?? "—" }}</small></el-card>
      <el-card shadow="never"><span>活跃记忆</span><strong>{{ memories ?? "—" }}</strong><small>参与检索</small></el-card>
      <el-card shadow="never"><span>删除台账</span><strong>{{ ledger?.length ?? "—" }}</strong><small>硬删除记录</small></el-card>
      <el-card shadow="never"><span>在线设备</span><strong>{{ devices?.filter(item => item.online && !item.revoked_at).length ?? "—" }}</strong><small>活跃 {{ devices?.filter(item => !item.revoked_at).length ?? "—" }} 台</small></el-card>
    </div>
    <el-card shadow="never" class="voice-latency-card">
      <template #header>
        <div class="section-head">
          <div>
            <strong>语音延迟</strong>
            <small>最近 {{ voiceLatency?.window_size ?? 200 }} 个完成回合 · 当前 {{ voiceLatency?.count ?? 0 }} 个样本</small>
          </div>
          <div class="section-actions">
            <el-button size="small" @click="refreshVoiceLatency">刷新</el-button>
            <el-button size="small" type="danger" plain @click="resetVoiceLatency">清空样本</el-button>
          </div>
        </div>
      </template>
      <div class="latency-grid">
        <div><span>ASR</span><strong>P90 {{ latencyText(voiceLatency?.asr_ms.p90 ?? null) }}</strong><small>P50 {{ latencyText(voiceLatency?.asr_ms.p50 ?? null) }}</small></div>
        <div><span>首 token</span><strong>P90 {{ latencyText(voiceLatency?.first_token_ms.p90 ?? null) }}</strong><small>P50 {{ latencyText(voiceLatency?.first_token_ms.p50 ?? null) }}</small></div>
        <div><span>首音频</span><strong>P90 {{ latencyText(voiceLatency?.first_audio_ms.p90 ?? null) }}</strong><small>目标 ≤ {{ voiceLatency?.targets.first_audio_p90_ms ?? 1800 }} ms</small></div>
        <div><span>打断</span><strong>P90 {{ latencyText(voiceLatency?.interrupt_ms.p90 ?? null) }}</strong><small>目标 ≤ {{ voiceLatency?.targets.interrupt_p90_ms ?? 300 }} ms</small></div>
        <div><span>总耗时</span><strong>P90 {{ latencyText(voiceLatency?.total_ms.p90 ?? null) }}</strong><small>P50 {{ latencyText(voiceLatency?.total_ms.p50 ?? null) }}</small></div>
      </div>
      <div class="acceptance-row">
        <el-tag :type="voiceLatency?.acceptance.completed_turns_ready ? 'success' : 'info'">
          完成回合 {{ voiceLatency?.count ?? 0 }}/{{ voiceLatency?.targets.completed_turns ?? 20 }}
        </el-tag>
        <el-tag type="info">ASR 预取 {{ voiceLatency?.asr_prefetched_count ?? 0 }} 次</el-tag>
        <el-tag :type="voiceLatency?.acceptance.first_audio_samples_ready ? 'success' : 'info'">
          首音频样本 {{ voiceLatency?.first_audio_ms.count ?? 0 }}/{{ voiceLatency?.targets.first_audio_samples ?? 20 }}
        </el-tag>
        <el-tag :type="voiceLatency?.acceptance.first_audio_p90_pass ? 'success' : 'warning'">首音频 P90</el-tag>
        <el-tag :type="voiceLatency?.acceptance.interrupt_samples_ready ? 'success' : 'info'">
          打断样本 {{ voiceLatency?.interrupt_ms.count ?? 0 }}/{{ voiceLatency?.targets.interrupt_samples ?? 20 }}
        </el-tag>
        <el-tag :type="voiceLatency?.acceptance.interrupt_p90_pass ? 'success' : 'warning'">打断 P90</el-tag>
        <el-tag :type="voiceLatency?.acceptance.overall_pass ? 'success' : 'danger'">M2 总判定</el-tag>
      </div>
    </el-card>
    <div class="links">
      <el-button link type="primary" @click="router.push({ path: '/models', query: { tab: 'services' } })">管理模型与路由 →</el-button>
      <el-button link type="primary" @click="router.push({ path: '/personas', query: { tab: 'profile' } })">编辑人格 →</el-button>
      <el-button link type="primary" @click="router.push({ path: '/memory', query: { tab: 'library' } })">记忆库治理 →</el-button>
      <el-button link type="primary" @click="router.push({ path: '/memory', query: { tab: 'timeline' } })">历史时间线 →</el-button>
      <el-button link type="primary" @click="router.push({ path: '/devices', query: { tab: 'registry' } })">设备与现实能力 →</el-button>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; display: grid; gap: 18px; align-content: start; overflow-y: auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.stats small { color: var(--muted); font-size: 11px; }
.links { display: grid; gap: 8px; }
.links :deep(.el-button) { justify-self:start; padding-left:0; }
.section-head { display:flex; justify-content:space-between; align-items:center; gap:12px; }
.section-head > div { display:grid; gap:3px; }
.section-actions { display:flex; gap:8px; }
.section-head small { color:var(--muted); font-weight:400; }
.latency-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
.latency-grid > div { display:grid; gap:4px; padding:10px; border:1px solid var(--line); border-radius:10px; }
.latency-grid span, .latency-grid small { color:var(--muted); font-size:12px; }
.latency-grid strong { font-size:16px; }
.acceptance-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
</style>
