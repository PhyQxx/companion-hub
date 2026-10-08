<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ensureLive2DRuntime, type AriaLive2DHandle } from "@aria/shared";

const props = defineProps<{
  name: string;
  engine: string;
  assets?: { thumbnail?: string; emotions?: Record<string, string>; model?: string };
}>();
const canvas = ref<HTMLCanvasElement | null>(null);
const state = ref<"loading" | "ready" | "runtime-missing" | "error">("loading");
const errorMessage = ref("");
let handle: AriaLive2DHandle;
let mountRequest = 0;

function dispose() { if (handle && typeof handle === "object") handle.destroy?.(); handle = undefined }
async function mountModel() {
  const request = ++mountRequest;
  dispose();
  await nextTick();
  if (props.engine !== "live2d" || !props.assets?.model || !canvas.value) return;
  const runtime = window.AriaLive2DRuntime ?? await ensureLive2DRuntime();
  if (request !== mountRequest) return;
  if (!runtime) { state.value = "runtime-missing"; return }
  state.value = "loading";
  errorMessage.value = "";
  try {
    const mountedHandle = await runtime.mount(canvas.value, { modelUrl: props.assets.model, transparent: true });
    if (request !== mountRequest) {
      if (mountedHandle && typeof mountedHandle === "object") mountedHandle.destroy?.();
      return;
    }
    handle = mountedHandle;
    state.value = "ready";
  } catch (error) {
    if (request !== mountRequest) return;
    errorMessage.value = error instanceof Error ? error.message : "未知加载错误";
    state.value = "error";
  }
}
onMounted(() => { void mountModel() });
watch(() => [props.engine, props.assets?.model], () => void mountModel());
onBeforeUnmount(() => { mountRequest++; dispose() });
</script>

<template>
  <div class="preview" :class="`engine-${engine}`">
    <template v-if="engine === 'live2d' && assets?.model">
      <canvas ref="canvas"></canvas>
      <div v-if="state !== 'ready'" class="runtime-state">
        <strong>{{ state === 'runtime-missing' ? '模型包校验通过' : state === 'error' ? '模型加载失败' : '正在加载模型' }}</strong>
        <span v-if="state === 'runtime-missing'">需要在本机安装官方 Cubism Web Core；系统不会用假动画替代。</span>
        <span v-else-if="state === 'error'">{{ errorMessage || '请检查模型导出版本与 Cubism Core 兼容性。' }}</span>
      </div>
    </template>
    <img v-else-if="assets?.thumbnail" :src="assets.thumbnail" :alt="`${name} 预览`" />
    <div v-else class="fallback"><span>{{ name[0] }}</span><small>{{ engine === 'abstract' ? '抽象形象' : '暂无预览资源' }}</small></div>
  </div>
</template>

<style scoped>
.preview{position:relative;display:grid;place-items:center;width:100%;height:min(58vh,520px);overflow:hidden;border:1px solid var(--line);border-radius:16px;background:linear-gradient(160deg,var(--stage-from,#f7eef0),var(--stage-to,#eef2ff))}.preview img{width:100%;height:100%;object-fit:contain}.preview canvas{width:100%;height:100%}.fallback{display:grid;justify-items:center;gap:10px;color:var(--muted)}.fallback span{display:grid;place-items:center;width:88px;height:88px;border-radius:50%;background:linear-gradient(135deg,#9a86d2,#75d8d7);color:#fff;font-size:32px;font-weight:700}.runtime-state{position:absolute;inset:0;display:grid;place-content:center;gap:8px;padding:28px;text-align:center;background:color-mix(in srgb,var(--panel) 88%,transparent)}.runtime-state strong{font-size:16px}.runtime-state span{max-width:360px;color:var(--muted);font-size:12px;line-height:1.6}
</style>
