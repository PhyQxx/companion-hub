import {
  availableMonitors,
  currentMonitor,
  getCurrentWindow,
  PhysicalPosition,
  primaryMonitor,
  type Monitor,
} from "@tauri-apps/api/window";
import { emit, listen } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import type { AvatarControl, PetAudioFrame, PetMessageState } from "./client";
import { PetAudioPlayback } from "./pet-audio";
import {
  DESKTOP_CONFIG_KEY,
  PET_CLICK_THROUGH_KEY,
  PET_POSITION_KEY,
  PET_VISIBLE_KEY,
  parsePetPosition,
  persistedPetPosition,
  petStageUrl,
  resolvePetPosition,
  type PetMonitorArea,
} from "./pet-state";
import "./pet.css";

const shell = document.querySelector<HTMLElement>("#pet-shell");
if (!shell) throw new Error("missing pet shell");

const stageUrl = petStageUrl(localStorage.getItem(DESKTOP_CONFIG_KEY));
const PET_SPEAK_KEY = "ariaDesktopPetSpeak";
shell.innerHTML = stageUrl
  ? `<iframe title="Aria 桌宠舞台" allow="autoplay" src="${stageUrl}"></iframe><div class="pet-tools"><button id="drag" aria-label="拖动桌宠" title="拖动桌宠">⋮⋮</button><button id="menu-toggle" aria-label="桌宠菜单" title="桌宠菜单">•••</button><nav id="pet-menu" hidden><button id="quick-chat" type="button">快速聊天</button><button id="open-main" type="button">打开控制台</button><button id="enable-click-through" type="button">开启鼠标穿透</button><button id="hide-pet" type="button">隐藏桌宠</button></nav></div><form id="pet-composer" hidden><textarea id="pet-message" rows="2" maxlength="2000" placeholder="跟 Aria 说点什么…" aria-label="给 Aria 发消息"></textarea><div class="composer-row"><select id="pet-privacy" aria-label="消息隐私级别"><option value="L1">普通 L1</option><option value="L2">私密 L2</option></select><label class="voice-toggle"><input id="pet-speak" type="checkbox" />播报</label><small id="pet-message-status" aria-live="polite"></small><button id="send-pet-message" type="submit">发送</button></div></form>`
  : `<section class="pet-error"><strong>还没有连接 Hub</strong><small>请先在 Aria Desktop 主窗口完成设备配对。</small></section>`;

const petWindow = getCurrentWindow();

function monitorArea(monitor: Monitor): PetMonitorArea {
  return {
    name: monitor.name,
    x: monitor.workArea.position.x,
    y: monitor.workArea.position.y,
    width: monitor.workArea.size.width,
    height: monitor.workArea.size.height,
  };
}

async function orderedMonitorAreas(): Promise<PetMonitorArea[]> {
  const [primary, available] = await Promise.all([primaryMonitor(), availableMonitors()]);
  const monitors = primary ? [primary, ...available] : available;
  return monitors
    .filter((monitor, index) => monitors.findIndex((candidate) =>
      candidate.name === monitor.name &&
      candidate.position.x === monitor.position.x &&
      candidate.position.y === monitor.position.y
    ) === index)
    .map(monitorArea);
}

async function ensurePetVisible() {
  const stored = parsePetPosition(localStorage.getItem(PET_POSITION_KEY));
  if (!stored) return;
  const [monitors, size] = await Promise.all([orderedMonitorAreas(), petWindow.outerSize()]);
  const resolved = resolvePetPosition(stored, monitors, size);
  if (!resolved) return;
  await petWindow.setPosition(new PhysicalPosition(resolved.x, resolved.y));
  localStorage.setItem(PET_POSITION_KEY, JSON.stringify(resolved));
}

let persistTimer: number | null = null;
function schedulePositionPersist() {
  if (persistTimer !== null) window.clearTimeout(persistTimer);
  persistTimer = window.setTimeout(() => {
    persistTimer = null;
    void persistPosition();
  }, 160);
}

async function persistPosition() {
  const [position, size, monitor] = await Promise.all([
    petWindow.outerPosition(),
    petWindow.outerSize(),
    currentMonitor(),
  ]);
  if (!monitor) return;
  localStorage.setItem(
    PET_POSITION_KEY,
    JSON.stringify(persistedPetPosition(position, monitorArea(monitor), size)),
  );
}

const clickThrough = localStorage.getItem(PET_CLICK_THROUGH_KEY) === "true";
void petWindow.setIgnoreCursorEvents(clickThrough);

document.querySelector<HTMLButtonElement>("#drag")?.addEventListener("mousedown", (event) => {
  if (event.button === 0) void petWindow.startDragging();
});

const petMenu = document.querySelector<HTMLElement>("#pet-menu");
const petComposer = document.querySelector<HTMLFormElement>("#pet-composer");
const petMessage = document.querySelector<HTMLTextAreaElement>("#pet-message");
const petPrivacy = document.querySelector<HTMLSelectElement>("#pet-privacy");
const petMessageStatus = document.querySelector<HTMLElement>("#pet-message-status");
const sendPetMessage = document.querySelector<HTMLButtonElement>("#send-pet-message");
const petSpeak = document.querySelector<HTMLInputElement>("#pet-speak");
const audioPlayback = new PetAudioPlayback();
let submittedPrivacy: "L1" | "L2" = "L1";
if (petSpeak) petSpeak.checked = localStorage.getItem(PET_SPEAK_KEY) !== "false";

function closeComposer() {
  if (petComposer) petComposer.hidden = true;
}

document.querySelector<HTMLButtonElement>("#menu-toggle")?.addEventListener("click", () => {
  if (petMenu) petMenu.hidden = !petMenu.hidden;
});
document.querySelector<HTMLButtonElement>("#quick-chat")?.addEventListener("click", () => {
  if (!petComposer || !petMessage) return;
  petComposer.hidden = false;
  if (petMenu) petMenu.hidden = true;
  petMessage.focus();
});
document.querySelector<HTMLButtonElement>("#open-main")?.addEventListener("click", async () => {
  const main = await WebviewWindow.getByLabel("main");
  await main?.show();
  await main?.setFocus();
  if (petMenu) petMenu.hidden = true;
});
document.querySelector<HTMLButtonElement>("#enable-click-through")?.addEventListener(
  "click",
  async () => {
    localStorage.setItem(PET_CLICK_THROUGH_KEY, "true");
    if (petMenu) petMenu.hidden = true;
    closeComposer();
    await petWindow.setIgnoreCursorEvents(true);
    await emit("pet-click-through-changed", true);
  },
);
document.querySelector<HTMLButtonElement>("#hide-pet")?.addEventListener("click", async () => {
  localStorage.setItem(PET_VISIBLE_KEY, "false");
  closeComposer();
  await emit("pet-visibility-changed", false);
  await petWindow.hide();
});

petComposer?.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = petMessage?.value.trim() ?? "";
  const privacyLevel = petPrivacy?.value === "L2" ? "L2" : "L1";
  if (!text || !sendPetMessage || !petMessageStatus) return;
  submittedPrivacy = privacyLevel;
  sendPetMessage.disabled = true;
  petMessageStatus.textContent = "正在发送…";
  const speak = petSpeak?.checked ?? true;
  localStorage.setItem(PET_SPEAK_KEY, String(speak));
  void emit("pet-message-submit", { text, privacyLevel, speak });
});

petMessage?.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeComposer();
    return;
  }
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    petComposer?.requestSubmit();
  }
});

void ensurePetVisible().finally(() => {
  void petWindow.onMoved(schedulePositionPersist);
  void petWindow.onResized(schedulePositionPersist);
});

window.addEventListener("storage", (event) => {
  if (event.key === DESKTOP_CONFIG_KEY) window.location.reload();
  if (event.key === PET_CLICK_THROUGH_KEY) {
    void petWindow.setIgnoreCursorEvents(event.newValue === "true");
  }
});

void listen<AvatarControl>("avatar-control", ({ payload }) => {
  const frame = document.querySelector<HTMLIFrameElement>("iframe");
  const targetOrigin = stageUrl ? new URL(stageUrl).origin : "*";
  frame?.contentWindow?.postMessage(
    { type: "aria.avatar.control", ...payload },
    targetOrigin,
  );
});

void listen<PetMessageState>("pet-message-state", ({ payload }) => {
  if (!petMessageStatus || !sendPetMessage) return;
  if (payload.status === "accepted") {
    petMessageStatus.textContent = "正在回复…";
    return;
  }
  sendPetMessage.disabled = false;
  if (payload.status === "completed") {
    if (petMessage) petMessage.value = "";
    petMessageStatus.textContent = submittedPrivacy === "L2"
      ? payload.audioDelivered !== true && petSpeak?.checked
        ? "私密回复已保存 · 本地语音不可用"
        : "私密回复已保存到主聊天"
      : payload.audioDelivered !== true && petSpeak?.checked
        ? "已回复 · 语音不可用"
        : "已回复";
    return;
  }
  const reason = {
    capability_not_authorized: "请先在 Admin 授权 avatar.chat",
    device_offline: "设备连接已断开",
    duplicate_request: "这条消息已提交",
    generation_failed: "回复生成失败，请重试",
    pet_chat_unavailable: "聊天服务暂不可用",
    turn_in_progress: "上一条还在回复",
  }[payload.reasonCode ?? ""];
  petMessageStatus.textContent = reason ?? "发送失败，请重试";
});

void listen<PetAudioFrame>("pet-audio", ({ payload }) => {
  void audioPlayback.handle(payload).catch(() => {
    if (petMessageStatus) petMessageStatus.textContent = "已回复 · 音频播放失败";
  });
});

void listen("pet-ensure-visible", () => void ensurePetVisible());

document.addEventListener("visibilitychange", () => {
  const frame = document.querySelector<HTMLIFrameElement>("iframe");
  frame?.contentWindow?.postMessage(
    { type: "aria.pet.visibility", visible: !document.hidden },
    stageUrl ? new URL(stageUrl).origin : "*",
  );
  if (!document.hidden) void ensurePetVisible();
  else audioPlayback.interrupt();
});
