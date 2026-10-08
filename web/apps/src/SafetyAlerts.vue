<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ChatApi, ApiError, type SafetyAlertItem } from "@aria/shared";

const props = defineProps<{ token: string }>();
const api = new ChatApi();
const alerts = ref<SafetyAlertItem[]>([]);
const busy = ref(new Set<string>());
const error = ref("");
let stopped = false;
let timer: ReturnType<typeof setTimeout> | undefined;

const levelLabels: Record<number, string> = { 1: "广播提醒", 2: "推送重提醒", 3: "已联系紧急联系人" };

async function load() {
  try {
    const items = await api.listSafetyAlerts(props.token);
    if (!stopped) alerts.value = items;
  } catch (err) {
    // 安全面板是增强路径：拉取失败静默，不打断聊天。
    // 服务端未启用安全服务时路由是 404，停止轮询避免刷日志。
    if (err instanceof ApiError && err.status === 404) stopped = true;
  }
}

async function poll() {
  await load();
  if (!stopped) timer = setTimeout(poll, 5000);
}

async function ack(alert: SafetyAlertItem) {
  if (busy.value.has(alert.id)) return;
  busy.value.add(alert.id);
  error.value = "";
  try {
    await api.ackSafetyAlert(props.token, alert.id);
    alerts.value = alerts.value.filter(item => item.id !== alert.id);
  } catch {
    error.value = "确认未生效，请重试；也可以直接回复「知道了」。";
  } finally {
    busy.value.delete(alert.id);
  }
}

function timeLabel(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

onMounted(poll);
onBeforeUnmount(() => {
  stopped = true;
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <div v-if="alerts.length" class="safety-alerts">
    <div v-for="alert in alerts" :key="alert.id" class="safety-card" role="alert">
      <div class="safety-head">
        <span class="safety-badge">危急告警</span>
        <span class="safety-level">{{ levelLabels[alert.level] ?? `L${alert.level}` }}</span>
        <span class="safety-time">{{ timeLabel(alert.created_at) }}</span>
      </div>
      <p class="safety-message">{{ alert.message }}</p>
      <div class="safety-actions">
        <button
          class="ack-btn"
          :disabled="busy.has(alert.id)"
          @click="ack(alert)"
        >{{ busy.has(alert.id) ? "确认中…" : "我已处理" }}</button>
        <small class="safety-hint">或直接回复「知道了」</small>
      </div>
    </div>
    <p v-if="error" class="safety-error">{{ error }}</p>
  </div>
</template>

<style scoped>
.safety-alerts { display: grid; gap: 10px; padding: 0 16px; }
.safety-card {
  display: grid; gap: 8px; padding: 12px 14px;
  border: 1px solid #f3c1c1; border-left: 4px solid #d9534f;
  border-radius: 10px; background: #fdf3f3;
}
.safety-head { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.safety-badge {
  padding: 1px 8px; border-radius: 999px; background: #d9534f; color: #fff;
  font-weight: 700; letter-spacing: .05em;
}
.safety-level { color: #a12622; font-weight: 600; }
.safety-time { margin-left: auto; color: #8c6f6f; }
.safety-message { margin: 0; font-size: 13px; line-height: 1.6; color: #4a3232; }
.safety-actions { display: flex; align-items: center; gap: 10px; }
.ack-btn {
  padding: 5px 16px; border: none; border-radius: 8px; cursor: pointer;
  background: #d9534f; color: #fff; font-size: 13px; font-weight: 600;
}
.ack-btn:disabled { opacity: .6; cursor: default; }
.safety-hint { color: #8c6f6f; font-size: 11px; }
.safety-error { margin: 0; color: #d9534f; font-size: 12px; }
</style>
