import { webcrypto } from "node:crypto";
import { JSDOM } from "jsdom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
let dom: JSDOM;
let current: { id: number; windowId: number; url: string };
let bridge: typeof import("./background");
let activate: (info: { tabId: number }) => void;
let update: (id: number, change: { status?: string; url?: string }) => void;
let inject: ReturnType<typeof vi.fn>;
beforeEach(async () => {
  vi.resetModules();
  vi.stubGlobal("crypto", webcrypto);
  dom = new JSDOM('<form><input name="user"></form>', {
    url: "https://example.com", pretendToBeVisual: true, runScripts: "outside-only",
  });
  current = { id: 1, windowId: 1, url: dom.window.location.href };
  inject = vi.fn(async ({ func, args, world, target }) => {
    expect(world).toBe("ISOLATED"); expect(target.tabId).toBe(1);
    return [{ result: dom.window.eval(`(${func.toString()})(${JSON.stringify(args[0])})`) }];
  });
  const event = () => ({ addListener: vi.fn() });
  vi.stubGlobal("chrome", {
    runtime: { onStartup: event(), onInstalled: event(), onMessage: event() },
    storage: { onChanged: event(), local: { setAccessLevel: vi.fn(), get: vi.fn(async () => ({})), set: vi.fn() } },
    tabs: { query: vi.fn(async () => [current]), onRemoved: event(),
      onActivated: { addListener: (fn: typeof activate) => { activate = fn; } },
      onUpdated: { addListener: (fn: typeof update) => { update = fn; } },
    },
    scripting: { executeScript: inject },
  });
  bridge = await import("./background");
});
afterEach(() => { dom.window.close(); vi.unstubAllGlobals(); });

it("rejects missing/stale snapshots and a different active tab before injection", async () => {
  const result = await bridge.readFormFields();
  const snapshotId = String(result.snapshot_id);
  current.id = 2;
  await expect(bridge.fillFormFields({ snapshotId, fields: [{ ref: "f0", value: "wrong" }] })).rejects.toThrow("form_snapshot_changed");
  expect(inject).toHaveBeenCalledTimes(1);
  current.id = 1;
  await expect(bridge.fillFormFields({ snapshotId, fields: [{ ref: "f0", value: "wrong" }] })).rejects.toThrow("form_snapshot_missing");
});

it("invalidates snapshots on switching away and back or same-URL reload", async () => {
  let result = await bridge.readFormFields();
  activate({ tabId: 2 }); activate({ tabId: 1 });
  await expect(bridge.submitForm({ snapshotId: String(result.snapshot_id), formRef: "form0" })).rejects.toThrow("form_snapshot_missing");
  result = await bridge.readFormFields();
  update(1, { status: "loading" });
  await expect(bridge.submitForm({ snapshotId: String(result.snapshot_id), formRef: "form0" })).rejects.toThrow("form_snapshot_missing");
  expect(inject).toHaveBeenCalledTimes(2);
});

it("keeps multibyte read receipts below the Hub cap and rejects unreturned refs", async () => {
  dom.window.document.body.innerHTML = `<form>${Array.from({ length: 50 }, (_, i) =>
    `<label>${"字段".repeat(60)}<input name="field${i}" value="${"值".repeat(120)}"></label>`).join("")}</form>`;
  const result = await bridge.readFormFields();
  expect(new TextEncoder().encode(JSON.stringify(result)).length).toBeLessThan(4096);
  expect(result.truncated).toBe(true);
  expect(Number(result.field_count)).toBeGreaterThan(0);
  await expect(bridge.fillFormFields({ snapshotId: String(result.snapshot_id), fields: [{ ref: "f49", value: "wrong" }] })).rejects.toThrow("control_not_found");
  expect(inject).toHaveBeenCalledTimes(1);
});

it("fills a bound field and consumes the submission snapshot", async () => {
  const result = await bridge.readFormFields();
  const snapshotId = String(result.snapshot_id);
  expect(await bridge.fillFormFields({ snapshotId, fields: [{ ref: "f0", value: "aria" }] })).toMatchObject({ filled: 1 });
  const submit = vi.fn();
  dom.window.HTMLFormElement.prototype.requestSubmit = submit;
  expect(await bridge.submitForm({ snapshotId, formRef: "form0" })).toMatchObject({ submitted: true });
  await expect(bridge.submitForm({ snapshotId, formRef: "form0" })).rejects.toThrow("form_snapshot_missing");
  expect(submit).toHaveBeenCalledTimes(1);
});
