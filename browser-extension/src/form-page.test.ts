import { JSDOM } from "jsdom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formPageOperation } from "./form-page";

const id = "a".repeat(32);
let dom: JSDOM;
// Execute the serialized injection, as Chrome does: no access to module closures.
function run(request: Parameters<typeof formPageOperation>[0]): Record<string, unknown> {
  return dom.window.eval(`(${formPageOperation.toString()})(${JSON.stringify(request)})`) as Record<string, unknown>;
}
const read = () => run({ operation: "read", snapshotId: id });
const fill = (fields = [{ ref: "f0", value: "aria" }]) => run({ operation: "fill", snapshotId: id, fields });
const input = (name: string) => dom.window.document.querySelector<HTMLInputElement>(`[name="${name}"]`)!;

beforeEach(() => {
  dom = new JSDOM(`<!doctype html><form action="/submit"><label>用户名<input name="user"></label>
    <input name="email"><input type="password" name="password" value="never-export">
    <select name="choice"><option value="a">A</option><option value="b">B</option></select></form>`,
  { url: "https://example.com/login", pretendToBeVisual: true, runScripts: "outside-only" });
});
afterEach(() => { dom.window.close(); vi.restoreAllMocks(); });

describe("isolated form snapshot", () => {
  it("masks passwords before crossing the executeScript boundary and skips password writes", () => {
    const result = read();
    expect(JSON.stringify(result)).not.toContain("never-export");
    expect(fill([{ ref: "f0", value: "aria" }, { ref: "f2", value: "overwrite" }])).toMatchObject({
      ok: true, filled: 1, skipped: [{ ref: "f2", reason: "password_field" }],
    });
    expect(input("password").value).toBe("never-export");
  });

  it.each(["insert", "remove", "reorder", "replace", "rename", "form_action", "form_replace", "hide", "value", "option"])(
    "rejects %s before any write or submit", change => {
      read();
      const doc = dom.window.document;
      switch (change) {
        case "insert": doc.querySelector("form")!.prepend(doc.createElement("input")); break;
        case "remove": input("email").remove(); break;
        case "reorder": doc.querySelector("form")!.append(input("user")); break;
        case "replace": input("user").replaceWith(input("user").cloneNode(true)); break;
        case "rename": input("user").name = "other"; break;
        case "form_action": doc.querySelector("form")!.action = "https://other.example/submit"; break;
        case "form_replace": doc.querySelector("form")!.replaceWith(doc.querySelector("form")!.cloneNode(true)); break;
        case "hide": input("email").style.display = "none"; break;
        case "value": input("email").value = "external edit"; break;
        case "option": doc.querySelector("option")!.value = "changed"; break;
      }
      const submit = vi.fn();
      dom.window.HTMLFormElement.prototype.requestSubmit = submit;
      expect(fill()).toMatchObject({ ok: false, reason: "form_snapshot_changed", filled: 0 });
      expect(run({ operation: "submit", snapshotId: id, formRef: "form0" }).ok).toBe(false);
      expect(submit).not.toHaveBeenCalled();
      expect(doc.querySelector<HTMLInputElement>("[name='user']")?.value ?? "").not.toBe("aria");
    },
  );

  it("fails closed after navigation, new reads, expiry or lost page state", () => {
    read();
    dom.reconfigure({ url: "https://example.com/other" });
    expect(fill().reason).toBe("form_snapshot_changed");
    read();
    run({ operation: "read", snapshotId: "b".repeat(32) });
    expect(fill().reason).toBe("form_snapshot_missing");
    read();
    vi.spyOn(dom.window.Date, "now").mockReturnValue(Date.now() + 6 * 60_000);
    expect(fill().reason).toBe("form_snapshot_expired");
    expect(fill().reason).toBe("form_snapshot_missing");
  });

  it("preflights all fields and options without partially writing invalid plans", () => {
    for (const fields of [
      [{ ref: "f0", value: "aria" }, { ref: "f49", value: "missing" }],
      [{ ref: "f0", value: "aria" }, { ref: "f3", value: "missing-option" }],
      [{ ref: "f0", value: "aria" }, { ref: "f0", value: "duplicate" }],
    ]) {
      read(); expect(fill(fields).ok).toBe(false); expect(input("user").value).toBe("");
    }
  });

  it("uses native setters and sends input/change events for text and select controls", () => {
    read();
    const ownSetter = vi.fn();
    Object.defineProperty(input("user"), "value", {
      get() { return Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.get!.call(this); },
      set: ownSetter,
    });
    const events: string[] = [];
    dom.window.document.addEventListener("input", event => events.push(`input:${(event.target as HTMLInputElement).name}`));
    dom.window.document.addEventListener("change", event => events.push(`change:${(event.target as HTMLInputElement).name}`));
    expect(fill([{ ref: "f0", value: "aria" }, { ref: "f3", value: "b" }]).ok).toBe(true);
    expect(ownSetter).not.toHaveBeenCalled();
    expect(events).toEqual(["input:user", "change:user", "input:choice", "change:choice"]);
    expect(input("user").value).toBe("aria");
  });

  it("stops after a synchronous controlled-page replacement and preserves partial-write count", () => {
    read();
    input("user").addEventListener("input", () => input("email").replaceWith(input("email").cloneNode(true)));
    expect(fill([{ ref: "f0", value: "aria" }, { ref: "f1", value: "do-not-write" }])).toMatchObject({
      ok: false, reason: "form_snapshot_changed", filled: 1,
    });
    expect(input("email").value).toBe("");
  });

  it("allows unchanged form submission once, and rejects later edits", () => {
    read(); expect(fill().ok).toBe(true);
    const submit = vi.fn();
    dom.window.HTMLFormElement.prototype.requestSubmit = submit;
    expect(run({ operation: "submit", snapshotId: id, formRef: "form0" }).submitted).toBe(true);
    expect(run({ operation: "submit", snapshotId: id, formRef: "form0" }).ok).toBe(false);
    expect(submit).toHaveBeenCalledTimes(1);
    read(); input("password").value = "user-edited-after-preview";
    expect(run({ operation: "submit", snapshotId: id, formRef: "form0" }).reason).toBe("form_snapshot_changed");
    expect(submit).toHaveBeenCalledTimes(1);
  });
});
