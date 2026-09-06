<script setup lang="ts">
import { computed } from "vue";
import type { ActionPlanProgress } from "@aria/shared";

const props = defineProps<{
  plan: ActionPlanProgress;
  stopping: boolean;
}>();

const emit = defineEmits<{ stop: []; dismiss: [] }>();

const STATUS_LABELS: Record<string, string> = {
  awaiting_confirmation: "待确认",
  ready: "就绪",
  executing: "执行中",
  completed: "已完成",
  partially_completed: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
  // 步骤状态
  skipped: "已跳过",
  unknown_outcome: "结果未知",
};

const RISK_LABELS: Record<string, string> = { A0: "只读", A1: "低风险", A2: "需确认", A3: "禁止" };

const VERIFICATION_LABELS: Record<string, string> = {
  pending: "验证中",
  verified: "已验证",
  not_required: "无需验证",
  inconclusive: "证据不足",
};

function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key;
}

const finished = computed(() => props.plan.status !== "executing");
const completedCount = computed(
  () => props.plan.steps.filter((step) => step.status === "completed").length,
);
const progressPercent = computed(() =>
  props.plan.steps.length === 0
    ? 0
    : Math.round((completedCount.value / props.plan.steps.length) * 100),
);
</script>

<template>
  <section class="plan-card" :class="{ finished }" role="status">
    <header class="plan-head">
      <div class="plan-title">
        <strong>⚙️ {{ plan.title ?? "动作计划" }}</strong>
        <span class="plan-status" :data-status="plan.status">{{ label(STATUS_LABELS, plan.status) }}</span>
        <span v-if="plan.cancel_requested && !finished" class="plan-stopping">正在停止…</span>
      </div>
      <button
        v-if="plan.status === 'executing' && !plan.cancel_requested"
        class="plan-stop"
        :disabled="stopping"
        @click="emit('stop')"
      >
        {{ stopping ? "停止中…" : "■ 停止" }}
      </button>
      <button v-else class="plan-dismiss" aria-label="关闭" @click="emit('dismiss')">✕</button>
    </header>

    <div class="plan-progress">
      <div class="plan-bar"><i :style="{ width: `${progressPercent}%` }" /></div>
      <small>{{ completedCount }}/{{ plan.steps.length }} 步完成</small>
    </div>

    <ol class="plan-steps">
      <li v-for="step in plan.steps" :key="step.position" :data-status="step.status">
        <span class="step-pos">{{ step.position }}</span>
        <span class="step-name">{{ step.action_id }}</span>
        <span class="step-risk">{{ label(RISK_LABELS, step.risk) }}</span>
        <span class="step-verification">{{ label(VERIFICATION_LABELS, step.verification_status) }}</span>
        <span class="step-status">{{ label(STATUS_LABELS, step.status) }}</span>
      </li>
    </ol>

    <small v-if="plan.reason_code" class="plan-reason">原因：{{ plan.reason_code }}</small>
  </section>
</template>

<style scoped>
.plan-card {
  margin: 0 16px 8px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
  display: grid;
  gap: 10px;
}
.plan-head { display: flex; align-items: center; gap: 10px; }
.plan-title { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
.plan-title strong { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.plan-status { font-size: 11px; padding: 1px 8px; border-radius: 999px; background: #eef2ff; color: #4453c9; }
.plan-status[data-status="completed"] { background: #e8f5e9; color: #2e7d32; }
.plan-status[data-status="failed"], .plan-status[data-status="cancelled"] { background: #fdecea; color: #c0392b; }
.plan-status[data-status="executing"] { background: #fff7e0; color: #b7791f; }
.plan-stopping { font-size: 11px; color: #b7791f; }
.plan-stop, .plan-dismiss {
  border: 1px solid var(--line); background: #fff; border-radius: 8px;
  padding: 4px 12px; font-size: 12px; cursor: pointer;
}
.plan-stop { color: #c0392b; border-color: #f0c4bd; font-weight: 600; }
.plan-stop:disabled { opacity: 0.6; cursor: default; }
.plan-progress { display: flex; align-items: center; gap: 10px; }
.plan-bar { flex: 1; height: 6px; border-radius: 999px; background: #edf0f7; overflow: hidden; }
.plan-bar i { display: block; height: 100%; background: #4453c9; transition: width 0.3s ease; }
.plan-progress small { color: var(--muted); font-size: 11px; }
.plan-steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.plan-steps li {
  display: grid; grid-template-columns: 24px minmax(0, 1fr) auto auto auto;
  gap: 8px; align-items: center; font-size: 12px; padding: 4px 6px; border-radius: 6px;
}
.plan-steps li[data-status="executing"] { background: #fff7e0; }
.plan-steps li[data-status="completed"] .step-status { color: #2e7d32; }
.plan-steps li[data-status="failed"], .plan-steps li[data-status="cancelled"] { color: #c0392b; }
.step-pos { color: var(--muted); }
.step-name { font-family: ui-monospace, monospace; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-risk, .step-verification, .step-status { color: var(--muted); font-size: 11px; }
.plan-reason { color: var(--muted); font-size: 11px; }
</style>
