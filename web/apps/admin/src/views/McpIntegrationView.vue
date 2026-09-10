<script setup lang="ts">
import { inject, onActivated, ref } from "vue";
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
  <section class="mcp-view">
    <div class="hero panel">
      <div>
        <div class="eyebrow">MODEL CONTEXT PROTOCOL</div>
        <h2>MCP 外部工具</h2>
        <p>查看 Server 连接、协议协商和白名单工具目录。工具发现不会自动把全部定义注入聊天上下文。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-alert
      :title="enabled ? 'MCP 总开关已开启' : 'MCP 总开关关闭，当前不会建立外部连接'"
      :type="enabled ? 'success' : 'info'"
      :closable="false"
    />

    <div class="panel">
      <h3>Server 状态</h3>
      <el-empty v-if="servers.length === 0" description="尚未配置 MCP Server" />
      <el-table v-else :data="servers">
        <el-table-column prop="server_id" label="Server" min-width="140" />
        <el-table-column label="状态" width="110">
          <template #default="scope"><el-tag :type="scope.row.available ? 'success' : 'info'">{{ scope.row.available ? "可用" : "不可用" }}</el-tag></template>
        </el-table-column>
        <el-table-column label="服务端" min-width="170">
          <template #default="scope">{{ scope.row.server_name || "—" }} {{ scope.row.server_version || "" }}</template>
        </el-table-column>
        <el-table-column prop="protocol_version" label="协议" min-width="130" />
        <el-table-column prop="tool_count" label="工具" width="75" />
        <el-table-column label="最近刷新" min-width="170">
          <template #default="scope">{{ fmt(scope.row.last_refresh_at) }}</template>
        </el-table-column>
        <el-table-column prop="last_error" label="最近错误" min-width="170" />
        <el-table-column label="操作" width="90">
          <template #default="scope"><el-button link type="primary" :disabled="!scope.row.configured_enabled" @click="refreshServer(scope.row.server_id)">刷新</el-button></template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <h3>已授权工具目录</h3>
      <el-empty v-if="tools.length === 0" description="没有可用的白名单工具" />
      <el-table v-else :data="tools">
        <el-table-column prop="internal_name" label="内部名称" min-width="220" />
        <el-table-column prop="title" label="标题" min-width="140" />
        <el-table-column prop="description" label="描述" min-width="260" show-overflow-tooltip />
        <el-table-column label="策略" width="150">
          <template #default="scope">
            <el-tag size="small" :type="scope.row.read_only ? 'success' : 'warning'">{{ scope.row.read_only ? "只读" : "写操作" }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.mcp-view { display: grid; gap: 16px; }
.panel { padding: 20px; border: 1px solid var(--el-border-color-light); border-radius: 14px; background: var(--el-bg-color); }
.hero { display: flex; justify-content: space-between; gap: 20px; align-items: start; }
.hero h2, .panel h3 { margin: 4px 0 8px; }
.hero p { margin: 0; color: var(--el-text-color-secondary); }
.eyebrow { color: var(--el-color-primary); font-size: 12px; font-weight: 700; letter-spacing: .08em; }
</style>
