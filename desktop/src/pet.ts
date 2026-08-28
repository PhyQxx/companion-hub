import {
  availableMonitors,
  currentMonitor,
  getCurrentWindow,
  PhysicalPosition,
  primaryMonitor,
  type Monitor,
} from "@tauri-apps/api/window";
import { listen } from "@tauri-apps/api/event";
import type { AvatarControl } from "./client";
import {
  DESKTOP_CONFIG_KEY,
  PET_CLICK_THROUGH_KEY,
  PET_POSITION_KEY,
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
shell.innerHTML = stageUrl
  ? `<iframe title="Aria 桌宠舞台" allow="autoplay" src="${stageUrl}"></iframe><button id="drag" aria-label="拖动桌宠" title="拖动桌宠">⋮⋮</button>`
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

void listen("pet-ensure-visible", () => void ensurePetVisible());

document.addEventListener("visibilitychange", () => {
  const frame = document.querySelector<HTMLIFrameElement>("iframe");
  frame?.contentWindow?.postMessage(
    { type: "aria.pet.visibility", visible: !document.hidden },
    stageUrl ? new URL(stageUrl).origin : "*",
  );
  if (!document.hidden) void ensurePetVisible();
});
