<script setup lang="ts">
import { computed, inject, onActivated, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage, ElMessageBox } from "element-plus";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const SECRET_MASK = "__ARIA_SECRET_CONFIGURED__DO_NOT_EDIT__";

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

interface ServerConfig {
  server_id: string;
  enabled: boolean;
  endpoint: string;
  secret_value: string | null;
  secret_ref: string | null;
  allowed_tools: string[];
  allow_write_tools: boolean;
  allow_insecure_local_http: boolean;
  connect_timeout_seconds: number;
  call_timeout_seconds: number;
  catalog_ttl_seconds: number;
  max_result_bytes: number;
}

interface McpConfigShape {
  enabled: boolean;
  max_tools_per_turn: number;
  servers: ServerConfig[];
}

const enabled = ref(false);
const maxToolsPerTurn = ref(8);
const servers = ref<ServerItem[]>([]);
const configuredServers = ref<ServerConfig[]>([]);
const tools = ref<ToolItem[]>([]);
const loading = ref(false);
const saving = ref(false);
const editing = ref(false);
const editingId = ref<string | null>(null);
const form = reactive({
  server_id: "",
  enabled: true,
  endpoint: "",
  secret_value: "",
  secret_ref: "",
  allowed_tools: "",
  allow_write_tools: false,
  allow_insecure_local_http: false,
  connect_timeout_seconds: 5,
  call_timeout_seconds: 15,
  catalog_ttl_seconds: 300,
  max_result_bytes: 32000,
});

const availableCount = computed(() => servers.value.filter((item) => item.available).length);

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    const [serverResult, toolResult, configResult] = await Promise.all([
      api.request<{ enabled: boolean; servers: ServerItem[] }>("/api/v1/admin/mcp/servers"),
      api.request<{ items: ToolItem[] }>("/api/v1/admin/mcp/tools"),
      api.request<{ config: { mcp: McpConfigShape } }>("/api/v1/admin/config/current"),
    ]);
    enabled.value = serverResult.enabled;
    servers.value = serverResult.servers;
    tools.value = toolResult.items;
    const mcp = configResult.config.mcp;
    maxToolsPerTurn.value = mcp.max_tools_per_turn;
    configuredServers.value = mcp.servers;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "MCP 状态加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function updateConfig(patch: Partial<McpConfigShape>, message: string) {
  saving.value = true;
  try {
    const current = await api.request<{ config: { mcp: McpConfigShape } }>(
      "/api/v1/admin/config/current",
    );
    const config = structuredClone(current.config);
    config.mcp = { ...config.mcp, ...patch };
    await api.request("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    ElMessage.success(message);
    emit("status", message);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "MCP 配置保存失败", true);
    ElMessage.error("MCP 配置保存失败");
    await load();
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(value: string | number | boolean) {
  await updateConfig({ enabled: Boolean(value) }, Boolean(value) ? "MCP 总开关已开启" : "MCP 总开关已关闭");
}

async function saveMaxTools() {
  const value = Math.max(1, Math.min(16, Number(maxToolsPerTurn.value) || 8));
  maxToolsPerTurn.value = value;
  await updateConfig({ max_tools_per_turn: value }, `每轮工具上限已设为 ${value}`);
}

function openCreate() {
  editingId.value = null;
  Object.assign(form, {
    server_id: "",
    enabled: true,
    endpoint: "",
    secret_value: "",
    secret_ref: "",
    allowed_tools: "",
    allow_write_tools: false,
    allow_insecure_local_http: false,
    connect_timeout_seconds: 5,
    call_timeout_seconds: 15,
    catalog_ttl_seconds: 300,
    max_result_bytes: 32000,
  });
  editing.value = true;
}

function openEdit(server: ServerConfig) {
  editingId.value = server.server_id;
  Object.assign(form, {
    server_id: server.server_id,
    enabled: server.enabled,
    endpoint: String(server.endpoint),
    secret_value: "",
    secret_ref: server.secret_ref ?? "",
    allowed_tools: server.allowed_tools.join("\n"),
    allow_write_tools: server.allow_write_tools,
    allow_insecure_local_http: server.allow_insecure_local_http,
    connect_timeout_seconds: server.connect_timeout_seconds,
    call_timeout_seconds: server.call_timeout_seconds,
    catalog_ttl_seconds: server.catalog_ttl_seconds,
    max_result_bytes: server.max_result_bytes,
  });
  editing.value = true;
}

async function saveServer() {
  const serverId = form.server_id.trim();
  if (!/^[a-z][a-z0-9_-]{1,63}$/.test(serverId)) {
    ElMessage.warning("server_id 需以小写字母开头，仅含小写字母/数字/_/-");
    return;
  }
  let endpoint = form.endpoint.trim();
  if (!endpoint) {
    ElMessage.warning("endpoint 不能为空");
    return;
  }
  if (form.allow_insecure_local_http && endpoint.startsWith("http://")) {
    // 仅回环调试允许 http，其余强制 https
  } else if (!endpoint.startsWith("https://")) {
    ElMessage.warning("endpoint 必须是 https://（回环 http 需勾选允许本地调试）");
    return;
  }
  const secretRef = form.secret_ref.trim();
  if (secretRef && !/^env:[A-Z][A-Z0-9_]{2,127}$/.test(secretRef)) {
    ElMessage.warning("secret_ref 需形如 env:MY_MCP_KEY");
    return;
  }
  const allowed = [...new Set(
    form.allowed_tools.split(/[\n,]/).map((item) => item.trim()).filter(Boolean),
  )];

  saving.value = true;
  try {
    const current = await api.request<{ config: { mcp: McpConfigShape } }>(
      "/api/v1/admin/config/current",
    );
    const config = structuredClone(current.config);
    const entry: ServerConfig = {
      server_id: serverId,
      enabled: form.enabled,
      endpoint,
      secret_value: null,
      secret_ref: secretRef || null,
      allowed_tools: allowed,
      allow_write_tools: form.allow_write_tools,
      allow_insecure_local_http: form.allow_insecure_local_http,
      connect_timeout_seconds: Math.max(1, Math.min(60, Number(form.connect_timeout_seconds) || 5)),
      call_timeout_seconds: Math.max(1, Math.min(300, Number(form.call_timeout_seconds) || 15)),
      catalog_ttl_seconds: Math.max(30, Math.min(86_400, Number(form.catalog_ttl_seconds) || 300)),
      max_result_bytes: Math.max(1_024, Math.min(1_000_000, Number(form.max_result_bytes) || 32_000)),
    };
    const existing = config.mcp.servers.find((item) => item.server_id === serverId);
    if (editingId.value && existing) {
      // 凭据留空则保持原值（掩码原样回传，由服务端还原）
      const secretChanged = form.secret_value.trim().length > 0;
      entry.secret_value = secretChanged
        ? form.secret_value.trim()
        : existing.secret_value ?? SECRET_MASK;
      if (secretChanged) entry.secret_ref = secretRef || null;
      else entry.secret_ref = existing.secret_ref;
      config.mcp.servers = config.mcp.servers.map((item) => (item.server_id === serverId ? entry : item));
    } else {
      entry.secret_value = form.secret_value.trim() || null;
      config.mcp.servers = [...config.mcp.servers, entry];
    }
    await api.request("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    editing.value = false;
    ElMessage.success(`Server ${serverId} 已保存`);
    await refreshServer(serverId);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "Server 保存失败", true);
    ElMessage.error("Server 保存失败");
  } finally {
    saving.value = false;
  }
}

async function removeServer(server: ServerConfig) {
  try {
    await ElMessageBox.confirm(
      `确认删除 Server ${server.server_id}？目录缓存会随下次刷新清除。`,
      "删除 MCP Server",
      { type: "warning" },
    );
  } catch {
    return;
  }
  await updateConfig(
    { servers: configuredServers.value.filter((item) => item.server_id !== server.server_id) },
    `Server ${server.server_id} 已删除`,
  );
}

async function refreshServer(serverId: string) {
  try {
    await api.request(`/api/v1/admin/mcp/servers/${encodeURIComponent(serverId)}/refresh`, {
      method: "POST",
    });
    ElMessage.success(`${serverId} 目录已刷新`);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "MCP 目录刷新失败", true);
  }
  await load();
}

function runtimeState(serverId: string) {
  return servers.value.find((item) => item.server_id === serverId);
}

function maskedHint(server: ServerConfig) {
  if (server.secret_value === SECRET_MASK) return "密钥已配置（保存于配置）";
  if (server.secret_ref) return `引用 ${server.secret_ref}`;
  return "匿名";
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
          管理外部 MCP Server 连接与白名单工具目录。工具发现不会自动把全部定义注入聊天上下文：
          只读工具按意图逐轮挂载，写工具只经行动计划触发。
        </p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="stats">
      <article class="switch-stat">
        <div><span>总开关</span><el-switch :model-value="enabled" :loading="saving" :disabled="saving" @change="toggleEnabled" /></div>
        <strong>{{ enabled ? "已开启" : "关闭" }}</strong>
        <small>关闭时不建立任何外部连接</small>
      </article>
      <article class="display-stat">
        <span>每轮工具上限</span>
        <el-input-number v-model="maxToolsPerTurn" :min="1" :max="16" size="small" :disabled="saving" @change="saveMaxTools" />
        <small>单回合最多挂载的只读工具数</small>
      </article>
      <article><span>已配置 Server</span><strong>{{ configuredServers.length }}</strong><small>{{ availableCount }} 个运行可用</small></article>
      <article>
        <span>授权工具</span>
        <strong>{{ tools.length }}</strong>
        <small>写操作 {{ tools.filter((item) => !item.read_only).length }} 个，仅计划可执行</small>
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
        <div><h2>Server 配置</h2><p>Streamable HTTP 端点、凭据（secret 双模式）与白名单；密钥保存后在配置中脱敏显示。</p></div>
        <el-button type="primary" size="small" @click="openCreate">添加 Server</el-button>
      </div>
      <el-table :data="configuredServers" empty-text="尚未配置 MCP Server" style="width:100%">
        <el-table-column prop="server_id" label="Server" min-width="120" />
        <el-table-column label="端点" min-width="240">
          <template #default="{ row }"><code class="endpoint">{{ row.endpoint }}</code></template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? "是" : "否" }}</el-tag></template>
        </el-table-column>
        <el-table-column label="运行状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="runtimeState(row.server_id)?.available ? 'success' : 'info'">
              {{ runtimeState(row.server_id)?.available ? "可用" : "未连接" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="凭据" min-width="150">
          <template #default="{ row }"><small>{{ maskedHint(row as ServerConfig) }}</small></template>
        </el-table-column>
        <el-table-column label="白名单工具" min-width="150">
          <template #default="{ row }">
            <small>{{ row.allowed_tools.length ? row.allowed_tools.join("、") : "全部发现工具" }}</small>
          </template>
        </el-table-column>
        <el-table-column label="写工具" width="80">
          <template #default="{ row }">{{ row.allow_write_tools ? "允许" : "禁止" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row as ServerConfig)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="removeServer(row as ServerConfig)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>运行状态</h2><p>协议协商结果与目录刷新健康。</p></div>
      </div>
      <el-table v-loading="loading" :data="servers" empty-text="暂无运行中的 Server" style="width:100%">
        <el-table-column prop="server_id" label="Server" min-width="120" />
        <el-table-column label="状态" width="100">
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
        <el-table-column prop="tool_count" label="工具" width="70" />
        <el-table-column label="最近刷新" min-width="170">
          <template #default="{ row }">{{ fmt(row.last_refresh_at) }}</template>
        </el-table-column>
        <el-table-column label="最近错误" min-width="170">
          <template #default="{ row }">{{ row.last_error ?? "—" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :disabled="!row.configured_enabled" @click="refreshServer(row.server_id)">刷新目录</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>已授权工具目录</h2><p>本地白名单与远端目录的交集；远端注解仅作参考，本地策略才是权威。</p></div>
      </div>
      <el-table v-loading="loading" :data="tools" empty-text="没有可用的白名单工具" style="width:100%">
        <el-table-column label="内部名称" min-width="220">
          <template #default="{ row }"><code>{{ row.internal_name }}</code></template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="140" />
        <el-table-column prop="description" label="描述" min-width="260" show-overflow-tooltip />
        <el-table-column label="策略" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.read_only ? 'success' : 'warning'">{{ row.read_only ? "只读" : "写操作" }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      :model-value="editing"
      :title="editingId ? `编辑 Server · ${editingId}` : '添加 MCP Server'"
      width="620px"
      @update:model-value="(value: boolean) => { if (!value) editing = false }"
    >
      <div class="form-grid">
        <label>Server ID<small>小写字母开头，a-z0-9_-</small>
          <el-input v-model="form.server_id" :disabled="!!editingId" placeholder="context7" />
        </label>
        <label>启用
          <el-switch v-model="form.enabled" />
        </label>
        <label class="wide">端点 endpoint<small>https://…/mcp；本机 http 需勾选下方允许回环调试</small>
          <el-input v-model="form.endpoint" placeholder="https://mcp.example.com/mcp" />
        </label>
        <label class="wide">API 密钥 secret_value<small>{{ editingId ? "留空保持不变（已脱敏保存）" : "Bearer 令牌，直接存配置（GET 脱敏）" }}</small>
          <el-input v-model="form.secret_value" type="password" show-password autocomplete="new-password" placeholder="留空 = 不变 / 匿名" />
        </label>
        <label class="wide">或环境变量引用 secret_ref<small>形如 env:MY_MCP_KEY；与 secret_value 二选一</small>
          <el-input v-model="form.secret_ref" placeholder="env:ARIA_MCP_CONTEXT7_API_KEY" />
        </label>
        <label class="wide">白名单工具<small>每行一个远端工具名；留空表示允许全部发现工具</small>
          <el-input v-model="form.allowed_tools" type="textarea" :rows="3" placeholder="resolve-library-id&#10;query-docs" />
        </label>
        <label>允许写工具<small>写操作仍只经行动计划</small>
          <el-switch v-model="form.allow_write_tools" />
        </label>
        <label>允许回环 HTTP<small>仅 127.0.0.1 调试用</small>
          <el-switch v-model="form.allow_insecure_local_http" />
        </label>
        <label>连接超时（秒）<el-input-number v-model="form.connect_timeout_seconds" :min="1" :max="60" size="small" /></label>
        <label>调用超时（秒）<el-input-number v-model="form.call_timeout_seconds" :min="1" :max="300" size="small" /></label>
        <label>目录缓存（秒）<el-input-number v-model="form.catalog_ttl_seconds" :min="30" :max="86400" size="small" /></label>
        <label>结果上限（字节）<el-input-number v-model="form.max_result_bytes" :min="1024" :max="1000000" :step="1000" size="small" /></label>
      </div>
      <template #footer>
        <el-button @click="editing = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveServer">保存并刷新目录</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 15px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.stats span, .stats small { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 22px; }
.switch-stat > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.display-stat :deep(.el-input-number) { width: 110px; margin: 4px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.panel-head h2 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.error-alert { border-radius: 10px; }
.endpoint { font-size: 11px; word-break: break-all; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.form-grid label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
.form-grid label small { color: var(--muted); font-size: 10px; }
.form-grid .wide { grid-column: 1 / -1; }
@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
  .form-grid { grid-template-columns: 1fr; }
}
</style>
