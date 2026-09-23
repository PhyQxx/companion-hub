<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

interface XiaoAiConfig {
  enabled: boolean;
  xiaomi_user_id: string | null;
  xiaomi_password_secret_value: string | null;
  xiaomi_password_secret_ref: string | null;
  xiaomi_pass_token_secret_value: string | null;
  xiaomi_pass_token_secret_ref: string | null;
  speaker_name: string | null;
  ha_device_id: string | null;
  model: string | null;
  owner_user_id: string | null;
  gateway_token_secret_value: string | null;
  gateway_token_secret_ref: string | null;
  trigger_prefix: string;
  tts_siid: number | null;
  tts_aiid: number | null;
  [key: string]: unknown;
}

interface EntityDetail {
  entity_id: string;
  friendly_name: string;
  domain: string;
  state: string;
  device_class?: string | null;
  unit_of_measurement?: string | null;
  area?: string | null;
  device_id?: string | null;
  device_name?: string | null;
  manufacturer?: string | null;
  model?: string | null;
}

interface HubConfig {
  integrations?: { xiaoai?: XiaoAiConfig };
  [key: string]: unknown;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const current = ref<{ version: number; config: HubConfig } | null>(null);
const xiaoai = ref<XiaoAiConfig | null>(null);
const loading = ref(false);
const saving = ref(false);
const fetchingDevices = ref(false);

// 与 HA 实体授权页共享同一份 localStorage 设备缓存，避免重复全量拉取
const HA_ENTITIES_KEY = "aria:ha:entities";
const allEntities = ref<EntityDetail[]>([]);

function loadCachedEntities(): EntityDetail[] {
  try {
    const raw = localStorage.getItem(HA_ENTITIES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? (parsed as EntityDetail[]) : [];
  } catch {
    return [];
  }
}

const speakerOptions = computed(() => {
  const found = new Map<string, { device_id: string; name: string; model: string; manufacturer: string }>();
  for (const entity of allEntities.value) {
    const entitySpeaker = entity.entity_id.match(
      /^media_player\.([a-z0-9]+)_cn_\d+_([a-z0-9]+)$/i,
    );
    const model = entity.model?.includes("wifispeaker")
      ? entity.model
      : entitySpeaker
        ? `${entitySpeaker[1]}.wifispeaker.${entitySpeaker[2]}`
        : null;
    if (!model) continue;
    const inferredName = entity.friendly_name.split(/\s{2,}/, 1)[0]?.trim();
    const key = entity.device_id || `entity:${entity.entity_id}`;
    found.set(key, {
      device_id: key,
      name: entity.device_name || inferredName || entity.friendly_name,
      model,
      manufacturer: entity.manufacturer || entitySpeaker?.[1] || "",
    });
  }
  return [...found.values()].sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
});

function defaultXiaoAi(): XiaoAiConfig {
  return {
    enabled: false,
    xiaomi_user_id: null,
    xiaomi_password_secret_value: null,
    xiaomi_password_secret_ref: null,
    xiaomi_pass_token_secret_value: null,
    xiaomi_pass_token_secret_ref: null,
    speaker_name: null,
    ha_device_id: null,
    model: null,
    owner_user_id: null,
    gateway_token_secret_value: null,
    gateway_token_secret_ref: null,
    trigger_prefix: "请阿莉娅",
    tts_siid: null,
    tts_aiid: null,
  };
}

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function onSpeakerChange(deviceId: string) {
  if (!xiaoai.value) return;
  const speaker = speakerOptions.value.find((item) => item.device_id === deviceId);
  if (!speaker) return;
  xiaoai.value.speaker_name = speaker.name;
  xiaoai.value.model = speaker.model;
}

function generateGatewayToken() {
  if (!xiaoai.value) return;
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  xiaoai.value.gateway_token_secret_value = btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function fetchDevices() {
  fetchingDevices.value = true;
  try {
    const result = await api.request<{ ok: boolean; message: string; entities: EntityDetail[] }>(
      "/api/v1/admin/config/integrations/home-assistant/entities",
      { method: "POST" },
    );
    if (result.ok) {
      allEntities.value = result.entities;
      try {
        localStorage.setItem(HA_ENTITIES_KEY, JSON.stringify(result.entities));
      } catch { /* storage full or private mode */ }
      ElMessage.success(result.message);
    } else {
      ElMessage.error(result.message);
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "获取 HA 设备列表失败");
  } finally {
    fetchingDevices.value = false;
  }
}

async function load() {
  loading.value = true;
  try {
    allEntities.value = loadCachedEntities();
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current");
    xiaoai.value = clonePlain(current.value.config.integrations?.xiaoai ?? defaultXiaoAi());
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "小爱网关配置加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!xiaoai.value || !current.value) return;
  if (xiaoai.value.enabled) {
    const required = [xiaoai.value.xiaomi_user_id, xiaoai.value.speaker_name, xiaoai.value.owner_user_id, xiaoai.value.gateway_token_secret_value ?? xiaoai.value.gateway_token_secret_ref];
    if (required.some((value) => !value || !String(value).trim())) {
      ElMessage.warning("启用小爱接入需要小米账号、目标音箱、中枢用户与网关密钥齐全");
      return;
    }
  }
  saving.value = true;
  try {
    const config = clonePlain(current.value.config);
    if (!config.integrations) config.integrations = {};
    config.integrations.xiaoai = clonePlain(xiaoai.value);
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    emit("status", `小爱网关配置已保存，配置版本 ${current.value.version}`);
    ElMessage.success("保存成功，已即时生效");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "保存失败", true);
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section v-loading="loading" class="content">
    <div class="hero panel">
      <div>
        <div class="eyebrow">集成与连接 · XIAOAI</div>
        <h2>小爱音箱网关</h2>
        <p>把小爱音箱当作文字终端：命中触发前缀的识别文本送到 Hub 回复。小米凭据不进 Hub 进程日志，账号、音箱和密钥由配置中心保存。</p>
      </div>
      <div class="hero-actions">
        <el-button :loading="fetchingDevices" @click="fetchDevices">刷新音箱列表</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并生效</el-button>
        <el-switch v-if="xiaoai" v-model="xiaoai.enabled" active-text="启用" />
      </div>
    </div>

    <div v-if="xiaoai" class="panel">
      <div class="form-grid">
        <label><span>目标音箱</span><el-select v-model="xiaoai.ha_device_id" filterable placeholder="请先刷新音箱列表" @change="onSpeakerChange"><el-option v-for="speaker in speakerOptions" :key="speaker.device_id" :label="`${speaker.name} · ${speaker.model}`" :value="speaker.device_id" /></el-select></label>
        <label><span>HA 型号</span><el-input :model-value="xiaoai.model || ''" disabled /></label>
        <label><span>触发前缀</span><el-input v-model="xiaoai.trigger_prefix" placeholder="请阿莉娅" /></label>
        <label><span>小米账号 ID</span><el-input v-model="xiaoai.xiaomi_user_id" autocomplete="off" /></label>
        <label><span>小米账号密码</span><el-input v-model="xiaoai.xiaomi_password_secret_value" type="password" show-password autocomplete="new-password" /></label>
        <label class="wide"><span>小米 passToken（触发验证码时填写）</span><el-input v-model="xiaoai.xiaomi_pass_token_secret_value" type="password" show-password autocomplete="new-password" /></label>
        <label><span>中枢用户 UUID</span><el-input v-model="xiaoai.owner_user_id" placeholder="登录用户 UUID" /></label>
        <label class="wide"><span>网关认证密钥</span><div class="secret-row"><el-input v-model="xiaoai.gateway_token_secret_value" type="password" show-password autocomplete="new-password" /><el-button @click="generateGatewayToken">随机生成</el-button></div></label>
        <label><span>TTS SIID（通常留空）</span><el-input-number v-model="xiaoai.tts_siid" :min="1" :controls="false" /></label>
        <label><span>TTS AIID（通常留空）</span><el-input-number v-model="xiaoai.tts_aiid" :min="1" :controls="false" /></label>
      </div>
      <div class="panel-note">保存后 Hub 会把配置写入小爱网关的私有共享卷。首次启用或更换账号后重启 xiaoai-gateway 容器即可生效；音箱型号来自 HA 设备注册表。</div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; display: grid; gap: 14px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f7f2ff 100%); }
.hero-actions { display: flex; align-items: center; gap: 10px; flex: none; flex-wrap: wrap; }
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p, .panel-note { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; max-width: 640px; }
.panel-note { margin: 0; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 14px; }
.form-grid label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
.form-grid .wide { grid-column: 1 / -1; }
.secret-row { display: flex; gap: 8px; }
@media (max-width: 1000px) { .form-grid { grid-template-columns: repeat(2, minmax(160px, 1fr)); } }
@media (max-width: 720px) { .content { padding: 14px; } .hero { flex-direction: column; align-items: flex-start; } .form-grid { grid-template-columns: 1fr; } }
</style>
