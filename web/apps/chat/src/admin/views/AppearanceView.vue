<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import {
  AdminApi,
  DEFAULT_THEME_SCHEDULE,
  THEME_OPTIONS,
  broadcastThemePreference,
  type ThemePreference,
  type ThemeSchedule,
  type UiThemeItem,
  type UiThemePreference,
} from "@aria/shared";

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const themes = ref<UiThemeItem[]>([]);
const selected = ref<ThemePreference>("pure-light");
const saved = ref<ThemePreference>("pure-light");
const schedule = ref<ThemeSchedule>({ ...DEFAULT_THEME_SCHEDULE });
const savedSchedule = ref<ThemeSchedule>({ ...DEFAULT_THEME_SCHEDULE });
const loading = ref(false);
const saving = ref(false);

const cards = computed(() => THEME_OPTIONS.map((option) => {
  const theme = themes.value.find((item) => item.key === option.value);
  return {
    ...option,
    description: option.value === "system"
      ? "根据设备的浅色或深色偏好自动切换。"
      : option.value === "scheduled"
        ? "按时段自动切换：白天用纯净明亮，夜晚用静夜紫。"
        : theme?.definition.description ?? "内置主题",
    swatches: option.value === "system"
      ? ["#f6f8fc", "#ffffff", "#141824", "#7c8ff5"]
      : option.value === "scheduled"
        ? ["#f6f8fc", "#ffffff", "#0c0e15", "#7c8ff5"]
        : theme?.definition.swatches ?? [],
    mode: option.value === "system" ? "自动" : option.value === "scheduled" ? "定时" : (theme?.mode === "dark" ? "深色" : "浅色"),
    version: theme?.version ?? 1,
  };
}));

const scheduleChanged = computed(() =>
  selected.value !== "scheduled"
    ? false
    : schedule.value.light_time !== savedSchedule.value.light_time
      || schedule.value.dark_time !== savedSchedule.value.dark_time,
);
const hasChanges = computed(() => selected.value !== saved.value || scheduleChanged.value);

function errorText(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

async function loadData() {
  loading.value = true;
  try {
    const [themeItems, preference] = await Promise.all([
      api.request<UiThemeItem[]>("/api/v1/admin/ui/themes"),
      api.request<UiThemePreference>("/api/v1/admin/ui/preferences"),
    ]);
    themes.value = themeItems;
    selected.value = preference.selection;
    saved.value = preference.selection;
    if (preference.schedule) {
      schedule.value = { ...preference.schedule };
      savedSchedule.value = { ...preference.schedule };
    }
  } catch (error) {
    emit("status", errorText(error, "主题中心加载失败"), true);
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (selected.value === "scheduled" && schedule.value.light_time === schedule.value.dark_time) {
    emit("status", "定时主题的明暗切换时刻不能相同", true);
    return;
  }
  saving.value = true;
  try {
    const body: { selection: ThemePreference; light_time?: string; dark_time?: string } = {
      selection: selected.value,
    };
    if (selected.value === "scheduled") {
      body.light_time = schedule.value.light_time;
      body.dark_time = schedule.value.dark_time;
    }
    const preference = await api.request<UiThemePreference>("/api/v1/admin/ui/preferences", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    saved.value = preference.selection;
    selected.value = preference.selection;
    if (preference.schedule) {
      schedule.value = { ...preference.schedule };
      savedSchedule.value = { ...preference.schedule };
    }
    broadcastThemePreference(
      preference.selection,
      preference.schedule ?? undefined,
    );
    ElMessage.success("账户主题已发布并同步到聊天端");
    emit("status", "主题配置已保存");
  } catch (error) {
    emit("status", errorText(error, "主题保存失败"), true);
  } finally {
    saving.value = false;
  }
}

function restore() {
  selected.value = saved.value;
  schedule.value = { ...savedSchedule.value };
}

onMounted(loadData);
</script>

<template>
  <section class="appearance-page" v-loading="loading">
    <header class="hero">
      <div>
        <p class="eyebrow">APPEARANCE</p>
        <h2>主题中心</h2>
        <p>选择账户主题。保存后，已打开的聊天端会立即切换，其他终端将在下次加载时同步。</p>
      </div>
      <div class="sync-state">
        <span class="sync-dot" />
        <div><strong>账户同步</strong><small>服务端持久化</small></div>
      </div>
    </header>

    <div class="section-heading">
      <div><h3>内置主题</h3><p>主题只改变界面表现，不会影响人格、记忆或隐私策略。</p></div>
      <div class="actions"><el-button :disabled="!hasChanges" @click="restore">撤销</el-button><el-button type="primary" :loading="saving" :disabled="!hasChanges" @click="save">保存并应用</el-button></div>
    </div>

    <div class="theme-grid">
      <button
        v-for="card in cards"
        :key="card.value"
        type="button"
        class="theme-card"
        :class="[{ selected: selected === card.value }, `theme-${card.value}`]"
        :aria-pressed="selected === card.value"
        @click="selected = card.value"
      >
        <div class="theme-preview">
          <div class="preview-sidebar"><span /><span /><span /></div>
          <div class="preview-main"><span class="preview-bubble assistant" /><span class="preview-bubble user" /><span class="preview-input" /></div>
        </div>
        <div class="theme-body">
          <div class="theme-title"><strong>{{ card.label }}</strong><el-tag v-if="selected === card.value" size="small" type="primary">已选择</el-tag></div>
          <p>{{ card.description }}</p>
          <div class="theme-meta"><span>{{ card.mode }}</span><span v-if="card.value !== 'scheduled'">v{{ card.version }}</span><span v-else>{{ schedule.light_time }} / {{ schedule.dark_time }}</span></div>
          <div class="swatches"><i v-for="color in card.swatches" :key="color" :style="{ background: color }" /></div>
        </div>
      </button>
    </div>

    <div v-if="selected === 'scheduled'" class="schedule-panel">
      <div class="schedule-head"><h3>切换时段</h3><p>按聊天端本地时钟生效：{{ schedule.light_time }} 起用纯净明亮，{{ schedule.dark_time }} 起用静夜紫（支持亮窗跨夜）。</p></div>
      <div class="schedule-fields">
        <label>明亮开始<el-time-select v-model="schedule.light_time" start="00:00" step="00:30" end="23:30" /></label>
        <label>深色开始<el-time-select v-model="schedule.dark_time" start="00:00" step="00:30" end="23:30" /></label>
      </div>
    </div>

    <el-alert class="scope-note" title="当前版本支持内置主题、跟随系统与定时切换。自定义色板与导入导出将在下一阶段开放。" type="info" :closable="false" show-icon />
  </section>
</template>

<style scoped>
.appearance-page{padding:20px 24px 32px;overflow-y:auto}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;padding:22px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(135deg,#fff,#eef2ff)}.hero h2{margin:2px 0 7px;font-size:24px}.hero p{margin:0;color:var(--muted);font-size:12px;line-height:1.6}.eyebrow{color:var(--accent)!important;font-size:10px!important;font-weight:700;letter-spacing:.16em}.sync-state{display:flex;align-items:center;gap:10px;min-width:150px;padding:11px 13px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.sync-state div{display:grid;gap:2px}.sync-state strong{font-size:12px}.sync-state small{color:var(--muted);font-size:10px}.sync-dot{width:9px;height:9px;border-radius:50%;background:#26b873;box-shadow:0 0 0 4px #26b87318}.section-heading{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:26px 0 13px}.section-heading h3,.section-heading p{margin:0}.section-heading h3{font-size:16px}.section-heading p{margin-top:5px;color:var(--muted);font-size:12px}.actions{display:flex;gap:8px}.theme-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}.theme-card{display:grid;overflow:hidden;padding:0;border:1px solid var(--line);border-radius:15px;background:var(--panel);color:var(--text);text-align:left;box-shadow:0 6px 20px rgba(36,50,82,.035);transition:.18s}.theme-card:hover{transform:translateY(-2px);border-color:#bac5e5}.theme-card.selected{border-color:var(--accent);box-shadow:0 0 0 2px #4f6df51c,0 10px 28px #4f6df51a}.theme-preview{height:150px;display:grid;grid-template-columns:30% 1fr;padding:14px;gap:12px}.preview-sidebar{display:grid;align-content:start;gap:8px;padding:10px;border-radius:9px}.preview-sidebar span{height:7px;border-radius:999px}.preview-main{display:grid;align-content:center;gap:10px;padding:12px;border-radius:10px}.preview-bubble{display:block;height:22px;border-radius:8px}.preview-bubble.assistant{width:80%}.preview-bubble.user{width:67%;margin-left:auto}.preview-input{height:25px;margin-top:4px;border-radius:7px}.theme-pure-light .theme-preview{background:#f6f8fc}.theme-pure-light .preview-sidebar,.theme-pure-light .preview-main{background:#fff;border:1px solid #e3e8f2}.theme-pure-light .preview-sidebar span,.theme-pure-light .preview-input{background:#eef1f7}.theme-pure-light .assistant{background:#edf0f6}.theme-pure-light .user{background:#4864dc}.theme-midnight-violet .theme-preview{background:#0c0e15}.theme-midnight-violet .preview-sidebar,.theme-midnight-violet .preview-main{background:#141824;border:1px solid #2a3041}.theme-midnight-violet .preview-sidebar span,.theme-midnight-violet .preview-input{background:#252b3b}.theme-midnight-violet .assistant{background:#1a1f2d}.theme-midnight-violet .user{background:#5264cc}.theme-system .theme-preview{background:linear-gradient(110deg,#f6f8fc 0 49%,#0c0e15 51%)}.theme-system .preview-sidebar{background:#fff;border:1px solid #e3e8f2}.theme-system .preview-main{background:#141824;border:1px solid #2a3041}.theme-system .preview-sidebar span{background:#eef1f7}.theme-system .assistant,.theme-system .preview-input{background:#252b3b}.theme-system .user{background:#5264cc}.theme-body{display:grid;gap:8px;padding:15px 16px 17px;border-top:1px solid var(--line)}.theme-title,.theme-meta{display:flex;justify-content:space-between;align-items:center;gap:10px}.theme-title strong{font-size:14px}.theme-body p{min-height:38px;margin:0;color:var(--muted);font-size:11px;line-height:1.65}.theme-meta{color:var(--muted);font-size:10px}.swatches{display:flex;gap:6px}.swatches i{width:18px;height:18px;border:1px solid #0001;border-radius:50%}.scope-note{margin-top:18px}
.schedule-panel{margin-top:18px;padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}
.schedule-head h3{margin:0;font-size:15px}
.schedule-head p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.6}
.schedule-fields{display:flex;gap:18px;margin-top:12px;flex-wrap:wrap}
.schedule-fields label{display:grid;gap:6px;color:var(--muted);font-size:11px}
.theme-scheduled .theme-preview{background:linear-gradient(110deg,#f6f8fc 0 49%,#0c0e15 51%)}
.theme-scheduled .preview-sidebar{background:#fff;border:1px solid #e3e8f2}
.theme-scheduled .preview-main{background:#141824;border:1px solid #2a3041}
.theme-scheduled .preview-sidebar span{background:#eef1f7}
.theme-scheduled .assistant,.theme-scheduled .preview-input{background:#252b3b}
.theme-scheduled .user{background:#5264cc}@media(max-width:1000px){.theme-grid{grid-template-columns:1fr 1fr}}@media(max-width:720px){.appearance-page{padding:16px}.hero,.section-heading{align-items:stretch;flex-direction:column}.theme-grid{grid-template-columns:1fr}.actions>*{flex:1}.sync-state{width:100%}}
</style>
