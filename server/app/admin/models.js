const api = "/api/v1/admin/config";
let token = sessionStorage.getItem("ariaAdminToken") || "";
let config = null;
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
const toast = (message, error = false) => { const node = $("#toast"); node.textContent = message; node.className = `show${error ? " error" : ""}`; clearTimeout(node.timer); node.timer = setTimeout(() => node.className = "", 3500); };
const statusLabels = {
  published: "已发布",
  draft: "草稿",
  superseded: "已废止",
};

async function request(path, options = {}) {
  const response = await fetch(`${api}${path}`, { ...options, headers: {"Content-Type":"application/json", "Authorization": `Bearer ${token}`, ...(options.headers || {})} });
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || `HTTP ${response.status}`); }
  return response.json();
}

function field(label, name, value, type = "text", wide = false) {
  return `<div class="field${wide ? " wide" : ""}"><label>${label}</label><input data-field="${name}" type="${type}" value="${escapeHtml(value)}"></div>`;
}

function renderModels() {
  const root = $("#models"); root.innerHTML = Object.entries(config.models).map(([name, model]) => `
    <article class="model-card" data-model-card>
      <div class="model-head"><div class="model-title"><span class="model-icon">◇</span><div><strong>${escapeHtml(name)}</strong><div><span class="badge ${model.enabled ? "" : "off"}">${model.enabled ? "已启用" : "已停用"}</span></div></div></div></div>
      <div class="fields">
        ${field("端点名称", "name", name)}${field("模型 ID", "model", model.model)}
        ${field("Provider", "provider", model.provider)}${field("Base URL", "base_url", model.base_url, "url")}
        ${field("密钥引用", "secret_ref", model.secret_ref || "", "text", true)}
        ${field("超时 (ms)", "timeout_ms", model.timeout_ms, "number")}${field("重试次数", "max_retries", model.max_retries, "number")}
        ${field("上下文上限", "max_context_tokens", model.max_context_tokens, "number")}${field("最大隐私级别", "max_privacy_level", model.max_privacy_level)}
        ${field("输入单价 / 百万 token", "input_cost_per_million", model.input_cost_per_million, "number")}${field("输出单价 / 百万 token", "output_cost_per_million", model.output_cost_per_million, "number")}
        <label class="check"><input data-field="enabled" type="checkbox" ${model.enabled ? "checked" : ""}>启用端点</label>
        <label class="check"><input data-field="runs_local" type="checkbox" ${model.runs_local ? "checked" : ""}>本地运行</label>
        <label class="check"><input data-field="supports_json_mode" type="checkbox" ${model.supports_json_mode ? "checked" : ""}>原生 JSON 模式</label>
        <label class="check"><input data-field="supports_tool_calling" type="checkbox" ${model.supports_tool_calling ? "checked" : ""}>Function Calling</label>
      </div><div class="card-footer"><button class="danger" data-remove-model>删除端点</button></div>
    </article>`).join("");
  root.querySelectorAll("[data-remove-model]").forEach(button => button.addEventListener("click", () => { button.closest("[data-model-card]").remove(); renderRoutes(); }));
}

function currentModels() { return [...document.querySelectorAll("[data-model-card]")].map(card => ({name:card.querySelector("[data-field=name]").value.trim(), enabled:card.querySelector("[data-field=enabled]").checked, local:card.querySelector("[data-field=runs_local]").checked})).filter(model => model.name); }
function renderRoutes() {
  const models = currentModels();
  const eligibleNames = route => models.filter(model => model.enabled && (route !== "private" || model.local)).map(model => model.name);
  const options = (names, selected) => names.map(name => `<option ${name === selected ? "selected" : ""}>${escapeHtml(name)}</option>`).join("");
  $("#routes").innerHTML = ["dialogue", "utility", "private"].map(route => { const names = eligibleNames(route); const policy = config.routes[route] || {primary:names[0] || "", fallbacks:[], timeout_ms:null}; return `<article class="route-card ${route}"><h3>${route}</h3><div class="fields"><div class="field wide"><label>主模型</label><select data-route="${route}" data-route-field="primary">${options(names, policy.primary)}</select></div>${field("降级链（逗号分隔）", `fallbacks-${route}`, (policy.fallbacks || []).join(", "), "text", true)}${field("路由超时 (ms)", `timeout-${route}`, policy.timeout_ms || "", "number", true)}</div></article>`; }).join("");
}

function collectConfig() {
  const models = {};
  document.querySelectorAll("[data-model-card]").forEach(card => {
    const get = (name) => card.querySelector(`[data-field=${name}]`);
    const name = get("name").value.trim();
    models[name] = {enabled:get("enabled").checked, provider:get("provider").value.trim(), model:get("model").value.trim(), supports_json_mode:get("supports_json_mode").checked, supports_tool_calling:get("supports_tool_calling").checked, base_url:get("base_url").value.trim(), secret_ref:get("secret_ref").value.trim() || null, runs_local:get("runs_local").checked, max_privacy_level:get("max_privacy_level").value.trim(), timeout_ms:Number(get("timeout_ms").value), max_retries:Number(get("max_retries").value), max_context_tokens:Number(get("max_context_tokens").value), input_cost_per_million:Number(get("input_cost_per_million").value), output_cost_per_million:Number(get("output_cost_per_million").value)};
  });
  const routes = {};
  ["dialogue", "utility", "private"].forEach(route => { const timeout = document.querySelector(`[data-field=timeout-${route}]`).value; routes[route] = {primary:document.querySelector(`[data-route=${route}]`).value, fallbacks:document.querySelector(`[data-field=fallbacks-${route}]`).value.split(",").map(v => v.trim()).filter(Boolean), timeout_ms:timeout ? Number(timeout) : null}; });
  return {schema_version:1, models, routes, capability_models:config.capability_models, voice:config.voice, tools:config.tools, observability:config.observability};
}

function renderCurrent(current) {
  config = current.config; $("#current-version").textContent = `v${current.version}`; $("#published-time").textContent = new Date(current.published_at).toLocaleString(); $("#enabled-count").textContent = Object.values(config.models).filter(model => model.enabled).length; $("#config-hash").textContent = current.content_hash.slice(0, 12); renderModels(); renderRoutes();
}
async function load() {
  try { const [current, versions] = await Promise.all([request("/current"), request("/versions")]); renderCurrent(current); renderVersions(versions); $("#workspace").hidden = false; $("#auth-panel").hidden = true; }
  catch (error) { $("#workspace").hidden = true; $("#auth-panel").hidden = false; toast(error.message, true); }
}
function renderVersions(versions) {
  $("#versions").innerHTML = versions.map(item => `<tr><td><strong>v${item.version}</strong>${item.rollback_from_version ? `<small> ↩ v${item.rollback_from_version}</small>` : ""}</td><td><span class="status ${item.status}">${statusLabels[item.status] || item.status}</span></td><td>${escapeHtml(item.created_by)}</td><td>${new Date(item.created_at).toLocaleString()}</td><td><code>${item.content_hash.slice(0, 12)}</code></td><td><div class="table-actions">${item.status === "draft" ? `<button class="primary" data-publish="${item.version}">发布</button>` : `<button class="secondary" data-rollback="${item.version}">回滚到此版</button>`}</div></td></tr>`).join("");
  document.querySelectorAll("[data-publish]").forEach(button => button.addEventListener("click", () => publish(button.dataset.publish)));
  document.querySelectorAll("[data-rollback]").forEach(button => button.addEventListener("click", () => rollback(button.dataset.rollback)));
}
async function saveDraft() { try { const candidate = collectConfig(); await request("/validate", {method:"POST", body:JSON.stringify(candidate)}); const draft = await request("/versions", {method:"POST", body:JSON.stringify(candidate)}); toast(`草稿 v${draft.version} 已保存，尚未发布`); await load(); } catch (error) { toast(error.message, true); } }
async function publish(version) { if (!confirm(`发布草稿 v${version}？当前配置会被替换。`)) return; try { await request(`/versions/${version}/publish`, {method:"POST"}); toast(`v${version} 已发布`); await load(); } catch (error) { toast(error.message, true); } }
async function rollback(version) { if (!confirm(`回滚到 v${version}？系统会创建一个新的已发布版本。`)) return; try { const result = await request(`/versions/${version}/rollback`, {method:"POST"}); toast(`已创建回滚版本 v${result.version}`); await load(); } catch (error) { toast(error.message, true); } }

$("#auth-form").addEventListener("submit", event => { event.preventDefault(); token = $("#admin-token").value; sessionStorage.setItem("ariaAdminToken", token); load(); });
$("#reload").addEventListener("click", load); $("#save-draft").addEventListener("click", saveDraft);
$("#add-model").addEventListener("click", () => { config.models[`new_endpoint_${Date.now()}`] = {enabled:false,provider:"openai_compatible",model:"model-id",supports_json_mode:false,supports_tool_calling:false,base_url:"https://example.com/v1",secret_ref:"env:MODEL_API_KEY",runs_local:false,max_privacy_level:"L1",timeout_ms:12000,max_retries:0,max_context_tokens:32768,input_cost_per_million:0,output_cost_per_million:0}; renderModels(); renderRoutes(); });
document.addEventListener("change", event => { if (event.target.matches("[data-field=name],[data-field=enabled],[data-field=runs_local]")) renderRoutes(); });
if (token) load();
