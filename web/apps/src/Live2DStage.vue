<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ensureLive2DRuntime, type AriaLive2DHandle } from "@aria/shared";

const props = defineProps<{
  modelUrl: string;
  emotion?: string;
  expression?: string | null;
  lipSync?: number;
  speaking?: boolean;
  motion?: string | null;
  motionSequence?: number;
}>();
const canvas = ref<HTMLCanvasElement | null>(null);
const state = ref<"loading" | "ready" | "runtime-missing" | "error">("loading");
let handle: AriaLive2DHandle;
let mountRequest = 0;

function dispose() {
  if (handle && typeof handle === "object") handle.destroy?.();
  handle = undefined;
}

function controlHandle() {
  return handle && typeof handle === "object" ? handle : null;
}

function applyControlState() {
  const current = controlHandle();
  if (!current) return;
  current.setSpeaking?.(props.speaking ?? false);
  if (props.emotion) current.setEmotion?.(props.emotion);
  if (props.expression) current.setExpression?.(props.expression);
  if (props.speaking || (props.lipSync ?? 0) > 0) current.setLipSync?.(props.lipSync ?? 0);
}

function playRequestedMotion() {
  const current = controlHandle();
  const motion = props.motion?.trim();
  if (!current || !motion) return;
  const match = motion.match(/^(.*?)(?::(\d+))?$/);
  const group = match?.[1]?.trim();
  if (group) current.playMotion?.(group, match?.[2] == null ? undefined : Number(match[2]));
}

async function mountModel() {
  const request = ++mountRequest;
  dispose();
  await nextTick();
  if (!canvas.value) return;
  const runtime = window.AriaLive2DRuntime ?? await ensureLive2DRuntime();
  if (request !== mountRequest) return;
  if (!runtime) {
    state.value = "runtime-missing";
    return;
  }
  state.value = "loading";
  try {
    const mountedHandle = await runtime.mount(canvas.value, { modelUrl: props.modelUrl, transparent: true });
    if (request !== mountRequest) {
      if (mountedHandle && typeof mountedHandle === "object") mountedHandle.destroy?.();
      return;
    }
    handle = mountedHandle;
    applyControlState();
    playRequestedMotion();
    state.value = "ready";
  } catch {
    if (request !== mountRequest) return;
    state.value = "error";
  }
}

onMounted(() => {
  void mountModel();
});
watch(() => props.modelUrl, () => void mountModel());
watch(() => props.emotion, (emotion) => { if (emotion) controlHandle()?.setEmotion?.(emotion) });
watch(() => props.expression, (expression) => { if (expression) controlHandle()?.setExpression?.(expression) });
watch(() => props.lipSync, (value) => {
  if (props.speaking || (value ?? 0) > 0) controlHandle()?.setLipSync?.(value ?? 0);
});
watch(() => props.speaking, (speaking) => controlHandle()?.setSpeaking?.(speaking ?? false));
watch(() => props.motionSequence, () => playRequestedMotion());
onBeforeUnmount(() => {
  mountRequest++;
  dispose();
});
</script>

<template>
  <div class="live2d-host">
    <canvas ref="canvas" aria-label="Live2D 形象画布"></canvas>
    <div v-if="state !== 'ready'" class="live2d-state">
      <strong v-if="state === 'runtime-missing'">Live2D 模型已就绪</strong>
      <strong v-else-if="state === 'error'">Live2D 加载失败</strong>
      <strong v-else>正在加载 Live2D</strong>
      <small v-if="state === 'runtime-missing'">需安装官方 Cubism Web Core 运行时</small>
      <small v-else-if="state === 'error'">请检查模型包与 Cubism Core 版本</small>
    </div>
  </div>
</template>

<style scoped>
.live2d-host{position:relative;width:100%;height:100%;min-height:300px}.live2d-host canvas{display:block;width:100%;height:100%}.live2d-state{position:absolute;inset:0;display:grid;place-content:center;gap:4px;padding:12px;text-align:center;background:linear-gradient(160deg,var(--stage-from),var(--stage-to));color:var(--text)}.live2d-state strong{font-size:12px}.live2d-state small{color:var(--muted);font-size:9px}@media(max-width:1100px){.live2d-host{min-height:260px}}@media(max-width:760px){.live2d-host{min-height:220px}}
</style>
