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

interface DraftItem {
  id: string;
  name: string;
  description: string | null;
  status: string;
  replay_status: string;
  replay_detail: {
    fixture_replay?: {
      status: string;
      validation_level: string;
      outcome: string;
      case_count: number;
      checks: Array<{ case_id: string; before: boolean | null; after: boolean }>;
    };
  };
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const workflows = ref<WorkflowItem[]>([]);
const loading = ref(false);
const drafts = ref<DraftItem[]>([]);
const draftError = ref<string | null>(null);
const busyDraft = ref<string | null>(null);
const evaluating = ref<DraftItem | null>(null);
const fixtureText = ref(JSON.stringify({ data_class: "synthetic", cases: [
  { id: "example", expected: [{ tool_name: "your_read_tool", arguments: {} }] },
] }, null, 2));

function canApprove(item: DraftItem) {
  return !["failed", "not_run"].includes(item.replay_status)
    && (!item.replay_detail.fixture_replay || item.replay_detail.fixture_replay.status === "passed");
}

async function review(item: DraftItem, action: "approve" | "dismiss" | "replay") {
  busyDraft.value = item.id;
  try {
    await api.request(`/api/v1/admin/butler/workflow-drafts/${item.id}/${action}`, { method: "POST" });
    ElMessage.success(action === "approve" ? "已保存为流程，运行时仍需按权限确认" : action === "dismiss" ? "已忽略候选" : "来源检查已更新");
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "操作失败", true);
  } finally {
    busyDraft.value = null;
  }
}

async function evaluate() {
  const item = evaluating.value;
  if (!item) return;
  busyDraft.value = item.id;
  try {
    const body = JSON.parse(fixtureText.value);
    await api.request(`/api/v1/admin/butler/workflow-drafts/${item.id}/evaluate`, {
      method: "POST", body: JSON.stringify(body),
    });
    evaluating.value = null;
    ElMessage.success("离线评测已保存");
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "评测失败", true);
  } finally {
    busyDraft.value = null;
  }
}

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    const [saved, candidates] = await Promise.allSettled([
      api.request<WorkflowItem[]>("/api/v1/admin/butler/workflows"),
      api.request<DraftItem[]>("/api/v1/admin/butler/workflow-drafts?status=pending"),
    ]);
    if (saved.status === "rejected") throw saved.reason;
    workflows.value = saved.value;
    if (candidates.status === "fulfilled") {
      drafts.value = candidates.value;
      draftError.value = null;
    } else {
      drafts.value = [];
      draftError.value = candidates.reason instanceof Error ? candidates.reason.message : "候选服务暂不可用";
    }
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
    <div class="panel">
      <h2>待审阅的流程候选</h2>
      <p class="muted">来源检查只说明只读步骤的结构是否一致。离线样例比较动作调用契约；两者都不证明实际业务结果正确。</p>
      <el-alert v-if="draftError" :title="draftError" type="warning" :closable="false" />
      <el-table v-else :data="drafts" empty-text="暂无待审阅候选">
        <el-table-column type="expand">
          <template #default="{ row }">
            <p v-for="check in row.replay_detail.fixture_replay?.checks ?? []" :key="check.case_id">
              {{ check.case_id }} · 来源 {{ check.before === null ? '未评估' : check.before ? '通过' : '失败' }} · 候选 {{ check.after ? '通过' : '失败' }}
            </p>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="候选" min-width="150" />
        <el-table-column label="来源检查" min-width="110">
          <template #default="{ row }">{{ ({ passed: "通过", failed: "失败", not_run: "未检查", not_applicable: "无可检查步骤" } as Record<string, string>)[row.replay_status] ?? row.replay_status }}</template>
        </el-table-column>
        <el-table-column label="离线对比" min-width="220">
          <template #default="{ row }">
            <template v-if="row.replay_detail.fixture_replay">
              {{ row.replay_detail.fixture_replay.status === 'passed' ? '通过' : '未通过' }} · {{ row.replay_detail.fixture_replay.validation_level }} · {{ row.replay_detail.fixture_replay.case_count }} 个样例
              <small class="muted">{{ ({ improved: "改善", regressed: "退化", unchanged: "无变化", inconclusive: "结论不足" } as Record<string, string>)[row.replay_detail.fixture_replay.outcome] }}</small>
            </template>
            <span v-else>未评测</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="340">
          <template #default="{ row }">
            <el-button size="small" :disabled="busyDraft !== null" @click="evaluating = row as DraftItem">离线评测</el-button>
            <el-button size="small" :disabled="busyDraft !== null" @click="review(row as DraftItem, 'replay')">检查只读来源</el-button>
            <el-button size="small" type="primary" :disabled="busyDraft !== null || !canApprove(row as DraftItem)" @click="review(row as DraftItem, 'approve')">批准保存</el-button>
            <el-button size="small" :disabled="busyDraft !== null" @click="review(row as DraftItem, 'dismiss')">忽略</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <el-dialog :model-value="evaluating !== null" title="流程离线评测" width="min(680px, 95vw)" @close="evaluating = null">
      <p>只使用自行构造的测试参数；不要粘贴私人对话。评测不会执行工具或访问外部连接。expected 列出完整轨迹的工具名与参数。</p>
      <el-input v-model="fixtureText" type="textarea" :rows="14" aria-label="自行构造的测试样例 JSON" />
      <template #footer><el-button type="primary" :loading="busyDraft !== null" @click="evaluate">运行离线评测</el-button></template>
    </el-dialog>
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
