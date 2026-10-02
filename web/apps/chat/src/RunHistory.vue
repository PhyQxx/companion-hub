<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { ChatApi, type TaskRun, type TaskRunEvent } from "@aria/shared";

const props = defineProps<{ token: string; refreshKey: number }>();
const api = new ChatApi();
const opened = ref(false);
const runs = ref<TaskRun[]>([]);
const selected = ref<TaskRun | null>(null);
const events = ref<TaskRunEvent[]>([]);
const error = ref("");
const loading = ref(false);
const cancelling = ref(false);
const canLoadMore = ref(false);
let stopped = false;
let selectionVersion = 0;
const stateLabels: Record<string, string> = {
  accepted: "等待处理", running: "正在处理", succeeded: "处理完成", failed: "处理失败", cancelled: "已停止",
};
const validationLabels: Record<string, string> = {
  V0: "尚未核实", V1: "已受理", V2: "结构通过", V3: "目标已核实", V4: "用户已验收",
};
const goalLabels: Record<string, string> = {
  passed: "已登记项目通过", pending: "关联工作仍在进行", failed: "有项目未完成",
  inconclusive: "证据不足，待核对", not_declared: "未声明完成判据",
};
const criterionLabels: Record<string, string> = {
  reply_committed: "回复保存", action_plan_verified: "操作核实", delegated_result: "委派结果", model_result_returned: "模型结果",
};
const active = (run: TaskRun) => ["accepted", "running"].includes(run.status) || (run.goal?.pending ?? 0) > 0;
const time = (value: string) => new Date(value).toLocaleString();
async function refresh(more = false) {
  if (loading.value || stopped) return;
  loading.value = true;
  error.value = "";
  try {
    const last = runs.value[runs.value.length - 1];
    const page = await api.listRuns(props.token, more ? last?.id : undefined);
    if (stopped) return;
    runs.value = more ? [...runs.value, ...page] : page;
    canLoadMore.value = page.length === 50;
  } catch { if (!stopped) error.value = "运行记录读取失败，请重试。"; }
  finally { loading.value = false; }
}
async function inspect(run: TaskRun) {
  const version = ++selectionVersion;
  selected.value = run;
  events.value = [];
  error.value = "";
  try {
    const detail = await api.getRun(props.token, run.id);
    const history: TaskRunEvent[] = [];
    let after = 0;
    while (!stopped && version === selectionVersion) {
      const page = await api.runEvents(props.token, run.id, after);
      history.push(...page);
      if (page.length < 200) break;
      after = page[page.length - 1]!.seq;
    }
    if (!stopped && version === selectionVersion) {
      selected.value = detail;
      events.value = history;
    }
  } catch {
    if (!stopped && version === selectionVersion) {
      selected.value = null;
      error.value = "这条运行记录已不可用，请刷新列表。";
    }
  }
}
async function cancel() {
  if (!selected.value || cancelling.value) return;
  const run = selected.value;
  cancelling.value = true;
  try {
    await api.cancelRun(props.token, run.id);
    await inspect(run);
    await refresh();
  } catch { error.value = "停止请求未完成，请刷新核对当前状态。"; }
  finally { cancelling.value = false; }
}
function toggle(event: Event) {
  opened.value = (event.target as HTMLDetailsElement).open;
  if (opened.value) void refresh();
}
watch(() => props.refreshKey, () => {
  if (opened.value) void refresh();
});
onBeforeUnmount(() => { stopped = true; selectionVersion++; });
</script>

<template>
  <details class="run-history" @toggle="toggle">
    <summary>运行记录</summary>
    <div class="run-content">
      <p>查看处理进度与核实结果。</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <button :disabled="loading" @click="refresh()">{{ loading ? "正在读取…" : "刷新" }}</button>
      <p v-if="!loading && !runs.length">还没有运行记录。</p>
      <ul class="run-list">
        <li v-for="run in runs" :key="run.id">
          <button :aria-pressed="selected?.id === run.id" @click="inspect(run)">
            {{ time(run.created_at) }} · {{ stateLabels[run.status] ?? run.status }}
            · {{ run.conversation_id ? "对话" : "后台任务" }}
          </button>
        </li>
      </ul>
      <button v-if="canLoadMore" :disabled="loading" @click="refresh(true)">更早的记录</button>
      <section v-if="selected" class="run-detail" aria-label="运行详情">
        <h3>{{ stateLabels[selected.status] ?? selected.status }}</h3>
        <p>更新时间：{{ time(selected.updated_at) }}</p>
        <p v-if="selected.status === 'succeeded' && selected.goal?.criteria.some(item => item.kind === 'reply_committed')">回复已保存；操作是否完成以各项核实结果为准。</p>
        <p v-if="selected.budget_summary && !selected.budget_summary.enabled">此运行已明确停用额度限制；没有记录可核对的调用计数。</p>
        <p v-if="selected.budget_summary?.enabled">
          模型调用 {{ selected.budget_summary.llm_attempts }} / {{ selected.budget_summary.max_llm_attempts }} 次；
          用量计入 {{ selected.budget_summary.charged_tokens }} / {{ selected.budget_summary.max_tokens }} token。
          <span v-if="selected.budget_summary.unknown_usage_calls">有 {{ selected.budget_summary.unknown_usage_calls }} 次调用用量待核对。</span>
          <span v-if="selected.budget_summary.unsettled_calls">有 {{ selected.budget_summary.unsettled_calls }} 次调用尚未结算。</span>
        </p>
        <p v-if="selected.budget_summary?.enabled">
          工具调用 {{ selected.budget_summary.tool_attempts }} / {{ selected.budget_summary.max_tool_attempts }} 次。
          <span v-if="selected.budget_summary.unknown_tool_calls">有 {{ selected.budget_summary.unknown_tool_calls }} 次结果未知。</span>
          <span v-if="selected.budget_summary.unsettled_tool_calls">有 {{ selected.budget_summary.unsettled_tool_calls }} 次尚未返回；计数会继续保留。</span>
        </p>
        <section v-if="selected.goal" aria-label="任务完成判据">
          <h3>{{ goalLabels[selected.goal.status] ?? "待核对" }}</h3>
          <p>仅汇总已登记的判据；回复保存和任务返回不代表业务目标已核实。</p>
          <p v-if="selected.goal.scope === 'legacy_associations'">旧记录按现有任务关联汇总。</p>
          <ul>
            <li v-for="item in selected.goal.criteria" :key="`${item.kind}:${item.source_id}`">
              {{ criterionLabels[item.kind] ?? "其他判据" }} · {{ goalLabels[item.status] ?? "待核对" }}
              <span v-if="item.status === 'passed'"> · {{ validationLabels[item.validation_level] ?? "尚未核实" }}</span>
            </li>
          </ul>
        </section>
        <p>关联 {{ selected.plan_ids.length }} 个操作计划、{{ selected.job_ids.length }} 个后台任务。</p>
        <ul>
          <li v-for="item in selected.action_outcomes" :key="item.step_id">
            {{ validationLabels[item.outcome.validation_level] ?? "尚未核实" }}
            <span v-if="item.outcome.execution_status === 'unknown_outcome'"> · 执行结果未知，请核对后处理</span>
            <span v-if="item.outcome.validation_status === 'inconclusive'"> · 证据不充分</span>
            <span v-if="item.outcome.reason_code"> · {{ item.outcome.reason_code }}</span>
          </li>
        </ul>
        <button v-if="active(selected)" :disabled="cancelling" @click="cancel">{{ cancelling ? "正在停止…" : "停止处理" }}</button>
        <button @click="inspect(selected)">更新详情</button>
        <ol aria-label="处理记录">
          <li v-for="event in events" :key="event.event_id">
            {{ time(event.occurred_at) }} · {{ stateLabels[event.kind.replace('run.', '')] ?? '状态已更新' }}
          </li>
        </ol>
      </section>
    </div>
  </details>
</template>

<style scoped>
.run-history { flex-shrink: 0; border-top: 1px solid var(--border-color, #8884); }
summary { cursor: pointer; padding: 8px 16px; }
.run-content { padding: 0 16px 12px; max-height: 45vh; overflow: auto; }
.run-content p { font-size: 13px; }
.run-list { list-style: none; padding: 0; }
.run-list li { margin: 5px 0; }
button { cursor: pointer; border: 1px solid #8885; border-radius: 6px; padding: 5px 9px; background: transparent; color: inherit; }
button:disabled { opacity: .6; cursor: default; }
button:focus-visible, summary:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
.run-detail { border-top: 1px solid #8884; margin-top: 12px; padding-top: 8px; }
h3 { font-size: 15px; margin: 5px 0; }
</style>
