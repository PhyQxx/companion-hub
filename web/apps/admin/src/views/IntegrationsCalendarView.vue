<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

interface CalDavConfig {
  enabled: boolean;
  url: string | null;
  username: string | null;
  secret_value: string | null;
  secret_ref: string | null;
  calendar_names: string[];
  window_days_back: number;
  window_days_forward: number;
  [key: string]: unknown;
}

interface GoogleCalendarConfig {
  enabled: boolean;
  client_id: string | null;
  secret_value: string | null;
  secret_ref: string | null;
  redirect_uri: string | null;
  calendar_ids: string[];
  window_days_back: number;
  window_days_forward: number;
  [key: string]: unknown;
}

interface CalendarIntegrations {
  caldav: CalDavConfig;
  google: GoogleCalendarConfig;
}

interface HubConfig {
  integrations?: { calendar?: CalendarIntegrations };
  [key: string]: unknown;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const current = ref<{ version: number; config: HubConfig } | null>(null);
const calendar = ref<CalendarIntegrations | null>(null);
const caldavSyncing = ref(false);
const googleSyncing = ref(false);
const googleSyncResult = ref<string>("");
const caldavSyncResult = ref<string>("");
const loading = ref(false);
const saving = ref(false);

function defaultCalendar(): CalendarIntegrations {
  return {
    caldav: {
      enabled: false,
      url: null,
      username: null,
      secret_value: null,
      secret_ref: null,
      calendar_names: [],
      window_days_back: 7,
      window_days_forward: 60,
    },
    google: {
      enabled: false,
      client_id: null,
      secret_value: null,
      secret_ref: null,
      redirect_uri: null,
      calendar_ids: [],
      window_days_back: 7,
      window_days_forward: 60,
    },
  };
}

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function parseList(raw: string): string[] {
  return raw.split(/[,,\n]/).map((item) => item.trim()).filter(Boolean);
}

async function load() {
  loading.value = true;
  try {
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current");
    const loaded = current.value.config.integrations?.calendar;
    calendar.value = loaded
      ? { caldav: { ...defaultCalendar().caldav, ...loaded.caldav }, google: { ...defaultCalendar().google, ...loaded.google } }
      : defaultCalendar();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "日历配置加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!calendar.value || !current.value) return;
  saving.value = true;
  try {
    const config = clonePlain(current.value.config);
    if (!config.integrations) config.integrations = {};
    config.integrations.calendar = clonePlain(calendar.value);
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    emit("status", `日历配置已保存，配置版本 ${current.value.version}`);
    ElMessage.success("保存成功，已即时生效");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "保存失败", true);
  } finally {
    saving.value = false;
  }
}

async function syncCalendar(provider: "caldav" | "google") {
  const syncing = provider === "caldav" ? caldavSyncing : googleSyncing;
  const resultRef = provider === "caldav" ? caldavSyncResult : googleSyncResult;
  syncing.value = true;
  resultRef.value = "";
  try {
    const result = await api.request<Record<string, unknown>>(
      `/api/v1/admin/config/integrations/calendar/${provider}/sync`,
      { method: "POST" },
    );
    const errors = (result.errors as string[]) ?? [];
    resultRef.value = errors.length
      ? `失败：${errors.join("；")}`
      : `同步 ${result.pulled} 条事件，新建 ${result.mirrors_created}，更新 ${result.mirrors_updated}`;
    errors.length ? ElMessage.error(resultRef.value) : ElMessage.success("同步完成");
  } catch (error) {
    resultRef.value = error instanceof Error ? error.message : "同步失败";
    ElMessage.error(resultRef.value);
  } finally {
    syncing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section v-loading="loading" class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">集成与连接 · CAL-01</div>
        <h2>外部日历</h2>
        <p>CalDAV / Google 日历只读镜像到本地时间线：查询、简报和通勤建议可用，镜像不产生本地提醒。</p>
      </div>
      <el-button type="primary" :loading="saving" @click="save">保存并生效</el-button>
    </div>

    <div v-if="calendar" class="panel">
      <div class="cal-columns">
        <div class="cal-block">
          <div class="cal-head"><strong>CalDAV</strong><el-switch v-model="calendar.caldav.enabled" active-text="启用" /></div>
          <div class="form-grid">
            <label class="wide"><span>服务器地址</span><el-input v-model="calendar.caldav.url" placeholder="https://…/calendars/用户/" /></label>
            <label><span>用户名</span><el-input v-model="calendar.caldav.username" autocomplete="off" /></label>
            <label><span>应用密码</span><el-input v-model="calendar.caldav.secret_value" type="password" show-password autocomplete="new-password" /></label>
            <label class="wide"><span>日历名单（逗号分隔，留空同步全部）</span><el-input :model-value="calendar.caldav.calendar_names.join(',')" @update:model-value="(v: string) => { if (calendar) calendar.caldav.calendar_names = parseList(v); }" /></label>
            <label><span>向前窗口（天）</span><el-input-number v-model="calendar.caldav.window_days_forward" :min="1" :max="365" /></label>
            <label><span>向后窗口（天）</span><el-input-number v-model="calendar.caldav.window_days_back" :min="0" :max="90" /></label>
            <label class="actions"><el-button :loading="caldavSyncing" @click="syncCalendar('caldav')">立即同步</el-button></label>
          </div>
          <p v-if="caldavSyncResult" class="config-note">{{ caldavSyncResult }}</p>
        </div>
        <div class="cal-block">
          <div class="cal-head"><strong>Google 日历</strong><el-switch v-model="calendar.google.enabled" active-text="启用" /></div>
          <div class="form-grid">
            <label class="wide"><span>OAuth 客户端 ID</span><el-input v-model="calendar.google.client_id" placeholder="…apps.googleusercontent.com" /></label>
            <label><span>客户端密钥</span><el-input v-model="calendar.google.secret_value" type="password" show-password autocomplete="new-password" /></label>
            <label><span>回调地址</span><el-input v-model="calendar.google.redirect_uri" placeholder="http://hub:8000/api/v1/calendar/google/callback" /></label>
            <label class="wide"><span>日历 ID（逗号分隔，留空用 primary）</span><el-input :model-value="calendar.google.calendar_ids.join(',')" @update:model-value="(v: string) => { if (calendar) calendar.google.calendar_ids = parseList(v); }" /></label>
            <label><span>向前窗口（天）</span><el-input-number v-model="calendar.google.window_days_forward" :min="1" :max="365" /></label>
            <label><span>向后窗口（天）</span><el-input-number v-model="calendar.google.window_days_back" :min="0" :max="90" /></label>
            <label class="actions">
              <el-button :loading="googleSyncing" @click="syncCalendar('google')">立即同步</el-button>
              <el-button v-if="calendar.google.enabled && calendar.google.client_id" tag="a" target="_blank" :href="`/api/v1/calendar/google/authorize`">打开授权页</el-button>
            </label>
          </div>
          <p class="config-note">先保存配置，再点「打开授权页」完成 Google 同意；刷新令牌只落库，不显示在页面上。</p>
          <p v-if="googleSyncResult" class="config-note">{{ googleSyncResult }}</p>
        </div>
      </div>
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
.cal-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.cal-block { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: grid; gap: 10px; }
.cal-head { display: flex; justify-content: space-between; align-items: center; }
.cal-block .form-grid { display: grid; grid-template-columns: repeat(2, minmax(150px, 1fr)); gap: 12px; }
.form-grid label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
.form-grid .wide { grid-column: 1 / -1; }
.cal-block .actions { display: flex; align-items: flex-end; gap: 8px; }
.config-note { color: var(--muted); font-size: 11px; margin: 0; }
@media (max-width: 1100px) { .cal-columns { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .content { padding: 14px; } .hero { flex-direction: column; align-items: flex-start; } .cal-block .form-grid { grid-template-columns: 1fr; } }
</style>
