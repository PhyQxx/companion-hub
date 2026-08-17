const state = {
  token: sessionStorage.getItem("ariaChatToken") || "",
  conversations: [],
  activeId: null,
  socket: null,
  socketReady: false,
  activeGeneration: null,
  pendingSend: false,
  streamDrafts: new Map(),
};
const el = (id) => document.getElementById(id);

function resizeComposer() {
  const input = el("text");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 128)}px`;
}

function scrollMessagesToBottom(behavior = "auto") {
  requestAnimationFrame(() => {
    const root = el("messages");
    root.scrollTo({ top: root.scrollHeight, behavior });
  });
}

function updateControls() {
  const authenticated = Boolean(state.token);
  document.body.classList.toggle("is-authenticated", authenticated);
  el("new-conversation").disabled = !authenticated;
  el("send").disabled = !state.activeId || !el("text").value.trim() || state.pendingSend || Boolean(state.activeGeneration);
  el("logout").disabled = !authenticated;
}

async function request(path, options = {}, token = state.token) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail?.reason_code || body.detail || `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return body;
}

function setStatus(text, error = false) {
  el("status").textContent = text;
  el("status").className = error ? "status-pill error" : "status-pill";
}

async function initialize() {
  try {
    const status = await request("/api/v1/auth/status", {}, "");
    el("setup-panel").hidden = !status.setup_required;
    if (status.setup_required) el("setup-panel").open = true;
    if (state.token) await connect();
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function setup() {
  try {
    const session = await request(
      "/api/v1/auth/setup",
      {
        method: "POST",
        body: JSON.stringify({
          display_name: el("display-name").value.trim() || "主人",
          password: el("password").value,
        }),
      },
      el("admin-token").value.trim(),
    );
    acceptSession(session);
    el("admin-token").value = "";
    el("setup-panel").hidden = true;
    await loadConversations();
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function login() {
  try {
    const session = await request(
      "/api/v1/auth/login",
      { method: "POST", body: JSON.stringify({ password: el("password").value }) },
      "",
    );
    acceptSession(session);
    await loadConversations();
  } catch (error) {
    setStatus(error.message, true);
  }
}

function acceptSession(session) {
  state.token = session.access_token;
  sessionStorage.setItem("ariaChatToken", state.token);
  el("password").value = "";
  setStatus(`已登录 · ${session.user.display_name}`);
  updateControls();
}

async function connect() {
  try {
    const me = await request("/api/v1/auth/me");
    setStatus(`已登录 · ${me.user.display_name}`);
    await loadConversations();
  } catch (error) {
    clearSession();
    setStatus(error.message, true);
  }
}

async function logout() {
  try {
    if (state.token) await request("/api/v1/auth/logout", { method: "POST" });
  } catch (_) {
    // Local cleanup still applies when the server session is already unavailable.
  }
  clearSession();
  renderConversations();
  renderMessages([]);
  setStatus("已退出");
}

function clearSession() {
  if (state.socket) {
    state.socket.close();
    state.socket = null;
  }
  state.socketReady = false;
  state.activeGeneration = null;
  state.pendingSend = false;
  el("cancel").hidden = true;
  state.token = "";
  state.conversations = [];
  state.activeId = null;
  sessionStorage.removeItem("ariaChatToken");
  updateControls();
}

async function loadConversations() {
  state.conversations = await request("/api/v1/chat/conversations");
  setStatus(`已连接 · ${state.conversations.length} 个会话`);
  renderConversations();
  if (state.conversations.length) await openConversation(state.conversations[0].id);
  else renderMessages([]);
  connectSocket();
}

function connectSocket() {
  if (!state.token || state.socket?.readyState === WebSocket.OPEN) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws/chat`);
  state.socket = socket;
  state.socketReady = false;
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ type: "authenticate", access_token: state.token }));
  });
  socket.addEventListener("message", (event) => handleSocketEvent(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    state.socketReady = false;
    state.pendingSend = false;
    updateControls();
    if (state.token) {
      setStatus("实时连接已断开，发送将使用 REST");
      setTimeout(connectSocket, 1500);
    }
  });
}

function handleSocketEvent(event) {
  if (event.type === "auth.accepted") {
    state.socketReady = true;
    const cursors = Object.fromEntries(state.conversations.map((item) => [item.id, item.last_seq]));
    state.socket.send(JSON.stringify({ type: "client_hello", cursors }));
    setStatus("实时连接已建立");
    return;
  }
  const message = event.payload?.message;
  if (event.type === "message.committed" && message) {
    updateConversationSeq(message.conversation_id || event.stream.split(":")[1], message.seq);
    if (event.stream === `conversation:${state.activeId}`) appendMessage(message);
  } else if (event.type === "turn.accepted") {
    state.pendingSend = false;
    state.activeGeneration = event.generation_id;
    el("cancel").hidden = false;
    updateControls();
  } else if (event.type === "reply.delta") {
    appendDelta(event.generation_id, event.payload.delta, event.stream);
  } else if (event.type === "reply.committed" && message) {
    removeDraft(event.generation_id);
    updateConversationSeq(event.stream.split(":")[1], message.seq);
    if (event.stream === `conversation:${state.activeId}`) appendMessage(message);
    finishGeneration("回复已保存");
  } else if (event.type === "turn.cancelled") {
    removeDraft(event.generation_id);
    finishGeneration("生成已取消");
  } else if (event.type === "turn.failed") {
    removeDraft(event.generation_id);
    finishGeneration(event.payload.reason_code || "生成失败", true);
  } else if (event.type === "protocol.error") {
    state.pendingSend = false;
    updateControls();
    setStatus(event.payload.reason_code || "协议错误", true);
  }
}

function appendDelta(generationId, delta, stream) {
  if (stream !== `conversation:${state.activeId}`) return;
  let draft = state.streamDrafts.get(generationId);
  if (!draft) {
    const item = document.createElement("article");
    item.className = "message assistant streaming";
    item.innerHTML = '<div class="bubble"></div><div class="meta">正在生成…</div>';
    el("messages").querySelector(".empty")?.remove();
    el("messages").appendChild(item);
    draft = { content: "", element: item };
    state.streamDrafts.set(generationId, draft);
  }
  draft.content += delta;
  draft.element.querySelector(".bubble").textContent = draft.content;
  scrollMessagesToBottom();
}

function removeDraft(generationId) {
  const draft = state.streamDrafts.get(generationId);
  draft?.element.remove();
  state.streamDrafts.delete(generationId);
}

function finishGeneration(text, error = false) {
  state.activeGeneration = null;
  state.pendingSend = false;
  el("cancel").hidden = true;
  updateControls();
  setStatus(text, error);
}

function updateConversationSeq(conversationId, seq) {
  const conversation = state.conversations.find((item) => item.id === conversationId);
  if (conversation) conversation.last_seq = Math.max(conversation.last_seq, seq);
  renderConversations();
}

function renderConversations() {
  const root = el("conversations");
  root.innerHTML = "";
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.className = `conversation${conversation.id === state.activeId ? " active" : ""}`;
    button.innerHTML = `${escapeHtml(conversation.title || "新会话")}<small>${conversation.last_seq} 条消息</small>`;
    button.addEventListener("click", () => openConversation(conversation.id));
    root.appendChild(button);
  }
}

async function createConversation() {
  if (!state.token) {
    setStatus("请先登录", true);
    return;
  }
  try {
    const conversation = await request("/api/v1/chat/conversations", {
      method: "POST",
      body: JSON.stringify({ title: `调试会话 ${state.conversations.length + 1}` }),
    });
    state.conversations.unshift(conversation);
    await openConversation(conversation.id);
    if (state.socketReady) {
      state.socket.send(JSON.stringify({ type: "sync.request", conversation_id: conversation.id, after_seq: 0 }));
    }
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function openConversation(id) {
  state.activeId = id;
  updateControls();
  renderConversations();
  try {
    renderMessages(await request(`/api/v1/chat/conversations/${id}/messages`));
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderMessages(messages) {
  const root = el("messages");
  root.innerHTML = messages.length ? "" : '<div class="empty">这个会话还没有消息。</div>';
  for (const message of messages) appendMessage(message, false);
  scrollMessagesToBottom();
}

function appendMessage(message, autoScroll = true) {
  const root = el("messages");
  if (root.querySelector(`[data-seq="${message.seq}"]`)) return;
  root.querySelector(".empty")?.remove();
  const item = document.createElement("article");
  item.className = `message ${message.role}`;
  item.dataset.seq = message.seq;
  const meta = message.decision_meta;
  const route = meta ? `${meta.endpoint} · ${meta.model} · ${Math.round(meta.latency_ms)}ms · cfg v${meta.config_version}` : message.privacy_level;
  item.innerHTML = `<div class="bubble">${escapeHtml(message.content)}</div><div class="meta">${escapeHtml(route)}</div>`;
  root.appendChild(item);
  if (autoScroll) scrollMessagesToBottom("smooth");
}

async function send(event) {
  event.preventDefault();
  const text = el("text").value.trim();
  if (!state.activeId || !text || state.pendingSend || state.activeGeneration) return;
  state.pendingSend = true;
  updateControls();
  setStatus("模型生成中…");
  if (state.socketReady) {
    state.socket.send(JSON.stringify({
      type: "message.send",
      conversation_id: state.activeId,
      text,
      privacy_level: el("privacy").value,
    }));
    el("text").value = "";
    resizeComposer();
    updateControls();
    return;
  }
  try {
    const turn = await request(`/api/v1/chat/conversations/${state.activeId}/messages`, {
      method: "POST",
      body: JSON.stringify({ text, privacy_level: el("privacy").value }),
    });
    el("text").value = "";
    resizeComposer();
    appendMessage(turn.user_message);
    appendMessage(turn.assistant_message);
    const conversation = state.conversations.find((item) => item.id === state.activeId);
    if (conversation) conversation.last_seq += 2;
    renderConversations();
    setStatus("回复已保存");
  } catch (error) {
    setStatus(error.message, true);
    await openConversation(state.activeId);
  } finally {
    state.pendingSend = false;
    updateControls();
  }
}

function cancelGeneration() {
  if (!state.socketReady || !state.activeGeneration) return;
  state.socket.send(JSON.stringify({ type: "turn.cancel", generation_id: state.activeGeneration }));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

el("setup").addEventListener("click", setup);
el("login").addEventListener("click", login);
el("logout").addEventListener("click", logout);
el("cancel").addEventListener("click", cancelGeneration);
el("new-conversation").addEventListener("click", createConversation);
el("composer").addEventListener("submit", send);
el("text").addEventListener("input", () => {
  resizeComposer();
  updateControls();
});
el("text").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  el("composer").requestSubmit();
});
el("privacy").addEventListener("change", (event) => {
  el("privacy-note").textContent = event.target.value === "L2"
    ? "L2 强制仅用本地模型；本地不可用时会明确失败。"
    : "L1 可按配置使用云模型。";
});

resizeComposer();
updateControls();
initialize();
