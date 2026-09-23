<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

interface WorkflowStep {
  action_id: string;
  arguments: Record<string, unknown>;
}

interface WorkflowItem {
  id: string;
  name: string;
  description: string | null;
  steps: WorkflowStep[];
  created_at: string | null;
  updated_at: string | null;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const workflows = ref<WorkflowItem[]>([]);
const loading = ref(false);

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    workflows.value = await api.request<WorkflowItem[]>("/api/v1/admin/butler/workflows");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "流程加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function remove(item: WorkflowItem) {
  try {
    await ElMessageBox.confirm(
      `删除「${item.name}」只移除模板，不影响已展开执行的计划。`,
      "删除流程",
      { type: "warning", confirmButtonText: "确认删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/butler/workflows/${item.id}`, { method: "DELETE" });
    emit("status", `流程 ${item.name} 已删除`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "删除失败", true);
  }
}

onMounted(load);
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">个人管家 · FLOW-01</div>
        <h2>可复用流程</h2>
        <p>已保存的动作模板（1-10 步，保存时经动作注册表编译校验）。运行须在聊天里说「运行某某流程」，展开为待确认计划后执行——Admin 只读，不提供运行入口。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="panel">
      <el-table v-loading="loading" :data="workflows" empty-text="还没有保存的流程（在聊天里说“把这个流程存下来”即可创建）" style="width:100%">
        <el-table-column label="名称" min-width="150">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong>
            <small v-if="row.description" class="muted">{{ row.description }}</small>
          </template>
        </el-table-column>
        <el-table-column label="步骤" min-width="320">
          <template #default="{ row }">
            <div class="steps">
              <span v-for="(step, index) in row.steps" :key="index" class="step-chip">
                <small>{{ index + 1 }}</small>{{ step.action_id }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ fmt(row.updated_at ?? row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="danger" plain @click="remove(row as WorkflowItem)">删除</el-button>
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
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.muted { color: var(--muted); font-size: 11px; display: block; margin-top: 3px; }
.steps { display: flex; gap: 6px; flex-wrap: wrap; }
.step-chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border: 1px solid #dfe5f1; border-radius: 6px; background: #f8fafd; font-size: 11px; }
.step-chip small { color: var(--muted); font-size: 10px; }
</style>
