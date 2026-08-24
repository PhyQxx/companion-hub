import {
  BROWSER_CAPABILITIES,
  isExecuteCommand,
  sanitizeBrowserDocument,
  verifyFrame,
  websocketUrl,
  type BrowserDocument,
  type ExecuteCommandFrame,
  type SignedFrame,
} from "./core";

interface StoredBridgeConfig {
  hubUrl: string;
  accessToken: string;
  deviceId: string;
  alias: string | null;
}

interface AssetResponse {
  asset_id: string;
  media_type: string;
  bytes: number;
  sha256: string;
}

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let reconnectAttempt = 0;
let connectGeneration = 0;
const cancelledCommands = new Set<string>();

void chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
chrome.runtime.onStartup.addListener(() => void connect());
chrome.runtime.onInstalled.addListener(() => void connect());
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.bridgeConfig) void connect();
});
chrome.runtime.onMessage.addListener((message: unknown) => {
  if (message && typeof message === "object" && "type" in message && message.type === "bridge.reconnect") {
    void connect();
  }
});
void connect();

async function storedConfig(): Promise<StoredBridgeConfig | null> {
  const result = await chrome.storage.local.get("bridgeConfig");
  const value = result.bridgeConfig as Partial<StoredBridgeConfig> | undefined;
  if (!value?.hubUrl || !value.accessToken || !value.deviceId) return null;
  return value as StoredBridgeConfig;
}

async function connect(): Promise<void> {
  const generation = ++connectGeneration;
  clearConnection();
  const config = await storedConfig();
  if (generation !== connectGeneration) return;
  if (config === null) {
    await setStatus("unpaired", "尚未配对");
    return;
  }
  await setStatus("connecting", "正在连接 Hub…");
  const current = new WebSocket(websocketUrl(config.hubUrl));
  socket = current;
  current.addEventListener("open", () => {
    current.send(JSON.stringify({
      type: "device.authenticate",
      access_token: config.accessToken,
      capabilities: BROWSER_CAPABILITIES,
    }));
  });
  current.addEventListener("message", (event) => void handleFrame(current, config, String(event.data)));
  current.addEventListener("close", (event) => {
    if (socket !== current) return;
    socket = null;
    stopHeartbeat();
    void setStatus("offline", `连接已断开（${event.code}）`);
    scheduleReconnect();
  });
  current.addEventListener("error", () => void setStatus("error", "无法连接 Hub"));
}

async function handleFrame(
  current: WebSocket,
  config: StoredBridgeConfig,
  raw: string,
): Promise<void> {
  let frame: SignedFrame;
  try {
    frame = JSON.parse(raw) as SignedFrame;
  } catch {
    return;
  }
  if (!(await verifyFrame(config.accessToken, frame))) {
    await setStatus("error", "收到签名无效的 Hub 消息");
    current.close(4403, "invalid signature");
    return;
  }
  if (frame.type === "device.accepted") {
    reconnectAttempt = 0;
    await setStatus("online", "已连接，等待网页读取请求");
    startHeartbeat(current);
    return;
  }
  if (frame.type === "command.cancel" && typeof frame.command_id === "string") {
    cancelledCommands.add(frame.command_id);
    if (cancelledCommands.size > 200) {
      const oldest = cancelledCommands.values().next().value;
      if (oldest) cancelledCommands.delete(oldest);
    }
    return;
  }
  if (!isExecuteCommand(frame)) return;
  await executeCommand(current, config, frame);
}

async function executeCommand(
  current: WebSocket,
  config: StoredBridgeConfig,
  frame: ExecuteCommandFrame,
): Promise<void> {
  if (Date.parse(frame.expires_at) <= Date.now()) {
    sendResult(current, frame.command_id, "failed", "command_expired", {});
    return;
  }
  current.send(JSON.stringify({ type: "command.ack", command_id: frame.command_id }));
  try {
    let asset: AssetResponse;
    if (frame.command === "browser.current_tab.read") {
      const document = await readCurrentTab();
      asset = await uploadAsset(config, frame.command_id, "application/json", JSON.stringify(document));
    } else if (frame.command === "browser.current_tab.capture") {
      const image = await captureCurrentTab();
      asset = await uploadAsset(config, frame.command_id, "image/png", image);
    } else {
      sendResult(current, frame.command_id, "failed", "unsupported_command", {});
      return;
    }
    if (cancelledCommands.delete(frame.command_id)) return;
    sendResult(current, frame.command_id, "succeeded", null, {
      asset_id: asset.asset_id,
      media_type: asset.media_type,
      bytes: asset.bytes,
      sha256: asset.sha256,
    });
    await setStatus("online", `${frame.command} 已完成`);
  } catch (error) {
    const reason = error instanceof Error && error.message === "restricted_page"
      ? "restricted_page"
      : "browser_command_failed";
    sendResult(current, frame.command_id, "failed", reason, {});
  }
}

async function activeTab(): Promise<chrome.tabs.Tab> {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab?.id === undefined || tab.windowId === undefined) throw new Error("active_tab_missing");
  const url = tab.url ?? "";
  if (!url.startsWith("http://") && !url.startsWith("https://")) throw new Error("restricted_page");
  return tab;
}

async function readCurrentTab(): Promise<BrowserDocument> {
  const tab = await activeTab();
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id! },
    func: () => ({
      title: document.title,
      origin: location.origin,
      language: document.documentElement.lang || navigator.language || null,
      text: document.body?.innerText ?? "",
    }),
  });
  if (injection?.result === undefined) throw new Error("page_read_failed");
  return sanitizeBrowserDocument(injection.result);
}

async function captureCurrentTab(): Promise<Blob> {
  const tab = await activeTab();
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  return await (await fetch(dataUrl)).blob();
}

async function uploadAsset(
  config: StoredBridgeConfig,
  commandId: string,
  mediaType: string,
  body: string | Blob,
): Promise<AssetResponse> {
  const response = await fetch(`${config.hubUrl}/api/v1/devices/commands/${commandId}/asset`, {
    method: "POST",
    headers: { Authorization: `Bearer ${config.accessToken}`, "Content-Type": mediaType },
    body,
  });
  if (!response.ok) throw new Error(`asset_upload_${response.status}`);
  return await response.json() as AssetResponse;
}

function sendResult(
  current: WebSocket,
  commandId: string,
  outcome: "succeeded" | "failed",
  reasonCode: string | null,
  resultMeta: Record<string, unknown>,
): void {
  if (current.readyState !== WebSocket.OPEN) return;
  current.send(JSON.stringify({
    type: "command.result",
    command_id: commandId,
    outcome,
    reason_code: reasonCode,
    result_meta: resultMeta,
  }));
}

function startHeartbeat(current: WebSocket): void {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (current.readyState === WebSocket.OPEN) {
      current.send(JSON.stringify({ type: "device.heartbeat", capabilities: BROWSER_CAPABILITIES }));
    }
  }, 20_000);
}

function stopHeartbeat(): void {
  if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return;
  const delay = Math.min(30_000, 1_000 * 2 ** reconnectAttempt);
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect();
  }, delay);
}

function clearConnection(): void {
  if (reconnectTimer !== null) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  stopHeartbeat();
  const current = socket;
  socket = null;
  current?.close(1000, "reconnecting");
}

async function setStatus(state: string, detail: string): Promise<void> {
  await chrome.storage.local.set({ bridgeStatus: { state, detail, updatedAt: new Date().toISOString() } });
}
