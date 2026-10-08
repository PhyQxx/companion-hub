<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

interface MemoryItem {
  id: number;
  user_id: string;
  subject: string;
  type: string;
  content: string;
  confidence: number | null;
  importance: number;
  conflict_with: number | null;
  created_at: string;
  updated_at: string;
}

const memories = ref<MemoryItem[]>([]);
const loading = ref(false);
const resolving = ref<number | null>(null);

const typeLabels: Record<string, string> = {
  semantic: "语义",
  preference: "偏好",
  commitment: "承诺",
  episodic: "事件",
  emotional: "情感",
};

async function refresh() {
  loading.value = true;
  try {
    const data = await api.request<MemoryItem[]>("/api/v1/admin/memories?limit=200");
    memories.value = data.filter((m) => m.conflict_with !== null);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function resolveConflict(id: number, action: "adopt" | "keep") {
  resolving.value = id;
  try {
    await ElMessageBox.confirm(
      action === "adopt" ? "确认采纳新记忆，替换旧记忆？" : "确认保留旧记忆，拒绝新记忆？",
      "冲突裁决",
      { confirmButtonText: "确认", cancelButtonText: "取消", type: "warning" },
    );
    await api.request(`/api/v1/admin/memories/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    ElMessage.success("裁决完成");
    await refresh();
  } catch (error) {
    if (error !== "cancel") {
      emit("status", error instanceof Error ? error.message : "裁决失败", true);
    }
  } finally {
    resolving.value = null;
  }
}

onMounted(refresh);
</script>

<template>
  <section class="content">
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
      <el-tag v-if="memories.length" type="warning">{{ memories.length }} 条冲突</el-tag>
      <el-tag v-else type="success">无冲突</el-tag>
    </div>

    <el-empty v-if="!loading && !memories.length" description="当前没有冲突记忆" />

    <div v-for="item in memories" :key="item.id" class="conflict-card">
      <el-card shadow="never">
        <div class="conflict-head">
          <div>
            <el-tag size="small">ID {{ item.id }}</el-tag>
            <el-tag size="small" type="info">{{ typeLabels[item.type] ?? item.type }}</el-tag>
            <el-tag v-if="item.confidence !== null" size="small" :type="item.confidence >= 0.7 ? 'success' : item.confidence >= 0.4 ? 'warning' : 'danger'">
              置信度 {{ (item.confidence * 100).toFixed(0) }}%
            </el-tag>
          </div>
          <div class="actions">
            <el-button size="small" type="primary" :loading="resolving === item.id" @click="resolveConflict(item.id, 'adopt')">
              采纳新记忆
            </el-button>
            <el-button size="small" :loading="resolving === item.id" @click="resolveConflict(item.id, 'keep')">
              保留旧记忆
            </el-button>
          </div>
        </div>
        <p class="content-text">{{ item.content }}</p>
        <div class="meta">
          <span>冲突对象 ID: {{ item.conflict_with }}</span>
          <span>重要度: {{ (item.importance * 100).toFixed(0) }}%</span>
          <span>创建于 {{ new Date(item.created_at).toLocaleString() }}</span>
        </div>
      </el-card>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 16px 22px 28px; overflow-y: auto; }
.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.conflict-card + .conflict-card { margin-top: 12px; }
.conflict-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }
.conflict-head > div:first-child { display: flex; gap: 6px; }
.actions { display: flex; gap: 8px; }
.content-text { margin: 0 0 10px; font-size: 14px; line-height: 1.6; color: #303133; }
.meta { display: flex; gap: 16px; font-size: 12px; color: var(--muted); }
</style>
