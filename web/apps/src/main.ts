import { createApp } from "vue";
import { ElLoading } from "element-plus";
// 脚本式调用的组件样式按需补齐（模板组件样式由 unplugin-vue-components 注入）
import "element-plus/es/components/loading/style/css";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import { initializeTheme } from "@aria/shared";
import App from "./App.vue";
import router from "./router";
import "./style.css";
import "./admin/admin-style.css";

initializeTheme();
const app = createApp(App).use(router);
// v-loading 指令不随按需导入注册，需手动挂载
app.directive("loading", ElLoading.directive);
app.mount("#app");

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/chat/sw.js", { scope: "/chat/" });
  });
}
