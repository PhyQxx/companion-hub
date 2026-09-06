export const BROWSER_CAPABILITIES = [
  "browser.current_tab.read",
  "browser.current_tab.capture",
  "browser.tab.open",
  "browser.form.read",
  "browser.form.fill",
  "browser.form.submit",
] as const;
export const MAX_VISIBLE_TEXT_CHARS = 100_000;
// WEB-01 表单工作流边界：控件数量、单值长度与展示截断都从紧。
export const MAX_FORM_FIELDS = 50;
export const MAX_FORM_FILL_VALUE_CHARS = 5_000;
export const MAX_FORM_FIELD_TEXT_CHARS = 120;

export interface SignedFrame {
  type: string;
  signature?: string;
  [key: string]: unknown;
}

export interface ExecuteCommandFrame extends SignedFrame {
  type: "command.execute";
  command_id: string;
  command: string;
  args_json: string;
  idempotency_key: string;
  issued_at: string;
  expires_at: string;
}

export interface BrowserDocument {
  title: string;
  origin: string;
  language: string | null;
  text: string;
  truncated: boolean;
}

export interface FormFieldDescriptor {
  ref: string;
  formRef: string | null;
  tag: string;
  type: string;
  label: string;
  placeholder: string;
  name: string;
  value: string;
  required: boolean;
}

export interface FormFillField {
  ref: string;
  value: string;
}

const FIELD_REF_PATTERN = /^f\d+$/;
const FORM_REF_PATTERN = /^form\d+$/;

function boundedText(value: unknown, limit: number): string {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, limit) : "";
}

/**
 * 把页面注入脚本返回的原始控件列表规范化为带稳定 ref 的描述符。
 * 控件按文档顺序枚举，ref 即顺序号 f0..fN；密码框值一律掩码，
 * 其余值也只截取片段用于展示，绝不回传完整表单内容。
 */
export function buildFormFieldDescriptors(
  rawControls: unknown,
  pageUrl: string,
): { fields: FormFieldDescriptor[]; truncated: boolean; origin: string } {
  const list = Array.isArray(rawControls) ? rawControls : [];
  const fields: FormFieldDescriptor[] = [];
  let origin = "";
  try {
    origin = new URL(pageUrl).origin;
  } catch {
    origin = "";
  }
  for (const raw of list.slice(0, MAX_FORM_FIELDS + 1)) {
    if (fields.length >= MAX_FORM_FIELDS) break;
    if (!raw || typeof raw !== "object") continue;
    const item = raw as Record<string, unknown>;
    const isPassword = item.type === "password";
    fields.push({
      ref: `f${fields.length}`,
      formRef: typeof item.formIndex === "number" && item.formIndex >= 0 ? `form${item.formIndex}` : null,
      tag: boundedText(item.tag, 12),
      type: boundedText(item.type, 24),
      label: boundedText(item.label, MAX_FORM_FIELD_TEXT_CHARS),
      placeholder: boundedText(item.placeholder, MAX_FORM_FIELD_TEXT_CHARS),
      name: boundedText(item.name, MAX_FORM_FIELD_TEXT_CHARS),
      value: isPassword ? "" : boundedText(item.value, MAX_FORM_FIELD_TEXT_CHARS),
      required: item.required === true,
    });
  }
  return { fields, truncated: list.length > MAX_FORM_FIELDS, origin };
}

/**
 * 校验填写计划：ref 必须来自 form.read 的顺序号，单值与总量有界。
 * 密码框在页面端也会被跳过，这里在计划层先拒绝引用密码字段的能力没有
 * ——类型信息在描述符里，由页面端执行时兜底；计划层只做结构与边界校验。
 */
export function parseFormFillFields(raw: unknown): FormFillField[] | null {
  if (!Array.isArray(raw) || raw.length === 0 || raw.length > MAX_FORM_FIELDS) return null;
  const fields: FormFillField[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return null;
    const entry = item as Record<string, unknown>;
    if (typeof entry.ref !== "string" || !FIELD_REF_PATTERN.test(entry.ref)) return null;
    if (typeof entry.value !== "string") return null;
    if (entry.value.length === 0 || entry.value.length > MAX_FORM_FILL_VALUE_CHARS) return null;
    fields.push({ ref: entry.ref, value: entry.value });
  }
  return fields;
}

export function isValidFormRef(raw: unknown): raw is string {
  return typeof raw === "string" && FORM_REF_PATTERN.test(raw);
}

export interface FormFillOutcome {
  filled: number;
  total: number;
  skipped: Array<{ ref: string; reason: string }>;
  pageUrl: string;
}

/** 规范化页面端回传的填写结果，丢弃超出边界的自由文本。 */
export function sanitizeFormFillOutcome(raw: unknown, pageUrl: string): FormFillOutcome | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const filled = typeof item.filled === "number" && Number.isInteger(item.filled) ? item.filled : -1;
  const total = typeof item.total === "number" && Number.isInteger(item.total) ? item.total : -1;
  if (filled < 0 || total < 0) return null;
  const skippedRaw = Array.isArray(item.skipped) ? item.skipped : [];
  const skipped: Array<{ ref: string; reason: string }> = [];
  for (const entry of skippedRaw.slice(0, MAX_FORM_FIELDS)) {
    if (!entry || typeof entry !== "object") continue;
    const record = entry as Record<string, unknown>;
    if (typeof record.ref !== "string" || !FIELD_REF_PATTERN.test(record.ref)) continue;
    skipped.push({
      ref: record.ref,
      reason: boundedText(record.reason, 40) || "unsupported_field",
    });
  }
  return {
    filled,
    total,
    skipped,
    pageUrl: boundedText(item.pageUrl, 2_048) || pageUrl,
  };
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => compareUnicodeKeys(left, right))
        .map(([key, item]) => [key, sortValue(item)]),
    );
  }
  return value;
}

function compareUnicodeKeys(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index]! - rightPoints[index]!;
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

export function canonicalFrame(frame: SignedFrame): string {
  const { signature: _, ...unsigned } = frame;
  return JSON.stringify(sortValue(unsigned));
}

export async function signFrame(accessToken: string, frame: SignedFrame): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(accessToken),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(canonicalFrame(frame)),
  );
  return Array.from(new Uint8Array(signature), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function verifyFrame(accessToken: string, frame: SignedFrame): Promise<boolean> {
  if (typeof frame.signature !== "string") return false;
  const expected = await signFrame(accessToken, frame);
  if (expected.length !== frame.signature.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index += 1) {
    difference |= expected.charCodeAt(index) ^ frame.signature.charCodeAt(index);
  }
  return difference === 0;
}

export function websocketUrl(hubUrl: string): string {
  const url = new URL(hubUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/devices";
  url.search = "";
  url.hash = "";
  return url.toString();
}

export function normalizeHubUrl(value: string): string {
  const url = new URL(value.trim());
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Hub 地址只允许 http:// 或 https://");
  }
  url.pathname = url.pathname.replace(/\/+$/, "");
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

export function isExecuteCommand(frame: SignedFrame): frame is ExecuteCommandFrame {
  return (
    frame.type === "command.execute" &&
    typeof frame.command_id === "string" &&
    typeof frame.command === "string" &&
    typeof frame.args_json === "string" &&
    typeof frame.idempotency_key === "string" &&
    typeof frame.expires_at === "string"
  );
}

export function sanitizeBrowserDocument(input: {
  title?: unknown;
  origin?: unknown;
  language?: unknown;
  text?: unknown;
}): BrowserDocument {
  const rawText = typeof input.text === "string" ? input.text.replace(/\s+/g, " ").trim() : "";
  return {
    title: typeof input.title === "string" ? input.title.slice(0, 500) : "",
    origin: typeof input.origin === "string" ? input.origin.slice(0, 2_048) : "",
    language: typeof input.language === "string" ? input.language.slice(0, 64) : null,
    text: rawText.slice(0, MAX_VISIBLE_TEXT_CHARS),
    truncated: rawText.length > MAX_VISIBLE_TEXT_CHARS,
  };
}
