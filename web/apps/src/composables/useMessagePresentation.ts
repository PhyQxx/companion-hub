import type { AgentReplyControl, ChatMessage, ToolPresentation } from "@aria/shared";

/** 消息展示辅助：错误文案、回忆标签、工具结果与情绪标签（纯函数，无组件状态）。 */
export function useMessagePresentation() {
  function generationFailureText(reason: unknown, prefix = "生成失败"): string {
    const code = String(reason ?? "unknown");
    const messages: Record<string, string> = {
      user_model_concurrency_exhausted: "同时处理的任务较多，请稍后再试。",
      budget_owner_invalid: "账户当前无法处理任务，请重新登录后重试。",
      context_tokenizer_unavailable: "模型的本地分词配置不可用，请在管理界面核对文件和指纹。",
      daily_cost_budget_exhausted: "今天已达到模型费用预留上限，请稍后再试或在管理界面调整限额。",
      monthly_cost_budget_exhausted: "本月已达到模型费用预留上限，请核对用量或在管理界面调整限额。",
      cost_pricing_unavailable: "当前模型没有与限额匹配的完整计价信息，请在管理界面补齐价格和币种。",
      cost_usage_unknown: "已有调用的费用无法确认，暂不能在金额限额内继续，请先核对费用记录。",
      cost_window_changed: "费用统计刚切换到新的周期，请重试本轮请求。",
      tool_budget_exhausted: "这项任务已达到工具调用上限，请核对已完成部分。",
      run_budget_exhausted: "这轮回复已达到处理上限，可以缩小问题范围继续。",
      run_deadline_exceeded: "这轮回复处理超时，已停止生成。",
      budget_admission_failed: "暂时无法确认处理额度，这轮回复已停止。",
      budget_settlement_failed: "暂时无法确认处理额度，这轮回复已停止。",
      budget_run_inactive: "这轮回复已结束，没有继续处理。",
      budget_snapshot_missing: "这轮回复暂时无法继续，请重新发起对话。",
    };
    return messages[code] ?? `${prefix}：${code}`;
  }

  function personaVersionOf(message: ChatMessage): number | null {
    const value = (message.decision_meta as { persona_version?: unknown } | null)?.persona_version;
    return typeof value === "number" && value > 0 ? value : null;
  }

  const recallLabels: Record<string, string> = {
    working: "当前上下文",
    memory: "长期记忆",
    timeline: "历史回溯",
    source: "历史回溯",
    none: "未找到历史",
  };

  function recallLabelOf(message: ChatMessage): string | null {
    const recall = (
      message.decision_meta as { recall?: { mode?: unknown } } | null
    )?.recall;
    const mode = recall?.mode;
    return typeof mode === "string" ? (recallLabels[mode] ?? null) : null;
  }

  function toolResultOf(message: ChatMessage): ToolPresentation | null {
    const value = (message.decision_meta as { tool_result?: unknown } | null)?.tool_result;
    if (!value || typeof value !== "object") return null;
    const kind = (value as { kind?: unknown }).kind;
    return ["location_ambiguous", "weather", "nearby", "route"].includes(String(kind))
      ? value as ToolPresentation
      : null;
  }

  function messageTimeOf(message: ChatMessage): string {
    const date = new Date(message.created_at);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  const emotionLabels: Record<string, string> = {
    neutral: "平静",
    happy: "开心",
    sad: "难过",
    angry: "生气",
    surprised: "惊讶",
    thinking: "思考",
    concerned: "关切",
  };

  function emotionKeyOf(message: ChatMessage): string | null {
    const reply = agentReplyOf(message);
    return reply?.emotion ?? null;
  }

  function agentReplyOf(message: ChatMessage): AgentReplyControl | null {
    return (message.decision_meta as { agent_reply?: AgentReplyControl } | null)?.agent_reply ?? null;
  }

  function emotionOf(message: ChatMessage): string | null {
    const emotion = emotionKeyOf(message);
    return emotion ? (emotionLabels[emotion] ?? emotion) : null;
  }

  return {
    generationFailureText,
    personaVersionOf,
    recallLabelOf,
    toolResultOf,
    messageTimeOf,
    emotionLabels,
    emotionKeyOf,
    agentReplyOf,
    emotionOf,
  };
}
