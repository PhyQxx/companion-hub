const stage = document.querySelector("#stage");
const canvas = document.querySelector("#live2d");
const image = document.querySelector("#avatar");
const orb = document.querySelector("#orb");
const status = document.querySelector("#status");

let mountedModel = null;
let handle = null;
let runtimeLoad = null;
let control = { emotion: "neutral", expression: null, motion: null, lipSync: 0, speaking: false };

function setMode(mode) {
  canvas.hidden = mode !== "live2d";
  image.hidden = mode !== "image";
  orb.hidden = mode !== "orb";
}

function loadRuntime() {
  if (window.AriaLive2DRuntime) return Promise.resolve(window.AriaLive2DRuntime);
  if (runtimeLoad) return runtimeLoad;
  runtimeLoad = new Promise((resolve) => {
    const script = document.createElement("script");
    const finish = () => resolve(window.AriaLive2DRuntime ?? null);
    script.src = "/api/v1/avatar-live2d-runtime/runtime.js";
    script.async = true;
    script.addEventListener("error", () => resolve(null), { once: true });
    window.addEventListener("aria-live2d-runtime-ready", finish, { once: true });
    document.head.appendChild(script);
    window.setTimeout(finish, 20_000);
  });
  return runtimeLoad;
}

function applyControl() {
  if (!handle) return;
  handle.setSpeaking?.(control.speaking);
  handle.setEmotion?.(control.emotion);
  if (control.expression) handle.setExpression?.(control.expression);
  if (control.motion) {
    const match = control.motion.match(/^(.*?)(?::(\d+))?$/);
    const group = match?.[1]?.trim();
    if (group) handle.playMotion?.(group, match?.[2] == null ? undefined : Number(match[2]));
    control.motion = null;
  }
  if (control.speaking || control.lipSync > 0) handle.setLipSync?.(control.lipSync);
}

async function renderAvatar(avatar) {
  const assets = avatar?.assets ?? {};
  const model = typeof assets.model === "string" ? assets.model : null;
  if (avatar?.engine === "live2d" && model) {
    if (mountedModel === model && handle) return;
    handle?.destroy?.();
    handle = null;
    mountedModel = model;
    setMode("live2d");
    status.textContent = `正在加载 ${avatar.name}`;
    const runtime = await loadRuntime();
    if (!runtime) throw new Error("未安装 Live2D 运行时");
    handle = await runtime.mount(canvas, { modelUrl: model, transparent: true });
    applyControl();
    status.textContent = avatar.name;
    return;
  }

  handle?.destroy?.();
  handle = null;
  mountedModel = null;
  const emotions = assets.emotions && typeof assets.emotions === "object" ? assets.emotions : {};
  const source = emotions[control.emotion] ?? assets.thumbnail;
  if (typeof source === "string") {
    image.src = source;
    setMode("image");
  } else {
    setMode("orb");
  }
  control.motion = null;
  status.textContent = avatar?.name ?? "Aria";
}

async function refresh() {
  try {
    const response = await fetch("/api/v1/meta/runtime", { cache: "no-store" });
    if (!response.ok) throw new Error(`Hub 返回 ${response.status}`);
    const runtime = await response.json();
    await renderAvatar(runtime.avatar ?? null);
  } catch (error) {
    handle?.destroy?.();
    handle = null;
    mountedModel = null;
    setMode("orb");
    status.textContent = error instanceof Error ? error.message : "Hub 暂时不可用";
  }
}

function receiveControl(value) {
  if (!value || typeof value !== "object" || value.type !== "aria.avatar.control") return;
  control = {
    emotion: typeof value.emotion === "string" ? value.emotion : control.emotion,
    expression: typeof value.expression === "string" ? value.expression : null,
    motion: typeof value.motion === "string" ? value.motion : null,
    lipSync: Number.isFinite(value.lipSync) ? Math.max(0, Math.min(1, value.lipSync)) : control.lipSync,
    speaking: typeof value.speaking === "boolean" ? value.speaking : control.speaking,
  };
  applyControl();
  if (!handle) void refresh();
}

window.addEventListener("message", (event) => receiveControl(event.data));
try {
  const channel = new BroadcastChannel("aria-avatar-control-v1");
  channel.addEventListener("message", (event) => receiveControl(event.data));
} catch { /* BroadcastChannel is optional in older webviews. */ }

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void refresh();
});
void refresh();
window.setInterval(() => { if (!document.hidden) void refresh(); }, 30_000);
