<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

interface MailConfig {
  enabled: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_use_ssl: boolean;
  imap_host: string;
  imap_port: number;
  address: string | null;
  display_name: string | null;
  secret_mode: "value" | "ref" | "none";
  secret_ref?: string | null;
  secret_value?: string | null;
  timeout_seconds: number;
  fetch_limit: number;
  [key: string]: unknown;
}

interface HubConfig {
  integrations?: { mail?: MailConfig };
  [key: string]: unknown;
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const current = ref<{ version: number; config: HubConfig } | null>(null);
const mail = ref<MailConfig | null>(null);
const loading = ref(false);
const saving = ref(false);

function defaultMail(): MailConfig {
  return {
    enabled: false,
    smtp_host: "smtp.qq.com",
    smtp_port: 465,
    smtp_use_ssl: true,
    imap_host: "imap.qq.com",
    imap_port: 993,
    address: null,
    display_name: null,
    secret_mode: "none",
    secret_ref: null,
    secret_value: null,
    timeout_seconds: 15,
    fetch_limit: 10,
  };
}

function normalize(config: MailConfig | undefined): MailConfig {
  return {
    ...defaultMail(),
    ...config,
    secret_mode: config?.secret_value ? "value" : config?.secret_ref ? "ref" : "none",
  };
}

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

async function load() {
  loading.value = true;
  try {
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current");
    mail.value = normalize(clonePlain(current.value.config.integrations?.mail));
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "邮件配置加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!mail.value || !current.value) return;
  if (mail.value.enabled) {
    if (!mail.value.address?.trim()) {
      ElMessage.warning("启用邮件助手前必须填写邮箱地址");
      return;
    }
    const secretReady =
      (mail.value.secret_mode === "value" && mail.value.secret_value) ||
      (mail.value.secret_mode === "ref" && mail.value.secret_ref);
    if (!secretReady) {
      ElMessage.warning("启用邮件助手前必须配置授权码");
      return;
    }
  }
  saving.value = true;
  try {
    const config = clonePlain(current.value.config);
    const { secret_mode, ...rest } = mail.value;
    const cleaned = rest as MailConfig;
    if (secret_mode === "value" && cleaned.secret_value) cleaned.secret_ref = null;
    else if (secret_mode === "ref" && cleaned.secret_ref) cleaned.secret_value = null;
    else {
      cleaned.secret_value = null;
      cleaned.secret_ref = null;
    }
    if (!config.integrations) config.integrations = {};
    config.integrations.mail = cleaned;
    current.value = await api.request<{ version: number; config: HubConfig }>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    mail.value = normalize(clonePlain(current.value.config.integrations?.mail));
    emit("status", `邮件配置已保存，配置版本 ${current.value.version}`);
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
        <div class="eyebrow">集成与连接 · MAIL-01</div>
        <h2>邮件助手</h2>
        <p>SMTP 发送与 IMAP 收取。授权码（非登录密码）走 secret 双模式；发送必须经 Chat 卡片用户确认，模型无法自行发出邮件。</p>
      </div>
      <div class="hero-actions">
        <el-button type="primary" :loading="saving" @click="save">保存并生效</el-button>
        <el-switch v-if="mail" v-model="mail.enabled" active-text="启用" />
      </div>
    </div>

    <div v-if="mail" class="panel">
      <div class="panel-head"><div><h2>账号与协议</h2><p>QQ 邮箱等需要在邮箱设置中开启 SMTP/IMAP 并生成授权码。</p></div></div>
      <div class="form-grid">
        <label><span>邮箱地址</span><el-input v-model="mail.address" placeholder="you@example.com" autocomplete="off" /></label>
        <label><span>发件显示名</span><el-input v-model="mail.display_name" placeholder="Aria" /></label>
        <label><span>SMTP 服务器</span><el-input v-model="mail.smtp_host" placeholder="smtp.qq.com" /></label>
        <label><span>SMTP 端口</span><el-input-number v-model="mail.smtp_port" :min="1" :max="65535" /></label>
        <label><span>SMTP SSL</span><el-switch v-model="mail.smtp_use_ssl" /></label>
        <label><span>IMAP 服务器</span><el-input v-model="mail.imap_host" placeholder="imap.qq.com" /></label>
        <label><span>IMAP 端口</span><el-input-number v-model="mail.imap_port" :min="1" :max="65535" /></label>
        <label><span>授权码保存方式</span><el-select v-model="mail.secret_mode"><el-option label="后台直接保存" value="value" /><el-option label="环境变量引用" value="ref" /><el-option label="暂不配置" value="none" /></el-select></label>
        <label v-if="mail.secret_mode === 'value'"><span>授权码</span><el-input v-model="mail.secret_value" type="password" show-password autocomplete="new-password" /></label>
        <label v-else-if="mail.secret_mode === 'ref'"><span>环境变量引用</span><el-input v-model="mail.secret_ref" placeholder="env:ARIA_MAIL_SECRET" /></label>
        <label><span>超时（秒）</span><el-input-number v-model="mail.timeout_seconds" :min="3" :max="60" /></label>
        <label><span>收取摘要条数</span><el-input-number v-model="mail.fetch_limit" :min="1" :max="50" /></label>
      </div>
      <div class="panel-note">邮件读取与发送均为云端操作，聊天工具仅在 L1 会话挂载；L2 私密会话不外发。发送前必须经用户在完整预览卡片上点击确认。</div>
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
.panel-head h2 { font-size: 15px; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 14px; }
.form-grid label { display: grid; gap: 6px; color: var(--muted); font-size: 11px; }
@media (max-width: 1000px) { .form-grid { grid-template-columns: repeat(2, minmax(160px, 1fr)); } }
@media (max-width: 720px) { .content { padding: 14px; } .hero { flex-direction: column; align-items: flex-start; } .form-grid { grid-template-columns: 1fr; } }
</style>
