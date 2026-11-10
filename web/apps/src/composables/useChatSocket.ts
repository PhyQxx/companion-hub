import { ref } from "vue";
import {
  ChatSocket,
  type ClientLocationPayload,
  type PrivacyLevel,
  type SocketEvent,
} from "@aria/shared";

/**
 * 聊天 WebSocket 生命周期：建连/关闭/断线重连退避/收发消息。
 * 事件语义（消息合并等）由调用方经 onEvent 注入，本 composable 只负责连接与传输。
 */
export function useChatSocket(options: {
  token: () => string;
  activeConversationId: () => string | null;
  onEvent: (event: SocketEvent) => void;
  setStatus: (text: string, error?: boolean) => void;
  lastSeqOf: (conversationId: string) => number;
}) {
  const socketReady = ref(false);
  let socket: ChatSocket | null = null;
  let reconnectTimer: number | null = null;
  let reconnectAttempts = 0;

  function closeSocket() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    socket?.close();
    socket = null;
    socketReady.value = false;
  }

  /** 建立实时连接；失败时保留基础 REST 可用性并提示 */
  function connectSocket() {
    closeSocket();
    let nextSocket: ChatSocket;
    nextSocket = new ChatSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat",
      options.token(),
      {
        onEvent: (event) => {
          if (socket === nextSocket) options.onEvent(event);
        },
        onClose: (code) => {
          if (socket === nextSocket) handleClose(code);
        },
      },
    );
    socket = nextSocket;
    nextSocket
      .connect()
      .then(() => {
        if (socket !== nextSocket) return;
        socketReady.value = true;
        reconnectAttempts = 0;
        options.setStatus("已连接");
        const activeId = options.activeConversationId();
        if (activeId) socket?.sync(activeId, options.lastSeqOf(activeId));
      })
      .catch(() => {
        if (socket === nextSocket) options.setStatus("实时连接失败，仍可基础使用", true);
      });
  }

  function handleClose(code: number) {
    socketReady.value = false;
    if (!options.token() || code === 1000) return;
    if (reconnectAttempts >= 3) {
      options.setStatus("实时连接已断开", true);
      return;
    }
    reconnectAttempts += 1;
    reconnectTimer = window.setTimeout(connectSocket, 3000);
  }

  function sendMessage(
    conversationId: string,
    text: string,
    privacyLevel: PrivacyLevel,
    location?: ClientLocationPayload | null,
  ) {
    socket?.sendMessage(conversationId, text, privacyLevel, location);
  }

  function cancel(generationId: string) {
    socket?.cancel(generationId);
  }

  function sync(conversationId: string, afterSeq: number) {
    socket?.sync(conversationId, afterSeq);
  }

  return {
    socketReady,
    connectSocket,
    closeSocket,
    sendMessage,
    cancel,
    sync,
  };
}
