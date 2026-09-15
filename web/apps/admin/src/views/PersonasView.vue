<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessageBox } from "element-plus";
import { useRoute } from "vue-router";

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
const route = useRoute();
const activeTab = computed(() => String(route.query.tab ?? "profile"));
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
interface EmotionMeta {
  key: string;
  label: string;
  emoji: string;
  color: string;
  custom?: boolean;
}
// 情绪卡片元数据：emoji 与颜色让映射一目了然；key 必须与后端 Emotion 字面量一致
const emotionMetas: EmotionMeta[] = [
  { key: "neutral", label: "平静", emoji: "😌", color: "#8a94a6" },
  { key: "happy", label: "开心", emoji: "😄", color: "#e8a23d" },
  { key: "sad", label: "难过", emoji: "😢", color: "#5b8def" },
  { key: "angry", label: "生气", emoji: "😠", color: "#e24b54" },
  { key: "surprised", label: "惊讶", emoji: "😮", color: "#9a6df2" },
  { key: "thinking", label: "思考", emoji: "🤔", color: "#2fa6a0" },
  { key: "concerned", label: "关切", emoji: "🥺", color: "#d97b93" },
];
const mapMode = ref<"visual" | "json">("visual");
function parseExpressionMap(): Record<string, string> | null {
  try {
    const value = JSON.parse(expressionMapText.value || "{}");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value as Record<string, string>;
    }
  } catch {
    // 落到下面的 null，由调用方提示
  }
  return null;
}
const visualMap = computed(() => parseExpressionMap() ?? {});
const emotionCards = computed<EmotionMeta[]>(() => [
  ...emotionMetas,
  ...Object.keys(visualMap.value)
    .filter((key) => !emotionMetas.some((meta) => meta.key === key))
    .map((key) => ({ key, label: key, emoji: "❓", color: "#8a94a6", custom: true })),
]);
function setMapping(key: string, value: string) {
  const current = { ...(parseExpressionMap() ?? {}) };
  if (value) current[key] = value;
  else delete current[key];
  expressionMapText.value = JSON.stringify(current, null, 2);
}
function removeCustomEmotion(key: string) {
  setMapping(key, "");
}
function fillIdentityMappings() {
  const current = { ...(parseExpressionMap() ?? {}) };
  for (const meta of emotionMetas) {
    if (!current[meta.key]) current[meta.key] = meta.key;
  }
  expressionMapText.value = JSON.stringify(current, null, 2);
}
function switchMapMode(mode: "visual" | "json") {
  if (mode === "visual" && !parseExpressionMap()) {
    emit("status", "表情映射 JSON 无效，修正后才能回到可视化模式", true);
    return;
  }
  mapMode.value = mode;
}
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

    <div v-if="activeTab !== 'versions'" class="panel">
      <h2>人格定义</h2>
      <p class="hint">保存只产生草稿，发布后才会用于新对话轮次。</p>
      <div class="fields">
        <label v-if="activeTab === 'profile'">名称<el-input v-model="form.name" maxlength="80" show-word-limit /></label>
        <label v-if="activeTab === 'profile'" class="wide">身份定位<el-input v-model="form.identity" type="textarea" :rows="3" /></label>
        <label v-if="activeTab === 'style'" class="wide">核心提示词<el-input v-model="form.system_prompt" type="textarea" :rows="6" /></label>
        <label v-if="activeTab === 'style'">表达风格<el-input v-model="form.speaking_style" type="textarea" :rows="3" /></label>
        <label v-if="activeTab === 'profile'">与用户的关系<el-input v-model="form.relationship" type="textarea" :rows="3" /></label>
        <label v-if="activeTab === 'boundaries'" class="wide">行为边界（每行一条）<el-input v-model="boundariesText" type="textarea" :rows="8" /></label>
        <label v-if="activeTab === 'style' || activeTab === 'motion'">声音配置标识<el-input v-model="form.voice_profile" placeholder="可选" /></label>
      </div>
      <div v-if="activeTab === 'motion'" class="motion-section">
        <div class="motion-head">
          <h3>情绪 → 表情映射</h3>
          <div class="motion-actions">
            <el-button size="small" @click="fillIdentityMappings">一键同名映射</el-button>
            <el-radio-group :model-value="mapMode" size="small" @update:model-value="switchMapMode($event as 'visual' | 'json')">
              <el-radio-button value="visual">可视化</el-radio-button>
              <el-radio-button value="json">JSON</el-radio-button>
            </el-radio-group>
          </div>
        </div>
        <p class="hint">对话判定为某种情绪时触发表情切换；未映射的情绪不切换表情。点击 ★ 设置默认情绪，模型无法判断情绪时使用。</p>
        <div v-if="mapMode === 'visual'" class="emotion-grid">
          <article
            v-for="meta in emotionCards"
            :key="meta.key"
            class="emotion-card"
            :class="{ 'is-default': form.default_emotion === meta.key, 'is-unmapped': !visualMap[meta.key], 'is-custom': meta.custom }"
            :style="{ '--emotion-color': meta.color }"
          >
            <header class="emotion-head">
              <span class="emotion-emoji">{{ meta.emoji }}</span>
              <span class="emotion-names">
                <strong>{{ meta.label }}</strong>
                <small>{{ meta.key }}</small>
              </span>
              <button
                type="button"
                class="emotion-star"
                :title="form.default_emotion === meta.key ? '当前默认情绪' : '设为默认情绪'"
                @click="form.default_emotion = meta.key"
              >{{ form.default_emotion === meta.key ? "★" : "☆" }}</button>
            </header>
            <el-input
              :model-value="visualMap[meta.key] ?? ''"
              size="small"
              clearable
              placeholder="未映射"
              @update:model-value="setMapping(meta.key, $event)"
            />
            <footer class="emotion-flow">
              <span class="arrow">→</span>
              <span v-if="visualMap[meta.key]" class="flow-chip">{{ visualMap[meta.key] }}</span>
              <span v-else class="flow-none">对话中不切换表情</span>
            </footer>
            <button
              v-if="meta.custom"
              type="button"
              class="emotion-remove"
              title="移除该自定义情绪"
              @click="removeCustomEmotion(meta.key)"
            >✕</button>
          </article>
        </div>
        <template v-else>
          <el-input v-model="expressionMapText" type="textarea" :rows="8" spellcheck="false" />
          <el-alert v-if="!parseExpressionMap()" title="JSON 格式无效，无法保存" type="error" :closable="false" />
        </template>
      </div>
      <div v-if="activeTab === 'profile'" class="profile-section">
        <h3>档案基线</h3>
        <p class="hint">身高、生日等稳定属性的默认值；对话中用户明确告知的新值会自动覆盖这里（进记忆库）。</p>
        <div class="profile-grid">
          <label v-for="field in profileFields" :key="field.key">{{ field.label }}<el-input v-model="profileValues[field.key]" :placeholder="field.placeholder" clearable /></label>
        </div>
      </div>
      <div class="row"><el-button type="primary" @click="saveDraft">校验并保存草稿</el-button></div>
    </div>

    <div v-if="activeTab === 'versions'" class="panel">
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
.motion-section { display: grid; gap: 10px; padding-top: 4px; border-top: 1px dashed var(--line); }
.motion-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
.motion-head h3 { margin: 0; font-size: 13px; }
.motion-actions { display: flex; gap: 8px; align-items: center; }
.emotion-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
.emotion-card {
  position: relative;
  display: grid;
  gap: 8px;
  align-content: start;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: linear-gradient(180deg, color-mix(in srgb, var(--emotion-color) 8%, #fff), #fff);
}
.emotion-card.is-default { border-color: var(--emotion-color); box-shadow: inset 0 0 0 1px var(--emotion-color); }
.emotion-card.is-unmapped { border-style: dashed; opacity: 0.78; }
.emotion-head { display: flex; align-items: center; gap: 8px; }
.emotion-emoji { font-size: 22px; line-height: 1; }
.emotion-names { flex: 1; display: grid; line-height: 1.25; }
.emotion-names strong { font-size: 13px; }
.emotion-names small { color: var(--muted); font-size: 10px; font-family: ui-monospace, monospace; }
.emotion-star { border: none; background: none; cursor: pointer; padding: 2px 4px; font-size: 15px; color: #c9ced9; }
.emotion-star:hover { color: #e8a23d; }
.emotion-card.is-default .emotion-star { color: #e8a23d; }
.emotion-remove {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: var(--line);
  color: var(--muted);
  font-size: 10px;
  line-height: 18px;
  cursor: pointer;
}
.emotion-remove:hover { background: var(--danger); color: #fff; }
.emotion-card.is-custom .emotion-star { display: none; }
.emotion-flow { display: flex; align-items: center; gap: 6px; min-height: 18px; font-size: 11px; }
.emotion-flow .arrow { color: var(--emotion-color); font-weight: 600; }
.flow-chip {
  padding: 1px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--emotion-color) 14%, #fff);
  color: color-mix(in srgb, var(--emotion-color) 70%, #000);
  font-family: ui-monospace, monospace;
  font-size: 11px;
  word-break: break-all;
}
.flow-none { color: var(--muted); }
small { color: var(--muted); display: block; font-size: 11px; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; border: 1px solid; }
.status.published { color: #168e59; border-color: #bfe8d2; background: #e9f8f0; }
.status.draft { color: #f2c078; border-color: #6b5325; background: #2a2113; }
.status.superseded { color: #6f63a8; border-color: #dcd7f2; background: #f1effb; }
@media (max-width: 720px) { .fields { grid-template-columns: 1fr; } .profile-grid { grid-template-columns: 1fr; } }
</style>
