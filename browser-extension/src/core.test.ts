import { webcrypto } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  MAX_VISIBLE_TEXT_CHARS,
  canonicalFrame,
  normalizeHubUrl,
  sanitizeBrowserDocument,
  signFrame,
  verifyFrame,
  websocketUrl,
} from "./core";

Object.defineProperty(globalThis, "crypto", { value: webcrypto });

describe("browser bridge core", () => {
  it("matches the Hub canonical signing vector", async () => {
    const frame = {
      proto_version: 1,
      type: "command.execute",
      command_id: "018f5f61-2a65-7a21-a835-1a2b3c4d5e6f",
      command: "device.ping",
      args_json: '{"ratio":1.0,"target":"活动窗口"}',
      idempotency_key: "desktop-vector-001",
      issued_at: "2026-08-23T12:00:00+00:00",
      expires_at: "2026-08-23T12:00:30+00:00",
    };
    expect(canonicalFrame(frame)).toContain('"args_json"');
    const signature = await signFrame("aria-device-test-secret-0123456789", frame);
    expect(signature).toBe("453be22a5a1be528b5b2d97f52ffbd9c02a274dafed88b8d084088f1692eebb5");
    expect(await verifyFrame("aria-device-test-secret-0123456789", { ...frame, signature })).toBe(true);
  });

  it("normalizes Hub and WebSocket URLs", () => {
    expect(normalizeHubUrl(" https://hub.example/base/ ")).toBe("https://hub.example/base");
    expect(websocketUrl("https://hub.example/base")).toBe("wss://hub.example/ws/devices");
    expect(() => normalizeHubUrl("file:///tmp/hub")).toThrow();
  });

  it("keeps only bounded page fields", () => {
    const document = sanitizeBrowserDocument({
      title: "A".repeat(600),
      origin: "https://example.com",
      language: "zh-CN",
      text: ` hello\n\tworld ${"x".repeat(MAX_VISIBLE_TEXT_CHARS)}`,
    });
    expect(document.title).toHaveLength(500);
    expect(document.origin).toBe("https://example.com");
    expect(document.text).not.toContain("\n");
    expect(document.text.length).toBe(MAX_VISIBLE_TEXT_CHARS);
    expect(document.truncated).toBe(true);
  });
});
