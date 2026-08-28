import type { PetAudioFrame } from "./client";

const MAX_AUDIO_BYTES = 8 * 1024 * 1024;

interface PendingAudio {
  mime: string;
  sampleRate: number;
  chunks: Uint8Array[];
  bytes: number;
  nextIndex: number;
}

export class PetAudioPlayback {
  private context: AudioContext | null = null;
  private active: AudioBufferSourceNode | null = null;
  private pending = new Map<string, PendingAudio>();

  async handle(frame: PetAudioFrame): Promise<void> {
    if (frame.type === "start" && frame.mime && frame.sampleRate) {
      this.pending.set(frame.requestId, {
        mime: frame.mime,
        sampleRate: frame.sampleRate,
        chunks: [],
        bytes: 0,
        nextIndex: 0,
      });
      return;
    }
    if (frame.type === "failed") {
      this.pending.delete(frame.requestId);
      return;
    }
    const pending = this.pending.get(frame.requestId);
    if (!pending) return;
    if (frame.type === "chunk" && frame.dataB64 && frame.index !== undefined) {
      if (frame.index !== pending.nextIndex) throw new Error("pet_audio_out_of_order");
      const chunk = decodeBase64(frame.dataB64);
      if (pending.bytes + chunk.byteLength > MAX_AUDIO_BYTES) {
        this.pending.delete(frame.requestId);
        throw new Error("pet_audio_too_large");
      }
      pending.chunks.push(chunk);
      pending.bytes += chunk.byteLength;
      pending.nextIndex += 1;
      return;
    }
    if (frame.type !== "end") return;
    this.pending.delete(frame.requestId);
    if (
      frame.chunks !== pending.nextIndex ||
      frame.bytes !== pending.bytes ||
      pending.bytes === 0
    ) throw new Error("pet_audio_length_mismatch");
    await this.play(pending);
  }

  interrupt(): void {
    this.pending.clear();
    this.stopActive();
  }

  private stopActive(): void {
    if (!this.active) return;
    try {
      this.active.stop();
    } catch {
      // 已播放结束的节点无需再次停止。
    }
    this.active.disconnect();
    this.active = null;
  }

  private async play(audio: PendingAudio): Promise<void> {
    const context = await this.audioContext();
    const payload = concatChunks(audio.chunks, audio.bytes);
    const encoded = new ArrayBuffer(payload.byteLength);
    new Uint8Array(encoded).set(payload);
    const buffer = audio.mime.startsWith("audio/pcm")
      ? decodePcm16(context, payload, audio.sampleRate)
      : await context.decodeAudioData(encoded);
    this.stopActive();
    const source = context.createBufferSource();
    this.active = source;
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => {
      source.disconnect();
      if (this.active === source) this.active = null;
    };
    source.start();
  }

  private async audioContext(): Promise<AudioContext> {
    if (!this.context || this.context.state === "closed") this.context = new AudioContext();
    if (this.context.state === "suspended") await this.context.resume();
    return this.context;
  }
}

export function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function concatChunks(chunks: Uint8Array[], bytes: number): Uint8Array {
  const result = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function decodePcm16(
  context: AudioContext,
  payload: Uint8Array,
  sampleRate: number,
): AudioBuffer {
  const samples = Math.floor(payload.byteLength / 2);
  const buffer = context.createBuffer(1, samples, sampleRate);
  const channel = buffer.getChannelData(0);
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  for (let index = 0; index < samples; index += 1) {
    channel[index] = view.getInt16(index * 2, true) / 32768;
  }
  return buffer;
}
