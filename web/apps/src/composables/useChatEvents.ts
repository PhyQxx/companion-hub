import { type Ref } from "vue";
import type { ChatMessage, SocketEvent } from "@aria/shared";
import type { StreamingState } from "./useVoice";
import { PENDING_REVOKE_MIN_AGE_MS } from "./useMessages";

/** WS 事件分发：delta 流式拼接、control 情绪标签、committed 落定与占位回收。 */
export function useChatEvents(options: {
  activeId: Ref<string | null>;
  streaming: Ref<StreamingState | null>;
  draft: Ref<string>;
  planRefresh: Ref<number>;
  avatarMotion: Ref<{ value: string; sequence: number } | null>;
  setStatus: (text: string, error?: boolean) => void;
  sync: (conversationId: string, afterSeq: number) => void;
  revokePendingUserMessages: (conversationId: string, minAgeMs?: number) => string | null;
  mergeMessages: (conversationId: string, incoming: ChatMessage[], replace?: boolean) => ChatMessage[];
  scrollToEnd: () => Promise<void>;
  loadRuntimeMeta: () => Promise<void>;
  generationFailureText: (reason: unknown, prefix?: string) => string;
}) {
  function handleEvent(event: SocketEvent) {
    const conversationId = event.stream.replace("conversation:", "");
    if (event.type === "protocol.error") {
      options.setStatus(`协议错误：${String(event.payload.reason_code ?? "")}`, true);
      return;
    }
    if (event.type === "tool.started") {
      options.setStatus(String(event.payload.label ?? "正在查询外部信息…"));
      return;
    }
    if (event.type === "tool.finished") {
      if (event.payload.ok === false) {
        options.setStatus(`查询未完成：${String(event.payload.reason_code ?? "unknown")}`, true);
      } else {
        options.setStatus("查询完成，正在组织回复…");
      }
      return;
    }
    if (event.type === "plan.execution" || event.type === "plan.changed") {
      options.planRefresh.value += 1;
      return;
    }
    if (event.type === "sync.completed") {
      const afterSeq = Number(event.payload.after_seq ?? 0);
      const nextAfterSeq = Number(event.payload.next_after_seq ?? event.seq ?? afterSeq);
      if (event.payload.has_more === true && nextAfterSeq > afterSeq) {
        options.sync(conversationId, nextAfterSeq);
        return;
      }
      // 同步收敛后仍存在的占位消息（超过最小年龄）说明服务端从未落库，恢复到输入框。
      const revoked = options.revokePendingUserMessages(conversationId, PENDING_REVOKE_MIN_AGE_MS);
      if (revoked && conversationId === options.activeId.value && !options.draft.value) {
        options.draft.value = revoked;
        options.setStatus("有消息未送达，已恢复到输入框", true);
      }
      return;
    }
    if (!event.payload.message && event.type !== "reply.delta" && event.type !== "reply.control") {
      if (event.type === "turn.failed") {
        options.streaming.value = null;
        // start_turn 阶段失败时不会有 message.committed 回显，占位消息需手动回收。
        const revoked = options.revokePendingUserMessages(conversationId);
        if (revoked && conversationId === options.activeId.value && !options.draft.value) {
          options.draft.value = revoked;
          options.setStatus(`${options.generationFailureText(event.payload.reason_code)}；消息已恢复到输入框`, true);
        } else {
          options.setStatus(options.generationFailureText(event.payload.reason_code), true);
        }
      } else if (event.type === "turn.cancelled") {
        options.streaming.value = null;
        options.setStatus("已取消");
      }
      return;
    }

    if (event.type === "reply.delta") {
      if (options.streaming.value && options.streaming.value.generationId === event.generation_id) {
        options.streaming.value.text += event.payload.delta ?? "";
      } else if (!options.streaming.value && event.generation_id) {
        options.streaming.value = {
          conversationId,
          generationId: event.generation_id,
          text: event.payload.delta ?? "",
          emotion: null,
        };
      }
      if (conversationId === options.activeId.value) void options.scrollToEnd();
      return;
    }
    if (event.type === "reply.control") {
      if (options.streaming.value && options.streaming.value.generationId === event.generation_id) {
        const control = event.payload.agent_reply;
        options.streaming.value.emotion = control?.emotion ?? null;
        options.streaming.value.expression = control?.expressions?.[0] ?? null;
        const motion = control?.actions?.find((action) => action.type === "animation")?.value;
        if (motion) {
          options.avatarMotion.value = {
            value: motion,
            sequence: (options.avatarMotion.value?.sequence ?? 0) + 1,
          };
        }
      }
      return;
    }

    const message = event.payload.message!;
    options.mergeMessages(conversationId, [message]);
    if (event.type === "reply.committed" || event.type === "message.committed") {
      if (options.streaming.value?.generationId === event.generation_id) options.streaming.value = null;
      if (event.type === "reply.committed") {
        options.setStatus("已连接");
        void options.loadRuntimeMeta();
      }
    }
    if (conversationId === options.activeId.value) void options.scrollToEnd();
  }

  return { handleEvent };
}
