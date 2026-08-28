const canvas = document.querySelector("#live2d");
const image = document.querySelector("#avatar");
const orb = document.querySelector("#orb");
const status = document.querySelector("#status");
const speech = document.querySelector("#speech");

let mountedModel = null;
let handle = null;
let runtimeLoad = null;
let suspended = document.hidden;
let refreshVersion = 0;
let speechTimer = null;
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

function showSpeech(text) {
  if (speechTimer !== null) window.clearTimeout(speechTimer);
  speech.textContent = text;
  speech.hidden = false;
  speechTimer = window.setTimeout(() => {
    speech.hidden = true;
    speechTimer = null;
  }, 12_000);
}

async function renderAvatar(avatar, version) {
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
    const mounted = await runtime.mount(canvas, { modelUrl: model, transparent: true });
    if (suspended || version !== refreshVersion) {
      mounted?.destroy?.();
      return;
    }
    handle = mounted;
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
  if (suspended) return;
  const version = ++refreshVersion;
  try {
    const response = await fetch("/api/v1/meta/runtime", { cache: "no-store" });
    if (!response.ok) throw new Error(`Hub 返回 ${response.status}`);
    const runtime = await response.json();
    if (suspended || version !== refreshVersion) return;
    await renderAvatar(runtime.avatar ?? null, version);
  } catch (error) {
    handle?.destroy?.();
    handle = null;
    mountedModel = null;
    setMode("orb");
    status.textContent = error instanceof Error ? error.message : "Hub 暂时不可用";
  }
}

function setSuspended(value) {
  suspended = value;
  if (value) {
    refreshVersion += 1;
    handle?.destroy?.();
    handle = null;
    mountedModel = null;
    return;
  }
  void refresh();
}

function receiveControl(value) {
  if (!value || typeof value !== "object") return;
  if (value.type === "aria.pet.visibility") {
    if (typeof value.visible === "boolean") setSuspended(!value.visible);
    return;
  }
  if (value.type !== "aria.avatar.control") return;
  control = {
    emotion: typeof value.emotion === "string" ? value.emotion : control.emotion,
    expression: typeof value.expression === "string" ? value.expression : null,
    motion: typeof value.motion === "string" ? value.motion : null,
    lipSync: Number.isFinite(value.lipSync) ? Math.max(0, Math.min(1, value.lipSync)) : control.lipSync,
    speaking: typeof value.speaking === "boolean" ? value.speaking : control.speaking,
  };
  if (typeof value.text === "string" && value.text.trim()) showSpeech(value.text.trim());
  applyControl();
  if (!handle && !suspended) void refresh();
}

speech.addEventListener("click", () => {
  speech.hidden = true;
  if (speechTimer !== null) window.clearTimeout(speechTimer);
  speechTimer = null;
});

canvas.addEventListener("click", () => {
  handle?.playMotion?.("TapBody", 0);
});
image.addEventListener("click", () => image.animate(
  [{ transform: "scale(1)" }, { transform: "scale(1.025)" }, { transform: "scale(1)" }],
  { duration: 260, easing: "ease-out" },
));
orb.addEventListener("click", () => orb.animate(
  [{ filter: "brightness(1)" }, { filter: "brightness(1.2)" }, { filter: "brightness(1)" }],
  { duration: 320, easing: "ease-out" },
));

window.addEventListener("message", (event) => receiveControl(event.data));
try {
  const channel = new BroadcastChannel("aria-avatar-control-v1");
  channel.addEventListener("message", (event) => receiveControl(event.data));
} catch { /* BroadcastChannel is optional in older webviews. */ }

document.addEventListener("visibilitychange", () => {
  setSuspended(document.hidden);
});
if (!suspended) void refresh();
window.setInterval(() => { if (!suspended) void refresh(); }, 30_000);
