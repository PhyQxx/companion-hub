<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ChatApi, type MailAttachment } from "@aria/shared";

const props = defineProps<{ token: string }>();
const api = new ChatApi();
const attachments = ref<MailAttachment[]>([]);
const uploading = ref(false);
const error = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
let stopped = false;
let timer: ReturnType<typeof setTimeout> | undefined;

async function load() {
  try {
    const result = await api.listMailAttachments(props.token);
    if (!stopped) {
      attachments.value = result;
      if (result.length) error.value = "";
    }
  } catch {
    if (!stopped && attachments.value.length) error.value = "附件列表暂时无法同步。";
  }
}
async function poll() {
  await load();
  if (!stopped) timer = setTimeout(poll, 10000);
}
async function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    error.value = "附件不能超过 10MB。";
    return;
  }
  uploading.value = true;
  error.value = "";
  try {
    await api.uploadMailAttachment(props.token, file);
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "附件上传失败。";
  } finally {
    uploading.value = false;
  }
}
async function discard(id: string) {
  try {
    await api.discardMailAttachment(props.token, id);
    await load();
  } catch {
    error.value = "移除失败，请重试。";
  }
}
onMounted(poll);
onBeforeUnmount(() => {
  stopped = true;
  clearTimeout(timer);
});
</script>

<template>
  <section v-if="attachments.length || uploading || error" class="mail-attachments" aria-label="待发送附件">
    <p v-if="error" role="alert">{{ error }}</p>
    <label class="attach-button" title="上传附件，之后告诉 Aria 随邮件发送">
      📎{{ uploading ? " 上传中…" : " 附件" }}
      <input type="file" hidden :disabled="uploading" @change="onFilePicked" />
    </label>
    <span v-for="item in attachments" :key="item.id" class="chip">
      {{ item.filename }}
      <button type="button" :aria-label="`移除 ${item.filename}`" @click="discard(item.id)">×</button>
    </span>
  </section>
</template>

<style scoped>
.mail-attachments { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 0 2px; }
.attach-button {
  display: inline-flex; align-items: center; min-height: 30px; padding: 2px 10px;
  border: 1px dashed var(--line); border-radius: 14px; cursor: pointer;
  color: var(--muted); font-size: 12px; user-select: none;
}
.attach-button:hover { border-color: var(--accent); color: var(--accent); }
.chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
  border-radius: 14px; background: var(--panel); border: 1px solid var(--line); font-size: 12px;
}
.chip button { border: 0; background: transparent; cursor: pointer; color: var(--muted); font-size: 13px; line-height: 1; padding: 0 2px; }
.chip button:hover { color: var(--danger, #d33); }
</style>
