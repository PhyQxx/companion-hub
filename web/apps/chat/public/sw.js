const CACHE_NAME = "aria-chat-shell-v1";
const SHELL = ["/chat/", "/chat/offline.html", "/chat/manifest.webmanifest", "/chat/icons/aria.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          void caches.open(CACHE_NAME).then((cache) => cache.put("/chat/", copy));
          return response;
        })
        .catch(async () => (await caches.match("/chat/")) ?? caches.match("/chat/offline.html")),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          void caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      });
      return cached ?? network;
    }),
  );
});

// 主动通知（Web Push）：tag 用事件 ID 做同事件去重，正文由服务端按通知习惯截断。
self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {};
  }
  const title = payload.title || "Aria";
  const options = {
    body: typeof payload.body === "string" ? payload.body : "",
    tag: typeof payload.tag === "string" ? payload.tag : undefined,
    icon: "/chat/icons/aria.svg",
    badge: "/chat/icons/aria.svg",
    data: payload.data || {},
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

// 点击回流：优先聚焦已打开的聊天窗口，否则新开 /chat/；
// 页面进入前台后会走既有 visibilitychange 补拉逻辑取回最新消息。
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const clientList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of clientList) {
        if ("focus" in client) {
          await client.focus();
          return;
        }
      }
      await self.clients.openWindow("/chat/");
    })(),
  );
});
