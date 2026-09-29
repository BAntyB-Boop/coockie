/* Service Worker: caches versioned static assets only.
   Control APIs, live state, WebSockets and auth data are never cached. */
const VERSION = "crb-static-v1.1.0";
const ASSETS = [
  "/", "/index.html", "/app.js", "/styles.css", "/manifest.json",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("crb-static-") && k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return; // network only
  if (req.headers.has("authorization")) return;
  if (!ASSETS.includes(url.pathname)) return;
  // Network first so updates apply; fall back to cache when the backend is down.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/index.html")))
  );
});
