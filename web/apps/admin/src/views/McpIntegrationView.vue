<script setup lang="ts">
import { computed, inject, onActivated, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface ServerItem {
  server_id: string;
  configured_enabled: boolean;
  available: boolean;
  refreshing: boolean;
  protocol_version: string | null;
  server_name: string | null;
  server_version: string | null;
  tool_count: number;
  last_refresh_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
}

interface ToolItem {
  internal_name: string;
  server_id: string;
  remote_name: string;
  title: string;
  description: string;
  read_only: boolean;
  destructive: boolean;
  idempotent: boolean;
}

const enabled = ref(false);
const servers = ref<ServerItem[]>([]);
const tools = ref<ToolItem[]>([]);
const loading = ref(false);

const availableCount = computed(() => servers.value.filter((item) => item.available).length);

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    const [serverResult, toolResult] = await Promise.all([
      api.request<{ enabled: boolean; servers: ServerItem[] }>("/api/v1/admin/mcp/servers"),
      api.request<{ items: ToolItem[] }>("/api/v1/admin/mcp/tools"),
    ]);
    enabled.value = serverResult.enabled;
    servers.value = serverResult.servers;
    tools.value = toolResult.items;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "MCP 状态加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function refreshServer(serverId: string) {
  try {
    await api.request(`/api/v1/admin/mcp/servers/${encodeURIComponent(serverId)}/refresh`, {
      method: "POST",
    });
    ElMessage.success(`${serverId} 目录已刷新`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "MCP 目录刷新失败", true);
  }
}

onActivated(() => void load());
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">MCP · MODEL CONTEXT PROTOCOL</div>
        <h2>MCP 外部工具</h2>
        <p>
          查看外部 MCP Server 的连接状态、协议协商与白名单工具目录。
          工具发现不会自动把全部定义注入聊天上下文：只读工具按意图逐轮挂载，写工具只经行动计划触发。
        </p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="stats">
      <article>
        <span>总开关</span>
        <strong>{{ enabled ? "已开启" : "关闭" }}</strong>
        <small>{{ enabled ? "允许建立外部连接" : "不会建立任何外部连接" }}</small>
      </article>
      <article><span>Server</span><strong>{{ servers.length }}</strong><small>{{ availableCount }} 个可用</small></article>
      <article><span>授权工具</span><strong>{{ tools.length }}</strong><small>只读按意图挂载 · 写经计划</small></article>
      <article>
        <span>写操作工具</span>
        <strong>{{ tools.filter((item) => !item.read_only).length }}</strong>
        <small>仅行动计划可执行</small>
      </article>
    </div>

    <el-alert
      v-if="servers.some((item) => item.last_error)"
      :title="`最近错误：${servers.find((item) => item.last_error)?.last_error}`"
      type="warning"
      :closable="false"
      class="error-alert"
    />

    <div class="panel">
      <div class="panel-head">
        <div><h2>Server 状态</h2><p>协议协商结果与目录刷新健康。</p></div>
      </div>
      <el-table
        v-loading="loading"
        :data="servers"
        empty-text="尚未配置 MCP Server"
        style="width:100%"
      >
        <el-table-column prop="server_id" label="Server" min-width="140" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="row.available ? 'success' : 'info'" size="small">{{ row.available ? "可用" : "不可用" }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="服务端" min-width="170">
          <template #default="{ row }">{{ row.server_name || "—" }} {{ row.server_version || "" }}</template>
        </el-table-column>
        <el-table-column label="协议" min-width="130">
          <template #default="{ row }"><code>{{ row.protocol_version ?? "—" }}</code></template>
        </el-table-column>
        <el-table-column prop="tool_count" label="工具" width="75" />
        <el-table-column label="最近刷新" min-width="170">
          <template #default="{ row }">{{ fmt(row.last_refresh_at) }}</template>
        </el-table-column>
        <el-table-column label="最近错误" min-width="170">
          <template #default="{ row }">{{ row.last_error ?? "—" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              :disabled="!row.configured_enabled"
              @click="refreshServer(row.server_id)"
            >刷新目录</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>已授权工具目录</h2><p>本地白名单与远端目录的交集；远端注解仅作参考，本地策略才是权威。</p></div>
      </div>
      <el-table
        v-loading="loading"
        :data="tools"
        empty-text="没有可用的白名单工具"
        style="width:100%"
      >
        <el-table-column label="内部名称" min-width="220">
          <template #default="{ row }"><code>{{ row.internal_name }}</code></template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="140" />
        <el-table-column prop="description" label="描述" min-width="260" show-overflow-tooltip />
        <el-table-column label="策略" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="row.read_only ? 'success' : 'warning'">{{ row.read_only ? "只读" : "写操作" }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 15px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.stats span, .stats small { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 22px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.panel-head h2 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.error-alert { border-radius: 10px; }
@media (max-width: 1000px) {
  .stats { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
}
</style>
