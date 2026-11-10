import { computed, type Ref } from "vue";
import { ChatApi, type ChatMessage, type Conversation, type PrivacyLevel } from "@aria/shared";

// 同步收敛后回收占位消息的最小年龄：刚发出的消息可能仍在服务端处理中，
// 立即回收会把已送达的消息误恢复到输入框。
export const PENDING_REVOKE_MIN_AGE_MS = 20_000;

/**
 * 消息域：按会话分组的消息存储、乐观占位、合并去重、增量补拉与刷新。
 * 占位 id/seq 与服务端空间隔离，避免污染 sync 游标。
 */
export function useMessages(options: {
  api: ChatApi;
  token: Ref<string>;
  activeId: Ref<string | null>;
  conversations: Ref<Conversation[]>;
  messagesByConversation: Map<string, ChatMessage[]>;
  privacy: Ref<PrivacyLevel>;
  setStatus: (text: string, error?: boolean) => void;
  scrollToEnd: () => Promise<void>;
}) {
  const LOCAL_MESSAGE_PREFIX = "local:";
  const LOCAL_SEQ_BASE = Number.MAX_SAFE_INTEGER - 100_000;
  let localMessageCount = 0;

  function isLocalMessage(message: ChatMessage): boolean {
    return message.id.startsWith(LOCAL_MESSAGE_PREFIX);
  }

  function appendPendingUserMessage(conversationId: string, text: string): void {
    localMessageCount += 1;
    const placeholder: ChatMessage = {
      id: `${LOCAL_MESSAGE_PREFIX}${Date.now().toString(36)}-${localMessageCount}`,
      conversation_id: conversationId,
      turn_id: "",
      seq: LOCAL_SEQ_BASE + localMessageCount,
      role: "user",
      content: text,
      privacy_level: options.privacy.value,
      generation_id: null,
      decision_meta: null,
      created_at: new Date().toISOString(),
    };
    mergeMessages(conversationId, [placeholder]);
    if (conversationId === options.activeId.value) void options.scrollToEnd();
  }

  /** 回收未被服务端确认的占位消息，返回最后一条的内容（便于恢复到输入框）。 */
  function revokePendingUserMessages(conversationId: string, minAgeMs = 0): string | null {
    const messages = options.messagesByConversation.get(conversationId);
    if (!messages?.some(isLocalMessage)) return null;
    const now = Date.now();
    let restored: string | null = null;
    const kept = messages.filter((message) => {
      if (!isLocalMessage(message)) return true;
      if (minAgeMs > 0 && now - Date.parse(message.created_at) < minAgeMs) return true;
      restored = message.content;
      return false;
    });
    if (kept.length === messages.length) return null;
    options.messagesByConversation.set(conversationId, kept);
    return restored;
  }

  function mergeMessages(
    conversationId: string,
    incoming: ChatMessage[],
    replace = false,
  ): ChatMessage[] {
    const merged = new Map<string, ChatMessage>();
    if (!replace) {
      for (const message of options.messagesByConversation.get(conversationId) ?? []) {
        merged.set(message.id, message);
      }
    }
    // 真实 user 消息落定后，按内容配对移除最早的占位符，避免回显时重复显示。
    const placeholders = [...merged.values()].filter((message) => isLocalMessage(message));
    for (const message of incoming) {
      if (message.role === "user" && !isLocalMessage(message)) {
        const index = placeholders.findIndex((item) => item.content === message.content);
        if (index >= 0) {
          merged.delete(placeholders[index]!.id);
          placeholders.splice(index, 1);
        }
      }
      merged.set(message.id, message);
    }
    const messages = [...merged.values()].sort((left, right) => left.seq - right.seq);
    options.messagesByConversation.set(conversationId, messages);
    const conversation = options.conversations.value.find((item) => item.id === conversationId);
    if (conversation && messages.length) {
      const lastServerSeq = messages.reduce(
        (latest, message) => (isLocalMessage(message) ? latest : Math.max(latest, message.seq)),
        0,
      );
      if (lastServerSeq) conversation.last_seq = Math.max(conversation.last_seq, lastServerSeq);
    }
    return messages;
  }

  async function pullMessagesAfter(conversationId: string, afterSeq: number): Promise<void> {
    const pageSize = 500;
    let cursor = afterSeq;
    while (true) {
      const page = await options.api.listMessages(options.token.value, conversationId, cursor, pageSize);
      mergeMessages(conversationId, page);
      if (page.length < pageSize) return;
      const nextCursor = page.reduce((latest, message) => Math.max(latest, message.seq), cursor);
      if (nextCursor <= cursor) return;
      cursor = nextCursor;
    }
  }

  async function refreshMessages(conversationId: string) {
    try {
      await pullMessagesAfter(conversationId, lastSeqOf(conversationId));
      if (conversationId === options.activeId.value) await options.scrollToEnd();
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "刷新语音消息失败", true);
    }
  }

  function lastSeqOf(id: string) {
    const messages = options.messagesByConversation.get(id) ?? [];
    // 只统计服务端确认过的序列：占位符是隔离的高位 seq，计入会让 sync 游标跳过真实消息。
    return messages.reduce(
      (latest, message) => (isLocalMessage(message) ? latest : Math.max(latest, message.seq ?? 0)),
      0,
    );
  }

  const activeMessages = computed(() =>
    options.activeId.value ? (options.messagesByConversation.get(options.activeId.value) ?? []) : [],
  );

  return {
    activeMessages,
    isLocalMessage,
    appendPendingUserMessage,
    revokePendingUserMessages,
    mergeMessages,
    pullMessagesAfter,
    refreshMessages,
    lastSeqOf,
  };
}
