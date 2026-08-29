import { createApp } from "vue";
import { initializeTheme } from "@aria/shared";
import App from "./App.vue";
import "./style.css";

initializeTheme();
createApp(App).mount("#app");

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/chat/sw.js", { scope: "/chat/" });
  });
}
