<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ChatApi, type ActionPlanProgress } from "@aria/shared";
import PlanProgressCard from "./PlanProgressCard.vue";
const props = defineProps<{ token: string; refreshKey: number }>();
const api = new ChatApi();
const plans = ref<ActionPlanProgress[]>([]);
const starting = ref(new Set<string>());
const stopping = ref(new Set<string>());
const dismissed = new Set<string>();
const error = ref("");
let stopped = false;
let loading = false;
let queued = false;
let timer: ReturnType<typeof setTimeout> | undefined;
const active = (p: ActionPlanProgress) => ["awaiting_confirmation", "ready", "executing"].includes(p.status);
function merge(incoming: ActionPlanProgress[]) {
  if (stopped) return;
  const byId = new Map(plans.value.map(p => [p.id, p]));
  for (const p of incoming) {
    const old = byId.get(p.id);
    if (!old || Date.parse(p.updated_at) >= Date.parse(old.updated_at)) byId.set(p.id, p);
  }
  plans.value = [...byId.values()].filter(p => !dismissed.has(p.id)).sort((a, b) =>
    Number(active(b)) - Number(active(a)) || b.updated_at.localeCompare(a.updated_at));
}
async function refresh() {
  if (stopped) return;
  if (loading) { queued = true; return; }
  loading = true;
  try {
    const pending: ActionPlanProgress[] = [];
    let before: string | undefined;
    while (!stopped) {
      const page = await api.listActionPlans(props.token, true, before);
      pending.push(...page);
      if (page.length < 100) break;
      before = page[page.length - 1]!.id;
    }
    const recent = await api.listActionPlans(props.token);
    // Previously visible pending plans may have finished beyond the latest page.
    const ids = new Set([...pending, ...recent].map(p => p.id));
    const missing = await Promise.all(plans.value.filter(p => active(p) && !ids.has(p.id)).map(async p => {
      try { return await api.getActionPlan(props.token, p.id); } catch { return null; }
    }));
    merge([...pending, ...recent.filter(p => Date.parse(p.updated_at) > Date.now() - 86400000),
      ...missing.filter((p): p is ActionPlanProgress => p !== null)]);
  } catch {
    if (!stopped) error.value = "计划列表同步失败，请刷新后重试。";
  } finally {
    loading = false;
    if (queued && !stopped) { queued = false; void refresh(); }
  }
}
async function confirm(plan: ActionPlanProgress) {
  if (starting.value.has(plan.id)) return;
  starting.value.add(plan.id); error.value = "";
  try {
    if (plan.status === "awaiting_confirmation") merge([await api.confirmActionPlan(props.token, plan.id)]);
    merge([await api.executeActionPlan(props.token, plan.id)]);
  } catch { error.value = "执行请求未完成，请核对计划状态；结果未知时不要重建计划重试。"; }
  finally { starting.value.delete(plan.id); await refresh(); }
}
async function cancel(plan: ActionPlanProgress) {
  if (stopping.value.has(plan.id)) return;
  stopping.value.add(plan.id); error.value = "";
  try { merge([await api.cancelActionPlan(props.token, plan.id)]); }
  catch { error.value = "取消请求未完成，请核对最新状态。"; }
  finally { stopping.value.delete(plan.id); await refresh(); }
}
function dismiss(plan: ActionPlanProgress) {
  if (active(plan)) return;
  dismissed.add(plan.id); plans.value = plans.value.filter(p => p.id !== plan.id);
}
async function poll() { await refresh(); if (!stopped) timer = setTimeout(poll, 10000); }
const onResume = () => { if (document.visibilityState === "visible") void refresh(); };
watch(() => props.refreshKey, () => void refresh());
onMounted(() => {
  void poll(); window.addEventListener("online", onResume);
  document.addEventListener("visibilitychange", onResume);
});
onBeforeUnmount(() => {
  stopped = true; clearTimeout(timer); window.removeEventListener("online", onResume);
  document.removeEventListener("visibilitychange", onResume);
});
</script>
<template>
  <section v-if="plans.length || error" class="plan-inbox" aria-label="待确认与执行计划">
    <p v-if="error" role="alert">{{ error }} <button @click="error = ''; refresh()">刷新</button></p>
    <PlanProgressCard v-for="plan in plans" :key="plan.id" :plan="plan"
      :starting="starting.has(plan.id)" :stopping="stopping.has(plan.id)"
      @confirm="confirm(plan)" @stop="cancel(plan)" @dismiss="dismiss(plan)" />
  </section>
</template>
<style scoped>
.plan-inbox { max-height: 50vh; overflow: auto; padding: 8px 0; flex-shrink: 0; }
.plan-inbox > p { margin: 8px 16px; }
</style>
