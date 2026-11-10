import { ref, type Ref } from "vue";
import { VoiceSocket, type ClientLocationPayload, type PrivacyLevel, type VoiceControlEvent } from "@aria/shared";
import {
  PcmMicrophoneCapture,
  VoicePlaybackQueue,
  type VoiceSentenceMeta,
  type VoiceVisemeFrame,
} from "../voice";

export interface StreamingState {
  conversationId: string;
  generationId: string;
  text: string;
  emotion: string | null;
  expression?: string | null;
}

const TEXT_REPLY_VOICE_KEY = "ariaTextReplyVoice";

const asrUnavailableLabels: Record<string, string> = {
  not_configured: "后台尚未启用语音识别",
  local_asr_required: "当前是 L2，仅允许本地语音识别",
  faster_whisper_not_installed: "本机尚未安装 faster-whisper",
  model_load_failed: "本地语音模型加载失败",
  transcription_failed: "本地语音转写失败",
  provider_error: "语音识别服务调用失败",
};

const voiceErrorLabels: Record<string, string> = {
  invalid_audio_frame: "音频片段格式或大小不合适，请重新开始录音",
  utterance_too_large: "单次录音最长两分钟，请分段发送",
};

/**
 * 语音链路：连接/事件处理/PTT 录音/连续对话（电话模式）/打断/文字回复播报。
 * 消息域状态（streaming/draft 等）由调用方以参数注入，保持单向依赖。
 */
export function useVoice(options: {
  token: () => string;
  activeId: Ref<string | null>;
  privacy: Ref<PrivacyLevel>;
  streaming: Ref<StreamingState | null>;
  draft: Ref<string>;
  setStatus: (text: string, error?: boolean) => void;
  scrollToEnd: () => Promise<void>;
  refreshMessages: (conversationId: string) => Promise<void>;
  revokePendingUserMessages: (conversationId: string, minAgeMs?: number) => string | null;
  loadRuntimeMeta: () => Promise<void>;
  freshCachedLocation: () => ClientLocationPayload | null;
  generationFailureText: (reason: unknown, prefix?: string) => string;
}) {
  const voiceReady = ref(false);
  const voiceAsrConfigured = ref<boolean | null>(null);
  const voiceAsrBlockMessage = ref("");
  const voiceRecording = ref(false);
  // 连续对话（电话模式）：麦克风常开，服务端自动断句即发送、说话即打断。
  const voiceLive = ref(false);
  const voiceBusy = ref(false);
  const voiceStatus = ref("语音未连接");
  const voiceTranscript = ref("");
  const voiceVadBackend = ref("");
  const voiceViseme = ref(0);
  const voiceTtsConfigured = ref<boolean | null>(null);
  const textReplyVoice = ref(localStorage.getItem(TEXT_REPLY_VOICE_KEY) === "1");

  let voiceSocket: VoiceSocket | null = null;
  let voiceSocketKey = "";
  let voiceConnectPromise: Promise<VoiceSocket> | null = null;
  let voiceConnectKey = "";
  let voiceSentence: {
    meta: VoiceSentenceMeta;
    chunks: ArrayBuffer[];
    visemes: VoiceVisemeFrame[];
  } | null = null;
  const microphone = new PcmMicrophoneCapture();
  const playback = new VoicePlaybackQueue();

  function unlockVoicePlayback() {
    void playback.unlock().then((unlocked) => {
      if (!unlocked) voiceStatus.value = "浏览器尚未允许播放声音，请关闭再开启“文字回复播报”";
    }).catch((error: unknown) => {
      voiceStatus.value = error instanceof Error
        ? `无法启用声音：${error.message}`
        : "浏览器尚未允许播放声音";
    });
  }

  async function closeVoice() {
    voiceRecording.value = false;
    voiceLive.value = false;
    voiceBusy.value = false;
    voiceReady.value = false;
    voiceAsrConfigured.value = null;
    voiceTtsConfigured.value = null;
    voiceAsrBlockMessage.value = "";
    voiceSocketKey = "";
    voiceConnectPromise = null;
    voiceConnectKey = "";
    voiceSentence = null;
    voiceSocket?.close();
    voiceSocket = null;
    playback.interrupt();
    await microphone.stop();
    voiceStatus.value = "语音未连接";
    voiceTranscript.value = "";
    voiceViseme.value = 0;
  }

  async function ensureVoiceSocket(continuous = false): Promise<VoiceSocket> {
    if (!options.activeId.value) throw new Error("请先选择会话");
    const conversationId = options.activeId.value;
    const privacyLevel = options.privacy.value;
    const key = `${conversationId}:${privacyLevel}:${continuous ? "live" : "auto"}`;
    if (
      voiceSocket &&
      voiceReady.value &&
      voiceSocketKey === key
    ) {
      return voiceSocket;
    }
    if (voiceConnectPromise && voiceConnectKey === key) return voiceConnectPromise;
    const connecting = (async () => {
      await closeVoice();
      voiceStatus.value = "正在连接语音…";
      const next = new VoiceSocket(
        (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/voice",
        options.token(),
        {
          onEvent: handleVoiceEvent,
          onAudio: handleVoiceAudio,
          onClose: () => {
            if (voiceSocket === next) {
              voiceReady.value = false;
              voiceBusy.value = false;
              voiceLive.value = false;
              if (options.streaming.value?.conversationId === options.activeId.value) {
                options.streaming.value = null;
              }
              voiceStatus.value = "语音连接已断开";
              options.setStatus("语音连接已断开，请重新发送", true);
            }
          },
        },
      );
      voiceSocket = next;
      voiceSocketKey = key;
      try {
        // 纯语音回合没有输入帧, hello 时带上新鲜缓存位置(不触发授权弹窗)。
        await next.connect(conversationId, privacyLevel, options.freshCachedLocation(), { continuous });
        return next;
      } catch (error) {
        if (voiceSocket === next) {
          voiceSocket = null;
          voiceSocketKey = "";
          voiceReady.value = false;
        }
        throw error;
      }
    })();
    voiceConnectPromise = connecting;
    voiceConnectKey = key;
    try {
      return await connecting;
    } finally {
      if (voiceConnectPromise === connecting) {
        voiceConnectPromise = null;
        voiceConnectKey = "";
      }
    }
  }

  function handleVoiceAudio(chunk: ArrayBuffer) {
    if (!voiceSentence) return;
    voiceSentence.chunks.push(chunk);
  }

  function handleVoiceEvent(event: VoiceControlEvent) {
    switch (event.type) {
      case "voice.ready":
        voiceReady.value = true;
        voiceTtsConfigured.value = event.tts_configured ?? false;
        voiceVadBackend.value = event.vad_backend === "silero" ? "Silero VAD" : "能量 VAD";
        if (event.asr_configured === false) {
          voiceAsrConfigured.value = false;
          voiceAsrBlockMessage.value = "后台尚未启用语音识别";
        } else if (options.privacy.value === "L2" && event.asr_runs_local === false) {
          voiceAsrConfigured.value = false;
          voiceAsrBlockMessage.value = "当前是 L2，需要在后台启用 faster-whisper 本地 ASR";
        } else {
          voiceAsrConfigured.value = true;
          voiceAsrBlockMessage.value = "";
        }
        if (textReplyVoice.value) {
          voiceStatus.value = voiceTtsConfigured.value
            ? "文字语音回复已就绪"
            : "未配置语音合成，回复将仅显示文字";
        } else if (voiceLive.value) {
          voiceStatus.value = "通话已连接，请说话";
        } else {
          voiceStatus.value = voiceAsrConfigured.value
            ? `语音已就绪 · ${event.asr_runs_local ? "本地识别" : "云端识别"} · ${event.vad_backend === "silero" ? "Silero VAD" : "Energy VAD"}${event.wake_word_configured ? " · 唤醒词待命" : ""}`
            : voiceAsrBlockMessage.value;
        }
        break;
      case "voice.wake_detected":
        voiceStatus.value = "已唤醒，开始说吧…";
        break;
      case "voice.wake_unavailable":
        voiceStatus.value = "唤醒词不可用，已切回自动监听 / PTT";
        break;
      case "voice.microphone_preempted":
        // 另一设备抢占了麦克风：本地立即停止采集（服务端已丢弃本连接话语）
        if (voiceRecording.value || voiceLive.value) {
          voiceRecording.value = false;
          voiceLive.value = false;
          void microphone.stop();
          voiceStatus.value = "麦克风已被其他设备接管，通话结束";
        }
        break;
      case "voice.audio_preempted":
        // 音频输出被其他设备/桌宠抢占：清空本地播放队列，文字仍会正常到达
        playback.interrupt();
        voiceSentence = null;
        voiceStatus.value = "语音播报已由其他设备接管";
        break;
      case "voice.partial_transcript":
        // 流式 ASR 实时字幕（docs/04 §6.4 P1）：断句前即时显示识别中的文字
        voiceTranscript.value = event.text ?? "";
        voiceStatus.value = "正在识别…";
        break;
      case "voice.transcript":
        voiceTranscript.value = event.text ?? "";
        voiceStatus.value = textReplyVoice.value
          ? "文字已提交，正在生成回复…"
          : "已识别，正在生成回复…";
        break;
      case "turn.accepted":
        voiceBusy.value = true;
        voiceStatus.value = "正在生成语音回复…";
        if (options.activeId.value && event.generation_id) {
          options.streaming.value = {
            conversationId: options.activeId.value,
            generationId: event.generation_id,
            text: "",
            emotion: null,
          };
        }
        if (options.activeId.value) void options.refreshMessages(options.activeId.value);
        break;
      case "reply.delta":
        if (options.activeId.value && event.generation_id) {
          if (options.streaming.value?.generationId === event.generation_id) {
            options.streaming.value.text += event.delta ?? "";
          } else {
            options.streaming.value = {
              conversationId: options.activeId.value,
              generationId: event.generation_id,
              text: event.delta ?? "",
              emotion: null,
            };
          }
          void options.scrollToEnd();
        }
        break;
      case "tool.started":
        voiceStatus.value = String(event.label ?? "正在查询外部信息…");
        break;
      case "tool.finished":
        voiceStatus.value = event.ok === false
          ? `查询未完成：${String(event.reason_code ?? "unknown")}`
          : "查询完成，正在组织回复…";
        break;
      case "voice.sentence":
        voiceSentence = {
          meta: {
            generationId: event.generation_id ?? null,
            index: event.index ?? 0,
            mime: event.mime ?? "audio/pcm;rate=24000",
            sampleRate: event.sample_rate ?? 24_000,
            provider: event.provider ?? null,
          },
          chunks: [],
          visemes: [],
        };
        voiceStatus.value = "正在接收语音…";
        break;
      case "voice.sentence.end":
        if (voiceSentence && (event.index ?? voiceSentence.meta.index) === voiceSentence.meta.index) {
          playback.enqueue(
            voiceSentence.meta,
            voiceSentence.chunks,
            voiceSentence.visemes,
            (amp) => {
              voiceViseme.value = amp;
            },
            (message) => {
              voiceBusy.value = false;
              voiceViseme.value = 0;
              voiceStatus.value = `语音播放失败：${message}`;
            },
          );
          voiceSentence = null;
          voiceStatus.value = "正在播放回复…";
        }
        break;
      case "voice.viseme":
        if (voiceSentence && (event.sentence_index ?? voiceSentence.meta.index) === voiceSentence.meta.index) {
          voiceSentence.visemes.push({
            amp: Math.max(0, Math.min(1, Number(event.amp ?? 0))),
            offsetMs: Math.max(0, Number(event.offset_ms ?? 0)),
            durationMs: Math.max(0, Number(event.duration_ms ?? 50)),
          });
        }
        break;
      case "voice.interrupted":
        voiceBusy.value = false;
        if (options.streaming.value?.generationId === event.generation_id) options.streaming.value = null;
        playback.interrupt();
        voiceSentence = null;
        voiceViseme.value = 0;
        voiceStatus.value = voiceLive.value ? "已打断，请继续说" : "已打断";
        break;
      case "turn.cancelled":
        voiceBusy.value = false;
        if (options.streaming.value?.generationId === event.generation_id) options.streaming.value = null;
        playback.interrupt();
        voiceSentence = null;
        voiceViseme.value = 0;
        voiceStatus.value = voiceLive.value ? "已打断，请继续说" : "语音回合已取消";
        break;
      case "turn.failed":
        voiceBusy.value = false;
        if (options.streaming.value?.generationId === event.generation_id) options.streaming.value = null;
        playback.interrupt();
        voiceSentence = null;
        voiceViseme.value = 0;
        voiceStatus.value = options.generationFailureText(event.reason_code, "语音生成失败");
        // 文字播报路径失败且没有回显时，回收占位消息并恢复到输入框。
        if (options.activeId.value) {
          const revoked = options.revokePendingUserMessages(options.activeId.value);
          if (revoked && !options.draft.value) {
            options.draft.value = revoked;
            options.setStatus("语音提交失败，消息已恢复到输入框", true);
          }
        }
        break;
      case "voice.asr_unavailable":
        voiceBusy.value = false;
        voiceStatus.value = `语音识别不可用：${asrUnavailableLabels[event.reason ?? "not_configured"] ?? event.reason ?? "unknown"}`;
        break;
      case "voice.tts_unavailable":
        voiceStatus.value = `语音合成不可用，将仅显示文字：${event.reason ?? "not_configured"}`;
        break;
      case "proactive.committed":
        voiceStatus.value = `主动提醒：${event.content ?? ""}`;
        break;
      case "voice.error":
        voiceBusy.value = false;
        if (event.reason === "invalid_audio_frame" || event.reason === "utterance_too_large") {
          voiceRecording.value = false;
          voiceLive.value = false;
          voiceTranscript.value = "";
          void microphone.stop().catch(() => undefined);
        }
        voiceStatus.value = `语音请求失败：${voiceErrorLabels[event.reason ?? ""] ?? event.reason ?? "unknown"}`;
        break;
      case "reply.committed":
        voiceBusy.value = false;
        voiceStatus.value = voiceLive.value ? "回复完成，请继续说" : "语音回复已完成";
        if (options.activeId.value) {
          const conversationId = options.activeId.value;
          const generationId = event.generation_id;
          void options.refreshMessages(conversationId).finally(() => {
            if (options.streaming.value?.generationId === generationId) options.streaming.value = null;
          });
        } else if (options.streaming.value?.generationId === event.generation_id) {
          options.streaming.value = null;
        }
        void options.loadRuntimeMeta();
        break;
    }
  }

  async function toggleVoiceRecording() {
    if (voiceLive.value) return;
    if (voiceRecording.value) {
      voiceRecording.value = false;
      voiceBusy.value = true;
      voiceSocket?.endUtterance();
      await microphone.stop();
      voiceStatus.value = "正在识别…";
      return;
    }
    if (!options.activeId.value || options.streaming.value || voiceBusy.value) return;
    try {
      // 麦克风按钮是移动端可信用户手势，同时解锁稍后的 TTS 播放。
      unlockVoicePlayback();
      const current = await ensureVoiceSocket();
      if (voiceAsrConfigured.value === false) {
        voiceStatus.value = voiceAsrBlockMessage.value || "语音识别当前不可用";
        return;
      }
      await microphone.start((chunk) => {
        if (voiceRecording.value) voiceSocket?.sendPcm(chunk);
      });
      current.beginUtterance();
      voiceTranscript.value = "";
      voiceRecording.value = true;
      voiceStatus.value = "正在听你说话…";
    } catch (error) {
      voiceRecording.value = false;
      await microphone.stop();
      voiceStatus.value = error instanceof Error ? `麦克风不可用：${error.message}` : "麦克风不可用";
    }
  }

  /**
   * 连续对话（电话模式）：接通后麦克风常开，不再逐句点开始/结束。
   * 服务端自动断句——停顿即发送；播报中一开口即打断（barge-in）；
   * 回复完毕继续待命，直到点挂断。
   */
  async function toggleVoiceCall() {
    if (voiceLive.value) {
      await closeVoice();
      return;
    }
    if (!options.activeId.value || options.streaming.value || voiceBusy.value || voiceRecording.value) return;
    try {
      // 通话按钮是移动端可信用户手势，同时解锁稍后的 TTS 播放。
      unlockVoicePlayback();
      const socket = await ensureVoiceSocket(true);
      if (voiceAsrConfigured.value === false) {
        voiceStatus.value = voiceAsrBlockMessage.value || "语音识别当前不可用";
        return;
      }
      await microphone.start((chunk) => {
        if (voiceLive.value) voiceSocket?.sendPcm(chunk);
      });
      voiceLive.value = true;
      voiceTranscript.value = "";
      voiceStatus.value = voiceVadBackend.value
        ? `通话已连接 · ${voiceVadBackend.value} · 请说话`
        : "通话已连接，请说话";
    } catch (error) {
      voiceLive.value = false;
      await microphone.stop();
      voiceStatus.value = error instanceof Error ? `麦克风不可用：${error.message}` : "麦克风不可用";
    }
  }

  function interruptVoice() {
    playback.interrupt();
    voiceSentence = null;
    voiceSocket?.interrupt();
    voiceStatus.value = "已请求打断";
  }

  function onPrivacyChanged() {
    if (voiceReady.value || voiceRecording.value) void closeVoice();
  }

  function onTextReplyVoiceChanged() {
    localStorage.setItem(TEXT_REPLY_VOICE_KEY, textReplyVoice.value ? "1" : "0");
    if (textReplyVoice.value && options.activeId.value) {
      // 必须在 change 手势仍生效时创建/恢复 AudioContext。
      unlockVoicePlayback();
      voiceStatus.value = "正在连接语音…";
      void ensureVoiceSocket().catch((error) => {
        voiceStatus.value = error instanceof Error ? error.message : "语音连接失败";
      });
    } else if (!voiceRecording.value) {
      void closeVoice();
    }
    if (!textReplyVoice.value) voiceStatus.value = "已关闭文字回复播报";
  }

  function disposeVoice() {
    void closeVoice();
    void playback.close();
  }

  return {
    voiceAsrConfigured,
    voiceAsrBlockMessage,
    voiceRecording,
    voiceLive,
    voiceBusy,
    voiceStatus,
    voiceTranscript,
    voiceVadBackend,
    voiceViseme,
    textReplyVoice,
    closeVoice,
    ensureVoiceSocket,
    toggleVoiceRecording,
    toggleVoiceCall,
    interruptVoice,
    unlockVoicePlayback,
    onPrivacyChanged,
    onTextReplyVoiceChanged,
    disposeVoice,
  };
}
