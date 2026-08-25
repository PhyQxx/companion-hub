<script setup lang="ts">
import { computed, inject, nextTick, onMounted, onUnmounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const lines = ref<string[]>([]);
const paused = ref(false);
const error = ref<string>("");
const terminal = ref<HTMLDivElement | null>(null);
const filterLevel = ref<string>("ALL");
let abortCtrl: AbortController | null = null;

const LEVELS = [
  { label: "全部", value: "ALL" },
  { label: "DEBUG", value: "DEBUG" },
  { label: "INFO", value: "INFO" },
  { label: "WARNING", value: "WARNING" },
  { label: "ERROR", value: "ERROR" },
];

function levelColor(line: string): string {
  if (line.includes("[ERROR]") || line.includes(" CRITICAL ")) return "#f56c6c";
  if (line.includes("[WARNING]") || line.includes("[WARN]")) return "#e6a23c";
  if (line.includes("[DEBUG]")) return "#909399";
  if (line.includes("[INFO]")) return "#67c23a";
  return "#c0c4cc";
}

function levelMatch(line: string, level: string): boolean {
  if (level === "ALL") return true;
  if (level === "WARNING") return line.includes("[WARNING]") || line.includes("[WARN]");
  return line.includes(`[${level}]`);
}

const filteredLines = computed(() => {
  if (filterLevel.value === "ALL") return lines.value;
  return lines.value.filter((l) => levelMatch(l, filterLevel.value));
});

async function scrollToBottom() {
  await nextTick();
  if (terminal.value) {
    terminal.value.scrollTop = terminal.value.scrollHeight;
  }
}

async function connect() {
  paused.value = false;
  error.value = "";
  abortCtrl?.abort();
  abortCtrl = new AbortController();

  try {
    const history = await api.request<{ line: string }[]>(
      "/api/v1/admin/logs/history?limit=200",
      { method: "GET" },
    );
    lines.value = history.map((h) => h.line);
    await scrollToBottom();
  } catch {
    // 历史拉不到继续连流
  }

  const url = `${api.baseUrl}/api/v1/admin/logs/stream`;
  try {
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${api.token}` },
      signal: abortCtrl.signal,
    });
    if (!response.body) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!abortCtrl.signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          const parsed = JSON.parse(payload) as { line: string };
          if (!paused.value) {
            lines.value.push(parsed.line);
            if (lines.value.length > 2000) {
              lines.value = lines.value.slice(-2000);
            }
          }
        } catch {
          // 忽略非 JSON 行
        }
      }
      if (!paused.value) await scrollToBottom();
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") return;
    error.value = err instanceof Error ? err.message : "连接失败";
  }
}

function disconnect() {
  abortCtrl?.abort();
  abortCtrl = null;
}

function clear() {
  lines.value = [];
}

onMounted(connect);
onUnmounted(disconnect);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-select v-model="filterLevel" size="small" style="width: 110px;">
        <el-option
          v-for="lvl in LEVELS"
          :key="lvl.value"
          :label="lvl.label"
          :value="lvl.value"
        />
      </el-select>
      <div class="spacer" />
      <el-tag v-if="paused" type="warning" size="small">已暂停</el-tag>
      <el-tag v-else-if="error" type="danger" size="small">{{ error }}</el-tag>
      <el-tag v-else type="success" size="small">接收中 {{ lines.length }} 行</el-tag>
      <el-button size="small" text @click="paused = !paused">
        {{ paused ? "继续" : "暂停" }}
      </el-button>
      <el-button size="small" text @click="clear">清空</el-button>
      <el-button size="small" text @click="connect">重连</el-button>
    </div>

    <div ref="terminal" class="terminal">
      <div
        v-for="(line, idx) in filteredLines"
        :key="idx"
        class="line"
        :style="{ color: levelColor(line) }"
      >
        {{ line }}
      </div>
      <div v-if="filteredLines.length === 0" class="empty">等待日志...</div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; display: flex; flex-direction: column; height: 100%; box-sizing: border-box; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; flex-shrink: 0; }
.spacer { flex: 1; }
.terminal {
  flex: 1;
  background: #0d0d0d;
  color: #c0c4cc;
  font-family: "SF Mono", "Fira Code", "Consolas", monospace;
  font-size: 12px;
  line-height: 1.6;
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid #333;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  min-height: 0;
}
.line { padding: 1px 0; }
.empty { color: #606266; font-style: italic; padding: 20px 0; text-align: center; }
</style>
