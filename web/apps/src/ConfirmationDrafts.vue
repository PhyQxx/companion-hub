<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ChatApi, type ConfirmationDraft } from "@aria/shared";

const props = defineProps<{ token: string }>();
const api = new ChatApi();
const drafts = ref<ConfirmationDraft[]>([]);
const busy = ref(new Set<string>());
const error = ref("");
const dismissed = new Set<string>();
let stopped = false;
let timer: ReturnType<typeof setTimeout> | undefined;

function endpoint(draft: ConfirmationDraft): "calendar" | "workflows" {
  return draft.kind === "calendar_create" ? "calendar" : "workflows";
}
async function load() {
  try {
    const [calendar, workflows] = await Promise.all([
      api.listConfirmationDrafts(props.token, "calendar"),
      api.listConfirmationDrafts(props.token, "workflows"),
    ]);
    if (!stopped) {
      drafts.value = [...calendar, ...workflows].filter(
        draft => draft.status !== "cancelled" && !dismissed.has(draft.id),
      );
    }
  } catch {
    if (!stopped && drafts.value.length) error.value = "待确认内容暂时无法同步，请稍后刷新。";
  }
}
async function poll() {
  await load();
  if (!stopped) timer = setTimeout(poll, 5000);
}
async function act(draft: ConfirmationDraft, action: "confirm" | "cancel") {
  if (busy.value.has(draft.id)) return;
  busy.value.add(draft.id); error.value = "";
  try {
    await api.confirmationDraftAction(
      props.token, endpoint(draft), draft.id, action, draft.digest,
    );
  } catch {
    error.value = action === "confirm"
      ? "保存结果未确认，请先刷新日历或流程列表；结果未知时不要重复保存。"
      : "取消未完成，请刷新后核对状态。";
  } finally {
    busy.value.delete(draft.id);
    await load();
  }
}
function dismiss(id: string) {
  dismissed.add(id);
  drafts.value = drafts.value.filter(draft => draft.id !== id);
}
function items(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value as Array<Record<string, unknown>> : [];
}
onMounted(poll);
onBeforeUnmount(() => { stopped = true; clearTimeout(timer); });
</script>

<template>
  <section v-if="drafts.length || error" class="confirmation-drafts" aria-label="保存确认">
    <p v-if="error" role="alert">{{ error }}</p>
    <article v-for="draft in drafts" :key="draft.id">
      <header>
        <strong>{{ draft.kind === "calendar_create" ? "📅 待确认日程" : "🔁 待确认流程" }}</strong>
        <span>{{ draft.status === "pending" ? "待确认" : draft.status === "completed" ? "已保存" : "处理中" }}</span>
      </header>
      <template v-if="draft.kind === 'calendar_create'">
        <p>标题：{{ draft.preview.title }}</p>
        <p>时间：{{ new Date(String(draft.preview.starts_at)).toLocaleString() }} — {{ new Date(String(draft.preview.ends_at)).toLocaleString() }}</p>
        <p v-if="draft.preview.location">地点：{{ draft.preview.location }}</p>
        <p>参与人：{{ items(draft.preview.participants).length ? items(draft.preview.participants).join("，") : "无" }}</p>
        <p>提醒：提前 {{ draft.preview.reminder_lead_minutes }} 分钟；日历：{{ draft.preview.calendar_id }}</p>
      </template>
      <template v-else>
        <p>名称：{{ draft.preview.name }}</p>
        <p v-if="draft.preview.description">说明：{{ draft.preview.description }}</p>
        <ol>
          <li v-for="(step, index) in items(draft.preview.steps)" :key="index">
            <strong>{{ index + 1 }}. {{ step.label }}</strong>
            <span>风险 {{ step.risk }} · {{ step.confirmation_policy }} · {{ step.reversible ? "可撤销" : "不可撤销" }}</span>
            <pre>{{ JSON.stringify(step.arguments, null, 2) }}</pre>
          </li>
        </ol>
      </template>
      <template v-if="draft.status === 'pending'">
        <small>请核对完整内容。有效期至 {{ new Date(draft.expires_at).toLocaleTimeString() }}。</small>
        <div class="actions">
          <button :disabled="busy.has(draft.id)" @click="act(draft, 'confirm')">
            {{ draft.kind === "calendar_create" ? "确认创建" : "确认保存" }}
          </button>
          <button :disabled="busy.has(draft.id)" @click="act(draft, 'cancel')">取消</button>
        </div>
      </template>
      <button v-else-if="draft.status !== 'saving'" @click="dismiss(draft.id)">收起</button>
    </article>
  </section>
</template>

<style scoped>
.confirmation-drafts { padding: 0 12px 8px; max-height: 48vh; overflow: auto; }
article { border: 1px solid var(--line); border-radius: 12px; padding: 14px; margin: 8px 0; background: var(--panel); }
header { display: flex; gap: 8px; justify-content: space-between; flex-wrap: wrap; }
p { margin: 7px 0; overflow-wrap: anywhere; font-size: 13px; }
ol { padding-left: 22px; }
li { margin: 8px 0; }
li span { display: block; color: var(--muted); font-size: 12px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 4px 0; font-size: 12px; }
.actions { display: flex; gap: 12px; margin-top: 10px; }
button { min-height: 44px; padding: 8px 16px; cursor: pointer; }
</style>
