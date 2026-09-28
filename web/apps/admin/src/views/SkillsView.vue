<script setup lang="ts">
import { inject, nextTick, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

interface SkillOperation {
  name: string;
  description: string;
  method: string;
  path: string;
  risk: "read" | "confirm";
}
interface SkillItem {
  id: string;
  name: string;
  description: string;
  instructions: string;
  source: string;
  enabled: boolean;
  content_hash: string;
  version: number;
  api: { connection: string; auth?: { type: string; path: string } | null; operations: SkillOperation[] } | null;
}
interface SkillVersion {
  version: number;
  content_hash: string;
  created_at: string;
}
interface SkillPreview { tools: string[]; guidance: string; }
interface SkillConnection {
  id: string;
  base_url: string;
  auth_type: "none" | "bearer" | "header" | "login_bearer";
  secret_ref: string | null;
  username_ref: string | null;
  header_name: string | null;
  allowed_paths: string[];
  allowed_write_paths: string[];
  allowed_auth_paths: string[];
  enabled: boolean;
}
interface SkillCredentialStatus {
  skill_id: string;
  connection_id: string | null;
  configured: boolean;
  key_ready: boolean;
  updated_at: string | null;
}
interface SkillRun {
  id: string;
  skill_version: number;
  connection_id: string;
  operation: string;
  ok: boolean;
  reason_code: string | null;
  latency_ms: number;
  created_at: string;
}
interface SkillSuggestion {
  id: string;
  skill_id: string;
  skill_version: number;
  operation: string;
  reason_code: string;
  title: string;
  guidance: string;
  evidence_run_ids: string[];
  created_at: string;
}
interface SkillProposal {
  document: { name: string; description: string; instructions: string; api: unknown | null };
  warnings: string[];
  evidence: string[];
  executable: boolean;
}
interface SkillDraft {
  id: string;
  system_name: string;
  document: { name: string; description: string; instructions: string; api: unknown | null };
  warnings: string[];
  evidence: string[];
  source: "chat" | "harvest";
  turn_id: string | null;
  status: string;
  skill_id: string | null;
  target_skill_id: string | null;
  base_version: number | null;
  verify_status: "passed" | "failed" | null;
  verify_reason: string | null;
  verified_at: string | null;
  created_at: string;
  reviewed_at: string | null;
}

const props = withDefaults(defineProps<{ mode?: string }>(), { mode: "library" });
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const api = inject<AdminApi>("adminApi")!;
const route = useRoute();
// 「创建技能」与「智能创建」合并为一个 Tab：手动编写 / 文档生成页内切换，
// ?section=generate 深链（旧 tab=generate 重定向而来）默认落在文档生成。
const createMode = ref<"manual" | "generate">(
  route.query.section === "generate" ? "generate" : "manual",
);
const items = ref<SkillItem[]>([]);
const selected = ref<SkillItem | null>(null);
const busy = ref(false);
const name = ref("");
const description = ref("");
const instructions = ref("");
const manifestText = ref("");
const apiEditText = ref("");
const versions = ref<SkillVersion[]>([]);
const testText = ref("");
const preview = ref<SkillPreview | null>(null);
const systemName = ref("");
const sourceText = ref("");
const proposal = ref<SkillProposal | null>(null);
const generationStatus = ref("");
const generationError = ref("");
const connections = ref<SkillConnection[]>([]);
const credentialStatuses = ref<Record<string, SkillCredentialStatus>>({});
const credentialUsername = ref("");
const credentialPassword = ref("");
const runs = ref<SkillRun[]>([]);
const runsLoading = ref(false);
let runsRequestId = 0;
const suggestions = ref<SkillSuggestion[]>([]);
const drafts = ref<SkillDraft[]>([]);
const connectionId = ref("");
const connectionBaseUrl = ref("");
const connectionAuth = ref<"none" | "bearer" | "header" | "login_bearer">("none");
const connectionSecretRef = ref("");
const connectionUsernameRef = ref("");
const connectionHeader = ref("");
const connectionPaths = ref("");
const connectionWritePaths = ref("");
const connectionAuthPaths = ref("");
const connectionEnabled = ref(false);
const zipInput = ref<HTMLInputElement | null>(null);
const docInput = ref<HTMLInputElement | null>(null);

function fmt(value: string) {
  return new Date(value).toLocaleString();
}

function ok(text: string) {
  emit("status", text);
  ElMessage.success(text);
}

function fail(error: unknown, fallback: string) {
  const detail = error instanceof Error ? error.message : fallback;
  emit(
    "status",
    detail.includes("skill_schema_not_migrated")
      ? "技能中心数据库尚未迁移，请管理员运行数据库迁移后重试。"
      : detail.includes("skill_name_exists")
        ? "同名技能已存在，请到技能库刷新查看。"
      : detail,
    true,
  );
}

const sourceMarkdown = ref<string | null>(null);

function selectItem(item: SkillItem) {
  selected.value = item;
  runs.value = [];
  credentialUsername.value = "";
  credentialPassword.value = "";
  apiEditText.value = item.api ? JSON.stringify(item.api, null, 2) : "";
  void loadVersions(item.id);
  void loadRuns(item.id);
  void loadSourceMarkdown(item.id);
  if (item.api?.auth?.type === "login_bearer") void loadCredentialStatus(item.id);
}

async function loadSourceMarkdown(id: string) {
  sourceMarkdown.value = null;
  try {
    const detail = await api.request<{ source_markdown: string | null }>(
      `/api/v1/admin/skills/${id}`,
    );
    if (selected.value?.id === id) sourceMarkdown.value = detail.source_markdown;
  } catch {
    sourceMarkdown.value = null;
  }
}

async function loadCredentialStatus(id: string) {
  const status = await api.request<SkillCredentialStatus>(`/api/v1/admin/skills/${id}/credential`);
  credentialStatuses.value[id] = status;
}

async function saveCredential() {
  if (!selected.value || !credentialUsername.value.trim() || !credentialPassword.value) return;
  busy.value = true;
  try {
    credentialStatuses.value[selected.value.id] = await api.request<SkillCredentialStatus>(
      `/api/v1/admin/skills/${selected.value.id}/credential`,
      { method: "PUT", body: JSON.stringify({ username: credentialUsername.value.trim(), password: credentialPassword.value }) },
    );
    credentialUsername.value = "";
    credentialPassword.value = "";
    ok("该技能的登录凭据已加密保存。页面不会回显账号或密码。");
  } catch (error) {
    fail(error, "保存登录凭据失败");
  } finally {
    busy.value = false;
  }
}

async function deleteCredential() {
  if (!selected.value) return;
  busy.value = true;
  try {
    credentialStatuses.value[selected.value.id] = await api.request<SkillCredentialStatus>(
      `/api/v1/admin/skills/${selected.value.id}/credential`, { method: "DELETE" },
    );
    credentialUsername.value = "";
    credentialPassword.value = "";
    ok("该技能的登录凭据已移除。");
  } catch (error) {
    fail(error, "移除登录凭据失败");
  } finally {
    busy.value = false;
  }
}

async function loadVersions(id: string) {
  try {
    versions.value = await api.request<SkillVersion[]>(`/api/v1/admin/skills/${id}/versions`);
  } catch (error) {
    fail(error, "版本历史加载失败");
  }
}

async function loadRuns(id: string) {
  const requestId = ++runsRequestId;
  runsLoading.value = true;
  try {
    const result = await api.request<SkillRun[]>(`/api/v1/admin/skills/${id}/runs`);
    if (requestId === runsRequestId && selected.value?.id === id) runs.value = result;
  } catch (error) {
    if (requestId === runsRequestId) fail(error, "运行记录加载失败");
  } finally {
    if (requestId === runsRequestId) runsLoading.value = false;
  }
}

async function loadConnections() {
  try {
    connections.value = await api.request<SkillConnection[]>("/api/v1/admin/skills/connections");
  } catch (error) {
    fail(error, "连接列表加载失败");
  }
}

async function loadSuggestions() {
  try {
    suggestions.value = await api.request<SkillSuggestion[]>("/api/v1/admin/skills/suggestions");
  } catch (error) {
    fail(error, "学习建议加载失败");
  }
}

async function dismissSuggestion(id: string) {
  busy.value = true;
  try {
    await api.request(`/api/v1/admin/skills/suggestions/${id}/dismiss`, { method: "POST" });
    await loadSuggestions();
  } catch (error) {
    fail(error, "处理建议失败");
  } finally {
    busy.value = false;
  }
}

async function loadDrafts() {
  try {
    drafts.value = await api.request<SkillDraft[]>("/api/v1/admin/skills/drafts");
  } catch (error) {
    fail(error, "待审草稿加载失败");
  }
}

async function approveDraft(id: string) {
  const isRevision = drafts.value.some((item) => item.id === id && item.target_skill_id);
  busy.value = true;
  try {
    const skill = await api.request<SkillItem>(`/api/v1/admin/skills/drafts/${id}/approve`, {
      method: "POST",
    });
    await Promise.all([loadDrafts(), reload()]);
    ok(
      isRevision
        ? `已为 ${skill.name} 创建新版本 v${skill.version}；技能保持停用，请核对后启用。`
        : "草稿已通过审阅，落库为未启用技能；请在技能库核对连接后再启用。",
    );
  } catch (error) {
    fail(error, "审阅草稿失败");
  } finally {
    busy.value = false;
  }
}

const verifyingId = ref("");

async function verifyDraft(id: string) {
  verifyingId.value = id;
  try {
    const updated = await api.request<SkillDraft>(
      `/api/v1/admin/skills/drafts/${id}/verify`,
      { method: "POST" },
    );
    const index = drafts.value.findIndex((item) => item.id === id);
    if (index >= 0) drafts.value[index] = updated;
    if (updated.verify_status === "passed") {
      ok("试跑通过：草稿声明的只读接口在真实连接上返回成功。");
    } else {
      emit("status", `试跑未通过：${updated.verify_reason ?? "未知原因"}。请核对连接配置与接口路径。`, true);
      ElMessage.warning(
        `试跑未通过：${updated.verify_reason ?? "未知原因"}。请核对连接配置与接口路径。`,
      );
    }
  } catch (error) {
    fail(error, "试跑失败");
  } finally {
    verifyingId.value = "";
  }
}

function draftTargetName(draft: SkillDraft): string {
  return items.value.find((item) => item.id === draft.target_skill_id)?.name ?? "未知技能";
}

interface DraftOperation {
  name: string;
  method: string;
  path: string;
  risk: string;
  parameters?: Record<string, unknown>;
}

/** 修订草稿与现有技能当前版本的差异摘要（docs/08 §5「与当前版本比较」）。 */
function revisionDiff(draft: SkillDraft): string[] {
  const target = items.value.find((item) => item.id === draft.target_skill_id);
  if (!target) return ["找不到目标技能的当前版本，请刷新技能库。"];
  const changes: string[] = [];
  if (draft.document.description !== target.description) changes.push("用途描述有修改");
  if (draft.document.instructions !== target.instructions) changes.push("使用说明有修改");
  const draftOps = (draft.document.api as { operations?: DraftOperation[] } | null)?.operations ?? [];
  const targetOps = (target.api?.operations ?? []) as DraftOperation[];
  const signature = (op: DraftOperation) =>
    JSON.stringify([op.method, op.path, op.risk, op.parameters ?? {}]);
  const draftByName = new Map(draftOps.map((op) => [op.name, op]));
  const targetByName = new Map(targetOps.map((op) => [op.name, op]));
  const added = draftOps.filter((op) => !targetByName.has(op.name)).map((op) => op.name);
  const removed = targetOps.filter((op) => !draftByName.has(op.name)).map((op) => op.name);
  const changed = draftOps
    .filter((op) => targetByName.has(op.name) && signature(targetByName.get(op.name)!) !== signature(op))
    .map((op) => op.name);
  if (added.length) changes.push(`新增操作：${added.join("、")}`);
  if (removed.length) changes.push(`移除操作：${removed.join("、")}`);
  if (changed.length) changes.push(`修改操作：${changed.join("、")}`);
  return changes.length ? changes : ["与当前版本内容一致"];
}

async function dismissDraft(id: string) {
  busy.value = true;
  try {
    await api.request(`/api/v1/admin/skills/drafts/${id}/dismiss`, { method: "POST" });
    await loadDrafts();
  } catch (error) {
    fail(error, "忽略草稿失败");
  } finally {
    busy.value = false;
  }
}

function editConnection(item: SkillConnection) {
  connectionId.value = item.id;
  connectionBaseUrl.value = item.base_url;
  connectionAuth.value = item.auth_type;
  connectionSecretRef.value = item.secret_ref ?? "";
  connectionUsernameRef.value = item.username_ref ?? "";
  connectionHeader.value = item.header_name ?? "";
  connectionPaths.value = item.allowed_paths.join("\n");
  connectionWritePaths.value = item.allowed_write_paths.join("\n");
  connectionAuthPaths.value = item.allowed_auth_paths.join("\n");
  connectionEnabled.value = item.enabled;
}

function connectionStatus(item: SkillItem): string {
  if (!item.enabled) return "技能未启用";
  if (!item.api) return "仅有说明，无 API 操作";
  if (item.api.connection === "pnkx") return "使用旧 PNKX 集成";
  const connection = connections.value.find((candidate) => candidate.id === item.api?.connection);
  if (!connection) return `缺少连接 ${item.api.connection}`;
  if (!connection.enabled) return `连接 ${connection.id} 未启用`;
  if (connection.auth_type === "login_bearer" && (!item.api.auth || !connection.allowed_auth_paths.includes(item.api.auth.path))) return "登录契约未获连接授权";
  if (connection.auth_type === "login_bearer") {
    const credential = credentialStatuses.value[item.id];
    if (!credential) return "凭据状态待加载";
    if (!credential.key_ready) return "加密密钥未配置";
    if (!credential.configured) return "该技能未配置账号密码";
  }
  const readPaths = item.api.operations.filter((operation) => operation.risk === "read").map((operation) => operation.path);
  if (!readPaths.some((path) => connection.allowed_paths.includes(path))) return "连接未放行只读路径";
  return "连接已启用；请确认服务端密钥有效";
}

function selectedConnection(): SkillConnection | undefined {
  return connections.value.find((item) => item.id === selected.value?.api?.connection);
}

async function toggleSelectedConnection() {
  const connection = selectedConnection();
  if (!connection) return;
  busy.value = true;
  try {
    await api.request(`/api/v1/admin/skills/connections/${encodeURIComponent(connection.id)}`, {
      method: "PUT",
      body: JSON.stringify({
        id: connection.id,
        base_url: connection.base_url,
        auth_type: connection.auth_type,
        secret_ref: connection.secret_ref,
        username_ref: connection.username_ref,
        header_name: connection.header_name,
        allowed_paths: connection.allowed_paths,
        allowed_write_paths: connection.allowed_write_paths,
        allowed_auth_paths: connection.allowed_auth_paths,
        enabled: !connection.enabled,
      }),
    });
    await loadConnections();
    ok(connection.enabled ? "API 连接已停用。" : "API 连接已启用。");
  } catch (error) {
    fail(error, "切换 API 连接失败");
  } finally {
    busy.value = false;
  }
}

async function saveConnection() {
  busy.value = true;
  try {
    const id = connectionId.value.trim();
    await api.request(`/api/v1/admin/skills/connections/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({
        id,
        base_url: connectionBaseUrl.value.trim(),
        auth_type: connectionAuth.value,
        secret_ref: connectionAuth.value === "none" || !connectionSecretRef.value.trim() ? null : connectionSecretRef.value.trim(),
        username_ref: connectionAuth.value === "login_bearer" && connectionUsernameRef.value.trim() ? connectionUsernameRef.value.trim() : null,
        header_name: connectionAuth.value === "header" ? connectionHeader.value.trim() : null,
        allowed_paths: connectionPaths.value.split("\n").map((path) => path.trim()).filter(Boolean),
        allowed_write_paths: connectionWritePaths.value.split("\n").map((path) => path.trim()).filter(Boolean),
        allowed_auth_paths: connectionAuth.value === "login_bearer" ? connectionAuthPaths.value.split("\n").map((path) => path.trim()).filter(Boolean) : [],
        enabled: connectionEnabled.value,
      }),
    });
    await loadConnections();
    ok("连接配置已保存。已启用的只读技能会按路径白名单挂载。");
  } catch (error) {
    fail(error, "保存连接失败");
  } finally {
    busy.value = false;
  }
}

async function reload() {
  try {
    items.value = await api.request<SkillItem[]>("/api/v1/admin/skills");
    selected.value = items.value.find((item) => item.id === selected.value?.id) ?? null;
    if (selected.value) await Promise.all([loadVersions(selected.value.id), loadRuns(selected.value.id)]);
    else { versions.value = []; runs.value = []; }
    await Promise.all(items.value.filter((item) => item.api?.auth?.type === "login_bearer").map(async (item) => {
      try { await loadCredentialStatus(item.id); } catch { /* 状态不影响技能列表读取 */ }
    }));
  } catch (error) {
    fail(error, "加载失败");
  }
}

async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  busy.value = true;
  try {
    const body = new FormData();
    body.append("file", file);
    const item = await api.request<SkillItem>("/api/v1/admin/skills/import", { method: "POST", body });
    ok(`已导入 ${item.name}，默认停用。请检查后启用。`);
    await reload();
    selectItem(item);
  } catch (error) {
    fail(error, "导入失败");
  } finally {
    busy.value = false;
    input.value = "";
  }
}

async function create(generated = false) {
  busy.value = true;
  try {
    const manifest = manifestText.value.trim() ? JSON.parse(manifestText.value) as unknown : null;
    const item = await api.request<SkillItem>(generated ? "/api/v1/admin/skills/generated" : "/api/v1/admin/skills", {
      method: "POST",
      body: JSON.stringify({ name: name.value, description: description.value, instructions: instructions.value, api: manifest }),
    });
    ok(`已创建 ${item.name}，默认停用。`);
    name.value = description.value = instructions.value = manifestText.value = "";
    if (generated) proposal.value = null;
    await reload();
    selectItem(item);
  } catch (error) {
    fail(error, "创建失败");
  } finally {
    busy.value = false;
  }
}

function setProposal(result: SkillProposal) {
  proposal.value = result;
  name.value = result.document.name;
  description.value = result.document.description;
  instructions.value = result.document.instructions;
  manifestText.value = result.document.api ? JSON.stringify(result.document.api, null, 2) : "";
  generationStatus.value = `已生成 ${result.document.name}，请在下方核对后保存。`;
  void nextTick(() => document.getElementById("generated-proposal")?.scrollIntoView({ behavior: "smooth", block: "start" }));
}

function generationErrorText(error: unknown): string {
  const detail = error instanceof Error ? error.message : "生成失败";
  if (detail.includes("generated_skill_truncated")) return "本地模型输出长度不足，无法生成完整草稿。请拆分文档后重试，或使用带接口表的 Markdown 文档。";
  if (detail.includes("generated_skill_invalid")) return "模型返回的技能格式无效。请缩小文档范围后重试。";
  return detail;
}

async function generate() {
  busy.value = true;
  proposal.value = null;
  generationError.value = "";
  generationStatus.value = "正在解析文档并生成草稿…";
  try {
    const result = await api.request<SkillProposal>("/api/v1/admin/skills/generate", {
      method: "POST",
      body: JSON.stringify({ system_name: systemName.value.trim(), source: sourceText.value }),
    });
    setProposal(result);
  } catch (error) {
    generationError.value = generationErrorText(error);
    generationStatus.value = "";
    fail(error, "生成失败");
  } finally {
    busy.value = false;
  }
}

async function generateUpload(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  busy.value = true;
  proposal.value = null;
  generationError.value = "";
  generationStatus.value = `正在解析 ${file.name} 并生成草稿…`;
  try {
    const body = new FormData();
    body.append("file", file);
    const result = await api.request<SkillProposal>(`/api/v1/admin/skills/generate-upload?system_name=${encodeURIComponent(systemName.value.trim())}`, { method: "POST", body });
    setProposal(result);
  } catch (error) {
    generationError.value = generationErrorText(error);
    generationStatus.value = "";
    fail(error, "解析或生成失败");
  } finally {
    busy.value = false;
    input.value = "";
  }
}

async function toggle(item: SkillItem) {
  busy.value = true;
  try {
    await api.request(`/api/v1/admin/skills/${item.id}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled: !item.enabled }),
    });
    await reload();
    ok(item.enabled ? "技能已停用" : "技能已启用");
  } catch (error) {
    fail(error, "更新失败");
  } finally {
    busy.value = false;
  }
}

async function saveApi() {
  if (!selected.value) return;
  busy.value = true;
  try {
    const item = await api.request<SkillItem>(`/api/v1/admin/skills/${selected.value.id}/api`, {
      method: "PUT",
      body: JSON.stringify(JSON.parse(apiEditText.value) as unknown),
    });
    await reload();
    selectItem(item);
    ok("API 契约已保存。请检查操作后启用。");
  } catch (error) {
    fail(error, "保存失败");
  } finally {
    busy.value = false;
  }
}

async function rollback(version: number) {
  if (!selected.value) return;
  busy.value = true;
  try {
    const item = await api.request<SkillItem>(`/api/v1/admin/skills/${selected.value.id}/rollback`, {
      method: "POST",
      body: JSON.stringify({ version }),
    });
    await reload();
    selectItem(item);
    ok(`已从版本 ${version} 恢复为新版本 ${item.version}。`);
  } catch (error) {
    fail(error, "回滚失败");
  } finally {
    busy.value = false;
  }
}

async function testMatch() {
  busy.value = true;
  try {
    preview.value = await api.request<SkillPreview>("/api/v1/admin/skills/preview", {
      method: "POST",
      body: JSON.stringify({ text: testText.value, privacy_level: "L1" }),
    });
  } catch (error) {
    fail(error, "测试失败");
  } finally {
    busy.value = false;
  }
}

let runsRefreshTimer: ReturnType<typeof setInterval> | undefined;
function refreshVisibleRuns() {
  if (props.mode === "library" && selected.value && document.visibilityState === "visible") {
    void loadRuns(selected.value.id);
  }
}
onMounted(() => {
  void reload();
  void loadConnections();
  void loadSuggestions();
  void loadDrafts();
  document.addEventListener("visibilitychange", refreshVisibleRuns);
  runsRefreshTimer = setInterval(refreshVisibleRuns, 30_000);
});
onUnmounted(() => {
  document.removeEventListener("visibilitychange", refreshVisibleRuns);
  if (runsRefreshTimer) clearInterval(runsRefreshTimer);
});
</script>

<template>
  <section class="content">
    <template v-if="mode === 'library'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">技能中心 · 技能库</div>
          <h2>技能库</h2>
          <p>上传包含 SKILL.md 的 ZIP。只读 API 在技能和对应连接启用后可由聊天匹配使用。</p>
        </div>
        <div class="hero-actions">
          <el-button :loading="busy" @click="reload">刷新</el-button>
          <el-button type="primary" :loading="busy" @click="zipInput?.click()">上传 ZIP</el-button>
          <input ref="zipInput" type="file" accept=".zip,application/zip" class="hidden-input" @change="upload" />
        </div>
      </div>
      <div class="library-layout">
        <div class="panel">
          <div class="panel-head"><div><h2>全部技能</h2><p>点击技能查看详情、契约与运行记录。</p></div></div>
          <el-table v-loading="busy" :data="items" empty-text="还没有技能" highlight-current-row style="width:100%" @row-click="(row: SkillItem) => selectItem(row)">
            <el-table-column prop="name" label="名称" min-width="130" />
            <el-table-column prop="description" label="描述" min-width="160" show-overflow-tooltip />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? "已启用" : "草稿 / 停用" }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="运行就绪" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ connectionStatus(row as SkillItem) }}</template>
            </el-table-column>
            <el-table-column prop="version" label="版本" width="70" />
          </el-table>
        </div>
        <div v-if="selected" class="panel">
          <div class="panel-head">
            <div><h2>{{ selected.name }}</h2><p>{{ selected.description }}</p></div>
            <el-button :type="selected.enabled ? 'default' : 'primary'" :loading="busy" @click="toggle(selected)">{{ selected.enabled ? "停用" : "启用" }}</el-button>
          </div>
          <p class="meta">版本 {{ selected.version }} · 来源 {{ selected.source }} · SHA-256 {{ selected.content_hash.slice(0, 12) }}…</p>
          <p class="hint">运行状态：{{ connectionStatus(selected) }}</p>
          <div v-if="selected.api?.auth?.type === 'login_bearer'">
            <h3>此技能的登录账号</h3>
            <p class="hint">账号密码加密存入数据库并只关联 {{ selected.name }}；页面不回显。Skill 只声明 {{ selected.api.auth.path }} 的登录方式。</p>
            <p class="hint">{{ credentialStatuses[selected.id]?.configured ? `已配置 · 更新于 ${fmt(credentialStatuses[selected.id].updated_at!)}` : '尚未配置' }} · {{ credentialStatuses[selected.id]?.key_ready ? '加密密钥就绪' : '需要配置 ARIA_SKILL_CREDENTIAL_KEY' }}</p>
            <div class="form-grid">
              <label><span>PNKX 用户名</span><el-input v-model="credentialUsername" autocomplete="off" placeholder="输入账号" /></label>
              <label><span>PNKX 密码</span><el-input v-model="credentialPassword" type="password" autocomplete="new-password" show-password placeholder="输入密码" /></label>
              <div class="actions wide">
                <el-button type="primary" :loading="busy" :disabled="!credentialStatuses[selected.id]?.key_ready || !credentialUsername.trim() || !credentialPassword" @click="saveCredential">加密保存</el-button>
                <el-button :loading="busy" :disabled="!credentialStatuses[selected.id]?.configured" @click="deleteCredential">移除凭据</el-button>
                <el-button v-if="selectedConnection()" :loading="busy" :disabled="!selectedConnection()?.enabled && (!credentialStatuses[selected.id]?.configured || !credentialStatuses[selected.id]?.key_ready)" @click="toggleSelectedConnection">{{ selectedConnection()?.enabled ? '停用 API 连接' : '启用 API 连接' }}</el-button>
              </div>
            </div>
          </div>
          <div>
            <h3>执行说明</h3>
            <pre>{{ selected.instructions }}</pre>
          </div>
          <div>
            <h3>API 操作</h3>
            <el-table v-if="selected.api" :data="selected.api.operations" empty-text="没有登记操作" style="width:100%">
              <el-table-column prop="name" label="操作" min-width="120" />
              <el-table-column label="方法与路径" min-width="170">
                <template #default="{ row }"><code>{{ row.method }} {{ row.path }}</code></template>
              </el-table-column>
              <el-table-column label="说明" min-width="200">
                <template #default="{ row }">
                  <small>{{ row.description }} · {{ row.risk === "read" ? (selected.api?.connection === "pnkx" ? "PNKX 只读可执行" : "需启用对应 API 连接并配置路径") : "写入 · A2 确认后经连接写白名单执行" }}</small>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-else description="此技能没有声明式 API" :image-size="60" />
          </div>
          <div v-if="sourceMarkdown">
            <h3>原始文档（导入的 SKILL.md）</h3>
            <pre class="hint" style="white-space: pre-wrap; max-height: 320px; overflow: auto; background: var(--el-fill-color-light); padding: 12px; border-radius: 6px;">{{ sourceMarkdown }}</pre>
          </div>
          <div>
            <h3>配置 API 契约</h3>
            <p class="hint">现成 Skill 只有 API 文字说明时，在此填入核对后的声明式契约。先停用才能修改。</p>
            <div class="form-grid">
              <label class="wide"><span>API 契约 JSON</span><el-input v-model="apiEditText" type="textarea" :rows="8" :disabled="selected.enabled" placeholder='{"schema_version":1,"connection":"pnkx","operations":[...]}' /></label>
              <div class="actions wide">
                <el-button type="primary" :disabled="selected.enabled || !apiEditText.trim()" :loading="busy" @click="saveApi">保存 API 契约</el-button>
              </div>
            </div>
          </div>
          <div>
            <h3>版本历史</h3>
            <el-table :data="versions" empty-text="暂无版本记录" style="width:100%">
              <el-table-column prop="version" label="版本" width="70" />
              <el-table-column label="创建时间" min-width="160">
                <template #default="{ row }">{{ fmt(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="120" fixed="right">
                <template #default="{ row }">
                  <el-tag v-if="row.version === selected.version" size="small" type="success">当前</el-tag>
                  <el-button v-else size="small" :disabled="selected.enabled || busy" @click="rollback(row.version)">恢复此版本</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
          <div>
            <div class="panel-head">
              <h3>最近运行记录</h3>
              <el-button size="small" :loading="runsLoading" @click="loadRuns(selected.id)">刷新记录</el-button>
            </div>
            <p class="hint">仅保存版本、操作、结果、错误码和耗时；不保存参数或 API 响应。</p>
            <el-table v-loading="runsLoading" :data="runs" empty-text="暂无运行记录" style="width:100%">
              <el-table-column label="时间" min-width="150">
                <template #default="{ row }">{{ fmt(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="连接 / 操作" min-width="150">
                <template #default="{ row }">{{ row.connection_id }} / {{ row.operation }}</template>
              </el-table-column>
              <el-table-column label="版本" width="70">
                <template #default="{ row }">v{{ row.skill_version }}</template>
              </el-table-column>
              <el-table-column label="结果" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? "成功" : (row.reason_code || "失败") }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="耗时" width="90">
                <template #default="{ row }">{{ Math.round(row.latency_ms) }} ms</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
        <div v-else class="panel">
          <el-empty description="选择左侧技能查看详情" />
        </div>
      </div>
    </template>

    <template v-else-if="mode === 'create'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">技能中心 · 创建技能</div>
          <h2>创建技能</h2>
          <p>手动编写说明与声明式 API 契约，或从 API/使用文档智能生成可审核草稿；生成路径来自文档逐字校验。保存后默认停用。</p>
        </div>
        <div class="hero-actions">
          <el-radio-group v-model="createMode">
            <el-radio-button value="manual">手动编写</el-radio-button>
            <el-radio-button value="generate">文档生成</el-radio-button>
          </el-radio-group>
        </div>
      </div>
      <template v-if="createMode === 'manual'">
        <div class="panel">
          <div class="panel-head"><div><h2>技能草稿</h2><p>保存后默认停用，可在技能库中检查并启用。</p></div></div>
          <form class="form-grid" @submit.prevent="create(false)">
            <label><span>名称（英文小写与连字符）</span><el-input v-model="name" placeholder="pnkx-coupons" /></label>
            <label class="wide"><span>用途与触发场景</span><el-input v-model="description" type="textarea" :rows="2" /></label>
            <label class="wide"><span>执行说明</span><el-input v-model="instructions" type="textarea" :rows="6" /></label>
            <label class="wide"><span>API 契约 JSON（可选）</span><el-input v-model="manifestText" type="textarea" :rows="8" placeholder='{"schema_version":1,"connection":"pnkx","operations":[...]}' /></label>
            <div class="actions wide">
              <el-button type="primary" native-type="submit" :loading="busy">保存草稿</el-button>
            </div>
          </form>
        </div>
      </template>
      <template v-else>
        <div class="panel">
          <div class="panel-head"><div><h2>文档输入</h2><p>需要先填写系统标识，再从文本或文档生成。</p></div></div>
          <div class="form-grid">
            <label><span>系统标识（英文小写与连字符）</span><el-input v-model="systemName" placeholder="pnkx 或 your-system" /></label>
            <label class="wide"><span>API 文档或使用文档</span><el-input v-model="sourceText" type="textarea" :rows="10" placeholder="粘贴接口路径、方法、参数，或系统的使用说明" /></label>
            <div class="actions wide">
              <el-button type="primary" :disabled="!systemName.trim() || !sourceText.trim()" :loading="busy" @click="generate">从文本生成</el-button>
              <el-button :disabled="busy || !systemName.trim()" :loading="busy" @click="docInput?.click()">上传文档生成</el-button>
              <input ref="docInput" type="file" accept=".md,.txt,.json,.yaml,.yml,.html,.htm,.docx" class="hidden-input" :disabled="busy || !systemName.trim()" @change="generateUpload" />
            </div>
            <p v-if="generationStatus" class="wide" role="status">{{ generationStatus }}</p>
            <el-alert v-if="generationError" class="wide" :title="generationError" type="error" show-icon :closable="false" />
          </div>
        </div>
        <div v-if="proposal" id="generated-proposal" class="panel">
          <div class="panel-head"><div><h2>审核生成草稿</h2><p>生成结果尚未保存，也不会调用外部 API。请逐项核对文档依据和必填参数。保存后默认停用。</p></div></div>
          <el-alert v-for="warning in proposal.warnings" :key="warning" :title="warning" type="warning" show-icon :closable="false" class="warning-alert" />
          <div v-if="proposal.evidence.length">
            <h3>文档依据</h3>
            <p v-for="quote in proposal.evidence" :key="quote" class="evidence">{{ quote }}</p>
          </div>
          <form class="form-grid" @submit.prevent="create(true)">
            <label><span>名称</span><el-input v-model="name" /></label>
            <label class="wide"><span>用途与触发场景</span><el-input v-model="description" type="textarea" :rows="2" /></label>
            <label class="wide"><span>执行说明</span><el-input v-model="instructions" type="textarea" :rows="6" /></label>
            <label class="wide"><span>API 契约 JSON（无 API 可留空）</span><el-input v-model="manifestText" type="textarea" :rows="12" /></label>
            <div class="actions wide">
              <el-button type="primary" native-type="submit" :loading="busy">保存为停用草稿</el-button>
            </div>
          </form>
        </div>
      </template>
    </template>

    <template v-else-if="mode === 'connections'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">技能中心 · API 连接</div>
          <h2>API 连接</h2>
          <p>连接由管理员配置，只读与写入路径模板分开登记，且只允许 HTTPS。只读路径按相关性挂载进对话；写入路径仅服务 Action Registry 计划—确认—执行链，每次执行都需用户确认。用户名密码登录的账号在技能详情中加密保存；固定令牌或自定义头可使用服务端 env: 引用。</p>
        </div>
        <div class="hero-actions">
          <el-button :loading="busy" @click="loadConnections">刷新</el-button>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><div><h2>已配置连接</h2><p>点击「编辑」把配置带入下方表单修改。</p></div></div>
        <el-table :data="connections" empty-text="尚未配置连接" style="width:100%">
          <el-table-column prop="id" label="连接标识" min-width="120" />
          <el-table-column label="服务地址" min-width="220">
            <template #default="{ row }"><code class="endpoint">{{ row.base_url }}</code></template>
          </el-table-column>
          <el-table-column label="认证" width="110">
            <template #default="{ row }">{{ row.auth_type === "none" ? "无认证" : row.auth_type === "bearer" ? "Bearer" : row.auth_type === "login_bearer" ? "账号登录 Bearer" : "自定义请求头" }}</template>
          </el-table-column>
          <el-table-column label="只读路径" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.allowed_paths.join("、") }}</template>
          </el-table-column>
          <el-table-column label="写入路径" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.allowed_write_paths.length ? row.allowed_write_paths.join("、") : "未放行写入" }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? "已启用" : "已停用" }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="editConnection(row as SkillConnection)">编辑</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="panel">
        <div class="panel-head"><div><h2>配置连接</h2><p>路径模板必须与技能契约完全一致，每行一条。</p></div></div>
        <div class="form-grid">
          <label><span>连接标识</span><el-input v-model="connectionId" placeholder="partner-system" /></label>
          <label><span>HTTPS 服务地址（可含固定前缀）</span><el-input v-model="connectionBaseUrl" placeholder="https://api.example.com/prod-api" /></label>
          <label><span>认证方式</span>
            <el-select v-model="connectionAuth">
              <el-option label="无认证" value="none" />
              <el-option label="Bearer Token" value="bearer" />
              <el-option label="用户名密码登录获取 Bearer" value="login_bearer" />
              <el-option label="自定义 X- 请求头" value="header" />
            </el-select>
          </label>
          <label v-if="connectionAuth === 'login_bearer'"><span>用户名环境变量回退（可选）</span><el-input v-model="connectionUsernameRef" placeholder="env:PNKX_USERNAME" /></label>
          <label v-if="connectionAuth !== 'none'"><span>{{ connectionAuth === 'login_bearer' ? '密码环境变量回退（可选）' : '密钥环境变量引用' }}</span><el-input v-model="connectionSecretRef" placeholder="env:PARTNER_API_TOKEN" /></label>
          <label v-if="connectionAuth === 'header'"><span>请求头名称</span><el-input v-model="connectionHeader" placeholder="X-API-Key" /></label>
          <label v-if="connectionAuth === 'login_bearer'" class="wide"><span>允许登录路径（每行一条）</span><el-input v-model="connectionAuthPaths" type="textarea" :rows="2" placeholder="/clientLogin" /></label>
          <label class="wide"><span>只读路径模板（每行一条，与技能契约完全一致）</span><el-input v-model="connectionPaths" type="textarea" :rows="5" placeholder="/coupons&#10;/coupons/{coupon_id}" /></label>
          <label class="wide"><span>写入路径模板（每行一条；写操作需用户逐次确认后才会执行）</span><el-input v-model="connectionWritePaths" type="textarea" :rows="3" placeholder="/todos" /></label>
          <label class="wide"><span>启用连接</span><el-checkbox v-model="connectionEnabled">已启用的只读技能会按路径白名单挂载</el-checkbox></label>
          <div class="actions wide">
            <el-button type="primary" :loading="busy" @click="saveConnection">保存连接</el-button>
          </div>
        </div>
      </div>
    </template>

    <template v-else-if="mode === 'suggestions'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">技能中心 · 学习建议</div>
          <h2>学习建议</h2>
          <p>同一技能版本的同一操作在最近 10 次运行中出现至少 3 次相同错误时生成。建议只用于排查，不会自动改写技能或扩展权限。</p>
        </div>
        <div class="hero-actions">
          <el-button :loading="busy" @click="loadSuggestions">刷新</el-button>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><div><h2>待审核建议</h2><p>结合错误码与运行证据判断是否需要调整技能契约或连接配置。</p></div></div>
        <el-table :data="suggestions" empty-text="暂无待审核建议" style="width:100%">
          <el-table-column label="建议" min-width="240">
            <template #default="{ row }">
              <strong>{{ row.title }}</strong>
              <small>{{ row.guidance }}</small>
            </template>
          </el-table-column>
          <el-table-column label="操作 · 版本" min-width="150">
            <template #default="{ row }">{{ row.operation }} · v{{ row.skill_version }}</template>
          </el-table-column>
          <el-table-column label="错误码" min-width="110">
            <template #default="{ row }"><code>{{ row.reason_code }}</code></template>
          </el-table-column>
          <el-table-column label="证据" width="80">
            <template #default="{ row }">{{ row.evidence_run_ids.length }} 次</template>
          </el-table-column>
          <el-table-column label="创建时间" min-width="150">
            <template #default="{ row }">{{ fmt(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="danger" plain :disabled="busy" @click="dismissSuggestion(row.id)">忽略</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>

    <template v-else-if="mode === 'drafts'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">技能中心 · 待审草稿</div>
          <h2>待审草稿</h2>
          <p>对话中通过 propose_skill 主动沉淀，或后台从含接口文档的消息中收割生成。草稿不会执行；可先试跑校验只读接口，通过后新建技能或为现有技能创建新版本，均保持停用待启用。</p>
        </div>
        <div class="hero-actions">
          <el-button :loading="busy" @click="loadDrafts">刷新</el-button>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><div><h2>待审阅草稿</h2><p>展开查看说明、警告与文档证据，逐字核对接口后再通过。</p></div></div>
        <el-table :data="drafts" empty-text="暂无待审草稿" style="width:100%">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div style="padding: 4px 16px 12px">
                <template v-if="row.target_skill_id">
                  <h3>与当前版本的差异</h3>
                  <ul class="hint">
                    <li v-for="(change, index) in revisionDiff(row as SkillDraft)" :key="index">{{ change }}</li>
                  </ul>
                </template>
                <h3>使用说明</h3>
                <p class="hint" style="white-space: pre-wrap">{{ row.document.instructions }}</p>
                <template v-if="row.warnings.length">
                  <h3>待核对警告</h3>
                  <ul class="hint">
                    <li v-for="(warning, index) in row.warnings" :key="index">{{ warning }}</li>
                  </ul>
                </template>
                <template v-if="row.evidence.length">
                  <h3>文档证据</h3>
                  <ul class="hint">
                    <li v-for="(item, index) in row.evidence" :key="index"><code>{{ item }}</code></li>
                  </ul>
                </template>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="技能" min-width="220">
            <template #default="{ row }">
              <el-tag v-if="row.target_skill_id" size="small" type="warning" style="margin-right: 6px">
                修订 {{ draftTargetName(row as SkillDraft) }}@v{{ row.base_version }}
              </el-tag>
              <strong>{{ row.document.name }}</strong>
              <small>{{ row.document.description }}</small>
            </template>
          </el-table-column>
          <el-table-column label="系统" min-width="100">
            <template #default="{ row }"><code>{{ row.system_name }}</code></template>
          </el-table-column>
          <el-table-column label="来源" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.source === 'chat' ? 'primary' : 'warning'">{{ row.source === "chat" ? "对话提案" : "后台收割" }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="试跑" width="150">
            <template #default="{ row }">
              <el-tag v-if="row.verify_status === 'passed'" size="small" type="success">通过</el-tag>
              <el-tooltip v-else-if="row.verify_status === 'failed'" :content="row.verify_reason ?? ''" placement="top">
                <el-tag size="small" type="danger">未通过</el-tag>
              </el-tooltip>
              <el-button
                v-else
                size="small"
                plain
                :loading="verifyingId === row.id"
                @click="verifyDraft(row.id)"
              >试跑</el-button>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" min-width="150">
            <template #default="{ row }">{{ fmt(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="190" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="primary" plain :disabled="busy" @click="approveDraft(row.id)">
                {{ row.target_skill_id ? "通过并建版本" : "通过" }}
              </el-button>
              <el-button size="small" type="danger" plain :disabled="busy" @click="dismissDraft(row.id)">忽略</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>

    <template v-else-if="mode === 'test'">
      <div class="hero panel">
        <div>
          <div class="eyebrow">技能中心 · 匹配测试</div>
          <h2>匹配测试</h2>
          <p>输入一条模拟用户请求，查看当前已启用技能的指导内容与可挂载只读工具。此处不调用远端 API。</p>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><div><h2>模拟请求</h2><p>例如：看看我们有哪些情侣卡券。</p></div></div>
        <el-input v-model="testText" type="textarea" :rows="3" placeholder="看看我们有哪些情侣卡券" />
        <div class="actions">
          <el-button type="primary" :disabled="!testText.trim()" :loading="busy" @click="testMatch">测试匹配</el-button>
        </div>
        <template v-if="preview">
          <div>
            <h3>可挂载工具</h3>
            <p class="hint">{{ preview.tools.join("、") || "没有匹配的只读工具" }}</p>
          </div>
          <div>
            <h3>技能说明</h3>
            <pre>{{ preview.guidance || "没有匹配的技能说明" }}</pre>
          </div>
        </template>
      </div>
    </template>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; display: grid; gap: 14px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero-actions { display: flex; align-items: center; gap: 10px; flex: none; }
.hero h2 { margin: 0; font-size: 16px; }
.hero p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.panel-head h2 { margin: 0; font-size: 15px; }
.panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.panel h3 { margin: 0; font-size: 13px; }
.meta, .hint { margin: 0; color: var(--muted); font-size: 12px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.form-grid label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
.form-grid .wide { grid-column: 1 / -1; }
.actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.hidden-input { display: none; }
.library-layout { display: grid; grid-template-columns: minmax(300px, 1fr) minmax(360px, 1.3fr); gap: 16px; align-items: start; }
.library-layout :deep(.el-table__row) { cursor: pointer; }
pre { margin: 0; max-height: 300px; overflow: auto; white-space: pre-wrap; word-break: break-word; padding: 12px; background: #f6f8fc; border: 1px solid var(--line); border-radius: 10px; font-size: 12px; }
code { font-size: 11px; word-break: break-all; }
.endpoint { word-break: break-all; }
.warning-alert { border-radius: 10px; }
.evidence { margin: 0; padding: 6px 12px; border-left: 3px solid var(--line); color: var(--muted); font-size: 12px; line-height: 1.6; }
@media (max-width: 1000px) { .library-layout { grid-template-columns: 1fr; } }
@media (max-width: 900px) { .form-grid { grid-template-columns: 1fr; } .hero { flex-direction: column; align-items: flex-start; } }
</style>
