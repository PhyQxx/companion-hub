import { BROWSER_CAPABILITIES, normalizeHubUrl } from "./core";

interface PairResponse {
  access_token: string;
  device: { id: string; alias: string | null };
}

const form = document.querySelector<HTMLFormElement>("#pair-form")!;
const hubInput = document.querySelector<HTMLInputElement>("#hub-url")!;
const codeInput = document.querySelector<HTMLInputElement>("#pairing-code")!;
const aliasInput = document.querySelector<HTMLInputElement>("#alias")!;
const forgetButton = document.querySelector<HTMLButtonElement>("#forget")!;
const status = document.querySelector<HTMLElement>("#status")!;

void refresh();
chrome.storage.onChanged.addListener(() => void refresh());

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void pair();
});
forgetButton.addEventListener("click", () => void forget());

async function pair(): Promise<void> {
  try {
    const hubUrl = normalizeHubUrl(hubInput.value);
    const granted = await chrome.permissions.request({ origins: ["<all_urls>"] });
    if (!granted) throw new Error("需要网页访问权限才能读取当前标签页");
    setStatus("正在配对…");
    const response = await fetch(`${hubUrl}/api/v1/devices/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pairing_code: codeInput.value.trim(),
        name: "Aria Browser Bridge",
        alias: aliasInput.value.trim() || null,
        client_type: "browser",
        capabilities: BROWSER_CAPABILITIES,
      }),
    });
    if (!response.ok) throw new Error(`Hub 拒绝配对（${response.status}）`);
    const paired = await response.json() as PairResponse;
    await chrome.storage.local.set({
      bridgeConfig: {
        hubUrl,
        accessToken: paired.access_token,
        deviceId: paired.device.id,
        alias: paired.device.alias,
      },
    });
    codeInput.value = "";
    await chrome.runtime.sendMessage({ type: "bridge.reconnect" });
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "配对失败");
  }
}

async function forget(): Promise<void> {
  await chrome.storage.local.remove(["bridgeConfig", "bridgeStatus"]);
  await chrome.runtime.sendMessage({ type: "bridge.reconnect" });
  setStatus("本机配对信息已清除；如需立即失效，请同时在 Hub 撤销设备");
}

async function refresh(): Promise<void> {
  const stored = await chrome.storage.local.get(["bridgeConfig", "bridgeStatus"]);
  const config = stored.bridgeConfig as { hubUrl?: string; alias?: string | null } | undefined;
  const bridgeStatus = stored.bridgeStatus as { detail?: string } | undefined;
  if (config?.hubUrl) hubInput.value = config.hubUrl;
  if (config?.alias) aliasInput.value = config.alias;
  setStatus(bridgeStatus?.detail ?? (config ? "已配对，等待连接" : "尚未配对"));
}

function setStatus(message: string): void {
  status.textContent = message;
}
