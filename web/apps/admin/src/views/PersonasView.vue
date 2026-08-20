<script setup lang="ts">
import { inject, onMounted, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessageBox } from "element-plus";

interface PersonaConfig {
  schema_version: number;
  name: string;
  identity: string;
  system_prompt: string;
  speaking_style: string;
  relationship: string;
  boundaries: string[];
  default_emotion: string;
  expression_map: Record<string, string>;
  voice_profile: string | null;
  profile?: Record<string, string>;
}
interface VersionRow {
  version: number;
  status: string;
  content_hash: string;
  created_by: string;
  created_at: string;
  rollback_from_version?: number | null;
}

// Persona 管理：人格定义表单 + 版本历史（草稿/发布/回滚）。
// 表单与后端 PersonaConfig 字段一一对应；表情映射是 JSON 编辑。
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const current = ref<{ version: number; published_at: string; persona: PersonaConfig } | null>(null);
const versions = ref<VersionRow[]>([]);
const form = reactive<PersonaConfig>({
  schema_version: 1,
  name: "",
  identity: "",
  system_prompt: "",
  speaking_style: "",
  relationship: "",
  boundaries: [],
  default_emotion: "neutral",
  expression_map: {},
  voice_profile: null,
});
const emotionLabels: Record<string, string> = {
  neutral: "平静",
  happy: "开心",
  sad: "难过",
  angry: "生气",
  surprised: "惊讶",
  thinking: "思考",
  concerned: "关切",
};
const statusLabels: Record<string, string> = {
  published: "已发布",
  draft: "草稿",
  superseded: "已废止",
};
const boundariesText = ref("");
const expressionMapText = ref("{}");
// 档案基线：key 与记忆库助手事实 fact_key 对齐，对话中用户明确告知的值会覆盖这里
const profileFields: Array<{ key: string; label: string; placeholder: string }> = [
  { key: "profile.height", label: "身高", placeholder: "例如 165 厘米" },
  { key: "profile.weight", label: "体重", placeholder: "例如 50 公斤" },
  { key: "profile.birthday", label: "生日", placeholder: "例如 5月20日" },
  { key: "profile.name", label: "名字", placeholder: "例如 小星" },
  { key: "profile.nickname", label: "昵称", placeholder: "例如 星星" },
  { key: "profile.measurements", label: "三围", placeholder: "例如 90-60-88" },
];
const profileValues = reactive<Record<string, string>>({});

async function load() {
  try {
    const [snapshot, history] = await Promise.all([
      api.request<typeof current.value>("/api/v1/admin/personas/current"),
      api.request<VersionRow[]>("/api/v1/admin/personas/versions"),
    ]);
    current.value = snapshot;
    versions.value = history;
    Object.assign(form, snapshot?.persona);
    boundariesText.value = form.boundaries.join("\n");
    expressionMapText.value = JSON.stringify(form.expression_map ?? {}, null, 2);
    for (const field of profileFields) {
      profileValues[field.key] = snapshot?.persona?.profile?.[field.key] ?? "";
    }
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
}

function collect(): PersonaConfig {
  let expressionMap: Record<string, string>;
  try {
    expressionMap = JSON.parse(expressionMapText.value || "{}") as Record<string, string>;
  } catch {
    throw new Error("表情映射必须是有效 JSON");
  }
  const profile: Record<string, string> = {};
  for (const field of profileFields) {
    const value = (profileValues[field.key] ?? "").trim();
    if (value) profile[field.key] = value;
  }
  for (const [key, value] of Object.entries(form.profile ?? {})) {
    // 保留非预设槽位的自定义档案键
    if (!profileFields.some((field) => field.key === key) && value) profile[key] = value;
  }
  return {
    ...form,
    boundaries: boundariesText.value.split("\n").map((line) => line.trim()).filter(Boolean),
    expression_map: expressionMap,
    voice_profile: form.voice_profile || null,
    profile,
  };
}

async function saveDraft() {
  try {
    const candidate = collect();
    await api.request("/api/v1/admin/personas/validate", {
      method: "POST",
      body: JSON.stringify(candidate),
    });
    const draft = await api.request<VersionRow & { persona: PersonaConfig }>(
      "/api/v1/admin/personas/versions",
      {
        method: "POST",
        body: JSON.stringify(candidate),
      },
    );
    emit("status", `草稿 v${draft.version} 已保存，发布后生效`);
    // 草稿尚未发布，/current 仍指向旧发布版；表单必须保持刚保存的草稿
    // 内容，只刷新版本列表，否则编辑内容会被旧发布版覆盖回默认值。
    Object.assign(form, draft.persona);
    boundariesText.value = draft.persona.boundaries.join("\n");
    expressionMapText.value = JSON.stringify(draft.persona.expression_map ?? {}, null, 2);
    for (const field of profileFields) {
      profileValues[field.key] = draft.persona.profile?.[field.key] ?? "";
    }
    versions.value = await api.request<VersionRow[]>("/api/v1/admin/personas/versions");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "保存失败", true);
  }
}

async function publish(version: number) {
  try {
    await ElMessageBox.confirm(`发布人格草稿 v${version}？`, "确认发布", { type: "warning", confirmButtonText: "发布", cancelButtonText: "取消" });
  } catch {
    return;
  }
  try {
    await api.request(`/api/v1/admin/personas/versions/${version}/publish`, { method: "POST" });
    emit("status", `人格 v${version} 已发布`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "发布失败", true);
  }
}

async function rollback(version: number) {
  try {
    await ElMessageBox.confirm(`回滚到人格 v${version}？`, "确认回滚", { type: "warning", confirmButtonText: "回滚", cancelButtonText: "取消" });
  } catch {
    return;
  }
  try {
    const result = await api.request<{ version: number }>(
      `/api/v1/admin/personas/versions/${version}/rollback`,
      { method: "POST" },
    );
    emit("status", `已创建回滚版本 v${result.version}`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "回滚失败", true);
  }
}

onMounted(load);
</script>

<template>
  <section class="content" v-if="current">
    <div class="stats">
      <article><span>当前版本</span><strong>v{{ current.version }}</strong></article>
      <article><span>角色名称</span><strong>{{ form.name }}</strong></article>
      <article><span>默认情绪</span><strong>{{ emotionLabels[form.default_emotion] ?? form.default_emotion }}</strong></article>
    </div>

    <div class="panel">
      <h2>人格定义</h2>
      <p class="hint">保存只产生草稿，发布后才会用于新对话轮次。</p>
      <div class="fields">
        <label>名称<el-input v-model="form.name" maxlength="80" show-word-limit /></label>
        <label>默认情绪
          <el-select v-model="form.default_emotion">
            <el-option v-for="emotion in ['neutral', 'happy', 'sad', 'angry', 'surprised', 'thinking', 'concerned']" :key="emotion" :label="emotionLabels[emotion] ?? emotion" :value="emotion" />
          </el-select>
        </label>
        <label class="wide">身份定位<el-input v-model="form.identity" type="textarea" :rows="3" /></label>
        <label class="wide">核心提示词<el-input v-model="form.system_prompt" type="textarea" :rows="6" /></label>
        <label>表达风格<el-input v-model="form.speaking_style" type="textarea" :rows="3" /></label>
        <label>与用户的关系<el-input v-model="form.relationship" type="textarea" :rows="3" /></label>
        <label class="wide">行为边界（每行一条）<el-input v-model="boundariesText" type="textarea" :rows="4" /></label>
        <label>声音配置标识<el-input v-model="form.voice_profile" placeholder="可选" /></label>
        <label class="wide">情绪 → 表情映射（JSON）<el-input v-model="expressionMapText" type="textarea" :rows="4" spellcheck="false" /></label>
      </div>
      <div class="profile-section">
        <h3>档案基线</h3>
        <p class="hint">身高、生日等稳定属性的默认值；对话中用户明确告知的新值会自动覆盖这里（进记忆库）。</p>
        <div class="profile-grid">
          <label v-for="field in profileFields" :key="field.key">{{ field.label }}<el-input v-model="profileValues[field.key]" :placeholder="field.placeholder" clearable /></label>
        </div>
      </div>
      <div class="row"><el-button type="primary" @click="saveDraft">校验并保存草稿</el-button></div>
    </div>

    <div class="panel">
      <h2>版本历史</h2>
      <el-table :data="versions" style="width:100%">
        <el-table-column label="版本" width="120">
          <template #default="{ row }"><strong>v{{ row.version }}</strong><small v-if="row.rollback_from_version">↩ v{{ row.rollback_from_version }}</small></template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }"><el-tag :type="row.status === 'published' ? 'success' : row.status === 'draft' ? 'warning' : 'info'">{{ statusLabels[row.status] ?? row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="created_by" label="创建人" min-width="140" />
        <el-table-column label="时间" min-width="180"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template></el-table-column>
        <el-table-column label="操作" width="150"><template #default="{ row }"><el-button v-if="row.status === 'draft'" type="primary" size="small" @click="publish(row.version)">发布</el-button><el-button v-else size="small" @click="rollback(row.version)">回滚到此版</el-button></template></el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.stats article { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 18px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; }
.panel h2 { margin: 0; font-size: 15px; }
.hint { color: var(--muted); font-size: 12px; margin: 0; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.fields .wide { grid-column: 1 / -1; }
.fields label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
.profile-section { display: grid; gap: 8px; padding-top: 4px; border-top: 1px dashed var(--line); }
.profile-section h3 { margin: 0; font-size: 13px; }
.profile-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.profile-grid label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
.row { display: flex; gap: 10px; }
small { color: var(--muted); display: block; font-size: 11px; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; border: 1px solid; }
.status.published { color: #168e59; border-color: #bfe8d2; background: #e9f8f0; }
.status.draft { color: #f2c078; border-color: #6b5325; background: #2a2113; }
.status.superseded { color: #6f63a8; border-color: #dcd7f2; background: #f1effb; }
@media (max-width: 720px) { .fields { grid-template-columns: 1fr; } .profile-grid { grid-template-columns: 1fr; } }
</style>
