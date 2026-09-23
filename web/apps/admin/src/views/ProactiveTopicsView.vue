<script setup lang="ts">
import { inject, onActivated, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

type SourceKey = "screen_awareness" | "browser_awareness";
interface AwarenessConfig {
  enabled: boolean;
  proactive_enabled: boolean;
  [key: string]: unknown;
}
interface CurrentConfig {
  version: number;
  config: Record<string, unknown> & Partial<Record<SourceKey, AwarenessConfig>>;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const current = ref<CurrentConfig | null>(null);
const loading = ref(false);
const saving = ref<SourceKey | null>(null);
const errorMessage = ref("");
const sources: Array<{ key: SourceKey; name: string; description: string; tab: string }> = [
  { key: "screen_awareness", name: "根据屏幕内容发起话题", description: "发现屏幕上值得交流的内容时，允许主动分享、建议或提问。", tab: "screen" },
  { key: "browser_awareness", name: "根据浏览内容发起话题", description: "发现当前网页中值得交流的内容时，允许主动分享、建议或提问。", tab: "browser" },
];

async function load() {
  loading.value = true;
  errorMessage.value = "";
  try {
    current.value = await api.request<CurrentConfig>("/api/v1/admin/config/current");
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "主动话题配置加载失败";
    emit("status", errorMessage.value, true);
  } finally {
    loading.value = false;
  }
}

async function updateSource(key: SourceKey, enabled: boolean) {
  if (loading.value || saving.value) return;
  saving.value = key;
  errorMessage.value = "";
  try {
    // 保存前读取最新完整配置，只修改当前来源的主动话题字段。
    const latest = await api.request<CurrentConfig>("/api/v1/admin/config/current");
    const source = latest.config[key];
    if (!source) throw new Error("当前配置缺少观察来源，请刷新后重试");
    latest.config[key] = { ...source, proactive_enabled: enabled };
    current.value = await api.request<CurrentConfig>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(latest.config),
    });
    const message = `主动话题已${enabled ? "开启" : "关闭"}`;
    ElMessage.success(`${message}，已保存`);
    emit("status", `${message}，配置版本 ${current.value.version}`);
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "主动话题配置保存失败";
    ElMessage.error("保存失败，请重试");
    emit("status", errorMessage.value, true);
  } finally {
    saving.value = null;
  }
}

onMounted(load);
onActivated(load);
</script>

<template>
  <section class="content" v-loading="loading">
    <div class="panel heading">
      <div>
        <h2>主动发起话题</h2>
        <p>选择允许主动交流的观察来源。开关自动保存，保存后生效。</p>
      </div>
      <el-button :loading="loading" :disabled="saving !== null" @click="load">刷新</el-button>
    </div>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <div v-for="source in sources" :key="source.key" class="panel">
      <div class="heading">
        <div>
          <h3>{{ source.name }}</h3>
          <p>{{ source.description }}</p>
        </div>
        <el-switch
          :model-value="current?.config[source.key]?.proactive_enabled ?? false"
          :aria-label="source.name"
          :loading="saving === source.key"
          :disabled="loading || saving !== null || !current?.config[source.key]"
          @change="(value: string | number | boolean) => updateSource(source.key, Boolean(value))"
        />
      </div>
      <div v-if="current?.config[source.key]" class="source-status">
        <span>{{ current.config[source.key]?.enabled ? '观察已开启' : '观察尚未开启，开启观察后才能触发话题' }}</span>
        <RouterLink :to="{ path: '/perception', query: { tab: source.tab } }">查看观察设置</RouterLink>
      </div>
    </div>
    <div class="panel">
      <p>开启后，中枢会判断内容是否值得交流，并遵守免打扰、安静时段和消息预算。</p>
      <RouterLink :to="{ path: '/output', query: { tab: 'channels' } }">配置主动输出通道</RouterLink>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 0; display: grid; gap: 16px; align-content: start; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
h2, h3 { margin: 0; font-size: 15px; }
p { margin: 6px 0 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
.source-status { display: flex; flex-wrap: wrap; gap: 12px; color: var(--muted); font-size: 12px; }
a { color: var(--accent); font-size: 13px; }
@media (max-width: 720px) { .content { padding: 16px; } }
</style>
