export interface VoiceSentenceMeta {
  generationId: string | null;
  index: number;
  mime: string;
  sampleRate: number;
  provider: string | null;
}

export interface VoiceVisemeFrame {
  amp: number;
  offsetMs: number;
  durationMs: number;
}

/** 浏览器麦克风：采集任意输入采样率，降采样为服务端要求的 PCM16/16k/mono。 */
export class PcmMicrophoneCapture {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private silentGain: GainNode | null = null;

  async start(onPcm: (chunk: ArrayBuffer) => void): Promise<void> {
    if (this.stream) return;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const context = new AudioContext();
    await context.resume();
    const source = context.createMediaStreamSource(stream);
    // Batch B 先用 ScriptProcessor 做兼容性垂直切片；后续可无缝换 AudioWorklet。
    const processor = context.createScriptProcessor(4096, 1, 1);
    const silentGain = context.createGain();
    silentGain.gain.value = 0;
    processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const resampled = resampleMono(input, context.sampleRate, 16_000);
      if (resampled.length === 0) return;
      onPcm(floatToPcm16(resampled));
    };
    source.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(context.destination);
    this.stream = stream;
    this.context = context;
    this.source = source;
    this.processor = processor;
    this.silentGain = silentGain;
  }

  async stop(): Promise<void> {
    this.processor?.disconnect();
    this.source?.disconnect();
    this.silentGain?.disconnect();
    this.processor = null;
    this.source = null;
    this.silentGain = null;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    const context = this.context;
    this.context = null;
    if (context && context.state !== "closed") await context.close();
  }
}

/**
 * 分句音频播放器。服务端允许同一连接按句切换 PCM / MP3，因此每句根据
 * voice.sentence 的 mime 单独解码；interrupt 会丢弃尚未播放的旧队列。
 */
export class VoicePlaybackQueue {
  private context: AudioContext | null = null;
  private tail: Promise<void> = Promise.resolve();
  private active: AudioBufferSourceNode | null = null;
  private epoch = 0;
  private visemeTimers = new Set<number>();

  /**
   * 移动浏览器要求在用户手势内解锁音频输出。必须在 click/change 处理器
   * 的第一个异步网络等待之前调用；否则稍后收到的 TTS 分句会被静默挂起。
   */
  async unlock(): Promise<boolean> {
    const context = this.ensureAudioContext();
    const source = context.createBufferSource();
    source.buffer = context.createBuffer(1, 1, context.sampleRate);
    source.connect(context.destination);
    source.onended = () => source.disconnect();
    source.start(0);
    if (context.state !== "running") await context.resume();
    return context.state === "running";
  }

  enqueue(
    meta: VoiceSentenceMeta,
    chunks: ArrayBuffer[],
    visemes: VoiceVisemeFrame[] = [],
    onViseme?: (amp: number) => void,
    onError?: (message: string) => void,
  ): void {
    const epoch = this.epoch;
    const payload = concatBuffers(chunks);
    this.tail = this.tail
      .catch(() => undefined)
      .then(async () => {
        if (epoch !== this.epoch || payload.byteLength === 0) return;
        const context = await this.audioContext();
        const buffer = meta.mime.startsWith("audio/pcm")
          ? decodePcm16(context, payload, meta.sampleRate)
          : await context.decodeAudioData(payload.slice(0));
        if (epoch !== this.epoch) return;
        await this.playBuffer(context, buffer, epoch, visemes, onViseme);
      })
      .catch((error: unknown) => {
        onError?.(error instanceof Error ? error.message : "浏览器无法播放语音");
      });
  }

  interrupt(): void {
    this.epoch += 1;
    this.clearVisemeTimers();
    if (this.active) {
      try {
        this.active.stop();
      } catch {
        // 已结束的 source.stop() 会抛 InvalidStateError，可忽略。
      }
      this.active.disconnect();
      this.active = null;
    }
    this.tail = Promise.resolve();
  }

  async close(): Promise<void> {
    this.interrupt();
    const context = this.context;
    this.context = null;
    if (context && context.state !== "closed") await context.close();
  }

  private async audioContext(): Promise<AudioContext> {
    const context = this.ensureAudioContext();
    if (context.state !== "running") await context.resume();
    if (context.state !== "running") {
      throw new Error("浏览器阻止了音频播放，请关闭再开启“文字回复播报”后重试");
    }
    return context;
  }

  private ensureAudioContext(): AudioContext {
    if (!this.context || this.context.state === "closed") this.context = new AudioContext();
    return this.context;
  }

  private playBuffer(
    context: AudioContext,
    buffer: AudioBuffer,
    epoch: number,
    visemes: VoiceVisemeFrame[],
    onViseme?: (amp: number) => void,
  ): Promise<void> {
    return new Promise((resolve) => {
      if (epoch !== this.epoch) {
        resolve();
        return;
      }
      const source = context.createBufferSource();
      this.active = source;
      source.buffer = buffer;
      source.connect(context.destination);
      if (onViseme) {
        for (const frame of visemes) {
          const timer = window.setTimeout(() => {
            this.visemeTimers.delete(timer);
            if (epoch === this.epoch) onViseme(frame.amp);
          }, Math.max(0, frame.offsetMs));
          this.visemeTimers.add(timer);
        }
        const last = visemes.at(-1);
        if (last) {
          const resetTimer = window.setTimeout(() => {
            this.visemeTimers.delete(resetTimer);
            if (epoch === this.epoch) onViseme(0);
          }, Math.max(0, last.offsetMs + last.durationMs));
          this.visemeTimers.add(resetTimer);
        }
      }
      source.onended = () => {
        this.clearVisemeTimers();
        onViseme?.(0);
        source.disconnect();
        if (this.active === source) this.active = null;
        resolve();
      };
      source.start();
    });
  }

  private clearVisemeTimers(): void {
    for (const timer of this.visemeTimers) window.clearTimeout(timer);
    this.visemeTimers.clear();
  }
}

function resampleMono(input: Float32Array, sourceRate: number, targetRate: number): Float32Array {
  if (sourceRate === targetRate) return input.slice();
  if (sourceRate < targetRate) {
    const length = Math.max(1, Math.round((input.length * targetRate) / sourceRate));
    const output = new Float32Array(length);
    const ratio = sourceRate / targetRate;
    for (let index = 0; index < length; index += 1) {
      const position = index * ratio;
      const left = Math.min(input.length - 1, Math.floor(position));
      const right = Math.min(input.length - 1, left + 1);
      const fraction = position - left;
      output[index] = (input[left] ?? 0) * (1 - fraction) + (input[right] ?? 0) * fraction;
    }
    return output;
  }
  const ratio = sourceRate / targetRate;
  const length = Math.max(1, Math.floor(input.length / ratio));
  const output = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    const start = Math.floor(index * ratio);
    const end = Math.min(input.length, Math.max(start + 1, Math.floor((index + 1) * ratio)));
    let total = 0;
    for (let cursor = start; cursor < end; cursor += 1) total += input[cursor] ?? 0;
    output[index] = total / Math.max(1, end - start);
  }
  return output;
}

function floatToPcm16(input: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < input.length; index += 1) {
    const value = Math.max(-1, Math.min(1, input[index] ?? 0));
    const sample = value < 0 ? value * 0x8000 : value * 0x7fff;
    view.setInt16(index * 2, Math.round(sample), true);
  }
  return buffer;
}

function concatBuffers(chunks: ArrayBuffer[]): ArrayBuffer {
  const length = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const output = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(new Uint8Array(chunk), offset);
    offset += chunk.byteLength;
  }
  return output.buffer;
}

function decodePcm16(context: AudioContext, payload: ArrayBuffer, sampleRate: number): AudioBuffer {
  const sampleCount = Math.floor(payload.byteLength / 2);
  const audio = context.createBuffer(1, sampleCount, sampleRate);
  const channel = audio.getChannelData(0);
  const view = new DataView(payload);
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = view.getInt16(index * 2, true);
    channel[index] = sample < 0 ? sample / 0x8000 : sample / 0x7fff;
  }
  return audio;
}
