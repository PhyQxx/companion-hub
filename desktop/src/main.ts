import { disable, enable, isEnabled } from "@tauri-apps/plugin-autostart";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { emit, listen } from "@tauri-apps/api/event";
import {
  DESKTOP_BASE_CAPABILITIES,
  DESKTOP_NOTIFICATION_CAPABILITY,
  DeviceConnection,
  forgetAccessToken,
  loadAccessToken,
  pairDevice,
  createScreenCaptureGrant,
  consumeScreenCaptureGrant,
  screenCaptureEnvironment,
  screenCaptureGrantActive,
  screenCapturePermission,
  type ConnectionState,
  type ScreenCaptureGrant,
  type StoredClientConfig,
} from "./client";
import {
  DESKTOP_CONFIG_KEY,
  PET_CLICK_THROUGH_KEY,
  PET_VISIBLE_KEY,
} from "./pet-state";
import "./style.css";

const PRIVACY_PAUSE_KEY = "ariaDesktopPrivacyPaused";
const app = document.querySelector<HTMLElement>("#app");
if (!app) throw new Error("missing app root");

app.innerHTML = `
  <section class="window">
    <header><div class="brand"><span>A</span><div><strong>Aria Desktop</strong><small>受控设备客户端</small></div></div><span id="state" class="state unpaired">未配对</span></header>
    <div class="hero"><div><p class="eyebrow">M3A · DEVICE CLIENT</p><h1>让 Hub 安全地找到这台电脑</h1><p>未锁屏且隐私暂停关闭时开放 <code>notification.show</code>；屏幕授权后才会临时开放 <code>screen.capture</code>。</p></div><div class="pulse"><i></i><span id="state-detail">等待配置</span></div></div>
    <form id="pair-form" class="panel form-grid">
      <div class="panel-head"><div><h2>设备配对</h2><p>配对码从 Aria 管理后台生成；截图还需授权 <code>screen.capture</code>。</p></div></div>
      <label>Hub 地址<input id="hub-url" type="url" required placeholder="http://127.0.0.1:8000" /></label>
      <label>一次性配对码<input id="pairing-code" type="password" required autocomplete="one-time-code" placeholder="aria_pair_…" /></label>
      <label>设备名称<input id="device-name" required maxlength="160" value="我的 Mac" /></label>
      <label>设备别名<input id="device-alias" maxlength="80" value="我的电脑" /></label>
      <div class="form-actions"><button type="submit" class="primary">配对并连接</button><button id="forget" type="button" class="danger">忘记此设备</button></div>
    </form>
    <section class="panel status-panel">
      <div class="panel-head"><div><h2>运行状态</h2><p id="identity">尚未保存设备身份</p></div><button id="toggle-connection" type="button">连接</button></div>
      <dl><dt>声明能力</dt><dd><code id="capabilities">device.ping</code></dd><dt>屏幕录制</dt><dd><button id="screen-permission" type="button">检查权限</button> <small id="screen-permission-state">尚未检查</small></dd><dt>会话状态</dt><dd><small id="screen-lock-state">正在检查锁屏状态</small></dd><dt>临时授权</dt><dd><button id="grant-screen-capture" type="button">允许下一次截图</button> <small id="screen-grant-state">未授权</small></dd><dt>桌宠</dt><dd class="inline-controls"><button id="toggle-pet" type="button">显示桌宠</button><label class="switch-label"><span>鼠标穿透</span><span class="switch"><input id="pet-click-through" type="checkbox" /><span></span></span></label></dd><dt>隐私暂停</dt><dd><label class="switch"><input id="privacy-pause" type="checkbox" /><span></span></label></dd><dt>开机启动</dt><dd><label class="switch"><input id="autostart" type="checkbox" /><span></span></label></dd><dt>凭据位置</dt><dd>系统安全凭据库（不写入 localStorage）</dd></dl>
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
const privacyPause = document.querySelector<HTMLInputElement>("#privacy-pause")!;
const permissionButton = document.querySelector<HTMLButtonElement>("#screen-permission")!;
const permissionState = document.querySelector<HTMLElement>("#screen-permission-state")!;
const lockState = document.querySelector<HTMLElement>("#screen-lock-state")!;
const grantButton = document.querySelector<HTMLButtonElement>("#grant-screen-capture")!;
const grantState = document.querySelector<HTMLElement>("#screen-grant-state")!;
const capabilitiesLabel = document.querySelector<HTMLElement>("#capabilities")!;
const togglePet = document.querySelector<HTMLButtonElement>("#toggle-pet")!;
const petClickThrough = document.querySelector<HTMLInputElement>("#pet-click-through")!;

let config = loadConfig();
let connection: DeviceConnection | null = null;
let currentState: ConnectionState = config ? "offline" : "unpaired";
let permissionGranted = false;
let screenLocked = true;
let screenGrant: ScreenCaptureGrant | null = null;
privacyPause.checked = localStorage.getItem(PRIVACY_PAUSE_KEY) === "true";
petClickThrough.checked = localStorage.getItem(PET_CLICK_THROUGH_KEY) === "true";

async function refreshPetState() {
  const pet = await WebviewWindow.getByLabel("pet");
  const visible = pet ? await pet.isVisible() : false;
  localStorage.setItem(PET_VISIBLE_KEY, String(visible));
  togglePet.textContent = visible ? "隐藏桌宠" : "显示桌宠";
}

async function restorePetState() {
  const pet = await WebviewWindow.getByLabel("pet");
  if (!pet) return;
  await pet.setIgnoreCursorEvents(petClickThrough.checked);
  if (localStorage.getItem(PET_VISIBLE_KEY) === "true") await pet.show();
  await refreshPetState();
}

togglePet.addEventListener("click", async () => {
  const pet = await WebviewWindow.getByLabel("pet");
  if (!pet) return addEvent("桌宠窗口不可用，请重启客户端");
  if (await pet.isVisible()) {
    await pet.hide();
    addEvent("已隐藏桌宠");
  } else {
    await pet.show();
    await emit("pet-ensure-visible");
    addEvent("已显示桌宠");
  }
  await refreshPetState();
});

petClickThrough.addEventListener("change", async () => {
  const pet = await WebviewWindow.getByLabel("pet");
  localStorage.setItem(PET_CLICK_THROUGH_KEY, String(petClickThrough.checked));
  await pet?.setIgnoreCursorEvents(petClickThrough.checked);
  addEvent(petClickThrough.checked ? "桌宠已开启鼠标穿透" : "桌宠已恢复鼠标交互");
});

void listen<boolean>("pet-visibility-changed", ({ payload }) => {
  localStorage.setItem(PET_VISIBLE_KEY, String(payload));
  togglePet.textContent = payload ? "隐藏桌宠" : "显示桌宠";
});
void listen("pet-interaction-restored", () => {
  petClickThrough.checked = false;
  localStorage.setItem(PET_CLICK_THROUGH_KEY, "false");
  addEvent("已从托盘恢复桌宠交互");
});
void listen<boolean>("pet-click-through-changed", ({ payload }) => {
  petClickThrough.checked = payload;
  localStorage.setItem(PET_CLICK_THROUGH_KEY, String(payload));
});
void listen<{ text: string; privacyLevel: "L0" | "L1" | "L2"; speak: boolean }>(
  "pet-message-submit",
  ({ payload }) => {
    const requestId = connection?.sendPetMessage(
      payload.text,
      payload.privacyLevel,
      payload.speak,
    );
    if (!requestId) {
      void emit("pet-message-state", {
        requestId: "local",
        status: "failed",
        reasonCode: "device_offline",
      });
    }
  },
);

function activeCapabilities(): string[] {
  const capabilities: string[] = [...DESKTOP_BASE_CAPABILITIES];
  if (!screenLocked && !privacyPause.checked) capabilities.push(DESKTOP_NOTIFICATION_CAPABILITY);
  if (
    permissionGranted && !screenLocked && !privacyPause.checked &&
    screenCaptureGrantActive(screenGrant)
  ) capabilities.push("screen.capture");
  // 屏幕感知：跟随 Hub 配置开关运行；这里只把关 TCC/锁屏/隐私暂停三道设备端闸门
  if (permissionGranted && !screenLocked && !privacyPause.checked) {
    capabilities.push("screen.monitor");
  }
  return capabilities;
}

function renderCapabilities() {
  const capabilities = activeCapabilities();
  capabilitiesLabel.textContent = capabilities.join("、");
  connection?.setCapabilities(capabilities);
  grantButton.disabled = !permissionGranted || screenLocked || privacyPause.checked;
  grantState.textContent = screenCaptureGrantActive(screenGrant)
    ? `已授权下一次 · ${Math.max(1, Math.ceil((screenGrant!.expiresAt - Date.now()) / 60_000))} 分钟内有效`
    : "未授权";
}

function authorizeScreenCapture(): "allowed" | "screen_locked" | "screen_capture_not_granted" {
  if (screenLocked) return "screen_locked";
  if (!screenCaptureGrantActive(screenGrant)) return "screen_capture_not_granted";
  screenGrant = consumeScreenCaptureGrant(screenGrant);
  renderCapabilities();
  addEvent("下一次截图临时授权已消费");
  return "allowed";
}

async function refreshScreenEnvironment() {
  try {
    const environment = await screenCaptureEnvironment();
    permissionGranted = environment.granted;
    permissionState.textContent = environment.supported
      ? environment.granted
        ? "已授权"
        : "未授权"
      : "当前平台暂不支持";
    const wasLocked = screenLocked;
    screenLocked = environment.locked;
    lockState.textContent = environment.supported
      ? screenLocked
        ? "已锁屏 · 截图已拒绝"
        : "未锁屏"
      : "当前平台暂不支持";
    if (screenLocked && !wasLocked) {
      screenGrant = null;
      addEvent("检测到锁屏，临时截图授权已撤销");
    }
    renderCapabilities();
  } catch {
    screenLocked = true;
    lockState.textContent = "状态检查失败 · 按锁屏处理";
    renderCapabilities();
  }
}

function loadConfig(): StoredClientConfig | null {
  try {
    const raw = localStorage.getItem(DESKTOP_CONFIG_KEY);
    return raw ? (JSON.parse(raw) as StoredClientConfig) : null;
  } catch {
    return null;
  }
}

function saveConfig(value: StoredClientConfig | null) {
  config = value;
  if (value) localStorage.setItem(DESKTOP_CONFIG_KEY, JSON.stringify(value));
  else localStorage.removeItem(DESKTOP_CONFIG_KEY);
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
  if (privacyPause.checked) return setState("offline", "隐私暂停已开启");
  connection = new DeviceConnection(config, token, activeCapabilities(), {
    onState: setState,
    onEvent: addEvent,
    authorizeScreenCapture,
    onAvatarControl: (control) => void emit("avatar-control", control),
    onPetMessageState: (messageState) => void emit("pet-message-state", messageState),
    onPetAudio: (audioFrame) => void emit("pet-audio", audioFrame),
  });
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

privacyPause.addEventListener("change", () => {
  localStorage.setItem(PRIVACY_PAUSE_KEY, String(privacyPause.checked));
  renderCapabilities();
  if (privacyPause.checked) {
    connection?.stop();
    addEvent("隐私暂停已开启，设备停止接收命令");
  } else {
    addEvent("隐私暂停已关闭");
    void startConnection();
  }
});

permissionButton.addEventListener("click", async () => {
  permissionButton.disabled = true;
  try {
    const status = await screenCapturePermission(true);
    permissionGranted = status.granted;
    permissionState.textContent = status.supported
      ? status.granted
        ? "已授权"
        : "未授权；请在系统设置中允许后重连"
      : "当前平台暂不支持";
    renderCapabilities();
    if (status.granted && !privacyPause.checked && config) await startConnection();
  } catch {
    permissionState.textContent = "权限检查失败";
  } finally {
    permissionButton.disabled = false;
  }
});

grantButton.addEventListener("click", () => {
  screenGrant = createScreenCaptureGrant();
  renderCapabilities();
  addEvent("已授权 5 分钟内的下一次截图；授权不会持久化");
});

document.querySelector("#clear-log")!.addEventListener("click", () => (events.innerHTML = ""));

renderIdentity();
renderCapabilities();
void restorePetState();
void isEnabled().then((enabled) => (autostart.checked = enabled));
void refreshScreenEnvironment().then(() => {
  if (config && !privacyPause.checked) void startConnection();
});
window.setInterval(() => void refreshScreenEnvironment(), 5_000);
