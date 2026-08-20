/**
 * @aria/shared：chat 与 admin 前端共用的 API 客户端、WS 客户端与协议类型。
 * 类型与后端 Pydantic 模型一一对应，改动时保持两端同步。
 */

/** 隐私等级：L0 可上云 / L1 常规 / L2 仅本地模型 */
export type PrivacyLevel = "L0" | "L1" | "L2";

export interface SetupStatus {
  setup_required: boolean;
}

export interface RuntimeMeta {
  persona?: {
    version: number;
    content_hash: string;
    name: string;
  };
}

export interface SystemHealth {
  status: string;
  version: string;
  persona?: {
    version: number;
    content_hash: string;
    name: string;
  };
}

export interface SessionUser {
  id: string;
  display_name: string;
}

export interface AuthSession {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: SessionUser;
}

export interface Conversation {
  id: string;
  user_id: string;
  title: string | null;
  status: string;
  last_seq: number;
  created_at: string;
  last_active_at: string;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  turn_id: string;
  seq: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  privacy_level: string;
  generation_id: string | null;
  decision_meta: Record<string, unknown> | null;
  created_at: string;
}

export interface AgentReplyControl {
  text: string;
  tts_text: string;
  emotion: string;
  expressions: string[];
  actions: Array<Record<string, string>>;
  parse_status?: string;
}

export interface SocketEvent {
  proto_version: number;
  stream: string;
  seq: number | null;
  event_id: string;
  type: string;
  generation_id: string | null;
  sent_at: string;
  payload: {
    message?: ChatMessage;
    delta?: string;
    delta_index?: number;
    agent_reply?: AgentReplyControl;
    reason_code?: string;
    user_id?: string;
    expires_at?: string;
    count?: number;
    after_seq?: number;
    [key: string]: unknown;
  };
}

/** 后端返回非 2xx 时抛出；message 已尽量展开 detail/reason_code */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/**
 * 展开后端错误响应的 detail 字段：
 * 字符串直接透传；{reason_code} 对象取其码；FastAPI 422 的校验错误
 * 数组逐条转为「字段路径: 原因」，避免界面只显示裸的 HTTP 状态码。
 */
function describeDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const reasonCode = (detail as { reason_code?: unknown }).reason_code;
    if (typeof reasonCode === "string" && reasonCode) return reasonCode;
    if (Array.isArray(detail)) {
      const lines = detail.map((item) => {
        if (!item || typeof item !== "object") return String(item);
        const { loc, msg } = item as { loc?: unknown; msg?: unknown };
        const path = Array.isArray(loc) ? loc.filter((part) => part !== "body").join(".") : "";
        const text = typeof msg === "string" ? msg.replace(/^Value error,\s*/, "") : JSON.stringify(item);
        return path ? `${path}: ${text}` : text;
      });
      if (lines.length) return lines.join("；");
    }
  }
  return fallback;
}

/** 聊天侧 REST 客户端：身份、会话与消息（不含管理端点） */
export class ChatApi {
  constructor(private baseUrl = "") {}

  /** 统一请求封装：附加 JSON 头与 Bearer 令牌，统一错误展开 */
  private async request<T>(path: string, init: RequestInit, token?: string): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(init.headers as Record<string, string> | undefined),
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = (body as { detail?: unknown }).detail;
      throw new ApiError(response.status, describeDetail(detail, `HTTP ${response.status}`));
    }
    return body as T;
  }

  authStatus() {
    return this.request<SetupStatus>("/api/v1/auth/status", { method: "GET" });
  }

  health() {
    return this.request<SystemHealth>("/healthz", { method: "GET" });
  }

  runtimeMeta() {
    return this.request<RuntimeMeta>("/api/v1/meta/runtime", { method: "GET" });
  }

  setup(password: string, adminToken: string, displayName = "主人") {
    return this.request<AuthSession>("/api/v1/auth/setup", {
      method: "POST",
      body: JSON.stringify({ password, display_name: displayName }),
      headers: { Authorization: `Bearer ${adminToken}` },
    });
  }

  login(password: string) {
    return this.request<AuthSession>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
  }

  me(token: string) {
    return this.request<{ session_id: string; expires_at: string; user: SessionUser }>(
      "/api/v1/auth/me",
      { method: "GET" },
      token,
    );
  }

  logout(token: string) {
    return this.request<Record<string, never>>("/api/v1/auth/logout", {
      method: "POST",
    }, token);
  }

  listConversations(token: string) {
    return this.request<Conversation[]>("/api/v1/chat/conversations", { method: "GET" }, token);
  }

  createConversation(token: string, title: string | null) {
    return this.request<Conversation>("/api/v1/chat/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }, token);
  }

  deleteConversation(token: string, conversationId: string) {
    return this.request<{ ledger_id: number; deleted_memory_ids: number[] }>(
      `/api/v1/chat/conversations/${conversationId}`,
      { method: "DELETE" },
      token,
    );
  }

  listMessages(token: string, conversationId: string) {
    return this.request<ChatMessage[]>(
      `/api/v1/chat/conversations/${conversationId}/messages`,
      { method: "GET" },
      token,
    );
  }
}

/** 管理端 REST 客户端：令牌保存在实例上（来自 sessionStorage） */
export class AdminApi {
  constructor(
    private baseUrl = "",
    public token = "",
  ) {}

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(init.headers as Record<string, string> | undefined),
    };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
      throw new ApiError(response.status, describeDetail(body.detail, `HTTP ${response.status}`));
    }
    if (response.status === 204) return {} as T;
    return (await response.json()) as T;
  }
}

export interface ChatSocketHandlers {
  onEvent: (event: SocketEvent) => void;
  onClose: (code: number) => void;
}

/**
 * 聊天 WebSocket 客户端：封装 authenticate 首帧、发送/取消/补拉帧
 * 与服务端事件的回调分发。断线重连策略由调用方决定。
 */
export class ChatSocket {
  private socket: WebSocket | null = null;

  constructor(
    private url: string,
    private token: string,
    private handlers: ChatSocketHandlers,
  ) {}

  /** 建立连接并完成 5 秒内的 authenticate 首帧鉴权，成功后 resolve */
  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(this.url);
      this.socket = socket;
      let authenticated = false;
      socket.onopen = () => {
        socket.send(JSON.stringify({ type: "authenticate", access_token: this.token }));
      };
      socket.onmessage = (frame) => {
        const event = JSON.parse(String(frame.data)) as SocketEvent;
        if (event.type === "auth.accepted") {
          authenticated = true;
          resolve();
        }
        this.handlers.onEvent(event);
      };
      socket.onerror = () => {
        if (!authenticated) reject(new Error("websocket_error"));
      };
      socket.onclose = (event) => {
        if (!authenticated) reject(new Error(`closed_${event.code}`));
        this.handlers.onClose(event.code);
      };
    });
  }

  send(frame: Record<string, unknown>) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(frame));
  }

  sendMessage(conversationId: string, text: string, privacyLevel: PrivacyLevel) {
    this.send({
      type: "message.send",
      conversation_id: conversationId,
      text,
      privacy_level: privacyLevel,
    });
  }

  cancel(generationId: string) {
    this.send({ type: "turn.cancel", generation_id: generationId });
  }

  sync(conversationId: string, afterSeq: number) {
    this.send({ type: "sync.request", conversation_id: conversationId, after_seq: afterSeq });
  }

  close() {
    this.socket?.close(1000);
    this.socket = null;
  }
}
