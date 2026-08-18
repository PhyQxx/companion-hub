<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  ApiError,
  ChatApi,
  ChatSocket,
  type AgentReplyControl,
  type AuthSession,
  type ChatMessage,
  type Conversation,
  type PrivacyLevel,
  type SocketEvent,
} from "@aria/shared";

const api = new ChatApi();
const TOKEN_KEY = "ariaChatToken";

const token = ref<string>("");
const displayName = ref<string>("");
const setupRequired = ref(false);
const password = ref("");
const adminToken = ref("");
const authBusy = ref(false);
const statusText = ref("");
const statusError = ref(false);

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

let socket: ChatSocket | null = null;
let reconnectTimer: number | null = null;
let reconnectAttempts = 0;

const activeMessages = computed(() =>
  activeId.value ? (messagesByConversation.get(activeId.value) ?? []) : [],
);
const canSend = computed(
  () => socketReady.value && !!activeId.value && draft.value.trim().length > 0 && !streaming.value,
);

function setStatus(text: string, error = false) {
  statusText.value = text;
  statusError.value = error;
}

async function scrollToEnd() {
  await nextTick();
  messagesRoot.value?.scrollTo({ top: messagesRoot.value.scrollHeight });
}

function emotionOf(message: ChatMessage): string | null {
  const reply = (message.decision_meta as { agent_reply?: AgentReplyControl } | null)?.agent_reply;
  return reply?.emotion ?? null;
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

function handleEvent(event: SocketEvent) {
  if (event.type === "protocol.error") {
    setStatus(`协议错误：${String(event.payload.reason_code ?? "")}`, true);
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
  }
  if (conversationId === activeId.value) void scrollToEnd();
}

function send() {
  if (!canSend.value || !activeId.value) return;
  socket?.sendMessage(activeId.value, draft.value.trim(), privacy.value);
  draft.value = "";
}

function cancelStreaming() {
  if (streaming.value) socket?.cancel(streaming.value.generationId);
}

onMounted(async () => {
  const saved = localStorage.getItem(TOKEN_KEY);
  if (!saved) {
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

onBeforeUnmount(closeSocket);
</script>

<template>
  <div v-if="!token" class="auth">
    <form class="card" @submit.prevent="submitAuth">
      <h1>Aria</h1>
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
        <strong>Aria</strong>
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
              <span v-if="message.role === 'assistant' && emotionOf(message)" class="emotion">{{ emotionOf(message) }}</span>
            </div>
          </div>
        </template>
        <div v-if="streaming && streaming.conversationId === activeId" class="message assistant">
          <div class="bubble streaming">
            {{ streaming.text }}▋
            <span v-if="streaming.emotion" class="emotion">{{ streaming.emotion }}</span>
          </div>
        </div>
      </div>

      <footer class="composer">
        <div class="composer-meta">
          <select v-model="privacy" :disabled="!!streaming">
            <option value="L0">L0 · 可上云</option>
            <option value="L1">L1 · 常规</option>
            <option value="L2">L2 · 仅本地</option>
          </select>
          <span class="status" :class="{ error: statusError }">{{ statusText }}</span>
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

.composer { border-top: 1px solid var(--line); padding: 12px 16px; display: grid; gap: 8px; }
.composer-meta { display: flex; align-items: center; gap: 12px; }
.composer-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: end; }
.composer textarea { resize: none; }

@media (max-width: 720px) {
  .shell { grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr); }
  aside { border-right: none; border-bottom: 1px solid var(--line); }
  .conversation-list { display: flex; overflow-x: auto; gap: 6px; }
  .message { max-width: 95%; }
}
</style>
