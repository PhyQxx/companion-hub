import { createHash } from "node:crypto";
import { MiGPT } from "@mi-gpt/next";
import { loadGatewayConfig } from "./config.js";
import { HubClient } from "./hub-client.js";

const config = await loadGatewayConfig();
const hubUrl = process.env.ARIA_HUB_XIAOAI_URL?.trim();
if (!hubUrl) throw new Error("Missing required environment variable: ARIA_HUB_XIAOAI_URL");
const trigger = config.trigger_prefix;
const displayName = config.speaker_name;
const statePath = process.env.XIAOAI_STATE_PATH || "/data/state.json";
const ttsSiid = config.tts_siid;
const ttsAiid = config.tts_aiid;

if ((ttsSiid === undefined) !== (ttsAiid === undefined)) {
  throw new Error("XIAOAI_TTS_SIID and XIAOAI_TTS_AIID must be set together");
}

const hub = new HubClient({
  url: hubUrl,
  token: config.gateway_token,
  did: config.speaker_name,
  displayName,
  model: config.model,
  statePath,
});

await hub.connect();

await MiGPT.start({
  speaker: {
    userId: config.xiaomi_user_id,
    password: config.xiaomi_password,
    passToken: config.xiaomi_pass_token,
    did: config.speaker_name,
  },
  // Hub owns the model and persona. These placeholders are never called because
  // every accepted query returns handled=true from onMessage.
  openai: {
    model: "aria-hub",
    baseURL: "http://127.0.0.1/unused",
    apiKey: "unused",
  },
  prompt: { system: "由 Aria Companion Hub 处理。" },
  callAIKeywords: [trigger],
  async onMessage(engine: any, message: any) {
    const rawText = typeof message?.text === "string" ? message.text.trim() : "";
    const text = stripTrigger(rawText, trigger);
    if (text === undefined) return undefined;

    const eventId = stableEventId(config.speaker_name, message, rawText);
    try {
      await engine.speaker.abortXiaoAI();
      await hub.ask(eventId, text || "你好", async (sentence) => {
        process.stdout.write(`XiaoAI TTS: ${sentence}\n`);
        if (ttsSiid !== undefined && ttsAiid !== undefined) {
          const ok = await engine.MiOT.doAction(ttsSiid, ttsAiid, sentence);
          if (!ok) throw new Error("MIoT TTS action failed");
        } else {
          const ok = await engine.MiNA.play({ text: sentence });
          if (ok === false) throw new Error("MiNA TTS playback failed");
        }
        // Both APIs acknowledge command submission, not playback completion.
        // Avoid the next sentence pre-empting the current one on stock firmware.
        await sleep(estimatedSpeechMs(sentence));
      });
    } catch (error) {
      process.stderr.write(`XiaoAI turn failed: ${String(error)}\n`);
      const failureText = "中枢暂时不可用，请稍后再试";
      if (ttsSiid !== undefined && ttsAiid !== undefined) {
        await engine.MiOT.doAction(ttsSiid, ttsAiid, failureText);
      } else {
        await engine.MiNA.play({ text: failureText });
      }
    }
    return { handled: true };
  },
} as any);

function stripTrigger(text: string, prefix: string): string | undefined {
  if (!text.startsWith(prefix)) return undefined;
  return text.slice(prefix.length).replace(/^[，,：:\s]+/u, "").trim();
}

function stableEventId(did: string, message: any, text: string): string {
  const sourceId = message?.id ?? message?.time ?? message?.timestamp ?? Date.now();
  return createHash("sha256")
    .update(`${did}:${String(sourceId)}:${text}`)
    .digest("hex");
}

function estimatedSpeechMs(text: string): number {
  return Math.min(12_000, Math.max(800, [...text].length * 230));
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
