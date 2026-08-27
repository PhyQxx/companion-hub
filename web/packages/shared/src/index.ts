/**
 * @aria/shared：chat 与 admin 前端共用的 API 客户端、WS 客户端与协议类型。
 * 类型与后端 Pydantic 模型一一对应，改动时保持两端同步。
 */

/** 隐私等级：L0 可上云 / L1 常规 / L2 仅本地模型 */
export type PrivacyLevel = "L0" | "L1" | "L2";

/** 聊天端与管理后台共享的基础主题。system 只负责选择明/暗预设。 */
export type ThemePreference = "pure-light" | "midnight-violet" | "system";

export const THEME_STORAGE_KEY = "ariaThemePreference";
export const THEME_CHANNEL_NAME = "ariaThemePreferenceChanged";

export const THEME_OPTIONS: ReadonlyArray<{ value: ThemePreference; label: string }> = [
  { value: "pure-light", label: "纯净明亮" },
  { value: "midnight-violet", label: "静夜紫" },
  { value: "system", label: "跟随系统" },
];

export interface UiThemeItem {
  id: string;
  key: string;
  name: string;
  mode: "light" | "dark";
  schema_version: number;
  version: number;
  definition: {
    description?: string;
    swatches?: string[];
    tokens?: Record<string, string>;
  };
  content_hash: string;
  built_in: boolean;
  updated_at: string;
}

export interface UiThemePreference {
  owner: string;
  selection: ThemePreference;
  theme: UiThemeItem;
  appearance_mode: "light" | "dark" | "system";
  updated_at: string | null;
}

const THEME_TOKENS = {
  "pure-light": {
    "--bg": "#f6f8fc", "--panel": "#ffffff", "--panel2": "#f7f9fd", "--control": "#ffffff",
    "--line": "#e3e8f2", "--text": "#172033", "--muted": "#68748a", "--accent": "#4f6df5",
    "--accent-soft": "#eef2ff", "--message-user-bg": "#4864dc", "--danger": "#e24b54", "--success": "#26b873",
    "--shadow": "0 8px 24px rgba(36, 50, 82, 0.06)", "--stage-from": "#f7eef0",
    "--stage-to": "#eef2ff", "--color-scheme": "light",
  },
  "midnight-violet": {
    "--bg": "#0c0e15", "--panel": "#141824", "--panel2": "#1a1f2d", "--control": "#10131d",
    "--line": "#2a3041", "--text": "#e7eaf3", "--muted": "#929caf", "--accent": "#7c8ff5",
    "--accent-soft": "#202944", "--message-user-bg": "#5264cc", "--danger": "#f08da2", "--success": "#6ee7b7",
    "--shadow": "0 16px 36px rgba(0, 0, 0, 0.24)", "--stage-from": "#282035",
    "--stage-to": "#171923", "--color-scheme": "dark",
  },
} as const;

export function readThemePreference(): ThemePreference {
  if (typeof localStorage === "undefined") return "pure-light";
  const saved = localStorage.getItem(THEME_STORAGE_KEY);
  return THEME_OPTIONS.some((option) => option.value === saved)
    ? saved as ThemePreference
    : "pure-light";
}

export function applyThemePreference(preference: ThemePreference): void {
  if (typeof document === "undefined") return;
  const prefersDark = typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches;
  const resolved = preference === "system" ? (prefersDark ? "midnight-violet" : "pure-light") : preference;
  const root = document.documentElement;
  root.dataset.themePreference = preference;
  root.dataset.theme = resolved;
  for (const [name, value] of Object.entries(THEME_TOKENS[resolved])) {
    if (name === "--color-scheme") root.style.colorScheme = value;
    else root.style.setProperty(name, value);
  }
}

export function saveThemePreference(preference: ThemePreference): void {
  if (typeof localStorage !== "undefined") localStorage.setItem(THEME_STORAGE_KEY, preference);
  applyThemePreference(preference);
}

export function broadcastThemePreference(preference: ThemePreference): void {
  if (typeof BroadcastChannel === "undefined") return;
  const channel = new BroadcastChannel(THEME_CHANNEL_NAME);
  channel.postMessage({ selection: preference });
  channel.close();
}

export function initializeTheme(): () => void {
  applyThemePreference(readThemePreference());
  if (typeof matchMedia === "undefined") return () => undefined;
  const query = matchMedia("(prefers-color-scheme: dark)");
  const update = () => {
    if (readThemePreference() === "system") applyThemePreference("system");
  };
  query.addEventListener("change", update);
  return () => query.removeEventListener("change", update);
}

/** 终端 WGS84 临时位置：仅随消息帧在内存中传递，用于工具位置解析，不落库 */
export interface ClientLocationPayload {
  latitude: number;
  longitude: number;
  accuracy_m: number;
}

/**
 * 与服务端 app/tools/intent.py 的查询关键词保持同步：
 * 文本命中时前端才请求/附带定位，避免每条消息都暴露坐标。
 */
const LOCATION_INTENT_TERMS = [
  "天气", "气温", "温度", "下雨", "降雨", "晴天", "阴天", "台风",
  "附近", "最近", "周边", "医院", "药店", "餐厅", "充电站",
  "怎么走", "路线", "导航", "开车去", "步行去", "坐公交",
];

export function matchesLocationIntent(text: string): boolean {
  return LOCATION_INTENT_TERMS.some((term) => text.includes(term));
}

export interface SetupStatus {
  setup_required: boolean;
}

export interface VoiceLatencyMetricSummary {
  count: number;
  p50: number | null;
  p90: number | null;
  max: number | null;
}

export interface VoiceLatencySummary {
  count: number;
  window_size: number;
  asr_ms: VoiceLatencyMetricSummary;
  first_token_ms: VoiceLatencyMetricSummary;
  first_audio_ms: VoiceLatencyMetricSummary;
  total_ms: VoiceLatencyMetricSummary;
  interrupt_ms: VoiceLatencyMetricSummary;
  targets: {
    completed_turns: number;
    interrupt_samples: number;
    first_audio_p90_ms: number;
    interrupt_p90_ms: number;
  };
  acceptance: {
    completed_turns_ready: boolean;
    interrupt_samples_ready: boolean;
    first_audio_p90_pass: boolean;
    interrupt_p90_pass: boolean;
  };
}

export interface VoiceControlEvent {
  type: string;
  conversation_id?: string;
  generation_id?: string;
  turn_id?: string;
  message_id?: string;
  privacy_level?: PrivacyLevel;
  text?: string;
  content?: string;
  delta?: string;
  is_final?: boolean;
  index?: number;
  mime?: string;
  sample_rate?: number;
  provider?: string;
  reason?: string;
  reason_code?: string;
  turn_cancelled?: boolean;
  asr_configured?: boolean;
  asr_runs_local?: boolean | null;
  asr_provider?: string | null;
  tts_configured?: boolean;
  tts_provider_count?: number;
  vad_backend?: string;
  wake_word_configured?: boolean;
  wake_word_backend?: string | null;
  backend?: string;
  amp?: number;
  offset_ms?: number;
  duration_ms?: number;
  sentence_index?: number;
  supported?: {
    format?: string;
    sample_rate?: number;
    channels?: number;
  };
  [key: string]: unknown;
}

export interface RuntimeMeta {
  persona?: {
    version: number;
    content_hash: string;
    name: string;
  };
  avatar?: {
    instance_id: string;
    pack_id: string;
    name: string;
    engine: "static" | "live2d" | "vrm" | "abstract";
    customization: Record<string, unknown>;
    assets: {
      thumbnail?: string;
      emotions?: Record<string, string>;
      model?: string;
    };
  };
  location_policy?: {
    tools_enabled: boolean;
    precise: "ask_each_time" | "allow_session";
  };
}

export interface AvatarChoice {
  instance_id: string;
  pack_id: string;
  name: string;
  engine: "static" | "live2d" | "vrm" | "abstract";
  status: string;
  is_current: boolean;
  customization: Record<string, unknown>;
  manifest: {
    assets?: { thumbnail?: string; emotions?: Record<string, string>; model?: string };
    runtime?: { adapter?: string; core_required?: boolean; network_access?: boolean };
    [key: string]: unknown;
  };
}

export type AriaLive2DHandle = {
  destroy?: () => void;
  setLipSync?: (value: number) => void;
  setSpeaking?: (active: boolean) => void;
  setExpression?: (expression: string) => boolean;
  setEmotion?: (emotion: string) => void;
  playMotion?: (group: string, index?: number) => boolean;
} | void;
export interface AriaLive2DRuntime {
  mount: (
    canvas: HTMLCanvasElement,
    options: { modelUrl: string; transparent: boolean },
  ) => AriaLive2DHandle | Promise<AriaLive2DHandle>;
}

declare global {
  interface Window { AriaLive2DRuntime?: AriaLive2DRuntime }
}

let live2DRuntimeLoad: Promise<AriaLive2DRuntime | null> | null = null;

/** Load an optional, locally installed Cubism adapter without bundling licensed Core files. */
export function ensureLive2DRuntime(): Promise<AriaLive2DRuntime | null> {
  if (typeof window === "undefined" || typeof document === "undefined") return Promise.resolve(null);
  if (window.AriaLive2DRuntime) return Promise.resolve(window.AriaLive2DRuntime);
  if (live2DRuntimeLoad) return live2DRuntimeLoad;
  live2DRuntimeLoad = new Promise((resolve) => {
    let settled = false;
    const script = document.createElement("script");
    script.src = "/api/v1/avatar-live2d-runtime/runtime.js";
    script.async = true;
    script.dataset.ariaLive2dRuntime = "true";
    const finish = (runtime: AriaLive2DRuntime | null) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      window.removeEventListener("aria-live2d-runtime-ready", onReady);
      window.removeEventListener("aria-live2d-runtime-error", onError);
      if (!runtime) {
        script.remove();
        live2DRuntimeLoad = null;
      }
      resolve(runtime);
    };
    const onReady = () => finish(window.AriaLive2DRuntime ?? null);
    const onError = () => finish(null);
    const timeout = window.setTimeout(onError, 20_000);
    window.addEventListener("aria-live2d-runtime-ready", onReady);
    window.addEventListener("aria-live2d-runtime-error", onError);
    script.onerror = onError;
    script.onload = () => {
      if (window.AriaLive2DRuntime) finish(window.AriaLive2DRuntime);
    };
    document.head.appendChild(script);
  });
  return live2DRuntimeLoad;
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

export interface LocationCandidate {
  name: string;
  adcode?: string;
}

export type ToolPresentation =
  | { kind: "location_ambiguous"; tool_name: string; candidates: LocationCandidate[] }
  | {
      kind: "weather"; provider: string; cache_hit: boolean; fetched_at?: string | null;
      report_time?: string | null; location?: { name?: string; adcode?: string } | null;
      current?: { weather?: string; temperature_c?: number; humidity_percent?: number; wind?: string | null } | null;
      forecast?: Array<{ date?: string; day_weather?: string; night_weather?: string; low_c?: number; high_c?: number }>;
    }
  | {
      kind: "nearby"; provider: string; cache_hit: boolean; fetched_at?: string | null;
      origin?: { name?: string; adcode?: string } | null; rank_by?: string;
      results: Array<{ name?: string; address?: string; category?: string; distance_m?: number;
        duration_s?: number | null; distance_basis?: string; navigation_uri?: string }>;
    }
  | {
      kind: "route"; provider: string; cache_hit: boolean; fetched_at?: string | null;
      origin?: string; destination?: string; mode?: string; distance_m?: number;
      duration_s?: number | null; steps?: string[]; navigation_uri?: string;
    };

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

export interface VoiceSocketHandlers {
  onEvent: (event: VoiceControlEvent) => void;
  onAudio: (chunk: ArrayBuffer) => void;
  onClose: (code: number) => void;
}

/**
 * 语音 WebSocket 客户端：鉴权后发送 voice.hello，并以 voice.ready
 * 作为会话建立成功信号。二进制帧为当前 voice.sentence 的音频分片。
 */
export class VoiceSocket {
  private socket: WebSocket | null = null;

  constructor(
    private url: string,
    private token: string,
    private handlers: VoiceSocketHandlers,
  ) {}

  connect(
    conversationId: string,
    privacyLevel: PrivacyLevel,
    location?: ClientLocationPayload | null,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(this.url);
      socket.binaryType = "arraybuffer";
      this.socket = socket;
      let ready = false;
      socket.onopen = () => {
        socket.send(JSON.stringify({ type: "authenticate", access_token: this.token }));
        socket.send(JSON.stringify({
          type: "voice.hello",
          conversation_id: conversationId,
          privacy_level: privacyLevel,
          ...(location ? { location } : {}),
          format: "pcm_s16le",
          sample_rate: 16_000,
          channels: 1,
        }));
      };
      socket.onmessage = (frame) => {
        if (typeof frame.data === "string") {
          const event = JSON.parse(frame.data) as VoiceControlEvent;
          if (event.type === "voice.ready" && !ready) {
            ready = true;
            resolve();
          }
          this.handlers.onEvent(event);
          return;
        }
        if (frame.data instanceof ArrayBuffer) {
          this.handlers.onAudio(frame.data);
          return;
        }
        if (frame.data instanceof Blob) {
          void frame.data.arrayBuffer().then((data) => this.handlers.onAudio(data));
        }
      };
      socket.onerror = () => {
        if (!ready) reject(new Error("voice_websocket_error"));
      };
      socket.onclose = (event) => {
        if (!ready) reject(new Error(`voice_closed_${event.code}`));
        this.handlers.onClose(event.code);
      };
    });
  }

  private sendJson(frame: Record<string, unknown>) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(frame));
  }

  beginUtterance() {
    this.sendJson({ type: "utterance.begin" });
  }

  endUtterance() {
    this.sendJson({ type: "utterance.end" });
  }

  submitText(text: string, location?: ClientLocationPayload | null) {
    this.sendJson({
      type: "text.submit",
      text,
      ...(location ? { location } : {}),
    });
  }

  interrupt() {
    this.sendJson({ type: "interrupt" });
  }

  sendPcm(chunk: ArrayBuffer) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(chunk);
  }

  close() {
    this.socket?.close(1000);
    this.socket = null;
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

  themePreference(token: string) {
    return this.request<UiThemePreference>("/api/v1/ui/preferences", { method: "GET" }, token);
  }

  updateThemePreference(token: string, selection: ThemePreference) {
    return this.request<UiThemePreference>("/api/v1/ui/preferences", {
      method: "PUT",
      body: JSON.stringify({ selection }),
    }, token);
  }

  listAvatars(token: string) {
    return this.request<AvatarChoice[]>("/api/v1/avatars", { method: "GET" }, token);
  }

  switchAvatar(token: string, instanceId: string) {
    return this.request<AvatarChoice>("/api/v1/avatars/current", {
      method: "PUT",
      body: JSON.stringify({ instance_id: instanceId }),
    }, token);
  }
}

/** 管理端 REST 客户端：令牌保存在实例上（来自 sessionStorage） */
export class AdminApi {
  constructor(
    public baseUrl = "",
    public token = "",
  ) {}

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = { ...(init.headers as Record<string, string> | undefined) };
    if (!(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
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

  sendMessage(
    conversationId: string,
    text: string,
    privacyLevel: PrivacyLevel,
    location?: ClientLocationPayload | null,
  ) {
    this.send({
      type: "message.send",
      conversation_id: conversationId,
      text,
      privacy_level: privacyLevel,
      ...(location ? { location } : {}),
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
