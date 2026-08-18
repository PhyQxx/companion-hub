const api = "/api/v1/admin/memories";
let token = sessionStorage.getItem("ariaAdminToken") || "";
let fallbackUser = "";
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
const toast = (message, error = false) => { const node = $("#toast"); node.textContent = message; node.className = `show${error ? " error" : ""}`; clearTimeout(node.timer); node.timer = setTimeout(() => node.className = "", 3500); };
const fmtTime = (value) => value ? new Date(value).toLocaleString() : "—";

async function request(path, options = {}) {
  const response = await fetch(path.startsWith("http") ? path : `${api}${path}`, { ...options, headers: {"Content-Type":"application/json", "Authorization":`Bearer ${token}`, ...(options.headers || {})} });
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || `HTTP ${response.status}`)); }
  return response.json();
}

async function loadStats() {
  const [active, conflict, archived, ledger] = await Promise.all([
    request("?status=active&limit=200"), request("?status=conflict&limit=200"),
    request("?status=archived&limit=200"), request("../deletion-ledger?limit=200"),
  ]);
  if (active[0]) fallbackUser = active[0].user_id;
  $("#stat-active").textContent = active.length;
  $("#stat-conflict").textContent = conflict.length;
  $("#stat-archived").textContent = archived.length;
  $("#stat-deleted").textContent = ledger.length;
  $("#stat-deleted-note").textContent = ledger.length ? `最近：${fmtTime(ledger[0].created_at)}` : "尚无硬删除";
  renderLedger(ledger);
}

function renderMemories(items) {
  $("#memories").innerHTML = items.map((item) => `<tr>
    <td><strong>#${item.id}</strong>${item.superseded_by ? `<small>→ #${item.superseded_by}</small>` : ""}${item.conflict_with ? `<small>⚠ vs #${item.conflict_with}</small>` : ""}</td>
    <td><span class="type-chip">${item.type}</span>${item.pin ? "<small>置顶</small>" : ""}</td>
    <td class="content">${escapeHtml(item.content)}${item.summary ? `<small>${escapeHtml(item.summary)}</small>` : ""}</td>
    <td><span class="status ${item.status}">${item.status}</span><small>${item.privacy_level}${item.extractor_version ? ` · ${item.extractor_version}` : ""}</small></td>
    <td>${item.importance.toFixed(2)}</td>
    <td>${item.access_count}</td>
    <td><small>${fmtTime(item.updated_at)}</small></td>
    <td><div class="table-actions">
      <button class="secondary" data-detail="${item.id}">溯源</button>
      ${item.status === "active" || item.status === "conflict" ? `<button class="secondary" data-edit="${item.id}">编辑</button>` : ""}
      ${item.status === "active" ? `<button class="secondary" data-archive="${item.id}">归档</button>` : ""}
      ${item.status === "conflict" ? `<button class="primary" data-adopt="${item.id}">采纳</button><button class="secondary" data-keep="${item.id}">保留旧值</button>` : ""}
      <button class="danger" data-delete="${item.id}">删除</button>
    </div></td></tr>`).join("");
  bindRowActions();
}

function renderLedger(entries) {
  $("#ledger").innerHTML = entries.length ? entries.map((item) => `<tr>
    <td><strong>#${item.id}</strong></td>
    <td>${item.entity_kind} #${escapeHtml(item.entity_id)}</td>
    <td>${item.deleted_ids.map((id) => `#${id}`).join(" ")}</td>
    <td>${escapeHtml(item.requested_by)}</td>
    <td>${escapeHtml(item.reason || "—")}</td>
    <td><small>${fmtTime(item.created_at)}</small></td></tr>`).join("") : `<tr><td colspan="6" style="color:var(--muted)">暂无删除记录</td></tr>`;
}

function bindRowActions() {
  document.querySelectorAll("[data-detail]").forEach((b) => b.addEventListener("click", () => showDetail(b.dataset.detail)));
  document.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => openEdit(b.dataset.edit)));
  document.querySelectorAll("[data-archive]").forEach((b) => b.addEventListener("click", () => archiveMemory(b.dataset.archive)));
  document.querySelectorAll("[data-adopt]").forEach((b) => b.addEventListener("click", () => resolveConflict(b.dataset.adopt, "adopt")));
  document.querySelectorAll("[data-keep]").forEach((b) => b.addEventListener("click", () => resolveConflict(b.dataset.keep, "keep")));
  document.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => deleteMemory(b.dataset.delete)));
}

async function loadMemories() {
  const params = new URLSearchParams();
  if ($("#filter-type").value) params.set("type", $("#filter-type").value);
  if ($("#filter-status").value) params.set("status", $("#filter-status").value);
  if ($("#filter-importance").value) params.set("min_importance", $("#filter-importance").value);
  params.set("limit", $("#filter-limit").value);
  renderMemories(await request(`?${params}`));
}

async function showDetail(id) {
  const dialog = $("#detail-dialog");
  $("#detail-body").innerHTML = '<p class="hint">载入中…</p>';
  dialog.showModal();
  try {
    const item = await request(`/${id}`);
    const sourceRows = item.sources.map((source) => `<div><code>${source.source_kind}:${escapeHtml(source.source_id)}</code>${source.excerpt_hash ? ` <small>${source.excerpt_hash.slice(0, 19)}…</small>` : ""}</div>`).join("") || '<p class="hint">无来源记录</p>';
    const lineageRows = item.lineage.map((entry) => `<div>#${entry.id} <span class="status ${entry.status}">${entry.status}</span> ${escapeHtml(entry.content)}</div>`).join("");
    $("#detail-body").innerHTML = `<dl class="detail-grid">
      <dt>记忆</dt><dd><strong>#${item.id}</strong>（${item.type} · ${item.privacy_level}）</dd>
      <dt>内容</dt><dd>${escapeHtml(item.content)}</dd>
      <dt>摘要</dt><dd>${escapeHtml(item.summary || "—")}</dd>
      <dt>重要性 / 置顶</dt><dd>${item.importance.toFixed(2)} / ${item.pin ? "是" : "否"}</dd>
      <dt>状态</dt><dd><span class="status ${item.status}">${item.status}</span>${item.superseded_by ? ` → #${item.superseded_by}` : ""}${item.supersede_reason ? `（${escapeHtml(item.supersede_reason)}）` : ""}</dd>
      <dt>有效期</dt><dd>${fmtTime(item.valid_from)} ～ ${item.valid_to ? fmtTime(item.valid_to) : "永久"}</dd>
      <dt>提取器</dt><dd>${escapeHtml(item.extractor_version || "—")} · 置信度 ${item.confidence ?? "—"}</dd>
      <dt>创建</dt><dd>${fmtTime(item.created_at)} · ${escapeHtml(item.created_by)}</dd>
      <dt>访问</dt><dd>${item.access_count} 次 · 最近 ${fmtTime(item.last_accessed_at)}</dd>
      <dt>嵌入</dt><dd>${escapeHtml(item.embedding_model || "—")} ${item.embedding_dimension ?? ""}d · ${escapeHtml(item.embedding_version || "—")}</dd>
    </dl>
    <div class="detail-section"><h4>来源（${item.sources.length}）</h4>${sourceRows}</div>
    <div class="detail-section"><h4>版本链（${item.lineage.length}）</h4>${lineageRows}</div>`;
  } catch (error) { $("#detail-body").innerHTML = `<p class="hint">${escapeHtml(error.message)}</p>`; }
}

async function openEdit(id) {
  try {
    const item = await request(`/${id}`);
    $("#edit-id").textContent = `#${id}`;
    $("#edit-content").value = item.content;
    $("#edit-summary").value = item.summary || "";
    $("#edit-importance").value = item.importance;
    $("#edit-pin").checked = item.pin;
    $("#edit-reason").value = "";
    $("#edit-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function submitEdit(event) {
  event.preventDefault();
  const id = $("#edit-id").textContent.replace("#", "");
  const body = { reason: $("#edit-reason").value.trim(), content: $("#edit-content").value.trim() };
  const summary = $("#edit-summary").value.trim();
  if (summary) body.summary = summary;
  if ($("#edit-importance").value) body.importance = Number($("#edit-importance").value);
  body.pin = $("#edit-pin").checked;
  try {
    const updated = await request(`/${id}`, { method: "PATCH", body: JSON.stringify(body) });
    toast(`已生成替代版本 #${updated.id}（原 #${id} → superseded）`);
    await refresh();
  } catch (error) { toast(error.message, true); }
  $("#edit-dialog").close();
}

async function submitAdd(event) {
  event.preventDefault();
  const body = {
    user_id: $("#add-user").value.trim() || fallbackUser,
    type: $("#add-type").value,
    privacy_level: $("#add-privacy").value,
    importance: Number($("#add-importance").value || 0.6),
    content: $("#add-content").value.trim(),
    pin: $("#add-pin").checked,
  };
  try {
    const created = await request("", { method: "POST", body: JSON.stringify(body) });
    toast(`记忆 #${created.id} 已添加`);
    await refresh();
  } catch (error) { toast(error.message, true); }
  $("#add-dialog").close();
}

async function archiveMemory(id) {
  if (!confirm(`归档记忆 #${id}？归档后不再参与检索，可随时恢复。`)) return;
  try { await request(`/${id}/archive`, { method: "POST" }); toast(`记忆 #${id} 已归档`); await refresh(); }
  catch (error) { toast(error.message, true); }
}

async function resolveConflict(id, action) {
  const label = action === "adopt" ? "采纳新记忆（旧版本转入 superseded）" : "保留旧值（新记忆归档）";
  if (!confirm(`冲突裁决 #${id}：${label}？`)) return;
  try { await request(`/${id}/resolve`, { method: "POST", body: JSON.stringify({ action }) }); toast(`冲突 #${id} 已裁决`); await refresh(); }
  catch (error) { toast(error.message, true); }
}

async function deleteMemory(id) {
  const reason = prompt(`硬删除记忆 #${id} 及其整个版本链。此操作不可撤销，将记入删除台账。\n请输入删除原因：`);
  if (reason === null) return;
  try { const receipt = await request(`/${id}?reason=${encodeURIComponent(reason)}`, { method: "DELETE" }); toast(`已删除 ${receipt.deleted_ids.length} 个版本（台账 #${receipt.ledger_id}）`); await refresh(); }
  catch (error) { toast(error.message, true); }
}

async function runQuery(event) {
  event.preventDefault();
  const user = $("#query-user").value.trim() || fallbackUser;
  if (!user) { toast("没有可用的 user_id，请先创建记忆或手动填写", true); return; }
  try {
    const result = await request("/query", { method: "POST", body: JSON.stringify({ user_id: user, query: $("#query-text").value.trim(), privacy_level: $("#query-privacy").value }) });
    $("#query-result").innerHTML = result.hits.length
      ? `<p class="hint">${result.candidate_count} 个候选 · 策略 ${result.policy_version} · 命中 ${result.hits.length} 条</p>` + result.hits.map((hit) => `<div class="hit"><span class="score">${hit.final_score.toFixed(3)}</span><span><strong>#${hit.memory.id}</strong> <span class="type-chip">${hit.memory.type}</span> ${escapeHtml(hit.memory.content)} <span class="reasons">vec ${hit.vector_score.toFixed(2)} · lex ${hit.lexical_score.toFixed(2)} · ${hit.reasons.join("+")}</span></span></div>`).join("")
      : '<p class="hint">没有命中。检查过滤条件或隐私等级。</p>';
  } catch (error) { toast(error.message, true); }
}

async function refresh() { await Promise.all([loadStats(), loadMemories()]); }
async function load() {
  try { await refresh(); $("#workspace").hidden = false; $("#auth-panel").hidden = true; }
  catch (error) { $("#workspace").hidden = true; $("#auth-panel").hidden = false; toast(error.message, true); }
}

$("#auth-form").addEventListener("submit", (event) => { event.preventDefault(); token = $("#admin-token").value; sessionStorage.setItem("ariaAdminToken", token); load(); });
$("#reload").addEventListener("click", load);
$("#apply-filters").addEventListener("click", loadMemories);
$("#add-memory").addEventListener("click", () => { $("#add-user").value = fallbackUser; $("#add-dialog").showModal(); });
$("#add-form").addEventListener("submit", submitAdd);
$("#edit-form").addEventListener("submit", submitEdit);
$("#query-form").addEventListener("submit", runQuery);
if (token) load();
