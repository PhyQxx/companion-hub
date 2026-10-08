// 会话导出工具：把会话消息格式化为便于粘贴给 AI / 他人阅读的 Markdown，
// 并提供下载与剪贴板两条出口。纯前端实现，消息数据本地已有，不经过服务端。
import type { ChatMessage } from "@aria/shared";

export interface ConversationExportOptions {
  conversationTitle: string;
  messages: ChatMessage[];
  /** true 表示仅导出会话中勾选的节选，标题会注明 */
  excerptOnly?: boolean;
}

const roleLabels: Record<ChatMessage["role"], string> = {
  user: "用户",
  assistant: "助手",
  system: "系统",
  tool: "工具",
};

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

/**
 * 渲染导出正文：每条消息一个二级标题（角色 · 消息序号 · 时间），
 * 内容保留原始 Markdown 不转义，条与条之间用水平线分隔，方便逐条引用。
 */
export function formatConversationMarkdown(options: ConversationExportOptions): string {
  const messages = options.messages
    .filter((message) => message.role !== "system")
    .sort((left, right) => left.seq - right.seq);
  const lines: string[] = [
    `# 会话导出${options.excerptOnly ? "（节选）" : ""}：${options.conversationTitle}`,
    "",
    `- 导出时间：${formatDateTime(new Date().toISOString())}`,
    `- 消息条数：${messages.length}`,
  ];
  if (messages.length) {
    lines.push(`- 消息范围：第 ${messages[0]!.seq} ~ ${messages[messages.length - 1]!.seq} 条`);
  }
  lines.push("", "---", "");
  for (const message of messages) {
    const time = formatDateTime(message.created_at);
    const heading = [roleLabels[message.role] ?? message.role, `#${message.seq}`, time]
      .filter(Boolean)
      .join(" · ");
    lines.push(`## ${heading}`, "", message.content.trim() || "（空消息）", "", "---", "");
  }
  return `${lines.join("\n").trimEnd()}\n`;
}

/** 生成安全的导出文件名：会话标题 + 本地时间戳，excerpt 时加“节选”后缀。 */
export function exportFileName(conversationTitle: string, excerpt = false): string {
  const safe = conversationTitle
    .replace(/[\\/:*?"<>|\s]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `${safe || "会话"}-${stamp}${excerpt ? "-节选" : ""}.md`;
}

/** 触发浏览器下载一个 Markdown 文件。 */
export function downloadMarkdown(fileName: string, markdown: string): void {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  // 立刻 revoke 在部分浏览器会中断未开始的下载，推迟到下一轮任务。
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** 复制文本到剪贴板；非安全上下文 / 权限受限时用临时 textarea 兜底。 */
export async function copyMarkdown(markdown: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(markdown);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = markdown;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch {
      return false;
    }
  }
}
