<script setup lang="ts">
import { computed, inject, onActivated, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage, ElMessageBox } from "element-plus";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface SafetyStatus {
  enabled: boolean;
  escalation_enabled: boolean;
  confirm_window_seconds: number;
  push_retry_minutes: number;
  active_alerts: number;
  active_authorizations: number;
}

interface AlertItem {
  id: string;
  rule_id: string;
  entity_id: string;
  message: string;
  status: string;
  level: number;
  l1_at: string;
  l2_at: string | null;
  acked_at: string | null;
  ack_source: string | null;
  created_at: string;
}

interface AuthorizationItem {
  id: string;
  contact_name: string;
  channel: string;
  destination: string;
  status: string;
  created_at: string;
  revoked_at: string | null;
}

const status = ref<SafetyStatus | null>(null);
const alerts = ref<AlertItem[]>([]);
const alertTotal = ref(0);
const alertPage = ref(1);
const alertPageSize = ref(20);
const statusFilter = ref("");
const authorizations = ref<AuthorizationItem[]>([]);
const loading = ref(false);
const saving = ref(false);
const adding = ref(false);
const addForm = ref({ contact_name: "", destination: "" });
const ownerId = ref("");

const statusLabels: Record<string, string> = {
  escalating: "升级中",
  acknowledged: "已确认",
  expired: "已到期",
};
const levelLabels: Record<number, string> = { 1: "L1 广播", 2: "L2 推送", 3: "L3 联系人" };

const visibleAlerts = computed(() => alerts.value);

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    const params = new URLSearchParams({
      limit: String(alertPageSize.value),
      offset: String((alertPage.value - 1) * alertPageSize.value),
    });
    if (statusFilter.value) params.set("status", statusFilter.value);
    const [statusResult, alertResult, authResult] = await Promise.all([
      api.request<SafetyStatus>("/api/v1/admin/safety/status"),
      api.request<{ items: AlertItem[]; total: number }>(
        `/api/v1/admin/safety/alerts?${params.toString()}`,
      ),
      api.request<{ items: AuthorizationItem[] }>("/api/v1/admin/safety/authorizations"),
    ]);
    status.value = statusResult;
    alerts.value = alertResult.items;
    alertTotal.value = alertResult.total;
    authorizations.value = authResult.items;
    if (alerts.value[0]) ownerId.value = "";
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "安全守护加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function updateConfig(patch: Record<string, unknown>, message: string) {
  saving.value = true;
  try {
    const current = await api.request<{ config: Record<string, unknown> & { safety?: Record<string, unknown> } }>(
      "/api/v1/admin/config/current",
    );
    const config = structuredClone(current.config);
    if (!config.safety) throw new Error("当前配置缺少 safety 节");
    config.safety = { ...config.safety, ...patch };
    await api.request("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    ElMessage.success(message);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "配置保存失败", true);
    ElMessage.error("配置保存失败");
  } finally {
    saving.value = false;
  }
}

async function ackAlert(item: AlertItem) {
  try {
    await api.request(`/api/v1/admin/safety/alerts/${item.id}/ack`, { method: "POST" });
    ElMessage.success("告警已确认");
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "确认失败", true);
  }
}

async function createAuthorization() {
  if (!addForm.value.contact_name.trim() || !addForm.value.destination.trim()) {
    ElMessage.warning("请填写联系人名称和邮箱");
    return;
  }
  saving.value = true;
  try {
    const user = await api.request<{ items: Array<{ user_id: string }> }>(
      "/api/v1/admin/memories?status=active&limit=1",
    );
    const userId = user.items[0]?.user_id;
    if (!userId) throw new Error("未找到用户");
    await api.request("/api/v1/admin/safety/authorizations", {
      method: "POST",
      body: JSON.stringify({
        user_id: userId,
        contact_name: addForm.value.contact_name.trim(),
        destination: addForm.value.destination.trim(),
      }),
    });
    adding.value = false;
    addForm.value = { contact_name: "", destination: "" };
    ElMessage.success("预授权已创建");
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "预授权创建失败", true);
    ElMessage.error("预授权创建失败");
  } finally {
    saving.value = false;
  }
}

async function revokeAuthorization(item: AuthorizationItem) {
  try {
    await ElMessageBox.confirm(
      `确认撤销 ${item.contact_name}（${item.destination}）的紧急联系人预授权？撤销后危急告警不再邮件通知该联系人。`,
      "撤销预授权",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/safety/authorizations/${item.id}/revoke`, {
      method: "POST",
    });
    ElMessage.success("预授权已撤销");
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "撤销失败", true);
  }
}

function onAlertPageChange() {
  void load();
}

function onFilterChange() {
  alertPage.value = 1;
  void load();
}

onActivated(() => void load());
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">SAFETY · 家庭守护</div>
        <h2>安全告警与紧急升级</h2>
        <p>
          critical 告警按 L1 全通道广播 → L2 推送重提醒 → L3 预授权联系人邮件升级；
          用户回复「知道了」或在下方确认即终止。第三方联系必须预授权并全程审计。
        </p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="stats">
      <article class="switch-stat">
        <div>
          <span>守护总开关</span>
          <el-switch
            :model-value="status?.enabled ?? false"
            :loading="saving"
            @change="(value: string | number | boolean) => updateConfig({ enabled: Boolean(value) }, Boolean(value) ? '家庭守护已开启' : '家庭守护已关闭')"
          />
        </div>
        <strong>{{ status?.enabled ? "开启" : "关闭" }}</strong>
        <small>关闭时告警回归普通提醒</small>
      </article>
      <article class="switch-stat">
        <div>
          <span>联系人升级</span>
          <el-switch
            :model-value="status?.escalation_enabled ?? false"
            :loading="saving"
            @change="(value: string | number | boolean) => updateConfig({ escalation_enabled: Boolean(value) }, Boolean(value) ? 'L3 联系人升级已开启' : 'L3 联系人升级已关闭')"
          />
        </div>
        <strong>{{ status?.escalation_enabled ? "启用" : "停用" }}</strong>
        <small>发送前仍会先告知用户</small>
      </article>
      <article><span>活跃告警</span><strong>{{ status?.active_alerts ?? 0 }}</strong><small>等待确认中</small></article>
      <article><span>预授权联系人</span><strong>{{ status?.active_authorizations ?? 0 }}</strong><small>仅邮件通道</small></article>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>告警记录</h2><p>升级链全程留痕；升级中的告警可在此确认。</p></div>
        <div class="filters">
          <el-select v-model="statusFilter" clearable placeholder="全部状态" style="width:130px" @change="onFilterChange">
            <el-option label="升级中" value="escalating" />
            <el-option label="已确认" value="acknowledged" />
            <el-option label="已到期" value="expired" />
          </el-select>
        </div>
      </div>
      <el-table v-loading="loading" :data="visibleAlerts" empty-text="还没有安全告警" style="width:100%">
        <el-table-column label="触发时间" width="170"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'escalating' ? 'danger' : row.status === 'acknowledged' ? 'success' : 'info'">
              {{ statusLabels[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="级别" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.level >= 3" size="small" type="warning">L3 已联系</el-tag>
            <span v-else>{{ levelLabels[row.level] ?? row.level }}</span>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="300">
          <template #default="{ row }"><small>{{ row.message }}</small></template>
        </el-table-column>
        <el-table-column label="确认时间" width="170"><template #default="{ row }">{{ fmt(row.acked_at) }}</template></el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'escalating'" size="small" type="primary" plain @click="ackAlert(row as AlertItem)">确认</el-button>
            <span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="alertPage"
          v-model:page-size="alertPageSize"
          :total="alertTotal"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onAlertPageChange"
          @size-change="onAlertPageChange"
        />
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div><h2>紧急联系人预授权</h2><p>授权即同意：危急告警长时间未确认时，系统会先告知你，再向该联系人发送邮件。</p></div>
        <el-button type="primary" size="small" @click="adding = true">添加预授权</el-button>
      </div>
      <el-table v-loading="loading" :data="authorizations" empty-text="尚未配置预授权联系人" style="width:100%">
        <el-table-column prop="contact_name" label="联系人" min-width="140" />
        <el-table-column prop="destination" label="邮箱" min-width="200" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
              {{ row.status === "active" ? "生效中" : "已撤销" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'active'" size="small" type="danger" plain @click="revokeAuthorization(row as AuthorizationItem)">撤销</el-button>
            <span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog :model-value="adding" title="添加紧急联系人预授权" width="460px" @update:model-value="(value: boolean) => { if (!value) adding = false }">
      <div class="auth-form">
        <label>联系人名称
          <el-input v-model="addForm.contact_name" placeholder="例如：张三 / 妈妈" />
        </label>
        <label>接收邮箱
          <el-input v-model="addForm.destination" placeholder="someone@example.com" />
        </label>
        <p class="hint">创建后即视为你已同意：危急告警（如烟雾/漏水）长时间未确认时向该邮箱发送通知；发送前系统会先告知你，且可随时撤销。</p>
      </div>
      <template #footer>
        <el-button @click="adding = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="createAuthorization">创建预授权</el-button>
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
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.panel-head h2 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.auth-form { display: grid; gap: 14px; }
.auth-form label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
.auth-form .hint { margin: 0; color: var(--muted); font-size: 11px; line-height: 1.6; }
</style>
