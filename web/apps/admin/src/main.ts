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
    { path: "/personas", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "persona" } },
    { path: "/memory", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "memory" } },
    { path: "/tasks", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "tasks" } },
    { path: "/timeline", redirect: { path: "/memory", query: { tab: "timeline" } } },
    { path: "/devices", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "devices" } },
    { path: "/perception", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "perception" } },
    { path: "/output", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "output" } },
    { path: "/logs", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "logs" } },
    { path: "/privacy", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "privacy" } },
    { path: "/settings", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "system" } },
    { path: "/jobs", redirect: { path: "/settings", query: { tab: "jobs" } } },
    { path: "/screen-awareness", redirect: { path: "/perception", query: { tab: "screen" } } },
    { path: "/avatars", redirect: { path: "/appearance", query: { tab: "gallery" } } },
    { path: "/appearance", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "appearance" } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

// 「设备与感知」拆分为设备终端 / 感知与守护 / 主动输出、HA 连接并入感知模块后，
// 旧 /devices 与 /models 深链按原 Tab 迁移到新模块。
const movedDeviceTabs: Record<string, { path: string; tab: string; section?: string }> = {
  pairing: { path: "/devices", tab: "registry" },
  channels: { path: "/output", tab: "channels" },
  home_assistant: { path: "/perception", tab: "home_assistant" },
  status: { path: "/perception", tab: "screen" },
  observations: { path: "/perception", tab: "screen", section: "records" },
  safety: { path: "/perception", tab: "safety" },
  browser_status: { path: "/perception", tab: "browser" },
  browser_observations: { path: "/perception", tab: "browser", section: "records" },
};

router.beforeEach((to) => {
  const tab = typeof to.query.tab === "string" ? to.query.tab : "";
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
  if (to.path === "/memory" && tab === "conflicts") {
    return { path: "/memory", query: { ...to.query, tab: "quality", section: "conflicts" } };
  }
  if (to.path === "/personas" && tab === "proactive") {
    return { path: "/personas", query: { ...to.query, tab: "boundaries" } };
  }
  return true;
});

createApp(App).use(router).use(ElementPlus).mount("#app");
