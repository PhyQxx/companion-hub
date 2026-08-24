import { createApp } from "vue";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "./style.css";
import AdminNavigationDemo from "./views/AdminNavigationDemo.vue";

createApp(AdminNavigationDemo).use(ElementPlus).mount("#app");
