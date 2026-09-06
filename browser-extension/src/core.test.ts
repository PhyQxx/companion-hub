import { webcrypto } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  MAX_FORM_FIELDS,
  MAX_VISIBLE_TEXT_CHARS,
  buildFormFieldDescriptors,
  canonicalFrame,
  isValidFormRef,
  normalizeHubUrl,
  parseFormFillFields,
  sanitizeBrowserDocument,
  sanitizeFormFillOutcome,
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

  it("assigns ordered refs and masks password values in form descriptors", () => {
    const { fields, truncated, origin } = buildFormFieldDescriptors(
      [
        {
          tag: "input", type: "text", label: "用户名", placeholder: "", name: "username",
          value: "aria", required: true, formIndex: 0,
        },
        {
          tag: "input", type: "password", label: "密码", placeholder: "", name: "secret",
          value: "super-secret", required: true, formIndex: 0,
        },
        {
          tag: "textarea", type: "textarea", label: "备注  多行", placeholder: "备注", name: "note",
          value: "  hello \n world  ", required: false, formIndex: -1,
        },
      ],
      "https://example.com/login?next=/home",
    );
    expect(truncated).toBe(false);
    expect(origin).toBe("https://example.com");
    expect(fields.map((field) => field.ref)).toEqual(["f0", "f1", "f2"]);
    expect(fields[0]).toMatchObject({ formRef: "form0", label: "用户名", value: "aria", required: true });
    expect(fields[1]?.value).toBe(""); // 密码框永不回传
    expect(fields[2]).toMatchObject({ formRef: null, placeholder: "备注", value: "hello world" });
  });

  it("caps form descriptors at the bounded field count", () => {
    const controls = Array.from({ length: MAX_FORM_FIELDS + 5 }, (_, index) => ({
      tag: "input", type: "text", label: `f${index}`, placeholder: "", name: "",
      value: "", required: false, formIndex: 0,
    }));
    const { fields, truncated } = buildFormFieldDescriptors(controls, "https://example.com");
    expect(fields).toHaveLength(MAX_FORM_FIELDS);
    expect(truncated).toBe(true);
    expect(buildFormFieldDescriptors("not-a-list", "https://example.com").fields).toEqual([]);
  });

  it("validates fill plans strictly", () => {
    expect(parseFormFillFields([
      { ref: "f0", value: "aria" },
      { ref: "f2", value: "x".repeat(5_000) },
    ])).toEqual([
      { ref: "f0", value: "aria" },
      { ref: "f2", value: "x".repeat(5_000) },
    ]);
    expect(parseFormFillFields([])).toBeNull();
    expect(parseFormFillFields([{ ref: "field-1", value: "x" }])).toBeNull();
    expect(parseFormFillFields([{ ref: "f0", value: "" }])).toBeNull();
    expect(parseFormFillFields([{ ref: "f0", value: "x".repeat(5_001) }])).toBeNull();
    expect(parseFormFillFields([{ ref: "f0", value: 42 }])).toBeNull();
    expect(parseFormFillFields({ ref: "f0", value: "x" })).toBeNull();
  });

  it("accepts only form.read-shaped form refs", () => {
    expect(isValidFormRef("form0")).toBe(true);
    expect(isValidFormRef("form12")).toBe(true);
    expect(isValidFormRef("f0")).toBe(false);
    expect(isValidFormRef("form")).toBe(false);
    expect(isValidFormRef(3)).toBe(false);
  });

  it("sanitizes fill outcomes and rejects malformed payloads", () => {
    expect(
      sanitizeFormFillOutcome(
        { filled: 2, total: 3, skipped: [{ ref: "f1", reason: "password_field" }, { ref: "bad" }], pageUrl: "https://example.com/x" },
        "https://example.com",
      ),
    ).toEqual({
      filled: 2,
      total: 3,
      skipped: [{ ref: "f1", reason: "password_field" }],
      pageUrl: "https://example.com/x",
    });
    expect(sanitizeFormFillOutcome({ filled: -1, total: 1 }, "https://e.com")).toBeNull();
    expect(sanitizeFormFillOutcome("nope", "https://e.com")).toBeNull();
  });
});
