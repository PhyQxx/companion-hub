/** Serialized into Chrome's ISOLATED world: keep all runtime helpers inside this function. */
export function formPageOperation(request: {
  operation: "read" | "fill" | "submit";
  snapshotId: string;
  fields?: Array<{ ref: string; value: string }>;
  formRef?: string;
}): Record<string, unknown> {
  type Control = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
  type Snapshot = {
    id: string; url: string; expires: number; nodes: Element[]; parents: Array<Node | null>;
    signatures: string[]; values: string[]; controls: Control[]; forms: HTMLFormElement[];
  };
  const scope = window as unknown as { __ariaFormSnapshot?: Snapshot };
  const isControl = (el: Element): el is Control =>
    el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
  const eligible = (el: Element): el is Control => {
    if (!isControl(el)) return false;
    if (el instanceof HTMLInputElement && ["hidden", "submit", "button", "image", "reset", "file"].includes(el.type)) return false;
    for (let parent: Element | null = el; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.display === "none" || style.visibility === "hidden" || parent.hasAttribute("hidden")) return false;
    }
    return true;
  };
  const nodesNow = () => Array.from(document.querySelectorAll("form,input,textarea,select,button"));
  const signature = (el: Element) => JSON.stringify({
    tag: el.tagName,
    attrs: ["id", "name", "type", "form", "action", "method", "target", "enctype", "formaction",
      "formmethod", "formtarget", "formenctype", "multiple", "required", "disabled", "readonly",
      "placeholder", "autocomplete"].map(name => el.getAttribute(name)),
    visible: eligible(el), disabled: el.matches(":disabled"),
    labels: isControl(el) ? Array.from(el.labels ?? []).map(label => label.textContent) : [],
    options: el instanceof HTMLSelectElement
      ? Array.from(el.options).map(option => [option.value, option.text, option.disabled]) : [],
    action: el instanceof HTMLFormElement ? el.action : null,
  });
  // Values stay in the isolated world; password values never cross executeScript's result boundary.
  const valueState = (el: Element) => JSON.stringify(isControl(el) ? [
    el.value, el instanceof HTMLInputElement ? el.checked : null,
    el instanceof HTMLSelectElement ? Array.from(el.options).map(option => option.selected) : null,
  ] : null);
  const fail = (reason: string, filled = 0): Record<string, unknown> => {
    delete scope.__ariaFormSnapshot;
    return { ok: false, reason, filled, total: request.fields?.length ?? 0 };
  };
  if (request.operation === "read") {
    const nodes = nodesNow();
    // Bound retained DOM references; oversized forms fail closed.
    if (nodes.length > 1000) return fail("form_too_large");
    const controls = nodes.filter(eligible);
    const forms = nodes.filter((el): el is HTMLFormElement => el instanceof HTMLFormElement);
    scope.__ariaFormSnapshot = {
      id: request.snapshotId, url: location.href, expires: Date.now() + 5 * 60_000,
      nodes, parents: nodes.map(el => el.parentNode), signatures: nodes.map(signature),
      values: nodes.map(valueState), controls, forms,
    };
    return {
      ok: true, pageUrl: location.href,
      controls: controls.slice(0, 51).map(el => ({
        tag: el.tagName.toLowerCase(), type: el instanceof HTMLInputElement ? el.type : el.tagName.toLowerCase(),
        label: Array.from(el.labels ?? []).map(label => label.textContent ?? "").join(" ").slice(0, 120),
        placeholder: el.getAttribute("placeholder") ?? "", name: el.name,
        value: el instanceof HTMLInputElement && el.type === "password" ? "" : el.value.slice(0, 120),
        required: el.required, formIndex: el.form ? forms.indexOf(el.form) : -1,
      })),
    };
  }
  const snapshot = scope.__ariaFormSnapshot;
  if (!snapshot || snapshot.id !== request.snapshotId) return fail("form_snapshot_missing");
  if (Date.now() >= snapshot.expires) return fail("form_snapshot_expired");
  const changed = () => {
    if (location.href !== snapshot.url) return true;
    const nodes = nodesNow();
    return nodes.length !== snapshot.nodes.length || nodes.some((el, i) =>
      el !== snapshot.nodes[i] || el.parentNode !== snapshot.parents[i] ||
      signature(el) !== snapshot.signatures[i] || valueState(el) !== snapshot.values[i]);
  };
  if (document.visibilityState !== "visible" || changed()) return fail("form_snapshot_changed");
  if (request.operation === "submit") {
    if (!/^form\d+$/.test(request.formRef ?? "")) return fail("invalid_command_args");
    const form = snapshot.forms[Number(request.formRef!.slice(4))];
    if (!form) return fail("form_not_found");
    if (!/^https?:$/.test(new URL(form.action).protocol)) return fail("restricted_page");
    // Consume before invoking page code: an uncertain submit can never reuse the snapshot.
    delete scope.__ariaFormSnapshot;
    HTMLFormElement.prototype.requestSubmit.call(form);
    return { ok: true, submitted: true, page_url: snapshot.url };
  }
  const entries = request.fields ?? [];
  if (!entries.length || entries.length > 50 || new Set(entries.map(e => e.ref)).size !== entries.length) {
    return fail("invalid_command_args");
  }
  // Preflight the whole plan before writing even the first value.
  for (const entry of entries) {
    if (!/^f\d+$/.test(entry.ref) || !entry.value || entry.value.length > 5000) return fail("invalid_command_args");
    const el = snapshot.controls[Number(entry.ref.slice(1))];
    if (!el || Number(entry.ref.slice(1)) >= 50) return fail("control_not_found");
    if (el.matches(":disabled") || (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) && el.readOnly) {
      return fail("control_not_editable");
    }
    if (el instanceof HTMLSelectElement && !Array.from(el.options).some(o => o.value === entry.value && !o.disabled)) {
      return fail("option_not_found");
    }
    if (el instanceof HTMLInputElement && ["checkbox", "radio", "file"].includes(el.type)) return fail("unsupported_field");
  }
  let filled = 0;
  const skipped: Array<{ ref: string; reason: string }> = [];
  for (const entry of entries) {
    if (changed()) return fail("form_snapshot_changed", filled);
    const el = snapshot.controls[Number(entry.ref.slice(1))]!;
    if (el instanceof HTMLInputElement && el.type === "password") {
      skipped.push({ ref: entry.ref, reason: "password_field" });
      continue;
    }
    const prototype = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    try {
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(el, entry.value);
      snapshot.values[snapshot.nodes.indexOf(el)] = valueState(el);
      filled += 1;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } catch { return fail("set_value_failed", filled); }
  }
  if (changed()) return fail("form_snapshot_changed", filled);
  return { ok: true, filled, total: entries.length, skipped, pageUrl: location.href };
}
