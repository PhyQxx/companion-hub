import { computed, type Ref } from "vue";
import {
  ChatApi,
  type AgentReplyControl,
  type AvatarChoice,
  type ChatMessage,
} from "@aria/shared";
import type { StreamingState } from "./useVoice";

export interface AvatarMeta {
  instanceId: string;
  packId: string;
  name: string;
  engine: "static" | "live2d" | "vrm" | "abstract";
  assets: { thumbnail?: string; emotions?: Record<string, string>; model?: string };
}

/** 伴侣形象：运行时元信息、形象列表与切换、情绪/表情/说话/图片派生状态。 */
export function useAvatar(options: {
  api: ChatApi;
  token: Ref<string>;
  personaMeta: Ref<{ version: number; name: string } | null>;
  avatarMeta: Ref<AvatarMeta | null>;
  avatars: Ref<AvatarChoice[]>;
  avatarSwitchBusy: Ref<boolean>;
  streaming: Ref<StreamingState | null>;
  activeMessages: Ref<ChatMessage[]>;
  voiceViseme: Ref<number>;
  voiceBusy: Ref<boolean>;
  locationPolicy: Ref<"ask_each_time" | "allow_session">;
  emotionKeyOf: (message: ChatMessage) => string | null;
  agentReplyOf: (message: ChatMessage) => AgentReplyControl | null;
  setStatus: (text: string, error?: boolean) => void;
}) {
  const avatarEmotion = computed(() => {
    if (options.streaming.value?.emotion) return options.streaming.value.emotion;
    const latest = [...options.activeMessages.value]
      .reverse()
      .find((message) => message.role === "assistant" && options.emotionKeyOf(message));
    return latest ? (options.emotionKeyOf(latest) ?? "neutral") : "neutral";
  });
  const avatarExpression = computed(() => {
    if (options.streaming.value?.expression) return options.streaming.value.expression;
    const latest = [...options.activeMessages.value]
      .reverse()
      .find((message) => message.role === "assistant" && options.agentReplyOf(message)?.expressions?.length);
    return latest ? (options.agentReplyOf(latest)?.expressions?.[0] ?? null) : null;
  });
  const avatarSpeaking = computed(
    () => options.voiceViseme.value > 0.001 || options.voiceBusy.value || Boolean(options.streaming.value?.text),
  );
  const avatarImageUrl = computed(() => {
    const assets = options.avatarMeta.value?.assets;
    return assets?.emotions?.[avatarEmotion.value] ?? assets?.thumbnail ?? null;
  });

  async function loadRuntimeMeta() {
    try {
      const runtime = await options.api.runtimeMeta(options.token.value || null);
      options.personaMeta.value = runtime.persona
        ? { version: runtime.persona.version, name: runtime.persona.name }
        : null;
      options.avatarMeta.value = runtime.avatar
        ? {
            instanceId: runtime.avatar.instance_id,
            packId: runtime.avatar.pack_id,
            name: runtime.avatar.name,
            engine: runtime.avatar.engine,
            assets: runtime.avatar.assets,
          }
        : null;
      if (runtime.location_policy) options.locationPolicy.value = runtime.location_policy.precise;
    } catch {
      options.personaMeta.value = null;
      options.avatarMeta.value = null;
    }
  }

  async function loadAvatars() {
    if (!options.token.value) return;
    try {
      options.avatars.value = await options.api.listAvatars(options.token.value);
    } catch {
      options.avatars.value = [];
    }
  }

  async function switchAvatar(event: Event) {
    const instanceId = (event.target as HTMLSelectElement).value;
    if (!instanceId || instanceId === options.avatarMeta.value?.instanceId) return;
    options.avatarSwitchBusy.value = true;
    try {
      await options.api.switchAvatar(options.token.value, instanceId);
      await Promise.all([loadRuntimeMeta(), loadAvatars()]);
      options.setStatus("形象已切换，记忆与当前会话保持不变");
    } catch (error) {
      options.setStatus(error instanceof Error ? error.message : "形象切换失败", true);
    } finally {
      options.avatarSwitchBusy.value = false;
    }
  }

  return {
    avatarEmotion,
    avatarExpression,
    avatarSpeaking,
    avatarImageUrl,
    loadRuntimeMeta,
    loadAvatars,
    switchAvatar,
  };
}
