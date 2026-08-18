export type PrivacyLevel = "L0" | "L1" | "L2";

export interface SetupStatus {
  setup_required: boolean;
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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export class ChatApi {
  constructor(private baseUrl = "") {}

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
      const message =
        typeof detail === "string"
          ? detail
          : ((detail as { reason_code?: string } | null)?.reason_code ??
            `HTTP ${response.status}`);
      throw new ApiError(response.status, message);
    }
    return body as T;
  }

  authStatus() {
    return this.request<SetupStatus>("/api/v1/auth/status", { method: "GET" });
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

export interface ChatSocketHandlers {
  onEvent: (event: SocketEvent) => void;
  onClose: (code: number) => void;
}

export class ChatSocket {
  private socket: WebSocket | null = null;

  constructor(
    private url: string,
    private token: string,
    private handlers: ChatSocketHandlers,
  ) {}

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
