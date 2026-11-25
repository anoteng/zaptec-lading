// Service worker for appen: henter alltid siden fra nettet, og viser siste kopi når serveren ikke svarer.
// Data (api/…) mellomlagres aldri.
const CACHE = "lading-v1";

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["./", "manifest.webmanifest", "ikon-192.png"]))
    .catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && r.type === "basic") {
      const kopi = r.clone();
      caches.open(CACHE).then(c => c.put("./", kopi));
    }
    return r;
  }).catch(() => caches.match("./")));
});
