<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  ApiError,
  ChatApi,
  ChatSocket,
  THEME_CHANNEL_NAME,
  THEME_OPTIONS,
  VoiceSocket,
  broadcastThemePreference,
  readThemePreference,
  readThemeSchedule,
  safeWebStorage,
  saveThemePreference,
  matchesLocationIntent,
  type AgentReplyControl,
  type AvatarChoice,
  type AuthSession,
  type ChatMessage,
  type ClientLocationPayload,
  type Conversation,
  type PrivacyLevel,
  type SocketEvent,
  type ThemePreference,
  type ThemeSchedule,
  type ToolPresentation,
  type VoiceControlEvent,
} from "@aria/shared";
import {
  PcmMicrophoneCapture,
  VoicePlaybackQueue,
  type VoiceSentenceMeta,
  type VoiceVisemeFrame,
} from "./voice";
import ToolResultCard from "./ToolResultCard.vue";
import PlanInbox from "./PlanInbox.vue";
import MailAttachments from "./MailAttachments.vue";
import MailDrafts from "./MailDrafts.vue";
import SafetyAlerts from "./SafetyAlerts.vue";
import ConfirmationDrafts from "./ConfirmationDrafts.vue";
import Live2DStage from "./Live2DStage.vue";
import MarkdownContent from "./MarkdownContent.vue";
import {
  disablePushNotifications,
  enablePushNotifications,
  pushSubscriptionState,
} from "./push";
import {
  copyMarkdown,
  downloadMarkdown,
  exportFileName,
  formatConversationMarkdown,
} from "./export";

// 聊天前端主组件：登录 → 会话侧栏 → 流式消息区 → 发送区。
// 普通 Web 保留既有本地会话；安装后的 PWA 只在当前会话存储令牌。
const api = new ChatApi();
const TOKEN_KEY = "ariaChatToken";
const TEXT_REPLY_VOICE_KEY = "ariaTextReplyVoice";
const LOCATION_ENABLED_KEY = "ariaLocationEnabled";
const themePreference = ref<ThemePreference>(readThemePreference());
const isStandalone =
  window.matchMedia("(display-mode: standalone)").matches ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true;
const isIos =
  /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const authStorage = safeWebStorage(isStandalone ? "session" : "local");
// 与服务端 tools/location.py 的 LOCATION_TTL(15 分钟)保持一致。
const LOCATION_TTL_MS = 15 * 60 * 1000;

const token = ref<string>("");
const displayName = ref<string>("");
const setupRequired = ref(false);
const password = ref("");
const adminToken = ref("");
const authBusy = ref(false);
const statusText = ref("");
const statusError = ref(false);
const personaMeta = ref<{ version: number; name: string } | null>(null);
const avatarMeta = ref<{
  instanceId: string;
  packId: string;
  name: string;
  engine: "static" | "live2d" | "vrm" | "abstract";
  assets: { thumbnail?: string; emotions?: Record<string, string>; model?: string };
} | null>(null);
const avatars = ref<AvatarChoice[]>([]);
const avatarSwitchBusy = ref(false);

const conversations = ref<Conversation[]>([]);
const archivedConversations = ref<Conversation[]>([]);
const showArchivedConversations = ref(false);
const activeId = ref<string | null>(null);
const messagesByConversation = reactive(new Map<string, ChatMessage[]>());
const draft = ref("");
const privacy = ref<PrivacyLevel>("L1");
const streaming = ref<{
  conversationId: string;
  generationId: string;
  text: string;
  emotion: string | null;
  expression?: string | null;
} | null>(null);
const socketReady = ref(false);
// PC-02：计划执行进度面板（plan.execution 事件驱动，展示目标/步骤/进度/证据与停止按钮）
const planRefresh = ref(0);
const messagesRoot = ref<HTMLElement | null>(null);
const voiceReady = ref(false);
const voiceAsrConfigured = ref<boolean | null>(null);
const voiceAsrBlockMessage = ref("");
const voiceRecording = ref(false);
// 连续对话（电话模式）：麦克风常开，服务端自动断句即发送、说话即打断。
const voiceLive = ref(false);
const voiceBusy = ref(false);
const sendPending = ref(false);
const voiceStatus = ref("语音未连接");
const voiceTranscript = ref("");
const voiceVadBackend = ref("");
const voiceViseme = ref(0);
const avatarMotion = ref<{ value: string; sequence: number } | null>(null);
const textReplyVoice = ref(localStorage.getItem(TEXT_REPLY_VOICE_KEY) === "1");
const voiceTtsConfigured = ref<boolean | null>(null);
const mobileConversationsOpen = ref(false);
const mobileCompanionOpen = ref(false);
const installPrompt = ref<BeforeInstallPromptEvent | null>(null);
const networkOnline = ref(navigator.onLine);
let resumePromise: Promise<void> | null = null;

interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

// 终端定位：命中位置类查询时才请求/附带(惰性授权)，坐标只随帧内存传输。
const locationEnabled = ref(localStorage.getItem(LOCATION_ENABLED_KEY) !== "0");
const locationSupported = typeof navigator !== "undefined" && "geolocation" in navigator;
const locationPolicy = ref<"ask_each_time" | "allow_session">("ask_each_time");
let cachedLocation: { payload: ClientLocationPayload; at: number } | null = null;

function freshCachedLocation(): ClientLocationPayload | null {
  if (cachedLocation && Date.now() - cachedLocation.at < LOCATION_TTL_MS) {
    return cachedLocation.payload;
  }
  cachedLocation = null;
  return null;
}

function fetchBrowserPosition(): Promise<ClientLocationPayload | null> {
  return new Promise((resolve) => {
    if (!locationSupported) return resolve(null);
    if (locationPolicy.value === "allow_session") {
      const cached = freshCachedLocation();
      if (cached) return resolve(cached);
    }
    // ask_each_time: 每次都取新位置(maximumAge 0); 浏览器授权弹窗只在首次出现。
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const payload: ClientLocationPayload = {
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy_m: Math.round(position.coords.accuracy ?? 0),
        };
        cachedLocation = { payload, at: Date.now() };
        resolve(payload);
      },
      () => resolve(null),
      {
        timeout: 8000,
        maximumAge: locationPolicy.value === "allow_session" ? LOCATION_TTL_MS : 0,
        enableHighAccuracy: false,
      },
    );
  });
}

async function resolveLocationForText(text: string): Promise<ClientLocationPayload | null> {
  if (!locationEnabled.value || !locationSupported) return null;
  if (privacy.value === "L2" || !matchesLocationIntent(text)) return null;
  const payload = await fetchBrowserPosition();
  if (!payload) setStatus("未获取到本机定位，将使用默认城市", false);
  return payload;
}

function onLocationEnabledChanged() {
  localStorage.setItem(LOCATION_ENABLED_KEY, locationEnabled.value ? "1" : "0");
  if (!locationEnabled.value) cachedLocation = null;
}

const pushSupported =
  typeof Notification !== "undefined" && "serviceWorker" in navigator && "PushManager" in window;
const notificationsEnabled = ref(false);
let pushToggling = false;

async function refreshPushToggle() {
  if (!pushSupported || !token.value) {
    notificationsEnabled.value = false;
    return;
  }
  notificationsEnabled.value = await pushSubscriptionState(api, token.value);
}

async function onNotificationsChanged() {
  if (pushToggling) return;
  pushToggling = true;
  try {
    if (notificationsEnabled.value) {
      const result = await enablePushNotifications(api, token.value);
      if (result === "enabled") {
        setStatus("已开启移动通知");
      } else {
        notificationsEnabled.value = false;
        setStatus(
          result === "denied"
            ? "浏览器拒绝了通知权限，请在系统设置中允许"
            : result === "disabled"
              ? "服务端尚未启用移动推送（需配置 VAPID 密钥）"
              : result === "unsupported"
                ? "当前浏览器不支持移动推送"
                : "开启移动通知失败，请稍后再试",
          result === "failed" || result === "denied",
        );
      }
    } else {
      await disablePushNotifications(api, token.value);
      setStatus("已关闭移动通知");
    }
  } finally {
    pushToggling = false;
  }
}

async function onThemeChanged() {
  // 定时切换时沿用当前存储的时段边界（时段编辑入口在管理后台主题中心）
  const schedule = themePreference.value === "scheduled" ? readThemeSchedule() : undefined;
  saveThemePreference(themePreference.value, schedule);
  broadcastThemePreference(themePreference.value, schedule);
  if (!token.value) return;
  try {
    await api.updateThemePreference(token.value, themePreference.value, schedule);
    setStatus("主题已同步");
  } catch (error) {
    setStatus(error instanceof Error ? `${error.message}；已保存在本机` : "主题同步失败；已保存在本机", true);
  }
}

async function syncThemePreference() {
  if (!token.value) return;
  try {
    const preference = await api.themePreference(token.value);
    if (preference.selection !== themePreference.value) {
      themePreference.value = preference.selection;
      saveThemePreference(preference.selection, preference.schedule ?? undefined);
    }
  } catch {
    /* 服务不可用时保留本机最后可用主题 */
  }
}

const asrUnavailableLabels: Record<string, string> = {
  not_configured: "后台尚未启用语音识别",
  local_asr_required: "当前是 L2，仅允许本地语音识别",
  faster_whisper_not_installed: "本机尚未安装 faster-whisper",
  model_load_failed: "本地语音模型加载失败",
  transcription_failed: "本地语音转写失败",
  provider_error: "语音识别服务调用失败",
};

let socket: ChatSocket | null = null;
let reconnectTimer: number | null = null;
let reconnectAttempts = 0;
let themeChannel: BroadcastChannel | null = null;
let voiceSocket: VoiceSocket | null = null;
let voiceSocketKey = "";
let voiceConnectPromise: Promise<VoiceSocket> | null = null;
let voiceConnectKey = "";
let voiceSentence: {
  meta: VoiceSentenceMeta;
  chunks: ArrayBuffer[];
  visemes: VoiceVisemeFrame[];
} | null = null;
const microphone = new PcmMicrophoneCapture();
const playback = new VoicePlaybackQueue();

function unlockVoicePlayback() {
  void playback.unlock().then((unlocked) => {
    if (!unlocked) voiceStatus.value = "浏览器尚未允许播放声音，请关闭再开启“文字回复播报”";
  }).catch((error: unknown) => {
    voiceStatus.value = error instanceof Error
      ? `无法启用声音：${error.message}`
      : "浏览器尚未允许播放声音";
  });
}

const activeMessages = computed(() =>
  activeId.value ? (messagesByConversation.get(activeId.value) ?? []) : [],
);

function mergeMessages(
  conversationId: string,
  incoming: ChatMessage[],
  replace = false,
): ChatMessage[] {
  const merged = new Map<string, ChatMessage>();
  if (!replace) {
    for (const message of messagesByConversation.get(conversationId) ?? []) {
      merged.set(message.id, message);
    }
  }
  for (const message of incoming) merged.set(message.id, message);
  const messages = [...merged.values()].sort((left, right) => left.seq - right.seq);
  messagesByConversation.set(conversationId, messages);
  const conversation = conversations.value.find((item) => item.id === conversationId);
  if (conversation && messages.length) {
    conversation.last_seq = Math.max(conversation.last_seq, messages[messages.length - 1]!.seq);
  }
  return messages;
}

async function pullMessagesAfter(conversationId: string, afterSeq: number): Promise<void> {
  const pageSize = 500;
  let cursor = afterSeq;
  while (true) {
    const page = await api.listMessages(token.value, conversationId, cursor, pageSize);
    mergeMessages(conversationId, page);
    if (page.length < pageSize) return;
    const nextCursor = page.reduce((latest, message) => Math.max(latest, message.seq), cursor);
    if (nextCursor <= cursor) return;
    cursor = nextCursor;
  }
}
const avatarEmotion = computed(() => {
  if (streaming.value?.emotion) return streaming.value.emotion;
  const latest = [...activeMessages.value]
    .reverse()
    .find((message) => message.role === "assistant" && emotionKeyOf(message));
  return latest ? (emotionKeyOf(latest) ?? "neutral") : "neutral";
});
const avatarExpression = computed(() => {
  if (streaming.value?.expression) return streaming.value.expression;
  const latest = [...activeMessages.value]
    .reverse()
    .find((message) => message.role === "assistant" && agentReplyOf(message)?.expressions?.length);
  return latest ? (agentReplyOf(latest)?.expressions?.[0] ?? null) : null;
});
const avatarSpeaking = computed(
  () => voiceViseme.value > 0.001 || voiceBusy.value || Boolean(streaming.value?.text),
);
const avatarImageUrl = computed(() => {
  const assets = avatarMeta.value?.assets;
  return assets?.emotions?.[avatarEmotion.value] ?? assets?.thumbnail ?? null;
});
const activeConversationArchived = computed(() =>
  archivedConversations.value.some((conversation) => conversation.id === activeId.value),
);
const canSend = computed(
  () =>
    socketReady.value &&
    !!activeId.value &&
    !activeConversationArchived.value &&
    draft.value.trim().length > 0 &&
    !streaming.value &&
    !sendPending.value &&
    !voiceBusy.value &&
    !voiceRecording.value &&
    !voiceLive.value,
);
const visibleConversations = computed(() =>
  showArchivedConversations.value ? archivedConversations.value : conversations.value,
);

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}

async function closeVoice() {
  voiceRecording.value = false;
  voiceLive.value = false;
  voiceBusy.value = false;
  voiceReady.value = false;
  voiceAsrConfigured.value = null;
  voiceTtsConfigured.value = null;
  voiceAsrBlockMessage.value = "";
  voiceSocketKey = "";
  voiceConnectPromise = null;
  voiceConnectKey = "";
  voiceSentence = null;
  voiceSocket?.close();
  voiceSocket = null;
  playback.interrupt();
  await microphone.stop();
  voiceStatus.value = "语音未连接";
  voiceTranscript.value = "";
  voiceViseme.value = 0;
}

async function ensureVoiceSocket(continuous = false): Promise<VoiceSocket> {
  if (!activeId.value) throw new Error("请先选择会话");
  const conversationId = activeId.value;
  const privacyLevel = privacy.value;
  const key = `${conversationId}:${privacyLevel}:${continuous ? "live" : "auto"}`;
  if (
    voiceSocket &&
    voiceReady.value &&
    voiceSocketKey === key
  ) {
    return voiceSocket;
  }
  if (voiceConnectPromise && voiceConnectKey === key) return voiceConnectPromise;
  const connecting = (async () => {
    await closeVoice();
    voiceStatus.value = "正在连接语音…";
    const next = new VoiceSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/voice",
      token.value,
      {
        onEvent: handleVoiceEvent,
        onAudio: handleVoiceAudio,
        onClose: () => {
          if (voiceSocket === next) {
            voiceReady.value = false;
            voiceBusy.value = false;
            voiceLive.value = false;
            if (streaming.value?.conversationId === activeId.value) {
              streaming.value = null;
            }
            voiceStatus.value = "语音连接已断开";
            setStatus("语音连接已断开，请重新发送", true);
          }
        },
      },
    );
    voiceSocket = next;
    voiceSocketKey = key;
    try {
      // 纯语音回合没有输入帧, hello 时带上新鲜缓存位置(不触发授权弹窗)。
      await next.connect(conversationId, privacyLevel, freshCachedLocation(), { continuous });
      return next;
    } catch (error) {
      if (voiceSocket === next) {
        voiceSocket = null;
        voiceSocketKey = "";
        voiceReady.value = false;
      }
      throw error;
    }
  })();
  voiceConnectPromise = connecting;
  voiceConnectKey = key;
  try {
    return await connecting;
  } finally {
    if (voiceConnectPromise === connecting) {
      voiceConnectPromise = null;
      voiceConnectKey = "";
    }
  }
}

function handleVoiceAudio(chunk: ArrayBuffer) {
  if (!voiceSentence) return;
  voiceSentence.chunks.push(chunk);
}

function handleVoiceEvent(event: VoiceControlEvent) {
  switch (event.type) {
    case "voice.ready":
      voiceReady.value = true;
      voiceTtsConfigured.value = event.tts_configured ?? false;
      voiceVadBackend.value = event.vad_backend === "silero" ? "Silero VAD" : "能量 VAD";
      if (event.asr_configured === false) {
        voiceAsrConfigured.value = false;
        voiceAsrBlockMessage.value = "后台尚未启用语音识别";
      } else if (privacy.value === "L2" && event.asr_runs_local === false) {
        voiceAsrConfigured.value = false;
        voiceAsrBlockMessage.value = "当前是 L2，需要在后台启用 faster-whisper 本地 ASR";
      } else {
        voiceAsrConfigured.value = true;
        voiceAsrBlockMessage.value = "";
      }
      if (textReplyVoice.value) {
        voiceStatus.value = voiceTtsConfigured.value
          ? "文字语音回复已就绪"
          : "未配置语音合成，回复将仅显示文字";
      } else if (voiceLive.value) {
        voiceStatus.value = "通话已连接，请说话";
      } else {
        voiceStatus.value = voiceAsrConfigured.value
          ? `语音已就绪 · ${event.asr_runs_local ? "本地识别" : "云端识别"} · ${event.vad_backend === "silero" ? "Silero VAD" : "Energy VAD"}${event.wake_word_configured ? " · 唤醒词待命" : ""}`
          : voiceAsrBlockMessage.value;
      }
      break;
    case "voice.wake_detected":
      voiceStatus.value = "已唤醒，开始说吧…";
      break;
    case "voice.wake_unavailable":
      voiceStatus.value = "唤醒词不可用，已切回自动监听 / PTT";
      break;
    case "voice.microphone_preempted":
      // 另一设备抢占了麦克风：本地立即停止采集（服务端已丢弃本连接话语）
      if (voiceRecording.value || voiceLive.value) {
        voiceRecording.value = false;
        voiceLive.value = false;
        void microphone.stop();
        voiceStatus.value = "麦克风已被其他设备接管，通话结束";
      }
      break;
    case "voice.audio_preempted":
      // 音频输出被其他设备/桌宠抢占：清空本地播放队列，文字仍会正常到达
      playback.interrupt();
      voiceSentence = null;
      voiceStatus.value = "语音播报已由其他设备接管";
      break;
    case "voice.partial_transcript":
      // 流式 ASR 实时字幕（docs/04 §6.4 P1）：断句前即时显示识别中的文字
      voiceTranscript.value = event.text ?? "";
      voiceStatus.value = "正在识别…";
      break;
    case "voice.transcript":
      voiceTranscript.value = event.text ?? "";
      voiceStatus.value = textReplyVoice.value
        ? "文字已提交，正在生成回复…"
        : "已识别，正在生成回复…";
      break;
    case "turn.accepted":
      voiceBusy.value = true;
      voiceStatus.value = "正在生成语音回复…";
      if (activeId.value && event.generation_id) {
        streaming.value = {
          conversationId: activeId.value,
          generationId: event.generation_id,
          text: "",
          emotion: null,
        };
      }
      if (activeId.value) void refreshMessages(activeId.value);
      break;
    case "reply.delta":
      if (activeId.value && event.generation_id) {
        if (streaming.value?.generationId === event.generation_id) {
          streaming.value.text += event.delta ?? "";
        } else {
          streaming.value = {
            conversationId: activeId.value,
            generationId: event.generation_id,
            text: event.delta ?? "",
            emotion: null,
          };
        }
        void scrollToEnd();
      }
      break;
    case "tool.started":
      voiceStatus.value = String(event.label ?? "正在查询外部信息…");
      break;
    case "tool.finished":
      voiceStatus.value = event.ok === false
        ? `查询未完成：${String(event.reason_code ?? "unknown")}`
        : "查询完成，正在组织回复…";
      break;
    case "voice.sentence":
      voiceSentence = {
        meta: {
          generationId: event.generation_id ?? null,
          index: event.index ?? 0,
          mime: event.mime ?? "audio/pcm;rate=24000",
          sampleRate: event.sample_rate ?? 24_000,
          provider: event.provider ?? null,
        },
        chunks: [],
        visemes: [],
      };
      voiceStatus.value = "正在接收语音…";
      break;
    case "voice.sentence.end":
      if (voiceSentence && (event.index ?? voiceSentence.meta.index) === voiceSentence.meta.index) {
        playback.enqueue(
          voiceSentence.meta,
          voiceSentence.chunks,
          voiceSentence.visemes,
          (amp) => {
            voiceViseme.value = amp;
          },
          (message) => {
            voiceBusy.value = false;
            voiceViseme.value = 0;
            voiceStatus.value = `语音播放失败：${message}`;
          },
        );
        voiceSentence = null;
        voiceStatus.value = "正在播放回复…";
      }
      break;
    case "voice.viseme":
      if (voiceSentence && (event.sentence_index ?? voiceSentence.meta.index) === voiceSentence.meta.index) {
        voiceSentence.visemes.push({
          amp: Math.max(0, Math.min(1, Number(event.amp ?? 0))),
          offsetMs: Math.max(0, Number(event.offset_ms ?? 0)),
          durationMs: Math.max(0, Number(event.duration_ms ?? 50)),
        });
      }
      break;
    case "voice.interrupted":
      voiceBusy.value = false;
      if (streaming.value?.generationId === event.generation_id) streaming.value = null;
      playback.interrupt();
      voiceSentence = null;
      voiceViseme.value = 0;
      voiceStatus.value = voiceLive.value ? "已打断，请继续说" : "已打断";
      break;
    case "turn.cancelled":
      voiceBusy.value = false;
      if (streaming.value?.generationId === event.generation_id) streaming.value = null;
      playback.interrupt();
      voiceSentence = null;
      voiceViseme.value = 0;
      voiceStatus.value = voiceLive.value ? "已打断，请继续说" : "语音回合已取消";
      break;
    case "turn.failed":
      voiceBusy.value = false;
      if (streaming.value?.generationId === event.generation_id) streaming.value = null;
      playback.interrupt();
      voiceSentence = null;
      voiceViseme.value = 0;
      voiceStatus.value = `语音生成失败：${event.reason_code ?? "unknown"}`;
      break;
    case "voice.asr_unavailable":
      voiceBusy.value = false;
      voiceStatus.value = `语音识别不可用：${asrUnavailableLabels[event.reason ?? "not_configured"] ?? event.reason ?? "unknown"}`;
      break;
    case "voice.tts_unavailable":
      voiceStatus.value = `语音合成不可用，将仅显示文字：${event.reason ?? "not_configured"}`;
      break;
    case "proactive.committed":
      voiceStatus.value = `主动提醒：${event.content ?? ""}`;
      break;
    case "voice.error":
      voiceBusy.value = false;
      voiceStatus.value = `语音请求失败：${event.reason ?? "unknown"}`;
      break;
    case "reply.committed":
      voiceBusy.value = false;
      voiceStatus.value = voiceLive.value ? "回复完成，请继续说" : "语音回复已完成";
      if (activeId.value) {
        const conversationId = activeId.value;
        const generationId = event.generation_id;
        void refreshMessages(conversationId).finally(() => {
          if (streaming.value?.generationId === generationId) streaming.value = null;
        });
      } else if (streaming.value?.generationId === event.generation_id) {
        streaming.value = null;
      }
      void loadRuntimeMeta();
      break;
  }
}

async function refreshMessages(conversationId: string) {
  try {
    await pullMessagesAfter(conversationId, lastSeqOf(conversationId));
    if (conversationId === activeId.value) await scrollToEnd();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "刷新语音消息失败", true);
  }
}

async function toggleVoiceRecording() {
  if (voiceLive.value) return;
  if (voiceRecording.value) {
    voiceRecording.value = false;
    voiceBusy.value = true;
    voiceSocket?.endUtterance();
    await microphone.stop();
    voiceStatus.value = "正在识别…";
    return;
  }
  if (!activeId.value || streaming.value || voiceBusy.value) return;
  try {
    // 麦克风按钮是移动端可信用户手势，同时解锁稍后的 TTS 播放。
    unlockVoicePlayback();
    const current = await ensureVoiceSocket();
    if (voiceAsrConfigured.value === false) {
      voiceStatus.value = voiceAsrBlockMessage.value || "语音识别当前不可用";
      return;
    }
    await microphone.start((chunk) => {
      if (voiceRecording.value) voiceSocket?.sendPcm(chunk);
    });
    current.beginUtterance();
    voiceTranscript.value = "";
    voiceRecording.value = true;
    voiceStatus.value = "正在听你说话…";
  } catch (error) {
    voiceRecording.value = false;
    await microphone.stop();
    voiceStatus.value = error instanceof Error ? `麦克风不可用：${error.message}` : "麦克风不可用";
  }
}

/**
 * 连续对话（电话模式）：接通后麦克风常开，不再逐句点开始/结束。
 * 服务端自动断句——停顿即发送；播报中一开口即打断（barge-in）；
 * 回复完毕继续待命，直到点挂断。
 */
async function toggleVoiceCall() {
  if (voiceLive.value) {
    await closeVoice();
    return;
  }
  if (!activeId.value || streaming.value || voiceBusy.value || voiceRecording.value) return;
  try {
    // 通话按钮是移动端可信用户手势，同时解锁稍后的 TTS 播放。
    unlockVoicePlayback();
    const socket = await ensureVoiceSocket(true);
    if (voiceAsrConfigured.value === false) {
      voiceStatus.value = voiceAsrBlockMessage.value || "语音识别当前不可用";
      return;
    }
    await microphone.start((chunk) => {
      if (voiceLive.value) voiceSocket?.sendPcm(chunk);
    });
    voiceLive.value = true;
    voiceTranscript.value = "";
    voiceStatus.value = voiceVadBackend.value
      ? `通话已连接 · ${voiceVadBackend.value} · 请说话`
      : "通话已连接，请说话";
  } catch (error) {
    voiceLive.value = false;
    await microphone.stop();
    voiceStatus.value = error instanceof Error ? `麦克风不可用：${error.message}` : "麦克风不可用";
  }
}

function interruptVoice() {
  playback.interrupt();
  voiceSentence = null;
  voiceSocket?.interrupt();
  voiceStatus.value = "已请求打断";
}

function onPrivacyChanged() {
  if (voiceReady.value || voiceRecording.value) void closeVoice();
}

function onTextReplyVoiceChanged() {
  localStorage.setItem(TEXT_REPLY_VOICE_KEY, textReplyVoice.value ? "1" : "0");
  if (textReplyVoice.value && activeId.value) {
    // 必须在 change 手势仍生效时创建/恢复 AudioContext。
    unlockVoicePlayback();
    voiceStatus.value = "正在连接语音…";
    void ensureVoiceSocket().catch((error) => {
      voiceStatus.value = error instanceof Error ? error.message : "语音连接失败";
    });
  } else if (!voiceRecording.value) {
    void closeVoice();
  }
  if (!textReplyVoice.value) voiceStatus.value = "已关闭文字回复播报";
}

function personaVersionOf(message: ChatMessage): number | null {
  const value = (message.decision_meta as { persona_version?: unknown } | null)?.persona_version;
  return typeof value === "number" && value > 0 ? value : null;
}

const recallLabels: Record<string, string> = {
  working: "当前上下文",
  memory: "长期记忆",
  timeline: "历史回溯",
  source: "历史回溯",
  none: "未找到历史",
};

function recallLabelOf(message: ChatMessage): string | null {
  const recall = (
    message.decision_meta as { recall?: { mode?: unknown } } | null
  )?.recall;
  const mode = recall?.mode;
  return typeof mode === "string" ? (recallLabels[mode] ?? null) : null;
}

function toolResultOf(message: ChatMessage): ToolPresentation | null {
  const value = (message.decision_meta as { tool_result?: unknown } | null)?.tool_result;
  if (!value || typeof value !== "object") return null;
  const kind = (value as { kind?: unknown }).kind;
  return ["location_ambiguous", "weather", "nearby", "route"].includes(String(kind))
    ? value as ToolPresentation
    : null;
}

async function chooseLocation(name: string) {
  draft.value = `请使用地点“${name}”继续刚才的查询`;
  await nextTick();
  if (canSend.value) await send();
}

function messageTimeOf(message: ChatMessage): string {
  const date = new Date(message.created_at);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

async function loadRuntimeMeta() {
  try {
    const runtime = await api.runtimeMeta();
    personaMeta.value = runtime.persona
      ? { version: runtime.persona.version, name: runtime.persona.name }
      : null;
    avatarMeta.value = runtime.avatar
      ? {
          instanceId: runtime.avatar.instance_id,
          packId: runtime.avatar.pack_id,
          name: runtime.avatar.name,
          engine: runtime.avatar.engine,
          assets: runtime.avatar.assets,
        }
      : null;
    if (runtime.location_policy) locationPolicy.value = runtime.location_policy.precise;
  } catch {
    personaMeta.value = null;
    avatarMeta.value = null;
  }
}

async function loadAvatars() {
  if (!token.value) return;
  try {
    avatars.value = await api.listAvatars(token.value);
  } catch {
    avatars.value = [];
  }
}

async function switchAvatar(event: Event) {
  const instanceId = (event.target as HTMLSelectElement).value;
  if (!instanceId || instanceId === avatarMeta.value?.instanceId) return;
  avatarSwitchBusy.value = true;
  try {
    await api.switchAvatar(token.value, instanceId);
    await Promise.all([loadRuntimeMeta(), loadAvatars()]);
    setStatus("形象已切换，记忆与当前会话保持不变");
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "形象切换失败", true);
  } finally {
    avatarSwitchBusy.value = false;
  }
}

async function scrollToEnd() {
  await nextTick();
  messagesRoot.value?.scrollTo({ top: messagesRoot.value.scrollHeight });
}

const emotionLabels: Record<string, string> = {
  neutral: "平静",
  happy: "开心",
  sad: "难过",
  angry: "生气",
  surprised: "惊讶",
  thinking: "思考",
  concerned: "关切",
};

function emotionKeyOf(message: ChatMessage): string | null {
  const reply = agentReplyOf(message);
  return reply?.emotion ?? null;
}

function agentReplyOf(message: ChatMessage): AgentReplyControl | null {
  return (message.decision_meta as { agent_reply?: AgentReplyControl } | null)?.agent_reply ?? null;
}

function emotionOf(message: ChatMessage): string | null {
  const emotion = emotionKeyOf(message);
  return emotion ? (emotionLabels[emotion] ?? emotion) : null;
}

function rememberSession(session: AuthSession) {
  token.value = session.access_token;
  displayName.value = session.user.display_name;
  authStorage.setItem(TOKEN_KEY, session.access_token);
}

async function submitAuth() {
  if (!password.value) return;
  authBusy.value = true;
  setStatus("");
  try {
    const session = setupRequired.value
      ? await api.setup(password.value, adminToken.value)
      : await api.login(password.value);
    rememberSession(session);
    await enterChat();
    void refreshPushToggle();
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "连接失败";
    setStatus(message, true);
  } finally {
    authBusy.value = false;
  }
}

async function logout() {
  const current = token.value;
  closeSocket();
  await closeVoice();
  if (current && pushSupported) {
    await disablePushNotifications(api, current);
    notificationsEnabled.value = false;
  }
  token.value = "";
  displayName.value = "";
  authStorage.removeItem(TOKEN_KEY);
  conversations.value = [];
  archivedConversations.value = [];
  activeId.value = null;
  messagesByConversation.clear();
  if (current) {
    try {
      await api.logout(current);
    } catch {
      /* session already gone server-side */
    }
  }
}

/**
 * 会话过期（登录态 8 小时 TTL）：轮询面板收到 401 时触发。WebSocket 是
 * 长连接、只在建连时鉴权，所以聊天看似正常而 REST 轮询持续 401——这里
 * 统一登出并明确提示，停止无效轮询刷屏。
 */
let sessionExpiring = false;
ChatApi.onUnauthorized = () => {
  if (!token.value || sessionExpiring) return;
  sessionExpiring = true;
  void (async () => {
    closeSocket();
    await closeVoice();
    token.value = "";
    displayName.value = "";
    authStorage.removeItem(TOKEN_KEY);
    conversations.value = [];
    archivedConversations.value = [];
    activeId.value = null;
    messagesByConversation.clear();
    setStatus("登录已过期，请重新登录", true);
    sessionExpiring = false;
  })();
}

function closeSocket() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  socket?.close();
  socket = null;
  socketReady.value = false;
}

async function enterChat() {
  await syncThemePreference();
  await loadRuntimeMeta();
  await loadAvatars();
  await loadConversations();
  connectSocket();
}

async function loadConversations() {
  try {
    [conversations.value, archivedConversations.value] = await Promise.all([
      api.listConversations(token.value, "active"),
      api.listConversations(token.value, "archived"),
    ]);
    if (!activeId.value && conversations.value.length) {
      await openConversation(conversations.value[0]!.id);
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await logout();
      setStatus("会话已过期，请重新登录", true);
      return;
    }
    setStatus(error instanceof Error ? error.message : "加载会话失败", true);
  }
}

async function openConversation(id: string) {
  if (activeId.value !== id) await closeVoice();
  // 切换会话后勾选集合不再有意义，退出选择模式。
  selectMode.value = false;
  selectedMessageIds.value = new Set();
  activeId.value = id;
  mobileConversationsOpen.value = false;
  if (!messagesByConversation.has(id)) {
    try {
      const messages = await api.listMessages(token.value, id);
      mergeMessages(id, messages, true);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "加载消息失败", true);
      return;
    }
  }
  socket?.sync(id, lastSeqOf(id));
  if (textReplyVoice.value) {
    void ensureVoiceSocket().catch((error) => {
      voiceStatus.value = error instanceof Error ? error.message : "语音连接失败";
    });
  }
  await scrollToEnd();
}

function lastSeqOf(id: string) {
  const messages = messagesByConversation.get(id) ?? [];
  return messages.reduce((latest, message) => Math.max(latest, message.seq ?? 0), 0);
}

async function createConversation() {
  try {
    const conversation = await api.createConversation(token.value, `会话 ${conversations.value.length + 1}`);
    conversations.value.unshift(conversation);
    messagesByConversation.set(conversation.id, []);
    await openConversation(conversation.id);
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "创建会话失败", true);
  }
}

async function selectConversationGroup(archived: boolean) {
  if (showArchivedConversations.value === archived) return;
  await closeVoice();
  showArchivedConversations.value = archived;
  activeId.value = null;
  const first = archived ? archivedConversations.value[0] : conversations.value[0];
  if (first) await openConversation(first.id);
}

async function removeConversation(id: string) {
  const conversation = [...conversations.value, ...archivedConversations.value]
    .find((item) => item.id === id);
  const label = conversation?.title ?? "该会话";
  if (!confirm(`删除「${label}」？消息与由它沉淀的记忆会被一并删除，不可恢复。`)) return;
  try {
    if (activeId.value === id) await closeVoice();
    await api.deleteConversation(token.value, id);
    conversations.value = conversations.value.filter((item) => item.id !== id);
    archivedConversations.value = archivedConversations.value.filter((item) => item.id !== id);
    messagesByConversation.delete(id);
    if (activeId.value === id) {
      activeId.value = null;
      const first = visibleConversations.value[0];
      if (first) await openConversation(first.id);
    }
    setStatus("会话已删除");
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "删除失败", true);
  }
}

async function archiveConversation(id: string) {
  try {
    if (activeId.value === id) await closeVoice();
    const archived = await api.archiveConversation(token.value, id);
    conversations.value = conversations.value.filter((item) => item.id !== id);
    archivedConversations.value.unshift(archived);
    if (activeId.value === id) {
      activeId.value = null;
      if (conversations.value.length) await openConversation(conversations.value[0]!.id);
    }
    setStatus("会话已归档，可在归档列表中恢复");
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "归档失败", true);
  }
}

async function restoreConversation(id: string) {
  try {
    const restored = await api.restoreConversation(token.value, id);
    archivedConversations.value = archivedConversations.value.filter((item) => item.id !== id);
    conversations.value.unshift(restored);
    if (activeId.value === id) {
      activeId.value = null;
      const first = archivedConversations.value[0];
      if (first) await openConversation(first.id);
    }
    setStatus("会话已恢复");
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "恢复失败", true);
  }
}

// 会话导出：整会话下载 .md；选择模式下勾选若干条，复制或下载节选，
// 方便把对话粘贴给外部 AI / 协作者分析。
const selectMode = ref(false);
const selectedMessageIds = ref(new Set<string>());

const selectableMessages = computed(() =>
  activeMessages.value.filter((message) => message.role !== "system"),
);
const selectedMessages = computed(() =>
  selectableMessages.value.filter((message) => selectedMessageIds.value.has(message.id)),
);

function toggleSelectMode() {
  selectMode.value = !selectMode.value;
  selectedMessageIds.value = new Set();
}

function toggleMessageSelected(id: string) {
  const next = new Set(selectedMessageIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedMessageIds.value = next;
}

function selectAllMessages() {
  selectedMessageIds.value = new Set(selectableMessages.value.map((message) => message.id));
}

function conversationTitleOf(id: string | null): string {
  const conversation = [...conversations.value, ...archivedConversations.value]
    .find((item) => item.id === id);
  return conversation?.title || "未命名会话";
}

async function exportFullConversation(id: string) {
  try {
    setStatus("正在导出会话…");
    // 从头分页拉全量，避免只导出当前已加载的部分。
    await pullMessagesAfter(id, 0);
    const messages = (messagesByConversation.get(id) ?? []).filter((message) => message.role !== "system");
    if (!messages.length) {
      setStatus("该会话还没有可导出的消息", true);
      return;
    }
    const title = conversationTitleOf(id);
    downloadMarkdown(exportFileName(title), formatConversationMarkdown({ conversationTitle: title, messages }));
    setStatus(`已导出 ${messages.length} 条消息`);
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "导出失败", true);
  }
}

async function exportSelected(action: "copy" | "download") {
  const messages = selectedMessages.value;
  if (!messages.length) {
    setStatus("请先勾选要导出的消息", true);
    return;
  }
  const title = conversationTitleOf(activeId.value);
  const markdown = formatConversationMarkdown({ conversationTitle: title, messages, excerptOnly: true });
  if (action === "copy") {
    const ok = await copyMarkdown(markdown);
    setStatus(
      ok ? `已复制 ${messages.length} 条消息的 Markdown，可直接粘贴` : "复制失败，请改用下载",
      !ok,
    );
  } else {
    downloadMarkdown(exportFileName(title, true), markdown);
    setStatus(`已下载 ${messages.length} 条消息的节选`);
  }
}

/** 建立实时连接；失败时保留基础 REST 可用性并提示 */
function connectSocket() {
  closeSocket();
  let nextSocket: ChatSocket;
  nextSocket = new ChatSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat",
    token.value,
    {
      onEvent: (event) => {
        if (socket === nextSocket) handleEvent(event);
      },
      onClose: (code) => {
        if (socket === nextSocket) handleClose(code);
      },
    },
  );
  socket = nextSocket;
  nextSocket
    .connect()
    .then(() => {
      if (socket !== nextSocket) return;
      socketReady.value = true;
      reconnectAttempts = 0;
      setStatus("已连接");
      if (activeId.value) socket?.sync(activeId.value, lastSeqOf(activeId.value));
    })
    .catch(() => {
      if (socket === nextSocket) setStatus("实时连接失败，仍可基础使用", true);
    });
}

function handleClose(code: number) {
  socketReady.value = false;
  if (!token.value || code === 1000) return;
  if (reconnectAttempts >= 3) {
    setStatus("实时连接已断开", true);
    return;
  }
  reconnectAttempts += 1;
  reconnectTimer = window.setTimeout(connectSocket, 3000);
}

async function resumeSession() {
  if (!token.value || document.hidden || !navigator.onLine) return;
  if (resumePromise) return resumePromise;
  resumePromise = (async () => {
    reconnectAttempts = 0;
    await syncThemePreference();
    const conversationId = activeId.value;
    if (conversationId) {
      try {
        await pullMessagesAfter(conversationId, lastSeqOf(conversationId));
        await scrollToEnd();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          await logout();
          setStatus("会话已过期，请重新登录", true);
          return;
        }
      }
    }
    if (!socketReady.value) connectSocket();
    else if (conversationId) socket?.sync(conversationId, lastSeqOf(conversationId));
  })().finally(() => {
    resumePromise = null;
  });
  return resumePromise;
}

function handleVisibilityChange() {
  if (document.hidden) {
    // 后台页面必须停止麦克风采集，但纯文字回复播报不应因切换浏览器
    // Tab 而中断。closeVoice() 会清空播放队列并停止当前音频，因此这里只在
    // PTT 正在录音或连续通话仍占用麦克风时关闭整条语音链路。
    if (voiceRecording.value || voiceLive.value) void closeVoice();
    return;
  }
  void resumeSession();
}

function handleOnline() {
  networkOnline.value = true;
  setStatus("网络已恢复，正在同步…");
  void resumeSession();
}

function handleOffline() {
  networkOnline.value = false;
  closeSocket();
  setStatus("当前离线，消息不会发送", true);
}

/** WS 事件分发：delta 流式拼接、control 情绪标签、committed 落定 */
function handleEvent(event: SocketEvent) {
  if (event.type === "protocol.error") {
    setStatus(`协议错误：${String(event.payload.reason_code ?? "")}`, true);
    return;
  }
  if (event.type === "tool.started") {
    setStatus(String(event.payload.label ?? "正在查询外部信息…"));
    return;
  }
  if (event.type === "tool.finished") {
    if (event.payload.ok === false) {
      setStatus(`查询未完成：${String(event.payload.reason_code ?? "unknown")}`, true);
    } else {
      setStatus("查询完成，正在组织回复…");
    }
    return;
  }
  if (event.type === "plan.execution" || event.type === "plan.changed") {
    planRefresh.value += 1;
    return;
  }
  if (event.type === "sync.completed") {
    const conversationId = event.stream.replace("conversation:", "");
    const afterSeq = Number(event.payload.after_seq ?? 0);
    const nextAfterSeq = Number(event.payload.next_after_seq ?? event.seq ?? afterSeq);
    if (event.payload.has_more === true && nextAfterSeq > afterSeq) {
      socket?.sync(conversationId, nextAfterSeq);
    }
    return;
  }
  if (!event.payload.message && event.type !== "reply.delta" && event.type !== "reply.control") {
    if (event.type === "turn.failed") {
      streaming.value = null;
      setStatus(`生成失败：${String(event.payload.reason_code ?? "")}`, true);
    } else if (event.type === "turn.cancelled") {
      streaming.value = null;
      setStatus("已取消");
    }
    return;
  }

  const conversationId = event.stream.replace("conversation:", "");
  if (event.type === "reply.delta") {
    if (streaming.value && streaming.value.generationId === event.generation_id) {
      streaming.value.text += event.payload.delta ?? "";
    } else if (!streaming.value && event.generation_id) {
      streaming.value = {
        conversationId,
        generationId: event.generation_id,
        text: event.payload.delta ?? "",
        emotion: null,
      };
    }
    if (conversationId === activeId.value) void scrollToEnd();
    return;
  }
  if (event.type === "reply.control") {
    if (streaming.value && streaming.value.generationId === event.generation_id) {
      const control = event.payload.agent_reply;
      streaming.value.emotion = control?.emotion ?? null;
      streaming.value.expression = control?.expressions?.[0] ?? null;
      const motion = control?.actions?.find((action) => action.type === "animation")?.value;
      if (motion) avatarMotion.value = { value: motion, sequence: (avatarMotion.value?.sequence ?? 0) + 1 };
    }
    return;
  }

  const message = event.payload.message!;
  mergeMessages(conversationId, [message]);
  if (event.type === "reply.committed" || event.type === "message.committed") {
    if (streaming.value?.generationId === event.generation_id) streaming.value = null;
    if (event.type === "reply.committed") {
      setStatus("已连接");
      void loadRuntimeMeta();
    }
  }
  if (conversationId === activeId.value) void scrollToEnd();
}

async function send() {
  if (!canSend.value || !activeId.value) return;
  // 已记住播报开关时，本次发送点击是恢复移动端音频权限的机会。
  if (textReplyVoice.value) unlockVoicePlayback();
  const text = draft.value.trim();
  sendPending.value = true;
  draft.value = "";
  setStatus(matchesLocationIntent(text) && locationEnabled.value && privacy.value !== "L2"
    ? "正在获取位置并发送…"
    : "正在发送…");
  const location = await resolveLocationForText(text);
  if (textReplyVoice.value) {
    try {
      const current = await ensureVoiceSocket();
      voiceBusy.value = true;
      voiceStatus.value = "文字已提交，正在生成语音回复…";
      current.submitText(text, location);
    } catch (error) {
      voiceBusy.value = false;
      if (!draft.value) draft.value = text;
      setStatus(error instanceof Error ? error.message : "语音连接失败", true);
    } finally {
      sendPending.value = false;
    }
    return;
  }
  socket?.sendMessage(activeId.value, text, privacy.value, location);
  sendPending.value = false;
}

function cancelStreaming() {
  if (streaming.value) socket?.cancel(streaming.value.generationId);
}

onMounted(async () => {
  window.addEventListener("focus", syncThemePreference);
  window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
  window.addEventListener("online", handleOnline);
  window.addEventListener("offline", handleOffline);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  if (typeof BroadcastChannel !== "undefined") {
    themeChannel = new BroadcastChannel(THEME_CHANNEL_NAME);
    themeChannel.onmessage = (event: MessageEvent<{ selection?: ThemePreference; schedule?: ThemeSchedule }>) => {
      const selection = event.data?.selection;
      if (!selection || !THEME_OPTIONS.some((option) => option.value === selection)) return;
      themePreference.value = selection;
      saveThemePreference(selection, event.data?.schedule);
    };
  }
  const saved = authStorage.getItem(TOKEN_KEY);
  if (!saved) {
    void loadRuntimeMeta();
    try {
      setupRequired.value = (await api.authStatus()).setup_required;
    } catch {
      setStatus("无法连接 Aria 服务", true);
    }
    return;
  }
  token.value = saved;
  try {
    const session = await api.me(token.value);
    displayName.value = session.user.display_name;
  } catch {
    authStorage.removeItem(TOKEN_KEY);
    token.value = "";
    setupRequired.value = (await api.authStatus().catch(() => ({ setup_required: false })))
      .setup_required;
    return;
  }
  await enterChat();
  void refreshPushToggle();
});

onBeforeUnmount(() => {
  window.removeEventListener("focus", syncThemePreference);
  window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
  window.removeEventListener("online", handleOnline);
  window.removeEventListener("offline", handleOffline);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  themeChannel?.close();
  themeChannel = null;
  closeSocket();
  void closeVoice();
  void playback.close();
});

function handleBeforeInstallPrompt(event: Event) {
  event.preventDefault();
  installPrompt.value = event as BeforeInstallPromptEvent;
}

async function installPwa() {
  const prompt = installPrompt.value;
  if (!prompt) return;
  await prompt.prompt();
  await prompt.userChoice;
  installPrompt.value = null;
}
</script>

<template>
  <div v-if="!token" class="auth">
    <form class="card" @submit.prevent="submitAuth">
      <h1>{{ personaMeta?.name ?? '助手' }}</h1>
      <p class="hint">{{ setupRequired ? "首次使用：设置聊天密码" : "输入聊天密码登录" }}</p>
      <template v-if="setupRequired">
        <input v-model="adminToken" type="password" placeholder="ARIA_ADMIN_TOKEN（仅首次）" />
        <input
          v-model="password"
          type="password"
          placeholder="设置密码（至少 12 位）"
          autocomplete="new-password"
        />
      </template>
      <input v-else v-model="password" type="password" placeholder="聊天密码" autocomplete="current-password" />
      <button class="primary" type="submit" :disabled="authBusy">
        {{ setupRequired ? "创建并登录" : "登录" }}
      </button>
      <button v-if="installPrompt && !isStandalone" class="ghost" type="button" @click="installPwa">安装到手机</button>
      <p v-else-if="isIos && !isStandalone" class="install-hint">iPhone：点 Safari“分享”→“添加到主屏幕”</p>
      <p v-if="statusText" class="status" :class="{ error: statusError }">{{ statusText }}</p>
    </form>
  </div>

  <div v-else class="shell">
    <button
      v-if="mobileConversationsOpen || mobileCompanionOpen"
      class="mobile-backdrop"
      type="button"
      aria-label="关闭面板"
      @click="mobileConversationsOpen = false; mobileCompanionOpen = false"
    ></button>
    <aside class="conversation-sidebar" :class="{ 'mobile-open': mobileConversationsOpen }">
      <header class="conversation-heading">
        <strong>{{ showArchivedConversations ? '已归档' : '会话' }}</strong>
        <small>{{ visibleConversations.length }} 个</small>
      </header>
      <div class="conversation-tabs">
        <button type="button" :class="{ active: !showArchivedConversations }" @click="selectConversationGroup(false)">会话</button>
        <button type="button" :class="{ active: showArchivedConversations }" @click="selectConversationGroup(true)">归档 {{ archivedConversations.length }}</button>
      </div>
      <button v-if="!showArchivedConversations" class="primary new-chat" type="button" @click="createConversation">新会话</button>
      <div class="conversation-list">
        <p v-if="!visibleConversations.length" class="conversation-empty">{{ showArchivedConversations ? '还没有归档会话' : '还没有会话' }}</p>
        <div
          v-for="conversation in visibleConversations"
          :key="conversation.id"
          class="conversation-item"
          :class="{ active: conversation.id === activeId }"
        >
          <button class="conversation" type="button" @click="openConversation(conversation.id)">
            {{ conversation.title || "新会话" }}
            <small>{{ conversation.last_seq }} 条消息</small>
          </button>
          <div class="conversation-actions">
            <button type="button" title="导出会话为 Markdown" @click="exportFullConversation(conversation.id)">⇩</button>
            <button v-if="showArchivedConversations" type="button" title="恢复会话" @click="restoreConversation(conversation.id)">↥</button>
            <button v-else type="button" title="归档会话" @click="archiveConversation(conversation.id)">↧</button>
            <button class="danger-text" type="button" title="永久删除会话" @click="removeConversation(conversation.id)">✕</button>
          </div>
        </div>
      </div>
      <div class="aside-footer">
        <label class="theme-field">
          <span>界面主题</span>
          <select v-model="themePreference" aria-label="界面主题" @change="onThemeChanged">
            <option v-for="option in THEME_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <a class="debug-link" href="/chat/debug">调试台</a>
      </div>
    </aside>

    <main>
      <header class="mobile-topbar">
        <button class="mobile-icon-button" type="button" aria-label="打开会话列表" @click="mobileConversationsOpen = true">☰</button>
        <div class="mobile-title">
          <strong>{{ personaMeta?.name ?? '助手' }}</strong>
          <small :class="{ offline: !networkOnline }">{{ !networkOnline ? '离线' : socketReady ? '在线' : '连接中' }}</small>
        </div>
        <button class="mobile-avatar-button" type="button" aria-label="打开伴侣形象" @click="mobileCompanionOpen = true">
          <img v-if="avatarImageUrl" :src="avatarImageUrl" alt="" />
          <span v-else>{{ personaMeta?.name?.[0] ?? 'A' }}</span>
        </button>
      </header>
      <div ref="messagesRoot" class="messages">
        <div v-if="selectMode" class="select-bar">
          <strong>已选 {{ selectedMessageIds.size }} 条</strong>
          <button type="button" @click="selectAllMessages">全选</button>
          <button type="button" :disabled="!selectedMessageIds.size" @click="exportSelected('copy')">复制 Markdown</button>
          <button type="button" :disabled="!selectedMessageIds.size" @click="exportSelected('download')">下载 .md</button>
          <button class="ghost" type="button" @click="toggleSelectMode">退出选择</button>
        </div>
        <div v-if="!activeMessages.length && activeId" class="empty">这个会话还没有消息。</div>
        <template v-for="message in activeMessages" :key="message.id">
          <div
            v-if="message.role !== 'system'"
            class="message"
            :class="[message.role, { selected: selectMode && selectedMessageIds.has(message.id) }]"
          >
            <label
              v-if="selectMode"
              class="select-check"
              :title="selectedMessageIds.has(message.id) ? '取消选择' : '选择该消息'"
            >
              <input
                type="checkbox"
                :checked="selectedMessageIds.has(message.id)"
                @change="toggleMessageSelected(message.id)"
              />
            </label>
            <div
              class="bubble"
              :class="{ selecting: selectMode }"
              @click="selectMode && toggleMessageSelected(message.id)"
            >
              <MarkdownContent :content="message.content" />
              <ToolResultCard
                v-if="message.role === 'assistant' && toolResultOf(message)"
                :result="toolResultOf(message)!"
                @choose-location="chooseLocation"
              />
              <span v-if="message.role === 'assistant' && emotionOf(message)" class="emotion">{{ emotionOf(message) }}</span>
              <span v-if="message.role === 'assistant' && personaVersionOf(message)" class="persona-badge">P v{{ personaVersionOf(message) }}</span>
              <span v-if="message.role === 'assistant' && recallLabelOf(message)" class="recall-badge">{{ recallLabelOf(message) }}</span>
              <span v-if="messageTimeOf(message)" class="message-time">{{ messageTimeOf(message) }}</span>
            </div>
          </div>
        </template>
        <div v-if="streaming && streaming.conversationId === activeId" class="message assistant">
          <div class="bubble streaming">
            {{ streaming.text }}▋
            <span v-if="streaming.emotion" class="emotion">{{ emotionLabels[streaming.emotion] ?? streaming.emotion }}</span>
          </div>
        </div>
      </div>

      <SafetyAlerts v-if="token" :key="token" :token="token" />

      <MailAttachments v-if="token && privacy === 'L1'" :key="token" :token="token" />

      <MailDrafts v-if="token && privacy === 'L1'" :key="token" :token="token" />

      <ConfirmationDrafts v-if="token && privacy === 'L1'" :key="token" :token="token" />

      <PlanInbox v-if="token && privacy === 'L1'" :key="token" :token="token" :refresh-key="planRefresh" />

      <footer class="composer">
        <p v-if="activeConversationArchived" class="archived-notice">此会话已归档。恢复后可以继续发送消息。</p>
        <div class="composer-meta">
          <select v-model="privacy" :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy" @change="onPrivacyChanged">
            <option value="L0">L0 · 可上云</option>
            <option value="L1">L1 · 常规</option>
            <option value="L2">L2 · 仅本地</option>
          </select>
          <button
            class="select-toggle"
            :class="{ active: selectMode }"
            type="button"
            title="勾选若干条消息，导出为 Markdown（复制或下载），方便粘贴给外部 AI 分析"
            :disabled="!activeId || !selectableMessages.length"
            @click="toggleSelectMode"
          >
            {{ selectMode ? "退出选择" : "⇩ 选择导出" }}
          </button>
          <label class="tts-toggle" title="开启后，文字输入也会播放 TTS 语音回复">
            <input
              v-model="textReplyVoice"
              type="checkbox"
              :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy"
              @change="onTextReplyVoiceChanged"
            />
            <span>文字回复播报</span>
          </label>
          <label
            v-if="locationSupported"
            class="tts-toggle"
            title="天气/附近/路线查询时自动附带本机定位（首次使用会请求浏览器授权，拒绝后回落默认城市）"
          >
            <input
              v-model="locationEnabled"
              type="checkbox"
              :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy"
              @change="onLocationEnabledChanged"
            />
            <span>📍自动定位</span>
          </label>
          <label
            v-if="pushSupported"
            class="tts-toggle"
            title="开启后，主动提醒/简报等会以系统通知推送到本机（PWA 关闭页面也能收到）"
          >
            <input
              v-model="notificationsEnabled"
              type="checkbox"
              :disabled="!token || !!streaming || voiceRecording || voiceLive || voiceBusy"
              @change="onNotificationsChanged"
            />
            <span>🔔移动通知</span>
          </label>
          <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
        </div>
        <div class="voice-row">
          <button
            class="voice-button"
            :class="{ recording: voiceLive }"
            type="button"
            :disabled="!activeId || activeConversationArchived || !!streaming || voiceRecording || (voiceBusy && !voiceLive)"
            @click="toggleVoiceCall"
          >
            {{ voiceLive ? "📞 挂断通话" : "📞 连续对话" }}
          </button>
          <button
            v-if="!voiceLive"
            class="voice-button"
            :class="{ recording: voiceRecording }"
            type="button"
            :disabled="!activeId || activeConversationArchived || !!streaming || (voiceBusy && !voiceRecording)"
            @click="toggleVoiceRecording"
          >
            {{ voiceRecording ? "结束并发送" : "🎙 说一句" }}
          </button>
          <button v-if="voiceBusy && !voiceRecording" type="button" @click="interruptVoice">打断/停止播报</button>
          <span class="voice-status">{{ voiceStatus }}</span>
          <span class="viseme-meter" title="实时口型幅度">
            <span class="viseme-fill" :style="{ transform: `scaleX(${voiceViseme})` }"></span>
          </span>
          <span v-if="voiceTranscript" class="voice-transcript">识别：{{ voiceTranscript }}</span>
        </div>
        <div class="composer-row">
          <textarea
            v-model="draft"
            rows="2"
            placeholder="输入消息，Enter 发送"
            :disabled="!socketReady || activeConversationArchived"
            @keydown.enter.exact.prevent="send"
          />
          <button v-if="!streaming" class="primary" type="button" :disabled="!canSend" @click="send">发送</button>
          <button v-else type="button" @click="cancelStreaming">停止</button>
        </div>
      </footer>
    </main>

    <aside class="avatar-sidebar" :class="{ 'mobile-open': mobileCompanionOpen }">
      <header class="companion-heading">
        <div>
          <strong>{{ personaMeta?.name ?? '助手' }}</strong>
          <small v-if="personaMeta" class="persona-version">Persona v{{ personaMeta.version }}</small>
        </div>
        <div class="aside-meta">
          <span>{{ displayName }}</span>
          <button v-if="installPrompt && !isStandalone" class="ghost" type="button" @click="installPwa">安装</button>
          <button class="ghost" type="button" @click="logout">退出</button>
        </div>
      </header>
      <section v-if="avatarMeta" class="avatar-stage" :class="`avatar-${avatarMeta.engine}`">
        <Transition name="avatar-expression" mode="out-in">
          <Live2DStage
            v-if="avatarMeta.engine === 'live2d' && avatarMeta.assets.model"
            :key="avatarMeta.assets.model"
            :model-url="avatarMeta.assets.model"
            :emotion="avatarEmotion"
            :expression="avatarExpression"
            :lip-sync="voiceViseme"
            :speaking="avatarSpeaking"
            :motion="avatarMotion?.value"
            :motion-sequence="avatarMotion?.sequence"
          />
          <img v-else-if="avatarImageUrl" :key="avatarImageUrl" class="avatar-image" :src="avatarImageUrl" :alt="`${avatarMeta.name} · ${emotionLabels[avatarEmotion] ?? avatarEmotion}`" />
          <div v-else :key="avatarMeta.instanceId" class="avatar-orb"><span>{{ avatarMeta.name[0] }}</span></div>
        </Transition>
        <div class="avatar-caption"><strong>{{ avatarMeta.name }}</strong><small>{{ emotionLabels[avatarEmotion] ?? avatarEmotion }}</small></div>
      </section>
      <label v-if="avatars.length" class="avatar-switcher">
        <span>当前形象</span>
        <select :value="avatarMeta?.instanceId ?? ''" :disabled="avatarSwitchBusy" @change="switchAvatar">
          <option v-for="avatar in avatars" :key="avatar.instance_id" :value="avatar.instance_id">{{ avatar.name }}</option>
        </select>
      </label>
    </aside>
  </div>
</template>

<style scoped>
.auth { display: grid; place-items: center; height: 100%; }
.card { display: grid; gap: 12px; width: min(360px, 90vw); background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 28px; box-shadow: var(--shadow); }
.card h1 { margin: 0; font-size: 22px; }
.hint { color: var(--muted); margin: 0; font-size: 13px; }
.install-hint { margin:0; color:var(--muted); font-size:12px; line-height:1.5; text-align:center; }
.status { color: var(--muted); font-size: 12px; margin: 0; }
.status.error { color: var(--danger); }

.shell { display:grid; grid-template-columns:250px minmax(0,1fr) clamp(320px,22vw,380px); grid-template-areas:"conversations chat avatar"; height:100%; background:var(--bg); }
aside { display:flex; flex-direction:column; gap:12px; min-width:0; padding:14px; min-height:0; background:var(--panel); }
aside header strong { font-size:16px; }
.conversation-sidebar { grid-area:conversations; border-right:1px solid var(--line); }
.conversation-heading { display:flex; align-items:center; justify-content:space-between; min-height:34px; }
.conversation-heading small { color:var(--muted); font-size:11px; }
.avatar-sidebar { grid-area:avatar; gap:14px; padding:18px; border-left:1px solid var(--line); }
.companion-heading { display:grid; gap:12px; }
.persona-version { display:block; margin-top:3px; color:var(--muted); font-size:10px; }
.aside-meta { display:flex; justify-content:space-between; align-items:center; color:var(--muted); font-size:12px; }
.avatar-stage { position:relative; display:grid; flex:1 1 auto; place-items:center; min-height:300px; overflow:hidden; border:1px solid var(--line); border-radius:16px; background:linear-gradient(160deg,var(--stage-from),var(--stage-to)); }
.avatar-image { width:100%; height:100%; min-height:300px; object-fit:contain; object-position:center; transition:opacity 180ms ease,transform 220ms ease; }
.avatar-expression-enter-from,.avatar-expression-leave-to { opacity:0; transform:scale(1.015); }
.avatar-expression-leave-active { position:absolute; }
.avatar-orb { display:grid; place-items:center; width:68px; height:68px; border-radius:50%; background:linear-gradient(135deg,#f2b6aa,#b97679); box-shadow:0 12px 34px #b9767955; color:#fff; font-size:24px; font-weight:700; }
.avatar-abstract .avatar-orb { background:radial-gradient(circle at 35% 30%,#fff,#75d8d7 24%,#9a86d2 62%,#28244c); box-shadow:0 0 38px #75d8d777; animation:avatar-pulse 3s ease-in-out infinite; }
.avatar-caption { position:absolute; inset:auto 12px 10px; display:flex; justify-content:space-between; align-items:end; gap:8px; }.avatar-caption strong { max-width:78%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; }.avatar-caption small { color:var(--muted); font-size:9px; }
.avatar-switcher{display:grid;flex:none;gap:6px;color:var(--muted);font-size:10px}.avatar-switcher select{min-width:0;width:100%;padding:9px 10px;font-size:12px}
@keyframes avatar-pulse { 50% { transform:scale(1.06); filter:brightness(1.12); } }
.new-chat { width: 100%; }
.conversation-tabs { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
.conversation-tabs button { padding:7px 8px; color:var(--muted); background:var(--panel2); }
.conversation-tabs button.active { border-color:var(--accent); color:var(--accent); background:var(--accent-soft); }
.conversation-list { flex: 1; min-width:0; min-height: 0; overflow-x:hidden; overflow-y: auto; display: grid; align-content: start; gap: 6px; }
.conversation-empty { margin:10px 4px; color:var(--muted); font-size:12px; text-align:center; }
.conversation-item { position: relative; display: flex; align-items: center; min-width:0; max-width:100%; }
.conversation { flex: 1 1 auto; min-width:0; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align: left; border: 1px solid var(--line); background: var(--panel); padding: 8px 30px 8px 10px; border-radius: 8px; }
.conversation-item.active .conversation { border-color: var(--accent); background: var(--accent-soft); color:var(--accent); font-weight:600; }
.conversation small { display: block; overflow:hidden; text-overflow:ellipsis; color: var(--muted); margin-top: 3px; font-size: 11px; font-weight:400; }
.conversation-actions { position:absolute; right:4px; display:flex; align-items:center; }
.conversation-actions button { padding:2px 5px; border:0; background:transparent; }
.conversation-item .conversation { padding-right:74px; }
.debug-link { color: var(--muted); font-size: 12px; text-decoration: none; }
.aside-footer { display:flex; align-items:end; gap:8px; min-width:0; padding-top:10px; border-top:1px solid var(--line); }
.theme-field { display:grid; flex:1; min-width:0; gap:5px; color:var(--muted); font-size:10px; }
.theme-field select { min-width:0; width:100%; padding:7px 9px; font-size:12px; }
.aside-footer .debug-link { flex:none; padding:8px 2px; }

main { grid-area:chat; display:grid; grid-template-rows:minmax(0,1fr) auto; min-width:0; min-height:0; background:var(--bg); }
.mobile-topbar,.mobile-backdrop { display:none; }
.messages { overflow-y: auto; padding: 20px; }
.empty { height: 100%; display: grid; place-items: center; color: var(--muted); }
.message { max-width: 80%; margin-bottom: 14px; display: flex; align-items: flex-start; gap: 8px; }
.message.user { margin-left: auto; }
.select-check { display: flex; padding: 12px 0 0 2px; cursor: pointer; accent-color: var(--accent); }
.select-check input { cursor: pointer; }
.message.selected .bubble { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.bubble.selecting { cursor: pointer; }
.bubble.selecting :deep(a), .bubble.selecting :deep(button) { pointer-events: none; }
.select-bar { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; padding: 8px 10px; border: 1px solid var(--accent); border-radius: 10px; background: color-mix(in srgb, var(--panel) 92%, transparent); backdrop-filter: blur(8px); }
.select-bar strong { color: var(--accent); font-size: 12px; }
.select-bar button { padding: 5px 10px; font-size: 12px; }
.select-toggle.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.bubble { line-height: 1.55; padding: 10px 14px; border:1px solid var(--line); border-radius: 14px; background: var(--panel); box-shadow:0 3px 12px color-mix(in srgb,var(--text) 4%,transparent); position: relative; }
.user .bubble { background: var(--message-user-bg); border-color:var(--message-user-bg); color: #fff; }
.user .bubble :deep(.markdown-content a) { color:inherit; }
.bubble.streaming { white-space:pre-wrap; }
.bubble.streaming::after { content: ""; }
.emotion { display: inline-block; margin-left: 8px; font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; vertical-align: 1px; }
.persona-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); opacity:.75; }
.recall-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:0 7px; opacity:.8; }

.composer { border-top: 1px solid var(--line); padding: 12px 16px; display: grid; gap: 8px; background:var(--panel); }
.archived-notice { margin:0; color:var(--muted); font-size:12px; }
.composer-meta { display: flex; align-items: center; gap: 12px; }
.tts-toggle { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:12px; cursor:pointer; user-select:none; }
.tts-toggle input { accent-color:var(--accent); cursor:pointer; }
.tts-toggle input:disabled { cursor:not-allowed; }
.voice-row { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.voice-button.recording { border-color: var(--danger); color: var(--danger); }
.voice-status { color: var(--muted); font-size: 12px; }
.viseme-meter { width:44px; height:8px; border:1px solid var(--line); border-radius:999px; overflow:hidden; background:var(--panel2); }
.viseme-fill { display:block; width:100%; height:100%; transform-origin:left center; background:var(--accent); transition:transform 50ms linear; }
.message-time { margin-left:6px; color:var(--muted); font-size:11px; white-space:nowrap; }
.user .message-time { color:#fff; font-weight:500; }
.voice-transcript { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.composer-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: end; }
.composer textarea { resize: none; }

@media (max-width:1100px) {
  .shell { grid-template-columns:220px minmax(0,1fr) 280px; }
  .avatar-sidebar { padding:14px; }
  .avatar-stage { min-height:260px; }
}

@media (max-width: 760px) {
  .shell { position:relative; display:block; height:100dvh; overflow:hidden; }
  main { height:100%; grid-template-rows:auto minmax(0,1fr) auto; }
  .mobile-topbar { display:grid; grid-template-columns:44px minmax(0,1fr) 44px; align-items:center; gap:10px; min-height:calc(56px + env(safe-area-inset-top)); padding:env(safe-area-inset-top) 10px 0; border-bottom:1px solid var(--line); background:color-mix(in srgb,var(--panel) 94%,transparent); backdrop-filter:blur(18px); z-index:3; }
  .mobile-icon-button,.mobile-avatar-button { display:grid; place-items:center; width:44px; height:44px; padding:0; border-color:transparent; background:transparent; font-size:20px; }
  .mobile-avatar-button { overflow:hidden; border-color:var(--line); border-radius:50%; color:#fff; background:linear-gradient(135deg,#f2b6aa,#8d79cf); font-size:15px; font-weight:700; }
  .mobile-avatar-button img { width:100%; height:100%; object-fit:cover; }
  .mobile-title { display:grid; min-width:0; text-align:center; }
  .mobile-title strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:15px; }
  .mobile-title small { color:var(--success); font-size:10px; }
  .mobile-title small.offline { color:var(--danger); }
  .mobile-backdrop { position:fixed; display:block; inset:0; z-index:9; width:100%; height:100%; padding:0; border:0; border-radius:0; background:rgba(18,24,39,.34); backdrop-filter:blur(2px); }
  .conversation-sidebar,.avatar-sidebar { position:fixed; top:0; bottom:0; z-index:10; width:min(86vw,360px); max-height:none; padding-top:calc(16px + env(safe-area-inset-top)); padding-bottom:calc(16px + env(safe-area-inset-bottom)); border:0; box-shadow:0 20px 60px rgba(17,24,39,.22); transition:transform 220ms ease; }
  .conversation-sidebar { left:0; transform:translateX(-105%); }
  .avatar-sidebar { right:0; transform:translateX(105%); }
  .conversation-sidebar.mobile-open,.avatar-sidebar.mobile-open { transform:translateX(0); }
  .conversation-heading { display:flex; }
  .conversation-list { display:grid; overflow-x:hidden; overflow-y:auto; gap:6px; }
  .conversation-item { flex:initial; }
  .aside-footer { display:flex; }
  .avatar-stage { flex:1 1 auto; height:auto; min-height:260px; }
  .avatar-image { min-height:260px; }
  .messages { padding:14px 12px 20px; overscroll-behavior:contain; }
  .message { max-width:92%; margin-bottom:10px; }
  .bubble { padding:10px 12px; border-radius:16px; }
  .composer { gap:7px; padding:9px 10px calc(9px + env(safe-area-inset-bottom)); }
  .composer-meta { gap:8px; overflow-x:auto; padding-bottom:1px; scrollbar-width:none; }
  .composer-meta::-webkit-scrollbar { display:none; }
  .composer-meta > * { flex:none; }
  .composer-meta .status { flex:1 0 100%; }
  .tts-toggle { white-space:nowrap; }
  .voice-row { flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; }
  .voice-row::-webkit-scrollbar { display:none; }
  .voice-row > * { flex:none; }
  .voice-transcript { flex:1 0 160px; }
  .composer-row { grid-template-columns:minmax(0,1fr) 56px; gap:8px; }
  .composer textarea { min-height:44px; max-height:112px; padding:10px 11px; }
  .composer-row button { min-width:56px; padding-inline:8px; }
}
</style>
