<script setup lang="ts">
import { computed, provide, ref } from "vue";
import { useRoute } from "vue-router";
import { AdminApi } from "@aria/shared";

// 管理后台外壳：令牌鉴权（sessionStorage）+ 侧栏导航 + 路由视图。
// 子视图通过 inject("adminApi") 拿到已鉴权的客户端实例。
const TOKEN_KEY = "ariaAdminToken";
const api = new AdminApi();
api.token = sessionStorage.getItem(TOKEN_KEY) ?? "";
const connected = ref(false);
const statusText = ref("");
const statusError = ref(false);
const tokenInput = ref("");

provide("adminApi", api);

const route = useRoute();
const moduleLabels: Record<string, string> = {
  devices: "设备",
  logs: "日志追踪",
  privacy: "隐私审计",
  settings: "系统配置",
};

const navItems = [
  { to: "/", label: "总览" },
  { to: "/models", label: "模型与路由" },
  { to: "/personas", label: "人格" },
  { to: "/memory", label: "记忆质量" },
  { to: "/timeline", label: "历史时间线" },
  { to: "/devices", label: "设备" },
  { to: "/logs", label: "日志追踪" },
  { to: "/privacy", label: "隐私审计" },
  { to: "/settings", label: "系统配置" },
];

const heading = computed(() => {
  const path = route.path.replace(/\/$/, "") || "/";
  if (path === "/") return "总览";
  if (path === "/models") return "模型与路由";
  if (path === "/personas") return "角色与表达";
  if (path === "/memory") return "记忆库";
  if (path === "/timeline") return "历史时间线";
  if (path === "/devices") return "设备与能力";
  return moduleLabels[String(route.params.module ?? "")] ?? "管理后台";
});

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}

/** 用管理令牌换取一次探测请求，验证通过后进入工作区 */
async function connect(token: string) {
  api.token = token;
  try {
    await api.request("/api/v1/admin/config/current");
    sessionStorage.setItem(TOKEN_KEY, token);
    connected.value = true;
    setStatus("已连接");
  } catch (error) {
    connected.value = false;
    setStatus(error instanceof Error ? error.message : "连接失败", true);
  }
}

if (api.token) void connect(api.token);
</script>

<template>
  <div v-if="!connected" class="auth">
    <el-card class="card" shadow="never">
      <h1>Aria 管理后台</h1>
      <p class="hint">输入 ARIA_ADMIN_TOKEN 连接管理 API，仅保存在当前浏览器会话中。</p>
      <el-input v-model="tokenInput" type="password" show-password placeholder="ARIA_ADMIN_TOKEN" autocomplete="current-password" @keyup.enter="connect(tokenInput)" />
      <el-button type="primary" @click="connect(tokenInput)">连接</el-button>
      <p v-if="statusText" class="status" :class="{ error: statusError }">{{ statusText }}</p>
    </el-card>
  </div>

  <div v-else class="shell admin-shell">
    <aside>
      <div class="brand"><span class="brand-mark">A</span><span>Aria Hub</span></div>
      <el-menu router :default-active="route.path" class="side-menu">
        <el-menu-item v-for="item in navItems" :key="item.to" :index="item.to">{{ item.label }}</el-menu-item>
      </el-menu>
      <div class="privacy-note"><span class="dot"></span>管理 API 已连接</div>
    </aside>
    <main>
      <header v-if="route.path.replace(/\/$/, '') !== '/models'" class="topbar">
        <h1>{{ heading }}</h1>
        <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
      </header>
      <router-view @status="setStatus" />
    </main>
  </div>
</template>

<style scoped>
.auth { display: grid; place-items: center; height: 100%; }
.card { width: min(380px, 90vw); border-radius: 16px; }
.card :deep(.el-card__body) { display:grid; gap:12px; padding:28px; }
.card h1 { margin: 0; font-size: 20px; }
.hint, .status { color: var(--muted); font-size: 13px; margin: 0; }
.status.error { color: var(--danger); }

.shell { display: grid; grid-template-columns: 230px 1fr; height: 100%; }
aside { display: flex; flex-direction: column; gap: 18px; border-right: 1px solid var(--line); padding: 16px; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.brand-mark { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 9px; background: var(--accent); color: #fff; }
.side-menu { border-right: 0; background: transparent; }
.side-menu :deep(.el-menu-item) { height: 38px; line-height: 38px; border-radius: 8px; margin: 2px 0; padding: 0 12px !important; color: #66738a; font-size: 14px; }
.side-menu :deep(.el-menu-item:hover) { background: #f7f9fd; color: #172033; }
.side-menu :deep(.el-menu-item.is-active) { color: #3658e8; background: #eef2ff; font-weight: 600; }
.privacy-note { margin-top: auto; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #7ee2a8; }
main { display: flex; flex-direction: column; min-height: 0; }
.topbar { display: flex; justify-content: space-between; align-items: center; padding: 18px 24px; border-bottom: 1px solid var(--line); }
.topbar h1 { margin: 0; font-size: 18px; }

.admin-shell { background: #f6f8fc; color: #172033; }
.admin-shell aside { background: #fff; border-right-color: #e3e8f2; }
.admin-shell .brand { color: #172033; }
.admin-shell .privacy-note { color: #66738a; }
.admin-shell main { background: #f6f8fc; color: #172033; }
.admin-shell .topbar { background: #fff; border-bottom-color: #e3e8f2; }
.admin-shell .topbar h1 { color: #172033; }
.admin-shell .topbar .status { color: #68748a; }

@media (max-width: 720px) {
  .shell { grid-template-columns: 1fr; }
  aside { flex-direction: row; align-items: center; overflow-x: auto; }
  .side-menu { display: flex; min-width: max-content; }
}
</style>
