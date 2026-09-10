<script setup lang="ts">
import { inject, onActivated, onDeactivated, ref, watch } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "browser_status" });

interface StatusResponse {
  configured_enabled: boolean;
  interval_seconds: number;
  memory_enabled: boolean;
  proactive_enabled: boolean;
  max_text_chars: number;
  blocked_hosts: string[];
  loop_running: boolean;
  cycles: number;
  observations: number;
  last_tick_at: string | null;
  last_origin: string | null;
  last_hash: string | null;
  last_analyzed_at: string | null;
  last_summary: string | null;
  consecutive_failures: number;
  cooldown_until: string | null;
  last_error: string | null;
}

interface ObservationItem {
  id: number;
  occurred_at: string;
  origin: string;
  page_title: string;
  summary: string;
  importance: number;
}

interface BrowserAwarenessConfig {
  enabled: boolean;
  interval_seconds: number;
  analysis_prompt: string;
  memory_enabled: boolean;
  proactive_enabled: boolean;
  max_text_chars: number;
  blocked_hosts: string[];
}

interface CurrentConfig {
  version: number;
  config: Record<string, unknown> & { browser_awareness?: BrowserAwarenessConfig };
}

const status = ref<StatusResponse | null>(null);
const observations = ref<ObservationItem[]>([]);
const loading = ref(false);
const saving = ref(false);
const blockedHostsText = ref("");
let timer: ReturnType<typeof window.setInterval> | null = null;

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function loadStatus() {
  loading.value = true;
  try {
    status.value = await api.request<StatusResponse>("/api/v1/admin/browser-awareness/status");
    blockedHostsText.value = status.value.blocked_hosts.join("\n");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "浏览感知状态加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function loadObservations() {
  loading.value = true;
  try {
    const result = await api.request<{ items: ObservationItem[] }>(
      "/api/v1/admin/browser-awareness/observations?limit=50",
    );
    observations.value = result.items;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "浏览观察记录加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function updateConfig(patch: Partial<BrowserAwarenessConfig>, message: string) {
  saving.value = true;
  try {
    const current = await api.request<CurrentConfig>("/api/v1/admin/config/current");
    const config = structuredClone(current.config);
    const existing = config.browser_awareness;
    if (!existing) throw new Error("当前配置缺少 browser_awareness");
    config.browser_awareness = { ...existing, ...patch };
    await api.request("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    await loadStatus();
    ElMessage.success(message);
    emit("status", message);
  } catch (error) {
    ElMessage.error("浏览感知配置保存失败");
    emit("status", error instanceof Error ? error.message : "浏览感知配置保存失败", true);
  } finally {
    saving.value = false;
  }
}

async function saveBlockedHosts() {
  const hosts = [...new Set(blockedHostsText.value.split(/\s+/).map((item) => item.trim().toLowerCase()).filter(Boolean))];
  await updateConfig({ blocked_hosts: hosts }, "主机黑名单已更新");
}

async function refresh() {
  if (props.mode === "browser_observations") await loadObservations();
  else await loadStatus();
}

watch(() => props.mode, refresh);
onActivated(() => {
  void refresh();
  timer = window.setInterval(() => {
    if (props.mode !== "browser_observations") void loadStatus();
  }, 15_000);
});
onDeactivated(() => {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
});
</script>

<template>
  <section class="content">
    <template v-if="props.mode !== 'browser_observations'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">浏览感知 · BROWSER AWARENESS</div>
          <h2>周期标签页观察</h2>
          <p>Hub 拉取已授权浏览器的当前标签页，页面变化时生成摘要。正文即焚，只保存 origin、截断标题和摘要。</p>
        </div>
        <el-button :loading="loading" @click="refresh">刷新</el-button>
      </div>

      <div class="stats">
        <article>
          <div class="switch-row"><span>配置开关</span><el-switch :model-value="status?.configured_enabled ?? false" :loading="saving" @change="(value: string | number | boolean) => updateConfig({ enabled: Boolean(value) }, Boolean(value) ? '浏览感知已开启' : '浏览感知已关闭')" /></div>
          <strong>{{ status?.configured_enabled ? "开启" : "关闭" }}</strong>
          <small>默认关闭，保存后热生效</small>
        </article>
        <article><span>循环</span><strong>{{ status?.loop_running ? "运行中" : "停止" }}</strong><small>{{ status?.cycles ?? 0 }} 次 tick</small></article>
        <article><span>观察</span><strong>{{ status?.observations ?? 0 }}</strong><small>变化后成功分析</small></article>
        <article><span>间隔</span><strong>{{ status?.interval_seconds ?? "—" }}s</strong><small>正文上限 {{ status?.max_text_chars ?? "—" }} 字</small></article>
      </div>

      <el-alert v-if="status?.last_error" :title="`最近错误：${status.last_error}`" type="warning" :closable="false" />

      <div class="panel details">
        <h2>最近状态</h2>
        <dl>
          <dt>上次 tick</dt><dd>{{ fmt(status?.last_tick_at ?? null) }}</dd>
          <dt>最近分析</dt><dd>{{ fmt(status?.last_analyzed_at ?? null) }}</dd>
          <dt>站点 origin</dt><dd><code>{{ status?.last_origin ?? "—" }}</code></dd>
          <dt>页面指纹</dt><dd><code>{{ status?.last_hash ?? "—" }}</code></dd>
          <dt>摘要</dt><dd>{{ status?.last_summary ?? "—" }}</dd>
          <dt>冷却至</dt><dd>{{ fmt(status?.cooldown_until ?? null) }}</dd>
        </dl>
      </div>

      <div class="panel">
        <div class="panel-head"><div><h2>主机黑名单</h2><p>每行一个精确主机名；命中后静默跳过，不调用模型或写时间线。</p></div><el-button :loading="saving" @click="saveBlockedHosts">保存</el-button></div>
        <el-input v-model="blockedHostsText" type="textarea" :rows="6" placeholder="bank.example.com&#10;accounts.example.com" />
      </div>
    </template>

    <template v-else>
      <div class="panel">
        <div class="panel-head"><div><h2>浏览观察记录</h2><p>最近 50 条 browser.observed 摘要，不含页面路径、查询串和正文。</p></div><el-button :loading="loading" @click="loadObservations">刷新</el-button></div>
        <el-table v-loading="loading" :data="observations" empty-text="还没有浏览观察记录">
          <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmt(row.occurred_at) }}</template></el-table-column>
          <el-table-column label="站点" min-width="190"><template #default="{ row }"><code>{{ row.origin }}</code></template></el-table-column>
          <el-table-column prop="page_title" label="页面标题" min-width="180" />
          <el-table-column prop="summary" label="摘要" min-width="320" />
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
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p, .panel-head p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 15px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.stats span, small { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 22px; }
.switch-row, .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.details dl { display: grid; grid-template-columns: 110px 1fr; gap: 10px 16px; margin: 16px 0 0; font-size: 13px; }
.details dt { color: var(--muted); }
.details dd { margin: 0; word-break: break-word; }
@media (max-width: 800px) { .stats { grid-template-columns: repeat(2, 1fr); } .details dl { grid-template-columns: 1fr; } }
</style>
