import { invoke } from "@tauri-apps/api/core";
import {
  isExecuteCommand,
  parseCommandArgs,
  type SignedFrame,
  verifyFrame,
  websocketUrl,
} from "./protocol";

export const DESKTOP_BASE_CAPABILITIES = ["device.ping"] as const;

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
  target: "main_display" | "display";
  displayIndex: number | null;
}

export function parseScreenCaptureRequest(
  args: Record<string, unknown>,
): ScreenCaptureRequest | null {
  const target = args.target ?? "main_display";
  const displayIndex = args.display_index;
  if (target === "main_display") {
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

export type ConnectionState = "unpaired" | "connecting" | "online" | "offline" | "error";

export interface ClientCallbacks {
  onState: (state: ConnectionState, detail: string) => void;
  onEvent: (message: string) => void;
  authorizeScreenCapture: () => "allowed" | "screen_locked" | "screen_capture_not_granted";
}

export class DeviceConnection {
  private socket: WebSocket | null = null;
  private heartbeat: number | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private manuallyStopped = false;
  private completedKeys = new Set<string>();
  private cancelledCommands = new Set<string>();

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
    } else if (frame.command === "screen.capture") {
      if (!this.capabilities.includes("screen.capture")) {
        this.sendResult(frame.command_id, "failed", "screen_capture_unavailable", {});
        return;
      }
      const request = parseScreenCaptureRequest(args);
      if (request === null) {
        this.sendResult(frame.command_id, "failed", "invalid_command_args", {});
        return;
      }
      const authorization = this.callbacks.authorizeScreenCapture();
      if (authorization !== "allowed") {
        this.sendResult(frame.command_id, "failed", authorization, {});
        return;
      }
      const uploaded = await captureAndUpload(
        frame.command_id,
        request.target,
        request.displayIndex,
      );
      if (this.cancelledCommands.has(frame.command_id)) {
        this.sendResult(frame.command_id, "failed", "command_cancelled", {});
        return;
      }
      resultMeta = { ...uploaded, latency_ms: Math.round(performance.now() - started) };
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
