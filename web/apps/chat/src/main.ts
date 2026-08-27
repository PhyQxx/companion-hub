import { createApp } from "vue";
import { initializeTheme } from "@aria/shared";
import App from "./App.vue";
import "./style.css";

initializeTheme();
createApp(App).mount("#app");
