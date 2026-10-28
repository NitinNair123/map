// App shell cache so SafeRoute opens instantly, and the SOS screen still works with a weak signal.
const CACHE = "saferoute-v14";
const SHELL = ["/", "/app/", "/app/app.js", "/app/app.css", "/theme.css", "/manifest.webmanifest", "/icons/icon.svg", "/icons/icon-192.png",
  "/vendor/leaflet/leaflet.css", "/vendor/leaflet/leaflet.js",
  "/vendor/maplibre/maplibre-gl.css", "/vendor/maplibre/maplibre-gl.js", "/vendor/maplibre/leaflet-maplibre-gl.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {})))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.hostname.endsWith("tile.openstreetmap.org")
      || url.hostname === "tiles.openfreemap.org") return;  // map tiles: the browser caches them, and there are far too many to keep
  // network first so updates show up, cache when offline
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return res;
  }).catch(() => caches.match(e.request).then((hit) => hit || caches.match("/app/"))));
});
