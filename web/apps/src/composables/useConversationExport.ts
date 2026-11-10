import { computed, ref, type Ref } from "vue";
import type { ChatMessage, Conversation } from "@aria/shared";
import {
  copyMarkdown,
  downloadMarkdown,
  exportFileName,
  formatConversationMarkdown,
} from "../export";

/**
 * 会话导出：整会话下载 .md；选择模式下勾选若干条，复制或下载节选，
 * 方便把对话粘贴给外部 AI / 协作者分析。
 */
export function useConversationExport(options: {
  activeId: Ref<string | null>;
  activeMessages: Ref<ChatMessage[]>;
  conversations: Ref<Conversation[]>;
  archivedConversations: Ref<Conversation[]>;
  messagesOf: (id: string) => ChatMessage[];
  pullMessagesAfter: (id: string, afterSeq: number) => Promise<void>;
  setStatus: (text: string, error?: boolean) => void;
}) {
  const selectMode = ref(false);
  const selectedMessageIds = ref(new Set<string>());

  const selectableMessages = computed(() =>
    options.activeMessages.value.filter((message) => message.role !== "system"),
  );
  const selectedMessages = computed(() =>
    selectableMessages.value.filter((message) => selectedMessageIds.value.has(message.id)),
  );

  function toggleSelectMode() {
    selectMode.value = !selectMode.value;
    selectedMessageIds.value = new Set();
  }

  function toggleMessageSelected(id: string) {
    const next = new Set(selectedMessageIds.value);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedMessageIds.value = next;
  }

  function selectAllMessages() {
    selectedMessageIds.value = new Set(selectableMessages.value.map((message) => message.id));
  }

  function conversationTitleOf(id: string | null): string {
    const conversation = [...options.conversations.value, ...options.archivedConversations.value]
      .find((item) => item.id === id);
    return conversation?.title || "未命名会话";
  }

  async function exportFullConversation(id: string) {
    try {
      options.setStatus("正在导出会话…");
      // 从头分页拉全量，避免只导出当前已加载的部分。
      await options.pullMessagesAfter(id, 0);
      const messages = options.messagesOf(id).filter((message) => message.role !== "system");
      if (!messages.length) {
        options.setStatus("该会话还没有可导出的消息", true);
        return;
      }
      const title = conversationTitleOf(id);
      downloadMarkdown(exportFileName(title), formatConversationMarkdown({ conversationTitle: title, messages }));
      options.setStatus(`已导出 ${messages.length} 条消息`);
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "导出失败", true);
    }
  }

  async function exportSelected(action: "copy" | "download") {
    const messages = selectedMessages.value;
    if (!messages.length) {
      options.setStatus("请先勾选要导出的消息", true);
      return;
    }
    const title = conversationTitleOf(options.activeId.value);
    const markdown = formatConversationMarkdown({ conversationTitle: title, messages, excerptOnly: true });
    if (action === "copy") {
      const ok = await copyMarkdown(markdown);
      options.setStatus(
        ok ? `已复制 ${messages.length} 条消息的 Markdown，可直接粘贴` : "复制失败，请改用下载",
        !ok,
      );
    } else {
      downloadMarkdown(exportFileName(title, true), markdown);
      options.setStatus(`已下载 ${messages.length} 条消息的节选`);
    }
  }

  return {
    selectMode,
    selectedMessageIds,
    selectableMessages,
    toggleSelectMode,
    toggleMessageSelected,
    selectAllMessages,
    exportFullConversation,
    exportSelected,
  };
}
