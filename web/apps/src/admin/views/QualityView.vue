<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface MemoryItem {
  id: number;
  type: string;
  status: string;
  confidence: number | null;
  importance: number;
  origin_kind: string;
  access_count: number;
  created_at: string;
}

const memories = ref<MemoryItem[]>([]);
const loading = ref(false);

const typeLabels: Record<string, string> = {
  semantic: "语义",
  preference: "偏好",
  commitment: "承诺",
  episodic: "事件",
  emotional: "情感",
};

const originLabels: Record<string, string> = {
  user_statement: "用户陈述",
  assistant_statement: "助手自述",
  shared_turn: "共同回合",
  system_event: "系统事件",
  manual: "手动添加",
};

async function refresh() {
  loading.value = true;
  try {
    const data = await api.request<MemoryItem[]>("/api/v1/admin/memories?status=active&limit=200");
    memories.value = data;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

const stats = computed(() => {
  const total = memories.value.length;
  if (!total) return null;
  const withConfidence = memories.value.filter((m) => m.confidence !== null);
  const avgConfidence = withConfidence.length
    ? withConfidence.reduce((s, m) => s + (m.confidence ?? 0), 0) / withConfidence.length
    : 0;
  const avgImportance = memories.value.reduce((s, m) => s + m.importance, 0) / total;
  const typeDist: Record<string, number> = {};
  const originDist: Record<string, number> = {};
  for (const m of memories.value) {
    typeDist[m.type] = (typeDist[m.type] ?? 0) + 1;
    originDist[m.origin_kind] = (originDist[m.origin_kind] ?? 0) + 1;
  }
  return { total, confidenceCount: withConfidence.length, avgConfidence, avgImportance, typeDist, originDist };
});

const lowConfidence = computed(() =>
  memories.value.filter((m) => m.confidence !== null && m.confidence < 0.5).sort((a, b) => (a.confidence ?? 0) - (b.confidence ?? 0)),
);

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <div v-if="stats" class="stats">
      <el-card shadow="never"><span>活跃记忆</span><strong>{{ stats.total }}</strong></el-card>
      <el-card shadow="never"><span>有置信度</span><strong>{{ stats.confidenceCount }}</strong></el-card>
      <el-card shadow="never"><span>平均置信度</span><strong>{{ (stats.avgConfidence * 100).toFixed(1) }}%</strong></el-card>
      <el-card shadow="never"><span>平均重要度</span><strong>{{ (stats.avgImportance * 100).toFixed(1) }}%</strong></el-card>
    </div>

    <div v-if="stats" class="dist-grid">
      <el-card shadow="never">
        <template #header><span>类型分布</span></template>
        <div class="dist">
          <div v-for="(count, key) in stats.typeDist" :key="key">
            <span>{{ typeLabels[key] ?? key }}</span>
            <el-progress :percentage="Math.round((count / stats.total) * 100)" :stroke-width="14" :show-text="false" />
            <small>{{ count }}</small>
          </div>
        </div>
      </el-card>
      <el-card shadow="never">
        <template #header><span>来源分布</span></template>
        <div class="dist">
          <div v-for="(count, key) in stats.originDist" :key="key">
            <span>{{ originLabels[key] ?? key }}</span>
            <el-progress :percentage="Math.round((count / stats.total) * 100)" :stroke-width="14" :show-text="false" />
            <small>{{ count }}</small>
          </div>
        </div>
      </el-card>
    </div>

    <el-card v-if="lowConfidence.length" shadow="never" class="low-confidence">
      <template #header>
        <div class="section-head">
          <span>低置信度记忆（&lt; 50%）</span>
          <el-tag type="warning" size="small">{{ lowConfidence.length }} 条</el-tag>
        </div>
      </template>
      <el-table :data="lowConfidence" size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="type" label="类型" width="90">
          <template #default="{ row }">
            {{ typeLabels[row.type] ?? row.type }}
          </template>
        </el-table-column>
        <el-table-column prop="confidence" label="置信度" width="90">
          <template #default="{ row }">
            <el-tag :type="(row.confidence ?? 0) < 0.3 ? 'danger' : 'warning'" size="small">
              {{ ((row.confidence ?? 0) * 100).toFixed(0) }}%
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="importance" label="重要度" width="90">
          <template #default="{ row }">
            {{ (row.importance * 100).toFixed(0) }}%
          </template>
        </el-table-column>
        <el-table-column prop="access_count" label="访问" width="70" />
        <el-table-column prop="created_at" label="创建时间">
          <template #default="{ row }">
            {{ new Date(row.created_at).toLocaleString() }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; display: grid; gap: 14px; align-content: start; }
.toolbar { display: flex; gap: 8px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.stats :deep(.el-card__body) { padding: 14px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 20px; }
.dist-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.dist { display: grid; gap: 10px; }
.dist > div { display: grid; grid-template-columns: 80px 1fr 40px; align-items: center; gap: 10px; font-size: 12px; }
.dist span { color: var(--muted); }
.dist small { text-align: right; }
.low-confidence { margin-top: 4px; }
.section-head { display: flex; justify-content: space-between; align-items: center; }
</style>
