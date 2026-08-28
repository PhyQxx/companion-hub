import { getCurrentWindow, PhysicalPosition } from "@tauri-apps/api/window";
import { listen } from "@tauri-apps/api/event";
import type { AvatarControl } from "./client";
import {
  DESKTOP_CONFIG_KEY,
  PET_CLICK_THROUGH_KEY,
  PET_POSITION_KEY,
  parsePetPosition,
  petStageUrl,
} from "./pet-state";
import "./pet.css";

const shell = document.querySelector<HTMLElement>("#pet-shell");
if (!shell) throw new Error("missing pet shell");

const stageUrl = petStageUrl(localStorage.getItem(DESKTOP_CONFIG_KEY));
shell.innerHTML = stageUrl
  ? `<iframe title="Aria 桌宠舞台" allow="autoplay" src="${stageUrl}"></iframe><button id="drag" aria-label="拖动桌宠" title="拖动桌宠">⋮⋮</button>`
  : `<section class="pet-error"><strong>还没有连接 Hub</strong><small>请先在 Aria Desktop 主窗口完成设备配对。</small></section>`;

const petWindow = getCurrentWindow();
const storedPosition = parsePetPosition(localStorage.getItem(PET_POSITION_KEY));
if (storedPosition) {
  void petWindow.setPosition(new PhysicalPosition(storedPosition.x, storedPosition.y));
}

const clickThrough = localStorage.getItem(PET_CLICK_THROUGH_KEY) === "true";
void petWindow.setIgnoreCursorEvents(clickThrough);

document.querySelector<HTMLButtonElement>("#drag")?.addEventListener("mousedown", (event) => {
  if (event.button === 0) void petWindow.startDragging();
});

void petWindow.onMoved(({ payload }) => {
  localStorage.setItem(PET_POSITION_KEY, JSON.stringify({ x: payload.x, y: payload.y }));
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
