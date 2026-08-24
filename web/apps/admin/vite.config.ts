import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "/admin/",
  build: {
    rollupOptions: {
      input: {
        admin: "index.html",
        demo: "demo.html",
      },
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
