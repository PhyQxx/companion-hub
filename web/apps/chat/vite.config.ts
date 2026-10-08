import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "/chat/",
  server: {
    port: 5175,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      // 管理后台并入同一开发入口：5175/admin/* 代理到 admin 的 dev server（5174），
      // 与生产同源结构一致，SSO 会话（localStorage）聊天/管理共用
      "/admin": { target: "http://127.0.0.1:5174", changeOrigin: true },
    },
  },
});
