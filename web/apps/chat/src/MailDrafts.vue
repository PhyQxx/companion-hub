<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ChatApi, type MailDraft } from "@aria/shared";
const props = defineProps<{ token: string }>();
const api = new ChatApi();
const drafts = ref<MailDraft[]>([]);
const busy = ref(false);
const error = ref("");
const loadError = ref("");
const dismissed = new Set<string>();
let timer: ReturnType<typeof setTimeout> | undefined;
let stopped = false;
async function load() {
  try {
    const result = await api.listMailDrafts(props.token);
    if (!stopped) {
      drafts.value = result.filter(d => d.status !== "cancelled" && !dismissed.has(d.id));
      loadError.value = "";
    }
  } catch { if (!stopped && drafts.value.length) loadError.value = "邮件预览暂时无法加载，请稍后重试。"; }
}
async function poll() {
  await load();
  if (!stopped) timer = setTimeout(poll, 5000);
}
async function act(draft: MailDraft, action: "confirm" | "cancel") {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api.mailDraftAction(props.token, draft.id, action, draft.digest);
  } catch {
    error.value = action === "confirm"
      ? "发送未完成或结果未知，请先核对邮箱；不要重复创建邮件发送。预览过期时需重新准备。"
      : "取消未完成，请刷新后核对状态。";
  } finally { await load(); busy.value = false; }
}
function dismiss(id: string) {
  dismissed.add(id);
  drafts.value = drafts.value.filter(d => d.id !== id);
}
onMounted(poll);
onBeforeUnmount(() => { stopped = true; clearTimeout(timer); });
const labels: Record<string, string> = {
  pending: "待确认邮件", sending: "正在发送，请勿重复操作", sent: "邮件已发送",
  unknown_outcome: "发送结果未知，请核对邮箱", cancelled: "已取消",
};
</script>

<template>
  <section v-if="drafts.length || error" class="mail-drafts" aria-label="邮件确认">
    <p v-if="error || loadError" role="alert">{{ error || loadError }}</p>
    <article v-for="draft in drafts" :key="draft.id">
      <strong>{{ labels[draft.status] ?? draft.status }}</strong>
      <p>收件人：{{ draft.content.to.join("，") }}</p>
      <p v-if="draft.content.cc.length">抄送：{{ draft.content.cc.join("，") }}</p>
      <p>主题：{{ draft.content.subject }}</p>
      <p v-if="draft.content.attachments?.length">
        附件：{{ draft.content.attachments.map(a => a.filename).join("，") }}
      </p>
      <pre>{{ draft.content.body }}</pre>
      <template v-if="draft.status === 'pending'">
        <small>请核对全文。有效期至 {{ new Date(draft.expires_at).toLocaleTimeString() }}；修改内容需重新准备预览。</small>
        <div class="actions">
          <button :disabled="busy" @click="act(draft, 'confirm')">确认发送</button>
          <button :disabled="busy" @click="act(draft, 'cancel')">取消</button>
        </div>
      </template>
      <button v-else-if="draft.status !== 'sending'" @click="dismiss(draft.id)">收起</button>
    </article>
  </section>
</template>

<style scoped>
.mail-drafts { padding: 12px; max-height: 45vh; overflow: auto; }
article { border: 1px solid currentColor; border-radius: 12px; padding: 14px; margin: 8px 0; }
p { margin: 8px 0; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; max-height: 22vh; overflow: auto; }
.actions { display: flex; gap: 12px; margin-top: 10px; }
button { min-height: 44px; padding: 8px 16px; cursor: pointer; }
</style>
