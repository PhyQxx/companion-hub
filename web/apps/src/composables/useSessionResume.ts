import { type Ref } from "vue";
import { ApiError } from "@aria/shared";

/** 会话续接与网络状态：页面回到前台/恢复在线时补拉消息并重建连接。 */
export function useSessionResume(options: {
  token: Ref<string>;
  activeId: Ref<string | null>;
  networkOnline: Ref<boolean>;
  setStatus: (text: string, error?: boolean) => void;
  syncThemePreference: () => Promise<void>;
  pullMessagesAfter: (conversationId: string, afterSeq: number) => Promise<void>;
  lastSeqOf: (id: string) => number;
  scrollToEnd: () => Promise<void>;
  logout: () => Promise<void>;
  socketReady: Ref<boolean>;
  connectSocket: () => void;
  sync: (conversationId: string, afterSeq: number) => void;
  closeSocket: () => void;
  voiceRecording: Ref<boolean>;
  voiceLive: Ref<boolean>;
  closeVoice: () => Promise<void>;
}) {
  let resumePromise: Promise<void> | null = null;

  async function resumeSession() {
    if (!options.token.value || document.hidden || !navigator.onLine) return;
    if (resumePromise) return resumePromise;
    resumePromise = (async () => {
      await options.syncThemePreference();
      const conversationId = options.activeId.value;
      if (conversationId) {
        try {
          await options.pullMessagesAfter(conversationId, options.lastSeqOf(conversationId));
          await options.scrollToEnd();
        } catch (error) {
          if (error instanceof ApiError && error.status === 401) {
            await options.logout();
            options.setStatus("会话已过期，请重新登录", true);
            return;
          }
        }
      }
      if (!options.socketReady.value) options.connectSocket();
      else if (conversationId) options.sync(conversationId, options.lastSeqOf(conversationId));
    })().finally(() => {
      resumePromise = null;
    });
    return resumePromise;
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      // 后台页面必须停止麦克风采集，但纯文字回复播报不应因切换浏览器
      // Tab 而中断。closeVoice() 会清空播放队列并停止当前音频，因此这里只在
      // PTT 正在录音或连续通话仍占用麦克风时关闭整条语音链路。
      if (options.voiceRecording.value || options.voiceLive.value) void options.closeVoice();
      return;
    }
    void resumeSession();
  }

  function handleOnline() {
    options.networkOnline.value = true;
    options.setStatus("网络已恢复，正在同步…");
    void resumeSession();
  }

  function handleOffline() {
    options.networkOnline.value = false;
    options.closeSocket();
    options.setStatus("当前离线，消息不会发送", true);
  }

  return { resumeSession, handleVisibilityChange, handleOnline, handleOffline };
}
