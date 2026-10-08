import { createApp } from "vue";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import { initializeTheme } from "@aria/shared";
import App from "./App.vue";
import router from "./router";
import "./style.css";
import "./admin/admin-style.css";

initializeTheme();
createApp(App).use(router).use(ElementPlus).mount("#app");

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/chat/sw.js", { scope: "/chat/" });
  });
}
