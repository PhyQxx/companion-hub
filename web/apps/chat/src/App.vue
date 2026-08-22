<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  ApiError,
  ChatApi,
  ChatSocket,
  VoiceSocket,
  matchesLocationIntent,
  type AgentReplyControl,
  type AuthSession,
  type ChatMessage,
  type ClientLocationPayload,
  type Conversation,
  type PrivacyLevel,
  type SocketEvent,
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

// 聊天前端主组件：登录 → 会话侧栏 → 流式消息区 → 发送区。
// 令牌持久化在 localStorage；WS 断线自动重连（最多 3 次）。
const api = new ChatApi();
const TOKEN_KEY = "ariaChatToken";
const TEXT_REPLY_VOICE_KEY = "ariaTextReplyVoice";
const LOCATION_ENABLED_KEY = "ariaLocationEnabled";
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

const conversations = ref<Conversation[]>([]);
const activeId = ref<string | null>(null);
const messagesByConversation = reactive(new Map<string, ChatMessage[]>());
const draft = ref("");
const privacy = ref<PrivacyLevel>("L1");
const streaming = ref<{
  conversationId: string;
  generationId: string;
  text: string;
  emotion: string | null;
} | null>(null);
const socketReady = ref(false);
const messagesRoot = ref<HTMLElement | null>(null);
const voiceReady = ref(false);
const voiceAsrConfigured = ref<boolean | null>(null);
const voiceAsrBlockMessage = ref("");
const voiceRecording = ref(false);
const voiceBusy = ref(false);
const sendPending = ref(false);
const voiceStatus = ref("语音未连接");
const voiceTranscript = ref("");
const voiceViseme = ref(0);
const textReplyVoice = ref(localStorage.getItem(TEXT_REPLY_VOICE_KEY) === "1");
const voiceTtsConfigured = ref<boolean | null>(null);

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

const activeMessages = computed(() =>
  activeId.value ? (messagesByConversation.get(activeId.value) ?? []) : [],
);
const canSend = computed(
  () =>
    socketReady.value &&
    !!activeId.value &&
    draft.value.trim().length > 0 &&
    !streaming.value &&
    !sendPending.value &&
    !voiceBusy.value &&
    !voiceRecording.value,
);

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}

async function closeVoice() {
  voiceRecording.value = false;
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

async function ensureVoiceSocket(): Promise<VoiceSocket> {
  if (!activeId.value) throw new Error("请先选择会话");
  const conversationId = activeId.value;
  const privacyLevel = privacy.value;
  const key = `${conversationId}:${privacyLevel}`;
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
            voiceStatus.value = "语音连接已断开";
          }
        },
      },
    );
    voiceSocket = next;
    voiceSocketKey = key;
    try {
      // 纯语音回合没有输入帧, hello 时带上新鲜缓存位置(不触发授权弹窗)。
      await next.connect(conversationId, privacyLevel, freshCachedLocation());
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
      voiceStatus.value = "已打断";
      break;
    case "turn.cancelled":
      voiceBusy.value = false;
      if (streaming.value?.generationId === event.generation_id) streaming.value = null;
      playback.interrupt();
      voiceSentence = null;
      voiceViseme.value = 0;
      voiceStatus.value = "语音回合已取消";
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
    case "voice.error":
      voiceBusy.value = false;
      voiceStatus.value = `语音请求失败：${event.reason ?? "unknown"}`;
      break;
    case "reply.committed":
      voiceBusy.value = false;
      voiceStatus.value = "语音回复已完成";
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
    const messages = await api.listMessages(token.value, conversationId);
    messagesByConversation.set(conversationId, messages);
    const conversation = conversations.value.find((item) => item.id === conversationId);
    if (conversation && messages.length) conversation.last_seq = messages[messages.length - 1]!.seq;
    if (conversationId === activeId.value) await scrollToEnd();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "刷新语音消息失败", true);
  }
}

async function toggleVoiceRecording() {
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
    if (runtime.location_policy) locationPolicy.value = runtime.location_policy.precise;
  } catch {
    personaMeta.value = null;
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

function emotionOf(message: ChatMessage): string | null {
  const reply = (message.decision_meta as { agent_reply?: AgentReplyControl } | null)?.agent_reply;
  const emotion = reply?.emotion ?? null;
  return emotion ? (emotionLabels[emotion] ?? emotion) : null;
}

function rememberSession(session: AuthSession) {
  token.value = session.access_token;
  displayName.value = session.user.display_name;
  localStorage.setItem(TOKEN_KEY, session.access_token);
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
  token.value = "";
  displayName.value = "";
  localStorage.removeItem(TOKEN_KEY);
  conversations.value = [];
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
  await loadRuntimeMeta();
  await loadConversations();
  connectSocket();
}

async function loadConversations() {
  try {
    conversations.value = await api.listConversations(token.value);
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
  activeId.value = id;
  if (!messagesByConversation.has(id)) {
    try {
      const messages = await api.listMessages(token.value, id);
      messagesByConversation.set(id, messages);
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
  return messages.length ? (messages[messages.length - 1]!.seq ?? 0) : 0;
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

async function removeConversation(id: string) {
  const conversation = conversations.value.find((item) => item.id === id);
  const label = conversation?.title ?? "该会话";
  if (!confirm(`删除「${label}」？消息与由它沉淀的记忆会被一并删除，不可恢复。`)) return;
  try {
    if (activeId.value === id) await closeVoice();
    await api.deleteConversation(token.value, id);
    conversations.value = conversations.value.filter((item) => item.id !== id);
    messagesByConversation.delete(id);
    if (activeId.value === id) {
      activeId.value = null;
      if (conversations.value.length) await openConversation(conversations.value[0]!.id);
    }
    setStatus("会话已删除");
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "删除失败", true);
  }
}

/** 建立实时连接；失败时保留基础 REST 可用性并提示 */
function connectSocket() {
  closeSocket();
  socket = new ChatSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat",
    token.value,
    { onEvent: handleEvent, onClose: handleClose },
  );
  socket
    .connect()
    .then(() => {
      socketReady.value = true;
      reconnectAttempts = 0;
      setStatus("已连接");
      if (activeId.value) socket?.sync(activeId.value, lastSeqOf(activeId.value));
    })
    .catch(() => setStatus("实时连接失败，仍可基础使用", true));
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
      streaming.value.emotion = event.payload.agent_reply?.emotion ?? null;
    }
    return;
  }

  const message = event.payload.message!;
  const bucket = messagesByConversation.get(conversationId);
  if (bucket) {
    if (!bucket.some((item) => item.id === message.id)) bucket.push(message);
  } else {
    messagesByConversation.set(conversationId, [message]);
  }
  const conversation = conversations.value.find((item) => item.id === conversationId);
  if (conversation) conversation.last_seq = Math.max(conversation.last_seq, message.seq);
  if (event.type === "reply.committed" || event.type === "message.committed") {
    if (streaming.value?.generationId === event.generation_id) streaming.value = null;
    if (event.type === "reply.committed") void loadRuntimeMeta();
  }
  if (conversationId === activeId.value) void scrollToEnd();
}

async function send() {
  if (!canSend.value || !activeId.value) return;
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
  const saved = localStorage.getItem(TOKEN_KEY);
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
    localStorage.removeItem(TOKEN_KEY);
    token.value = "";
    setupRequired.value = (await api.authStatus().catch(() => ({ setup_required: false })))
      .setup_required;
    return;
  }
  await enterChat();
});

onBeforeUnmount(() => {
  closeSocket();
  void closeVoice();
  void playback.close();
});
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
      <p v-if="statusText" class="status" :class="{ error: statusError }">{{ statusText }}</p>
    </form>
  </div>

  <div v-else class="shell">
    <aside>
      <header>
        <strong>{{ personaMeta?.name ?? '助手' }}</strong>
        <small v-if="personaMeta" class="persona-version">Persona v{{ personaMeta.version }}</small>
        <div class="aside-meta">
          <span>{{ displayName }}</span>
          <button class="ghost" type="button" @click="logout">退出</button>
        </div>
      </header>
      <button class="primary new-chat" type="button" @click="createConversation">新会话</button>
      <div class="conversation-list">
        <div
          v-for="conversation in conversations"
          :key="conversation.id"
          class="conversation-item"
          :class="{ active: conversation.id === activeId }"
        >
          <button class="conversation" type="button" @click="openConversation(conversation.id)">
            {{ conversation.title || "新会话" }}
            <small>{{ conversation.last_seq }} 条消息</small>
          </button>
          <button class="danger-text" type="button" title="删除会话" @click="removeConversation(conversation.id)">✕</button>
        </div>
      </div>
      <a class="debug-link" href="/chat/debug">调试台</a>
    </aside>

    <main>
      <div ref="messagesRoot" class="messages">
        <div v-if="!activeMessages.length && activeId" class="empty">这个会话还没有消息。</div>
        <template v-for="message in activeMessages" :key="message.id">
          <div v-if="message.role !== 'system'" class="message" :class="message.role">
            <div class="bubble">
              {{ message.content }}
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

      <footer class="composer">
        <div class="composer-meta">
          <select v-model="privacy" :disabled="!!streaming || voiceRecording || voiceBusy" @change="onPrivacyChanged">
            <option value="L0">L0 · 可上云</option>
            <option value="L1">L1 · 常规</option>
            <option value="L2">L2 · 仅本地</option>
          </select>
          <label class="tts-toggle" title="开启后，文字输入也会播放 TTS 语音回复">
            <input
              v-model="textReplyVoice"
              type="checkbox"
              :disabled="!!streaming || voiceRecording || voiceBusy"
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
              :disabled="!!streaming || voiceRecording || voiceBusy"
              @change="onLocationEnabledChanged"
            />
            <span>📍自动定位</span>
          </label>
          <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
        </div>
        <div class="voice-row">
          <button
            class="voice-button"
            :class="{ recording: voiceRecording }"
            type="button"
            :disabled="!activeId || !!streaming || (voiceBusy && !voiceRecording)"
            @click="toggleVoiceRecording"
          >
            {{ voiceRecording ? "结束并发送" : "🎙 开始说话" }}
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
            :disabled="!socketReady"
            @keydown.enter.exact.prevent="send"
          />
          <button v-if="!streaming" class="primary" type="button" :disabled="!canSend" @click="send">发送</button>
          <button v-else type="button" @click="cancelStreaming">停止</button>
        </div>
      </footer>
    </main>
  </div>
</template>

<style scoped>
.auth { display: grid; place-items: center; height: 100%; }
.card { display: grid; gap: 12px; width: min(360px, 90vw); background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 28px; }
.card h1 { margin: 0; font-size: 22px; }
.hint { color: var(--muted); margin: 0; font-size: 13px; }
.status { color: var(--muted); font-size: 12px; margin: 0; }
.status.error { color: var(--danger); }

.shell { display: grid; grid-template-columns: 250px 1fr; height: 100%; }
aside { display: flex; flex-direction: column; gap: 12px; border-right: 1px solid var(--line); padding: 14px; min-height: 0; }
aside header strong { font-size: 16px; }
.persona-version { display:block; margin-top:3px; color:var(--muted); font-size:10px; }
.aside-meta { display: flex; justify-content: space-between; align-items: center; color: var(--muted); font-size: 12px; margin-top: 4px; }
.new-chat { width: 100%; }
.conversation-list { flex: 1; min-height: 0; overflow-y: auto; display: grid; align-content: start; gap: 6px; }
.conversation-item { position: relative; display: flex; align-items: center; }
.conversation { flex: 1; text-align: left; border: 1px solid var(--line); background: #10131d; padding: 8px 30px 8px 10px; border-radius: 8px; }
.conversation-item.active .conversation { border-color: var(--accent); background: #1b2540; }
.conversation small { display: block; color: var(--muted); margin-top: 3px; font-size: 11px; }
.conversation-item .danger-text { position: absolute; right: 4px; padding: 2px 6px; }
.debug-link { color: var(--muted); font-size: 12px; text-decoration: none; }

main { display: grid; grid-template-rows: minmax(0, 1fr) auto; min-height: 0; }
.messages { overflow-y: auto; padding: 20px; }
.empty { height: 100%; display: grid; place-items: center; color: var(--muted); }
.message { max-width: 80%; margin-bottom: 14px; }
.message.user { margin-left: auto; }
.bubble { white-space: pre-wrap; line-height: 1.55; padding: 10px 14px; border-radius: 14px; background: #1a2130; position: relative; }
.user .bubble { background: var(--accent); color: #fff; }
.bubble.streaming::after { content: ""; }
.emotion { display: inline-block; margin-left: 8px; font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; vertical-align: 1px; }
.persona-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); opacity:.75; }
.recall-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:0 7px; opacity:.8; }

.composer { border-top: 1px solid var(--line); padding: 12px 16px; display: grid; gap: 8px; }
.composer-meta { display: flex; align-items: center; gap: 12px; }
.tts-toggle { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:12px; cursor:pointer; user-select:none; }
.tts-toggle input { accent-color:var(--accent); cursor:pointer; }
.tts-toggle input:disabled { cursor:not-allowed; }
.voice-row { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.voice-button.recording { border-color: var(--danger); color: var(--danger); }
.voice-status { color: var(--muted); font-size: 12px; }
.viseme-meter { width:44px; height:8px; border:1px solid var(--line); border-radius:999px; overflow:hidden; background:#10131d; }
.viseme-fill { display:block; width:100%; height:100%; transform-origin:left center; background:var(--accent); transition:transform 50ms linear; }
.message-time { margin-left:6px; color:var(--muted); font-size:11px; white-space:nowrap; }
.voice-transcript { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.composer-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: end; }
.composer textarea { resize: none; }

@media (max-width: 720px) {
  .shell { grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr); }
  aside { border-right: none; border-bottom: 1px solid var(--line); }
  .conversation-list { display: flex; overflow-x: auto; gap: 6px; }
  .message { max-width: 95%; }
}
</style>
