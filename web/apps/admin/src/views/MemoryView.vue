<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";

interface MemoryItem {
  id: number;
  user_id: string;
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

const memories = ref<MemoryItem[]>([]);
const ledger = ref<LedgerItem[]>([]);
const stats = reactive({ active: 0, conflict: 0, archived: 0, deleted: 0 });
const filters = reactive({ type: "", status: "", minImportance: "", limit: "50" });
const fallbackUser = ref("");

const detail = ref<MemoryDetail | null>(null);
const editing = ref<MemoryItem | null>(null);
const editForm = reactive({ content: "", summary: "", importance: 0.6, pin: false, reason: "" });
const adding = ref(false);
const addForm = reactive({ user_id: "", type: "semantic", privacy_level: "L1", importance: 0.6, content: "", pin: false });
const queryForm = reactive({ text: "", privacy: "L1", user: "" });
const queryResult = ref<{ policy_version: string; candidate_count: number; hits: HitItem[] } | null>(null);

const memoryParams = computed(() => {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  if (filters.status) params.set("status", filters.status);
  if (filters.minImportance) params.set("min_importance", filters.minImportance);
  params.set("limit", filters.limit);
  return params.toString();
});

const fmt = (value: string | null) => (value ? new Date(value).toLocaleString() : "—");

/** 并行拉取列表、四类统计与台账；有活跃记忆时记住默认 user_id */
async function load() {
  try {
    const [items, active, conflict, archived, ledgerRows] = await Promise.all([
      api.request<MemoryItem[]>(`/api/v1/admin/memories?${memoryParams.value}`),
      api.request<MemoryItem[]>("/api/v1/admin/memories?status=active&limit=200"),
      api.request<MemoryItem[]>("/api/v1/admin/memories?status=conflict&limit=200"),
      api.request<MemoryItem[]>("/api/v1/admin/memories?status=archived&limit=200"),
      api.request<LedgerItem[]>("/api/v1/admin/deletion-ledger?limit=200"),
    ]);
    memories.value = items;
    if (active[0]) fallbackUser.value = active[0].user_id;
    stats.active = active.length;
    stats.conflict = conflict.length;
    stats.archived = archived.length;
    stats.deleted = ledgerRows.length;
    ledger.value = ledgerRows;
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
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
  editForm.content = item.content;
  editForm.summary = item.summary ?? "";
  editForm.importance = item.importance;
  editForm.pin = item.pin;
  editForm.reason = "";
}

async function submitEdit() {
  if (!editing.value) return;
  const body: Record<string, unknown> = {
    content: editForm.content,
    reason: editForm.reason,
    pin: editForm.pin,
  };
  if (editForm.summary) body.summary = editForm.summary;
  if (editForm.importance !== editing.value.importance) body.importance = editForm.importance;
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
  try {
    const created = await api.request<MemoryItem>("/api/v1/admin/memories", {
      method: "POST",
      body: JSON.stringify({
        user_id: addForm.user_id || fallbackUser,
        type: addForm.type,
        privacy_level: addForm.privacy_level,
        importance: addForm.importance,
        content: addForm.content,
        pin: addForm.pin,
      }),
    });
    emit("status", `记忆 #${created.id} 已添加`);
    adding.value = false;
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "添加失败", true);
  }
}

async function archive(id: number) {
  if (!confirm(`归档记忆 #${id}？归档后不再参与检索。`)) return;
  try {
    await api.request(`/api/v1/admin/memories/${id}/archive`, { method: "POST" });
    emit("status", `记忆 #${id} 已归档`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "操作失败", true);
  }
}

async function resolve(id: number, action: "adopt" | "keep") {
  const label = action === "adopt" ? "采纳新记忆（旧版本转入 superseded）" : "保留旧值（新记忆归档）";
  if (!confirm(`冲突裁决 #${id}：${label}？`)) return;
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
  const reason = prompt(`硬删除记忆 #${id} 及其整个版本链，不可撤销并记入台账。\n请输入删除原因：`);
  if (reason === null) return;
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
        user_id: queryForm.user || fallbackUser,
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
  if (!dryRun && !confirm("重放删除台账？将清除备份恢复后复活的数据。")) return;
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

    <div class="panel">
      <h2>检索调试</h2>
      <form class="query-row" @submit.prevent="runQuery">
        <input v-model="queryForm.text" placeholder="例如：帮我点菜，我能吃香菜吗" required />
        <select v-model="queryForm.privacy">
          <option value="L0">L0</option><option value="L1">L1</option><option value="L2">L2（本地）</option>
        </select>
        <input v-model="queryForm.user" placeholder="user_id（留空自动）" />
        <button class="primary" type="submit">检索</button>
      </form>
      <div v-if="queryResult" class="query-result">
        <p class="hint">{{ queryResult.candidate_count }} 个候选 · {{ queryResult.policy_version }} · 命中 {{ queryResult.hits.length }} 条</p>
        <div v-for="hit in queryResult.hits" :key="hit.memory.id" class="hit">
          <span class="score">{{ hit.final_score.toFixed(3) }}</span>
          <span>
            <strong>#{{ hit.memory.id }}</strong> [{{ hit.memory.type }}] {{ hit.memory.content }}
            <small>vec {{ hit.vector_score.toFixed(2) }} · lex {{ hit.lexical_score.toFixed(2) }} · {{ hit.reasons.join("+") }}</small>
          </span>
        </div>
        <p v-if="!queryResult.hits.length" class="hint">没有命中。</p>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>记忆列表</h2>
        <button class="primary" @click="adding = true">手动添加</button>
      </div>
      <div class="filters">
        <select v-model="filters.type"><option value="">全部类型</option><option v-for="t in types" :key="t">{{ t }}</option></select>
        <select v-model="filters.status"><option value="">全部状态</option><option v-for="s in statuses" :key="s">{{ s }}</option></select>
        <input v-model="filters.minImportance" type="number" min="0" max="1" step="0.1" placeholder="最低重要性" />
        <select v-model="filters.limit"><option>25</option><option>50</option><option>100</option><option>200</option></select>
        <button @click="load">应用过滤</button>
      </div>
      <table>
        <thead><tr><th>ID</th><th>类型</th><th>内容</th><th>状态</th><th>重要性</th><th>访问</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in memories" :key="item.id">
            <td>
              <strong>#{{ item.id }}</strong>
              <small v-if="item.superseded_by">→ #{{ item.superseded_by }}</small>
              <small v-if="item.conflict_with">⚠ vs #{{ item.conflict_with }}</small>
            </td>
            <td>{{ item.type }}{{ item.pin ? " 📌" : "" }}</td>
            <td class="content-cell">{{ item.content }}</td>
            <td><span class="status" :class="item.status">{{ item.status }}</span><small>{{ item.privacy_level }}</small></td>
            <td>{{ item.importance.toFixed(2) }}</td>
            <td>{{ item.access_count }}</td>
            <td class="actions">
              <button @click="showDetail(item.id)">溯源</button>
              <button v-if="item.status === 'active' || item.status === 'conflict'" @click="openEdit(item)">编辑</button>
              <button v-if="item.status === 'active'" @click="archive(item.id)">归档</button>
              <template v-if="item.status === 'conflict'">
                <button class="primary" @click="resolve(item.id, 'adopt')">采纳</button>
                <button @click="resolve(item.id, 'keep')">保留旧值</button>
              </template>
              <button class="danger" @click="remove(item.id)">删除</button>
            </td>
          </tr>
          <tr v-if="!memories.length"><td colspan="7" class="hint">没有匹配的记忆。</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>删除台账</h2>
        <div class="row">
          <button @click="replayLedger(true)">重放试运行</button>
          <button @click="replayLedger(false)">执行重放</button>
        </div>
      </div>
      <table>
        <thead><tr><th>台账</th><th>实体</th><th>被删版本</th><th>操作人</th><th>原因</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="item in ledger" :key="item.id">
            <td><strong>#{{ item.id }}</strong></td>
            <td>{{ item.entity_kind }} #{{ item.entity_id.slice(0, 8) }}</td>
            <td>{{ item.deleted_ids.map((id) => `#${id}`).join(" ") }}</td>
            <td>{{ item.requested_by }}</td>
            <td>{{ item.reason ?? "—" }}</td>
            <td><small>{{ fmt(item.created_at) }}</small></td>
          </tr>
          <tr v-if="!ledger.length"><td colspan="6" class="hint">暂无删除记录。</td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="detail" class="overlay" @click.self="detail = null">
      <div class="dialog">
        <h3>记忆 #{{ detail.id }}（{{ detail.type }} · {{ detail.privacy_level }}）</h3>
        <dl class="detail-grid">
          <dt>内容</dt><dd>{{ detail.content }}</dd>
          <dt>摘要</dt><dd>{{ detail.summary ?? "—" }}</dd>
          <dt>状态</dt><dd>{{ detail.status }}{{ detail.superseded_by ? ` → #${detail.superseded_by}` : "" }}{{ detail.supersede_reason ? `（${detail.supersede_reason}）` : "" }}</dd>
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
          #{{ entry.id }} <span class="status" :class="entry.status">{{ entry.status }}</span> {{ entry.content }}
        </div>
        <div class="row"><button @click="detail = null">关闭</button></div>
      </div>
    </div>

    <div v-if="editing" class="overlay" @click.self="editing = null">
      <div class="dialog">
        <h3>纠错编辑 #{{ editing.id }}</h3>
        <p class="hint">保存后生成替代版本，原版本转入 superseded 并保留溯源。</p>
        <label>内容<textarea v-model="editForm.content" rows="4" /></label>
        <label>摘要<input v-model="editForm.summary" /></label>
        <div class="row">
          <label>重要性<input v-model="editForm.importance" type="number" min="0" max="1" step="0.05" /></label>
          <label class="check"><input v-model="editForm.pin" type="checkbox" /> 置顶</label>
        </div>
        <label>原因（必填）<input v-model="editForm.reason" minlength="3" /></label>
        <div class="row">
          <button @click="editing = null">取消</button>
          <button class="primary" :disabled="editForm.reason.trim().length < 3" @click="submitEdit">保存替代版本</button>
        </div>
      </div>
    </div>

    <div v-if="adding" class="overlay" @click.self="adding = false">
      <div class="dialog">
        <h3>手动添加记忆</h3>
        <label>用户 ID<input v-model="addForm.user_id" :placeholder="fallbackUser || 'app_user UUID'" /></label>
        <div class="row">
          <label>类型<select v-model="addForm.type"><option v-for="t in types" :key="t">{{ t }}</option></select></label>
          <label>隐私<select v-model="addForm.privacy_level"><option>L0</option><option>L1</option><option>L2</option></select></label>
          <label>重要性<input v-model="addForm.importance" type="number" min="0" max="1" step="0.05" /></label>
        </div>
        <label>内容<textarea v-model="addForm.content" rows="3" /></label>
        <label class="check"><input v-model="addForm.pin" type="checkbox" /> 置顶</label>
        <div class="row">
          <button @click="adding = false">取消</button>
          <button class="primary" :disabled="addForm.content.trim().length < 2" @click="submitAdd">添加</button>
        </div>
      </div>
    </div>
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
.query-row input:first-child { flex: 1; min-width: 220px; }
.query-result .hit { display: flex; gap: 10px; padding: 5px 0; border-bottom: 1px dashed #232736; font-size: 13px; }
.score { color: var(--accent); min-width: 46px; font-variant-numeric: tabular-nums; }
.filters { display: flex; gap: 8px; flex-wrap: wrap; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; padding: 8px; border-bottom: 1px solid var(--line); }
td { padding: 8px; border-bottom: 1px solid #1c1f2b; vertical-align: top; }
td.content-cell { max-width: 320px; }
small { color: var(--muted); display: block; font-size: 11px; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; }
.actions button { padding: 4px 10px; font-size: 12px; }
button.danger { color: var(--danger); border-color: #4b2b36; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; border: 1px solid; }
.status.active { color: #7ee2a8; border-color: #2c5c41; background: #12241b; }
.status.conflict { color: #f2c078; border-color: #6b5325; background: #2a2113; }
.status.archived { color: #9aa0b4; border-color: #3a3f52; background: #191c27; }
.status.superseded { color: #b7a6f0; border-color: #4b3f76; background: #1d1830; }
.overlay { position: fixed; inset: 0; background: #05060a99; display: grid; place-items: center; z-index: 20; padding: 20px; }
.dialog { background: #141724; border: 1px solid #303342; border-radius: 14px; padding: 20px; width: min(620px, 94vw); max-height: 86vh; overflow-y: auto; display: grid; gap: 10px; }
.dialog h3 { margin: 0; }
.dialog h4 { margin: 8px 0 0; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.dialog label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
.dialog textarea, .dialog input, .dialog select { width: 100%; }
.check { display: flex; align-items: center; gap: 8px; }
.check input { width: auto; }
.detail-grid { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; font-size: 13px; margin: 0; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; word-break: break-all; }
.source-row { font-size: 12px; padding: 3px 0; }
.source-row code { color: #c9cfe6; }
</style>
