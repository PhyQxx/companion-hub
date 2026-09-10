<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

interface TimelineItem {
  id: number;
  user_id: string;
  occurred_at: string;
  ended_at: string | null;
  source_type: string;
  source_id: string;
  actor: string;
  event_type: string;
  conversation_id: string | null;
  title: string | null;
  summary: string;
  privacy_level: string;
  importance: number;
  entities: Array<Record<string, unknown>>;
  keywords: string[];
  metadata: Record<string, unknown>;
  created_at: string;
}

interface TimelineQueryResult {
  candidate_count: number;
  total: number;
  limit: number;
  offset: number;
  events: TimelineItem[];
}

interface TimelineSource {
  timeline_id: number;
  source_type: string;
  source_id: string;
  occurred_at: string;
  actor: string;
  event_type: string;
  text: string;
}

const USER_KEY = "ariaTimelineUserId";
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const actorOptions = ["user", "assistant", "device", "system", "external"];
const sourceOptions = ["message", "event", "device", "tool", "calendar", "system"];
const actorLabels: Record<string, string> = {
  user: "用户",
  assistant: "助手",
  device: "设备",
  system: "系统",
  external: "外部",
};
const sourceLabels: Record<string, string> = {
  message: "消息",
  event: "事件",
  device: "设备",
  tool: "工具",
  calendar: "日历",
  system: "系统",
};

const userId = ref(sessionStorage.getItem(USER_KEY) ?? "");
const query = ref("");
const privacy = ref("L1");
const actor = ref("");
const sourceType = ref("");
const eventType = ref("");
const conversationId = ref("");
const page = ref(1);
const limit = ref(20);
const total = ref(0);
const timeRange = ref<[Date, Date] | null>(null);
const loading = ref(false);
const events = ref<TimelineItem[]>([]);
const candidateCount = ref(0);
const selected = ref<TimelineItem | null>(null);
const selectedSource = ref<TimelineSource | null>(null);
const sourceUnavailable = ref(false);

const resultText = computed(() => {
  if (!userId.value) return "输入 user_id 后可查询历史索引";
  return `共 ${total.value} 条 · 本页显示 ${events.value.length} 条`;
});

const fmt = (value: string | null) => (value ? new Date(value).toLocaleString() : "—");
const pretty = (value: unknown) => JSON.stringify(value, null, 2);

async function resolveDefaultUser() {
  if (userId.value) return;
  try {
    const rows = await api.request<{ items: Array<{ user_id: string }> }>(
      "/api/v1/admin/memories?status=active&limit=1",
    );
    if (rows.items[0]?.user_id) userId.value = rows.items[0].user_id;
  } catch {
    // Timeline 本身不依赖 Memory；没有默认用户时保留手工输入即可。
  }
}

function setRecent(hours: number) {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
  timeRange.value = [start, end];
}

function clearFilters() {
  query.value = "";
  actor.value = "";
  sourceType.value = "";
  eventType.value = "";
  conversationId.value = "";
  privacy.value = "L1";
  timeRange.value = null;
  startQuery();
}

function startQuery() {
  page.value = 1;
  void runQuery();
}

async function runQuery() {
  const owner = userId.value.trim();
  if (!owner) {
    emit("status", "请输入 user_id 后再查询时间线", true);
    return;
  }
  sessionStorage.setItem(USER_KEY, owner);
  loading.value = true;
  try {
    const body: Record<string, unknown> = {
      user_id: owner,
      query: query.value.trim(),
      privacy_level: privacy.value,
      limit: limit.value,
      offset: (page.value - 1) * limit.value,
      actors: actor.value ? [actor.value] : [],
      source_types: sourceType.value ? [sourceType.value] : [],
      event_types: eventType.value.trim() ? [eventType.value.trim()] : [],
    };
    if (conversationId.value.trim()) body.conversation_id = conversationId.value.trim();
    if (timeRange.value) {
      body.start_at = timeRange.value[0].toISOString();
      body.end_at = timeRange.value[1].toISOString();
    }
    const result = await api.request<TimelineQueryResult>("/api/v1/admin/timeline/query", {
      method: "POST",
      body: JSON.stringify(body),
    });
    candidateCount.value = result.candidate_count;
    total.value = result.total;
    events.value = result.events;
    emit("status", `时间线查询完成：${result.events.length} 条`);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "时间线查询失败", true);
  } finally {
    loading.value = false;
  }
}

function onPageChange() {
  void runQuery();
}

async function showDetail(item: TimelineItem) {
  selected.value = null;
  selectedSource.value = null;
  sourceUnavailable.value = false;
  try {
    selected.value = await api.request<TimelineItem>(`/api/v1/admin/timeline/${item.id}`);
    try {
      selectedSource.value = await api.request<TimelineSource>(
        `/api/v1/admin/timeline/${item.id}/source`,
      );
    } catch {
      sourceUnavailable.value = true;
    }
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载时间线详情失败", true);
  }
}

onMounted(async () => {
  await resolveDefaultUser();
  if (userId.value) await runQuery();
});
</script>

<template>
  <section class="content">
    <div class="intro panel">
      <div>
        <h2>历史时间线</h2>
        <p class="hint">这里查看“过去发生过什么”。长期稳定事实请到记忆库管理；Timeline 只保存可回溯索引，Source 才是原始证据。</p>
      </div>
      <el-tag type="info">Timeline → Source</el-tag>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>检索条件</h2>
        <div class="quick-range">
          <el-button size="small" @click="setRecent(24)">最近 24 小时</el-button>
          <el-button size="small" @click="setRecent(24 * 7)">最近 7 天</el-button>
          <el-button size="small" @click="clearFilters">清空条件</el-button>
        </div>
      </div>

      <div class="filters primary-filters">
        <el-input v-model="userId" placeholder="user_id" clearable />
        <el-input v-model="query" placeholder="主题 / 关键词，例如：星际穿越" clearable @keyup.enter="startQuery" />
        <el-date-picker
          v-model="timeRange"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          :clearable="true"
        />
        <el-button type="primary" :loading="loading" @click="startQuery">查询</el-button>
      </div>

      <div class="filters secondary-filters">
        <el-select v-model="privacy" placeholder="可见隐私">
          <el-option label="L0/L1" value="L1" />
          <el-option label="L0/L1/L2（管理员）" value="L2" />
        </el-select>
        <el-select v-model="actor" placeholder="全部 Actor" clearable>
          <el-option v-for="value in actorOptions" :key="value" :label="actorLabels[value] ?? value" :value="value" />
        </el-select>
        <el-select v-model="sourceType" placeholder="全部来源" clearable>
          <el-option v-for="value in sourceOptions" :key="value" :label="sourceLabels[value] ?? value" :value="value" />
        </el-select>
        <el-input v-model="eventType" placeholder="event_type，例如 conversation.message" clearable />
        <el-input class="conversation-filter" v-model="conversationId" placeholder="conversation_id（可选）" clearable />
        <el-select v-model="limit" placeholder="每页数量" @change="startQuery">
          <el-option v-for="value in [20, 50, 100]" :key="value" :label="`${value} 条/页`" :value="value" />
        </el-select>
      </div>

      <p class="hint">{{ resultText }}</p>
    </div>

    <div class="panel timeline-panel">
      <el-table v-loading="loading" :data="events" empty-text="没有匹配的历史事件" style="width:100%">
        <el-table-column label="发生时间" min-width="170">
          <template #default="{ row }">{{ fmt(row.occurred_at) }}</template>
        </el-table-column>
        <el-table-column label="Actor" width="100">
          <template #default="{ row }"><el-tag size="small" effect="plain">{{ actorLabels[row.actor] ?? row.actor }}</el-tag></template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }">{{ sourceLabels[row.source_type] ?? row.source_type }}</template>
        </el-table-column>
        <el-table-column prop="event_type" label="事件类型" min-width="190" show-overflow-tooltip />
        <el-table-column label="摘要" min-width="320" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.summary }}</span>
            <small v-if="row.privacy_level === 'L2'">L2 仅索引壳，正文需受控下钻 Source</small>
          </template>
        </el-table-column>
        <el-table-column label="隐私" width="80">
          <template #default="{ row }"><el-tag :type="row.privacy_level === 'L2' ? 'warning' : 'info'" size="small">{{ row.privacy_level }}</el-tag></template>
        </el-table-column>
        <el-table-column label="重要性" width="90">
          <template #default="{ row }">{{ row.importance.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }"><el-button size="small" @click="showDetail(row as TimelineItem)">证据</el-button></template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="limit"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onPageChange"
          @size-change="startQuery"
        />
      </div>
    </div>

    <el-dialog :model-value="!!selected" width="760px" :title="selected ? `Timeline #${selected.id}` : 'Timeline 详情'" @update:model-value="value => { if (!value) selected = null }">
      <template v-if="selected">
        <dl class="detail-grid">
          <dt>发生时间</dt><dd>{{ fmt(selected.occurred_at) }}<template v-if="selected.ended_at"> ～ {{ fmt(selected.ended_at) }}</template></dd>
          <dt>Actor</dt><dd>{{ actorLabels[selected.actor] ?? selected.actor }}</dd>
          <dt>事件类型</dt><dd><code>{{ selected.event_type }}</code></dd>
          <dt>来源</dt><dd><code>{{ selected.source_type }}:{{ selected.source_id }}</code></dd>
          <dt>会话</dt><dd>{{ selected.conversation_id ?? "—" }}</dd>
          <dt>隐私</dt><dd>{{ selected.privacy_level }}</dd>
          <dt>摘要</dt><dd>{{ selected.summary }}</dd>
          <dt>关键词</dt><dd>{{ selected.keywords.length ? selected.keywords.join(" · ") : "—" }}</dd>
        </dl>

        <div class="source-card" :class="{ private: selected.privacy_level === 'L2' }">
          <div class="source-head">
            <h3>Source 原始证据</h3>
            <el-tag v-if="selected.privacy_level === 'L2'" type="warning" size="small">L2</el-tag>
          </div>
          <pre v-if="selectedSource">{{ selectedSource.text }}</pre>
          <p v-else-if="sourceUnavailable" class="hint">当前来源不可展开、已被删除，或该 source_type 尚未接入 Source Expansion。</p>
          <p v-else class="hint">正在读取 Source…</p>
        </div>

        <details>
          <summary>索引元数据</summary>
          <pre>{{ pretty({ entities: selected.entities, metadata: selected.metadata, created_at: selected.created_at }) }}</pre>
        </details>
      </template>
      <template #footer><el-button @click="selected = null">关闭</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; }
.intro { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.panel h2, .panel h3 { margin: 0; font-size: 15px; }
.intro p { margin-top: 6px; }
.panel-head, .source-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.hint { color: var(--muted); font-size: 12px; margin: 0; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.quick-range { display: flex; gap: 6px; flex-wrap: wrap; }
.filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.primary-filters > :nth-child(1) { width: 250px; }
.primary-filters > :nth-child(2) { flex: 1; min-width: 260px; }
.primary-filters :deep(.el-date-editor) { width: 390px; }
.secondary-filters :deep(.el-select) { width: 155px; }
.secondary-filters :deep(.el-input) { width: 230px; }
.secondary-filters .conversation-filter { width: 280px; }
.timeline-panel { min-width: 0; }
small { color: var(--muted); display: block; font-size: 11px; margin-top: 3px; }
.detail-grid { display: grid; grid-template-columns: 90px 1fr; gap: 8px 14px; margin: 0; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.source-card { border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: grid; gap: 8px; }
.source-card.private { border-color: #e8c57d; background: #fff9ed; }
pre { margin: 0; padding: 10px; border-radius: 8px; background: #f5f7fb; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
details { font-size: 12px; color: var(--muted); }
details summary { cursor: pointer; margin-bottom: 8px; }

@media (max-width: 900px) {
  .intro { display: grid; }
  .primary-filters :deep(.el-date-editor) { width: 100%; }
  .primary-filters > :nth-child(1), .primary-filters > :nth-child(2) { width: 100%; min-width: 0; }
}
</style>
