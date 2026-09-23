<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

interface SceneStep {
  action_id: string;
  arguments: Record<string, unknown>;
}

interface SceneItem {
  id: string;
  name: string;
  trigger: string;
  window_start: string | null;
  window_end: string | null;
  steps: SceneStep[];
  enabled: boolean;
  updated_at: string | null;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const scenes = ref<SceneItem[]>([]);
const loading = ref(false);
const toggling = ref<string | null>(null);

const triggerLabels: Record<string, string> = {
  user_arrived_home: "到家",
  user_left_home: "离家",
  presence_changed: "在场变化",
  manual: "手动",
};

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    scenes.value = await api.request<SceneItem[]>("/api/v1/admin/butler/scenes");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "场景加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function setEnabled(item: SceneItem, enabled: boolean) {
  toggling.value = item.id;
  try {
    const updated = await api.request<SceneItem>(
      `/api/v1/admin/butler/scenes/${item.id}/${enabled ? "enable" : "disable"}`,
      { method: "POST" },
    );
    item.enabled = updated.enabled;
    emit("status", `场景 ${item.name} 已${updated.enabled ? "启用" : "停用"}`);
    ElMessage.success(updated.enabled ? "已启用" : "已停用");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "操作失败", true);
  } finally {
    toggling.value = null;
  }
}

async function remove(item: SceneItem) {
  try {
    await ElMessageBox.confirm(
      `删除「${item.name}」只移除场景定义，不影响已创建的计划。`,
      "删除场景",
      { type: "warning", confirmButtonText: "确认删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/butler/scenes/${item.id}`, { method: "DELETE" });
    emit("status", `场景 ${item.name} 已删除`);
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
        <div class="eyebrow">个人管家 · HOME-01</div>
        <h2>家庭场景</h2>
        <p>感知事件（到家/离家）或手动触发时展开为待确认行动计划。场景只建计划不直接执行；运行与确认仍在聊天端完成。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="panel">
      <el-table v-loading="loading" :data="scenes" empty-text="还没有场景（在聊天里说“帮我建一个回家模式”即可创建）" style="width:100%">
        <el-table-column label="名称" min-width="140">
          <template #default="{ row }"><strong>{{ row.name }}</strong></template>
        </el-table-column>
        <el-table-column label="触发器" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ triggerLabels[row.trigger] ?? row.trigger }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="生效时段" width="140">
          <template #default="{ row }">
            <span v-if="row.window_start">{{ row.window_start }}–{{ row.window_end ?? "…" }}</span>
            <span v-else class="muted">全天</span>
          </template>
        </el-table-column>
        <el-table-column label="步骤" min-width="280">
          <template #default="{ row }">
            <div class="steps">
              <span v-for="(step, index) in row.steps" :key="index" class="step-chip">
                <small>{{ index + 1 }}</small>{{ step.action_id }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="toggling === row.id"
              @change="(value: string | number | boolean) => setEnabled(row as SceneItem, Boolean(value))"
            />
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">{{ fmt(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="danger" plain @click="remove(row as SceneItem)">删除</el-button>
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
.muted { color: var(--muted); font-size: 12px; }
.steps { display: flex; gap: 6px; flex-wrap: wrap; }
.step-chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border: 1px solid #dfe5f1; border-radius: 6px; background: #f8fafd; font-size: 11px; }
.step-chip small { color: var(--muted); font-size: 10px; }
</style>
