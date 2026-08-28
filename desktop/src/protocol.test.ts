import { webcrypto } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  captureFailureCode,
  consumeScreenCaptureGrant,
  createScreenCaptureGrant,
  parseNotificationRequest,
  parseAvatarControl,
  parsePetAudioFrame,
  parsePetMessageState,
  parseScreenCaptureRequest,
  screenCaptureGrantActive,
} from "./client";
import { canonicalFrame, signFrame, verifyFrame, websocketUrl } from "./protocol";

Object.defineProperty(globalThis, "crypto", { value: webcrypto });

describe("device command protocol", () => {
  it("accepts bounded ephemeral avatar controls", () => {
    expect(parseAvatarControl({
      type: "avatar.control",
      sequence: 7,
      control: {
        emotion: "happy",
        speaking: true,
        lipSyncMilli: 1000,
        motion: "TapBody:0",
        text: "你好呀",
      },
    })).toEqual({
      sequence: 7,
      emotion: "happy",
      motion: "TapBody:0",
      text: "你好呀",
      lipSync: 1,
      speaking: true,
    });
    expect(parseAvatarControl({ type: "avatar.control", sequence: 0, control: {} }))
      .toBeNull();
    expect(parseAvatarControl({ type: "avatar.control", sequence: 1, control: {} }))
      .toBeNull();
  });
  it("accepts bounded proactive notification requests", () => {
    expect(
      parseNotificationRequest({ title: "Aria", body: "该回家了", privacy_level: "L1" }),
    ).toEqual({ title: "Aria", body: "该回家了", privacyLevel: "L1" });
    expect(parseNotificationRequest({ title: "", body: "hello", privacy_level: "L1" }))
      .toBeNull();
    expect(parseNotificationRequest({ title: "Aria", body: "hello", privacy_level: "L3" }))
      .toBeNull();
  });

  it("accepts only bounded pet message lifecycle frames", () => {
    expect(parsePetMessageState({
      type: "pet.message.accepted",
      request_id: "018f5f61-2a65-7a21-a835-1a2b3c4d5e6f",
    })).toEqual({
      requestId: "018f5f61-2a65-7a21-a835-1a2b3c4d5e6f",
      status: "accepted",
    });
    expect(parsePetMessageState({
      type: "pet.message.failed",
      request_id: "request-1",
      reason_code: "turn_in_progress",
    })).toEqual({
      requestId: "request-1",
      status: "failed",
      reasonCode: "turn_in_progress",
    });
    expect(parsePetMessageState({
      type: "pet.message.completed",
      request_id: "request-2",
      result: { audio_requested: true, audio_delivered: true },
    })).toEqual({
      requestId: "request-2",
      status: "completed",
      audioDelivered: true,
    });
    expect(parsePetMessageState({ type: "pet.message.completed", request_id: 3 }))
      .toBeNull();
    expect(parsePetMessageState({ type: "pet.message.failed", request_id: "x", reason_code: 3 }))
      .toBeNull();
  });

  it("accepts bounded signed pet audio frames", () => {
    expect(parsePetAudioFrame({
      type: "pet.audio.start",
      request_id: "request-1",
      mime: "audio/pcm;rate=24000",
      sample_rate: 24_000,
    })).toEqual({
      requestId: "request-1",
      type: "start",
      mime: "audio/pcm;rate=24000",
      sampleRate: 24_000,
    });
    expect(parsePetAudioFrame({
      type: "pet.audio.chunk",
      request_id: "request-1",
      index: 0,
      data_b64: "AQI=",
    })).toEqual({ requestId: "request-1", type: "chunk", index: 0, dataB64: "AQI=" });
    expect(parsePetAudioFrame({
      type: "pet.audio.end",
      request_id: "request-1",
      chunks: 1,
      bytes: 2,
    })).toEqual({ requestId: "request-1", type: "end", chunks: 1, bytes: 2 });
    expect(parsePetAudioFrame({
      type: "pet.audio.chunk",
      request_id: "request-1",
      index: -1,
      data_b64: "not base64!",
    })).toBeNull();
  });

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

  it("accepts the interactive picker target without a display index", () => {
    expect(parseScreenCaptureRequest({ target: "interactive" })).toEqual({
      target: "interactive",
      displayIndex: null,
    });
    expect(
      parseScreenCaptureRequest({ target: "interactive", display_index: 1 }),
    ).toBeNull();
  });

  it("maps structured capture errors to command reason codes", () => {
    expect(captureFailureCode({ code: "picker_cancelled", message: "用户取消" }))
      .toBe("picker_cancelled");
    expect(captureFailureCode({ code: "picker_timeout", message: "" }))
      .toBe("picker_timeout");
    expect(captureFailureCode("设备已锁屏，拒绝截图")).toBe("screen_locked");
    expect(captureFailureCode("尚未获得屏幕录制权限")).toBe("screen_capture_not_granted");
    expect(captureFailureCode("别的错误")).toBe("command_execution_failed");
    expect(captureFailureCode(null)).toBe("command_execution_failed");
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

  it("matches the Python signer for integer avatar lip sync", async () => {
    const frame = {
      proto_version: 1,
      type: "avatar.control",
      sequence: 7,
      sent_at: "2026-08-28T06:00:00+00:00",
      control: { emotion: "happy", lipSyncMilli: 700, speaking: true },
    };
    expect(await signFrame("aria-device-test-secret-0123456789", frame)).toBe(
      "2152788d406cb1f572e03719f58b36f360422fb5eebf7d015a16dceadd4f9f9e",
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
