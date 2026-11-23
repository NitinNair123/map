// App shell cache so SafeRoute opens instantly, and the SOS screen still works with a weak signal.
// Bump this with every release, and the ?v= on the app's files to the same number (test_server checks they match):
// a CDN in front caches by full URL, so a new number makes every phone fetch the new code at once.
const CACHE = "saferoute-v25";
const SHELL = ["/", "/app/", "/app/app.js?v=25", "/app/voice.js?v=25", "/app/app.css?v=25", "/theme.css?v=25", "/manifest.webmanifest", "/icons/icon.svg", "/icons/icon-192.png",
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
