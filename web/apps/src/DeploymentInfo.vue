<script setup lang="ts">
import { onMounted, ref } from "vue";
defineProps<{ compact?: boolean }>();
const info = ref<{ version: string; built_at: string | null; deployed_at: string | null; started_at: string } | null>(null);
const failed = ref(false);
function date(value: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未记录";
}
async function refresh() {
  failed.value = false;
  try {
    const response = await fetch("/api/v1/meta/version", { cache: "no-store" });
    if (!response.ok) throw new Error("version_unavailable");
    info.value = await response.json();
  } catch { info.value = null; failed.value = true; }
}
onMounted(refresh);
</script>

<template>
  <details v-if="compact" class="deployment-compact">
    <summary><span>版本信息</span><span class="version-badge" :title="info?.version">{{ info?.version || (failed ? '读取失败' : '读取中…') }}</span></summary>
    <dl v-if="info">
      <dt>镜像构建</dt><dd>{{ date(info.built_at) }}</dd>
      <dt>部署启动 · 北京时间</dt><dd>{{ date(info.deployed_at || info.started_at) }}</dd>
    </dl>
    <p v-if="failed" role="status">版本信息获取失败，请重试。</p>
    <button type="button" @click="refresh">刷新版本信息</button>
  </details>
  <div v-else class="deployment-info">
    <template v-if="info">
      <span>服务版本 {{ info.version }}</span>
      <span>构建 {{ date(info.built_at) }}</span>
      <span title="北京时间；容器重新启动时更新">部署启动 {{ date(info.deployed_at || info.started_at) }}</span>
    </template>
    <span v-else>{{ failed ? "版本信息获取失败" : "正在读取版本…" }}</span>
    <button type="button" @click="refresh">刷新版本</button>
  </div>
</template>

<style scoped>
.deployment-info { display:grid; gap:3px; color:var(--muted); font-size:10px; line-height:1.5; overflow-wrap:anywhere; }
button { justify-self:start; border:0; padding:0; background:none; color:var(--accent); font:inherit; cursor:pointer; }
.deployment-compact { min-width:0; border-top:1px solid var(--line); padding-top:8px; font-size:11px; color:var(--muted); }
.deployment-compact summary { display:flex; align-items:center; gap:6px; min-height:28px; cursor:pointer; list-style:none; }
.deployment-compact summary::-webkit-details-marker { display:none; }
.deployment-compact summary::after { content:'⌄'; flex:none; transition:transform 150ms; }
.deployment-compact[open] summary::after { transform:rotate(180deg); }
.deployment-compact summary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:4px; }
.version-badge { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:right; font-variant-numeric:tabular-nums; font-size:10px; }
.deployment-compact dl { margin:8px 0; display:grid; gap:3px; }
.deployment-compact dt { font-size:10px; }
.deployment-compact dd { margin:0 0 5px; color:var(--text); overflow-wrap:anywhere; font-variant-numeric:tabular-nums; }
.deployment-compact button { padding:5px 0; min-height:28px; font-size:11px; }
</style>
