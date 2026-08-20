const api = "/api/v1/admin/personas";
let token = sessionStorage.getItem("ariaAdminToken") || "";
let persona = null;
const $ = (selector) => document.querySelector(selector);
const emotionLabels = {
  neutral: "平静",
  happy: "开心",
  sad: "难过",
  angry: "生气",
  surprised: "惊讶",
  thinking: "思考",
  concerned: "关切",
};
const statusLabels = {
  published: "已发布",
  draft: "草稿",
  superseded: "已废止",
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
const toast = (message, error = false) => { const node = $("#toast"); node.textContent = message; node.className = `show${error ? " error" : ""}`; clearTimeout(node.timer); node.timer = setTimeout(() => node.className = "", 3500); };

async function request(path, options = {}) {
  const response = await fetch(`${api}${path}`, { ...options, headers: {"Content-Type":"application/json", "Authorization":`Bearer ${token}`, ...(options.headers || {})} });
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || `HTTP ${response.status}`)); }
  return response.json();
}

function renderCurrent(current) {
  persona = current.persona;
  $("#current-version").textContent = `v${current.version}`;
  $("#published-time").textContent = new Date(current.published_at).toLocaleString();
  $("#persona-name").textContent = persona.name;
  $("#default-emotion").textContent = emotionLabels[persona.default_emotion] || persona.default_emotion;
  $("#persona-hash").textContent = current.content_hash.slice(0, 12);
  $("#name").value = persona.name;
  $("#identity").value = persona.identity;
  $("#system-prompt").value = persona.system_prompt;
  $("#speaking-style").value = persona.speaking_style;
  $("#relationship").value = persona.relationship;
  $("#boundaries").value = persona.boundaries.join("\n");
  $("#emotion").value = persona.default_emotion;
  $("#voice-profile").value = persona.voice_profile || "";
  $("#expression-map").value = JSON.stringify(persona.expression_map, null, 2);
}

function collectPersona() {
  let expressionMap;
  try { expressionMap = JSON.parse($("#expression-map").value || "{}"); }
  catch { throw new Error("表情映射必须是有效 JSON"); }
  return {
    schema_version: 1,
    name: $("#name").value.trim(),
    identity: $("#identity").value.trim(),
    system_prompt: $("#system-prompt").value.trim(),
    speaking_style: $("#speaking-style").value.trim(),
    relationship: $("#relationship").value.trim(),
    boundaries: $("#boundaries").value.split("\n").map((value) => value.trim()).filter(Boolean),
    default_emotion: $("#emotion").value,
    expression_map: expressionMap,
    voice_profile: $("#voice-profile").value.trim() || null,
  };
}

function renderVersions(versions) {
  $("#versions").innerHTML = versions.map((item) => `<tr><td><strong>v${item.version}</strong>${item.rollback_from_version ? `<small> ↩ v${item.rollback_from_version}</small>` : ""}</td><td><span class="status ${item.status}">${statusLabels[item.status] || item.status}</span></td><td>${escapeHtml(item.created_by)}</td><td>${new Date(item.created_at).toLocaleString()}</td><td><code>${item.content_hash.slice(0, 12)}</code></td><td><div class="table-actions">${item.status === "draft" ? `<button class="primary" data-publish="${item.version}">发布</button>` : `<button class="secondary" data-rollback="${item.version}">回滚到此版</button>`}</div></td></tr>`).join("");
  document.querySelectorAll("[data-publish]").forEach((button) => button.addEventListener("click", () => publish(button.dataset.publish)));
  document.querySelectorAll("[data-rollback]").forEach((button) => button.addEventListener("click", () => rollback(button.dataset.rollback)));
}

async function load() {
  try { const [current, versions] = await Promise.all([request("/current"), request("/versions")]); renderCurrent(current); renderVersions(versions); $("#workspace").hidden = false; $("#auth-panel").hidden = true; }
  catch (error) { $("#workspace").hidden = true; $("#auth-panel").hidden = false; toast(error.message, true); }
}
async function saveDraft() { try { const candidate = collectPersona(); await request("/validate", {method:"POST", body:JSON.stringify(candidate)}); const draft = await request("/versions", {method:"POST", body:JSON.stringify(candidate)}); toast(`草稿 v${draft.version} 已保存，发布后生效`); await load(); } catch (error) { toast(error.message, true); } }
async function publish(version) { if (!confirm(`发布人格草稿 v${version}？`)) return; try { await request(`/versions/${version}/publish`, {method:"POST"}); toast(`人格 v${version} 已发布`); await load(); } catch (error) { toast(error.message, true); } }
async function rollback(version) { if (!confirm(`回滚到人格 v${version}？`)) return; try { const result = await request(`/versions/${version}/rollback`, {method:"POST"}); toast(`已创建回滚版本 v${result.version}`); await load(); } catch (error) { toast(error.message, true); } }

$("#auth-form").addEventListener("submit", (event) => { event.preventDefault(); token = $("#admin-token").value; sessionStorage.setItem("ariaAdminToken", token); load(); });
$("#reload").addEventListener("click", load);
$("#save-draft").addEventListener("click", saveDraft);
if (token) load();
