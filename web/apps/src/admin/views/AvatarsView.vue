<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { AdminApi } from "@aria/shared";
import AvatarPreview from "../components/AvatarPreview.vue";

interface PackItem {
  id: string;
  name: string;
  archetype: string;
  engine: string;
  manifest: {
    emotions?: string[];
    gestures?: string[];
    outfits?: string[];
    customizable_slots?: string[];
    assets?: { thumbnail?: string; emotions?: Record<string, string>; model?: string };
  };
  content_hash: string;
  license: Record<string, unknown>;
  built_in: boolean;
  installed_at: string;
}

interface InstanceItem {
  id: string;
  owner: string;
  name: string;
  pack_id: string;
  customization: Record<string, unknown>;
  voice_profile_id: string | null;
  theme_id: string | null;
  version: number;
  status: "active" | "preview" | "archived";
  created_at: string;
}

interface BindingItem { persona_id: number; avatar: InstanceItem; is_default: boolean; created_at: string }
interface CurrentPersona { version: number; persona: { name: string } }

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const packs = ref<PackItem[]>([]);
const instances = ref<InstanceItem[]>([]);
const bindings = ref<BindingItem[]>([]);
const currentPersona = ref<CurrentPersona | null>(null);
const loading = ref(false);
const saving = ref(false);
const createDialog = ref(false);
const editDialog = ref(false);
const uploadDialog = ref(false);
const previewDialog = ref(false);
const previewInstance = ref<InstanceItem | null>(null);
const uploadFile = ref<File | null>(null);
const uploadForm = reactive({ kind: "static" as "static" | "live2d", name: "", rights_confirmed: false });
const editingId = ref<string | null>(null);
const createForm = reactive({ pack_id: "", name: "", customization: "{}", voice_profile_id: "" });
const editForm = reactive({ name: "", customization: "{}", voice_profile_id: "", status: "active" as InstanceItem["status"] });
const defaultAvatarId = computed(() => bindings.value.find((item) => item.is_default)?.avatar.id ?? null);
const boundAvatarIds = computed(() => new Set(bindings.value.map((item) => item.avatar.id)));
const engineLabels: Record<string, string> = { static: "静态立绘", live2d: "Live2D", vrm: "VRM", abstract: "抽象" };
const statusLabels: Record<InstanceItem["status"], string> = { active: "可用", preview: "预览中", archived: "已归档" };
const packById = computed(() => new Map(packs.value.map((pack) => [pack.id, pack])));
const previewPack = computed(() => previewInstance.value ? packById.value.get(previewInstance.value.pack_id) ?? null : null);

function errorText(error: unknown, fallback: string) { return error instanceof Error ? error.message : fallback }
function statusLabel(value: unknown) { return statusLabels[value as InstanceItem["status"]] ?? String(value) }

function parseCustomization(value: string) {
  let parsed: unknown;
  try { parsed = JSON.parse(value || "{}"); } catch { throw new Error("定制参数必须是有效 JSON") }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("定制参数必须是 JSON 对象");
  return parsed as Record<string, unknown>;
}

async function loadData() {
  loading.value = true;
  try {
    const [packRows, instanceRows, persona] = await Promise.all([
      api.request<PackItem[]>("/api/v1/admin/avatars/packs"),
      api.request<InstanceItem[]>("/api/v1/admin/avatars/instances"),
      api.request<CurrentPersona>("/api/v1/admin/personas/current"),
    ]);
    packs.value = packRows;
    instances.value = instanceRows;
    currentPersona.value = persona;
    bindings.value = await api.request<BindingItem[]>(`/api/v1/admin/avatars/bindings?persona_id=${persona.version}`);
  } catch (error) { emit("status", errorText(error, "形象中心加载失败"), true); }
  finally { loading.value = false; }
}

function openCreate(pack?: PackItem) {
  createForm.pack_id = pack?.id ?? packs.value[0]?.id ?? "";
  createForm.name = pack ? `${pack.name} · 我的形象` : "";
  createForm.customization = "{}";
  createForm.voice_profile_id = "";
  createDialog.value = true;
}

async function createInstance() {
  if (!createForm.pack_id || !createForm.name.trim()) { emit("status", "请选择形象包并填写名称", true); return }
  saving.value = true;
  try {
    await api.request("/api/v1/admin/avatars/instances", { method: "POST", body: JSON.stringify({
      pack_id: createForm.pack_id,
      name: createForm.name.trim(),
      customization: parseCustomization(createForm.customization),
      voice_profile_id: createForm.voice_profile_id.trim() || null,
    }) });
    ElMessage.success("形象实例已创建");
    createDialog.value = false;
    await loadData();
  } catch (error) { emit("status", errorText(error, "创建失败"), true); }
  finally { saving.value = false; }
}

function openUpload() {
  uploadForm.kind = "static";
  uploadForm.name = "";
  uploadForm.rights_confirmed = false;
  uploadFile.value = null;
  uploadDialog.value = true;
}

function selectUploadFile(event: Event) {
  uploadFile.value = (event.target as HTMLInputElement).files?.[0] ?? null;
  if (!uploadForm.name && uploadFile.value) uploadForm.name = uploadFile.value.name.replace(/\.(png|jpe?g|webp|zip)$/i, "");
}

async function importAvatar() {
  if (!uploadFile.value || !uploadForm.name.trim()) { emit("status", "请选择文件并填写形象名称", true); return }
  if (!uploadForm.rights_confirmed) { emit("status", "请先确认素材使用权", true); return }
  const body = new FormData();
  body.append("name", uploadForm.name.trim());
  body.append("rights_confirmed", "true");
  body.append("file", uploadFile.value);
  saving.value = true;
  try {
    const result = await api.request<{ instance: InstanceItem }>(`/api/v1/admin/avatars/imports/${uploadForm.kind}`, { method: "POST", body });
    ElMessage.success(uploadForm.kind === "live2d" ? "Live2D 模型包已安装" : "自定义立绘已上传");
    uploadDialog.value = false;
    await loadData();
    openPreview(result.instance);
  } catch (error) { emit("status", errorText(error, "导入失败"), true); }
  finally { saving.value = false; }
}

function openPreview(value: unknown) {
  previewInstance.value = value as InstanceItem;
  previewDialog.value = true;
}

function openEdit(value: unknown) {
  const row = value as InstanceItem;
  editingId.value = row.id;
  editForm.name = row.name;
  editForm.customization = JSON.stringify(row.customization, null, 2);
  editForm.voice_profile_id = row.voice_profile_id ?? "";
  editForm.status = row.status;
  editDialog.value = true;
}

async function saveEdit() {
  if (!editingId.value) return;
  saving.value = true;
  try {
    await api.request(`/api/v1/admin/avatars/instances/${editingId.value}`, { method: "PATCH", body: JSON.stringify({
      name: editForm.name.trim(),
      customization: parseCustomization(editForm.customization),
      voice_profile_id: editForm.voice_profile_id.trim() || null,
      status: editForm.status,
    }) });
    ElMessage.success("形象设置已保存");
    editDialog.value = false;
    await loadData();
  } catch (error) { emit("status", errorText(error, "保存失败"), true); }
  finally { saving.value = false; }
}

async function applyInstance(value: unknown) {
  const row = value as InstanceItem;
  if (!currentPersona.value) return;
  try {
    await ElMessageBox.confirm(`将“${row.name}”设为 ${currentPersona.value.persona.name} 的当前形象？`, "切换形象", { type: "warning", confirmButtonText: "应用", cancelButtonText: "取消" });
  } catch { return }
  try {
    await api.request(`/api/v1/admin/avatars/instances/${row.id}/bind`, { method: "POST", body: JSON.stringify({ persona_id: currentPersona.value.version, is_default: true }) });
    ElMessage.success("当前形象已切换");
    await loadData();
  } catch (error) { emit("status", errorText(error, "切换失败"), true); }
}

async function unbindInstance(value: unknown) {
  const row = value as InstanceItem;
  if (!currentPersona.value) return;
  try {
    await ElMessageBox.confirm(`取消“${row.name}”与当前人格的绑定？`, "取消绑定", { type: "warning", confirmButtonText: "取消绑定", cancelButtonText: "返回" });
  } catch { return }
  try {
    await api.request(`/api/v1/admin/avatars/instances/${row.id}/bindings/${currentPersona.value.version}`, { method: "DELETE" });
    ElMessage.success("绑定已取消");
    await loadData();
  } catch (error) { emit("status", errorText(error, "取消绑定失败"), true); }
}

async function deleteInstance(value: unknown) {
  const row = value as InstanceItem;
  try {
    await ElMessageBox.confirm(`确认删除“${row.name}”？此操作不可撤销。`, "删除形象", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  } catch { return }
  try {
    await api.request(`/api/v1/admin/avatars/instances/${row.id}`, { method: "DELETE" });
    ElMessage.success("形象实例已删除");
    await loadData();
  } catch (error) { emit("status", errorText(error, "删除失败"), true); }
}

onMounted(loadData);
</script>

<template>
  <section class="content" v-loading="loading">
    <div class="hero">
      <div><p class="eyebrow">AVATAR CENTER</p><h2>形象库</h2><p class="hint">形象与人格、记忆彼此独立。切换外观不会改变关系和对话历史。</p></div>
      <div class="current-card"><span>当前人格</span><strong>{{ currentPersona?.persona.name ?? "—" }}</strong><small>Persona v{{ currentPersona?.version ?? "—" }}</small></div>
    </div>

    <div class="section-head"><div><h3>形象库</h3><p>从内置形象包创建自己的实例。</p></div></div>
    <div class="packs-grid">
      <article v-for="pack in packs" :key="pack.id" class="pack-card">
        <div class="pack-preview" :class="pack.engine">
          <img v-if="pack.manifest.assets?.thumbnail" :src="pack.manifest.assets.thumbnail" :alt="`${pack.name} 缩略图`" />
          <span v-else>{{ pack.name[0] }}</span>
        </div>
        <div class="pack-body">
          <div class="pack-title"><strong>{{ pack.name }}</strong><el-tag v-if="pack.built_in" size="small" type="success">内置</el-tag></div>
          <p>{{ engineLabels[pack.engine] || pack.engine }} · {{ pack.archetype }}</p>
          <div class="capabilities"><span>{{ pack.manifest.emotions?.length ?? 0 }} 种情绪</span><span>{{ pack.manifest.gestures?.length ?? 0 }} 个动作</span><span>{{ pack.manifest.outfits?.length ?? 0 }} 套服装</span></div>
          <el-button type="primary" plain size="small" @click="openCreate(pack)">以此创建</el-button>
        </div>
      </article>
    </div>

    <div class="section-head instances-head"><div><h3>我的形象</h3><p>管理实例并选择当前人格正在使用的形象。</p></div><div class="head-actions"><el-button @click="openUpload">上传自定义形象</el-button><el-button type="primary" @click="openCreate()">创建实例</el-button></div></div>
    <el-empty v-if="!loading && instances.length === 0" description="还没有形象实例" />
    <el-table v-else :data="instances" stripe>
      <el-table-column prop="name" label="名称" min-width="170"><template #default="{ row }"><div class="instance-name"><strong>{{ row.name }}</strong><el-tag v-if="row.id === defaultAvatarId" size="small" type="success">当前</el-tag></div></template></el-table-column>
      <el-table-column prop="pack_id" label="来源包" width="130" />
      <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag size="small" type="info">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
      <el-table-column prop="voice_profile_id" label="语音配置" min-width="120"><template #default="{ row }">{{ row.voice_profile_id || "继承人格" }}</template></el-table-column>
      <el-table-column prop="version" label="版本" width="70"><template #default="{ row }">v{{ row.version }}</template></el-table-column>
      <el-table-column label="操作" width="310" fixed="right"><template #default="{ row }">
        <el-button link size="small" @click="openPreview(row)">预览</el-button>
        <el-button v-if="row.id !== defaultAvatarId" link type="primary" size="small" @click="applyInstance(row)">设为当前</el-button>
        <el-button v-if="boundAvatarIds.has(row.id)" link size="small" @click="unbindInstance(row)">取消绑定</el-button>
        <el-button link size="small" @click="openEdit(row)">编辑</el-button>
        <el-button link type="danger" size="small" :disabled="row.id === defaultAvatarId" @click="deleteInstance(row)">删除</el-button>
      </template></el-table-column>
    </el-table>

    <el-dialog v-model="createDialog" title="创建形象实例" width="480px">
      <el-form label-width="90px">
        <el-form-item label="形象包" required><el-select v-model="createForm.pack_id" placeholder="选择形象包"><el-option v-for="pack in packs" :key="pack.id" :label="pack.name" :value="pack.id" /></el-select></el-form-item>
        <el-form-item label="名称" required><el-input v-model="createForm.name" maxlength="160" /></el-form-item>
        <el-form-item label="语音配置"><el-input v-model="createForm.voice_profile_id" placeholder="留空则继承人格配置" /></el-form-item>
        <el-form-item label="定制参数"><el-input v-model="createForm.customization" type="textarea" :rows="5" spellcheck="false" /><small class="field-tip">仅可填写形象包声明的可定制槽位。</small></el-form-item>
      </el-form>
      <template #footer><el-button @click="createDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="createInstance">创建</el-button></template>
    </el-dialog>

    <el-dialog v-model="uploadDialog" title="上传自定义形象" width="520px">
      <el-form label-width="94px">
        <el-form-item label="资源类型"><el-radio-group v-model="uploadForm.kind"><el-radio-button value="static">静态立绘</el-radio-button><el-radio-button value="live2d">Live2D 包</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="形象名称" required><el-input v-model="uploadForm.name" maxlength="160" /></el-form-item>
        <el-form-item label="选择文件" required><input class="file-input" type="file" :accept="uploadForm.kind === 'static' ? '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp' : '.zip,application/zip'" @change="selectUploadFile" /><small class="field-tip">{{ uploadForm.kind === 'static' ? 'PNG / JPEG / WebP，最大 10 MB；上传后会清除 EXIF。' : 'ZIP，最大 50 MB；支持同时包含 FREE/PRO 的官方发行包，将优先选择 PRO runtime。' }}</small></el-form-item>
        <el-form-item><el-checkbox v-model="uploadForm.rights_confirmed">我确认拥有该素材的使用与展示权限</el-checkbox></el-form-item>
      </el-form>
      <template #footer><el-button @click="uploadDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="importAvatar">上传并预览</el-button></template>
    </el-dialog>

    <el-dialog v-model="previewDialog" :title="`预览 · ${previewInstance?.name ?? ''}`" width="min(720px, 92vw)">
      <AvatarPreview v-if="previewInstance && previewPack" :name="previewInstance.name" :engine="previewPack.engine" :assets="previewPack.manifest.assets" />
      <el-empty v-else description="预览资源不可用" />
      <template #footer><span v-if="previewPack" class="preview-meta">{{ engineLabels[previewPack.engine] || previewPack.engine }} · {{ previewPack.name }}</span><el-button @click="previewDialog = false">关闭</el-button><el-button v-if="previewInstance?.id !== defaultAvatarId" type="primary" @click="applyInstance(previewInstance)">设为当前形象</el-button></template>
    </el-dialog>

    <el-dialog v-model="editDialog" title="编辑形象实例" width="480px">
      <el-form label-width="90px">
        <el-form-item label="名称" required><el-input v-model="editForm.name" maxlength="160" /></el-form-item>
        <el-form-item label="状态"><el-select v-model="editForm.status"><el-option label="可用" value="active" /><el-option label="预览中" value="preview" /><el-option label="已归档" value="archived" /></el-select></el-form-item>
        <el-form-item label="语音配置"><el-input v-model="editForm.voice_profile_id" placeholder="留空则继承人格配置" /></el-form-item>
        <el-form-item label="定制参数"><el-input v-model="editForm.customization" type="textarea" :rows="6" spellcheck="false" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="editDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.content{padding:20px 24px 32px;overflow-y:auto}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:26px;padding:22px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(135deg,color-mix(in srgb,var(--panel) 88%,#9a86d2),var(--panel))}.hero h2{margin:2px 0 6px;font-size:24px}.eyebrow{margin:0;color:var(--muted);font-size:11px;letter-spacing:.16em}.hint,.section-head p,.pack-body p{margin:4px 0 0;color:var(--muted);font-size:12px}.current-card{min-width:150px;display:grid;gap:3px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.current-card span,.current-card small{color:var(--muted);font-size:11px}.current-card strong{font-size:16px}.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.section-head h3{margin:0;font-size:16px}.head-actions{display:flex;gap:8px}.instances-head{margin-top:28px}.packs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}.pack-card{display:grid;grid-template-columns:92px 1fr;overflow:hidden;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.pack-preview{display:grid;place-items:center;min-height:132px}.pack-preview span{display:grid;place-items:center;width:56px;height:56px;border-radius:50%;color:#fff;font-size:22px;font-weight:700;box-shadow:0 10px 30px #0003}.pack-preview.static{background:linear-gradient(145deg,#f3eee5,#e9c4bc)}.pack-preview.static span{background:linear-gradient(135deg,#f2b6aa,#b97679)}.pack-preview.abstract{background:linear-gradient(145deg,#17192b,#242044)}.pack-preview.abstract span{background:radial-gradient(circle at 35% 30%,#fff,#75d8d7 25%,#9a86d2 65%,#28244c)}.pack-preview.live2d,.pack-preview.vrm{background:linear-gradient(145deg,#d9d2f0,#b8e4e3)}.pack-preview.live2d span,.pack-preview.vrm span{background:linear-gradient(135deg,#9a86d2,#75d8d7)}.pack-body{display:grid;align-content:center;gap:9px;padding:14px}.pack-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.capabilities{display:flex;flex-wrap:wrap;gap:5px}.capabilities span{padding:3px 7px;border-radius:999px;background:var(--panel2);color:var(--muted);font-size:10px}.instance-name{display:flex;align-items:center;gap:8px}.field-tip{display:block;width:100%;margin-top:5px;color:var(--muted)}.file-input{max-width:100%;color:var(--text)}.preview-meta{margin-right:auto;color:var(--muted);font-size:12px}@media(max-width:720px){.content{padding:16px}.hero{flex-direction:column}.current-card{width:100%}.pack-card{grid-template-columns:76px 1fr}.section-head{align-items:flex-start}.head-actions{flex-direction:column}}
.pack-preview{overflow:hidden}.pack-preview img{width:100%;height:132px;object-fit:cover;object-position:center 18%}
</style>
