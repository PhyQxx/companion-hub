<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
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

const data = ref<SystemData | null>(null);
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

onMounted(refresh);
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
        <el-table :data="data.storage" size="small">
          <el-table-column prop="name" label="表名" width="200" />
          <el-table-column prop="row_count" label="行数" width="120">
            <template #default="{ row }">
              <el-tag :type="row.row_count > 10000 ? 'warning' : 'success'" size="small">{{ row.row_count.toLocaleString() }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <el-alert title="备份策略与配额管理后续补充" type="info" :closable="false" show-icon style="margin-top: 12px;" />
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
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-weight: 600; }
code { font-size: 11px; background: #f5f7fa; padding: 1px 4px; border-radius: 3px; }
</style>
