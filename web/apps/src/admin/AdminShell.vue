<script setup lang="ts">
import { computed, provide, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { AdminApi, ChatApi } from "@aria/shared";
import {
  adminModules,
  defaultModulePath,
  findModuleForRole,
  groupsForRole,
  isOwner,
  tabsFor,
} from "./admin-navigation";

// 管理后台外壳（并入主应用）：SSO 会话鉴权 + 侧栏导航 + 路由视图。
// 子视图通过 inject("adminApi") 拿到已鉴权的客户端实例。
// 会话令牌来自 pnkx SSO 登录（回调页写入 localStorage.ariaChatToken）。
// 多用户：/auth/me 返回 role——业主见全局配置；成员只见个人数据模块
// （后端 Admin 分级鉴权为准，导航仅收敛入口与拦截越权深链）。
const api = new AdminApi();
api.token = localStorage.getItem("ariaChatToken") ?? "";
const connected = ref(false);
const statusText = ref("");
const statusError = ref(false);
const role = ref<string>("");
const displayName = ref<string>("");

provide("adminApi", api);
provide("adminRole", role);

const route = useRoute();
const router = useRouter();
const normalizedPath = computed(() => {
  const path = route.path.replace(/\/$/, "");
  return path === "/admin/timeline" ? "/admin/memory" : path;
});
const activeModule = computed(() => findModuleForRole(normalizedPath.value, role.value));
// 成员打开仅业主模块的深链：显示无权限提示而不是静默跳转
const deniedModule = computed(() => {
  if (!connected.value || isOwner(role.value)) return null;
  return adminModules.find((item) => item.path === normalizedPath.value && item.requiresOwner) ?? null;
});
const heading = computed(() => activeModule.value?.label ?? "管理后台");
const visibleTabs = computed(() => (activeModule.value ? tabsFor(activeModule.value, role.value) : []));
const activeTab = computed(() => String(route.query.tab ?? visibleTabs.value[0]?.key ?? ""));

function ensureActiveTab() {
  // 成员落在 /admin（overview 仅业主）：跳到首个可见模块
  if (connected.value && !isOwner(role.value) && normalizedPath.value === "/admin") {
    void router.replace({ path: defaultModulePath(role.value), query: route.query });
    return;
  }
  if (!activeModule.value || deniedModule.value) return;
  const valid = visibleTabs.value.some((tab) => tab.key === route.query.tab);
  if (!valid) {
    const first = visibleTabs.value[0];
    if (first) void router.replace({ path: activeModule.value.path, query: { ...route.query, tab: first.key } });
  }
}

watch(() => [normalizedPath.value, route.query.tab, role.value], ensureActiveTab, { flush: "post" });
void router.isReady().then(ensureActiveTab);

function openModule(path: string, tab: string) {
  void router.push({ path, query: { tab } });
}

function openTab(tab: string) {
  if (!activeModule.value) return;
  void router.replace({ path: activeModule.value.path, query: { ...route.query, tab } });
}

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}

interface MeResponse {
  user: { display_name: string; role: string };
}

async function connect() {
  try {
    const me = await api.request<MeResponse>("/api/v1/auth/me");
    role.value = me.user.role;
    displayName.value = me.user.display_name;
    connected.value = true;
    setStatus(
      isOwner(role.value)
        ? `已连接 · ${displayName.value}（业主）`
        : `已连接 · ${displayName.value}（成员）`,
    );
  } catch (error) {
    connected.value = false;
    setStatus(error instanceof Error ? error.message : "连接失败", true);
  }
}

// 会话缺失/失效：账号密码登录，或跳 pnkx SSO 重新建立（回调落回本应用根）
const chatApi = new ChatApi();
const loginUsername = ref("");
const loginPassword = ref("");
const loginBusy = ref(false);
const ssoEnabled = ref(false);

async function loginWithPassword() {
  if (!loginPassword.value || loginBusy.value) return;
  loginBusy.value = true;
  try {
    const session = await chatApi.login(
      loginPassword.value,
      loginUsername.value.trim() || null,
    );
    api.token = session.access_token;
    loginPassword.value = "";
    await connect();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "登录失败", true);
  } finally {
    loginBusy.value = false;
  }
}

function loginWithPnkx() {
  window.location.href = "/api/v1/auth/sso/login";
}

function backToMyModules() {
  void router.push({ path: defaultModulePath(role.value) });
}

const navGroups = computed(() => groupsForRole(role.value));

if (api.token) void connect();
else {
  setStatus("尚未登录", true);
  void fetch("/api/v1/auth/sso/status")
    .then((res) => (res.ok ? res.json() : { enabled: false }))
    .then((data: { enabled?: boolean }) => {
      ssoEnabled.value = data.enabled === true;
    })
    .catch(() => {
      ssoEnabled.value = false;
    });
}
</script>

<template>
  <div v-if="!connected" class="auth">
    <el-card class="card" shadow="never">
      <h1>Aria 管理后台</h1>
      <form class="login-form" @submit.prevent="loginWithPassword">
        <el-input v-model="loginUsername" placeholder="用户名（未设置可留空）" autocomplete="username" />
        <el-input
          v-model="loginPassword"
          type="password"
          placeholder="密码"
          autocomplete="current-password"
          show-password
        />
        <el-button type="primary" native-type="submit" :disabled="loginBusy || !loginPassword" :loading="loginBusy">登录</el-button>
      </form>
      <template v-if="ssoEnabled">
        <div class="login-divider"><span>或</span></div>
        <el-button @click="loginWithPnkx">使用 pnkx 账号登录</el-button>
      </template>
      <p v-if="statusText" class="status" :class="{ error: statusError }">{{ statusText }}</p>
    </el-card>
  </div>

  <div v-else class="shell admin-shell">
    <aside>
      <div class="brand"><span class="brand-mark">A</span><span>Aria Hub</span></div>
      <nav class="side-menu" aria-label="管理后台导航">
        <section v-for="group in navGroups" :key="group.group" class="nav-group">
          <p>{{ group.group }}</p>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            :class="{ active: item.path === normalizedPath }"
            @click="openModule(item.path, tabsFor(item, role)[0]?.key ?? '')"
          >{{ item.label }}</button>
        </section>
      </nav>
      <div class="privacy-note"><span class="dot"></span>{{ statusText }}</div>
    </aside>
    <main>
      <header class="topbar">
        <h1>{{ heading }}</h1>
        <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
      </header>
      <div v-if="deniedModule" class="route-content denied">
        <el-card class="card" shadow="never">
          <h2>需要业主权限</h2>
          <p class="hint">「{{ deniedModule.label }}」属于中枢全局配置，仅业主可访问。你已登录为成员（{{ displayName }}）。</p>
          <el-button type="primary" @click="backToMyModules">返回我的工作区</el-button>
        </el-card>
      </div>
      <template v-else-if="activeModule">
        <div class="module-navigation">
          <div v-if="visibleTabs.length > 1" class="module-tabs" role="tablist" :aria-label="`${heading}子功能`">
            <button
              v-for="tab in visibleTabs"
              :key="tab.key"
              type="button"
              role="tab"
              :aria-selected="activeTab === tab.key"
              :class="{ active: activeTab === tab.key }"
              @click="openTab(tab.key)"
            >{{ tab.label }}</button>
          </div>
          <div id="module-tab-actions" class="module-tab-actions" />
        </div>
        <div class="route-content" :class="{ 'route-content--fixed': activeModule.key === 'models' }">
          <router-view @status="setStatus" />
        </div>
      </template>
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
.login-form { display: grid; gap: 10px; }
.login-divider { display: flex; align-items: center; gap: 10px; color: #8a94a6; font-size: 12px; }
.login-divider::before, .login-divider::after { content: ""; flex: 1; height: 1px; background: #e3e8f2; }

.shell { display: grid; grid-template-columns: 230px 1fr; height: 100%; }
aside { display: flex; flex-direction: column; gap: 18px; border-right: 1px solid var(--line); padding: 16px; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.brand-mark { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 9px; background: var(--accent); color: #fff; }
.side-menu { display: grid; gap: 10px; }
.nav-group > p { margin: 0 10px 4px; color: #9aa4b5; font-size: 10px; font-weight: 700; letter-spacing: .1em; }
.nav-group button { width: 100%; height: 36px; border: 0; border-radius: 8px; padding: 0 11px; background: transparent; color: #66738a; font: inherit; font-size: 13px; text-align: left; cursor: pointer; }
.nav-group button:hover { background: #f7f9fd; color: #172033; }
.nav-group button.active { color: #3658e8; background: #eef2ff; font-weight: 600; }
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
.module-navigation { display:flex; align-items:center; gap:16px; flex:none; min-width:0; margin:12px 22px 0; }
.module-tabs { display: flex; gap: 4px; flex: 0 1 auto; min-width:0; padding: 4px; width: fit-content; max-width: 100%; overflow-x: auto; border: 1px solid #e1e6f0; border-radius: 10px; background: #fff; box-shadow: 0 3px 10px rgba(34,49,82,.04); }
.module-tab-actions { min-width:0; margin-left:auto; flex-shrink:0; }
.module-tab-actions:empty { display:none; }
.route-content { flex:1; min-height:0; overflow:auto; }
.route-content.denied { display:grid; place-items:center; padding:24px; }
.route-content.denied .card { width:min(420px,90vw); border-radius:14px; }
.route-content.denied .card :deep(.el-card__body) { display:grid; gap:12px; padding:26px; }
.route-content.denied h2 { margin:0; font-size:17px; }
.route-content.denied .hint { color:#68748a; font-size:13px; margin:0; line-height:1.6; }
.route-content--fixed { overflow:hidden; }
.route-content--fixed > :deep(*) { height:100%; min-height:0; }
.module-tabs, .side-menu { scrollbar-width: none; }
.module-tabs::-webkit-scrollbar, .side-menu::-webkit-scrollbar { display: none; }
.module-tabs button { flex: none; height: 34px; border: 0; border-radius: 7px; padding: 0 15px; background: transparent; color: #6b768a; font: inherit; font-size: 12px; cursor: pointer; }
.module-tabs button:hover { color: #314358; background: #f7f9fd; }
.module-tabs button.active { background: #4f6df5; color: #fff; font-weight: 600; box-shadow: 0 3px 8px rgba(79,109,245,.2); }

@media (max-width: 720px) {
  .shell { grid-template-columns: 1fr; }
  aside { height: auto; flex-direction: column; align-items: stretch; gap: 10px; overflow: visible; }
  .side-menu { display: flex; width: 100%; overflow-x: auto; }
  .nav-group { display: flex; }
  .nav-group > p { display: none; }
  .nav-group button { width: auto; white-space: nowrap; }
  .privacy-note { display: none; }
  .module-navigation { margin-inline:14px; flex-wrap:wrap; }
  .module-tab-actions { width:100%; margin-left:0; overflow-x:auto; }
}
</style>
