// Web Push 订阅管理：权限申请、订阅登记与退订。
// 权限申请必须发生在用户手势内（按钮点击），iOS Safari 才会弹出系统询问。

import type { ChatApi } from "@aria/shared";

export type PushToggleResult =
  | "enabled"
  | "denied"
  | "unsupported"
  | "disabled"
  | "failed";

function urlBase64ToUint8Array(base64: string): ArrayBuffer {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const buffer = new ArrayBuffer(raw.length);
  const view = new Uint8Array(buffer);
  for (let index = 0; index < raw.length; index += 1) {
    view[index] = raw.charCodeAt(index);
  }
  return buffer;
}

function sameApplicationServerKey(subscription: PushSubscription, publicKey: string): boolean {
  const existing = subscription.options.applicationServerKey;
  if (!existing) return false;
  const expected = new Uint8Array(urlBase64ToUint8Array(publicKey));
  const actual = new Uint8Array(existing);
  return actual.length === expected.length && actual.every((byte, index) => byte === expected[index]);
}

async function bindSubscription(
  api: ChatApi,
  token: string,
  subscription: PushSubscription,
): Promise<void> {
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error("push_subscription_incomplete");
  }
  await api.pushSubscribe(token, {
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
  });
}

async function subscriptionForKey(
  registration: ServiceWorkerRegistration,
  publicKey: string,
): Promise<PushSubscription> {
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameApplicationServerKey(subscription, publicKey)) {
    await subscription.unsubscribe();
    subscription = null;
  }
  return subscription ?? registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey),
  });
}

async function serviceWorkerRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  const existing = await navigator.serviceWorker.getRegistration();
  if (existing) return existing;
  try {
    return await navigator.serviceWorker.register("/chat/sw.js", { scope: "/chat/" });
  } catch {
    return null;
  }
}

/** 用户点击开关时调用：申请权限并完成订阅登记。 */
export async function enablePushNotifications(
  api: ChatApi,
  token: string,
): Promise<PushToggleResult> {
  if (
    typeof Notification === "undefined" ||
    !("serviceWorker" in navigator) ||
    !("PushManager" in window)
  ) {
    return "unsupported";
  }
  let key: { enabled: boolean; public_key: string | null };
  try {
    key = await api.pushVapidKey(token);
  } catch {
    return "failed";
  }
  if (!key.enabled || !key.public_key) return "disabled";
  if (Notification.permission === "denied") return "denied";
  const permission =
    Notification.permission === "granted"
      ? "granted"
      : await Notification.requestPermission();
  if (permission !== "granted") return "denied";
  const registration = await serviceWorkerRegistration();
  if (!registration) return "unsupported";
  try {
    const subscription = await subscriptionForKey(registration, key.public_key);
    await bindSubscription(api, token, subscription);
    return "enabled";
  } catch {
    return "failed";
  }
}

/** 关闭通知：通知服务端退订并撤销浏览器本地订阅。失败不阻塞 UI。 */
export async function disablePushNotifications(
  api: ChatApi,
  token: string,
): Promise<void> {
  try {
    const registration = await navigator.serviceWorker?.getRegistration();
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription) {
      await api.pushUnsubscribe(token, subscription.endpoint).catch(() => undefined);
      await subscription.unsubscribe().catch(() => undefined);
    }
  } catch {
    // 退订失败不影响关闭开关；服务端订阅在推送失败 404/410 时也会自动清理。
  }
}

/** 当前订阅状态：用于恢复 UI 开关（不触发权限询问）。 */
export async function pushSubscriptionState(api: ChatApi, token: string): Promise<boolean> {
  let subscription: PushSubscription | null = null;
  try {
    const registration = await navigator.serviceWorker?.getRegistration();
    subscription = await registration?.pushManager.getSubscription() ?? null;
    if (!subscription) return false;
    const key = await api.pushVapidKey(token);
    if (!key.enabled || !key.public_key || Notification.permission !== "granted") return false;
    subscription = await subscriptionForKey(registration!, key.public_key);
    // Rebind on every authenticated restore. This repairs server DB loss and prevents
    // one browser endpoint from remaining attached to a previously signed-in user.
    await bindSubscription(api, token, subscription);
    return true;
  } catch {
    // A stale endpoint must not keep receiving the previous account's notifications.
    await subscription?.unsubscribe().catch(() => undefined);
    return false;
  }
}
