import { formPageOperation } from "./form-page";
import {
  BROWSER_CAPABILITIES,
  buildFormFieldDescriptors,
  buildTabHint,
  isExecuteCommand,
  isValidFormRef,
  parseFormFillFields,
  sanitizeBrowserDocument,
  sanitizeFormFillOutcome,
  verifyFrame,
  websocketUrl,
  type BrowserDocument,
  type ExecuteCommandFrame,
  type SignedFrame,
  type TabHint,
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
let observeTabHint = false;
const cancelledCommands = new Set<string>();
let formSnapshot: { id: string; tabId: number; url: string; expires: number; refs: Set<string>; forms: Set<string> } | null = null;

chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (formSnapshot && tabId !== formSnapshot.tabId) formSnapshot = null;
});
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (formSnapshot?.tabId === tabId && (change.status === "loading" || change.url !== undefined)) formSnapshot = null;
});
chrome.tabs.onRemoved.addListener(tabId => {
  if (formSnapshot?.tabId === tabId) formSnapshot = null;
});

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
  formSnapshot = null;
  observeTabHint = false;
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
    observeTabHint = frame.observe_tab_hint === true;
    await setStatus("online", "已连接，等待网页读取请求");
    startHeartbeat(current);
    return;
  }
  if (frame.type === "heartbeat.accepted") {
    observeTabHint = frame.observe_tab_hint === true;
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
    let asset: AssetResponse | null = null;
    let resultMeta: Record<string, unknown> | null = null;
    if (frame.command === "browser.current_tab.read") {
      const document = await readCurrentTab();
      asset = await uploadAsset(config, frame.command_id, "application/json", JSON.stringify(document));
    } else if (frame.command === "browser.current_tab.capture") {
      const image = await captureCurrentTab();
      asset = await uploadAsset(config, frame.command_id, "image/png", image);
    } else if (frame.command === "browser.tab.open") {
      resultMeta = await openTab(parseTabOpenArgs(frame.args_json));
    } else if (frame.command === "browser.form.read") {
      resultMeta = await readFormFields();
    } else if (frame.command === "browser.form.fill") {
      resultMeta = await fillFormFields(parseFormFillArgs(frame.args_json));
    } else if (frame.command === "browser.form.submit") {
      resultMeta = await submitForm(parseFormSubmitArgs(frame.args_json));
    } else {
      sendResult(current, frame.command_id, "failed", "unsupported_command", {});
      return;
    }
    if (cancelledCommands.delete(frame.command_id)) return;
    if (asset !== null) {
      sendResult(current, frame.command_id, "succeeded", null, {
        asset_id: asset.asset_id,
        media_type: asset.media_type,
        bytes: asset.bytes,
        sha256: asset.sha256,
      });
    } else {
      sendResult(current, frame.command_id, "succeeded", null, resultMeta ?? {});
    }
    await setStatus("online", `${frame.command} 已完成`);
  } catch (error) {
    const allowed = new Set(["restricted_page", "active_tab_missing", "invalid_command_args", "form_snapshot_missing",
      "form_snapshot_expired", "form_snapshot_changed", "control_not_found", "form_not_found",
      "control_not_editable", "option_not_found", "unsupported_field", "set_value_failed", "form_too_large"]);
    const reason = error instanceof Error && allowed.has(error.message) ? error.message : "browser_command_failed";
    sendResult(current, frame.command_id, "failed", reason,
      error instanceof FormOperationError ? { filled: error.filled, reread_required: true } : {});
  }
}

/** WEB-01：新标签页导航只允许 http/https，页面上下文与 read/capture 相同闸门。 */
function parseTabOpenArgs(argsJson: string): { url: string } {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    throw new Error("invalid_command_args");
  }
  const url = typeof parsed.url === "string" ? parsed.url.trim() : "";
  if (!url || url.length > 2048) throw new Error("invalid_command_args");
  let candidate: URL;
  try {
    candidate = new URL(url);
  } catch {
    throw new Error("invalid_command_args");
  }
  if (candidate.protocol !== "http:" && candidate.protocol !== "https:") {
    throw new Error("invalid_command_args");
  }
  return { url: candidate.toString() };
}

function parseFormFillArgs(argsJson: string): { snapshotId: string; fields: Array<{ ref: string; value: string }> } {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    throw new Error("invalid_command_args");
  }
  const fields = parseFormFillFields(parsed.fields);
  if (fields === null) throw new Error("invalid_command_args");
  return { snapshotId: parseSnapshotId(parsed.snapshot_id), fields };
}

function parseFormSubmitArgs(argsJson: string): { snapshotId: string; formRef: string } {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    throw new Error("invalid_command_args");
  }
  if (!isValidFormRef(parsed.form_ref)) throw new Error("invalid_command_args");
  return { snapshotId: parseSnapshotId(parsed.snapshot_id), formRef: parsed.form_ref };
}

async function openTab(request: { url: string }): Promise<Record<string, unknown>> {
  const tab = await chrome.tabs.create({ url: request.url, active: true });
  return { tab_id: tab.id ?? null, url: request.url };
}

function parseSnapshotId(value: unknown): string {
  if (typeof value !== "string" || !/^[a-f0-9]{32}$/.test(value)) throw new Error("invalid_command_args");
  return value;
}

export async function readFormFields(): Promise<Record<string, unknown>> {
  formSnapshot = null;
  const tab = await activeTab();
  const snapshotId = crypto.randomUUID().replaceAll("-", "");
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id! }, world: "ISOLATED", func: formPageOperation,
    args: [{ operation: "read", snapshotId }],
  });
  const raw = injection?.result;
  if (!raw || raw.ok !== true) throw new Error(String(raw?.reason ?? "page_read_failed"));
  if (raw.pageUrl !== tab.url) throw new Error("form_snapshot_changed");
  const { fields, truncated, origin } = buildFormFieldDescriptors(raw.controls, tab.url ?? "");
  const result: Record<string, unknown> = {
    snapshot_id: snapshotId, page_url: (tab.url ?? "").slice(0, 1024), origin,
    title: (tab.title ?? "").slice(0, 120), field_count: fields.length, truncated, fields,
  };
  // Hub terminal receipts are capped at 4096 bytes, including multi-byte labels.
  while (fields.length && new TextEncoder().encode(JSON.stringify(result)).length > 3800) {
    fields.pop(); result.field_count = fields.length; result.truncated = true;
  }
  if (new TextEncoder().encode(JSON.stringify(result)).length > 3800) throw new Error("form_too_large");
  formSnapshot = { id: snapshotId, tabId: tab.id!, url: tab.url!, expires: Date.now() + 5 * 60_000,
    refs: new Set(fields.map(field => field.ref)),
    forms: new Set(fields.flatMap(field => field.formRef ? [field.formRef] : [])) };
  return result;
}

async function runFormOperation(request: {
  operation: "fill" | "submit"; snapshotId: string;
  fields?: Array<{ ref: string; value: string }>; formRef?: string;
}): Promise<Record<string, unknown>> {
  const snapshot = formSnapshot;
  if (!snapshot || snapshot.id !== request.snapshotId) throw new Error("form_snapshot_missing");
  if (snapshot.expires <= Date.now()) { formSnapshot = null; throw new Error("form_snapshot_expired"); }
  const tab = await activeTab();
  if (tab.id !== snapshot.tabId || tab.url !== snapshot.url) {
    formSnapshot = null; throw new Error("form_snapshot_changed");
  }
  if (request.fields?.some(field => !snapshot.refs.has(field.ref))) throw new Error("control_not_found");
  if (request.formRef && !snapshot.forms.has(request.formRef)) throw new Error("form_not_found");
  if (request.operation === "submit") formSnapshot = null;
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: snapshot.tabId }, world: "ISOLATED", func: formPageOperation, args: [request],
  });
  const result = injection?.result;
  if (!result || result.ok !== true) {
    formSnapshot = null;
    throw new FormOperationError(String(result?.reason ?? "page_read_failed"), Number(result?.filled ?? 0));
  }
  if (request.operation === "submit") return { submitted: true, page_url: snapshot.url };
  const outcome = sanitizeFormFillOutcome(result, snapshot.url);
  if (!outcome) throw new Error("page_read_failed");
  return { ...outcome };
}

class FormOperationError extends Error {
  constructor(reason: string, readonly filled: number) { super(reason); }
}

export async function fillFormFields(request: { snapshotId: string; fields: Array<{ ref: string; value: string }> }): Promise<Record<string, unknown>> {
  return runFormOperation({ operation: "fill", ...request });
}

export async function submitForm(request: { snapshotId: string; formRef: string }): Promise<Record<string, unknown>> {
  return runFormOperation({ operation: "submit", ...request });
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
    if (current.readyState === WebSocket.OPEN) void sendHeartbeat(current);
  }, 20_000);
}

async function sendHeartbeat(current: WebSocket): Promise<void> {
  const frame: Record<string, unknown> = {
    type: "device.heartbeat",
    capabilities: BROWSER_CAPABILITIES,
  };
  if (observeTabHint) {
    const hint = await currentTabHint();
    if (hint) frame.tab_hint = hint;
  }
  current.send(JSON.stringify(frame));
}

async function currentTabHint(): Promise<TabHint | null> {
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return buildTabHint(tab?.url, tab?.title);
  } catch {
    return null;
  }
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
