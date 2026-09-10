<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";

const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "library" });
import { ElMessageBox } from "element-plus";

interface MemoryItem {
  id: number;
  user_id: string;
  subject: string;
  subject_key: string;
  fact_key: string | null;
  origin_kind: string;
  type: string;
  content: string;
  summary: string | null;
  privacy_level: string;
  importance: number;
  pin: boolean;
  status: string;
  confidence: number | null;
  valid_from: string | null;
  valid_to: string | null;
  superseded_by: number | null;
  supersede_reason: string | null;
  conflict_with: number | null;
  extractor_version: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  last_accessed_at: string | null;
  access_count: number;
}
interface MemoryDetail extends MemoryItem {
  sources: Array<{ source_kind: string; source_id: string; excerpt_hash: string | null }>;
  lineage: MemoryItem[];
}
interface HitItem {
  memory: MemoryItem;
  vector_score: number;
  lexical_score: number;
  final_score: number;
  reasons: string[];
}
interface LedgerItem {
  id: number;
  entity_kind: string;
  entity_id: string;
  deleted_ids: number[];
  requested_by: string;
  reason: string | null;
  created_at: string;
}

// 记忆库治理：统计、过滤列表、溯源详情、纠错编辑、冲突裁决、
// 手动添加、检索调试与删除台账（含重放试运行/执行）。
// 所有破坏性操作（删除/裁决/重放）都有确认框二次确认。
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const types = ["semantic", "preference", "commitment", "episodic", "emotional"];
const statuses = ["active", "conflict", "archived", "superseded"];
const subjects = ["user", "assistant", "shared"];
const subjectLabels: Record<string, string> = {
  user: "关于用户",
  assistant: "关于助手",
  shared: "关于双方",
};
const originLabels: Record<string, string> = {
  user_statement: "用户陈述",
  assistant_statement: "助手自述",
  shared_turn: "共同回合",
  system_event: "系统事件",
  manual: "手动添加",
};
const typeLabels: Record<string, string> = {
  semantic: "语义",
  preference: "偏好",
  commitment: "承诺",
  episodic: "情景",
  emotional: "情感",
};
const statusLabels: Record<string, string> = {
  active: "活跃",
  conflict: "冲突",
  archived: "已归档",
  superseded: "已废止",
};

const memories = ref<MemoryItem[]>([]);
const ledger = ref<LedgerItem[]>([]);
const stats = reactive({ active: 0, conflict: 0, archived: 0, deleted: 0 });
const filters = reactive({ subject: "", type: "", status: "", factKey: "", minImportance: "" });
const memoryPage = ref(1);
const memoryPageSize = ref(20);
const memoryTotal = ref(0);
const ledgerPage = ref(1);
const ledgerPageSize = ref(20);
const ledgerTotal = ref(0);
const fallbackUser = ref("");

const detail = ref<MemoryDetail | null>(null);
const editing = ref<MemoryItem | null>(null);
const editForm = ref({ content: "", summary: "", importance: 0.6, pin: false, reason: "" });
const adding = ref(false);
const addForm = reactive({
  user_id: "",
  subject: "user",
  subject_key: "",
  fact_key: "",
  type: "semantic",
  privacy_level: "L1",
  importance: 0.6,
  content: "",
  pin: false,
});
const queryForm = reactive({ text: "", privacy: "L1", user: "" });
const queryResult = ref<{ policy_version: string; candidate_count: number; hits: HitItem[] } | null>(null);

const memoryParams = computed(() => {
  const params = new URLSearchParams();
  if (filters.subject) params.set("subject", filters.subject);
  if (filters.type) params.set("type", filters.type);
  if (filters.status) params.set("status", filters.status);
  if (filters.factKey.trim()) params.set("fact_key", filters.factKey.trim());
  if (filters.minImportance) params.set("min_importance", filters.minImportance);
  params.set("limit", String(memoryPageSize.value));
  params.set("offset", String((memoryPage.value - 1) * memoryPageSize.value));
  return params.toString();
});

const fmt = (value: string | null) => (value ? new Date(value).toLocaleString() : "—");

/** 并行拉取列表、四类统计与台账；统计读 total 不再整表下载 */
async function load() {
  try {
    const [page, active, conflict, archived, ledgerPageData] = await Promise.all([
      api.request<{ items: MemoryItem[]; total: number }>(`/api/v1/admin/memories?${memoryParams.value}`),
      api.request<{ items: MemoryItem[]; total: number }>("/api/v1/admin/memories?status=active&limit=1"),
      api.request<{ items: MemoryItem[]; total: number }>("/api/v1/admin/memories?status=conflict&limit=1"),
      api.request<{ items: MemoryItem[]; total: number }>("/api/v1/admin/memories?status=archived&limit=1"),
      api.request<{ items: LedgerItem[]; total: number }>(
        `/api/v1/admin/deletion-ledger?limit=${ledgerPageSize.value}`
          + `&offset=${(ledgerPage.value - 1) * ledgerPageSize.value}`,
      ),
    ]);
    memories.value = page.items;
    memoryTotal.value = page.total;
    if (active.items[0]) fallbackUser.value = active.items[0].user_id;
    stats.active = active.total;
    stats.conflict = conflict.total;
    stats.archived = archived.total;
    stats.deleted = ledgerPageData.total;
    ledger.value = ledgerPageData.items;
    ledgerTotal.value = ledgerPageData.total;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
}

function onMemoryPageChange() {
  void load();
}

function onLedgerPageChange() {
  void load();
}

function applyFilters() {
  memoryPage.value = 1;
  void load();
}

async function showDetail(id: number) {
  try {
    detail.value = await api.request<MemoryDetail>(`/api/v1/admin/memories/${id}`);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
}

function openEdit(item: MemoryItem) {
  editing.value = item;
  editForm.value.content = item.content;
  editForm.value.summary = item.summary ?? "";
  editForm.value.importance = item.importance;
  editForm.value.pin = item.pin;
  editForm.value.reason = "";
}

async function submitEdit() {
  if (!editing.value) return;
  const body: Record<string, unknown> = {
    content: editForm.value.content,
    reason: editForm.value.reason,
    pin: editForm.value.pin,
  };
  if (editForm.value.summary) body.summary = editForm.value.summary;
  if (editForm.value.importance !== editing.value.importance) body.importance = editForm.value.importance;
  try {
    const updated = await api.request<MemoryItem>(
      `/api/v1/admin/memories/${editing.value.id}`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
    emit("status", `已生成替代版本 #${updated.id}（原 #${editing.value.id} → superseded）`);
    editing.value = null;
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "编辑失败", true);
  }
}

async function submitAdd() {
  const body: Record<string, unknown> = {
    user_id: addForm.user_id || fallbackUser.value,
    subject: addForm.subject,
    type: addForm.type,
    privacy_level: addForm.privacy_level,
    importance: addForm.importance,
    content: addForm.content,
    pin: addForm.pin,
  };
  if (addForm.subject_key.trim()) body.subject_key = addForm.subject_key.trim();
  if (addForm.fact_key.trim()) body.fact_key = addForm.fact_key.trim();
  try {
    const created = await api.request<MemoryItem>("/api/v1/admin/memories", {
      method: "POST",
      body: JSON.stringify(body),
    });
    emit("status", `${subjectLabels[created.subject] ?? created.subject}记忆 #${created.id} 已添加`);
    adding.value = false;
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "添加失败", true);
  }
}

async function archive(id: number) {
  try {
    await ElMessageBox.confirm(`归档记忆 #${id}？归档后不再参与检索。`, "确认归档", { type: "warning", confirmButtonText: "归档", cancelButtonText: "取消" });
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/memories/${id}/archive`, { method: "POST" });
    emit("status", `记忆 #${id} 已归档`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "操作失败", true);
  }
}

async function resolve(id: number, action: "adopt" | "keep") {
  const label = action === "adopt" ? "采纳新记忆（旧版本转入已废止）" : "保留旧值（新记忆归档）";
  try {
    await ElMessageBox.confirm(`冲突裁决 #${id}：${label}？`, "确认裁决", { type: "warning", confirmButtonText: "确认", cancelButtonText: "取消" });
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/memories/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    emit("status", `冲突 #${id} 已裁决`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "操作失败", true);
  }
}

async function remove(id: number) {
  let reason = "";
  try {
    const result = await ElMessageBox.prompt(`硬删除记忆 #${id} 及其整个版本链，不可撤销并记入台账。`, "删除记忆", {
      type: "error",
      inputPlaceholder: "请输入删除原因",
      inputValidator: (value) => value.trim().length >= 2 || "请填写删除原因",
      confirmButtonText: "确认删除",
      cancelButtonText: "取消",
    });
    reason = result.value;
  } catch {
    return;
  }
  try {
    const receipt = await api.request<{ ledger_id: number; deleted_ids: number[] }>(
      `/api/v1/admin/memories/${id}?reason=${encodeURIComponent(reason)}`,
      { method: "DELETE" },
    );
    emit("status", `已删除 ${receipt.deleted_ids.length} 个版本（台账 #${receipt.ledger_id}）`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "删除失败", true);
  }
}

async function runQuery() {
  try {
    queryResult.value = await api.request<{
      policy_version: string;
      candidate_count: number;
      hits: HitItem[];
    }>("/api/v1/admin/memories/query", {
      method: "POST",
      body: JSON.stringify({
        user_id: queryForm.user || fallbackUser.value,
        query: queryForm.text,
        privacy_level: queryForm.privacy,
      }),
    });
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "检索失败", true);
  }
}

async function replayLedger(dryRun: boolean) {
  const label = dryRun ? "试运行" : "执行";
  if (!dryRun) {
    try {
      await ElMessageBox.confirm("重放删除台账？将清除备份恢复后复活的数据。", "确认重放", { type: "warning", confirmButtonText: "执行", cancelButtonText: "取消" });
    } catch {
      return;
    }
  }
  try {
    const report = await api.request<{
      ledger_rows: number;
      conversations_deleted: number;
      memories_deleted: number;
      dry_run: boolean;
    }>("/api/v1/admin/deletion-ledger/replay", {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun }),
    });
    emit(
      "status",
      `台账重放（${label}）：${report.ledger_rows} 行，清除会话 ${report.conversations_deleted}、记忆 ${report.memories_deleted}`,
    );
    if (!dryRun) await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "重放失败", true);
  }
}

onMounted(load);
</script>

<template>
  <section class="content">
    <div class="stats">
      <article><span>活跃记忆</span><strong>{{ stats.active }}</strong></article>
      <article><span>待裁决冲突</span><strong>{{ stats.conflict }}</strong></article>
      <article><span>已归档</span><strong>{{ stats.archived }}</strong></article>
      <article><span>删除台账</span><strong>{{ stats.deleted }}</strong></article>
    </div>

    <div v-if="props.mode === 'library'" class="panel">
      <h2>检索调试</h2>
      <div class="query-row">
        <el-input v-model="queryForm.text" placeholder="例如：帮我点菜，我能吃香菜吗" @keyup.enter="runQuery" />
        <el-select v-model="queryForm.privacy"><el-option label="L0" value="L0" /><el-option label="L1" value="L1" /><el-option label="L2（本地）" value="L2" /></el-select>
        <el-input v-model="queryForm.user" placeholder="user_id（留空自动）" />
        <el-button type="primary" @click="runQuery">检索</el-button>
      </div>
      <div v-if="queryResult" class="query-result">
        <p class="hint">{{ queryResult.candidate_count }} 个候选 · {{ queryResult.policy_version }} · 命中 {{ queryResult.hits.length }} 条</p>
        <div v-for="hit in queryResult.hits" :key="hit.memory.id" class="hit">
          <span class="score">{{ hit.final_score.toFixed(3) }}</span>
          <span>
            <strong>#{{ hit.memory.id }}</strong>
            [{{ subjectLabels[hit.memory.subject] ?? hit.memory.subject }}]
            [{{ typeLabels[hit.memory.type] ?? hit.memory.type }}]
            <code v-if="hit.memory.fact_key">{{ hit.memory.fact_key }}</code>
            {{ hit.memory.content }}
            <small>vec {{ hit.vector_score.toFixed(2) }} · lex {{ hit.lexical_score.toFixed(2) }} · {{ hit.reasons.join("+") }}</small>
          </span>
        </div>
        <p v-if="!queryResult.hits.length" class="hint">没有命中。</p>
      </div>
    </div>

    <div v-if="props.mode === 'library'" class="panel">
      <div class="panel-head">
        <h2>记忆列表</h2>
        <el-button type="primary" @click="adding = true">手动添加</el-button>
      </div>
      <el-radio-group v-model="filters.subject" class="subject-switch" @change="applyFilters">
        <el-radio-button value="">全部主体</el-radio-button>
        <el-radio-button v-for="subject in subjects" :key="subject" :value="subject">
          {{ subjectLabels[subject] }}
        </el-radio-button>
      </el-radio-group>
      <div class="filters">
        <el-select v-model="filters.type" placeholder="全部类型" clearable><el-option v-for="t in types" :key="t" :label="typeLabels[t] ?? t" :value="t" /></el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable><el-option v-for="s in statuses" :key="s" :label="statusLabels[s] ?? s" :value="s" /></el-select>
        <el-input v-model="filters.factKey" placeholder="fact_key" clearable />
        <el-input v-model="filters.minImportance" placeholder="最低重要性" />
        <el-button @click="applyFilters">应用过滤</el-button>
      </div>
      <el-table :data="memories" empty-text="没有匹配的记忆" style="width:100%">
        <el-table-column label="ID" width="100"><template #default="{ row }"><strong>#{{ row.id }}</strong><small v-if="row.superseded_by">→ #{{ row.superseded_by }}</small><small v-if="row.conflict_with">⚠ vs #{{ row.conflict_with }}</small></template></el-table-column>
        <el-table-column label="主体" width="110"><template #default="{ row }"><el-tag size="small" effect="plain">{{ subjectLabels[row.subject] ?? row.subject }}</el-tag><small>{{ row.subject_key }}</small></template></el-table-column>
        <el-table-column label="类型" width="110"><template #default="{ row }">{{ typeLabels[row.type] ?? row.type }}{{ row.pin ? ' 📌' : '' }}</template></el-table-column>
        <el-table-column prop="content" label="内容" min-width="320" show-overflow-tooltip />
        <el-table-column label="事实槽位" min-width="160"><template #default="{ row }"><code>{{ row.fact_key ?? '—' }}</code><small>{{ originLabels[row.origin_kind] ?? row.origin_kind }}</small></template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : row.status === 'conflict' ? 'warning' : 'info'">{{ statusLabels[row.status] ?? row.status }}</el-tag><small>{{ row.privacy_level }}</small></template></el-table-column>
        <el-table-column label="重要性" width="90"><template #default="{ row }">{{ row.importance.toFixed(2) }}</template></el-table-column>
        <el-table-column prop="access_count" label="访问" width="75" />
        <el-table-column label="操作" min-width="320" fixed="right"><template #default="{ row }"><div class="actions"><el-button size="small" @click="showDetail(row.id)">溯源</el-button><el-button v-if="row.status === 'active' || row.status === 'conflict'" size="small" @click="openEdit(row as MemoryItem)">编辑</el-button><el-button v-if="row.status === 'active'" size="small" @click="archive(row.id)">归档</el-button><template v-if="row.status === 'conflict'"><el-button type="primary" size="small" @click="resolve(row.id, 'adopt')">采纳</el-button><el-button size="small" @click="resolve(row.id, 'keep')">保留旧值</el-button></template><el-button type="danger" plain size="small" @click="remove(row.id)">删除</el-button></div></template></el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="memoryPage"
          v-model:page-size="memoryPageSize"
          :total="memoryTotal"
          :page-sizes="[20, 50, 100, 200]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onMemoryPageChange"
          @size-change="onMemoryPageChange"
        />
      </div>
    </div>

    <div v-if="props.mode === 'deletion'" class="panel">
      <div class="panel-head">
        <h2>删除台账</h2>
        <div class="row">
          <el-button @click="replayLedger(true)">重放试运行</el-button>
          <el-button type="warning" plain @click="replayLedger(false)">执行重放</el-button>
        </div>
      </div>
      <el-table :data="ledger" empty-text="暂无删除记录" style="width:100%">
        <el-table-column label="台账" width="90"><template #default="{ row }"><strong>#{{ row.id }}</strong></template></el-table-column>
        <el-table-column label="实体" min-width="150"><template #default="{ row }">{{ row.entity_kind }} #{{ row.entity_id.slice(0, 8) }}</template></el-table-column>
        <el-table-column label="被删版本" min-width="180"><template #default="{ row }">{{ row.deleted_ids.map((id: number) => `#${id}`).join(' ') }}</template></el-table-column>
        <el-table-column prop="requested_by" label="操作人" min-width="130" />
        <el-table-column label="原因" min-width="180"><template #default="{ row }">{{ row.reason ?? '—' }}</template></el-table-column>
        <el-table-column label="时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="ledgerPage"
          v-model:page-size="ledgerPageSize"
          :total="ledgerTotal"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onLedgerPageChange"
          @size-change="onLedgerPageChange"
        />
      </div>
    </div>

    <el-dialog :model-value="!!detail" width="680px" :title="detail ? `记忆 #${detail.id}` : '记忆详情'" @update:model-value="value => { if (!value) detail = null }">
      <template v-if="detail">
        <h3>记忆 #{{ detail.id }}（{{ typeLabels[detail.type] ?? detail.type }} · {{ detail.privacy_level }}）</h3>
        <dl class="detail-grid">
          <dt>内容</dt><dd>{{ detail.content }}</dd>
          <dt>摘要</dt><dd>{{ detail.summary ?? "—" }}</dd>
          <dt>主体</dt><dd>{{ subjectLabels[detail.subject] ?? detail.subject }} · <code>{{ detail.subject_key }}</code></dd>
          <dt>事实槽位</dt><dd><code>{{ detail.fact_key ?? "—" }}</code></dd>
          <dt>来源语义</dt><dd>{{ originLabels[detail.origin_kind] ?? detail.origin_kind }}</dd>
          <dt>状态</dt><dd>{{ statusLabels[detail.status] ?? detail.status }}{{ detail.superseded_by ? ` → #${detail.superseded_by}` : "" }}{{ detail.supersede_reason ? `（${detail.supersede_reason}）` : "" }}</dd>
          <dt>有效期</dt><dd>{{ fmt(detail.valid_from) }} ～ {{ detail.valid_to ? fmt(detail.valid_to) : "永久" }}</dd>
          <dt>提取器</dt><dd>{{ detail.extractor_version ?? "—" }} · 置信度 {{ detail.confidence ?? "—" }}</dd>
          <dt>创建</dt><dd>{{ fmt(detail.created_at) }} · {{ detail.created_by }}</dd>
          <dt>访问</dt><dd>{{ detail.access_count }} 次 · 最近 {{ fmt(detail.last_accessed_at) }}</dd>
        </dl>
        <h4>来源（{{ detail.sources.length }}）</h4>
        <div v-for="source in detail.sources" :key="source.source_id" class="source-row">
          <code>{{ source.source_kind }}:{{ source.source_id.slice(0, 13) }}…</code>
        </div>
        <h4>版本链（{{ detail.lineage.length }}）</h4>
        <div v-for="entry in detail.lineage" :key="entry.id" class="source-row">
          #{{ entry.id }} <span class="status" :class="entry.status">{{ statusLabels[entry.status] ?? entry.status }}</span>
          [{{ subjectLabels[entry.subject] ?? entry.subject }}]<code v-if="entry.fact_key"> {{ entry.fact_key }}</code> {{ entry.content }}
        </div>
      </template>
      <template #footer><el-button @click="detail = null">关闭</el-button></template>
    </el-dialog>

    <el-dialog :model-value="!!editing" width="620px" title="纠错编辑" @update:model-value="value => { if (!value) editing = null }">
      <template v-if="editing">
        <h3>纠错编辑 #{{ editing.id }}</h3>
        <p class="hint">保存后生成替代版本，原版本转入 superseded 并保留溯源。</p>
        <label>内容<el-input v-model="editForm.content" type="textarea" :rows="4" /></label>
        <label>摘要<el-input v-model="editForm.summary" /></label>
        <div class="row">
          <label>重要性<el-input-number v-model="editForm.importance" :min="0" :max="1" :step="0.05" /></label>
          <el-checkbox v-model="editForm.pin">置顶</el-checkbox>
        </div>
        <label>原因（必填）<el-input v-model="editForm.reason" /></label>
      </template>
      <template #footer><el-button @click="editing = null">取消</el-button><el-button type="primary" :disabled="!editForm.reason.trim()" @click="submitEdit">保存替代版本</el-button></template>
    </el-dialog>

    <el-dialog v-model="adding" width="620px" title="手动添加记忆">
        <h3>手动添加记忆</h3>
        <label>用户 ID<el-input v-model="addForm.user_id" :placeholder="fallbackUser || 'app_user UUID'" /></label>
        <div class="row">
          <label>主体<el-select v-model="addForm.subject"><el-option v-for="subject in subjects" :key="subject" :label="subjectLabels[subject]" :value="subject" /></el-select></label>
          <label>类型<el-select v-model="addForm.type"><el-option v-for="t in types" :key="t" :label="typeLabels[t] ?? t" :value="t" /></el-select></label>
          <label>隐私<el-select v-model="addForm.privacy_level"><el-option v-for="level in ['L0','L1','L2']" :key="level" :label="level" :value="level" /></el-select></label>
        </div>
        <div class="row">
          <label>subject_key（可选）<el-input v-model="addForm.subject_key" placeholder="留空使用主体默认值" /></label>
          <label>fact_key（可选）<el-input v-model="addForm.fact_key" placeholder="如 profile.height" /></label>
          <label>重要性<el-input-number v-model="addForm.importance" :min="0" :max="1" :step="0.05" /></label>
        </div>
        <label>内容<el-input v-model="addForm.content" type="textarea" :rows="3" /></label>
        <el-checkbox v-model="addForm.pin">置顶</el-checkbox>
      <template #footer><el-button @click="adding = false">取消</el-button><el-button type="primary" :disabled="addForm.content.trim().length < 2" @click="submitAdd">添加</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stats article { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 18px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; }
.panel h2, .panel h3 { margin: 0; font-size: 15px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.hint { color: var(--muted); font-size: 12px; margin: 0; }
.row { display: flex; gap: 10px; align-items: end; }
.query-row { display: flex; gap: 8px; flex-wrap: wrap; }
.query-row > :first-child { flex: 1; min-width: 260px; }
.query-row :deep(.el-select) { width: 140px; }
.query-result .hit { display: flex; gap: 10px; padding: 5px 0; border-bottom: 1px dashed #232736; font-size: 13px; }
.score { color: var(--accent); min-width: 46px; font-variant-numeric: tabular-nums; }
.filters { display: flex; gap: 8px; flex-wrap: wrap; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.subject-switch { justify-self: start; }
.filters :deep(.el-select) { width: 150px; }
.filters :deep(.el-input) { width: 150px; }
small { color: var(--muted); display: block; font-size: 11px; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; border: 1px solid; }
.status.active { color: #168e59; border-color: #bfe8d2; background: #e9f8f0; }
.status.conflict { color: #f2c078; border-color: #6b5325; background: #2a2113; }
.status.archived { color: #68748a; border-color: #d8deea; background: #f1f3f7; }
.status.superseded { color: #6f63a8; border-color: #dcd7f2; background: #f1effb; }
:deep(.el-dialog__body) { display:grid; gap:12px; color:var(--text); }
:deep(.el-dialog__body > label) { display:grid; gap:6px; color:var(--muted); font-size:12px; }
:deep(.el-dialog__body .row > label) { min-width:0; flex:1; display:grid; gap:6px; color:var(--muted); font-size:12px; }
:deep(.el-dialog__body .el-select), :deep(.el-dialog__body .el-input-number) { width:100%; }
.detail-grid { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; font-size: 13px; margin: 0; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; word-break: break-all; }
.source-row { font-size: 12px; padding: 3px 0; }
.source-row code { color: #c9cfe6; }
</style>
