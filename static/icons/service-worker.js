const CACHE = "k-crm-v1";
const ASSETS = [
  "/",                     // shell
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.9/main.min.css",
  "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.9/index.global.min.js"
];

// install: önbelleğe al
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)));
  self.skipWaiting();
});

// activate: eski cache temizle
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.map(k => (k !== CACHE ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});

// fetch: cache-first (ağ yoksa offline kabuk çalışır)
self.addEventListener("fetch", (e) => {
  const req = e.request;
  // yalnız GET önbellekle
  if (req.method !== "GET") return;
  e.respondWith(
    caches.match(req).then((res) => res || fetch(req).catch(() => caches.match("/")))
  );
});
