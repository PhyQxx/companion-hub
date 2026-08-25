<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "summary" });
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface PrivacyDist {
  level: string;
  event_count: number;
  message_count: number;
  memory_count: number;
  timeline_count: number;
}

interface EgressRecord {
  id: string;
  kind: string;
  provider: string | null;
  privacy_level: string;
  created_at: string;
}

interface OperationAudit {
  id: string;
  kind: string;
  actor: string;
  description: string;
  created_at: string;
}

interface PrivacyData {
  summary: PrivacyDist[];
  egress: EgressRecord[];
  operations: OperationAudit[];
  total_conversations: number;
  total_messages: number;
  total_memories: number;
}

const data = ref<PrivacyData | null>(null);
const loading = ref(false);

async function refresh() {
  loading.value = true;
  try {
    data.value = await api.request<PrivacyData>("/api/v1/admin/dashboard/privacy");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <div v-if="data" v-loading="loading">
      <!-- 隐私概览 -->
      <div v-if="mode === 'summary'">
        <div class="stats">
          <el-card shadow="never"><span>会话</span><strong>{{ data.total_conversations }}</strong></el-card>
          <el-card shadow="never"><span>消息</span><strong>{{ data.total_messages }}</strong></el-card>
          <el-card shadow="never"><span>记忆</span><strong>{{ data.total_memories }}</strong></el-card>
        </div>
        <el-table :data="data.summary" size="small" style="margin-top: 12px;">
          <el-table-column prop="level" label="隐私等级" width="100"><template #default="{ row }"><el-tag :type="row.level === 'L2' ? 'danger' : row.level === 'L1' ? 'warning' : 'success'" size="small">{{ row.level }}</el-tag></template></el-table-column>
          <el-table-column prop="event_count" label="事件" width="100" />
          <el-table-column prop="message_count" label="消息" width="100" />
          <el-table-column prop="memory_count" label="记忆" width="100" />
          <el-table-column prop="timeline_count" label="时间线" width="100" />
        </el-table>
      </div>

      <!-- 数据流向 -->
      <div v-if="mode === 'flow'">
        <el-card shadow="never">
          <template #header><span>数据生命周期</span></template>
          <div class="flow">
            <div class="flow-step"><el-tag type="success">采集</el-tag><span>事件 (event) → 消息 (message)</span></div>
            <div class="flow-step"><el-tag type="primary">处理</el-tag><span>认知决策 → 记忆提取 → 时间线</span></div>
            <div class="flow-step"><el-tag type="warning">存储</el-tag><span>记忆持久化 → 配置版本 → 删除台账</span></div>
            <div class="flow-step"><el-tag type="danger">删除</el-tag><span>用户请求 → 删除台账 → 跨存储清理</span></div>
          </div>
        </el-card>
        <el-alert title="详细流向图需要后续实现" type="info" :closable="false" show-icon style="margin-top: 12px;" />
      </div>

      <!-- 外发审计 -->
      <div v-if="mode === 'egress'">
        <el-table :data="data.egress" size="small">
          <el-table-column prop="id" label="ID" width="220"><template #default="{ row }"><code>{{ row.id.slice(0, 8) }}</code></template></el-table-column>
          <el-table-column prop="kind" label="渠道" width="120" />
          <el-table-column prop="provider" label="Provider" width="140"><template #default="{ row }">{{ row.provider ?? "—" }}</template></el-table-column>
          <el-table-column prop="privacy_level" label="隐私" width="80" />
          <el-table-column prop="created_at" label="时间" width="170"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template></el-table-column>
        </el-table>
      </div>

      <!-- 操作审计 -->
      <div v-if="mode === 'operations'">
        <el-table :data="data.operations" size="small">
          <el-table-column prop="id" label="ID" width="80" />
          <el-table-column prop="kind" label="类型" width="120"><template #default="{ row }"><el-tag size="small">{{ row.kind }}</el-tag></template></el-table-column>
          <el-table-column prop="actor" label="操作人" width="120" />
          <el-table-column prop="description" label="描述" />
          <el-table-column prop="created_at" label="时间" width="170"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template></el-table-column>
        </el-table>
      </div>

      <!-- 策略与验证 -->
      <div v-if="mode === 'policies'">
        <el-card shadow="never">
          <template #header><span>当前隐私策略</span></template>
          <div class="meta">
            <div><dt>消息隐私等级</dt><dd>L0 / L1 / L2 三级约束</dd></div>
            <div><dt>记忆隐私等级</dt><dd>L0 / L1 / L2 三级约束</dd></div>
            <div><dt>事件隐私等级</dt><dd>L0 / L1 / L2 三级约束</dd></div>
            <div><dt>删除策略</dt><dd>台账记录 + 跨存储硬删除</dd></div>
            <div><dt>外发审计</dt><dd>主动推送渠道记录</dd></div>
          </div>
        </el-card>
        <el-alert title="Canary 检测与策略配置表单后续补充" type="info" :closable="false" show-icon style="margin-top: 12px;" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.flow { display: grid; gap: 12px; }
.flow-step { display: flex; align-items: center; gap: 12px; font-size: 13px; }
.meta { display: grid; gap: 8px; }
.meta div { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-weight: 600; }
code { font-size: 11px; background: #f5f7fa; padding: 1px 4px; border-radius: 3px; }
</style>
