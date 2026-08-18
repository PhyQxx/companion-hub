<script setup lang="ts">
import { inject, onMounted, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";

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
const boundariesText = ref("");
const expressionMapText = ref("{}");

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
  return {
    ...form,
    boundaries: boundariesText.value.split("\n").map((line) => line.trim()).filter(Boolean),
    expression_map: expressionMap,
    voice_profile: form.voice_profile || null,
  };
}

async function saveDraft() {
  try {
    const candidate = collect();
    await api.request("/api/v1/admin/personas/validate", {
      method: "POST",
      body: JSON.stringify(candidate),
    });
    const draft = await api.request<VersionRow>("/api/v1/admin/personas/versions", {
      method: "POST",
      body: JSON.stringify(candidate),
    });
    emit("status", `草稿 v${draft.version} 已保存，发布后生效`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "保存失败", true);
  }
}

async function publish(version: number) {
  if (!confirm(`发布 Persona 草稿 v${version}？`)) return;
  try {
    await api.request(`/api/v1/admin/personas/versions/${version}/publish`, { method: "POST" });
    emit("status", `Persona v${version} 已发布`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "发布失败", true);
  }
}

async function rollback(version: number) {
  if (!confirm(`回滚到 Persona v${version}？`)) return;
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
      <article><span>默认情绪</span><strong>{{ form.default_emotion }}</strong></article>
    </div>

    <div class="panel">
      <h2>人格定义</h2>
      <p class="hint">保存只产生草稿，发布后才会用于新对话轮次。</p>
      <div class="fields">
        <label>名称<input v-model="form.name" maxlength="80" /></label>
        <label>默认情绪
          <select v-model="form.default_emotion">
            <option v-for="emotion in ['neutral', 'happy', 'sad', 'angry', 'surprised', 'thinking', 'concerned']" :key="emotion" :value="emotion">{{ emotion }}</option>
          </select>
        </label>
        <label class="wide">身份定位<textarea v-model="form.identity" rows="3" /></label>
        <label class="wide">核心提示词<textarea v-model="form.system_prompt" rows="6" /></label>
        <label>表达风格<textarea v-model="form.speaking_style" rows="3" /></label>
        <label>与用户的关系<textarea v-model="form.relationship" rows="3" /></label>
        <label class="wide">行为边界（每行一条）<textarea v-model="boundariesText" rows="4" /></label>
        <label>声音配置标识<input v-model="form.voice_profile" placeholder="可选" /></label>
        <label class="wide">情绪 → 表情映射（JSON）<textarea v-model="expressionMapText" rows="4" spellcheck="false" /></label>
      </div>
      <div class="row"><button class="primary" @click="saveDraft">校验并保存草稿</button></div>
    </div>

    <div class="panel">
      <h2>版本历史</h2>
      <table>
        <thead><tr><th>版本</th><th>状态</th><th>创建人</th><th>时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in versions" :key="item.version">
            <td><strong>v{{ item.version }}</strong><small v-if="item.rollback_from_version"> ↩ v{{ item.rollback_from_version }}</small></td>
            <td><span class="status" :class="item.status">{{ item.status }}</span></td>
            <td>{{ item.created_by }}</td>
            <td><small>{{ new Date(item.created_at).toLocaleString() }}</small></td>
            <td>
              <button v-if="item.status === 'draft'" class="primary" @click="publish(item.version)">发布</button>
              <button v-else @click="rollback(item.version)">回滚到此版</button>
            </td>
          </tr>
        </tbody>
      </table>
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
textarea { width: 100%; resize: vertical; }
.row { display: flex; gap: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; padding: 8px; border-bottom: 1px solid var(--line); }
td { padding: 8px; border-bottom: 1px solid #1c1f2b; }
small { color: var(--muted); display: block; font-size: 11px; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; border: 1px solid; }
.status.published { color: #7ee2a8; border-color: #2c5c41; background: #12241b; }
.status.draft { color: #f2c078; border-color: #6b5325; background: #2a2113; }
.status.superseded { color: #b7a6f0; border-color: #4b3f76; background: #1d1830; }
@media (max-width: 720px) { .fields { grid-template-columns: 1fr; } }
</style>
