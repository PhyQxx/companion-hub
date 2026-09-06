import {
  BROWSER_CAPABILITIES,
  buildFormFieldDescriptors,
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
    const reason = error instanceof Error && error.message === "restricted_page"
      ? "restricted_page"
      : error instanceof Error && error.message === "invalid_command_args"
        ? "invalid_command_args"
        : "browser_command_failed";
    sendResult(current, frame.command_id, "failed", reason, {});
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

function parseFormFillArgs(argsJson: string): Array<{ ref: string; value: string }> {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    throw new Error("invalid_command_args");
  }
  const fields = parseFormFillFields(parsed.fields);
  if (fields === null) throw new Error("invalid_command_args");
  return fields;
}

function parseFormSubmitArgs(argsJson: string): { formRef: string } {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(argsJson) as Record<string, unknown>;
  } catch {
    throw new Error("invalid_command_args");
  }
  if (!isValidFormRef(parsed.form_ref)) throw new Error("invalid_command_args");
  return { formRef: parsed.form_ref };
}

async function openTab(request: { url: string }): Promise<Record<string, unknown>> {
  const tab = await chrome.tabs.create({ url: request.url, active: true });
  return { tab_id: tab.id ?? null, url: request.url };
}

/**
 * 页面端控件枚举必须自包含（chrome.scripting 序列化注入函数，不能引用外层闭包）。
 * 枚举顺序即 ref 顺序：fill/submit 依据同一枚举重放定位，页面结构变化会导致
 * mismatch，由执行结果反映而不是静默错位。
 */
const ENUMERATE_CONTROLS_SOURCE = () => {
  const eligible = (element: Element): element is HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement => {
    if (
      !(element instanceof HTMLInputElement) &&
      !(element instanceof HTMLTextAreaElement) &&
      !(element instanceof HTMLSelectElement)
    ) return false;
    const type = element instanceof HTMLInputElement ? element.type.toLowerCase() : "";
    if (["hidden", "submit", "button", "image", "reset", "file"].includes(type)) return false;
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden";
  };
  const formIndexes = new Map<HTMLFormElement, number>();
  Array.from(document.querySelectorAll("form")).forEach((form, index) => {
    formIndexes.set(form as HTMLFormElement, index);
  });
  const described: Array<Record<string, unknown>> = [];
  Array.from(document.querySelectorAll("input, textarea, select")).forEach((element) => {
    if (!eligible(element)) return;
    const form = element.form;
    const label =
      element instanceof HTMLSelectElement
        ? ""
        : element.labels && element.labels.length > 0
          ? (element.labels[0] as HTMLLabelElement).innerText
          : "";
    described.push({
      tag: element.tagName.toLowerCase(),
      type: element instanceof HTMLInputElement ? element.type.toLowerCase() : element.tagName.toLowerCase(),
      label,
      placeholder: "placeholder" in element ? String(element.placeholder ?? "") : "",
      name: element.name ?? "",
      value: element.value ?? "",
      required: element.required,
      formIndex: form !== null && formIndexes.has(form) ? formIndexes.get(form)! : -1,
    });
  });
  return described;
};

async function readFormFields(): Promise<Record<string, unknown>> {
  const tab = await activeTab();
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id! },
    func: ENUMERATE_CONTROLS_SOURCE,
  });
  if (injection?.result === undefined) throw new Error("page_read_failed");
  const { fields, truncated, origin } = buildFormFieldDescriptors(injection.result, tab.url ?? "");
  return {
    page_url: tab.url ?? "",
    origin: origin || (tab.url ?? ""),
    title: tab.title ?? "",
    field_count: fields.length,
    truncated,
    fields,
  };
}

async function fillFormFields(plan: Array<{ ref: string; value: string }>): Promise<Record<string, unknown>> {
  const tab = await activeTab();
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id! },
    func: (entries: Array<{ ref: string; value: string }>) => {
      const eligible = (element: Element): element is HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement => {
        if (
          !(element instanceof HTMLInputElement) &&
          !(element instanceof HTMLTextAreaElement) &&
          !(element instanceof HTMLSelectElement)
        ) return false;
        const type = element instanceof HTMLInputElement ? element.type.toLowerCase() : "";
        if (["hidden", "submit", "button", "image", "reset", "file"].includes(type)) return false;
        const style = window.getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden";
      };
      const controls: Array<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement> = [];
      Array.from(document.querySelectorAll("input, textarea, select")).forEach((element) => {
        if (eligible(element)) controls.push(element);
      });
      // 重新按文档顺序编号，与 form.read 的 ref 契约一致。
      let filled = 0;
      const skipped: Array<{ ref: string; reason: string }> = [];
      for (const entry of entries) {
        const index = Number(entry.ref.slice(1));
        const element = controls[index];
        if (element === undefined) {
          skipped.push({ ref: entry.ref, reason: "control_not_found" });
          continue;
        }
        if (element instanceof HTMLInputElement && element.type.toLowerCase() === "password") {
          skipped.push({ ref: entry.ref, reason: "password_field" });
          continue;
        }
        try {
          if (element instanceof HTMLSelectElement) {
            const option = Array.from(element.options).find((candidate) => candidate.value === entry.value);
            if (option === undefined) {
              skipped.push({ ref: entry.ref, reason: "option_not_found" });
              continue;
            }
            element.value = option.value;
          } else {
            // 走原型 setter + input/change 事件，兼容 React/Vue 受控组件。
            const prototype = element instanceof HTMLTextAreaElement
              ? HTMLTextAreaElement.prototype
              : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
            if (setter) setter.call(element, entry.value);
            else element.value = entry.value;
            element.dispatchEvent(new Event("input", { bubbles: true }));
            element.dispatchEvent(new Event("change", { bubbles: true }));
          }
          filled += 1;
        } catch {
          skipped.push({ ref: entry.ref, reason: "set_value_failed" });
        }
      }
      return { filled, total: entries.length, skipped, pageUrl: location.href };
    },
    args: [plan],
  });
  if (injection?.result === undefined) throw new Error("page_read_failed");
  const outcome = sanitizeFormFillOutcome(injection.result, tab.url ?? "");
  if (outcome === null) throw new Error("page_read_failed");
  return { ...outcome };
}

async function submitForm(request: { formRef: string }): Promise<Record<string, unknown>> {
  const tab = await activeTab();
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id! },
    func: (formRef: string) => {
      const forms = Array.from(document.querySelectorAll("form"));
      const index = Number(formRef.slice(4));
      const form = forms[index];
      if (!(form instanceof HTMLFormElement)) {
        return { submitted: false, pageUrl: location.href };
      }
      form.requestSubmit();
      return { submitted: true, pageUrl: location.href };
    },
    args: [request.formRef],
  });
  if (injection?.result === undefined) throw new Error("page_read_failed");
  const result = injection.result as { submitted?: unknown; pageUrl?: unknown };
  if (result.submitted !== true) throw new Error("form_not_found");
  return {
    submitted: true,
    page_url: typeof result.pageUrl === "string" ? result.pageUrl : tab.url ?? "",
  };
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
