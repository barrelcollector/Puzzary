const SHELL_CACHE = "puzzary-shell-v2-__PUZZARY_VERSION__";
const SHELL_PREFIX = "puzzary-shell-";
const USER_CACHE_PREFIX = "puzzary-user-";
const OFFLINE_FALLBACK = "/static/offline.html?v=1";
const SHELL_ASSETS = [
  OFFLINE_FALLBACK,
  "/static/styles.css?v=54",
  "/static/pwa.js?v=1",
  "/static/brand/puzzary-mark.svg?v=1",
  "/static/brand/puzzary-192.png?v=1",
  "/static/brand/puzzary-512.png?v=1",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith(SHELL_PREFIX) && name !== SHELL_CACHE)
      .map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

async function clearUserData() {
  const names = await caches.keys();
  await Promise.all(names.filter((name) => name.startsWith(USER_CACHE_PREFIX)).map((name) => caches.delete(name)));
}

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") self.skipWaiting();
  if (event.data?.type === "CLEAR_USER_DATA") event.waitUntil(clearUserData());
});

function cacheableStatic(url) {
  return url.pathname.startsWith("/static/") || /^\/plugins\/[^/]+\/static\//.test(url.pathname);
}

async function staticResponse(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request).then((response) => {
    if (response.ok && response.type === "basic" && !/private|no-store/i.test(response.headers.get("Cache-Control") || "")) {
      cache.put(request, response.clone());
    }
    return response;
  });
  if (new URL(request.url).pathname.startsWith("/plugins/")) {
    try { return await network; } catch (_error) { return cached || Response.error(); }
  }
  return cached || network;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_FALLBACK)));
    return;
  }
  if (cacheableStatic(url)) event.respondWith(staticResponse(request));
  // API responses and authenticated HTML are deliberately always network-only.
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = { title: "New Puzzary puzzle", body: "A new puzzle is available.", url: "/library" };
  }
  const title = payload.title || "New Puzzary puzzle";
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.body || "A new puzzle is available.",
    tag: payload.tag || "puzzary-new-puzzle",
    data: { url: payload.url || "/library", notificationId: payload.notification_id || "" },
    icon: "/static/brand/puzzary-192.png?v=1",
    badge: "/static/brand/puzzary-48.png?v=1",
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/library", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const windowClient of windows) {
      if ("navigate" in windowClient) await windowClient.navigate(target);
      return windowClient.focus();
    }
    return clients.openWindow(target);
  })());
});
