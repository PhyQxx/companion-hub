import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import WebSocket from "ws";

type JsonObject = Record<string, unknown>;

interface PendingTurn {
  generationId?: string;
  cancelled: boolean;
  speechChain: Promise<void>;
  speak: (text: string) => Promise<void>;
  resolve: () => void;
  reject: (error: Error) => void;
}

interface GatewayState {
  conversationId?: string;
}

export interface HubClientOptions {
  url: string;
  token: string;
  did: string;
  displayName: string;
  model?: string;
  statePath: string;
}

export class HubClient {
  private socket?: WebSocket;
  private pending?: PendingTurn;
  private ready?: Promise<void>;
  private state: GatewayState = {};

  constructor(private readonly options: HubClientOptions) {}

  async connect(): Promise<void> {
    this.state = await this.loadState();
    this.ready = new Promise<void>((resolve, reject) => {
      let handshakeComplete = false;
      const socket = new WebSocket(this.options.url);
      this.socket = socket;
      socket.on("open", () => {
        socket.send(JSON.stringify({
          type: "authenticate",
          gateway_token: this.options.token,
        }));
      });
      socket.on("message", (data) => {
        void this.handle(JSON.parse(data.toString()) as JsonObject, () => {
          handshakeComplete = true;
          resolve();
        });
      });
      socket.on("error", reject);
      socket.on("close", () => {
        const error = new Error("Hub XiaoAI WebSocket disconnected");
        if (!handshakeComplete) reject(error);
        if (this.pending) this.pending.cancelled = true;
        this.pending?.reject(error);
        this.pending = undefined;
        // Let Docker restart the process and establish a fresh authenticated
        // session instead of leaving MiGPT alive with a dead Hub connection.
        setTimeout(() => process.exit(1), 100);
      });
    });
    await this.ready;
  }

  async ask(
    eventId: string,
    text: string,
    speak: (sentence: string) => Promise<void>,
  ): Promise<void> {
    await this.ready;
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("Hub XiaoAI WebSocket is not connected");
    }
    if (this.pending) {
      this.socket.send(JSON.stringify({
        type: "xiaoai.cancel",
        generation_id: this.pending.generationId,
      }));
      this.pending.cancelled = true;
      this.pending.reject(new Error("superseded by a newer XiaoAI query"));
    }
    const completion = new Promise<void>((resolve, reject) => {
      this.pending = {
        cancelled: false,
        speechChain: Promise.resolve(),
        speak,
        resolve,
        reject,
      };
    });
    this.socket.send(JSON.stringify({
      type: "xiaoai.query",
      event_id: eventId,
      text,
      privacy_level: "L1",
    }));
    await completion;
  }

  private async handle(frame: JsonObject, authReady: () => void): Promise<void> {
    const type = String(frame.type ?? "");
    if (type === "auth.accepted") {
      this.socket?.send(JSON.stringify({
        type: "xiaoai.hello",
        did: this.options.did,
        display_name: this.options.displayName,
        model: this.options.model,
        conversation_id: this.state.conversationId,
        capabilities: { tts: true, play_url: false, interrupt: true },
      }));
      return;
    }
    if (type === "xiaoai.ready") {
      const conversationId = frame.conversation_id;
      if (typeof conversationId === "string") {
        this.state.conversationId = conversationId;
        await this.saveState();
      }
      authReady();
      return;
    }
    if (type === "turn.accepted" && this.pending) {
      const generationId = frame.generation_id;
      if (typeof generationId === "string") this.pending.generationId = generationId;
      return;
    }
    if (type === "reply.sentence" && this.pending) {
      const text = frame.text;
      if (typeof text === "string" && text.trim()) {
        const pending = this.pending;
        pending.speechChain = pending.speechChain.then(async () => {
          if (!pending.cancelled) await pending.speak(text);
        });
      }
      return;
    }
    if (type === "reply.committed" && this.pending) {
      const pending = this.pending;
      try {
        await pending.speechChain;
        pending.resolve();
      } catch (error) {
        pending.reject(asError(error));
      }
      if (this.pending === pending) this.pending = undefined;
      return;
    }
    if ((type === "turn.failed" || type === "turn.cancelled") && this.pending) {
      const pending = this.pending;
      this.pending = undefined;
      pending.reject(new Error(String(frame.reason_code ?? type)));
      pending.cancelled = true;
      return;
    }
    if ((type === "xiaoai.query.duplicate" || type === "xiaoai.error") && this.pending) {
      const pending = this.pending;
      this.pending = undefined;
      pending.cancelled = true;
      pending.reject(new Error(String(frame.reason_code ?? type)));
    }
  }

  private async loadState(): Promise<GatewayState> {
    try {
      return JSON.parse(await readFile(this.options.statePath, "utf8")) as GatewayState;
    } catch {
      return {};
    }
  }

  private async saveState(): Promise<void> {
    await mkdir(dirname(this.options.statePath), { recursive: true });
    await writeFile(this.options.statePath, JSON.stringify(this.state, null, 2), "utf8");
  }
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
