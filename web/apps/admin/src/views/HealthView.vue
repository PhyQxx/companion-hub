<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface HealthData {
  status: string;
  version: string;
  dispatcher?: {
    running: boolean;
    cycles: number;
    last_error: string | null;
  };
  configuration?: {
    version: number;
    content_hash: string;
    last_error: string | null;
  };
  persona?: {
    version: number;
    content_hash: string;
    name: string;
  };
  home_assistant?: {
    status: string;
    connected: boolean;
    cached_entities: number;
    last_sync_at: string | null;
    reason_code: string | null;
  };
}

const health = ref<HealthData | null>(null);
const loading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    health.value = await api.request<HealthData>("/healthz");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

function statusType(status: string | undefined) {
  if (status === "ok") return "success";
  if (status === "degraded") return "warning";
  return "danger";
}

function statusText(status: string | undefined) {
  if (status === "ok") return "正常";
  if (status === "degraded") return "降级";
  return "异常";
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>
    <div v-if="health" class="grid">
      <el-card shadow="never">
        <template #header>
          <div class="card-head">
            <span>整体状态</span>
            <el-tag :type="statusType(health.status)">{{ statusText(health.status) }}</el-tag>
          </div>
        </template>
        <div class="meta">
          <div><dt>版本</dt><dd>{{ health.version }}</dd></div>
          <div><dt>状态</dt><dd>{{ statusText(health.status) }}</dd></div>
        </div>
      </el-card>

      <el-card v-if="health.dispatcher" shadow="never">
        <template #header>
          <div class="card-head">
            <span>认知调度器</span>
            <el-tag :type="health.dispatcher.running ? 'success' : 'danger'">
              {{ health.dispatcher.running ? "运行中" : "停止" }}
            </el-tag>
          </div>
        </template>
        <div class="meta">
          <div><dt>运行周期</dt><dd>{{ health.dispatcher.cycles }}</dd></div>
          <div v-if="health.dispatcher.last_error"><dt>最近错误</dt><dd class="error">{{ health.dispatcher.last_error }}</dd></div>
        </div>
      </el-card>

      <el-card v-if="health.configuration" shadow="never">
        <template #header>
          <div class="card-head">
            <span>配置中心</span>
            <el-tag :type="health.configuration.last_error ? 'warning' : 'success'">
              {{ health.configuration.last_error ? "异常" : "正常" }}
            </el-tag>
          </div>
        </template>
        <div class="meta">
          <div><dt>当前版本</dt><dd>v{{ health.configuration.version }}</dd></div>
          <div><dt>内容哈希</dt><dd>{{ health.configuration.content_hash.slice(0, 12) }}</dd></div>
          <div v-if="health.configuration.last_error"><dt>最近错误</dt><dd class="error">{{ health.configuration.last_error }}</dd></div>
        </div>
      </el-card>

      <el-card v-if="health.persona" shadow="never">
        <template #header>
          <div class="card-head">
            <span>人格</span>
            <el-tag type="success">正常</el-tag>
          </div>
        </template>
        <div class="meta">
          <div><dt>名称</dt><dd>{{ health.persona.name }}</dd></div>
          <div><dt>版本</dt><dd>v{{ health.persona.version }}</dd></div>
          <div><dt>内容哈希</dt><dd>{{ health.persona.content_hash.slice(0, 12) }}</dd></div>
        </div>
      </el-card>

      <el-card v-if="health.home_assistant" shadow="never">
        <template #header>
          <div class="card-head">
            <span>Home Assistant</span>
            <el-tag :type="statusType(health.home_assistant.status)">{{ statusText(health.home_assistant.status) }}</el-tag>
          </div>
        </template>
        <div class="meta">
          <div><dt>连接状态</dt><dd>{{ health.home_assistant.connected ? "已连接" : "未连接" }}</dd></div>
          <div><dt>缓存实体</dt><dd>{{ health.home_assistant.cached_entities }}</dd></div>
          <div v-if="health.home_assistant.last_sync_at"><dt>最近同步</dt><dd>{{ new Date(health.home_assistant.last_sync_at).toLocaleString() }}</dd></div>
          <div v-if="health.home_assistant.reason_code"><dt>原因码</dt><dd>{{ health.home_assistant.reason_code }}</dd></div>
        </div>
      </el-card>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.meta { display: grid; gap: 8px; }
.meta div { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-weight: 600; }
.meta .error { color: #f56c6c; }
</style>
