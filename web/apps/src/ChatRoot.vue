<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  ChatApi,
  type AvatarChoice,
  type ChatMessage,
  type Conversation,
  type PrivacyLevel,
} from "@aria/shared";
import ChatAuthCard from "./ChatAuthCard.vue";
import ChatSidebar from "./ChatSidebar.vue";
import ChatMessages from "./ChatMessages.vue";
import ChatComposer from "./ChatComposer.vue";
import ChatAvatar from "./ChatAvatar.vue";
import { useChatSocket } from "./composables/useChatSocket";
import { useVoice, type StreamingState } from "./composables/useVoice";
import { useAuth } from "./composables/useAuth";
import { useChatLocation } from "./composables/useChatLocation";
import { useChatPush } from "./composables/useChatPush";
import { useChatTheme } from "./composables/useChatTheme";
import { useMessagePresentation } from "./composables/useMessagePresentation";
import { useMessages, PENDING_REVOKE_MIN_AGE_MS } from "./composables/useMessages";
import { useAvatar, type AvatarMeta } from "./composables/useAvatar";
import { useConversations } from "./composables/useConversations";
import PlanInbox from "./PlanInbox.vue";
import RunHistory from "./RunHistory.vue";
import MailAttachments from "./MailAttachments.vue";
import MailDrafts from "./MailDrafts.vue";
import SafetyAlerts from "./SafetyAlerts.vue";
import ConfirmationDrafts from "./ConfirmationDrafts.vue";
import { useConversationExport } from "./composables/useConversationExport";
import { useChatEvents } from "./composables/useChatEvents";
import { useSessionResume } from "./composables/useSessionResume";
import { useComposer } from "./composables/useComposer";
import { usePwaInstall, type BeforeInstallPromptEvent } from "./composables/usePwaInstall";

// 聊天前端装配层：状态与行为全部来自 composables，子组件只负责渲染。
// 普通 Web 保留既有本地会话；安装后的 PWA 只在当前会话存储令牌。
const api = new ChatApi();
const isStandalone =
  window.matchMedia("(display-mode: standalone)").matches ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true;
const isIos =
  /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

const token = ref<string>("");
const displayName = ref<string>("");
const accountName = ref<string>("");
const statusText = ref("");
const statusError = ref(false);
const personaMeta = ref<{ version: number; name: string } | null>(null);
const avatarMeta = ref<AvatarMeta | null>(null);
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

const activeConversationArchived = computed(() =>
  archivedConversations.value.some((conversation) => conversation.id === activeId.value),
);
const visibleConversations = computed(() =>
  showArchivedConversations.value ? archivedConversations.value : conversations.value,
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

const { locationEnabled, locationSupported, locationPolicy, freshCachedLocation, resolveLocationForText, onLocationEnabledChanged } =
  useChatLocation({ privacy, setStatus });

const { pushSupported, notificationsEnabled, refreshPushToggle, onNotificationsChanged } = useChatPush({
  api,
  token,
  setStatus,
});

const { themePreference, onThemeChanged, syncThemePreference, attachThemeChannel, detachThemeChannel } =
  useChatTheme({ api, token, setStatus });
const { generationFailureText, emotionKeyOf, agentReplyOf } = useMessagePresentation();
const {
  activeMessages,
  isLocalMessage,
  appendPendingUserMessage,
  revokePendingUserMessages,
  mergeMessages,
  pullMessagesAfter,
  refreshMessages,
  lastSeqOf,
} = useMessages({
  api,
  token,
  activeId,
  conversations,
  messagesByConversation,
  privacy,
  setStatus,
  scrollToEnd,
});
const { handleEvent } = useChatEvents({
  activeId,
  streaming,
  draft,
  planRefresh,
  avatarMotion,
  setStatus,
  sync: (conversationId, afterSeq) => sync(conversationId, afterSeq),
  revokePendingUserMessages,
  mergeMessages,
  scrollToEnd,
  loadRuntimeMeta: () => loadRuntimeMeta(),
  generationFailureText,
});
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
  loadRuntimeMeta: () => loadRuntimeMeta(),
  freshCachedLocation,
  generationFailureText,
});
const {
  avatarEmotion,
  avatarExpression,
  avatarSpeaking,
  avatarImageUrl,
  loadRuntimeMeta,
  loadAvatars,
  switchAvatar,
} = useAvatar({
  api,
  token,
  personaMeta,
  avatarMeta,
  avatars,
  avatarSwitchBusy,
  streaming,
  activeMessages,
  voiceViseme,
  voiceBusy,
  locationPolicy,
  emotionKeyOf,
  agentReplyOf,
  setStatus,
});
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
const {
  enterChat,
  loadConversations,
  openConversation,
  createConversation,
  selectConversationGroup,
  removeConversation,
  archiveConversation,
  restoreConversation,
} = useConversations({
  api,
  token,
  activeId,
  conversations,
  archivedConversations,
  showArchivedConversations,
  visibleConversations,
  mobileConversationsOpen,
  messagesByConversation,
  setStatus,
  closeVoice,
  sync,
  connectSocket,
  scrollToEnd,
  selectMode,
  selectedMessageIds,
  textReplyVoice,
  ensureVoiceSocket,
  voiceStatus,
  logout: () => logout(),
  mergeMessages,
  lastSeqOf,
  syncThemePreference,
  loadRuntimeMeta,
  loadAvatars,
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
const { resumeSession, handleVisibilityChange, handleOnline, handleOffline } = useSessionResume({
  token,
  activeId,
  networkOnline,
  setStatus,
  syncThemePreference,
  pullMessagesAfter,
  lastSeqOf,
  scrollToEnd,
  logout,
  socketReady,
  connectSocket,
  sync,
  closeSocket,
  voiceRecording,
  voiceLive,
  closeVoice,
});
const { chooseLocation, send, cancelStreaming } = useComposer({
  canSend,
  activeId,
  draft,
  sendPending,
  streaming,
  textReplyVoice,
  unlockVoicePlayback,
  appendPendingUserMessage,
  locationEnabled,
  privacy,
  setStatus,
  resolveLocationForText,
  ensureVoiceSocket,
  voiceBusy,
  voiceStatus,
  revokePendingUserMessages,
  sendMessage,
  cancel,
});
const { handleBeforeInstallPrompt, installPwa } = usePwaInstall(installPrompt);

function clearConversationState() {
  conversations.value = [];
  archivedConversations.value = [];
  activeId.value = null;
  messagesByConversation.clear();
}

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}
async function scrollToEnd() {
  await nextTick();
  messagesRoot.value?.scrollTo({ top: messagesRoot.value.scrollHeight });
}
function setMessagesRoot(el: Element | null) {
  messagesRoot.value = el as HTMLElement | null;
}
onMounted(async () => {
  window.addEventListener("focus", syncThemePreference);
  window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
  window.addEventListener("online", handleOnline);
  window.addEventListener("offline", handleOffline);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  attachThemeChannel();
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
  detachThemeChannel();
  closeSocket();
  void closeVoice();
  disposeVoice();
  disposeAuth();
});
</script>

<template>
  <ChatAuthCard
    v-if="!token"
    v-model:login-username="loginUsername"
    v-model:login-password="loginPassword"
    :sso-enabled="ssoEnabled" :auth-busy="authBusy" :login-busy="loginBusy"
    :is-standalone="isStandalone" :is-ios="isIos" :can-install="!!installPrompt"
    :status-text="statusText" :status-error="statusError"
    @login="loginWithPassword" @login-pnkx="loginWithPnkx" @install="installPwa"
  />

  <div v-else class="shell">
    <button
      v-if="mobileConversationsOpen || mobileCompanionOpen"
      class="mobile-backdrop" type="button" aria-label="关闭面板"
      @click="mobileConversationsOpen = false; mobileCompanionOpen = false"
    ></button>

    <ChatSidebar
      :mobile-open="mobileConversationsOpen" :show-archived="showArchivedConversations"
      :visible-conversations="visibleConversations" :archived-count="archivedConversations.length"
      :active-id="activeId" :token="token" :privacy="privacy"
      v-model:theme-preference="themePreference"
      @select-group="selectConversationGroup" @create-conversation="createConversation"
      @open-conversation="openConversation" @export-conversation="exportFullConversation"
      @restore-conversation="restoreConversation" @archive-conversation="archiveConversation"
      @remove-conversation="removeConversation" @theme-changed="onThemeChanged"
    />

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

      <ChatMessages
        :messages="activeMessages" :active-id="activeId" :select-mode="selectMode"
        :selected-ids="selectedMessageIds" :streaming="streaming"
        :is-local="isLocalMessage" :set-messages-root="setMessagesRoot"
        @select-all="selectAllMessages" @export-selected="exportSelected"
        @toggle-select-mode="toggleSelectMode" @toggle-message-selected="toggleMessageSelected"
        @choose-location="chooseLocation"
      />

      <SafetyAlerts v-if="token" :key="token" :token="token" />
      <MailAttachments v-if="token && privacy === 'L1'" :key="token" :token="token" />
      <MailDrafts v-if="token && privacy === 'L1'" :key="token" :token="token" />
      <ConfirmationDrafts v-if="token && privacy === 'L1'" :key="token" :token="token" />
      <PlanInbox v-if="token && privacy === 'L1'" :key="token" :token="token" :refresh-key="planRefresh" />
      <RunHistory v-if="token" :key="token" :token="token" :refresh-key="planRefresh" />

      <ChatComposer
        v-model:draft="draft" v-model:privacy="privacy" v-model:text-reply-voice="textReplyVoice"
        v-model:location-enabled="locationEnabled" v-model:notifications-enabled="notificationsEnabled"
        :active-conversation-archived="activeConversationArchived" :has-active-id="!!activeId"
        :streaming="streaming" :voice-busy="voiceBusy" :voice-recording="voiceRecording"
        :voice-live="voiceLive" :voice-status="voiceStatus" :voice-viseme="voiceViseme"
        :voice-transcript="voiceTranscript" :status-text="statusText" :status-error="statusError"
        :location-supported="locationSupported" :push-supported="pushSupported" :has-token="!!token"
        :select-mode="selectMode" :can-export="selectableMessages.length > 0"
        :socket-ready="socketReady" :can-send="canSend"
        @toggle-voice-call="toggleVoiceCall" @toggle-voice-recording="toggleVoiceRecording"
        @interrupt-voice="interruptVoice" @privacy-changed="onPrivacyChanged"
        @text-reply-voice-changed="onTextReplyVoiceChanged"
        @location-enabled-changed="onLocationEnabledChanged"
        @notifications-changed="onNotificationsChanged" @toggle-select-mode="toggleSelectMode"
        @send="send" @cancel-streaming="cancelStreaming"
      />
    </main>

    <ChatAvatar
      :mobile-open="mobileCompanionOpen" :persona-meta="personaMeta" :avatar-meta="avatarMeta"
      :avatars="avatars" :avatar-switch-busy="avatarSwitchBusy" :avatar-motion="avatarMotion"
      :avatar-emotion="avatarEmotion" :avatar-expression="avatarExpression"
      :avatar-speaking="avatarSpeaking" :avatar-image-url="avatarImageUrl"
      :voice-viseme="voiceViseme" :display-name="displayName" :account-name="accountName"
      :password-panel-open="passwordPanelOpen" :password-busy="passwordBusy"
      :is-standalone="isStandalone" :can-install="!!installPrompt"
      v-model:password-current="passwordCurrent" v-model:password-new="passwordNew"
      v-model:password-confirm="passwordConfirm"
      @toggle-password-panel="togglePasswordPanel" @submit-password-change="submitPasswordChange"
      @install="installPwa" @logout="logout" @switch-avatar="switchAvatar"
    />
  </div>
</template>

<style scoped>
.shell { display:grid; grid-template-columns:250px minmax(0,1fr) clamp(320px,22vw,380px); grid-template-areas:"conversations chat avatar"; height:100%; background:var(--bg); }
aside { display:flex; flex-direction:column; gap:12px; min-width:0; padding:14px; min-height:0; background:var(--panel); }
.conversation-sidebar { grid-area:conversations; border-right:1px solid var(--line); }
.avatar-sidebar { grid-area:avatar; gap:14px; padding:18px; border-left:1px solid var(--line); }
main { grid-area:chat; display:grid; grid-template-rows:minmax(0,1fr) auto; min-width:0; min-height:0; background:var(--bg); }
.mobile-topbar,.mobile-backdrop { display:none; }

@media (max-width:1100px) {
  .shell { grid-template-columns:220px minmax(0,1fr) 280px; }
  .avatar-sidebar { padding:14px; }
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
}
</style>
