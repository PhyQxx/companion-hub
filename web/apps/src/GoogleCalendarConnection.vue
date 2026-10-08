<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
import { ChatApi } from "@aria/shared";

const props = defineProps<{ token: string }>();
const api = new ChatApi();
const configured = ref(false);
const connected = ref(false);
const loaded = ref(false);
const busy = ref(false);
const message = ref("");
let stopped = false;
let popup: Window | null = null;

async function refresh() {
  const token = props.token;
  try {
    const status = await api.googleCalendarStatus(token);
    if (stopped || token !== props.token) return;
    configured.value = status.configured;
    connected.value = status.connected;
    loaded.value = true;
    message.value = status.configured ? "" : "请先在管理页面配置 Google 日历。";
  } catch { if (!stopped) message.value = "连接状态读取失败，请重试。"; }
}

async function connect() {
  if (busy.value) return;
  // Open from the user's click so mobile browsers can allow the new window.
  popup = window.open("about:blank", "_blank");
  if (!popup) { message.value = "请允许打开新窗口，再重试授权。"; return; }
  popup.opener = null;
  busy.value = true;
  const token = props.token;
  try {
    const result = await api.authorizeGoogleCalendar(token);
    if (stopped || token !== props.token) { popup?.close(); return; }
    const url = new URL(result.authorize_url);
    if (url.origin !== "https://accounts.google.com" || url.pathname !== "/o/oauth2/v2/auth") {
      throw new Error("授权地址无效。");
    }
    popup.location.href = url.href;
    popup = null;
    message.value = "请在新窗口完成授权，返回后刷新连接状态。";
  } catch {
    popup?.close(); popup = null;
    if (!stopped) message.value = "授权未开始，请检查日历配置和费用上限后重试。";
  } finally { if (!stopped) busy.value = false; }
}

async function disconnect() {
  if (busy.value) return;
  busy.value = true;
  const token = props.token;
  try {
    await api.disconnectGoogleCalendar(token);
    if (stopped || token !== props.token) return;
    connected.value = false;
    message.value = "已断开连接，尚未完成的授权也已失效。";
  } catch { if (!stopped) message.value = "断开连接失败，请刷新核对状态。"; }
  finally { if (!stopped) busy.value = false; }
}

function toggle(event: Event) {
  if ((event.target as HTMLDetailsElement).open) void refresh();
}
onBeforeUnmount(() => { stopped = true; popup?.close(); popup = null; });
</script>

<template>
  <details id="google-calendar" class="calendar-connection" @toggle="toggle">
    <summary>Google 日历</summary>
    <p v-if="loaded">{{ connected ? '已连接，只读同步' : '尚未连接' }}</p>
    <div class="calendar-actions">
      <button type="button" :disabled="busy || !configured" @click="connect">{{ connected ? '重新授权' : '连接 Google 日历' }}</button>
      <button type="button" :disabled="busy" @click="refresh">刷新状态</button>
      <button type="button" :disabled="busy" @click="disconnect">断开并撤销待完成授权</button>
    </div>
    <p role="status">{{ message }}</p>
  </details>
</template>

<style scoped>
.calendar-connection { font-size: 12px; }
.calendar-connection summary { cursor: pointer; }
.calendar-connection p { line-height: 1.5; }
.calendar-actions { display: grid; gap: 6px; margin-top: 10px; }
.calendar-actions button { white-space: normal; }
</style>
