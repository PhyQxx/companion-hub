<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  PhArchive,
  PhArrowCounterClockwise,
  PhBell,
  PhDownloadSimple,
  PhExport,
  PhMapPin,
  PhMicrophone,
  PhPaperPlaneRight,
  PhPhone,
  PhPhoneDisconnect,
  PhPlus,
  PhSpeakerHigh,
  PhStopCircle,
  PhTrash,
} from "@phosphor-icons/vue";
import {
  ApiError,
  ChatApi,
  THEME_CHANNEL_NAME,
  THEME_OPTIONS,
  broadcastThemePreference,
  readThemePreference,
  readThemeSchedule,
  saveThemePreference,
  matchesLocationIntent,
  type AgentReplyControl,
  type AvatarChoice,
  type ChatMessage,
  type ClientLocationPayload,
  type Conversation,
  type PrivacyLevel,
  type SocketEvent,
  type ThemePreference,
  type ThemeSchedule,
  type ToolPresentation,
} from "@aria/shared";
import ToolResultCard from "./ToolResultCard.vue";
import { useChatSocket } from "./composables/useChatSocket";
import { useVoice, type StreamingState } from "./composables/useVoice";
import { useAuth } from "./composables/useAuth";
import PlanInbox from "./PlanInbox.vue";
import GoogleCalendarConnection from "./GoogleCalendarConnection.vue";
import RunHistory from "./RunHistory.vue";
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
import { useConversationExport } from "./composables/useConversationExport";

// 聊天前端主组件：登录 → 会话侧栏 → 流式消息区 → 发送区。
// 普通 Web 保留既有本地会话；安装后的 PWA 只在当前会话存储令牌。
const api = new ChatApi();
const LOCATION_ENABLED_KEY = "ariaLocationEnabled";
const themePreference = ref<ThemePreference>(readThemePreference());
const isStandalone =
  window.matchMedia("(display-mode: standalone)").matches ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true;
const isIos =
  /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
// 与服务端 tools/location.py 的 LOCATION_TTL(15 分钟)保持一致。
const LOCATION_TTL_MS = 15 * 60 * 1000;

const token = ref<string>("");
const displayName = ref<string>("");
const accountName = ref<string>("");
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
const avatarMotion = ref<{ value: string; sequence: number } | null>(null);

const conversations = ref<Conversation[]>([]);
const archivedConversations = ref<Conversation[]>([]);
const showArchivedConversations = ref(false);
const activeId = ref<string | null>(null);
const messagesByConversation = reactive(new Map<string, ChatMessage[]>());
const draft = ref("");
const privacy = ref<PrivacyLevel>("L1");
const streaming = ref<StreamingState | null>(null);
// PC-02：计划执行进度面板（plan.execution 事件驱动，展示目标/步骤/进度/证据与停止按钮）
const planRefresh = ref(0);
const messagesRoot = ref<HTMLElement | null>(null);
const sendPending = ref(false);
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

let themeChannel: BroadcastChannel | null = null;
const { socketReady, connectSocket, closeSocket, sendMessage, cancel, sync } = useChatSocket({
  token: () => token.value,
  activeConversationId: () => activeId.value,
  onEvent: handleEvent,
  setStatus,
  lastSeqOf,
});
const {
  voiceAsrConfigured,
  voiceAsrBlockMessage,
  voiceRecording,
  voiceLive,
  voiceBusy,
  voiceStatus,
  voiceTranscript,
  voiceVadBackend,
  voiceViseme,
  textReplyVoice,
  closeVoice,
  ensureVoiceSocket,
  toggleVoiceRecording,
  toggleVoiceCall,
  interruptVoice,
  unlockVoicePlayback,
  onPrivacyChanged,
  onTextReplyVoiceChanged,
  disposeVoice,
} = useVoice({
  token: () => token.value,
  activeId,
  privacy,
  streaming,
  draft,
  setStatus,
  scrollToEnd,
  refreshMessages,
  revokePendingUserMessages,
  loadRuntimeMeta,
  freshCachedLocation,
  generationFailureText,
});
const {
  ssoEnabled,
  authBusy,
  loginUsername,
  loginPassword,
  loginBusy,
  loginWithPnkx,
  loginWithPassword,
  passwordPanelOpen,
  passwordBusy,
  passwordCurrent,
  passwordNew,
  passwordConfirm,
  togglePasswordPanel,
  submitPasswordChange,
  logout,
  restoreSession,
  disposeAuth,
} = useAuth({
  api,
  token,
  displayName,
  accountName,
  notificationsEnabled,
  isStandalone,
  pushSupported,
  setStatus,
  closeSocket,
  closeVoice,
  enterChat,
  refreshPushToggle,
  clearConversationState,
});

function clearConversationState() {
  conversations.value = [];
  archivedConversations.value = [];
  activeId.value = null;
  messagesByConversation.clear();
}

const activeMessages = computed(() =>
  activeId.value ? (messagesByConversation.get(activeId.value) ?? []) : [],
);

// 乐观显示：点击发送先落一条本地 user 占位消息，服务端 message.committed
// 回显（或断线补拉）后按内容配对回收。占位 id/seq 与服务端空间隔离，
// 避免污染 sync 游标（lastSeqOf 与会话 last_seq 都会跳过占位符）。
const LOCAL_MESSAGE_PREFIX = "local:";
const LOCAL_SEQ_BASE = Number.MAX_SAFE_INTEGER - 100_000;
let localMessageCount = 0;
// 同步收敛后回收占位消息的最小年龄：刚发出的消息可能仍在服务端处理中，
// 立即回收会把已送达的消息误恢复到输入框。
const PENDING_REVOKE_MIN_AGE_MS = 20_000;

function isLocalMessage(message: ChatMessage): boolean {
  return message.id.startsWith(LOCAL_MESSAGE_PREFIX);
}

function appendPendingUserMessage(conversationId: string, text: string): void {
  localMessageCount += 1;
  const placeholder: ChatMessage = {
    id: `${LOCAL_MESSAGE_PREFIX}${Date.now().toString(36)}-${localMessageCount}`,
    conversation_id: conversationId,
    turn_id: "",
    seq: LOCAL_SEQ_BASE + localMessageCount,
    role: "user",
    content: text,
    privacy_level: privacy.value,
    generation_id: null,
    decision_meta: null,
    created_at: new Date().toISOString(),
  };
  mergeMessages(conversationId, [placeholder]);
  if (conversationId === activeId.value) void scrollToEnd();
}

/** 回收未被服务端确认的占位消息，返回最后一条的内容（便于恢复到输入框）。 */
function revokePendingUserMessages(conversationId: string, minAgeMs = 0): string | null {
  const messages = messagesByConversation.get(conversationId);
  if (!messages?.some(isLocalMessage)) return null;
  const now = Date.now();
  let restored: string | null = null;
  const kept = messages.filter((message) => {
    if (!isLocalMessage(message)) return true;
    if (minAgeMs > 0 && now - Date.parse(message.created_at) < minAgeMs) return true;
    restored = message.content;
    return false;
  });
  if (kept.length === messages.length) return null;
  messagesByConversation.set(conversationId, kept);
  return restored;
}

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
  // 真实 user 消息落定后，按内容配对移除最早的占位符，避免回显时重复显示。
  const placeholders = [...merged.values()].filter((message) => isLocalMessage(message));
  for (const message of incoming) {
    if (message.role === "user" && !isLocalMessage(message)) {
      const index = placeholders.findIndex((item) => item.content === message.content);
      if (index >= 0) {
        merged.delete(placeholders[index]!.id);
        placeholders.splice(index, 1);
      }
    }
    merged.set(message.id, message);
  }
  const messages = [...merged.values()].sort((left, right) => left.seq - right.seq);
  messagesByConversation.set(conversationId, messages);
  const conversation = conversations.value.find((item) => item.id === conversationId);
  if (conversation && messages.length) {
    const lastServerSeq = messages.reduce(
      (latest, message) => (isLocalMessage(message) ? latest : Math.max(latest, message.seq)),
      0,
    );
    if (lastServerSeq) conversation.last_seq = Math.max(conversation.last_seq, lastServerSeq);
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

function generationFailureText(reason: unknown, prefix = "生成失败"): string {
  const code = String(reason ?? "unknown");
  const messages: Record<string, string> = {
    user_model_concurrency_exhausted: "同时处理的任务较多，请稍后再试。",
    budget_owner_invalid: "账户当前无法处理任务，请重新登录后重试。",
    context_tokenizer_unavailable: "模型的本地分词配置不可用，请在管理界面核对文件和指纹。",
    daily_cost_budget_exhausted: "今天已达到模型费用预留上限，请稍后再试或在管理界面调整限额。",
    monthly_cost_budget_exhausted: "本月已达到模型费用预留上限，请核对用量或在管理界面调整限额。",
    cost_pricing_unavailable: "当前模型没有与限额匹配的完整计价信息，请在管理界面补齐价格和币种。",
    cost_usage_unknown: "已有调用的费用无法确认，暂不能在金额限额内继续，请先核对费用记录。",
    cost_window_changed: "费用统计刚切换到新的周期，请重试本轮请求。",
    tool_budget_exhausted: "这项任务已达到工具调用上限，请核对已完成部分。",
    run_budget_exhausted: "这轮回复已达到处理上限，可以缩小问题范围继续。",
    run_deadline_exceeded: "这轮回复处理超时，已停止生成。",
    budget_admission_failed: "暂时无法确认处理额度，这轮回复已停止。",
    budget_settlement_failed: "暂时无法确认处理额度，这轮回复已停止。",
    budget_run_inactive: "这轮回复已结束，没有继续处理。",
    budget_snapshot_missing: "这轮回复暂时无法继续，请重新发起对话。",
  };
  return messages[code] ?? `${prefix}：${code}`;
}

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}


async function refreshMessages(conversationId: string) {
  try {
    await pullMessagesAfter(conversationId, lastSeqOf(conversationId));
    if (conversationId === activeId.value) await scrollToEnd();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "刷新语音消息失败", true);
  }
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
    const runtime = await api.runtimeMeta(token.value || null);
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
  sync(id, lastSeqOf(id));
  if (textReplyVoice.value) {
    void ensureVoiceSocket().catch((error) => {
      voiceStatus.value = error instanceof Error ? error.message : "语音连接失败";
    });
  }
  await scrollToEnd();
}

function lastSeqOf(id: string) {
  const messages = messagesByConversation.get(id) ?? [];
  // 只统计服务端确认过的序列：占位符是隔离的高位 seq，计入会让 sync 游标跳过真实消息。
  return messages.reduce(
    (latest, message) => (isLocalMessage(message) ? latest : Math.max(latest, message.seq ?? 0)),
    0,
  );
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

const {
  selectMode,
  selectedMessageIds,
  selectableMessages,
  toggleSelectMode,
  toggleMessageSelected,
  selectAllMessages,
  exportFullConversation,
  exportSelected,
} = useConversationExport({
  activeId,
  activeMessages,
  conversations,
  archivedConversations,
  messagesOf: (id) => messagesByConversation.get(id) ?? [],
  pullMessagesAfter,
  setStatus,
});

async function resumeSession() {
  if (!token.value || document.hidden || !navigator.onLine) return;
  if (resumePromise) return resumePromise;
  resumePromise = (async () => {
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
    else if (conversationId) sync(conversationId, lastSeqOf(conversationId));
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
  const conversationId = event.stream.replace("conversation:", "");
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
    const afterSeq = Number(event.payload.after_seq ?? 0);
    const nextAfterSeq = Number(event.payload.next_after_seq ?? event.seq ?? afterSeq);
    if (event.payload.has_more === true && nextAfterSeq > afterSeq) {
      sync(conversationId, nextAfterSeq);
      return;
    }
    // 同步收敛后仍存在的占位消息（超过最小年龄）说明服务端从未落库，恢复到输入框。
    const revoked = revokePendingUserMessages(conversationId, PENDING_REVOKE_MIN_AGE_MS);
    if (revoked && conversationId === activeId.value && !draft.value) {
      draft.value = revoked;
      setStatus("有消息未送达，已恢复到输入框", true);
    }
    return;
  }
  if (!event.payload.message && event.type !== "reply.delta" && event.type !== "reply.control") {
    if (event.type === "turn.failed") {
      streaming.value = null;
      // start_turn 阶段失败时不会有 message.committed 回显，占位消息需手动回收。
      const revoked = revokePendingUserMessages(conversationId);
      if (revoked && conversationId === activeId.value && !draft.value) {
        draft.value = revoked;
        setStatus(`${generationFailureText(event.payload.reason_code)}；消息已恢复到输入框`, true);
      } else {
        setStatus(generationFailureText(event.payload.reason_code), true);
      }
    } else if (event.type === "turn.cancelled") {
      streaming.value = null;
      setStatus("已取消");
    }
    return;
  }

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
  const conversationId = activeId.value;
  const text = draft.value.trim();
  sendPending.value = true;
  draft.value = "";
  // 乐观显示：立即落占位消息；定位等待与服务端管线期间消息始终可见。
  appendPendingUserMessage(conversationId, text);
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
      if (!draft.value) draft.value = revokePendingUserMessages(conversationId) ?? text;
      setStatus(error instanceof Error ? error.message : "语音连接失败", true);
    } finally {
      sendPending.value = false;
    }
    return;
  }
  sendMessage(conversationId, text, privacy.value, location);
  sendPending.value = false;
}

function cancelStreaming() {
  if (streaming.value) cancel(streaming.value.generationId);
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
  const restored = await restoreSession();
  if (!restored) return;
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
  disposeVoice();
  disposeAuth();
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
    <div class="card">
      <h1>Aria Hub</h1>
      <form class="login-form" @submit.prevent="loginWithPassword">
        <label>
          <span>账号</span>
          <input
            v-model="loginUsername"
            type="text"
            autocomplete="username"
            placeholder="用户名（未设置可留空）"
          >
        </label>
        <label>
          <span>密码</span>
          <input
            v-model="loginPassword"
            type="password"
            autocomplete="current-password"
            placeholder="密码"
          >
        </label>
        <button class="primary" type="submit" :disabled="loginBusy || !loginPassword">登录</button>
      </form>
      <div v-if="ssoEnabled" class="login-divider"><span>或</span></div>
      <button v-if="ssoEnabled" class="ghost" type="button" :disabled="authBusy" @click="loginWithPnkx">
        使用 pnkx 账号登录
      </button>
      <button v-if="installPrompt && !isStandalone" class="ghost" type="button" @click="installPwa">安装到手机</button>
      <p v-else-if="isIos && !isStandalone" class="install-hint">iPhone：点 Safari“分享”→“添加到主屏幕”</p>
      <p v-if="statusText" class="status" :class="{ error: statusError }">{{ statusText }}</p>
    </div>
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
        <a class="admin-entry" href="/admin/" target="_blank" rel="noopener" title="打开 Aria 管理后台">管理</a>
      </header>
      <div class="conversation-tabs">
        <button type="button" :class="{ active: !showArchivedConversations }" @click="selectConversationGroup(false)">会话</button>
        <button type="button" :class="{ active: showArchivedConversations }" @click="selectConversationGroup(true)">归档 {{ archivedConversations.length }}</button>
      </div>
      <button v-if="!showArchivedConversations" class="primary new-chat" type="button" @click="createConversation">
        <PhPlus :size="17" weight="bold" aria-hidden="true" />
        <span>新会话</span>
      </button>
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
            <button class="conversation-action export-action" type="button" title="导出会话为 Markdown" aria-label="导出会话为 Markdown" @click="exportFullConversation(conversation.id)">
              <PhDownloadSimple :size="16" weight="bold" aria-hidden="true" />
            </button>
            <button v-if="showArchivedConversations" class="conversation-action" type="button" title="恢复会话" aria-label="恢复会话" @click="restoreConversation(conversation.id)">
              <PhArrowCounterClockwise :size="16" weight="bold" aria-hidden="true" />
            </button>
            <button v-else class="conversation-action" type="button" title="归档会话" aria-label="归档会话" @click="archiveConversation(conversation.id)">
              <PhArchive :size="16" weight="bold" aria-hidden="true" />
            </button>
            <button class="conversation-action danger-action" type="button" title="永久删除会话" aria-label="永久删除会话" @click="removeConversation(conversation.id)">
              <PhTrash :size="16" weight="bold" aria-hidden="true" />
            </button>
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
        <GoogleCalendarConnection v-if="privacy === 'L1'" :key="token" :token="token" />
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
            :class="[message.role, { selected: selectMode && selectedMessageIds.has(message.id), pending: isLocalMessage(message) }]"
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

      <RunHistory v-if="token" :key="token" :token="token" :refresh-key="planRefresh" />

      <footer class="composer">
        <p v-if="activeConversationArchived" class="archived-notice">此会话已归档。恢复后可以继续发送消息。</p>
        <div class="composer-toolbar">
          <button
            class="voice-button"
            :class="{ recording: voiceLive }"
            type="button"
            :disabled="!activeId || activeConversationArchived || !!streaming || voiceRecording || (voiceBusy && !voiceLive)"
            @click="toggleVoiceCall"
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
            :disabled="!activeId || activeConversationArchived || !!streaming || (voiceBusy && !voiceRecording)"
            @click="toggleVoiceRecording"
          >
            <PhStopCircle v-if="voiceRecording" :size="17" weight="fill" aria-hidden="true" />
            <PhMicrophone v-else :size="17" weight="fill" aria-hidden="true" />
            <span>{{ voiceRecording ? "结束并发送" : "说一句" }}</span>
          </button>
          <button v-if="voiceBusy && !voiceRecording" class="interrupt-button" type="button" @click="interruptVoice">打断播报</button>
          <select v-model="privacy" :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy" @change="onPrivacyChanged">
            <option value="L0">L0 · 可上云</option>
            <option value="L1">L1 · 常规</option>
            <option value="L2">L2 · 仅本地</option>
          </select>
          <label class="feature-toggle" title="开启后，文字输入也会播放 TTS 语音回复">
            <input
              v-model="textReplyVoice"
              type="checkbox"
              :disabled="!!streaming || voiceRecording || voiceLive || voiceBusy"
              @change="onTextReplyVoiceChanged"
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
              @change="onLocationEnabledChanged"
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
              :disabled="!token || !!streaming || voiceRecording || voiceLive || voiceBusy"
              @change="onNotificationsChanged"
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
            :disabled="!activeId || !selectableMessages.length"
            @click="toggleSelectMode"
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
            @keydown.enter.exact.prevent="send"
          />
          <button v-if="!streaming" class="primary send-button" type="button" :disabled="!canSend" @click="send">
            <span>发送</span>
            <PhPaperPlaneRight :size="18" weight="fill" aria-hidden="true" />
          </button>
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
          <span :title="accountName || undefined">{{ displayName }}</span>
          <button class="ghost" type="button" @click="togglePasswordPanel">密码</button>
          <button v-if="installPrompt && !isStandalone" class="ghost" type="button" @click="installPwa">安装</button>
          <button class="ghost" type="button" @click="logout">退出</button>
        </div>
        <form v-if="passwordPanelOpen" class="password-panel" @submit.prevent="submitPasswordChange">
          <p class="password-hint">设置后可用「{{ accountName || '用户名' }} + 密码」直接登录{{ accountName ? '' : '（用户名由管理员在用户管理中设置）' }}</p>
          <input v-model="passwordCurrent" type="password" autocomplete="current-password" placeholder="当前密码（首次设置留空）">
          <input v-model="passwordNew" type="password" autocomplete="new-password" placeholder="新密码（至少 8 位）">
          <input v-model="passwordConfirm" type="password" autocomplete="new-password" placeholder="再次输入新密码">
          <div class="password-actions">
            <button class="ghost" type="button" @click="togglePasswordPanel">取消</button>
            <button class="primary" type="submit" :disabled="passwordBusy">保存</button>
          </div>
        </form>
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
.login-form { display: grid; gap: 10px; }
.login-form label { display: grid; gap: 4px; font-size: 12px; color: var(--muted); }
.login-form input, .password-panel input { min-height: 38px; border: 1px solid var(--line); border-radius: 10px; padding: 0 12px; background: var(--panel2); color: var(--text); font: inherit; }
.login-divider { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.login-divider::before, .login-divider::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.password-panel { display: grid; gap: 8px; margin: 0 0 10px; padding: 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel2); }
.password-hint { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.5; }
.password-actions { display: flex; justify-content: flex-end; gap: 8px; }
.card h1 { margin: 0; font-size: 22px; }
.hint { color: var(--muted); margin: 0; font-size: 13px; }
.install-hint { margin:0; color:var(--muted); font-size:12px; line-height:1.5; text-align:center; }
.status { color: var(--muted); font-size: 12px; margin: 0; }
.status.error { color: var(--danger); }

.shell { display:grid; grid-template-columns:250px minmax(0,1fr) clamp(320px,22vw,380px); grid-template-areas:"conversations chat avatar"; height:100%; background:var(--bg); }
aside { display:flex; flex-direction:column; gap:12px; min-width:0; padding:14px; min-height:0; background:var(--panel); }
aside header strong { font-size:16px; }
.admin-entry { font-size:11px; color:#9cbdad; border:1px solid #2b5142; border-radius:6px; padding:2px 8px; text-decoration:none; }
.admin-entry:hover { color:#fff; background:#2b5142; }
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
.new-chat { display:flex; align-items:center; justify-content:center; gap:7px; width:100%; box-shadow:0 5px 14px color-mix(in srgb,var(--accent) 20%,transparent); }
.conversation-tabs { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
.conversation-tabs button { padding:7px 8px; color:var(--muted); background:var(--panel2); }
.conversation-tabs button.active { border-color:var(--accent); color:var(--accent); background:var(--accent-soft); }
.conversation-list { flex: 1; min-width:0; min-height: 0; overflow-x:hidden; overflow-y: auto; display: grid; align-content: start; gap: 6px; }
.conversation-empty { margin:10px 4px; color:var(--muted); font-size:12px; text-align:center; }
.conversation-item { position:relative; display:flex; align-items:center; min-width:0; max-width:100%; border-radius:10px; }
.conversation { flex:1 1 auto; min-width:0; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:left; border:1px solid var(--line); background:var(--panel); padding:9px 12px; border-radius:10px; transition:padding-right 160ms ease, background-color 160ms ease, border-color 160ms ease, box-shadow 160ms ease; }
.conversation-item:hover .conversation,.conversation-item:focus-within .conversation { padding-right:98px; border-color:color-mix(in srgb,var(--accent) 28%,var(--line)); box-shadow:0 5px 16px color-mix(in srgb,var(--text) 6%,transparent); }
.conversation-item.active .conversation { border-color: var(--accent); background: var(--accent-soft); color:var(--accent); font-weight:600; }
.conversation small { display: block; overflow:hidden; text-overflow:ellipsis; color: var(--muted); margin-top: 3px; font-size: 11px; font-weight:400; }
.conversation-actions { position:absolute; right:6px; display:flex; align-items:center; gap:2px; opacity:0; transform:translateX(4px); pointer-events:none; transition:opacity 150ms ease,transform 150ms ease; }
.conversation-item:hover .conversation-actions,.conversation-item:focus-within .conversation-actions,.conversation-item.active .conversation-actions { opacity:1; transform:translateX(0); pointer-events:auto; }
.conversation-action { display:grid; place-items:center; width:27px; height:27px; padding:0; border:0; border-radius:7px; background:color-mix(in srgb,var(--panel) 86%,transparent); color:var(--muted); }
.conversation-action:hover:not(:disabled),.conversation-action:focus-visible { border-color:transparent; background:var(--accent-soft); color:var(--accent); outline:none; }
.conversation-action.danger-action:hover:not(:disabled),.conversation-action.danger-action:focus-visible { background:color-mix(in srgb,var(--danger) 10%,var(--panel)); color:var(--danger); }
.debug-link { color: var(--muted); font-size: 12px; text-decoration: none; }
.aside-footer { display:flex; align-items:end; gap:8px; min-width:0; padding-top:10px; border-top:1px solid var(--line); }
.theme-field { display:grid; flex:1; min-width:0; gap:5px; color:var(--muted); font-size:10px; }
.theme-field select { min-width:0; width:100%; padding:7px 9px; font-size:12px; }
.aside-footer .debug-link { flex:none; padding:8px 2px; }

main { grid-area:chat; display:grid; grid-template-rows:minmax(0,1fr) auto; min-width:0; min-height:0; background:var(--bg); }
.mobile-topbar,.mobile-backdrop { display:none; }
.messages { overflow-y:auto; padding:20px clamp(20px,3vw,42px); }
.empty { height: 100%; display: grid; place-items: center; color: var(--muted); }
.message { width:fit-content; max-width:min(78%,820px); margin:0 auto 14px 0; display:flex; align-items:flex-start; gap:8px; }
.message.user { margin-right:0; margin-left:auto; justify-content:flex-end; }
.select-check { display: flex; padding: 12px 0 0 2px; cursor: pointer; accent-color: var(--accent); }
.select-check input { cursor: pointer; }
.message.selected .bubble { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.bubble.selecting { cursor: pointer; }
.bubble.selecting :deep(a), .bubble.selecting :deep(button) { pointer-events: none; }
.select-bar { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; padding: 8px 10px; border: 1px solid var(--accent); border-radius: 10px; background: color-mix(in srgb, var(--panel) 92%, transparent); backdrop-filter: blur(8px); }
.select-bar strong { color: var(--accent); font-size: 12px; }
.select-bar button { padding: 5px 10px; font-size: 12px; }
.select-toggle.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.bubble { min-width:0; line-height:1.55; padding:10px 14px; border:1px solid var(--line); border-radius:6px 16px 16px 16px; background:var(--panel); box-shadow:0 4px 14px color-mix(in srgb,var(--text) 5%,transparent); position:relative; overflow-wrap:anywhere; }
.user .bubble { border-radius:16px 6px 16px 16px; background:var(--message-user-bg); border-color:var(--message-user-bg); color:#fff; box-shadow:0 7px 18px color-mix(in srgb,var(--message-user-bg) 22%,transparent); }
.user .bubble :deep(.markdown-content a) { color:inherit; }
/* 本地占位消息：半透明表示尚未被服务端确认，回显后恢复 */
.message.pending .bubble { opacity:.62; }
.bubble { transition:opacity 180ms ease; }
.bubble.streaming { white-space:pre-wrap; }
.bubble.streaming::after { content: ""; }
.emotion { display: inline-block; margin-left: 8px; font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; vertical-align: 1px; }
.persona-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); opacity:.75; }
.recall-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:0 7px; opacity:.8; }

.composer { border-top:1px solid var(--line); padding:12px clamp(16px,2vw,26px) 14px; display:grid; gap:10px; background:color-mix(in srgb,var(--panel) 97%,var(--bg)); box-shadow:0 -8px 24px color-mix(in srgb,var(--text) 3%,transparent); }
.archived-notice { margin:0; color:var(--muted); font-size:12px; }
.composer-toolbar { display:flex; align-items:center; gap:7px; min-width:0; overflow-x:auto; overflow-y:hidden; padding-bottom:1px; scrollbar-width:none; }
.composer-toolbar::-webkit-scrollbar { display:none; }
.composer-toolbar > * { flex:none; }
.composer-toolbar select { min-width:112px; padding:7px 9px; border-radius:10px; font-size:12px; font-weight:600; }
.select-toggle,.feature-toggle { min-height:34px; border:1px solid var(--line); border-radius:10px; background:var(--panel); }
.select-toggle { display:inline-flex; align-items:center; gap:6px; padding:7px 10px; color:var(--muted); font-size:12px; }
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
.voice-status { color: var(--muted); font-size: 12px; }
.viseme-meter { width:44px; height:8px; border:1px solid var(--line); border-radius:999px; overflow:hidden; background:var(--panel2); }
.viseme-fill { display:block; width:100%; height:100%; transform-origin:left center; background:var(--accent); transition:transform 50ms linear; }
.message-time { margin-left:6px; color:var(--muted); font-size:11px; white-space:nowrap; }
.user .message-time { color:#fff; font-weight:500; }
.voice-transcript { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.composer-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:stretch; }
.composer textarea { min-height:56px; resize:none; padding:12px 14px; border-radius:12px; line-height:1.45; }
.send-button { display:inline-flex; align-items:center; justify-content:center; gap:7px; min-width:82px; border-radius:12px; font-weight:600; box-shadow:0 6px 16px color-mix(in srgb,var(--accent) 22%,transparent); }

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
