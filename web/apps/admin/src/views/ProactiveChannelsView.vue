<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage } from "element-plus";

type PrivacyLevel = "L0" | "L1" | "L2";
type ChannelKey = "web_chat" | "desktop_notification" | "web_push" | "voice";

interface ChannelPolicy {
  enabled: boolean;
  priority: number;
  max_privacy_level: PrivacyLevel;
  critical_only: boolean;
}

interface ProactiveOutputConfig {
  enabled: boolean;
  delivery_mode: "first_available" | "all_enabled";
  web_chat: ChannelPolicy;
  desktop_notification: ChannelPolicy;
  web_push: ChannelPolicy;
  voice: ChannelPolicy;
}

interface CurrentConfig {
  version: number;
  config: Record<string, unknown> & { proactive_output?: ProactiveOutputConfig };
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const current = ref<CurrentConfig | null>(null);
const draft = ref<ProactiveOutputConfig | null>(null);
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);

const channelMeta: Record<ChannelKey, { name: string; description: string; requirement: string }> = {
  web_chat: {
    name: "Web 对话",
    description: "写入事件所属用户的最近活动对话，并向已订阅该对话的 WebSocket 实时推送。",
    requirement: "需要用户至少有一个活动对话。",
  },
  desktop_notification: {
    name: "macOS 桌面通知",
    description: "通过签名设备命令调用 Aria Desktop 的原生系统通知。",
    requirement: "Desktop 必须在线、未锁屏、未隐私暂停，并获授 notification.show。",
  },
  web_push: {
    name: "移动推送（Web Push）",
    description: "通过 Web Push 把通知推到手机 PWA / 已订阅浏览器，即使页面关闭也能收到。",
    requirement: "需在「集成 → push」配置 VAPID 密钥并启用；用户在聊天端开启通知并完成订阅。",
  },
  voice: {
    name: "在线语音播报",
    description: "向当前在线且空闲的语音会话发送文字，并在隐私允许时使用现有 TTS 链播报。",
    requirement: "需要已打开的语音会话；L2 只会使用本地 TTS，否则降级为文字。",
  },
};

const channelKeys: ChannelKey[] = ["web_chat", "desktop_notification", "web_push", "voice"];
const privacyOptions: Record<ChannelKey, PrivacyLevel[]> = {
  web_chat: ["L0", "L1", "L2"],
  desktop_notification: ["L0", "L1"],
  web_push: ["L0", "L1"],
  voice: ["L0", "L1", "L2"],
};
const enabledCount = computed(() => draft.value
  ? channelKeys.filter((key) => draft.value![key].enabled).length
  : 0);

function defaults(): ProactiveOutputConfig {
  return {
    enabled: true,
    delivery_mode: "all_enabled",
    web_chat: { enabled: true, priority: 100, max_privacy_level: "L1", critical_only: false },
    desktop_notification: { enabled: false, priority: 80, max_privacy_level: "L1", critical_only: false },
    web_push: { enabled: false, priority: 70, max_privacy_level: "L1", critical_only: false },
    voice: { enabled: false, priority: 60, max_privacy_level: "L1", critical_only: false },
  };
}

function normalize(value?: ProactiveOutputConfig): ProactiveOutputConfig {
  const base = defaults();
  if (!value) return base;
  return {
    ...base,
    ...value,
    web_chat: { ...base.web_chat, ...value.web_chat },
    desktop_notification: { ...base.desktop_notification, ...value.desktop_notification },
    web_push: { ...base.web_push, ...value.web_push },
    voice: { ...base.voice, ...value.voice },
  };
}

async function load() {
  loading.value = true;
  try {
    current.value = await api.request<CurrentConfig>("/api/v1/admin/config/current");
    draft.value = normalize(current.value.config.proactive_output);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "主动通道配置加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!current.value || !draft.value) return;
  if (draft.value.enabled && enabledCount.value === 0) {
    ElMessage.warning("总开关启用时至少需要开启一个通道");
    return;
  }
  saving.value = true;
  try {
    const config = JSON.parse(JSON.stringify(current.value.config)) as Record<string, unknown>;
    config.proactive_output = draft.value;
    current.value = await api.request<CurrentConfig>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    draft.value = normalize(current.value.config.proactive_output);
    emit("status", `主动输出配置已保存，配置版本 ${current.value.version}`);
    ElMessage.success("保存成功，已即时生效");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "主动输出配置保存失败", true);
  } finally {
    saving.value = false;
  }
}

async function testDelivery() {
  testing.value = true;
  try {
    const result = await api.request<{ ok: boolean; message: string }>(
      "/api/v1/admin/config/integrations/home-assistant/proactive/test",
      { method: "POST" },
    );
    result.ok ? ElMessage.success(result.message) : ElMessage.warning(result.message);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "测试推送失败");
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section v-loading="loading" class="channels-workspace">
    <div class="hero panel">
      <div>
        <div class="eyebrow">设备与授权 · 主动输出</div>
        <h2>主动推送通道</h2>
        <p>只有已启用、隐私允许且真实在线的通道会被选择；关闭通道会即时生效。</p>
      </div>
      <div class="actions">
        <el-button :loading="testing" @click="testDelivery">测试当前策略</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并生效</el-button>
      </div>
    </div>

    <template v-if="draft">
      <div class="panel global-policy">
        <div>
          <strong>主动输出总开关</strong>
          <p>关闭后认知决策和审计仍保留，但不会向任何终端投递。</p>
        </div>
        <el-switch v-model="draft.enabled" active-text="允许主动推送" />
        <label>
          <span>投递模式</span>
          <el-select v-model="draft.delivery_mode" :disabled="!draft.enabled">
            <el-option label="投递到全部可用通道" value="all_enabled" />
            <el-option label="只投递到最高优先级可用通道" value="first_available" />
          </el-select>
        </label>
        <el-tag effect="plain">{{ enabledCount }} 个通道已启用</el-tag>
      </div>

      <div class="channel-grid">
        <article v-for="key in channelKeys" :key="key" class="panel channel-card">
          <div class="channel-head">
            <div><h3>{{ channelMeta[key].name }}</h3><p>{{ channelMeta[key].description }}</p></div>
            <el-switch v-model="draft[key].enabled" :disabled="!draft.enabled" />
          </div>
          <div class="requirement">{{ channelMeta[key].requirement }}</div>
          <div class="fields">
            <label><span>优先级</span><el-input-number v-model="draft[key].priority" :min="1" :max="100" :disabled="!draft.enabled || !draft[key].enabled" /></label>
            <label><span>最高隐私等级</span><el-select v-model="draft[key].max_privacy_level" :disabled="!draft.enabled || !draft[key].enabled"><el-option v-for="level in privacyOptions[key]" :key="level" :label="level" :value="level" /></el-select></label>
            <label class="switch-field"><span>事件范围</span><el-switch v-model="draft[key].critical_only" active-text="仅紧急事件" :disabled="!draft.enabled || !draft[key].enabled" /></label>
          </div>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.channels-workspace{padding:20px 24px 28px;display:grid;gap:16px}.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px}.hero,.global-policy,.channel-head,.actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.hero{background:linear-gradient(135deg,#fff,#f1f5ff)}h2,h3,p{margin:0}.hero h2{font-size:16px}.hero p,.channel-head p,.global-policy p{margin-top:7px;color:var(--muted);font-size:12px}.eyebrow{margin-bottom:7px;color:var(--accent);font-size:11px;font-weight:700}.global-policy>div:first-child{flex:1}.global-policy label,.fields label{display:grid;gap:6px;color:var(--muted);font-size:11px}.global-policy label{min-width:260px}.channel-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.channel-card{display:grid;gap:16px;align-content:start}.channel-head{align-items:flex-start}.channel-head h3{font-size:15px}.requirement{padding:10px 12px;border-radius:9px;background:#f5f7fb;color:var(--muted);font-size:11px;line-height:1.6}.fields{display:grid;gap:12px}.switch-field{min-height:54px}@media(max-width:1100px){.channel-grid{grid-template-columns:1fr}.global-policy{align-items:flex-start;flex-wrap:wrap}}@media(max-width:700px){.hero,.global-policy,.channel-head{align-items:flex-start;flex-direction:column}.actions{width:100%;justify-content:flex-start}.global-policy label{min-width:100%;width:100%}}
</style>
