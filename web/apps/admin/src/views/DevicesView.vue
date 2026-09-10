<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "registry" });

interface DeviceItem {
  id: string;
  owner_user_id: string;
  name: string;
  alias: string | null;
  client_type: string;
  capabilities: string[];
  granted_capabilities: string[];
  effective_capabilities: string[];
  revision: number;
  paired_at: string;
  last_seen_at: string;
  revoked_at: string | null;
  online: boolean;
}

interface PairingCodeResult {
  pairing_code: string;
  owner_user_id: string;
  granted_capabilities: string[];
  expires_at: string;
}

interface CommandItem {
  id: string;
  device_id: string;
  command: string;
  args_redacted: Record<string, unknown>;
  idempotency_key: string;
  status: string;
  revision: number;
  issued_at: string;
  expires_at: string;
  sent_at: string | null;
  acknowledged_at: string | null;
  completed_at: string | null;
  reason_code: string | null;
  result_meta: Record<string, unknown> | null;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const knownCapabilities = [
  "device.ping",
  "avatar.render",
  "avatar.chat",
  "screen.capture",
  "screen.monitor",
  "notification.show",
  "browser.current_tab.read",
  "browser.current_tab.capture",
  "sensor.read",
];
const clientTypeLabels: Record<string, string> = {
  desktop: "桌面客户端",
  browser: "浏览器",
  mobile: "移动端",
  iot: "IoT",
  other: "其他",
};
const commandStatusLabels: Record<string, string> = {
  pending: "等待发送",
  sent: "已发送",
  acknowledged: "已确认",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
  timed_out: "超时",
};

const devices = ref<DeviceItem[]>([]);
const commands = ref<CommandItem[]>([]);
const loading = ref(false);
const commandLoading = ref(false);
const includeRevoked = ref(false);
const selectedDeviceId = ref("");
const pairingOpen = ref(false);
const pairingResult = ref<PairingCodeResult | null>(null);
const editOpen = ref(false);
const editing = ref<DeviceItem | null>(null);
const issueOpen = ref(false);
const issuing = ref<DeviceItem | null>(null);

const pairForm = reactive({
  ownerUserId: "",
  ttlSeconds: 600,
  grants: ["device.ping"] as string[],
});
const editForm = reactive({ name: "", alias: "", grants: [] as string[] });
const issueForm = reactive({
  command: "",
  args: "{}",
  ttlSeconds: 30,
  idempotencyKey: "",
});

const visibleDevices = computed(() =>
  includeRevoked.value ? devices.value : devices.value.filter((item) => !item.revoked_at),
);
const onlineCount = computed(
  () => devices.value.filter((item) => item.online && !item.revoked_at).length,
);
const activeCount = computed(() => devices.value.filter((item) => !item.revoked_at).length);
const effectiveCount = computed(
  () =>
    new Set(
      devices.value
        .filter((item) => item.online && !item.revoked_at)
        .flatMap((item) => item.effective_capabilities),
    ).size,
);
const selectedDeviceName = computed(
  () => devices.value.find((item) => item.id === selectedDeviceId.value)?.name ?? "全部设备",
);

const commandPage = ref(1);
const commandPageSize = ref(20);
// 感知循环的轮询命令（screen-monitor-* / browser-observe-*）默认折叠，保持台账聚焦人工操作
const AWARENESS_POLL_PREFIXES = ["screen-monitor-", "browser-observe-"];
const hidePollCommands = ref(true);
const visibleCommands = computed(() =>
  hidePollCommands.value
    ? commands.value.filter(
        (item) => !AWARENESS_POLL_PREFIXES.some((prefix) => item.idempotency_key.startsWith(prefix)),
      )
    : commands.value,
);
const pagedCommands = computed(() =>
  visibleCommands.value.slice(
    (commandPage.value - 1) * commandPageSize.value,
    commandPage.value * commandPageSize.value,
  ),
);

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

function relativeTime(value: string) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function statusType(value: string) {
  if (value === "succeeded") return "success";
  if (["failed", "timed_out"].includes(value)) return "danger";
  if (["cancelled", "expired"].includes(value)) return "warning";
  return "info";
}

async function loadDevices() {
  loading.value = true;
  try {
    devices.value = await api.request<DeviceItem[]>("/api/v1/admin/devices");
    if (
      selectedDeviceId.value &&
      !devices.value.some((item) => item.id === selectedDeviceId.value)
    ) {
      selectedDeviceId.value = "";
    }
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "设备加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function loadCommands() {
  commandLoading.value = true;
  try {
    const query = selectedDeviceId.value
      ? `?device_id=${encodeURIComponent(selectedDeviceId.value)}&limit=100`
      : "?limit=100";
    commands.value = await api.request<CommandItem[]>(
      `/api/v1/admin/device-commands${query}`,
    );
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "命令台账加载失败", true);
  } finally {
    commandLoading.value = false;
  }
}

async function onDeviceFilterChange() {
  commandPage.value = 1;
  await loadCommands();
}

async function refresh() {
  await Promise.all([loadDevices(), loadCommands()]);
  emit("status", "设备状态与命令台账已刷新");
}

function openPairing() {
  pairingResult.value = null;
  pairForm.ownerUserId = "";
  pairForm.grants = ["device.ping"];
  pairForm.ttlSeconds = 600;
  pairingOpen.value = true;
}

/** 按现有设备预填所有者与授权能力，生成新的重新配对码；一次性码只显示一次。 */
function openPairingFor(item: DeviceItem) {
  pairingResult.value = null;
  pairForm.ownerUserId = item.owner_user_id;
  pairForm.grants = item.granted_capabilities.length
    ? [...item.granted_capabilities]
    : ["device.ping"];
  pairForm.ttlSeconds = 600;
  pairingOpen.value = true;
}

async function createPairingCode() {
  try {
    const body: Record<string, unknown> = {
      granted_capabilities: pairForm.grants,
      ttl_seconds: pairForm.ttlSeconds,
    };
    if (pairForm.ownerUserId.trim()) body.owner_user_id = pairForm.ownerUserId.trim();
    pairingResult.value = await api.request<PairingCodeResult>(
      "/api/v1/admin/devices/pairing-codes",
      { method: "POST", body: JSON.stringify(body) },
    );
    emit("status", "一次性配对码已生成");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "生成配对码失败", true);
  }
}

async function copyPairingCode() {
  if (!pairingResult.value) return;
  try {
    await navigator.clipboard.writeText(pairingResult.value.pairing_code);
    ElMessage.success("配对码已复制");
  } catch {
    ElMessage.warning("无法自动复制，请手动选择配对码");
  }
}

function openEdit(item: DeviceItem) {
  editing.value = item;
  editForm.name = item.name;
  editForm.alias = item.alias ?? "";
  editForm.grants = [...item.granted_capabilities];
  editOpen.value = true;
}

async function saveDevice() {
  if (!editing.value) return;
  try {
    await api.request<DeviceItem>(`/api/v1/admin/devices/${editing.value.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: editing.value.revision,
        name: editForm.name,
        alias: editForm.alias.trim() || null,
        granted_capabilities: editForm.grants,
      }),
    });
    editOpen.value = false;
    emit("status", `${editForm.name} 已更新`);
    await loadDevices();
  } catch (error) {
    const message = error instanceof Error ? error.message : "设备更新失败";
    if (/revision/i.test(message)) {
      // 设备心跳期间能力声明可能变化导致版本冲突：刷新版本号但保留正在编辑的内容
      await loadDevices();
      const fresh = devices.value.find((item) => item.id === editing.value?.id);
      if (fresh) {
        editing.value = fresh;
        emit("status", "设备心跳期间能力有更新，版本号已刷新，请再次点击保存", true);
        return;
      }
    }
    emit("status", message, true);
  }
}

async function revoke(item: DeviceItem) {
  try {
    await ElMessageBox.confirm(
      `撤销「${item.name}」？设备凭据将立即失效，之后需要重新配对。`,
      "撤销设备",
      {
        type: "warning",
        confirmButtonText: "确认撤销",
        cancelButtonText: "取消",
      },
    );
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/devices/${item.id}/revoke`, { method: "POST" });
    emit("status", `${item.name} 已撤销`);
    await refresh();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "撤销失败", true);
  }
}

function openIssue(item: DeviceItem) {
  issuing.value = item;
  issueForm.command = item.effective_capabilities[0] ?? "";
  issueForm.args = "{}";
  issueForm.ttlSeconds = 30;
  issueForm.idempotencyKey = `admin-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  issueOpen.value = true;
}

async function issueCommand() {
  if (!issuing.value || !issueForm.command) return;
  let args: Record<string, unknown>;
  try {
    const parsed = JSON.parse(issueForm.args) as unknown;
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("参数必须是 JSON 对象");
    }
    args = parsed as Record<string, unknown>;
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "参数 JSON 无效");
    return;
  }
  try {
    const result = await api.request<CommandItem>(
      `/api/v1/admin/devices/${issuing.value.id}/commands`,
      {
        method: "POST",
        body: JSON.stringify({
          command: issueForm.command,
          args,
          idempotency_key: issueForm.idempotencyKey,
          ttl_seconds: issueForm.ttlSeconds,
        }),
      },
    );
    issueOpen.value = false;
    emit("status", `命令 ${result.command}：${commandStatusLabels[result.status] ?? result.status}`);
    selectedDeviceId.value = issuing.value.id;
    commandPage.value = 1;
    await loadCommands();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "命令下发失败", true);
  }
}

async function cancelCommand(item: CommandItem) {
  try {
    await api.request(`/api/v1/admin/device-commands/${item.id}/cancel`, {
      method: "POST",
    });
    emit("status", `命令 ${item.id.slice(0, 8)} 已取消`);
    await loadCommands();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "取消失败", true);
  }
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">M3A · 多终端底座</div>
        <h2>设备与现实能力</h2>
        <p>设备只有在线声明且经管理员授权的能力才会进入模型上下文。配对码仅显示一次，撤销后设备凭据立即失效。</p>
      </div>
      <div class="hero-actions">
        <el-button @click="refresh">刷新状态</el-button>
        <el-button type="primary" @click="openPairing">生成配对码</el-button>
      </div>
    </div>

    <div class="stats">
      <article><span>活跃设备</span><strong>{{ activeCount }}</strong><small>含离线设备</small></article>
      <article><span>当前在线</span><strong>{{ onlineCount }}</strong><small>90 秒心跳窗口</small></article>
      <article><span>可用能力</span><strong>{{ effectiveCount }}</strong><small>授权与声明的交集</small></article>
      <article><span>最近命令</span><strong>{{ commands.length }}</strong><small>最多显示 100 条</small></article>
    </div>

    <div v-if="props.mode === 'registry'" class="panel device-panel">
      <div class="panel-head">
        <div><h2>设备注册表</h2><p>名称、在线状态、能力快照和授权策略。</p></div>
        <el-switch v-model="includeRevoked" active-text="显示已撤销" />
      </div>
      <el-table v-loading="loading" :data="visibleDevices" empty-text="还没有配对设备" style="width:100%">
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <span v-if="row.revoked_at" class="state revoked">已撤销</span>
            <span v-else-if="row.online" class="state online">在线</span>
            <span v-else class="state offline">离线</span>
          </template>
        </el-table-column>
        <el-table-column label="设备" min-width="190">
          <template #default="{ row }">
            <strong class="device-name">{{ row.name }}</strong>
            <small>{{ row.alias ? `${row.alias} · ` : "" }}{{ clientTypeLabels[row.client_type] ?? row.client_type }}</small>
          </template>
        </el-table-column>
        <el-table-column label="当前能力" min-width="270">
          <template #default="{ row }">
            <div v-if="row.effective_capabilities.length" class="tag-list">
              <el-tag v-for="capability in row.effective_capabilities" :key="capability" size="small" type="success" effect="plain">{{ capability }}</el-tag>
            </div>
            <span v-else class="muted">暂无有效能力</span>
            <small v-if="row.capabilities.length !== row.effective_capabilities.length">声明 {{ row.capabilities.length }} · 授权 {{ row.granted_capabilities.length }}</small>
          </template>
        </el-table-column>
        <el-table-column label="最后心跳" min-width="150">
          <template #default="{ row }"><span>{{ relativeTime(row.last_seen_at) }}</span><small>{{ fmt(row.last_seen_at) }}</small></template>
        </el-table-column>
        <el-table-column label="操作" width="330" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button size="small" :disabled="!!row.revoked_at" @click="openEdit(row as DeviceItem)">设置</el-button>
              <el-button size="small" :disabled="!row.online || !row.effective_capabilities.length || !!row.revoked_at" @click="openIssue(row as DeviceItem)">测试命令</el-button>
              <el-button size="small" @click="openPairingFor(row as DeviceItem)">配对码</el-button>
              <el-button size="small" type="danger" plain :disabled="!!row.revoked_at" @click="revoke(row as DeviceItem)">撤销</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-if="props.mode === 'commands'" class="panel command-panel">
      <div class="panel-head">
        <div><h2>命令台账</h2><p>{{ selectedDeviceName }} · 参数与结果均为脱敏摘要。</p></div>
        <div class="command-filters">
          <el-checkbox v-model="hidePollCommands">隐藏感知轮询</el-checkbox>
          <el-select v-model="selectedDeviceId" placeholder="全部设备" clearable @change="onDeviceFilterChange">
            <el-option v-for="item in devices" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </div>
      </div>
      <el-table v-loading="commandLoading" :data="pagedCommands" empty-text="没有命令记录" style="width:100%">
        <el-table-column label="发起时间" min-width="170"><template #default="{ row }">{{ fmt(row.issued_at) }}</template></el-table-column>
        <el-table-column prop="command" label="命令" min-width="180" />
        <el-table-column label="设备" min-width="150"><template #default="{ row }">{{ devices.find(item => item.id === row.device_id)?.name ?? row.device_id.slice(0, 8) }}</template></el-table-column>
        <el-table-column label="状态" width="105"><template #default="{ row }"><el-tag :type="statusType(row.status)" size="small">{{ commandStatusLabels[row.status] ?? row.status }}</el-tag></template></el-table-column>
        <el-table-column label="原因/结果" min-width="220">
          <template #default="{ row }">
            <span>{{ row.reason_code ?? (row.result_meta ? JSON.stringify(row.result_meta) : "—") }}</span>
            <small>幂等键 {{ row.idempotency_key }}</small>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }"><el-button v-if="['pending', 'sent', 'acknowledged'].includes(row.status)" size="small" type="warning" plain @click="cancelCommand(row as CommandItem)">取消</el-button></template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="commandPage"
          v-model:page-size="commandPageSize"
          :total="visibleCommands.length"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </div>

    <el-dialog v-model="pairingOpen" title="生成一次性配对码" width="620px">
      <div class="dialog-stack">
        <el-alert title="配对码只能使用一次，到期自动失效；仅本次显示，请立即复制。从设备行进入时已预填该设备的所有者与授权能力。" type="info" :closable="false" />
        <template v-if="!pairingResult">
          <el-form label-position="top">
            <el-form-item label="Owner user_id（可选）"><el-input v-model="pairForm.ownerUserId" placeholder="单用户环境可留空" /></el-form-item>
            <el-form-item label="初始授权能力"><el-select v-model="pairForm.grants" multiple filterable allow-create default-first-option style="width:100%"><el-option v-for="item in knownCapabilities" :key="item" :label="item" :value="item" /></el-select></el-form-item>
            <el-form-item label="有效期"><el-select v-model="pairForm.ttlSeconds"><el-option label="5 分钟" :value="300" /><el-option label="10 分钟" :value="600" /><el-option label="30 分钟" :value="1800" /></el-select></el-form-item>
          </el-form>
        </template>
        <template v-else>
          <div class="pairing-secret"><code>{{ pairingResult.pairing_code }}</code><el-button type="primary" @click="copyPairingCode">复制</el-button></div>
          <dl class="pairing-meta"><dt>所有者</dt><dd>{{ pairingResult.owner_user_id }}</dd><dt>到期时间</dt><dd>{{ fmt(pairingResult.expires_at) }}</dd><dt>授权能力</dt><dd>{{ pairingResult.granted_capabilities.join("、") || "无" }}</dd></dl>
        </template>
      </div>
      <template #footer><el-button @click="pairingOpen = false">关闭</el-button><el-button v-if="!pairingResult" type="primary" @click="createPairingCode">生成配对码</el-button></template>
    </el-dialog>

    <el-dialog v-model="editOpen" title="设备设置" width="620px">
      <el-form v-if="editing" label-position="top">
        <el-form-item label="显示名称"><el-input v-model="editForm.name" /></el-form-item>
        <el-form-item label="设备别名"><el-input v-model="editForm.alias" placeholder="例如：我的电脑" /></el-form-item>
        <el-form-item label="授权能力"><el-select v-model="editForm.grants" multiple filterable allow-create default-first-option style="width:100%"><el-option v-for="item in Array.from(new Set([...knownCapabilities, ...editing.capabilities]))" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <p class="dialog-hint">只有“终端当前声明”与“这里授权”的交集才会成为有效能力。保存使用 revision {{ editing.revision }} 做冲突保护。</p>
      </el-form>
      <template #footer><el-button @click="editOpen = false">取消</el-button><el-button type="primary" @click="saveDevice">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="issueOpen" title="下发测试命令" width="620px">
      <el-form v-if="issuing" label-position="top">
        <el-alert title="只可选择当前在线且已授权的能力。命令参数仅通过在线连接单次发送，台账只保留脱敏摘要。" type="warning" :closable="false" />
        <el-form-item label="命令"><el-select v-model="issueForm.command" style="width:100%"><el-option v-for="item in issuing.effective_capabilities" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <el-form-item label="参数 JSON"><el-input v-model="issueForm.args" type="textarea" :rows="5" /></el-form-item>
        <el-form-item label="TTL"><el-input-number v-model="issueForm.ttlSeconds" :min="1" :max="120" /> 秒</el-form-item>
        <el-form-item label="幂等键"><el-input v-model="issueForm.idempotencyKey" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="issueOpen = false">取消</el-button><el-button type="primary" @click="issueCommand">下发</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p, .panel-head p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.hero-actions, .row-actions, .tag-list { display: flex; gap: 7px; flex-wrap: wrap; }
.stats { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; }
.stats article { display: grid; gap: 4px; padding: 15px 16px; background: #fff; border: 1px solid var(--line); border-radius: 12px; }
.stats span, small, .muted { color: var(--muted); font-size: 11px; }
.stats strong { font-size: 24px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.panel-head :deep(.el-select) { width: 220px; }
.command-filters { display: flex; align-items: center; gap: 14px; }
.command-filters :deep(.el-checkbox) { height: auto; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.device-name { display: block; font-size: 13px; margin-bottom: 4px; }
.state { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.state::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #a8b0bf; }
.state.online { color: #138a54; }
.state.online::before { background: #26b873; box-shadow: 0 0 0 4px #e3f8ed; }
.state.revoked { color: var(--danger); }
.state.revoked::before { background: var(--danger); }
.dialog-stack { display: grid; gap: 16px; }
.pairing-secret { display: flex; align-items: center; gap: 10px; padding: 14px; border: 1px solid #cfd9ff; background: #f4f7ff; border-radius: 10px; }
.pairing-secret code { flex: 1; overflow-wrap: anywhere; font-size: 12px; }
.pairing-meta { display: grid; grid-template-columns: 80px 1fr; gap: 8px 12px; margin: 0; font-size: 12px; }
.pairing-meta dt { color: var(--muted); }
.pairing-meta dd { margin: 0; overflow-wrap: anywhere; }
.dialog-hint { color: var(--muted); font-size: 12px; line-height: 1.6; margin: 0; }

@media (max-width: 1000px) {
  .stats { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
  .hero { align-items: flex-start; }
}
@media (max-width: 720px) {
  .content { padding: 14px; }
  .hero, .panel-head { display: grid; }
  .stats { grid-template-columns: 1fr 1fr; }
  .hero-actions { width: 100%; }
}
</style>
