<script setup lang="ts">
import {
  PhBell,
  PhExport,
  PhMapPin,
  PhMicrophone,
  PhPaperPlaneRight,
  PhPhone,
  PhPhoneDisconnect,
  PhSpeakerHigh,
  PhStopCircle,
} from "@phosphor-icons/vue";
import type { PrivacyLevel } from "@aria/shared";
import type { StreamingState } from "./composables/useVoice";

defineProps<{
  activeConversationArchived: boolean;
  hasActiveId: boolean;
  streaming: StreamingState | null;
  voiceBusy: boolean;
  voiceRecording: boolean;
  voiceLive: boolean;
  voiceStatus: string;
  voiceViseme: number;
  voiceTranscript: string;
  statusText: string;
  statusError: boolean;
  locationSupported: boolean;
  pushSupported: boolean;
  hasToken: boolean;
  selectMode: boolean;
  canExport: boolean;
  socketReady: boolean;
  canSend: boolean;
}>();
const draft = defineModel<string>("draft", { default: "" });
const privacy = defineModel<PrivacyLevel>("privacy", { required: true });
const textReplyVoice = defineModel<boolean>("textReplyVoice", { required: true });
const locationEnabled = defineModel<boolean>("locationEnabled", { required: true });
const notificationsEnabled = defineModel<boolean>("notificationsEnabled", { required: true });
const emit = defineEmits<{
  toggleVoiceCall: [];
  toggleVoiceRecording: [];
  interruptVoice: [];
  privacyChanged: [];
  textReplyVoiceChanged: [];
  locationEnabledChanged: [];
  notificationsChanged: [];
  toggleSelectMode: [];
  send: [];
  cancelStreaming: [];
}>();
</script>

<template>
  <footer class="composer">
    <p v-if="activeConversationArchived" class="archived-notice">此会话已归档。恢复后可以继续发送消息。</p>
    <div class="composer-toolbar">
      <button
        class="voice-button"
        :class="{ recording: voiceLive }"
        type="button"
        :disabled="!hasActiveId || activeConversationArchived || !!streaming || voiceRecording || (voiceBusy && !voiceLive)"
        @click="emit('toggleVoiceCall')"
      >
        <PhPhoneDisconnect v-if="voiceLive" :size="17" weight="fill" aria-hidden="true" />
        <PhPhone v-else :size="17" weight="fill" aria-hidden="true" />
        <span>{{ voiceLive ? "挂断通话" : "连续对话" }}</span>
      </button>
      <button
        v-if="!voiceLive"
        class="voice-button"
        :class="{ recording: voiceRecording }"
        type="button"
        :disabled="!hasActiveId || activeConversationArchived || !!streaming || (voiceBusy && !voiceRecording)"
        @click="emit('toggleVoiceRecording')"
      >
        <PhStopCircle v-if="voiceRecording" :size="17" weight="fill" aria-hidden="true" />
        <PhMicrophone v-else :size="17" weight="fill" aria-hidden="true" />
        <span>{{ voiceRecording ? "结束并发送" : "说一句" }}</span>
      </button>
      <button v-if="voiceBusy && !voiceRecording" class="interrupt-button" type="button" @click="emit('interruptVoice')">打断播报</button>
      <select v-model="privacy" :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy" @change="emit('privacyChanged')">
        <option value="L0">L0 · 可上云</option>
        <option value="L1">L1 · 常规</option>
        <option value="L2">L2 · 仅本地</option>
      </select>
      <label class="feature-toggle" title="开启后，文字输入也会播放 TTS 语音回复">
        <input
          v-model="textReplyVoice"
          type="checkbox"
          :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy"
          @change="emit('textReplyVoiceChanged')"
        />
        <span class="toggle-track" aria-hidden="true"><span></span></span>
        <PhSpeakerHigh :size="16" weight="bold" aria-hidden="true" />
        <span>回复播报</span>
      </label>
      <label
        v-if="locationSupported"
        class="feature-toggle"
        title="天气/附近/路线查询时自动附带本机定位（首次使用会请求浏览器授权，拒绝后回落默认城市）"
      >
        <input
          v-model="locationEnabled"
          type="checkbox"
          :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy"
          @change="emit('locationEnabledChanged')"
        />
        <span class="toggle-track" aria-hidden="true"><span></span></span>
        <PhMapPin :size="16" weight="bold" aria-hidden="true" />
        <span>自动定位</span>
      </label>
      <label
        v-if="pushSupported"
        class="feature-toggle"
        title="开启后，主动提醒/简报等会以系统通知推送到本机（PWA 关闭页面也能收到）"
      >
        <input
          v-model="notificationsEnabled"
          type="checkbox"
          :disabled="!hasToken || !!streaming || voiceRecording || voiceLive || voiceBusy"
          @change="emit('notificationsChanged')"
        />
        <span class="toggle-track" aria-hidden="true"><span></span></span>
        <PhBell :size="16" weight="bold" aria-hidden="true" />
        <span>移动通知</span>
      </label>
      <button
        class="select-toggle"
        :class="{ active: selectMode }"
        type="button"
        title="勾选若干条消息，导出为 Markdown（复制或下载），方便粘贴给外部 AI 分析"
        :disabled="!hasActiveId || !canExport"
        @click="emit('toggleSelectMode')"
      >
        <PhExport :size="16" weight="bold" aria-hidden="true" />
        <span>{{ selectMode ? "退出选择" : "选择导出" }}</span>
      </button>
      <span class="toolbar-status">
        <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
        <span class="voice-status">{{ voiceStatus }}</span>
        <span class="viseme-meter" title="实时口型幅度">
          <span class="viseme-fill" :style="{ transform: `scaleX(${voiceViseme})` }"></span>
        </span>
        <span v-if="voiceTranscript" class="voice-transcript">识别：{{ voiceTranscript }}</span>
      </span>
    </div>
    <div class="composer-row">
      <textarea
        v-model="draft"
        rows="2"
        placeholder="输入消息，Enter 发送"
        :disabled="!socketReady || activeConversationArchived"
        @keydown.enter.exact.prevent="emit('send')"
      />
      <button v-if="!streaming" class="primary send-button" type="button" :disabled="!canSend" @click="emit('send')">
        <span>发送</span>
        <PhPaperPlaneRight :size="18" weight="fill" aria-hidden="true" />
      </button>
      <button v-else type="button" @click="emit('cancelStreaming')">停止</button>
    </div>
  </footer>
</template>

<style scoped>
.composer { border-top:1px solid var(--line); padding:12px clamp(16px,2vw,26px) 14px; display:grid; gap:10px; background:color-mix(in srgb,var(--panel) 97%,var(--bg)); box-shadow:0 -8px 24px color-mix(in srgb,var(--text) 3%,transparent); }
.archived-notice { margin:0; color:var(--muted); font-size:12px; }
.composer-toolbar { display:flex; align-items:center; gap:7px; min-width:0; overflow-x:auto; overflow-y:hidden; padding-bottom:1px; scrollbar-width:none; }
.composer-toolbar::-webkit-scrollbar { display:none; }
.composer-toolbar > * { flex:none; }
.composer-toolbar select { min-width:112px; padding:7px 9px; border-radius:10px; font-size:12px; font-weight:600; }
.select-toggle,.feature-toggle { min-height:34px; border:1px solid var(--line); border-radius:10px; background:var(--panel); }
.select-toggle { display:inline-flex; align-items:center; gap:6px; padding:7px 10px; color:var(--muted); font-size:12px; }
.select-toggle.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.feature-toggle { position:relative; display:inline-flex; align-items:center; gap:6px; padding:6px 9px 6px 7px; color:var(--muted); font-size:12px; cursor:pointer; user-select:none; transition:background-color 160ms ease,border-color 160ms ease,color 160ms ease; }
.feature-toggle:hover { border-color:color-mix(in srgb,var(--accent) 38%,var(--line)); color:var(--text); }
.feature-toggle:has(input:checked) { border-color:color-mix(in srgb,var(--accent) 35%,var(--line)); background:var(--accent-soft); color:var(--accent); }
.feature-toggle input { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
.toggle-track { display:flex; align-items:center; width:26px; height:16px; padding:2px; border-radius:999px; background:color-mix(in srgb,var(--muted) 30%,var(--panel)); transition:background-color 160ms ease; }
.toggle-track span { width:12px; height:12px; border-radius:50%; background:#fff; box-shadow:0 1px 3px rgba(17,24,39,.2); transition:transform 160ms ease; }
.feature-toggle input:checked + .toggle-track { background:var(--accent); }
.feature-toggle input:checked + .toggle-track span { transform:translateX(10px); }
.feature-toggle:has(input:focus-visible) { outline:3px solid color-mix(in srgb,var(--accent) 16%,transparent); outline-offset:1px; }
.feature-toggle:has(input:disabled) { opacity:.5; cursor:not-allowed; }
.voice-button { display:inline-flex; align-items:center; gap:7px; min-height:36px; padding:8px 12px; border-radius:10px; font-size:12px; font-weight:600; }
.composer-toolbar > .voice-button:first-child { border-color:color-mix(in srgb,var(--accent) 30%,var(--line)); color:var(--accent); background:var(--accent-soft); }
.voice-button.recording { border-color:var(--danger); color:var(--danger); background:color-mix(in srgb,var(--danger) 8%,var(--panel)); }
.interrupt-button { min-height:34px; padding:7px 10px; border-color:color-mix(in srgb,var(--danger) 28%,var(--line)); color:var(--danger); font-size:12px; }
.toolbar-status { display:flex; flex:1 1 auto; align-items:center; justify-content:flex-end; gap:8px; min-width:140px; overflow:hidden; white-space:nowrap; }
.toolbar-status .status,.toolbar-status .voice-status { min-width:0; overflow:hidden; text-overflow:ellipsis; }
.toolbar-status .status:empty { display:none; }
.status { color: var(--muted); font-size: 12px; margin: 0; }
.status.error { color: var(--danger); }
.voice-status { color: var(--muted); font-size: 12px; }
.viseme-meter { width:44px; height:8px; border:1px solid var(--line); border-radius:999px; overflow:hidden; background:var(--panel2); }
.viseme-fill { display:block; width:100%; height:100%; transform-origin:left center; background:var(--accent); transition:transform 50ms linear; }
.voice-transcript { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.composer-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:stretch; }
.composer textarea { min-height:56px; resize:none; padding:12px 14px; border-radius:12px; line-height:1.45; }
.send-button { display:inline-flex; align-items:center; justify-content:center; gap:7px; min-width:82px; border-radius:12px; font-weight:600; box-shadow:0 6px 16px color-mix(in srgb,var(--accent) 22%,transparent); }

@media (max-width: 760px) {
  .composer { gap:7px; padding:9px 10px calc(9px + env(safe-area-inset-bottom)); }
  .composer-toolbar { gap:7px; }
  .composer-toolbar select { min-width:108px; }
  .toolbar-status { flex:none; min-width:auto; }
  .feature-toggle { white-space:nowrap; }
  .voice-transcript { flex:1 0 160px; }
  .composer-row { grid-template-columns:minmax(0,1fr) 56px; gap:8px; }
  .composer textarea { min-height:44px; max-height:112px; padding:10px 11px; }
  .composer-row button { min-width:56px; padding-inline:8px; }
}
</style>
