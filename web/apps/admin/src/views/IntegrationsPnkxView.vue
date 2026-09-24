<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

interface PnkxConfig {
  enabled: boolean;
  base_url: string | null;
  secret_value: string | null;
  secret_ref: string | null;
  writes_enabled: boolean;
  sync_interval_seconds: number;
}

interface HubConfig {
  integrations?: { pnkx?: PnkxConfig };
  [key: string]: unknown;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const current = ref<{ version: number; config: HubConfig } | null>(null);
const pnkx = ref<PnkxConfig | null>(null);
const loading = ref(false);
const saving = ref(false);

function defaults(): PnkxConfig {
  return {
    enabled: false,
    base_url: null,
    secret_value: null,
    secret_ref: null,
    writes_enabled: false,
    sync_interval_seconds: 300,
  };
}

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

async function load() {
  loading.value = true;
  try {
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current");
    pnkx.value = { ...defaults(), ...clonePlain(current.value.config.integrations?.pnkx) };
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "PNKX 配置加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!pnkx.value || !current.value) return;
  if (pnkx.value.enabled && (!pnkx.value.base_url?.trim() || !pnkx.value.secret_value)) {
    ElMessage.warning("启用 PNKX 前必须填写服务地址和集成令牌");
    return;
  }
  saving.value = true;
  try {
    const config = clonePlain(current.value.config);
    if (!config.integrations) config.integrations = {};
    config.integrations.pnkx = clonePlain(pnkx.value);
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    pnkx.value = { ...defaults(), ...clonePlain(current.value.config.integrations?.pnkx) };
    emit("status", `PNKX 配置已保存，配置版本 ${current.value.version}`);
    ElMessage.success("保存成功；连接配置将在服务重启后完全生效");
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
        <div class="eyebrow">集成与连接 · PNKX</div>
        <h2>PNKX 生活服务</h2>
        <p>统一管理待办、生活数据等 PNKX 接口。令牌保存在数据库配置版本中，管理接口只返回掩码。</p>
      </div>
      <div class="hero-actions">
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        <el-switch v-if="pnkx" v-model="pnkx.enabled" active-text="启用" />
      </div>
    </div>

    <div v-if="pnkx" class="panel">
      <div class="panel-head"><div><h2>连接与权限</h2><p>写入权限会控制聊天工具和 API 是否允许创建 PNKX 数据。</p></div></div>
      <div class="form-grid">
        <label><span>服务地址</span><el-input v-model="pnkx.base_url" placeholder="https://example.com" autocomplete="off" /></label>
        <label><span>集成令牌</span><el-input v-model="pnkx.secret_value" type="password" show-password autocomplete="new-password" /></label>
        <label><span>允许写入</span><el-switch v-model="pnkx.writes_enabled" /></label>
        <label><span>待办同步间隔（秒）</span><el-input-number v-model="pnkx.sync_interval_seconds" :min="30" :max="86400" /></label>
      </div>
      <div class="panel-note">令牌不会从管理 API 明文返回；未修改令牌字段时，保存操作会保留数据库中的原值。</div>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px 28px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; display: grid; gap: 14px; }
.hero { display: flex; justify-content: space-between; align-items: center; gap: 24px; background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
.hero-actions { display: flex; align-items: center; gap: 14px; flex: none; }
.hero h2, .panel h2 { margin: 0; font-size: 16px; }
.hero p, .panel-head p, .panel-note { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.panel-note { margin: 0; }
.eyebrow { color: var(--accent); font-size: 11px; font-weight: 700; letter-spacing: .08em; margin-bottom: 7px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); gap: 14px; }
.form-grid label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
@media (max-width: 720px) { .content { padding: 14px; } .hero { flex-direction: column; align-items: flex-start; } .form-grid { grid-template-columns: 1fr; } }
</style>
