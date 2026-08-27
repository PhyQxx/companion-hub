import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./style.css";

const router = createRouter({
  history: createWebHistory("/admin/"),
  routes: [
    { path: "/", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "overview" } },
    { path: "/models", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "models" } },
    { path: "/personas", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "persona" } },
    { path: "/memory", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "memory" } },
    { path: "/timeline", redirect: { path: "/memory", query: { tab: "timeline" } } },
    { path: "/devices", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "devices" } },
    { path: "/logs", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "logs" } },
    { path: "/privacy", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "privacy" } },
    { path: "/settings", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "system" } },
    { path: "/jobs", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "jobs" } },
    { path: "/avatars", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "avatars" } },
    { path: "/appearance", component: () => import("./views/ModuleWorkspaceView.vue"), props: { module: "appearance" } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

createApp(App).use(router).use(ElementPlus).mount("#app");
