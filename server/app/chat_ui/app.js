const state = { token: sessionStorage.getItem("ariaAdminToken") || "", conversations: [], activeId: null };
const el = (id) => document.getElementById(id);

el("token").value = state.token;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${state.token}`,
      ...(options.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail?.reason_code || body.detail || `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return body;
}

function setStatus(text, error = false) {
  el("status").textContent = text;
  el("status").className = error ? "error" : "muted";
}

async function connect() {
  state.token = el("token").value.trim();
  sessionStorage.setItem("ariaAdminToken", state.token);
  try {
    state.conversations = await api("/api/v1/chat/conversations");
    setStatus(`已连接 · ${state.conversations.length} 个会话`);
    renderConversations();
    if (state.conversations.length) await openConversation(state.conversations[0].id);
  } catch (error) {
    setStatus(error.message, true);
  }
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
  try {
    const conversation = await api("/api/v1/chat/conversations", {
      method: "POST",
      body: JSON.stringify({ display_name: "主人", title: `调试会话 ${state.conversations.length + 1}` }),
    });
    state.conversations.unshift(conversation);
    await openConversation(conversation.id);
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function openConversation(id) {
  state.activeId = id;
  renderConversations();
  try {
    renderMessages(await api(`/api/v1/chat/conversations/${id}/messages`));
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderMessages(messages) {
  const root = el("messages");
  root.innerHTML = messages.length ? "" : '<div class="empty">这个会话还没有消息。</div>';
  for (const message of messages) appendMessage(message);
  root.scrollTop = root.scrollHeight;
}

function appendMessage(message) {
  const root = el("messages");
  root.querySelector(".empty")?.remove();
  const item = document.createElement("article");
  item.className = `message ${message.role}`;
  const meta = message.decision_meta;
  const route = meta ? `${meta.endpoint} · ${meta.model} · ${Math.round(meta.latency_ms)}ms · cfg v${meta.config_version}` : message.privacy_level;
  item.innerHTML = `<div class="bubble">${escapeHtml(message.content)}</div><div class="meta">${escapeHtml(route)}</div>`;
  root.appendChild(item);
}

async function send(event) {
  event.preventDefault();
  const text = el("text").value.trim();
  if (!state.activeId || !text) return;
  const button = el("send");
  button.disabled = true;
  setStatus("模型生成中…");
  try {
    const turn = await api(`/api/v1/chat/conversations/${state.activeId}/messages`, {
      method: "POST",
      body: JSON.stringify({ text, privacy_level: el("privacy").value }),
    });
    el("text").value = "";
    appendMessage(turn.user_message);
    appendMessage(turn.assistant_message);
    const conversation = state.conversations.find((item) => item.id === state.activeId);
    if (conversation) conversation.last_seq += 2;
    renderConversations();
    el("messages").scrollTop = el("messages").scrollHeight;
    setStatus("回复已保存");
  } catch (error) {
    setStatus(error.message, true);
    await openConversation(state.activeId);
  } finally {
    button.disabled = false;
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

el("connect").addEventListener("click", connect);
el("new-conversation").addEventListener("click", createConversation);
el("composer").addEventListener("submit", send);
el("privacy").addEventListener("change", (event) => {
  el("privacy-note").textContent = event.target.value === "L2"
    ? "L2 强制仅用本地模型；本地不可用时会明确失败。"
    : "L1 可按配置使用云模型。";
});

if (state.token) connect();
