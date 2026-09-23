<script setup lang="ts">
import { computed, provide, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { AdminApi } from "@aria/shared";
import { adminGroups, findAdminModule } from "./admin-navigation";

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
const router = useRouter();
const normalizedPath = computed(() => {
  const path = route.path.replace(/\/$/, "") || "/";
  return path === "/timeline" ? "/memory" : path;
});
const activeModule = computed(() => findAdminModule(normalizedPath.value));
const heading = computed(() => activeModule.value.label);
const activeTab = computed(() => String(route.query.tab ?? activeModule.value.tabs[0].key));

function ensureActiveTab() {
  const valid = activeModule.value.tabs.some((tab) => tab.key === route.query.tab);
  if (!valid) void router.replace({ path: activeModule.value.path, query: { ...route.query, tab: activeModule.value.tabs[0].key } });
}

watch(() => [normalizedPath.value, route.query.tab], ensureActiveTab, { flush: "post" });
void router.isReady().then(ensureActiveTab);

function openModule(path: string, tab: string) {
  void router.push({ path, query: { tab } });
}

function openTab(tab: string) {
  void router.replace({ path: activeModule.value.path, query: { ...route.query, tab } });
}

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
      <nav class="side-menu" aria-label="管理后台导航">
        <section v-for="group in adminGroups" :key="group.group" class="nav-group">
          <p>{{ group.group }}</p>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            :class="{ active: item.path === activeModule.path }"
            @click="openModule(item.path, item.tabs[0].key)"
          >{{ item.label }}</button>
        </section>
      </nav>
      <div class="privacy-note"><span class="dot"></span>管理 API 已连接</div>
    </aside>
    <main>
      <header class="topbar">
        <h1>{{ heading }}</h1>
        <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
      </header>
      <div class="module-navigation">
        <div v-if="activeModule.tabs.length > 1" class="module-tabs" role="tablist" :aria-label="`${heading}子功能`">
          <button
            v-for="tab in activeModule.tabs"
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
