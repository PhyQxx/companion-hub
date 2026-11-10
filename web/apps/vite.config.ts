import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需导入：模板组件自动注册并注入对应样式；
    // ElMessage/ElMessageBox 为脚本显式导入，样式在 main.ts 单独引入。
    Components({ resolvers: [ElementPlusResolver()], dts: "src/components.d.ts" }),
  ],
  base: "/chat/",
  server: {
    port: 5175,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
