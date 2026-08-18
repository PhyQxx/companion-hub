<script setup lang="ts">
defineProps<{ module: string }>();
const labels: Record<string, { title: string; note: string }> = {
  devices: { title: "设备", note: "设备接入、遥测与通道隐私调整随 M3A 传感器批次交付。" },
  logs: { title: "日志追踪", note: "结构化日志检索与 Trace 下钻随可观测性批次交付。" },
  privacy: { title: "隐私审计", note: "隐私决策审计视图随删除闭环后的审计批次交付。" },
  settings: { title: "系统配置", note: "系统级设置（备份、升级、license）随运维批次交付。" },
};
</script>

<template>
  <section class="content">
    <div class="card">
      <h2>{{ labels[module]?.title ?? module }}</h2>
      <p>{{ labels[module]?.note ?? "该模块尚未交付。" }}</p>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 20px; max-width: 560px; }
h2 { margin: 0 0 8px; font-size: 16px; }
p { color: var(--muted); margin: 0; font-size: 14px; }
</style>
