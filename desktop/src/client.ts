import { invoke } from "@tauri-apps/api/core";
import {
  isExecuteCommand,
  parseCommandArgs,
  type SignedFrame,
  verifyFrame,
  websocketUrl,
} from "./protocol";

export const DESKTOP_BASE_CAPABILITIES = ["device.ping", "avatar.render"] as const;
export const DESKTOP_NOTIFICATION_CAPABILITY = "notification.show" as const;

export interface PairResult {
  device_id: string;
  owner_user_id: string;
  name: string;
  alias: string | null;
  hub_url: string;
}

export interface StoredClientConfig extends PairResult {}

export interface ScreenPermissionStatus {
  supported: boolean;
  granted: boolean;
}

export interface ScreenCaptureEnvironment extends ScreenPermissionStatus {
  locked: boolean;
}

export interface ScreenCaptureGrant {
  expiresAt: number;
  remainingUses: number;
}

export interface AvatarControl {
  sequence: number;
  emotion?: string;
  expression?: string;
  motion?: string;
  text?: string;
  lipSync?: number;
  speaking?: boolean;
}

export function parseAvatarControl(frame: SignedFrame): AvatarControl | null {
  if (
    frame.type !== "avatar.control" ||
    !Number.isSafeInteger(frame.sequence) ||
    Number(frame.sequence) < 1 ||
    !frame.control ||
    typeof frame.control !== "object" ||
    Array.isArray(frame.control)
  ) return null;
  const value = frame.control as Record<string, unknown>;
  const control: AvatarControl = { sequence: Number(frame.sequence) };
  if (typeof value.emotion === "string" && value.emotion.length <= 80) {
    control.emotion = value.emotion;
  }
  if (typeof value.expression === "string" && value.expression.length <= 160) {
    control.expression = value.expression;
  }
  if (typeof value.motion === "string" && value.motion.length <= 160) {
    control.motion = value.motion;
  }
  if (typeof value.text === "string" && value.text.length <= 280) {
    control.text = value.text;
  }
  if (
    typeof value.lipSyncMilli === "number" &&
    Number.isInteger(value.lipSyncMilli) &&
    value.lipSyncMilli >= 0 &&
    value.lipSyncMilli <= 1000
  ) {
    control.lipSync = value.lipSyncMilli / 1000;
  }
  if (typeof value.speaking === "boolean") control.speaking = value.speaking;
  return Object.keys(control).length > 1 ? control : null;
}

export function createScreenCaptureGrant(now = Date.now()): ScreenCaptureGrant {
  return { expiresAt: now + 5 * 60_000, remainingUses: 1 };
}

export function screenCaptureGrantActive(
  grant: ScreenCaptureGrant | null,
  now = Date.now(),
): boolean {
  return grant !== null && grant.remainingUses > 0 && grant.expiresAt > now;
}

export function consumeScreenCaptureGrant(
  grant: ScreenCaptureGrant | null,
  now = Date.now(),
): ScreenCaptureGrant | null {
  if (grant === null || !screenCaptureGrantActive(grant, now)) return null;
  return grant.remainingUses > 1
    ? { ...grant, remainingUses: grant.remainingUses - 1 }
    : null;
}

interface DeviceAssetUpload {
  asset_id: string;
  command_id: string;
  media_type: string;
  bytes: number;
  sha256: string;
  expires_at: string;
}

export interface ScreenCaptureRequest {
  target: "main_display" | "display" | "active_window" | "interactive";
  displayIndex: number | null;
}

export interface NotificationRequest {
  title: string;
  body: string;
  privacyLevel: "L0" | "L1" | "L2";
}

export function parseNotificationRequest(
  args: Record<string, unknown>,
): NotificationRequest | null {
  const title = typeof args.title === "string" ? args.title.trim() : "";
  const body = typeof args.body === "string" ? args.body.trim() : "";
  const privacyLevel = args.privacy_level;
  if (
    !title || title.length > 80 || !body || body.length > 1000 ||
    !["L0", "L1", "L2"].includes(String(privacyLevel))
  ) return null;
  return { title, body, privacyLevel: privacyLevel as "L0" | "L1" | "L2" };
}

export function parseScreenCaptureRequest(
  args: Record<string, unknown>,
): ScreenCaptureRequest | null {
  const target = args.target ?? "main_display";
  const displayIndex = args.display_index;
  if (target === "main_display") {
    return displayIndex === undefined ? { target, displayIndex: null } : null;
  }
  if (target === "active_window" || target === "interactive") {
    return displayIndex === undefined ? { target, displayIndex: null } : null;
  }
  if (
    target === "display" &&
    typeof displayIndex === "number" &&
    Number.isInteger(displayIndex) &&
    displayIndex >= 1 &&
    displayIndex <= 32
  ) {
    return { target, displayIndex };
  }
  return null;
}

/** 把 Rust 侧结构化截图错误映射为命令回执 reason_code；旧字符串错误按已知文案归类。 */
export function captureFailureCode(error: unknown): string {
  if (typeof error === "object" && error !== null && "code" in error) {
    const code = (error as { code: unknown }).code;
    if (typeof code === "string" && code.length > 0) return code;
  }
  const message = typeof error === "string"
    ? error
    : error instanceof Error
      ? error.message
      : String(error ?? "");
  if (message.includes("锁屏")) return "screen_locked";
  if (message.includes("屏幕录制权限")) return "screen_capture_not_granted";
  return "command_execution_failed";
}

export type ConnectionState = "unpaired" | "connecting" | "online" | "offline" | "error";

export interface ClientCallbacks {
  onState: (state: ConnectionState, detail: string) => void;
  onEvent: (message: string) => void;
  authorizeScreenCapture: () => "allowed" | "screen_locked" | "screen_capture_not_granted";
  onAvatarControl?: (control: AvatarControl) => void;
}

export class DeviceConnection {
  private socket: WebSocket | null = null;
  private heartbeat: number | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private manuallyStopped = false;
  private completedKeys = new Set<string>();
  private cancelledCommands = new Set<string>();
  private lastAvatarSequence = 0;

  constructor(
    private config: StoredClientConfig,
    private accessToken: string,
    private capabilities: string[],
    private callbacks: ClientCallbacks,
  ) {}

  setCapabilities(capabilities: string[]): void {
    this.capabilities = capabilities;
    this.send({ type: "device.heartbeat", capabilities: this.capabilities });
  }

  connect(): void {
    this.manuallyStopped = false;
    this.clearTimers();
    this.callbacks.onState("connecting", "正在连接 Hub…");
    const socket = new WebSocket(websocketUrl(this.config.hub_url));
    this.socket = socket;
    socket.addEventListener("open", () => {
      socket.send(
        JSON.stringify({
          type: "device.authenticate",
          access_token: this.accessToken,
          capabilities: this.capabilities,
        }),
      );
    });
    socket.addEventListener("message", (event) => void this.handleMessage(String(event.data)));
    socket.addEventListener("close", (event) => {
      this.stopHeartbeat();
      if (this.socket === socket) this.socket = null;
      if (this.manuallyStopped) return;
      this.callbacks.onState("offline", `连接已断开（${event.code}）`);
      this.scheduleReconnect();
    });
    socket.addEventListener("error", () => {
      this.callbacks.onState("error", "WebSocket 连接失败");
    });
  }

  stop(): void {
    this.manuallyStopped = true;
    this.clearTimers();
    this.socket?.close(1000, "client paused");
    this.socket = null;
    this.callbacks.onState("offline", "连接已暂停");
  }

  private async handleMessage(raw: string): Promise<void> {
    let frame: SignedFrame;
    try {
      frame = JSON.parse(raw) as SignedFrame;
    } catch {
      this.callbacks.onEvent("忽略了无法解析的服务端帧");
      return;
    }
    if (!(await verifyFrame(this.accessToken, frame))) {
      this.callbacks.onState("error", "服务端帧签名无效，连接已关闭");
      this.socket?.close(4403, "invalid server signature");
      return;
    }
    if (frame.type === "device.accepted") {
      this.lastAvatarSequence = 0;
      this.reconnectAttempt = 0;
      this.callbacks.onState("online", "已连接，等待命令");
      this.callbacks.onEvent("设备鉴权通过");
      this.startHeartbeat();
      return;
    }
    if (frame.type === "command.cancel" && typeof frame.command_id === "string") {
      this.cancelledCommands.add(frame.command_id);
      this.callbacks.onEvent(`命令 ${frame.command_id.slice(0, 8)} 已取消`);
      return;
    }
    const avatarControl = parseAvatarControl(frame);
    if (avatarControl) {
      if (avatarControl.sequence <= this.lastAvatarSequence) return;
      this.lastAvatarSequence = avatarControl.sequence;
      this.callbacks.onAvatarControl?.(avatarControl);
      return;
    }
    if (isExecuteCommand(frame)) await this.execute(frame);
  }

  private async execute(frame: ReturnType<typeof asExecute>): Promise<void> {
    if (new Date(frame.expires_at).getTime() <= Date.now()) {
      this.sendResult(frame.command_id, "failed", "command_expired", {});
      return;
    }
    this.send({ type: "command.ack", command_id: frame.command_id });
    let args: Record<string, unknown>;
    try {
      args = parseCommandArgs(frame);
    } catch {
      this.sendResult(frame.command_id, "failed", "invalid_command_args", {});
      return;
    }
    try {
      await this.runCommand(frame, args);
    } catch {
      this.sendResult(frame.command_id, "failed", "command_execution_failed", {});
    }
  }

  private async runCommand(
    frame: ReturnType<typeof asExecute>,
    args: Record<string, unknown>,
  ): Promise<void> {
    if (this.completedKeys.has(frame.idempotency_key)) {
      this.sendResult(frame.command_id, "succeeded", null, { duplicate: true });
      return;
    }
    if (this.cancelledCommands.has(frame.command_id)) {
      this.sendResult(frame.command_id, "failed", "command_cancelled", {});
      return;
    }
    const started = performance.now();
    let resultMeta: Record<string, unknown>;
    if (frame.command === "device.ping") {
      resultMeta = {
        latency_ms: Math.round(performance.now() - started),
        client_version: "0.1.0",
        platform: navigator.platform,
      };
    } else if (frame.command === "screen.capture" || frame.command === "screen.monitor") {
      const isMonitor = frame.command === "screen.monitor";
      const requiredCapability = isMonitor ? "screen.monitor" : "screen.capture";
      if (!this.capabilities.includes(requiredCapability)) {
        this.sendResult(frame.command_id, "failed", `${isMonitor ? "screen_monitor" : "screen_capture"}_unavailable`, {});
        return;
      }
      const request = parseScreenCaptureRequest(args);
      if (request === null) {
        this.sendResult(frame.command_id, "failed", "invalid_command_args", {});
        return;
      }
      if (!isMonitor) {
        // 单次授权只约束手动截图；screen.monitor 由 Hub 配置开关驱动，
        // 设备端硬闸门（TCC/锁屏/隐私暂停）分别在能力声明与 Rust 端把关。
        const authorization = this.callbacks.authorizeScreenCapture();
        if (authorization !== "allowed") {
          this.sendResult(frame.command_id, "failed", authorization, {});
          return;
        }
      }
      let uploaded: DeviceAssetUpload;
      try {
        uploaded = await captureAndUpload(
          frame.command_id,
          request.target,
          request.displayIndex,
        );
      } catch (error) {
        this.sendResult(frame.command_id, "failed", captureFailureCode(error), {});
        return;
      }
      if (this.cancelledCommands.has(frame.command_id)) {
        this.sendResult(frame.command_id, "failed", "command_cancelled", {});
        return;
      }
      resultMeta = { ...uploaded, latency_ms: Math.round(performance.now() - started) };
    } else if (frame.command === "notification.show") {
      if (!this.capabilities.includes("notification.show")) {
        this.sendResult(frame.command_id, "failed", "notification_unavailable", {});
        return;
      }
      const request = parseNotificationRequest(args);
      if (request === null) {
        this.sendResult(frame.command_id, "failed", "invalid_command_args", {});
        return;
      }
      await invoke("show_notification", { title: request.title, body: request.body });
      resultMeta = {
        privacy_level: request.privacyLevel,
        latency_ms: Math.round(performance.now() - started),
      };
    } else {
      this.sendResult(frame.command_id, "failed", "unsupported_command", {});
      return;
    }
    this.completedKeys.add(frame.idempotency_key);
    if (this.completedKeys.size > 200) {
      const oldest = this.completedKeys.values().next().value;
      if (oldest) this.completedKeys.delete(oldest);
    }
    this.sendResult(frame.command_id, "succeeded", null, resultMeta);
    this.callbacks.onEvent(`${frame.command} ${frame.command_id.slice(0, 8)} 已完成`);
  }

  private sendResult(
    commandId: string,
    outcome: "succeeded" | "failed",
    reasonCode: string | null,
    resultMeta: Record<string, unknown>,
  ): void {
    this.send({
      type: "command.result",
      command_id: commandId,
      outcome,
      reason_code: reasonCode,
      result_meta: resultMeta,
    });
  }

  private send(frame: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(frame));
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeat = window.setInterval(() => {
      this.send({ type: "device.heartbeat", capabilities: this.capabilities });
    }, 30_000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeat !== null) window.clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  private scheduleReconnect(): void {
    if (this.manuallyStopped || this.reconnectTimer !== null) return;
    const delay = Math.min(30_000, 1_000 * 2 ** this.reconnectAttempt);
    this.reconnectAttempt += 1;
    this.callbacks.onEvent(`${Math.round(delay / 1000)} 秒后重连`);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private clearTimers(): void {
    this.stopHeartbeat();
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }
}

function asExecute(frame: SignedFrame) {
  if (!isExecuteCommand(frame)) throw new Error("not an execute command");
  return frame;
}

export async function pairDevice(input: {
  hubUrl: string;
  pairingCode: string;
  name: string;
  alias: string | null;
}): Promise<PairResult> {
  return invoke<PairResult>("pair_device", {
    hubUrl: input.hubUrl,
    pairingCode: input.pairingCode,
    name: input.name,
    alias: input.alias,
  });
}

export async function loadAccessToken(): Promise<string | null> {
  return invoke<string | null>("load_device_credential");
}

export async function forgetAccessToken(): Promise<void> {
  await invoke("forget_device_credential");
}

export function screenCapturePermission(request = false): Promise<ScreenPermissionStatus> {
  return invoke<ScreenPermissionStatus>("screen_capture_permission", { request });
}

export function screenCaptureEnvironment(): Promise<ScreenCaptureEnvironment> {
  return invoke<ScreenCaptureEnvironment>("screen_capture_environment");
}

function captureAndUpload(
  commandId: string,
  target: string,
  displayIndex: number | null,
): Promise<DeviceAssetUpload> {
  return invoke<DeviceAssetUpload>("capture_and_upload", {
    commandId,
    target,
    displayIndex,
  });
}
