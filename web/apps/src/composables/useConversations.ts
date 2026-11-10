import { type Ref } from "vue";
import {
  ApiError,
  ChatApi,
  type ChatMessage,
  type Conversation,
  VoiceSocket,
} from "@aria/shared";

/** 会话域：列表加载、打开/创建/归档/恢复/删除与进入聊天时的初始化编排。 */
export function useConversations(options: {
  api: ChatApi;
  token: Ref<string>;
  activeId: Ref<string | null>;
  conversations: Ref<Conversation[]>;
  archivedConversations: Ref<Conversation[]>;
  showArchivedConversations: Ref<boolean>;
  visibleConversations: Ref<Conversation[]>;
  mobileConversationsOpen: Ref<boolean>;
  messagesByConversation: Map<string, ChatMessage[]>;
  setStatus: (text: string, error?: boolean) => void;
  closeVoice: () => Promise<void>;
  sync: (conversationId: string, afterSeq: number) => void;
  connectSocket: () => void;
  scrollToEnd: () => Promise<void>;
  selectMode: Ref<boolean>;
  selectedMessageIds: Ref<Set<string>>;
  textReplyVoice: Ref<boolean>;
  ensureVoiceSocket: () => Promise<VoiceSocket>;
  voiceStatus: Ref<string>;
  logout: () => Promise<void>;
  mergeMessages: (conversationId: string, incoming: ChatMessage[], replace?: boolean) => ChatMessage[];
  lastSeqOf: (id: string) => number;
  syncThemePreference: () => Promise<void>;
  loadRuntimeMeta: () => Promise<void>;
  loadAvatars: () => Promise<void>;
}) {
  async function enterChat() {
    await options.syncThemePreference();
    await options.loadRuntimeMeta();
    await options.loadAvatars();
    await loadConversations();
    options.connectSocket();
  }

  async function loadConversations() {
    try {
      [options.conversations.value, options.archivedConversations.value] = await Promise.all([
        options.api.listConversations(options.token.value, "active"),
        options.api.listConversations(options.token.value, "archived"),
      ]);
      if (!options.activeId.value && options.conversations.value.length) {
        await openConversation(options.conversations.value[0]!.id);
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        await options.logout();
        options.setStatus("会话已过期，请重新登录", true);
        return;
      }
      options.setStatus(error instanceof Error ? error.message : "加载会话失败", true);
    }
  }

  async function openConversation(id: string) {
    if (options.activeId.value !== id) await options.closeVoice();
    // 切换会话后勾选集合不再有意义，退出选择模式。
    options.selectMode.value = false;
    options.selectedMessageIds.value = new Set();
    options.activeId.value = id;
    options.mobileConversationsOpen.value = false;
    if (!options.messagesByConversation.has(id)) {
      try {
        const messages = await options.api.listMessages(options.token.value, id);
        options.mergeMessages(id, messages, true);
      } catch (error) {
        options.setStatus(error instanceof Error ? error.message : "加载消息失败", true);
        return;
      }
    }
    options.sync(id, options.lastSeqOf(id));
    if (options.textReplyVoice.value) {
      void options.ensureVoiceSocket().catch((error) => {
        options.voiceStatus.value = error instanceof Error ? error.message : "语音连接失败";
      });
    }
    await options.scrollToEnd();
  }

  async function createConversation() {
    try {
      const conversation = await options.api.createConversation(
        options.token.value,
        `会话 ${options.conversations.value.length + 1}`,
      );
      options.conversations.value.unshift(conversation);
      options.messagesByConversation.set(conversation.id, []);
      await openConversation(conversation.id);
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "创建会话失败", true);
    }
  }

  async function selectConversationGroup(archived: boolean) {
    if (options.showArchivedConversations.value === archived) return;
    await options.closeVoice();
    options.showArchivedConversations.value = archived;
    options.activeId.value = null;
    const first = archived ? options.archivedConversations.value[0] : options.conversations.value[0];
    if (first) await openConversation(first.id);
  }

  async function removeConversation(id: string) {
    const conversation = [...options.conversations.value, ...options.archivedConversations.value]
      .find((item) => item.id === id);
    const label = conversation?.title ?? "该会话";
    if (!confirm(`删除「${label}」？消息与由它沉淀的记忆会被一并删除，不可恢复。`)) return;
    try {
      if (options.activeId.value === id) await options.closeVoice();
      await options.api.deleteConversation(options.token.value, id);
      options.conversations.value = options.conversations.value.filter((item) => item.id !== id);
      options.archivedConversations.value = options.archivedConversations.value.filter((item) => item.id !== id);
      options.messagesByConversation.delete(id);
      if (options.activeId.value === id) {
        options.activeId.value = null;
        const first = options.visibleConversations.value[0];
        if (first) await openConversation(first.id);
      }
      options.setStatus("会话已删除");
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "删除失败", true);
    }
  }

  async function archiveConversation(id: string) {
    try {
      if (options.activeId.value === id) await options.closeVoice();
      const archived = await options.api.archiveConversation(options.token.value, id);
      options.conversations.value = options.conversations.value.filter((item) => item.id !== id);
      options.archivedConversations.value.unshift(archived);
      if (options.activeId.value === id) {
        options.activeId.value = null;
        if (options.conversations.value.length) await openConversation(options.conversations.value[0]!.id);
      }
      options.setStatus("会话已归档，可在归档列表中恢复");
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "归档失败", true);
    }
  }

  async function restoreConversation(id: string) {
    try {
      const restored = await options.api.restoreConversation(options.token.value, id);
      options.archivedConversations.value = options.archivedConversations.value.filter((item) => item.id !== id);
      options.conversations.value.unshift(restored);
      if (options.activeId.value === id) {
        options.activeId.value = null;
        const first = options.archivedConversations.value[0];
        if (first) await openConversation(first.id);
      }
      options.setStatus("会话已恢复");
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "恢复失败", true);
    }
  }

  return {
    enterChat,
    loadConversations,
    openConversation,
    createConversation,
    selectConversationGroup,
    removeConversation,
    archiveConversation,
    restoreConversation,
  };
}
