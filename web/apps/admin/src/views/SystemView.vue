<script setup lang="ts">
import { inject, onMounted, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "general" });
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface UserIdentity {
  id: string;
  display_name: string;
  status: string;
  created_at: string;
  session_count: number;
}

interface StorageTable {
  name: string;
  row_count: number;
}

interface SystemData {
  version: string;
  users: UserIdentity[];
  active_sessions: number;
  total_conversations: number;
  storage: StorageTable[];
  database_url_type: string;
}

interface BackupFileInfo {
  file: string;
  size_bytes: number;
  modified_at: string;
}

interface BackupStatus {
  available: boolean;
  dir: string;
  keep_days: number;
  backup_at: string;
  timezone: string;
  stale_after_hours: number;
  items: BackupFileInfo[];
  last_backup_at: string | null;
  healthy: boolean;
}

const data = ref<SystemData | null>(null);
const backups = ref<BackupStatus | null>(null);
const backupsLoading = ref(false);
const loading = ref(false);

const adminToken = ref("");
const adminTokenConfirm = ref("");
const adminTokenLoading = ref(false);

const chatPassword = ref("");
const chatPasswordConfirm = ref("");
const chatPasswordLoading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    data.value = await api.request<SystemData>("/api/v1/admin/dashboard/system");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function refreshBackups() {
  backupsLoading.value = true;
  try {
    backups.value = await api.request<BackupStatus>("/api/v1/admin/backups");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "备份状态加载失败", true);
  } finally {
    backupsLoading.value = false;
  }
}

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${value} B`;
}

function backupAgeHours(): string | null {
  if (!backups.value?.last_backup_at) return null;
  const hours = (Date.now() - new Date(backups.value.last_backup_at).getTime()) / 3600000;
  return hours < 1 ? "刚刚" : hours < 48 ? `${hours.toFixed(1)} 小时前` : `${(hours / 24).toFixed(1)} 天前`;
}

// ---- 数据导出/导入（FR-S3）：伴侣数据 JSON 档案 ----

const exporting = ref(false);
const importing = ref(false);
const importResult = ref("");

async function downloadExport() {
  exporting.value = true;
  try {
    const response = await fetch("/api/v1/admin/data/export", {
      headers: { Authorization: `Bearer ${api.token}` },
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail ?? `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `aria-export-${stamp}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    emit("status", "导出已下载（包含全部私密内容，妥善保管）");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "导出失败", true);
  } finally {
    exporting.value = false;
  }
}

async function onImportFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  let archive: unknown;
  try {
    archive = JSON.parse(await file.text()) as unknown;
  } catch {
    emit("status", "文件不是有效的 JSON", true);
    return;
  }
  const confirmed = await ElMessageBox.confirm(
    "导入会把档案中的对话、记忆、时间线等全部写入当前数据库（仅支持空库恢复），且不会自动发布人格或触发任何投递。确认继续？",
    "导入数据档案",
    { type: "warning", confirmButtonText: "确认导入", cancelButtonText: "取消" },
  ).then(() => true).catch(() => false);
  if (!confirmed) return;
  importing.value = true;
  importResult.value = "";
  try {
    const summary = await api.request<{ inserted: Record<string, number>; skipped_tables: string[] }>(
      "/api/v1/admin/data/import",
      { method: "POST", body: JSON.stringify({ archive, confirm: true }) },
    );
    const parts = Object.entries(summary.inserted).map(([table, count]) => `${table} ${count}`);
    importResult.value = `导入完成：${parts.join("、") || "无数据"}`;
    emit("status", importResult.value);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "导入失败", true);
  } finally {
    importing.value = false;
  }
}

async function submitAdminToken() {
  if (adminToken.value.length < 8) {
    emit("status", "admin token 至少需要 8 位", true);
    return;
  }
  if (adminToken.value !== adminTokenConfirm.value) {
    emit("status", "两次输入的 token 不一致", true);
    return;
  }
  adminTokenLoading.value = true;
  try {
    await api.request("/api/v1/admin/system/admin-token", {
      method: "POST",
      body: JSON.stringify({ new_token: adminToken.value }),
    });
    emit("status", "admin token 已更新，请使用新 token 重新登录", false);
    adminToken.value = "";
    adminTokenConfirm.value = "";
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "更新失败", true);
  } finally {
    adminTokenLoading.value = false;
  }
}

async function submitChatPassword() {
  if (chatPassword.value.length < 8) {
    emit("status", "密码至少需要 8 位", true);
    return;
  }
  if (chatPassword.value !== chatPasswordConfirm.value) {
    emit("status", "两次输入的密码不一致", true);
    return;
  }
  chatPasswordLoading.value = true;
  try {
    await api.request("/api/v1/admin/system/chat-password", {
      method: "POST",
      body: JSON.stringify({ new_password: chatPassword.value }),
    });
    emit("status", "聊天密码已重置，所有会话已撤销", false);
    chatPassword.value = "";
    chatPasswordConfirm.value = "";
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "重置失败", true);
  } finally {
    chatPasswordLoading.value = false;
  }
}

onMounted(() => {
  void refresh();
  if (props.mode === "storage") void refreshBackups();
});

// KeepAlive 缓存组件：切到存储与备份 Tab 时按需加载备份状态
watch(
  () => props.mode,
  (mode) => {
    if (mode === "storage") void refreshBackups();
  },
);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <div v-if="data" v-loading="loading">
      <!-- 基础设置 -->
      <div v-if="mode === 'general'">
        <div class="stats">
          <el-card shadow="never"><span>版本</span><strong>{{ data.version }}</strong></el-card>
          <el-card shadow="never"><span>数据库</span><strong>{{ data.database_url_type }}</strong></el-card>
          <el-card shadow="never"><span>用户</span><strong>{{ data.users.length }}</strong></el-card>
          <el-card shadow="never"><span>会话</span><strong>{{ data.active_sessions }}</strong></el-card>
        </div>
        <el-card shadow="never" style="margin-top: 12px;">
          <template #header><span>实例信息</span></template>
          <div class="meta">
            <div><dt>系统版本</dt><dd>{{ data.version }}</dd></div>
            <div><dt>数据库类型</dt><dd>{{ data.database_url_type }}</dd></div>
            <div><dt>活跃会话</dt><dd>{{ data.active_sessions }}</dd></div>
            <div><dt>总会话</dt><dd>{{ data.total_conversations }}</dd></div>
          </div>
        </el-card>
      </div>

      <!-- 身份与会话 -->
      <div v-if="mode === 'identity'">
        <el-table :data="data.users" size="small" style="margin-bottom: 16px;">
          <el-table-column prop="id" label="ID" width="220"><template #default="{ row }"><code>{{ row.id.slice(0, 8) }}</code></template></el-table-column>
          <el-table-column prop="display_name" label="显示名" width="140" />
          <el-table-column prop="status" label="状态" width="90"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">{{ row.status }}</el-tag></template></el-table-column>
          <el-table-column prop="session_count" label="会话数" width="90" />
          <el-table-column prop="created_at" label="创建时间" width="170"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template></el-table-column>
        </el-table>

        <el-row :gutter="16">
          <el-col :span="12">
            <el-card shadow="never">
              <template #header><span>修改 Admin Token</span></template>
              <el-form label-position="top" size="small" @submit.prevent="submitAdminToken">
                <el-form-item label="新 Token">
                  <el-input v-model="adminToken" type="password" show-password placeholder="至少 8 位" />
                </el-form-item>
                <el-form-item label="确认 Token">
                  <el-input v-model="adminTokenConfirm" type="password" show-password placeholder="再次输入" />
                </el-form-item>
                <el-button type="primary" size="small" :loading="adminTokenLoading" @click="submitAdminToken">更新</el-button>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="never">
              <template #header><span>重置聊天密码</span></template>
              <el-form label-position="top" size="small" @submit.prevent="submitChatPassword">
                <el-form-item label="新密码">
                  <el-input v-model="chatPassword" type="password" show-password placeholder="至少 8 位" />
                </el-form-item>
                <el-form-item label="确认密码">
                  <el-input v-model="chatPasswordConfirm" type="password" show-password placeholder="再次输入" />
                </el-form-item>
                <el-button type="primary" size="small" :loading="chatPasswordLoading" @click="submitChatPassword">重置</el-button>
              </el-form>
            </el-card>
          </el-col>
        </el-row>
      </div>

      <!-- 可观测性 -->
      <div v-if="mode === 'observability'">
        <el-card shadow="never">
          <template #header><span>日志与追踪</span></template>
          <div class="meta">
            <div><dt>日志级别</dt><dd>由 ARIA_LOG_LEVEL 环境变量控制</dd></div>
            <div><dt>结构化日志</dt><dd>已启用</dd></div>
            <div><dt>字段脱敏</dt><dd>已启用</dd></div>
            <div><dt>性能追踪</dt><dd>InMemorySpanSink</dd></div>
          </div>
        </el-card>
        <el-alert title="日志保留期、Trace 采样率与告警策略配置后续补充" type="info" :closable="false" show-icon style="margin-top: 12px;" />
      </div>

      <!-- 存储与备份 -->
      <div v-if="mode === 'storage'">
        <el-card shadow="never" class="backup-card">
          <template #header>
            <div class="backup-head">
              <span>数据库每日备份（BK-01）</span>
              <div class="backup-head-actions">
                <el-tag v-if="backups" :type="backups.healthy ? 'success' : 'danger'" size="small">
                  {{ backups.healthy ? "健康" : backups.available ? "最近备份过期" : "备份目录不可用" }}
                </el-tag>
                <el-button size="small" :loading="backupsLoading" @click="refreshBackups">刷新</el-button>
              </div>
            </div>
          </template>
          <template v-if="backups">
            <div class="backup-meta">
              <div><dt>计划时刻</dt><dd>{{ backups.backup_at }}（{{ backups.timezone }}）每日</dd></div>
              <div><dt>保留</dt><dd>{{ backups.keep_days }} 天滚动清理</dd></div>
              <div><dt>最近备份</dt><dd>{{ backupAgeHours() ?? "尚无备份" }}</dd></div>
              <div><dt>转储目录</dt><dd><code>{{ backups.dir }}</code></dd></div>
            </div>
            <el-table v-if="backups.items.length" :data="backups.items" size="small" style="margin-top: 12px;">
              <el-table-column prop="file" label="文件" min-width="220" />
              <el-table-column label="大小" width="100">
                <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
              </el-table-column>
              <el-table-column label="时间" width="170">
                <template #default="{ row }">{{ new Date(row.modified_at).toLocaleString() }}</template>
              </el-table-column>
            </el-table>
            <el-alert
              v-else
              :title="backups.available ? '备份目录为空：确认 backup profile 已启用（docker compose --profile backup up -d）' : 'Hub 看不到备份目录：确认 compose 已把 ./backups 只读挂载到 /backups'"
              type="warning"
              :closable="false"
              show-icon
              style="margin-top: 12px;"
            />
            <p class="backup-hint">转储与清理由 backup profile 容器执行，Hub 只读检视；恢复步骤见 docs/07「数据备份与恢复」。</p>
          </template>
          <el-skeleton v-else :rows="3" animated />
        </el-card>

        <el-card shadow="never" class="backup-card">
          <template #header><span>数据导出与导入（FR-S3）</span></template>
          <div class="export-actions">
            <el-button type="primary" plain :loading="exporting" @click="downloadExport">导出 JSON 档案</el-button>
            <el-button :loading="importing" @click="($refs.importInput as HTMLInputElement | undefined)?.click()">选择档案导入…</el-button>
            <input ref="importInput" type="file" accept="application/json,.json" style="display:none" @change="onImportFile" />
          </div>
          <p class="backup-hint">
            档案包含对话、消息、记忆（含来源）、时间线、任务、承诺、联系人、日历、简报/回顾、会议、流程与场景及人格版本；
            登录凭据、设备注册、推送订阅、OAuth 令牌与配置中心不会导出。文件含全部私密内容，敏感度等同数据库备份。
            导入仅支持恢复到空库。
          </p>
          <el-alert v-if="importResult" :title="importResult" type="success" :closable="false" show-icon style="margin-top: 10px;" />
        </el-card>

        <el-table :data="data.storage" size="small" style="margin-top: 12px;">
          <el-table-column prop="name" label="表名" width="200" />
          <el-table-column prop="row_count" label="行数" width="120">
            <template #default="{ row }">
              <el-tag :type="row.row_count > 10000 ? 'warning' : 'success'" size="small">{{ row.row_count.toLocaleString() }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 升级与信息 -->
      <div v-if="mode === 'updates'">
        <el-card shadow="never">
          <template #header><span>版本信息</span></template>
          <div class="meta">
            <div><dt>当前版本</dt><dd>{{ data.version }}</dd></div>
            <div><dt>数据库类型</dt><dd>{{ data.database_url_type }}</dd></div>
          </div>
        </el-card>
        <el-alert title="迁移状态、更新检查与系统信息后续补充" type="info" :closable="false" show-icon style="margin-top: 12px;" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.meta { display: grid; gap: 8px; }
.meta div { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.backup-head { display: flex; justify-content: space-between; align-items: center; }
.backup-head-actions { display: flex; align-items: center; gap: 10px; }
.backup-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.backup-meta div { display: grid; gap: 3px; font-size: 12px; }
.backup-meta dt { color: var(--muted); font-size: 11px; }
.backup-meta dd { margin: 0; overflow-wrap: anywhere; }
.backup-hint { margin: 12px 0 0; color: var(--muted); font-size: 12px; line-height: 1.7; }
.export-actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-weight: 600; }
code { font-size: 11px; background: #f5f7fa; padding: 1px 4px; border-radius: 3px; }
</style>
