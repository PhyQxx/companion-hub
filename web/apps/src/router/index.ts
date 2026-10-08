import { createRouter, createWebHistory } from "vue-router";
import ChatRoot from "../ChatRoot.vue";
import AdminShell from "../admin/AdminShell.vue";
import ModuleWorkspaceView from "../admin/views/ModuleWorkspaceView.vue";

// 聊天与管理后台同属一个 SPA：/ 为聊天，/admin/** 为管理后台（懒加载）。
// base 取 /chat/（生产由 FastAPI 同源服务，/admin 旧入口重定向到 /chat/admin）。
const router = createRouter({
  history: createWebHistory("/chat/"),
  routes: [
    { path: "/", name: "chat", component: ChatRoot },
    {
      path: "/admin",
      name: "admin",
      component: AdminShell,
      children: [
        { path: "", component: ModuleWorkspaceView, props: { module: "overview" } },
        { path: "models", component: ModuleWorkspaceView, props: { module: "models" } },
        { path: "integrations", component: ModuleWorkspaceView, props: { module: "integrations" } },
        { path: "skills", component: ModuleWorkspaceView, props: { module: "skills" } },
        { path: "personas", component: ModuleWorkspaceView, props: { module: "persona" } },
        { path: "memory", component: ModuleWorkspaceView, props: { module: "memory" } },
        { path: "tasks", component: ModuleWorkspaceView, props: { module: "tasks" } },
        { path: "timeline", redirect: { path: "/admin/memory", query: { tab: "timeline" } } },
        { path: "devices", component: ModuleWorkspaceView, props: { module: "devices" } },
        { path: "perception", component: ModuleWorkspaceView, props: { module: "perception" } },
        { path: "logs", component: ModuleWorkspaceView, props: { module: "logs" } },
        { path: "settings", component: ModuleWorkspaceView, props: { module: "system" } },
        { path: "jobs", redirect: { path: "/admin/settings", query: { tab: "jobs" } } },
        { path: "screen-awareness", redirect: { path: "/admin/perception", query: { tab: "screen" } } },
        { path: "avatars", redirect: { path: "/admin/personas", query: { tab: "gallery" } } },
        { path: "appearance", redirect: { path: "/admin/personas", query: { tab: "gallery" } } },
        { path: "output", redirect: { path: "/admin/perception", query: { tab: "channels" } } },
        { path: "privacy", redirect: { path: "/admin/logs", query: { tab: "summary" } } },
        { path: ":pathMatch(.*)*", redirect: "/admin" },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

// 旧管理端深链的 Tab 迁移（自 admin 工程 main.ts 平移）
router.beforeEach((to) => {
  if (!to.path.startsWith("/admin")) return true;
  const tab = typeof to.query.tab === "string" ? to.query.tab : "";
  if (to.path === "/admin/integrations" && tab === "pnkx") {
    return { path: "/admin/skills", query: { tab: "connections" } };
  }
  const movedDeviceTabs: Record<string, { path: string; tab: string; section?: string }> = {
    pairing: { path: "/admin/devices", tab: "registry" },
    channels: { path: "/admin/perception", tab: "channels" },
    home_assistant: { path: "/admin/perception", tab: "home_assistant" },
    status: { path: "/admin/perception", tab: "screen" },
    observations: { path: "/admin/perception", tab: "screen", section: "records" },
    safety: { path: "/admin/perception", tab: "safety" },
    browser_status: { path: "/admin/perception", tab: "browser" },
    browser_observations: { path: "/admin/perception", tab: "browser", section: "records" },
  };
  if (to.path === "/admin/devices") {
    const target = movedDeviceTabs[tab];
    if (!target) return true;
    const query: Record<string, string> = {};
    for (const [k, v] of Object.entries(to.query)) {
      if (typeof v === "string") query[k] = v;
    }
    query.tab = target.tab;
    if (target.section) query.section = target.section;
    return { path: target.path, query };
  }
  if (to.path === "/admin/models" && ["home_assistant", "channels"].includes(tab)) {
    return { path: "/admin/perception", query: { ...to.query, tab } };
  }
  if (to.path === "/admin/memory" && tab === "conflicts") {
    return { path: "/admin/memory", query: { ...to.query, tab: "quality", section: "conflicts" } };
  }
  if (to.path === "/admin/personas" && tab === "proactive") {
    return { path: "/admin/personas", query: { ...to.query, tab: "boundaries" } };
  }
  if (to.path === "/admin/skills" && ["generate", "suggestions"].includes(tab)) {
    return tab === "generate"
      ? { path: "/admin/skills", query: { ...to.query, tab: "create", section: "generate" } }
      : { path: "/admin/skills", query: { ...to.query, tab: "drafts", section: "suggestions" } };
  }
  return true;
});

export default router;
