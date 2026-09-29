import { createApp } from "vue";
import { createRouter, createWebHistory, type LocationQuery } from "vue-router";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./style.css";

const router = createRouter({
  history: createWebHistory("/admin/"),
  routes: [
    { path: "/", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "overview" } },
    { path: "/models", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "models" } },
    { path: "/integrations", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "integrations" } },
    { path: "/skills", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "skills" } },
    { path: "/personas", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "persona" } },
    { path: "/memory", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "memory" } },
    { path: "/tasks", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "tasks" } },
    { path: "/timeline", redirect: { path: "/memory", query: { tab: "timeline" } } },
    { path: "/devices", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "devices" } },
    { path: "/perception", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "perception" } },
    { path: "/logs", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "logs" } },
    { path: "/privacy", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "privacy" } },
    { path: "/settings", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "system" } },
    { path: "/jobs", redirect: { path: "/settings", query: { tab: "jobs" } } },
    { path: "/screen-awareness", redirect: { path: "/perception", query: { tab: "screen" } } },
    { path: "/avatars", redirect: { path: "/personas", query: { tab: "gallery" } } },
    // 信息架构合并：形象与外观并入「人格与形象」；主动输出并入「感知与守护」；
    // 隐私与安全并入「观测与审计」。
    { path: "/appearance", redirect: { path: "/personas", query: { tab: "gallery" } } },
    { path: "/output", redirect: { path: "/perception", query: { tab: "channels" } } },
    { path: "/privacy", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "logs" } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

// 「设备与感知」拆分为设备终端 / 感知与守护、HA 连接并入感知模块后，
// 旧 /devices 与 /models 深链按原 Tab 迁移到新模块；后续信息架构合并
// 再把主动输出并入感知模块、形象与外观并入了人格模块。
const movedDeviceTabs: Record<string, { path: string; tab: string; section?: string }> = {
  pairing: { path: "/devices", tab: "registry" },
  channels: { path: "/perception", tab: "channels" },
  home_assistant: { path: "/perception", tab: "home_assistant" },
  status: { path: "/perception", tab: "screen" },
  observations: { path: "/perception", tab: "screen", section: "records" },
  safety: { path: "/perception", tab: "safety" },
  browser_status: { path: "/perception", tab: "browser" },
  browser_observations: { path: "/perception", tab: "browser", section: "records" },
};

router.beforeEach((to) => {
  const tab = typeof to.query.tab === "string" ? to.query.tab : "";
  if (to.path === "/integrations" && tab === "pnkx") {
    return { path: "/skills", query: { tab: "connections" } };
  }
  if (to.path === "/devices") {
    const target = movedDeviceTabs[tab];
    if (!target) return true;
    const query: LocationQuery = { ...to.query };
    query.tab = target.tab;
    if (target.section) query.section = target.section;
    else delete query.section;
    return { path: target.path, query };
  }
  if (to.path === "/models" && tab === "home_assistant") {
    return { path: "/perception", query: { ...to.query, tab: "home_assistant" } };
  }
  if (to.path === "/models" && tab === "channels") {
    return { path: "/perception", query: { ...to.query, tab: "channels" } };
  }
  if (to.path === "/memory" && tab === "conflicts") {
    return { path: "/memory", query: { ...to.query, tab: "quality", section: "conflicts" } };
  }
  if (to.path === "/personas" && tab === "proactive") {
    return { path: "/personas", query: { ...to.query, tab: "boundaries" } };
  }
  if (to.path === "/skills" && tab === "generate") {
    // 智能创建并入「创建技能」页内切换
    return { path: "/skills", query: { ...to.query, tab: "create", section: "generate" } };
  }
  if (to.path === "/skills" && tab === "suggestions") {
    // 学习建议并入「审阅中心」页内切换
    return { path: "/skills", query: { ...to.query, tab: "drafts", section: "suggestions" } };
  }
  if (to.path === "/privacy") {
    // 隐私与安全并入「观测与审计」：旧 Tab 迁移到新键
    const movedPrivacyTabs: Record<string, string> = {
      summary: "summary",
      flow: "summary",
      egress: "egress",
      operations: "egress",
      policies: "policies",
    };
    const target = movedPrivacyTabs[tab] ?? "summary";
    return { path: "/logs", query: { ...to.query, tab: target } };
  }
  if (to.path === "/logs" && ["events", "performance", "errors", "alerts"].includes(tab)) {
    // 日志追踪 Tab 合并：事件并入 Trace，性能/错误/告警并入分析与告警
    const merged = tab === "events" ? "traces" : "analysis";
    return { path: "/logs", query: { ...to.query, tab: merged } };
  }
  return true;
});

createApp(App).use(router).use(ElementPlus).mount("#app");
