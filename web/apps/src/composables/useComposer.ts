import { nextTick, type Ref } from "vue";
import {
  matchesLocationIntent,
  VoiceSocket,
  type ClientLocationPayload,
  type PrivacyLevel,
} from "@aria/shared";
import type { StreamingState } from "./useVoice";

/** 发送域：文字发送（含定位与语音播报旁路）、地点确认续问与流式取消。 */
export function useComposer(options: {
  canSend: Ref<boolean>;
  activeId: Ref<string | null>;
  draft: Ref<string>;
  sendPending: Ref<boolean>;
  streaming: Ref<StreamingState | null>;
  textReplyVoice: Ref<boolean>;
  unlockVoicePlayback: () => void;
  appendPendingUserMessage: (conversationId: string, text: string) => void;
  locationEnabled: Ref<boolean>;
  privacy: Ref<PrivacyLevel>;
  setStatus: (text: string, error?: boolean) => void;
  resolveLocationForText: (text: string) => Promise<ClientLocationPayload | null>;
  ensureVoiceSocket: () => Promise<VoiceSocket>;
  voiceBusy: Ref<boolean>;
  voiceStatus: Ref<string>;
  revokePendingUserMessages: (conversationId: string, minAgeMs?: number) => string | null;
  sendMessage: (
    conversationId: string,
    text: string,
    privacyLevel: PrivacyLevel,
    location?: ClientLocationPayload | null,
  ) => void;
  cancel: (generationId: string) => void;
}) {
  async function chooseLocation(name: string) {
    options.draft.value = `请使用地点“${name}”继续刚才的查询`;
    await nextTick();
    if (options.canSend.value) await send();
  }

  async function send() {
    if (!options.canSend.value || !options.activeId.value) return;
    // 已记住播报开关时，本次发送点击是恢复移动端音频权限的机会。
    if (options.textReplyVoice.value) options.unlockVoicePlayback();
    const conversationId = options.activeId.value;
    const text = options.draft.value.trim();
    options.sendPending.value = true;
    options.draft.value = "";
    // 乐观显示：立即落占位消息；定位等待与服务端管线期间消息始终可见。
    options.appendPendingUserMessage(conversationId, text);
    options.setStatus(
      matchesLocationIntent(text) && options.locationEnabled.value && options.privacy.value !== "L2"
        ? "正在获取位置并发送…"
        : "正在发送…",
    );
    const location = await options.resolveLocationForText(text);
    if (options.textReplyVoice.value) {
      try {
        const current = await options.ensureVoiceSocket();
        options.voiceBusy.value = true;
        options.voiceStatus.value = "文字已提交，正在生成语音回复…";
        current.submitText(text, location);
      } catch (error) {
        options.voiceBusy.value = false;
        if (!options.draft.value) {
          options.draft.value = options.revokePendingUserMessages(conversationId) ?? text;
        }
        options.setStatus(error instanceof Error ? error.message : "语音连接失败", true);
      } finally {
        options.sendPending.value = false;
      }
      return;
    }
    options.sendMessage(conversationId, text, options.privacy.value, location);
    options.sendPending.value = false;
  }

  function cancelStreaming() {
    if (options.streaming.value) options.cancel(options.streaming.value.generationId);
  }

  return { chooseLocation, send, cancelStreaming };
}
