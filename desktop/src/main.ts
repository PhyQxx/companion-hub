import { disable, enable, isEnabled } from "@tauri-apps/plugin-autostart";
import {
  DESKTOP_CAPABILITIES,
  DeviceConnection,
  forgetAccessToken,
  loadAccessToken,
  pairDevice,
  type ConnectionState,
  type StoredClientConfig,
} from "./client";
import "./style.css";

const CONFIG_KEY = "ariaDesktopClientConfig";
const app = document.querySelector<HTMLElement>("#app");
if (!app) throw new Error("missing app root");

app.innerHTML = `
  <section class="window">
    <header><div class="brand"><span>A</span><div><strong>Aria Desktop</strong><small>受控设备客户端</small></div></div><span id="state" class="state unpaired">未配对</span></header>
    <div class="hero"><div><p class="eyebrow">M3A · DEVICE CLIENT</p><h1>让 Hub 安全地找到这台电脑</h1><p>当前最小壳只声明 <code>device.ping</code>。屏幕和网页能力将在系统权限与临时资产销毁闭环完成后再启用。</p></div><div class="pulse"><i></i><span id="state-detail">等待配置</span></div></div>
    <form id="pair-form" class="panel form-grid">
      <div class="panel-head"><div><h2>设备配对</h2><p>配对码从 Aria 管理后台生成，并需授权 <code>device.ping</code>。</p></div></div>
      <label>Hub 地址<input id="hub-url" type="url" required placeholder="http://127.0.0.1:8000" /></label>
      <label>一次性配对码<input id="pairing-code" type="password" required autocomplete="one-time-code" placeholder="aria_pair_…" /></label>
      <label>设备名称<input id="device-name" required maxlength="160" value="我的 Mac" /></label>
      <label>设备别名<input id="device-alias" maxlength="80" value="我的电脑" /></label>
      <div class="form-actions"><button type="submit" class="primary">配对并连接</button><button id="forget" type="button" class="danger">忘记此设备</button></div>
    </form>
    <section class="panel status-panel">
      <div class="panel-head"><div><h2>运行状态</h2><p id="identity">尚未保存设备身份</p></div><button id="toggle-connection" type="button">连接</button></div>
      <dl><dt>声明能力</dt><dd><code>${DESKTOP_CAPABILITIES.join("、")}</code></dd><dt>开机启动</dt><dd><label class="switch"><input id="autostart" type="checkbox" /><span></span></label></dd><dt>凭据位置</dt><dd>系统安全凭据库（不写入 localStorage）</dd></dl>
    </section>
    <section class="panel log-panel"><div class="panel-head"><div><h2>最近事件</h2><p>只记录协议状态，不记录令牌或命令参数。</p></div><button id="clear-log" type="button">清空</button></div><ol id="events"></ol></section>
  </section>
`;

const pairForm = document.querySelector<HTMLFormElement>("#pair-form")!;
const hubUrl = document.querySelector<HTMLInputElement>("#hub-url")!;
const pairingCode = document.querySelector<HTMLInputElement>("#pairing-code")!;
const deviceName = document.querySelector<HTMLInputElement>("#device-name")!;
const deviceAlias = document.querySelector<HTMLInputElement>("#device-alias")!;
const stateBadge = document.querySelector<HTMLElement>("#state")!;
const stateDetail = document.querySelector<HTMLElement>("#state-detail")!;
const identity = document.querySelector<HTMLElement>("#identity")!;
const events = document.querySelector<HTMLOListElement>("#events")!;
const toggleConnection = document.querySelector<HTMLButtonElement>("#toggle-connection")!;
const autostart = document.querySelector<HTMLInputElement>("#autostart")!;

let config = loadConfig();
let connection: DeviceConnection | null = null;
let currentState: ConnectionState = config ? "offline" : "unpaired";

function loadConfig(): StoredClientConfig | null {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    return raw ? (JSON.parse(raw) as StoredClientConfig) : null;
  } catch {
    return null;
  }
}

function saveConfig(value: StoredClientConfig | null) {
  config = value;
  if (value) localStorage.setItem(CONFIG_KEY, JSON.stringify(value));
  else localStorage.removeItem(CONFIG_KEY);
  renderIdentity();
}

function renderIdentity() {
  if (!config) {
    identity.textContent = "尚未保存设备身份";
    return;
  }
  hubUrl.value = config.hub_url;
  deviceName.value = config.name;
  deviceAlias.value = config.alias ?? "";
  identity.textContent = `${config.alias ?? config.name} · ${config.device_id.slice(0, 8)} · ${config.hub_url}`;
}

function setState(state: ConnectionState, detail: string) {
  currentState = state;
  stateBadge.className = `state ${state}`;
  stateBadge.textContent = { unpaired: "未配对", connecting: "连接中", online: "在线", offline: "离线", error: "异常" }[state];
  stateDetail.textContent = detail;
  toggleConnection.textContent = state === "online" || state === "connecting" ? "暂停" : "连接";
}

function addEvent(message: string) {
  const item = document.createElement("li");
  item.innerHTML = `<time>${new Date().toLocaleTimeString()}</time><span></span>`;
  item.querySelector("span")!.textContent = message;
  events.prepend(item);
  while (events.children.length > 30) events.lastElementChild?.remove();
}

async function startConnection() {
  if (!config) return setState("unpaired", "请先完成配对");
  const token = await loadAccessToken();
  if (!token) return setState("unpaired", "系统凭据库中没有设备凭据，请重新配对");
  connection?.stop();
  connection = new DeviceConnection(config, token, { onState: setState, onEvent: addEvent });
  connection.connect();
}

pairForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = pairForm.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  submit.disabled = true;
  try {
    const result = await pairDevice({
      hubUrl: hubUrl.value.trim(),
      pairingCode: pairingCode.value.trim(),
      name: deviceName.value.trim(),
      alias: deviceAlias.value.trim() || null,
    });
    saveConfig(result);
    pairingCode.value = "";
    addEvent(`设备 ${result.device_id.slice(0, 8)} 配对成功`);
    await startConnection();
  } catch (error) {
    setState("error", error instanceof Error ? error.message : String(error));
  } finally {
    submit.disabled = false;
  }
});

toggleConnection.addEventListener("click", () => {
  if (currentState === "online" || currentState === "connecting") connection?.stop();
  else void startConnection();
});

document.querySelector("#forget")!.addEventListener("click", async () => {
  connection?.stop();
  connection = null;
  await forgetAccessToken();
  saveConfig(null);
  setState("unpaired", "本机设备凭据已清除；Hub 侧记录仍需在管理后台撤销");
  addEvent("已清除系统凭据库中的设备令牌");
});

autostart.addEventListener("change", async () => {
  try {
    if (autostart.checked) await enable();
    else await disable();
    addEvent(autostart.checked ? "已启用开机启动" : "已关闭开机启动");
  } catch (error) {
    autostart.checked = !autostart.checked;
    addEvent(`开机启动设置失败：${String(error)}`);
  }
});

document.querySelector("#clear-log")!.addEventListener("click", () => (events.innerHTML = ""));

renderIdentity();
void isEnabled().then((enabled) => (autostart.checked = enabled));
if (config) void startConnection();
