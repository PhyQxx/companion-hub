<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const config = ref<{ version: number; content_hash: string; config?: unknown } | null>(null);
const personas = ref<{ version: number; persona: { name?: string } } | null>(null);
const memories = ref<number | null>(null);
const ledger = ref<unknown[] | null>(null);

onMounted(async () => {
  try {
    config.value = await api.request("/api/v1/admin/config/current");
    personas.value = await api.request("/api/v1/admin/personas/current");
    memories.value = (
      await api.request<unknown[]>("/api/v1/admin/memories?status=active&limit=200")
    ).length;
    ledger.value = await api.request<unknown[]>("/api/v1/admin/deletion-ledger?limit=200");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
});
</script>

<template>
  <section class="content">
    <div class="stats">
      <article><span>模型配置</span><strong>v{{ config?.version ?? "—" }}</strong><small>{{ config?.content_hash?.slice(0, 12) ?? "" }}</small></article>
      <article><span>当前 Persona</span><strong>{{ personas?.persona?.name ?? "—" }}</strong><small>v{{ personas?.version ?? "—" }}</small></article>
      <article><span>活跃记忆</span><strong>{{ memories ?? "—" }}</strong><small>参与检索</small></article>
      <article><span>删除台账</span><strong>{{ ledger?.length ?? "—" }}</strong><small>硬删除记录</small></article>
    </div>
    <div class="links">
      <router-link to="/models">管理模型与路由 →</router-link>
      <router-link to="/personas">编辑 Persona →</router-link>
      <router-link to="/memory">记忆库治理 →</router-link>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; display: grid; gap: 18px; align-content: start; overflow-y: auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.stats article { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.stats small { color: var(--muted); font-size: 11px; }
.links { display: grid; gap: 8px; }
.links a { color: var(--accent); text-decoration: none; font-size: 14px; }
</style>
