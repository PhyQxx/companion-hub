export const BROWSER_CAPABILITIES = [
  "browser.current_tab.read",
  "browser.current_tab.capture",
] as const;
export const MAX_VISIBLE_TEXT_CHARS = 100_000;

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
