const state = {
  token: sessionStorage.getItem("ariaChatToken") || "",
  conversations: [],
  activeId: null,
  socket: null,
  socketReady: false,
  activeGeneration: null,
  pendingSend: false,
  streamDrafts: new Map(),
  voiceSocket: null,
  voiceSocketKey: "",
  voiceConnectPromise: null,
  voiceConnectKey: "",
  voiceReady: false,
  voiceSentence: null,
  voicePlayback: Promise.resolve(),
  voiceTurn: false,
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
  el("privacy").disabled = state.pendingSend || Boolean(state.activeGeneration);
  el("text-reply-voice").disabled = !authenticated || state.pendingSend || Boolean(state.activeGeneration);
}

function closeVoiceSocket() {
  state.voiceReady = false;
  state.voiceSentence = null;
  state.voiceSocketKey = "";
  state.voiceConnectPromise = null;
  state.voiceConnectKey = "";
  if (state.voiceSocket) state.voiceSocket.close(1000);
  state.voiceSocket = null;
}

function ensureVoiceSocket() {
  if (!state.activeId) return Promise.reject(new Error("请先选择会话"));
  const conversationId = state.activeId;
  const privacyLevel = el("privacy").value;
  const key = `${conversationId}:${privacyLevel}`;
  if (state.voiceSocket?.readyState === WebSocket.OPEN && state.voiceReady && state.voiceSocketKey === key) {
    return Promise.resolve(state.voiceSocket);
  }
  if (state.voiceConnectPromise && state.voiceConnectKey === key) return state.voiceConnectPromise;
  closeVoiceSocket();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws/voice`);
  socket.binaryType = "arraybuffer";
  state.voiceSocket = socket;
  state.voiceSocketKey = key;
  setStatus("正在连接语音输出…");
  const connecting = new Promise((resolve, reject) => {
    let settled = false;
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "authenticate", access_token: state.token }));
      socket.send(JSON.stringify({
        type: "voice.hello",
        conversation_id: conversationId,
        privacy_level: privacyLevel,
        format: "pcm_s16le",
        sample_rate: 16000,
        channels: 1,
      }));
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        const frame = JSON.parse(event.data);
        if (frame.type === "voice.ready" && !settled) {
          settled = true;
          state.voiceReady = true;
          resolve(socket);
        }
        handleVoiceEvent(frame);
      } else if (event.data instanceof ArrayBuffer) {
        state.voiceSentence?.chunks.push(event.data);
      } else if (event.data instanceof Blob) {
        void event.data.arrayBuffer().then((chunk) => state.voiceSentence?.chunks.push(chunk));
      }
    });
    socket.addEventListener("error", () => {
      if (!settled) {
        settled = true;
        reject(new Error("语音连接失败"));
      }
    });
    socket.addEventListener("close", (event) => {
      if (!settled) {
        settled = true;
        reject(new Error(`语音连接已关闭 (${event.code})`));
      }
      if (state.voiceSocket === socket) {
        state.voiceReady = false;
        state.voiceSocket = null;
        state.voiceSocketKey = "";
      }
    });
  });
  state.voiceConnectPromise = connecting;
  state.voiceConnectKey = key;
  return connecting.finally(() => {
    if (state.voiceConnectPromise === connecting) {
      state.voiceConnectPromise = null;
      state.voiceConnectKey = "";
    }
  });
}

function handleVoiceEvent(frame) {
  if (frame.type === "voice.ready") {
    setStatus(frame.tts_configured ? "语音输出已就绪" : "未配置 TTS，回复将仅显示文字", !frame.tts_configured);
  } else if (frame.type === "voice.transcript") {
    setStatus("文字已提交，模型生成中…");
  } else if (frame.type === "turn.accepted") {
    state.pendingSend = false;
    state.activeGeneration = frame.generation_id;
    state.voiceTurn = true;
    el("cancel").hidden = false;
    updateControls();
    void refreshActiveMessages();
  } else if (frame.type === "reply.delta") {
    appendDelta(frame.generation_id, frame.delta || "", `conversation:${state.activeId}`);
  } else if (frame.type === "voice.sentence") {
    state.voiceSentence = {
      mime: frame.mime || "audio/pcm;rate=24000",
      sampleRate: Number(frame.sample_rate || 24000),
      chunks: [],
    };
    setStatus("正在接收并播放语音回复…");
  } else if (frame.type === "voice.sentence.end") {
    if (state.voiceSentence) enqueueVoiceSentence(state.voiceSentence);
    state.voiceSentence = null;
  } else if (frame.type === "reply.committed") {
    removeDraft(frame.generation_id);
    void refreshActiveMessages();
    finishGeneration("语音回复已完成");
  } else if (frame.type === "voice.tts_unavailable") {
    setStatus(`语音合成不可用，已保留文字回复：${frame.reason || "not_configured"}`, true);
  } else if (frame.type === "voice.interrupted" || frame.type === "turn.cancelled") {
    removeDraft(frame.generation_id);
    finishGeneration("语音回复已停止");
  } else if (frame.type === "turn.failed" || frame.type === "voice.error") {
    removeDraft(frame.generation_id);
    finishGeneration(frame.reason_code || frame.reason || "语音回合失败", true);
  }
}

async function refreshActiveMessages() {
  if (!state.activeId) return;
  try {
    const messages = await request(`/api/v1/chat/conversations/${state.activeId}/messages`);
    renderMessages(messages);
    const conversation = state.conversations.find((item) => item.id === state.activeId);
    if (conversation && messages.length) {
      conversation.last_seq = Math.max(conversation.last_seq, messages[messages.length - 1].seq);
      renderConversations();
    }
  } catch (error) {
    setStatus(error.message, true);
  }
}

function enqueueVoiceSentence(sentence) {
  const bytes = joinAudioChunks(sentence.chunks);
  const blob = sentence.mime.startsWith("audio/pcm")
    ? new Blob([pcmToWav(bytes, sentence.sampleRate)], { type: "audio/wav" })
    : new Blob([bytes], { type: sentence.mime.split(";")[0] });
  state.voicePlayback = state.voicePlayback
    .catch(() => undefined)
    .then(() => playAudioBlob(blob))
    .catch((error) => setStatus(`语音播放失败：${error.message}`, true));
}

function joinAudioChunks(chunks) {
  const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(new Uint8Array(chunk), offset);
    offset += chunk.byteLength;
  }
  return joined;
}

function pcmToWav(pcm, sampleRate) {
  const buffer = new ArrayBuffer(44 + pcm.byteLength);
  const view = new DataView(buffer);
  const write = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  write(0, "RIFF");
  view.setUint32(4, 36 + pcm.byteLength, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, "data");
  view.setUint32(40, pcm.byteLength, true);
  new Uint8Array(buffer, 44).set(pcm);
  return buffer;
}

function playAudioBlob(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    const cleanup = () => URL.revokeObjectURL(url);
    audio.addEventListener("ended", () => { cleanup(); resolve(); }, { once: true });
    audio.addEventListener("error", () => { cleanup(); reject(new Error("浏览器无法解码音频")); }, { once: true });
    audio.play().catch((error) => { cleanup(); reject(error); });
  });
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
  closeVoiceSocket();
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
    item.innerHTML = `<div class="bubble"></div><div class="meta">正在生成… · ${escapeHtml(formatMessageTime(new Date()))}</div>`;
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
  state.voiceTurn = false;
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
    const item = document.createElement("div");
    item.className = `conversation-item${conversation.id === state.activeId ? " active" : ""}`;
    const button = document.createElement("button");
    button.className = "conversation";
    button.innerHTML = `${escapeHtml(conversation.title || "新会话")}<small>${conversation.last_seq} 条消息</small>`;
    button.addEventListener("click", () => openConversation(conversation.id));
    const remove = document.createElement("button");
    remove.className = "conversation-delete";
    remove.title = "删除会话及其沉淀记忆";
    remove.textContent = "✕";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteConversation(conversation.id);
    });
    item.appendChild(button);
    item.appendChild(remove);
    root.appendChild(item);
  }
}

async function deleteConversation(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  const label = conversation ? conversation.title || "新会话" : "该会话";
  if (!confirm(`删除「${label}」？消息与由它沉淀的记忆会被一并删除并记入台账，不可恢复。`)) return;
  try {
    await request(`/api/v1/chat/conversations/${id}`, { method: "DELETE" });
    state.conversations = state.conversations.filter((item) => item.id !== id);
    if (state.activeId === id) {
      state.activeId = null;
      if (state.conversations.length) await openConversation(state.conversations[0].id);
      else renderMessages([]);
    }
    renderConversations();
    setStatus("会话已删除");
  } catch (error) {
    setStatus(error.message, true);
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
  if (state.activeId !== id) closeVoiceSocket();
  state.activeId = id;
  updateControls();
  renderConversations();
  try {
    renderMessages(await request(`/api/v1/chat/conversations/${id}/messages`));
    if (el("text-reply-voice").checked) {
      void ensureVoiceSocket().catch((error) => setStatus(error.message, true));
    }
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
  const detail = meta ? `${meta.endpoint} · ${meta.model} · ${Math.round(meta.latency_ms)}ms · cfg v${meta.config_version}` : message.privacy_level;
  const route = `${detail} · ${formatMessageTime(message.created_at)}`;
  item.innerHTML = `<div class="bubble">${escapeHtml(message.content)}</div><div class="meta">${escapeHtml(route)}</div>`;
  root.appendChild(item);
  if (autoScroll) scrollMessagesToBottom("smooth");
}

async function send(event) {
  event.preventDefault();
  const text = el("text").value.trim();
  if (!state.activeId || !text || state.pendingSend || state.activeGeneration) return;
  state.pendingSend = true;
  el("text").value = "";
  resizeComposer();
  updateControls();
  setStatus("正在发送…");
  if (el("text-reply-voice").checked) {
    try {
      const voiceSocket = await ensureVoiceSocket();
      state.voiceTurn = true;
      voiceSocket.send(JSON.stringify({ type: "text.submit", text }));
      setStatus("模型生成中…");
      updateControls();
    } catch (error) {
      state.pendingSend = false;
      state.voiceTurn = false;
      if (!el("text").value) {
        el("text").value = text;
        resizeComposer();
      }
      updateControls();
      setStatus(error.message, true);
    }
    return;
  }
  if (state.socketReady) {
    state.socket.send(JSON.stringify({
      type: "message.send",
      conversation_id: state.activeId,
      text,
      privacy_level: el("privacy").value,
    }));
    setStatus("模型生成中…");
    updateControls();
    return;
  }
  try {
    const turn = await request(`/api/v1/chat/conversations/${state.activeId}/messages`, {
      method: "POST",
      body: JSON.stringify({ text, privacy_level: el("privacy").value }),
    });
    appendMessage(turn.user_message);
    appendMessage(turn.assistant_message);
    const conversation = state.conversations.find((item) => item.id === state.activeId);
    if (conversation) conversation.last_seq += 2;
    renderConversations();
    setStatus("回复已保存");
  } catch (error) {
    if (!el("text").value) {
      el("text").value = text;
      resizeComposer();
    }
    setStatus(error.message, true);
    await openConversation(state.activeId);
  } finally {
    state.pendingSend = false;
    updateControls();
  }
}

function cancelGeneration() {
  if (state.voiceTurn && state.voiceSocket?.readyState === WebSocket.OPEN) {
    state.voiceSocket.send(JSON.stringify({ type: "interrupt" }));
    return;
  }
  if (!state.socketReady || !state.activeGeneration) return;
  state.socket.send(JSON.stringify({ type: "turn.cancel", generation_id: state.activeGeneration }));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatMessageTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
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
  closeVoiceSocket();
  el("privacy-note").textContent = event.target.value === "L2"
    ? "L2 强制仅用本地模型；本地不可用时会明确失败。"
    : "L1 可按配置使用云模型。";
});
el("text-reply-voice").checked = localStorage.getItem("ariaDebugTextReplyVoice") === "1";
el("text-reply-voice").addEventListener("change", (event) => {
  localStorage.setItem("ariaDebugTextReplyVoice", event.target.checked ? "1" : "0");
  if (event.target.checked && state.activeId) {
    setStatus("正在连接语音输出…");
    void ensureVoiceSocket().catch((error) => setStatus(error.message, true));
  } else if (!event.target.checked) {
    closeVoiceSocket();
    setStatus("已关闭文字回复播报");
  }
});

resizeComposer();
updateControls();
initialize();
