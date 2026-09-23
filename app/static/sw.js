/* Orbit service worker: the app shell installs and opens instantly; every
   API call still goes to the network.

   - Navigations (/ui/) are network-first so a deploy is picked up on the next
     open, with the cached shell as the offline fallback.
   - Same-origin static files under /ui/ (styles, scripts, icons, fonts) are
     served from cache and refreshed in the background (stale-while-revalidate).
   - Anything outside /ui/ -- /chat, /chat/stream, /auth, /me, /billing and
     every other API route -- is never intercepted, so nothing about a turn,
     a session or a payment changes when the worker is installed.
   Bump VERSION when the shell changes shape; the old cache is dropped on activate. */
const PREFIX = "orbit-shell-";
const VERSION = PREFIX + "v12";
const SHELL = ["/ui/", "/ui/index.html", "/ui/theme.css?v=1", "/ui/chat.css?v=8", "/ui/product.css?v=8", "/ui/mobile.css?v=7", "/ui/manifest.webmanifest",
               "/ui/icons/icon-192.png", "/ui/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

// Only Orbit's own older shells are dropped: another app on the same origin
// keeps its caches.
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith(PREFIX) && k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("message", (event) => {
  if (event.data === "skipWaiting") self.skipWaiting();
});

function isShellRequest(url) {
  return url.origin === self.location.origin && url.pathname.startsWith("/ui/");
}

// Each page is cached under its own path (the query string -- ?source=pwa,
// ?new=1 -- never makes a different page), so opening Admin can never
// replace the offline chat shell, and offline Admin comes back as Admin.
function pageKey(url) {
  return url.pathname === "/ui/index.html" ? "/ui/" : url.pathname;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (request.mode === "navigate") {
    if (!isShellRequest(url)) return;
    const key = pageKey(url);
    event.respondWith(
      fetch(request).then((response) => {
        if (response && response.ok) {                     // an error page never replaces a good one
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(key, copy)).catch(() => {});
        }
        return response;
      }).catch(() => caches.match(key).then((cached) => cached || caches.match("/ui/")))
    );
    return;
  }
  if (!isShellRequest(url)) return;                     // API and everything else: untouched
  event.respondWith(
    caches.open(VERSION).then(async (cache) => {
      const cached = await cache.match(request);
      const refresh = fetch(request).then((response) => {
        if (response && response.ok) cache.put(request, response.clone()).catch(() => {});
        return response;
      }).catch(() => cached);
      return cached || refresh;
    })
  );
});
