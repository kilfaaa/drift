/* Offline shell: network first, cache as fallback. Weather APIs are never cached here. */
const CACHE = "medvedovka-v1";
const SHELL = ["./", "index.html", "manifest.webmanifest", "photos/spot.jpg", "icons/icon-192.png", "crew/vault.json", "data/calibration.json"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => null)))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(url.pathname === "/" ? req : new Request(url.origin + url.pathname), copy)); }
      return res;
    }).catch(() => caches.match(new Request(url.origin + url.pathname)).then(r => r || caches.match("index.html")))
  );
});
