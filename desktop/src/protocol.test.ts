import { webcrypto } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  consumeScreenCaptureGrant,
  createScreenCaptureGrant,
  parseScreenCaptureRequest,
  screenCaptureGrantActive,
} from "./client";
import { canonicalFrame, signFrame, verifyFrame, websocketUrl } from "./protocol";

Object.defineProperty(globalThis, "crypto", { value: webcrypto });

describe("device command protocol", () => {
  it("accepts active-window and bounded explicit display capture targets", () => {
    expect(parseScreenCaptureRequest({})).toEqual({
      target: "main_display",
      displayIndex: null,
    });
    expect(
      parseScreenCaptureRequest({ target: "display", display_index: 2 }),
    ).toEqual({ target: "display", displayIndex: 2 });
    expect(parseScreenCaptureRequest({ target: "display", display_index: 0 })).toBeNull();
    expect(parseScreenCaptureRequest({ target: "display", display_index: 2.5 })).toBeNull();
    expect(parseScreenCaptureRequest({ target: "active_window" })).toEqual({
      target: "active_window",
      displayIndex: null,
    });
    expect(
      parseScreenCaptureRequest({ target: "active_window", display_index: 1 }),
    ).toBeNull();
    expect(
      parseScreenCaptureRequest({ target: "main_display", display_index: 1 }),
    ).toBeNull();
  });

  it("matches Python sort_keys canonical JSON ordering", () => {
    expect(
      canonicalFrame({
        type: "command.execute",
        z: 1,
        args_json: '{"alpha":true,"target":"active_window"}',
        signature: "ignored",
      }),
    ).toBe(
      '{"args_json":"{\\"alpha\\":true,\\"target\\":\\"active_window\\"}","type":"command.execute","z":1}',
    );
  });

  it("detects signed frame tampering", async () => {
    const frame = {
      type: "command.execute",
      command_id: "1",
      args_json: '{"ratio":1.0,"target":"active_window"}',
    };
    const signed = { ...frame, signature: await signFrame("device-secret", frame) };
    expect(await verifyFrame("device-secret", signed)).toBe(true);
    expect(
      await verifyFrame("device-secret", {
        ...signed,
        args_json: '{"ratio":1.0,"target":"other_window"}',
      }),
    ).toBe(false);
  });

  it("matches the Python signer for a fixed command frame", async () => {
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
    expect(await signFrame("aria-device-test-secret-0123456789", frame)).toBe(
      "453be22a5a1be528b5b2d97f52ffbd9c02a274dafed88b8d084088f1692eebb5",
    );
  });

  it("converts Hub HTTP URLs to the device WebSocket endpoint", () => {
    expect(websocketUrl("https://hub.example/base")).toBe("wss://hub.example/ws/devices");
    expect(websocketUrl("http://127.0.0.1:8000")).toBe(
      "ws://127.0.0.1:8000/ws/devices",
    );
  });

  it("grants exactly one screen capture for at most five minutes", () => {
    const now = new Date("2026-08-24T12:00:00Z").getTime();
    const grant = createScreenCaptureGrant(now);
    expect(screenCaptureGrantActive(grant, now + 299_999)).toBe(true);
    expect(screenCaptureGrantActive(grant, now + 300_000)).toBe(false);
    expect(consumeScreenCaptureGrant(grant, now + 1)).toBeNull();
    expect(consumeScreenCaptureGrant(grant, now + 300_000)).toBeNull();
  });
});
