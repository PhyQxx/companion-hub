<script setup lang="ts">
import type { ChatMessage } from "@aria/shared";
import MarkdownContent from "./MarkdownContent.vue";
import ToolResultCard from "./ToolResultCard.vue";
import { useMessagePresentation } from "./composables/useMessagePresentation";
import type { StreamingState } from "./composables/useVoice";

defineProps<{
  messages: ChatMessage[];
  activeId: string | null;
  selectMode: boolean;
  selectedIds: Set<string>;
  streaming: StreamingState | null;
  isLocal: (message: ChatMessage) => boolean;
  setMessagesRoot: (el: Element | null) => void;
}>();
const emit = defineEmits<{
  selectAll: [];
  exportSelected: [action: "copy" | "download"];
  toggleSelectMode: [];
  toggleMessageSelected: [id: string];
  chooseLocation: [name: string];
}>();
const { toolResultOf, emotionOf, personaVersionOf, recallLabelOf, messageTimeOf, emotionLabels } =
  useMessagePresentation();
</script>

<template>
  <div :ref="(el) => setMessagesRoot(el as Element | null)" class="messages">
    <div v-if="selectMode" class="select-bar">
      <strong>已选 {{ selectedIds.size }} 条</strong>
      <button type="button" @click="emit('selectAll')">全选</button>
      <button type="button" :disabled="!selectedIds.size" @click="emit('exportSelected', 'copy')">复制 Markdown</button>
      <button type="button" :disabled="!selectedIds.size" @click="emit('exportSelected', 'download')">下载 .md</button>
      <button class="ghost" type="button" @click="emit('toggleSelectMode')">退出选择</button>
    </div>
    <div v-if="!messages.length && activeId" class="empty">这个会话还没有消息。</div>
    <template v-for="message in messages" :key="message.id">
      <div
        v-if="message.role !== 'system'"
        class="message"
        :class="[message.role, { selected: selectMode && selectedIds.has(message.id), pending: isLocal(message) }]"
      >
        <label
          v-if="selectMode"
          class="select-check"
          :title="selectedIds.has(message.id) ? '取消选择' : '选择该消息'"
        >
          <input
            type="checkbox"
            :checked="selectedIds.has(message.id)"
            @change="emit('toggleMessageSelected', message.id)"
          />
        </label>
        <div
          class="bubble"
          :class="{ selecting: selectMode }"
          @click="selectMode && emit('toggleMessageSelected', message.id)"
        >
          <MarkdownContent :content="message.content" />
          <ToolResultCard
            v-if="message.role === 'assistant' && toolResultOf(message)"
            :result="toolResultOf(message)!"
            @choose-location="emit('chooseLocation', $event)"
          />
          <span v-if="message.role === 'assistant' && emotionOf(message)" class="emotion">{{ emotionOf(message) }}</span>
          <span v-if="message.role === 'assistant' && personaVersionOf(message)" class="persona-badge">P v{{ personaVersionOf(message) }}</span>
          <span v-if="message.role === 'assistant' && recallLabelOf(message)" class="recall-badge">{{ recallLabelOf(message) }}</span>
          <span v-if="messageTimeOf(message)" class="message-time">{{ messageTimeOf(message) }}</span>
        </div>
      </div>
    </template>
    <div v-if="streaming && streaming.conversationId === activeId" class="message assistant">
      <div class="bubble streaming">
        {{ streaming.text }}▋
        <span v-if="streaming.emotion" class="emotion">{{ emotionLabels[streaming.emotion] ?? streaming.emotion }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.messages { overflow-y:auto; padding:20px clamp(20px,3vw,42px); }
.empty { height: 100%; display: grid; place-items: center; color: var(--muted); }
.message { width:fit-content; max-width:min(78%,820px); margin:0 auto 14px 0; display:flex; align-items:flex-start; gap:8px; }
.message.user { margin-right:0; margin-left:auto; justify-content:flex-end; }
.select-check { display: flex; padding: 12px 0 0 2px; cursor: pointer; accent-color: var(--accent); }
.select-check input { cursor: pointer; }
.message.selected .bubble { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.bubble.selecting { cursor: pointer; }
.bubble.selecting :deep(a), .bubble.selecting :deep(button) { pointer-events: none; }
.select-bar { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; padding: 8px 10px; border: 1px solid var(--accent); border-radius: 10px; background: color-mix(in srgb, var(--panel) 92%, transparent); backdrop-filter: blur(8px); }
.select-bar strong { color: var(--accent); font-size: 12px; }
.select-bar button { padding: 5px 10px; font-size: 12px; }
.bubble { min-width:0; line-height:1.55; padding:10px 14px; border:1px solid var(--line); border-radius:6px 16px 16px 16px; background:var(--panel); box-shadow:0 4px 14px color-mix(in srgb,var(--text) 5%,transparent); position:relative; overflow-wrap:anywhere; }
.user .bubble { border-radius:16px 6px 16px 16px; background:var(--message-user-bg); border-color:var(--message-user-bg); color:#fff; box-shadow:0 7px 18px color-mix(in srgb,var(--message-user-bg) 22%,transparent); }
.user .bubble :deep(.markdown-content a) { color:inherit; }
/* 本地占位消息：半透明表示尚未被服务端确认，回显后恢复 */
.message.pending .bubble { opacity:.62; }
.bubble { transition:opacity 180ms ease; }
.bubble.streaming { white-space:pre-wrap; }
.bubble.streaming::after { content: ""; }
.emotion { display: inline-block; margin-left: 8px; font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; vertical-align: 1px; }
.persona-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); opacity:.75; }
.recall-badge { display:inline-block; margin-left:6px; font-size:10px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:0 7px; opacity:.8; }
.message-time { margin-left:6px; color:var(--muted); font-size:11px; white-space:nowrap; }
.user .message-time { color:#fff; font-weight:500; }

@media (max-width: 760px) {
  .messages { padding:14px 12px 20px; overscroll-behavior:contain; }
  .message { max-width:92%; margin-bottom:10px; }
  .bubble { padding:10px 12px; border-radius:16px; }
}
</style>
