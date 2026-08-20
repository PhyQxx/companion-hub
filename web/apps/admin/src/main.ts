import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./style.css";

const router = createRouter({
  history: createWebHistory("/admin"),
  routes: [
    { path: "/", component: () => import("./views/OverviewView.vue") },
    { path: "/models", component: () => import("./views/ModelsView.vue") },
    { path: "/personas", component: () => import("./views/PersonasView.vue") },
    { path: "/memory", component: () => import("./views/MemoryView.vue") },
    { path: "/timeline", component: () => import("./views/TimelineView.vue") },
    {
      path: "/:module(devices|logs|privacy|settings)",
      component: () => import("./views/PlaceholderView.vue"),
      props: true,
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

createApp(App).use(router).use(ElementPlus).mount("#app");
