import { invoke } from "@tauri-apps/api/core";
import {
  isExecuteCommand,
  parseCommandArgs,
  type SignedFrame,
  verifyFrame,
  websocketUrl,
} from "./protocol";

export const DESKTOP_CAPABILITIES = ["device.ping"] as const;

export interface PairResult {
  device_id: string;
  owner_user_id: string;
  name: string;
  alias: string | null;
  hub_url: string;
}

export interface StoredClientConfig extends PairResult {}

export type ConnectionState = "unpaired" | "connecting" | "online" | "offline" | "error";

export interface ClientCallbacks {
  onState: (state: ConnectionState, detail: string) => void;
  onEvent: (message: string) => void;
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
    private callbacks: ClientCallbacks,
  ) {}

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
          capabilities: DESKTOP_CAPABILITIES,
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
    try {
      parseCommandArgs(frame);
    } catch {
      this.sendResult(frame.command_id, "failed", "invalid_command_args", {});
      return;
    }
    if (this.completedKeys.has(frame.idempotency_key)) {
      this.sendResult(frame.command_id, "succeeded", null, { duplicate: true });
      return;
    }
    if (this.cancelledCommands.has(frame.command_id)) {
      this.sendResult(frame.command_id, "failed", "command_cancelled", {});
      return;
    }
    if (frame.command !== "device.ping") {
      this.sendResult(frame.command_id, "failed", "unsupported_command", {});
      return;
    }
    const started = performance.now();
    this.completedKeys.add(frame.idempotency_key);
    if (this.completedKeys.size > 200) {
      const oldest = this.completedKeys.values().next().value;
      if (oldest) this.completedKeys.delete(oldest);
    }
    this.sendResult(frame.command_id, "succeeded", null, {
      latency_ms: Math.round(performance.now() - started),
      client_version: "0.1.0",
      platform: navigator.platform,
    });
    this.callbacks.onEvent(`device.ping ${frame.command_id.slice(0, 8)} 已完成`);
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
      this.send({ type: "device.heartbeat", capabilities: DESKTOP_CAPABILITIES });
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
