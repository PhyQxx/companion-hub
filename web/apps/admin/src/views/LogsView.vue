<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "traces" });
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface DecisionTrace {
  id: string;
  user_id: string;
  trigger_kind: string;
  decision: string;
  confidence: number;
  urgency: string;
  attention_score: number;
  model_name: string | null;
  created_at: string;
}

interface EventAudit {
  event_id: string;
  user_id: string;
  kind: string;
  source_kind: string;
  disposition: string;
  privacy_level: string;
  occurred_at: string;
}

interface DeadLetter {
  id: number;
  topic: string;
  attempts: number;
  error_code: string;
  created_at: string;
}

interface CommandError {
  id: string;
  device_id: string;
  command: string;
  status: string;
  reason_code: string | null;
  issued_at: string;
}

interface LogsData {
  traces: DecisionTrace[];
  events: EventAudit[];
  dead_letters: DeadLetter[];
  command_errors: CommandError[];
  trace_count: number;
  event_count: number;
  dead_letter_count: number;
  command_error_count: number;
}

const data = ref<LogsData | null>(null);
const loading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    data.value = await api.request<LogsData>("/api/v1/admin/dashboard/logs");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

const urgencyType = (u: string) => {
  if (u === "critical") return "danger";
  if (u === "high") return "warning";
  if (u === "normal") return "info";
  return "";
};

const decisionType = (d: string) => {
  if (["act", "escalate"].includes(d)) return "danger";
  if (["inform", "ask", "suggest"].includes(d)) return "warning";
  if (d === "record") return "success";
  return "info";
};

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <div v-if="data" v-loading="loading">
      <!-- Trace 查询 -->
      <div v-if="mode === 'traces'">
        <el-table :data="data.traces" size="small">
          <el-table-column prop="id" label="ID" width="220"><template #default="{ row }"><code>{{ row.id.slice(0, 8) }}</code></template></el-table-column>
          <el-table-column prop="trigger_kind" label="触发源" width="140" />
          <el-table-column prop="decision" label="决策" width="100"><template #default="{ row }"><el-tag :type="decisionType(row.decision)" size="small">{{ row.decision }}</el-tag></template></el-table-column>
          <el-table-column prop="urgency" label="紧急度" width="90"><template #default="{ row }"><el-tag :type="urgencyType(row.urgency)" size="small">{{ row.urgency }}</el-tag></template></el-table-column>
          <el-table-column prop="confidence" label="置信度" width="90"><template #default="{ row }">{{ (row.confidence * 100).toFixed(0) }}%</template></el-table-column>
          <el-table-column prop="attention_score" label="关注度" width="90"><template #default="{ row }">{{ (row.attention_score * 100).toFixed(0) }}%</template></el-table-column>
          <el-table-column prop="model_name" label="模型" width="140" />
          <el-table-column prop="created_at" label="时间" width="170"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template></el-table-column>
        </el-table>
        <p class="hint">共 {{ data.trace_count }} 条决策记录</p>
      </div>

      <!-- 事件日志 -->
      <div v-if="mode === 'events'">
        <el-table :data="data.events" size="small">
          <el-table-column prop="event_id" label="Event ID" width="220"><template #default="{ row }"><code>{{ row.event_id.slice(0, 8) }}</code></template></el-table-column>
          <el-table-column prop="kind" label="类型" width="140" />
          <el-table-column prop="source_kind" label="来源" width="120" />
          <el-table-column prop="disposition" label="处置" width="100"><template #default="{ row }"><el-tag size="small">{{ row.disposition }}</el-tag></template></el-table-column>
          <el-table-column prop="privacy_level" label="隐私" width="70" />
          <el-table-column prop="occurred_at" label="发生时间" width="170"><template #default="{ row }">{{ new Date(row.occurred_at).toLocaleString() }}</template></el-table-column>
        </el-table>
        <p class="hint">共 {{ data.event_count }} 条事件记录</p>
      </div>

      <!-- 性能分析 -->
      <div v-if="mode === 'performance'">
        <div class="stats">
          <el-card shadow="never"><span>决策总数</span><strong>{{ data.trace_count }}</strong></el-card>
          <el-card shadow="never"><span>事件总数</span><strong>{{ data.event_count }}</strong></el-card>
          <el-card shadow="never"><span>死信数</span><strong>{{ data.dead_letter_count }}</strong></el-card>
          <el-card shadow="never"><span>命令失败</span><strong>{{ data.command_error_count }}</strong></el-card>
        </div>
        <el-alert title="性能分析需要更多指标采集（P50/P90 阶段耗时）" type="info" :closable="false" show-icon style="margin-top: 12px;" />
      </div>

      <!-- 错误分析 -->
      <div v-if="mode === 'errors'">
        <h4>死信队列（{{ data.dead_letters.length }} 条最近）</h4>
        <el-table :data="data.dead_letters" size="small" style="margin-bottom: 16px;">
          <el-table-column prop="id" label="ID" width="80" />
          <el-table-column prop="topic" label="Topic" width="180" />
          <el-table-column prop="error_code" label="错误码" width="140" />
          <el-table-column prop="attempts" label="重试" width="70" />
          <el-table-column prop="created_at" label="时间" width="170"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template></el-table-column>
        </el-table>
        <h4>设备命令失败（{{ data.command_errors.length }} 条最近）</h4>
        <el-table :data="data.command_errors" size="small">
          <el-table-column prop="command" label="命令" width="160" />
          <el-table-column prop="status" label="状态" width="100"><template #default="{ row }"><el-tag type="danger" size="small">{{ row.status }}</el-tag></template></el-table-column>
          <el-table-column prop="reason_code" label="原因码" width="160" />
          <el-table-column prop="issued_at" label="时间" width="170"><template #default="{ row }">{{ new Date(row.issued_at).toLocaleString() }}</template></el-table-column>
        </el-table>
      </div>

      <!-- 告警记录 -->
      <div v-if="mode === 'alerts'">
        <el-empty description="告警系统需要专门的告警规则与通知渠道配置" />
        <el-alert title="当前状态" type="info" :closable="false" show-icon style="margin-top: 12px;">
          <div>死信队列: {{ data.dead_letter_count }} 条</div>
          <div>命令失败: {{ data.command_error_count }} 条</div>
        </el-alert>
      </div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.hint { color: var(--muted); font-size: 12px; margin-top: 8px; }
code { font-size: 11px; background: #f5f7fa; padding: 1px 4px; border-radius: 3px; }
h4 { margin: 0 0 10px; font-size: 14px; color: var(--muted); }
</style>
