<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

interface MeetingItem {
  id: string;
  title: string;
  participants: string[];
  privacy_level: "L1" | "L2";
  status: string;
  consent_at: string | null;
  summary: string | null;
  decisions: Array<{ text: string }>;
  action_items: Array<{ title: string; owner: string | null; status: string }>;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const meetings = ref<MeetingItem[]>([]);
const loading = ref(false);

const statusLabels: Record<string, { label: string; type: "info" | "success" | "warning" | "danger" }> = {
  prepared: { label: "已准备", type: "info" },
  recording: { label: "录音中", type: "warning" },
  consent_revoked: { label: "授权已撤销", type: "danger" },
  completed: { label: "已完成", type: "success" },
  cancelled: { label: "已取消", type: "info" },
};

function fmt(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

async function load() {
  loading.value = true;
  try {
    meetings.value = await api.request<MeetingItem[]>("/api/v1/admin/butler/meetings");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "会议加载失败", true);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">个人管家 · MEET-01</div>
        <h2>会议助手</h2>
        <p>会议记录与摘要的只读检视。录音授权、转写上传与行动项确认都在聊天端进行——Hub 不接收原始音频，Admin 也不提供写入口。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <div class="panel">
      <el-table v-loading="loading" :data="meetings" empty-text="还没有会议记录（从日历准备会议或让 Aria 帮你记录）" style="width:100%">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="detail">
              <p v-if="row.summary"><strong>摘要：</strong>{{ row.summary }}</p>
              <p v-if="row.decisions.length"><strong>决定：</strong></p>
              <ul v-if="row.decisions.length">
                <li v-for="(decision, index) in row.decisions" :key="index">{{ decision.text }}</li>
              </ul>
              <p v-if="row.action_items.length"><strong>行动项：</strong></p>
              <ul v-if="row.action_items.length">
                <li v-for="(item, index) in row.action_items" :key="index">
                  {{ item.title }}<small v-if="item.owner"> · {{ item.owner }}</small>
                  <el-tag size="small" effect="plain" class="ai-tag">{{ item.status === "confirmed" ? "已确认" : item.status === "proposed" ? "待确认" : item.status }}</el-tag>
                </li>
              </ul>
              <p v-if="!row.summary && !row.decisions.length && !row.action_items.length" class="muted">尚无摘要（会议未完成或未生成）。</p>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="170">
          <template #default="{ row }"><strong>{{ row.title }}</strong></template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusLabels[row.status]?.type ?? 'info'" size="small">{{ statusLabels[row.status]?.label ?? row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="参与人" min-width="150">
          <template #default="{ row }">{{ row.participants.join("、") || "—" }}</template>
        </el-table-column>
        <el-table-column label="隐私" width="70">
          <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.privacy_level }}</el-tag></template>
        </el-table-column>
        <el-table-column label="授权" width="110">
          <template #default="{ row }">
            <span v-if="row.consent_at && row.status !== 'consent_revoked'">已授权</span>
            <span v-else class="muted">未授权</span>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ fmt(row.started_at ?? row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.muted { color: var(--muted); font-size: 12px; }
.detail { padding: 4px 12px; font-size: 13px; line-height: 1.7; }
.detail p { margin: 4px 0; }
.detail ul { margin: 2px 0 8px; padding-left: 18px; }
.ai-tag { margin-left: 6px; }
</style>
