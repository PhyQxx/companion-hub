<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

interface UserItem {
  id: string;
  display_name: string;
  role: string;
  status: string;
  sso_sub: string | null;
  created_at: string;
  active_sessions: number;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const users = ref<UserItem[]>([]);
const loading = ref(false);
const busy = ref<string | null>(null);

const roleLabels: Record<string, string> = { owner: "业主", member: "成员" };
const statusLabels: Record<string, string> = { active: "活跃", disabled: "已停用" };

function fmt(value: string) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    const data = await api.request<{ items: UserItem[] }>("/api/v1/admin/users");
    users.value = data.items;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "用户列表加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function setStatus(item: UserItem, status: "active" | "disabled") {
  if (status === "disabled") {
    const confirmed = await ElMessageBox.confirm(
      `停用「${item.display_name}」后，其全部会话立即失效且无法再登录，直到重新启用。`,
      "停用用户",
      { confirmButtonText: "停用", cancelButtonText: "取消", type: "warning" },
    ).catch(() => false);
    if (!confirmed) return;
  }
  busy.value = item.id;
  try {
    const updated = await api.request<UserItem>(`/api/v1/admin/users/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    Object.assign(item, updated);
    emit("status", `用户 ${updated.display_name} 已${status === "active" ? "启用" : "停用"}`);
    ElMessage.success(status === "active" ? "已启用" : "已停用");
  } catch (error) {
    const message = error instanceof Error ? error.message : "操作失败";
    emit("status", message, true);
    ElMessage.error(message);
  } finally {
    busy.value = null;
  }
}

async function rename(item: UserItem) {
  const name = await ElMessageBox.prompt("修改显示名称", "改名", {
    inputValue: item.display_name,
    inputPattern: /^.{1,160}$/,
    inputErrorMessage: "名称不能为空且不超过 160 字",
    confirmButtonText: "保存",
    cancelButtonText: "取消",
  }).catch(() => null);
  if (!name || name.value === item.display_name) return;
  busy.value = item.id;
  try {
    const updated = await api.request<UserItem>(`/api/v1/admin/users/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ display_name: name.value }),
    });
    Object.assign(item, updated);
    emit("status", "名称已更新");
    ElMessage.success("已保存");
  } catch (error) {
    const message = error instanceof Error ? error.message : "操作失败";
    emit("status", message, true);
    ElMessage.error(message);
  } finally {
    busy.value = null;
  }
}

onMounted(load);
</script>

<template>
  <div class="users-view">
    <el-alert
      class="hint"
      type="info"
      :closable="false"
      show-icon
      title="新用户由 pnkx SSO 白名单（ARIA_SSO_ALLOWED_SUBS）在首次登录时自动开户；这里可停用账号、恢复访问或修改名称。"
    />
    <el-table v-loading="loading" :data="users" size="default">
      <el-table-column label="用户" min-width="180">
        <template #default="{ row }">
          <div class="user-cell">
            <span class="name">{{ row.display_name }}</span>
            <span class="sub">pnkx：{{ row.sso_sub ?? "（本地密码通道）" }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="角色" width="90">
        <template #default="{ row }">
          <el-tag :type="row.role === 'owner' ? 'warning' : 'info'" size="small">
            {{ roleLabels[row.role] ?? row.role }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'danger'" size="small">
            {{ statusLabels[row.status] ?? row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="active_sessions" label="活跃会话" width="90" />
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ fmt(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :disabled="busy === row.id" @click="rename(row)">改名</el-button>
          <el-button
            v-if="row.status === 'active'"
            size="small"
            type="danger"
            :disabled="busy === row.id"
            @click="setStatus(row, 'disabled')"
          >停用</el-button>
          <el-button
            v-else
            size="small"
            type="success"
            :disabled="busy === row.id"
            @click="setStatus(row, 'active')"
          >启用</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.users-view { display: grid; gap: 14px; }
.hint { border-radius: 10px; }
.user-cell { display: grid; gap: 2px; }
.user-cell .name { font-weight: 600; }
.user-cell .sub { color: #8a94a6; font-size: 12px; }
</style>
