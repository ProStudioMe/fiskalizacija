/* ProRačun PWA service worker — shell + recent lists; no offline fiscalize */
const CACHE = "sepko-shell-v13";
const PRECACHE = [
  "/app",
  "/app/kasa",
  "/static/style.css?v=210",
  "/static/pos.js?v=7",
  "/manifest.webmanifest",
  "/manifest-kasa.webmanifest",
  "/static/img/sepko-mark.svg?v=15",
  "/static/img/sepko-mark.png?v=15",
  "/static/img/sepko-logo.png?v=15",
  "/static/img/og-proracun.png?v=5",
  "/static/img/proracun-lockup-clear.png?v=1",
  "/static/qr-scan.js?v=1",
  "/static/pwa.css?v=2",
  "/static/lucide.min.js?v=0.544.0",
  "/static/lucide-init.js?v=1",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Network-first for versioned static (?v=); cache-first for unversioned
  if (url.pathname.startsWith("/static/") || url.pathname === "/sw.js") {
    if (url.search || url.pathname === "/sw.js") {
      event.respondWith(
        fetch(req)
          .then((res) => {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
            return res;
          })
          .catch(() => caches.match(req))
      );
      return;
    }
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  if (
    url.pathname === "/app" ||
    url.pathname.startsWith("/app/kasa") ||
    url.pathname.startsWith("/ulazne") ||
    url.pathname === "/" ||
    url.pathname.startsWith("/racuni")
  ) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match("/app/kasa") || caches.match("/app")))
    );
  }
});
