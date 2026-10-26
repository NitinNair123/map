"use strict";

// ---------- small helpers ----------
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pad = (n) => String(n).padStart(2, "0");
const store = {
  get(k, d) { try { const v = localStorage.getItem("sr." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("sr." + k, JSON.stringify(v)); } catch {} },
};
const icon = (id) => `<svg aria-hidden="true"><use href="#i-${id}"/></svg>`;
const scoreClass = (s) => (s >= 65 ? "good" : s >= 40 ? "ok" : "bad");
const scoreWord = (s) => (s >= 65 ? "Safer" : s >= 40 ? "Take care" : "Avoid if you can");
const COLOR = { good: "#12b76a", ok: "#f79009", bad: "#f04438", none: "#98a2b3" };
const CONF = { high: "High confidence", medium: "Medium confidence", low: "Low confidence", none: "Not rated" };
const MIN_CONF = 0.35; // below this a street has too little data to rate
const LABEL = { safest: "Safest", balanced: "Balanced", fastest: "Fastest" };
const isMain = (r) => r.kind in LABEL;  // the other scanned routes ("#4"...) are drawn only when picked from the table
const MODE = { walk: { verb: "walking", noun: "walk", start: "Start walking", icon: "walk" },
  bike: { verb: "riding", noun: "ride", start: "Start riding", icon: "bike" }, car: { verb: "driving", noun: "drive", start: "Start driving", icon: "car" } };
// kinds of places along a route: map pin letter, colour and filter chip name
const GROUP = { shop: ["S", "#d99a00", "Shops"], food: ["R", "#f97316", "Food"], pharmacy: ["+", "#e11d74", "Pharmacies"],
  hospital: ["H", "#7c3aed", "Hospitals"], fuel: ["P", "#16a34a", "Petrol"], transit: ["B", "#2563eb", "Bus and metro"], cctv: ["C", "#0891b2", "Cameras"] };
const groupOf = (g) => (GROUP[g] ? g : "shop");
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const fmtDist = (m) => (m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(1)} km`);

function isoLocal(d) {
  const off = -d.getTimezoneOffset(), a = Math.abs(off);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00${off >= 0 ? "+" : "-"}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
}
function metres(a, b) {
  const x = ((b[1] - a[1]) * Math.PI / 180) * Math.cos(((a[0] + b[0]) / 2) * Math.PI / 180);
  const y = ((b[0] - a[0]) * Math.PI / 180);
  return 6371000 * Math.hypot(x, y);
}
function segDist(p, a, b) {
  const k = Math.cos(a[0] * Math.PI / 180) * 111320;
  const px = (p[1] - a[1]) * k, py = (p[0] - a[0]) * 110540, bx = (b[1] - a[1]) * k, by = (b[0] - a[0]) * 110540;
  const t = Math.max(0, Math.min(1, (px * bx + py * by) / (bx * bx + by * by || 1)));
  return Math.hypot(px - t * bx, py - t * by);
}
async function api(path, body, timeoutMs = 90000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(path, { signal: ctrl.signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  } catch (e) {
    throw new Error(e.name === "AbortError" ? "The map service is slow right now. Please try again in a moment." : "Can't reach SafeRoute. Check your connection.");
  } finally { clearTimeout(timer); }
  const data = await res.json().catch(() => ({ error: "Unexpected response from the server" }));
  if (!res.ok || data.error) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
let toastTimer;
function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}
const vibrate = (p) => navigator.vibrate && navigator.vibrate(p);

// ---------- theme ----------
function applyTheme() {
  const t = store.get("theme", "system");
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = t;
  const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.toggleAttribute("data-dark", dark);
}
applyTheme();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { applyTheme(); setBaseMap(); });

// ---------- state ----------
const state = {
  from: null, fromLabel: "", // null start means "my location"
  to: null, toLabel: "",
  when: null, // null means now
  here: null,
  routes: [], sel: "safest", rain: false, area: [],
  mode: store.get("mode", "walk"), groups: new Set(store.get("groups", Object.keys(GROUP))),
  trip: null,
  installPrompt: null,
};

// ---------- map ----------
const map = L.map("map", { zoomControl: false, attributionControl: true, preferCanvas: true, worldCopyJump: true });
// Vector map from OpenFreeMap (free, no key) drawn by MapLibre; the plain OpenStreetMap tiles if WebGL or the style can't load
let baseLayer = null;
function rasterBase() {
  if (baseLayer) baseLayer.remove();
  document.body.classList.remove("vector-map");
  baseLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
}
function setBaseMap() {
  const webgl = (() => { try { return !!document.createElement("canvas").getContext("webgl"); } catch { return false; } })();
  if (!L.maplibreGL || !window.maplibregl || !webgl) return rasterBase();
  if (baseLayer) baseLayer.remove();
  // always the light map, like Google Maps: the dark style was too dark to read streets on a phone
  baseLayer = L.maplibreGL({ style: "https://tiles.openfreemap.org/styles/liberty",
    attribution: '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> © <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>' }).addTo(map);
  document.body.classList.add("vector-map");
  const layer = baseLayer;
  map.whenReady(() => {  // the MapLibre map exists once Leaflet has a view
    const gl = layer.getMaplibreMap?.();
    gl?.on("error", () => { if (baseLayer === layer && !gl.isStyleLoaded()) rasterBase(); });
    setTimeout(() => { if (gl && baseLayer === layer && !gl.isStyleLoaded()) rasterBase(); }, 8000);  // a hanging style server sends no error
  });
}
setBaseMap();
const routeLayer = L.layerGroup().addTo(map);
const havenLayer = L.layerGroup().addTo(map);
let heatLayer = null, meMarker = null, fromMarker = null, toMarker = null;
const divIcon = (cls, size = 22) => L.divIcon({ className: "", html: `<div class="${cls}"></div>`, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });

// open where the user last looked, else on Delhi NCR (the area with street data) until we know where they are;
// a zoomed-out saved view (the old whole-country default) is ignored
const saved = store.get("view", null), lastView = saved && saved.z >= 9 ? saved : null;
if (lastView) map.setView(lastView.c, lastView.z); else map.setView([28.57, 77.32], 11);
map.on("moveend", () => {
  const c = map.getCenter().wrap();  // a map dragged round the globe reports longitudes past ±180
  store.set("view", { c: [c.lat, c.lng], z: map.getZoom() });
  if (state.heatOn) loadHeatSoon();
});

function setMarker(which, pt) {
  const old = which === "from" ? fromMarker : toMarker;
  if (old) old.remove();
  const m = pt ? L.marker(pt, { icon: divIcon(`pin ${which === "from" ? "start" : "end"}`), keyboard: false }).addTo(map) : null;
  if (which === "from") fromMarker = m; else toMarker = m;
}

// ---------- location ----------
let watchId = null;
function startWatching() {
  if (watchId !== null || !navigator.geolocation) return;
  watchId = navigator.geolocation.watchPosition(onPosition, (err) => {
    if (err.code === err.PERMISSION_DENIED) toast("Location is off. You can still pick places on the map.");
    navigator.geolocation.clearWatch(watchId); watchId = null;
  }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
}
function currentPosition(timeout = 8000) {
  if (state.here && Date.now() - state.hereAt < 30000) return Promise.resolve(state.here);
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(state.here);
    navigator.geolocation.getCurrentPosition((p) => { onPosition(p); resolve(state.here); }, () => resolve(state.here), { enableHighAccuracy: true, timeout });
  });
}
function onPosition(pos, simulated) {
  if (state.sim && !simulated) return;
  const pt = [pos.coords.latitude, pos.coords.longitude];
  state.here = pt; state.hereAt = Date.now();
  if (!meMarker) {
    meMarker = L.marker(pt, { icon: divIcon("me-dot", 18), keyboard: false, zIndexOffset: 1000 }).addTo(map);
    if (!state.to && !lastView) map.setView(pt, 16);
  }
  else meMarker.setLatLng(pt);
  if (state.trip) onTripPosition(pt);
}
$("#locateBtn").onclick = async () => {
  startWatching();
  const p = await currentPosition();
  if (p) map.setView(p, 17);
  else toast(window.isSecureContext ? "Couldn't find you. Check location permission." : "Your browser only shares location over https. Type where you are starting from instead.");
};

// ---------- search ----------
let searchSeq = 0, activeField = null;
function bindSearch(input, which) {
  input.addEventListener("focus", () => { activeField = which; showResults(input.value); });
  input.addEventListener("input", () => showResults(input.value));
  input.addEventListener("keydown", (e) => {
    const items = [...$("#results").children];
    const cur = items.findIndex((li) => li.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = items[(cur + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
      items.forEach((li) => li.setAttribute("aria-selected", li === next));
    } else if (e.key === "Enter" && items.length) items[Math.max(cur, 0)].click();
    else if (e.key === "Escape") hideResults();
  });
}
bindSearch($("#fromInput"), "from");
bindSearch($("#toInput"), "to");
document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".search-card")) hideResults(); });
function hideResults() { $("#results").hidden = true; }

async function showResults(q) {
  const seq = ++searchSeq;
  const extra = [];
  if (activeField === "from") extra.push({ name: "Your location", kind: "Use GPS", here: true });
  extra.push({ name: "Choose on map", kind: "Tap anywhere on the map", pick: true });
  let found = [];
  if (q.trim().length >= 2) {
    await new Promise((r) => setTimeout(r, 250)); // wait for typing to pause before searching
    if (seq !== searchSeq) return;
    const c = map.getCenter().wrap();
    found = await api(`/api/search?q=${encodeURIComponent(q)}&near=${c.lat},${c.lng}`).catch(() => []);
  }
  if (seq !== searchSeq) return;
  const list = $("#results");
  list.innerHTML = "";
  [...found, ...extra].forEach((r, i) => {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === 0 && found.length ? "true" : "false");
    li.innerHTML = `<span class="ico">${icon(r.here ? "locate" : r.pick ? "flag" : r.kind === "street" ? "walk" : "store")}</span><span><b>${esc(r.name)}</b><small>${esc(cap(String(r.kind).replace(/_/g, " ")))}</small></span>`;
    li.onclick = () => choose(r);
    list.appendChild(li);
  });
  list.hidden = false;
}
async function choose(r) {
  hideResults();
  const which = activeField;
  if (r.pick) { pickMode = which; toast(`Tap the map to set your ${which === "from" ? "start" : "destination"}`); return; }
  if (r.here) {
    startWatching();
    state.from = null; state.fromLabel = ""; $("#fromInput").value = ""; setMarker("from", null);
  } else setPlace(which, r.pt, r.name);
  document.activeElement.blur();
  maybeRoute();
}
function setPlace(which, pt, label) {
  state[which] = pt; state[which + "Label"] = label;
  $(which === "from" ? "#fromInput" : "#toInput").value = label;
  setMarker(which, pt);
}
$("#swapBtn").onclick = () => {
  const f = [state.from, state.fromLabel], t = [state.to, state.toLabel];
  if (!t[0]) return;
  setPlace("from", t[0], t[1]);
  if (f[0]) setPlace("to", f[0], f[1]);
  else if (state.here) setPlace("to", state.here, "Your location");
  maybeRoute();
};

let pickMode = null, reportType = null;
map.on("click", (e) => {
  const { lat, lng } = e.latlng.wrap();
  const pt = [lat, lng];
  if (reportType) return sendReport(pt);
  if (state.trip) return;
  if (state.heatOn && !pickMode) return explainStreet(pt);
  const which = pickMode || "to";
  pickMode = null;
  setPlace(which, pt, "Dropped pin");
  maybeRoute();
});

// ---------- time ----------
function timeLabel() {
  if (!state.when) return "Leave now";
  const d = state.when, today = new Date();
  const day = d.toDateString() === today.toDateString() ? "Today" : d.toLocaleDateString([], { weekday: "short" });
  return `${day} ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}
$("#timeChip").onclick = () => {
  const d = state.when || new Date();
  openDrawer(`
    <h2 id="drawerTitle">When are you leaving?</h2>
    <p>Scores change through the day: shops close, streets empty and lighting matters more after sunset.</p>
    <input type="datetime-local" id="leaveAt" class="input" value="${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}">
    <button class="btn btn-primary btn-block" id="leaveSet">Set time</button>
    <button class="btn btn-block" id="leaveNow">Leave now</button>`);
  $("#leaveSet").onclick = () => { const v = $("#leaveAt").value; state.when = v ? new Date(v) : null; afterTime(); };
  $("#leaveNow").onclick = () => { state.when = null; afterTime(); };
};
function afterTime() {
  closeDrawer();
  $("#timeLabel").textContent = timeLabel();
  if (state.heatOn) loadHeat();
  maybeRoute();
}
const whenParam = () => isoLocal(state.when || new Date());

// ---------- routing ----------
async function maybeRoute() {
  if (!state.to) return renderIdle();
  let from = state.from;
  if (!from) {
    startWatching();
    from = await currentPosition();
    if (!from) { renderSheet(`<div class="empty"><div class="badge">${icon("locate")}</div><div><h3>Where are you starting?</h3><p class="muted">Turn on location, or type a start point above.</p></div></div>`); $("#fromInput").focus(); return; }
  }
  if (metres(from, state.to) < 30) {
    routeLayer.clearLayers(); havenLayer.clearLayers();
    return renderSheet(`<div class="empty"><div class="badge">${icon("check")}</div><div><h3>You're already there</h3><p class="muted">Pick a destination further away.</p></div></div>`);
  }
  const seq = ++routeSeq;
  renderSheet(`<div class="empty"><div class="badge">${icon("shield")}</div><div><h3>Checking every street…</h3><p class="muted" id="routeWait">Lighting, open shops, police, CCTV and reports for ${esc(timeLabel().toLowerCase())}.</p></div></div>`);
  const slow = setTimeout(() => { const w = $("#routeWait"); if (w) w.textContent = "First trip in this area: downloading and scoring its streets. This can take up to a minute."; }, 6000);
  try {
    const q = new URLSearchParams({ from: from.join(","), to: state.to.join(","), at: whenParam(), mode: state.mode, extra: store.get("extra", "") });
    const res = await api("/api/route?" + q);
    if (seq !== routeSeq) return; // the user picked something else meanwhile
    state.routes = res.routes; state.rain = res.rain; state.area = res.area || []; state.routeFrom = from;
    state.sel = "safest";
    renderRoutes();
    drawRoutes(true);  // after the sheet is drawn, so the route is fitted into the map left above it
  } catch (err) {
    if (seq !== routeSeq) return;
    renderSheet(`<div class="note">${icon("alert")}<span>${esc(err.message)}</span></div><button class="btn btn-block" id="retryRoute">Try again</button>`);
    $("#retryRoute").onclick = maybeRoute;
  } finally { clearTimeout(slow); }
}
let routeSeq = 0;

function drawRoutes(fit) {
  routeLayer.clearLayers(); havenLayer.clearLayers();
  const order = state.routes.filter((r) => !r.same_as && (isMain(r) || r.kind === state.sel)).sort((a, b) => (a.kind === state.sel) - (b.kind === state.sel));
  for (const r of order) {
    const sel = r.kind === state.sel || (state.routes.find((x) => x.kind === state.sel)?.same_as === r.kind);
    const c = r.confidence_level === "none" ? COLOR.none : COLOR[scoreClass(r.score)];
    if (sel) L.polyline(r.coords, { color: "#fff", weight: 11, opacity: 0.9 }).addTo(routeLayer);
    const line = L.polyline(r.coords, { color: sel ? c : "#8f8aa0", weight: sel ? 7 : 5, opacity: sel ? 1 : 0.6 }).addTo(routeLayer);
    line.on("click", (e) => { L.DomEvent.stop(e); if (!state.trip) { state.sel = r.kind; drawRoutes(); renderRoutes(); } });
    if (sel) {
      if (r.weakest && r.weakest.score < 45)
        L.polyline(r.weakest.coords, { color: "#16131f", weight: 7, opacity: 0.8, dashArray: "2 10", lineCap: "round" }).addTo(routeLayer)
          .bindTooltip(`Weakest stretch: ${esc(r.weakest.name)}`);
      r.segments.forEach(([a, b, c, d]) => L.polyline([[a, b], [c, d]], { color: COLOR.none, weight: 7, dashArray: "6 8", interactive: false }).addTo(routeLayer));
      drawPlaces(r);
    }
  }
  if (fit) {
    const all = state.routes.filter(isMain).flatMap((r) => r.coords);
    const desk = innerWidth >= 900; // keep the route clear of the search card and the sheet
    const top = $(".topbar").getBoundingClientRect().bottom + 16, sheet = $("#sheet").getBoundingClientRect().height + 16;
    if (all.length) map.fitBounds(all, { paddingTopLeft: desk ? [460, 40] : [24, top], paddingBottomRight: desk ? [40, 40] : [90, sheet] });
  }
}

function drawPlaces(r) {
  for (const pl of r.places || []) {
    const g = groupOf(pl.group);
    if (!state.groups.has(g)) continue;
    const [letter, color, name] = GROUP[g];
    const status = g === "cctv" ? "" : pl.open === true ? "open when you pass" : pl.open === false ? "closed when you pass" : "hours not published";
    L.marker(pl.pt, { icon: L.divIcon({ className: "", html: `<div class="gpin ${g === "cctv" ? "" : pl.open === true ? "open" : pl.open === false ? "closed" : "unknown"}" style="background:${color}">${letter}</div>`, iconSize: [22, 22], iconAnchor: [11, 11] }), keyboard: false })
      .addTo(havenLayer).bindTooltip(`${esc(pl.name)}${status ? `: ${status}` : ""} · ${pl.minutes} min into the ${MODE[r.mode || "walk"].noun}${pl.source && pl.source !== "OpenStreetMap" ? ` · ${esc(pl.source)}` : ""}`);
  }
}

function groupChips(r) {
  const n = {};
  (r.places || []).forEach((pl) => { const g = groupOf(pl.group); n[g] = (n[g] || 0) + 1; });
  return `<div class="groups" role="group" aria-label="Show on the map">${Object.entries(GROUP).filter(([g]) => n[g]).map(([g, [, color, name]]) =>
    `<button data-group="${g}" aria-pressed="${state.groups.has(g)}"><i style="background:${color}"></i>${name} ${n[g]}</button>`).join("")}</div>`;
}

function stripRow(x, r) {
  const ends = `<span class="end" style="left:0;background:var(--brand)"></span><span class="end" style="left:100%;background:var(--risk)"></span>`;
  const gaps = (x.strip?.gaps || []).map(([a, b]) => `<span class="gap" style="left:${a * 100}%;width:${(b - a) * 100}%"></span>`).join("");
  const dark = (x.strip?.dark || []).map(([a, b]) => `<span class="dark" style="left:${a * 100}%;width:${(b - a) * 100}%"></span>`).join("");
  const dots = (x.places || []).filter((p) => p.group !== "cctv" && p.group !== "transit").map((p) =>
    `<span class="p" style="left:${p.frac * 100}%;background:${p.open === true ? "var(--safe)" : p.open === false ? "#475467" : "#c0c5d0"}"></span>`).join("");
  const n = (x.strip?.gaps || []).length;
  return `<div class="strip-row ${x.kind === r.kind || r.same_as === x.kind ? "this" : ""}" data-kind="${x.kind}">
    <small><span><b>${LABEL[x.kind] || `Route ${x.kind}`}</b> · ${x.minutes} min</span><span>${n ? `${n} long empty ${n === 1 ? "stretch" : "stretches"}` : "No long empty stretches"}</span></small>
    <div class="strip" aria-hidden="true">${gaps}${dark}${dots}${ends}</div></div>`;
}

function lookTable(r) {
  const g = r.look?.groups || {}, z = { found: 0, open: 0, no_hours: 0 };
  const row = (label, k) => { const v = g[k] || z; return `<tr><td>${label}</td><td>${v.found ? `${v.found} found · ${v.open} open${v.no_hours ? `, ${v.no_hours} unknown` : ""}` : "None found"}</td></tr>`; };
  const shops = ["shop", "food", "other"].reduce((a, k) => ({ found: a.found + (g[k]?.found || 0), open: a.open + (g[k]?.open || 0) }), z);
  const help = ["pharmacy", "hospital", "fuel"].reduce((a, k) => a + (g[k]?.open || 0), 0);
  const noHours = Object.values(g).reduce((a, v) => a + (v.no_hours || 0), 0);
  const arrive = r.look?.arrive ? r.look.arrive.slice(11, 16) : "";
  return `<details class="why"><summary>A closer look at this route</summary>
    <table class="look"><tbody>
    ${row("Medical stores and clinics", "pharmacy")}${row("Hospitals", "hospital")}${row("Petrol pumps", "fuel")}
    <tr><td>Shops and food</td><td>${shops.found} found · ${shops.open} open</td></tr>
    <tr><td>Bus and metro stops</td><td>${g.transit?.found || 0}</td></tr>
    <tr><td>CCTV cameras mapped</td><td>${g.cctv?.found || 0}</td></tr>
    <tr><td>Help points open as you pass</td><td>${help}</td></tr>
    <tr><td>Longest stretch without an open help point or police</td><td>${fmtDist(r.look?.help_gap_m || 0)}</td></tr>
    <tr><td>Longest stretch with nothing open</td><td>${fmtDist(r.look?.nothing_open_m || 0)}</td></tr>
    ${arrive ? `<tr><td>You arrive about</td><td>${arrive}</td></tr>` : ""}
    </tbody></table>
    <p class="muted" style="margin:0 0 12px">Opening hours are checked for the minute you pass each place, not when you leave. ${noHours ? `${noHours} places publish no hours, so they count as unknown, never as open. ` : ""}Help points are medical stores, hospitals and petrol pumps that are open, and police stations within 150 m.</p></details>`;
}

function gauge(score) {
  const c = 2 * Math.PI * 36, cls = scoreClass(score);
  return `<div class="gauge" role="img" aria-label="Safety score ${score} out of 100">
    <svg viewBox="0 0 84 84"><circle class="track" cx="42" cy="42" r="36"/><circle class="val" cx="42" cy="42" r="36" stroke="${COLOR[cls]}" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - score / 100)}"/></svg>
    <div><span>${score}<small>SCORE</small></span></div></div>`;
}

function renderRoutes() {
  const r = state.routes.find((x) => x.kind === state.sel);
  const fastest = state.routes.find((x) => x.kind === "fastest");
  const tabs = state.routes.filter(isMain).map((x) => {
    const extra = x.minutes - fastest.minutes;
    return `<button class="tab" role="tab" aria-selected="${x.kind === state.sel}" data-kind="${x.kind}">
      <b>${LABEL[x.kind]}</b><span class="num ${x.confidence_level === "none" ? "" : scoreClass(x.score)}">${x.confidence_level === "none" ? "–" : x.score}</span>
      <small>${x.same_as ? `Same as ${LABEL[x.same_as].toLowerCase()}` : `${x.minutes} min${extra > 0 ? ` · +${extra}` : ""}`}</small></button>`;
  }).join("");
  const factors = Object.entries(r.factors).map(([k, v]) =>
    v === null ? `<div class="bar"><span>${esc(k)}</span><span class="track"></span><span class="muted">No data</span></div>`
      : `<div class="bar"><span>${esc(k)}<small class="muted"> · ${v.measured}% measured</small></span><span class="track"><span class="fill" style="display:block;width:${v.score}%;background:${COLOR[scoreClass(v.score)]}"></span></span><span>${v.score}</span></div>`).join("");
  const notes = [];
  if (r.confidence_level === "none") notes.push(["", "There isn't enough real data on these streets to rate this route yet. Grey dashes mark the unrated parts. Reports and the street survey fill this in."]);
  else if (r.unrated_pct >= 10) notes.push(["", `${r.unrated_pct}% of this route is too thinly mapped to rate. It's shown in grey dashes, and those streets earn no points, which pulls the score down.`]);
  if (state.rain) notes.push(["", "It's raining, so streets may be emptier than usual."]);
  if (r.weakest && r.weakest.score < 45) notes.push(["", `Weakest stretch: ${r.weakest.name} (score ${r.weakest.score}), mainly because of ${r.weakest.why.toLowerCase()}. It's dashed on the map.`]);
  const unknown = (r.places || []).filter((pl) => pl.open === null).length;
  if (unknown) notes.push(["info", `Faded pins on the map are ${unknown} places with no published hours, so we can't say if they're open. Bright pins are open when you pass. Tap "Report" and "Shops open now" to fix that for everyone.`]);
  if (r.headline) notes.unshift(["info", r.headline]);
  if (r.checked > 1) notes.push(["info", `We scored ${r.checked} different ways there${r.max_extra_min ? `, up to ${r.max_extra_min} min longer than the fastest` : ""}. This one ranks #${r.rank} for safety.`]);
  else if (r.checked === 1) notes.push(["info", "This is the only sensible way there on the map, so all three options are the same."]);
  if (r.same_as) notes.push(["info", `This is the same way as the ${LABEL[r.same_as].toLowerCase()} route.`]);
  const dusk = r.daylight > 0 && r.daylight < 1;
  renderSheet(`
    <div class="tabs" role="tablist" aria-label="Route options">${tabs}</div>
    <div class="detail">
      ${r.confidence_level === "none" ? "" : gauge(r.score)}
      <div>
        <h3>${r.confidence_level === "none" ? "Not rated" : scoreWord(r.score)}</h3>
        <p class="conf conf-${r.confidence_level}">${CONF[r.confidence_level]} · ${Math.round(r.confidence * 100)}% measured</p>
        <div class="stats">
          <div><b>${r.minutes} min</b>${MODE[r.mode || "walk"].noun}</div>
          <div><b>${fmtDist(r.length_m)}</b>distance</div>
          <div><b>${r.turns}</b>${r.turns === 1 ? "turn" : "turns"}</div>
        </div>
      </div>
    </div>
    <ul class="tags">
      ${r.reasons.map((t) => `<li>${esc(t)}</li>`).join("")}
      ${r.warnings.map((t) => `<li class="warn">${esc(t)}</li>`).join("")}
      <li class="info">${r.daylight >= 1 ? "Daylight" : dusk ? "Dusk" : "After dark"}</li>
    </ul>
    <div class="actions">
      <button class="btn btn-primary" id="startBtn">${icon(MODE[r.mode || "walk"].icon)}${MODE[r.mode || "walk"].start}</button>
      <button class="btn" id="shareRouteBtn" aria-label="Share this trip">${icon("share")}</button>
    </div>
    ${groupChips(r)}
    <div class="strips">${state.routes.filter((x) => !x.same_as && isMain(x)).map((x) => stripRow(x, r)).join("")}
      <div class="strip-key"><span><i style="background:var(--risk)"></i>300 m or more with nothing open</span><span><i style="background:#6941c6"></i>unlit</span><span><i style="background:var(--safe);width:8px;height:8px;border-radius:50%"></i>open</span><span><i style="background:#c0c5d0;width:8px;height:8px;border-radius:50%"></i>hours unknown</span></div></div>
    ${notes.map(([kind, n]) => `<div class="note ${kind}">${icon(kind === "info" ? "shield" : "alert")}<span>${esc(n)}</span></div>`).join("")}
    ${lookTable(r)}
    ${r.compared && r.compared.length > 1 ? `<details class="why"><summary>${r.rank === 1 ? "Why this is the safest" : `Why this ranks #${r.rank}`} · ${r.checked} ways scanned</summary>
      <ul class="why-list">${(r.why || []).map((t) => `<li class="${t.startsWith("But:") ? "warn" : ""}">${esc(t)}</li>`).join("")}</ul>
      <div class="compare"><table><thead><tr><th>#</th><th>Score</th><th>Time</th><th>Lit</th><th>Open</th><th>Unrated</th></tr></thead><tbody>
      ${r.compared.map((c) => `<tr class="${c.rank === r.rank ? "this" : ""}" data-kind="${state.routes.find((x) => x.rank === c.rank && !x.same_as)?.kind || ""}"><td>${c.rank}</td><td><b class="${c.confidence_level === "none" ? "" : scoreClass(c.score)}">${c.confidence_level === "none" ? "–" : c.score}</b></td><td>${c.minutes} min</td><td>${c.stats.lit_pct || c.stats.dark_pct ? `${c.stats.lit_pct}%` : "–"}</td><td>${c.stats.open_now ?? 0}</td><td>${c.unrated_pct}%</td></tr>`).join("")}
      </tbody></table><p class="muted" style="margin:6px 0 0">Tap a row to see that route on the map.</p></div></details>` : ""}
    ${(state.area || []).map((a) => `<details class="why"><summary>Past crime in ${esc(a.district)} · ${a.year}</summary>
      <p style="margin:0 0 8px">Police recorded <b>${a.assault}</b> cases of assault on women (IPC 354, BNS 74-78) and <b>${a.insult}</b> of insulting a woman's modesty (IPC 509, BNS 79) across the whole district in ${a.year}${a.previous ? `, against ${a.previous.assault} and ${a.previous.insult} in ${a.previous.year}` : ""}.</p>
      ${a.year >= 2024 ? `<p class="muted" style="margin:0 0 8px">NCRB warns that ${a.year} isn't directly comparable with earlier years: the new criminal law (BNS) replaced the IPC on 1 July 2024.</p>` : ""}
      <p class="muted" style="margin:0 0 12px">Background only, it doesn't change the score: it covers the whole district, only counts cases reported to police, and says nothing about a single street tonight. Source: ${esc(a.source)}.</p></details>`).join("")}
    <details class="why"><summary>Directions · ${r.steps.length - 1} ${r.steps.length === 2 ? "step" : "steps"}</summary><ol class="steps-list">${r.steps.map((st) =>
      `<li><span class="turn">${turnIcon(st.turn)}</span>${esc(st.text)}<small>${st.dist ? fmtDist(st.dist) : ""}</small></li>`).join("")}</ol></details>
    <details class="why"><summary>Why this score?</summary><div class="bars">${factors}</div>
      <p class="muted" style="margin:0 0 8px">Data used: ${r.sources.map(esc).join(", ") || "none"}.</p>
      <p class="muted" style="margin:0 0 12px">Scores compare routes. They mean <b>safer</b>, never guaranteed safe. Trust your instincts.</p></details>`, true);
  document.querySelectorAll(".tab, .strip-row, .compare tr[data-kind]").forEach((t) => (t.onclick = () => { if (state.trip || !t.dataset.kind) return; state.sel = t.dataset.kind; drawRoutes(); renderRoutes(); }));
  document.querySelectorAll("[data-group]").forEach((b) => (b.onclick = () => {
    const g = b.dataset.group;
    if (state.groups.has(g)) state.groups.delete(g); else state.groups.add(g);
    store.set("groups", [...state.groups]);
    b.setAttribute("aria-pressed", state.groups.has(g));
    drawRoutes();
  }));
  $("#startBtn").onclick = startTrip;
  $("#shareRouteBtn").onclick = () => startTrip({ shareOnly: true });
}

// ---------- sheet ----------
function renderSheet(html, keepSize) { $("#sheetBody").innerHTML = html; if (!keepSize) setSheet("half"); }
// The sheet has three heights: peek (just the top), half (default) and full. Drag or tap the handle to change it.
function setSheet(size) {
  const s = $("#sheet");
  s.classList.toggle("peek", size === "peek"); s.classList.toggle("full", size === "full");
  if (size !== "full") s.scrollTop = 0;
}
{
  const sizes = ["peek", "half", "full"];
  const now = () => $("#sheet").classList.contains("full") ? 2 : $("#sheet").classList.contains("peek") ? 0 : 1;
  let startY = null;
  $("#grabber").addEventListener("pointerdown", (e) => { startY = e.clientY; e.target.setPointerCapture?.(e.pointerId); });
  $("#grabber").addEventListener("pointerup", (e) => {
    if (startY === null) return;
    const dy = e.clientY - startY, i = now();
    startY = null;
    setSheet(sizes[Math.abs(dy) < 10 ? (i === 2 ? 1 : i + 1) : Math.max(0, Math.min(2, i + (dy < 0 ? 1 : -1)))]);
  });
}
// keep the SOS button floating just above the sheet whatever its height
new ResizeObserver(([e]) => document.documentElement.style.setProperty("--sheet-h", e.target.offsetHeight + "px")).observe($("#sheet"));
async function renderIdle() {
  routeLayer.clearLayers(); havenLayer.clearLayers();
  const hour = new Date().getHours();
  const greet = hour < 5 ? "Out late?" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const contacts = store.get("contacts", []);
  const install = state.installPrompt
    ? `<button class="btn btn-block" id="installBtn">${icon("check")}Install SafeRoute on this phone</button>`
    : isIOS() && !standalone() ? `<p class="muted">Install: tap <b>Share</b> then <b>Add to Home Screen</b>.</p>` : "";
  renderSheet(`
    <div class="sheet-head"><div><h3>${greet}</h3><p class="muted" style="margin:0">Search a place or tap the map to see the safest way there.</p></div></div>
    ${contacts.length ? "" : `<button class="btn btn-block" id="addContactsBtn">${icon("user")}Add a trusted contact for SOS</button>`}
    <div id="nearby"></div>
    ${install}`);
  $("#addContactsBtn")?.addEventListener("click", openSettings);
  $("#installBtn")?.addEventListener("click", async () => { state.installPrompt.prompt(); state.installPrompt = null; renderIdle(); });
  if (state.here) {
    const list = await api(`/api/havens?near=${state.here.join(",")}&at=${encodeURIComponent(whenParam())}`).catch(() => []);
    if (list.length && $("#nearby")) {
      $("#nearby").innerHTML = `<p class="muted" style="margin:12px 0 4px">Open near you right now</p>` + list.slice(0, 3).map((h) =>
        `<div class="haven"><span class="ico">${icon(h.kind === "police" ? "shield" : "store")}</span><div><b>${esc(h.name)}</b><div class="muted">${fmtDist(h.distance_m)} away</div></div></div>`).join("");
    }
  }
}

// ---------- live trip ----------
const tripLink = (id) => `${location.origin}/t/?id=${id}`;
async function createTrip(dest, eta) {
  const t = await api("/api/trip", { dest, eta, name: store.get("myName", "") });
  if (state.here) api(`/api/trip/${t.id}`, { key: t.key, lat: state.here[0], lon: state.here[1] }).catch(() => {});
  return t;
}
async function shareText(text, url) {
  if (navigator.share) {
    try { await navigator.share({ title: "SafeRoute", text, url }); return true; } catch (e) { if (e.name === "AbortError") return false; }
  }
  const contacts = store.get("contacts", []);
  if (contacts.length) { location.href = smsLink(contacts, `${text} ${url}`); return true; }
  try { await navigator.clipboard.writeText(`${text} ${url}`); toast("Link copied. Paste it to a friend."); return true; } catch { return false; }
}
const smsLink = (contacts, body) => `sms:${contacts.map((c) => c.phone.replace(/[^\d+]/g, "")).join(",")}?&body=${encodeURIComponent(body)}`;

async function startTrip(opts = {}) {
  const r = state.routes.find((x) => x.kind === state.sel);
  let t;
  try { t = await createTrip(state.to, r.minutes); } catch (e) { return toast(e.message); }
  const shared = shareText(`I'm ${MODE[r.mode || "walk"].verb} to ${state.toLabel || "my destination"} with SafeRoute, about ${r.minutes} min. Follow me live:`, tripLink(t.id));
  if (opts.shareOnly) { await shared; return; }
  startWatching();
  state.trip = { ...t, route: r, started: Date.now(), offCount: 0, lastMovePt: state.here, lastMoveAt: Date.now(), lastPost: 0, nextCheck: 0,
    spoken: {}, lastReroute: 0 };
  state.follow = true;
  document.body.classList.add("navigating");
  $("#navBanner").hidden = false;
  drawTripRoute();
  try { state.wakeLock = await navigator.wakeLock?.request("screen"); } catch {}
  const start = state.here && metres(state.here, r.coords[0]) < 60 ? state.here : r.coords[0];
  map.setView(start, 18);
  renderTrip();
  updateGuidance(start);
  if (!state.here || metres(state.here, r.coords[0]) > 200) toast("No GPS fix near the start. Use Simulate walk to preview the trip.", 5000);
}

function drawTripRoute() {
  const r = state.trip.route;
  routeLayer.clearLayers(); havenLayer.clearLayers();
  L.polyline(r.coords, { color: "#fff", weight: 12, opacity: 0.9 }).addTo(routeLayer);
  state.tripLine = L.polyline(r.coords, { color: COLOR[scoreClass(r.score)], weight: 8 }).addTo(routeLayer);
  drawPlaces(r);
}

// ---------- turn-by-turn ----------
const TURN_ANGLE = { depart: 0, straight: 0, "slight right": 45, right: 90, "slight left": -45, left: -90, "u-turn": 180 };
const turnIcon = (turn) => turn === "arrive" ? icon("flag") : `<svg aria-hidden="true" style="transform:rotate(${TURN_ANGLE[turn] || 0}deg)"><use href="#i-arrow"/></svg>`;
const speakDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} kilometres` : `${Math.max(10, Math.round(m / 10) * 10)} metres`);
function speak(text) {
  if (store.get("muted", false) || !window.speechSynthesis) return;
  speechSynthesis.cancel();
  speechSynthesis.speak(Object.assign(new SpeechSynthesisUtterance(text), { rate: 1.02 }));
}
$("#muteBtn").onclick = () => {
  const m = !store.get("muted", false);
  store.set("muted", m);
  if (m) speechSynthesis?.cancel();
  $("#muteBtn").setAttribute("aria-pressed", m);
  $("#muteBtn").innerHTML = icon(m ? "mute" : "volume");
};
$("#muteBtn").innerHTML = icon(store.get("muted", false) ? "mute" : "volume");

function updateGuidance(pt) {
  const t = state.trip, r = t.route;
  const p = routeProgress(pt, r.coords);
  const done = r.length_m - p.left;
  const i = r.steps.findIndex((st) => st.dist > done + 5);
  const step = r.steps[i] || r.steps[r.steps.length - 1];
  const togo = Math.max(0, step.dist - done);
  const then = r.steps[i + 1];
  $("#navArrow").innerHTML = turnIcon(step.turn);
  $("#navArrow").classList.toggle("arrive", step.turn === "arrive");
  $("#navDist").textContent = fmtDist(togo);
  $("#navText").textContent = step.text;
  $("#navThen").textContent = then && then.dist - step.dist < 80 ? `Then ${then.text.charAt(0).toLowerCase()}${then.text.slice(1)}` : "";
  // say the first instruction once, warn at ~150 m, and again just before the turn
  if (!t.spoken.start) { t.spoken.start = 1; speak(`${r.steps[0].text}. Then in ${speakDist(togo)}, ${step.text.toLowerCase()}.`); }
  else if (togo < 30 && !t.spoken[i + "now"]) { t.spoken[i + "now"] = 1; speak(step.text); }
  else if (togo < 160 && togo > 60 && !t.spoken[i + "soon"]) { t.spoken[i + "soon"] = 1; speak(`In ${speakDist(togo)}, ${step.text.toLowerCase()}`); }
  return p;
}

map.on("dragstart", () => { if (state.trip) { state.follow = false; $("#recenterBtn").hidden = false; } });
$("#recenterBtn").onclick = () => { state.follow = true; $("#recenterBtn").hidden = true; if (state.here) map.setView(state.here, 18); };

async function reroute(pt) {
  const t = state.trip;
  if (Date.now() - t.lastReroute < 20000) return;
  t.lastReroute = Date.now();
  speak("Off route. Finding the safest way from here.");
  toast("Off route. Finding the safest way from here…");
  try {
    const q = new URLSearchParams({ from: pt.join(","), to: state.to.join(","), at: whenParam(), mode: state.mode, extra: store.get("extra", "") });
    const res = await api("/api/route?" + q, null, 60000);
    if (state.trip !== t) return;
    t.route = res.routes.find((x) => x.kind === state.sel) || res.routes[0];
    t.spoken = { start: 1 };
    drawTripRoute();
    renderTrip();
    updateGuidance(pt);
  } catch (e) { toast(e.message); }
}

// Demo: walk the route at 4x walking speed so the trip can be shown without GPS
function simulateWalk() {
  const t = state.trip;
  if (!t) return;
  if (state.sim) { clearInterval(state.sim); state.sim = null; return renderTrip(); }
  let along = Math.max(0, t.route.length_m - routeProgress(state.here || t.route.coords[0], t.route.coords).left);
  state.sim = setInterval(() => {
    if (!state.trip) { clearInterval(state.sim); state.sim = null; return; }
    along += 1.4 * 4;
    const c = state.trip.route.coords;
    let left = along, pt = c[c.length - 1];
    for (let k = 0; k < c.length - 1; k++) {
      const d = metres(c[k], c[k + 1]);
      if (left <= d) { const f = left / d; pt = [c[k][0] + (c[k + 1][0] - c[k][0]) * f, c[k][1] + (c[k + 1][1] - c[k][1]) * f]; break; }
      left -= d;
    }
    onPosition({ coords: { latitude: pt[0], longitude: pt[1] } }, true);
  }, 1000);
  renderTrip();
}

function routeProgress(pt, coords) {
  let best = Infinity, idx = 0;
  for (let i = 0; i < coords.length - 1; i++) {
    const d = segDist(pt, coords[i], coords[i + 1]);
    if (d < best) { best = d; idx = i; }
  }
  let left = metres(pt, coords[idx + 1] || coords[idx]);
  for (let i = idx + 1; i < coords.length - 1; i++) left += metres(coords[i], coords[i + 1]);
  return { off: best, idx, left };
}

function renderTrip() {
  const t = state.trip, r = t.route;
  const p = state.here ? routeProgress(state.here, r.coords) : { left: r.length_m, idx: 0 };
  const done = Math.max(0, Math.min(100, 100 * (1 - p.left / Math.max(r.length_m, 1))));
  const ahead = state.here ? r.havens.map((h) => ({ ...h, d: metres(state.here, h.pt) })).sort((a, b) => a.d - b.d)[0] : r.havens[0];
  renderSheet(`
    <div class="nav-head"><div style="flex:1"><h3>${Math.max(1, Math.round(p.left / 1.3 / 60))} min · ${fmtDist(p.left)} left</h3>
      <p class="muted" style="margin:0">to ${esc(state.toLabel || "destination")}</p></div><span class="live">Sharing live</span></div>
    <div class="progress"><div style="width:${done}%"></div></div>
    ${ahead ? `<div class="haven"><span class="ico">${icon("store")}</span><div><b>${esc(ahead.name)}</b><div class="muted">Open now${ahead.d ? ` · ${fmtDist(ahead.d)}` : ""}. Step in if anything feels off.</div></div></div>` : ""}
    <div class="actions"><button class="btn btn-primary" id="safeBtn">${icon("check")}I've arrived safely</button>
      <button class="btn" id="tripShare" aria-label="Share live link again">${icon("share")}</button></div>
    <div class="actions" style="grid-template-columns:1fr 1fr"><button class="btn btn-ghost" id="simBtn">${icon("walk")}${state.sim ? "Stop simulation" : "Simulate walk"}</button>
      <button class="btn btn-ghost" id="endTrip">End trip</button></div>`);
  $("#simBtn").onclick = simulateWalk;
  $("#safeBtn").onclick = () => endTrip(true);
  $("#endTrip").onclick = () => endTrip(false);
  $("#tripShare").onclick = () => shareText("Follow my walk live on SafeRoute:", tripLink(t.id));
}

function onTripPosition(pt) {
  const t = state.trip, r = t.route;
  const p = updateGuidance(pt);
  if (state.follow) map.setView(pt, Math.max(map.getZoom(), 17), { animate: true });
  if (metres(pt, t.lastMovePt || pt) > 15) { t.lastMovePt = pt; t.lastMoveAt = Date.now(); }
  if (Date.now() - t.lastPost > 10000) {
    t.lastPost = Date.now();
    api(`/api/trip/${t.id}`, { key: t.key, lat: pt[0], lon: pt[1] }).catch(() => {});
  }
  if (metres(pt, r.coords[r.coords.length - 1]) < 40) return endTrip(true, true);
  t.offCount = p.off > 40 ? t.offCount + 1 : 0;
  if (t.offCount >= 3) { t.offCount = 0; reroute(pt); checkIn("You seem to have left your route."); }
  renderTrip();
}

setInterval(() => {
  const t = state.trip;
  if (!t || !$("#checkin").hidden || !$("#sosOverlay").hidden || Date.now() < t.nextCheck) return;
  const buffer = store.get("buffer", 10) * 60000;
  if (Date.now() - t.lastMoveAt > 4 * 60000) checkIn("You haven't moved for a few minutes.");
  else if (Date.now() - t.started > t.route.minutes * 60000 * 1.5 + buffer) checkIn("You're taking longer than expected.");
}, 5000);

async function endTrip(safe, auto) {
  const t = state.trip;
  if (!t) return;
  state.trip = null;
  state.routes = [];
  clearInterval(state.sim); state.sim = null;
  document.body.classList.remove("navigating");
  $("#navBanner").hidden = true; $("#recenterBtn").hidden = true;
  if (safe) speak("You have arrived.");
  api(`/api/trip/${t.id}`, { key: t.key, ended: true }).catch(() => {});
  state.wakeLock?.release?.().catch(() => {});
  vibrate(200);
  const contacts = store.get("contacts", []);
  renderSheet(`<div class="empty"><div class="badge" style="background:var(--safe-soft);color:var(--safe)">${icon("check")}</div>
    <div><h3>${safe ? (auto ? "You've arrived" : "Glad you're safe") : "Trip ended"}</h3><p class="muted">Live sharing has stopped.</p></div></div>
    ${safe && contacts.length ? `<a class="btn btn-primary btn-block" style="margin-top:12px" href="${esc(smsLink(contacts, "I've reached safely 🙂 (sent from SafeRoute)"))}">Tell my contacts I'm safe</a>` : ""}
    <button class="btn btn-block" style="margin-top:8px" id="newTrip">Plan another trip</button>`);
  $("#newTrip").onclick = () => { state.to = null; $("#toInput").value = ""; setMarker("to", null); renderIdle(); };
}

// ---------- check-in ----------
let checkTimer = null;
function checkIn(why) {
  if (state.trip) state.trip.nextCheck = Date.now() + 3 * 60000;
  $("#checkWhy").textContent = why;
  $("#checkin").hidden = false;
  vibrate([300, 150, 300, 150, 300]);
  beep();
  let left = 60;
  $("#checkSecs").textContent = left;
  clearInterval(checkTimer);
  checkTimer = setInterval(() => {
    left -= 1; $("#checkSecs").textContent = left;
    if (left % 10 === 0) { vibrate(400); beep(); }
    if (left <= 0) { closeCheckIn(); openSOS(); }
  }, 1000);
  $("#checkOk").focus();
}
function closeCheckIn() { clearInterval(checkTimer); $("#checkin").hidden = true; }
$("#checkOk").onclick = () => { closeCheckIn(); if (state.trip) { state.trip.lastMoveAt = Date.now(); state.trip.started = Date.now() - state.trip.route.minutes * 30000; } toast("Okay, we'll keep watching your trip."); };
$("#checkHelp").onclick = () => { closeCheckIn(); openSOS(true); };

// ---------- SOS ----------
const sosBtn = $("#sosBtn");
let holdTimer = null;
sosBtn.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  sosBtn.classList.add("holding");
  vibrate(30);
  holdTimer = setTimeout(() => { sosBtn.classList.remove("holding"); openSOS(); }, 1500);
});
["pointerup", "pointerleave", "pointercancel"].forEach((ev) => sosBtn.addEventListener(ev, () => {
  if (holdTimer && sosBtn.classList.contains("holding")) toast("Press and hold SOS to call for help");
  clearTimeout(holdTimer); holdTimer = null; sosBtn.classList.remove("holding");
}));
sosBtn.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSOS(); } });
sosBtn.addEventListener("contextmenu", (e) => e.preventDefault());

let sosTimer = null;
async function openSOS(now = false) {
  $("#sosOverlay").hidden = false;
  vibrate([500, 200, 500]);
  let left = now ? 0 : 5;
  const tick = () => {
    $("#sosCount").textContent = left > 0 ? left : "SOS";
    $("#sosCount").classList.toggle("done", left <= 0);
    $("#sosSecs").textContent = left;
    if (left <= 0) { clearInterval(sosTimer); alertContacts(); }
    left -= 1;
  };
  clearInterval(sosTimer);
  tick();
  if (!now) sosTimer = setInterval(tick, 1000);
  $("#sosCancel").focus();
  startWatching();
  const pt = await currentPosition(5000);
  if (pt) {
    const hv = await api(`/api/havens?near=${pt.join(",")}&at=${encodeURIComponent(isoLocal(new Date()))}`).catch(() => []);
    $("#sosHavens").innerHTML = hv.length ? `<b>Nearest open places</b><br>${hv.slice(0, 3).map((h) => `${esc(h.name)} · ${fmtDist(h.distance_m)}`).join("<br>")}` : "";
  }
}
async function alertContacts() {
  $("#sosTitle").textContent = "Help is a tap away";
  $("#sosSub").textContent = "Your contacts get your live location. Call 112 if you're in danger.";
  const contacts = store.get("contacts", []);
  const pt = await currentPosition(5000);
  let link = "";
  try {
    if (!state.trip) { const t = await createTrip(pt || state.to, 0); state.sosTrip = t; link = tripLink(t.id); }
    else link = tripLink(state.trip.id);
  } catch {}
  const maps = pt ? `https://maps.google.com/?q=${pt[0].toFixed(6)},${pt[1].toFixed(6)}` : "";
  const body = `SOS from ${store.get("myName", "me") || "me"}: I need help. ${link ? "Live location: " + link : ""} ${maps}`.trim();
  if (contacts.length) location.href = smsLink(contacts, body);
  else shareText(body, link);
  // keep the SOS trip's dot moving while the screen is open
  if (state.sosTrip && !state.trip) {
    clearInterval(state.sosPing);
    state.sosPing = setInterval(async () => {
      const p = await currentPosition(5000);
      if (p && state.sosTrip) api(`/api/trip/${state.sosTrip.id}`, { key: state.sosTrip.key, lat: p[0], lon: p[1] }).catch(() => {});
    }, 15000);
  }
}
$("#sosAlertNow").onclick = () => { clearInterval(sosTimer); $("#sosCount").textContent = "SOS"; $("#sosCount").classList.add("done"); alertContacts(); };
$("#sosCancel").onclick = () => {
  clearInterval(sosTimer); clearInterval(state.sosPing); stopSiren();
  if (state.sosTrip) { api(`/api/trip/${state.sosTrip.id}`, { key: state.sosTrip.key, ended: true }).catch(() => {}); state.sosTrip = null; }
  $("#sosOverlay").hidden = true;
  $("#sosTitle").textContent = "Alerting your contacts";
  $("#sosSub").innerHTML = 'Sending your live location in <b id="sosSecs">5</b> seconds. Tap cancel if you\'re OK.';
};

// ---------- sound: siren, beeps, ringtone ----------
let audio = null, siren = null, ring = null;
const ctx = () => (audio ||= new (window.AudioContext || window.webkitAudioContext)());
function beep() {
  try {
    const a = ctx(), o = a.createOscillator(), g = a.createGain();
    o.frequency.value = 880; g.gain.value = 0.3; o.connect(g).connect(a.destination);
    o.start(); o.stop(a.currentTime + 0.25);
  } catch {}
}
function stopSiren() { if (siren) { siren.stop(); siren = null; } $("#sirenBtn").setAttribute("aria-pressed", "false"); }
$("#sirenBtn").onclick = () => {
  if (siren) return stopSiren();
  const a = ctx(), o = a.createOscillator(), lfo = a.createOscillator(), depth = a.createGain(), g = a.createGain();
  o.type = "square"; o.frequency.value = 1000;
  lfo.frequency.value = 1.5; depth.gain.value = 400;
  lfo.connect(depth).connect(o.frequency);
  g.gain.value = 1; o.connect(g).connect(a.destination);
  o.start(); lfo.start();
  siren = { stop() { o.stop(); lfo.stop(); } };
  $("#sirenBtn").setAttribute("aria-pressed", "true");
};
function startRing() {
  try {
    const a = ctx(), g = a.createGain(), o1 = a.createOscillator(), o2 = a.createOscillator();
    o1.frequency.value = 440; o2.frequency.value = 480; g.gain.value = 0;
    o1.connect(g); o2.connect(g); g.connect(a.destination);
    o1.start(); o2.start();
    const t0 = a.currentTime;
    for (let i = 0; i < 20; i++) { g.gain.setValueAtTime(0.15, t0 + i * 6); g.gain.setValueAtTime(0, t0 + i * 6 + 2); }
    ring = { stop() { o1.stop(); o2.stop(); } };
  } catch {}
  vibrate([1000, 500, 1000, 500, 1000, 500, 1000]);
}
function stopRing() { ring?.stop(); ring = null; vibrate(0); }

// ---------- fake call ----------
let callTimer = null;
function fakeCall() {
  const name = store.get("caller", "Mom");
  $("#callerName").textContent = name;
  $("#callerInitial").textContent = name.trim()[0]?.toUpperCase() || "?";
  $("#callStatus").textContent = "mobile · incoming call";
  $("#callActions").innerHTML = `<button class="round decline" id="declineCall" aria-label="Decline">${icon("phone")}</button><button class="round accept" id="acceptCall" aria-label="Accept">${icon("phone")}</button>`;
  $("#fakeCall").hidden = false;
  startRing();
  $("#acceptCall").onclick = () => {
    stopRing();
    let s = 0;
    $("#callActions").innerHTML = `<button class="round decline" id="declineCall" aria-label="Hang up">${icon("phone")}</button>`;
    $("#declineCall").onclick = endCall;
    clearInterval(callTimer);
    callTimer = setInterval(() => { s++; $("#callStatus").textContent = `${pad(Math.floor(s / 60))}:${pad(s % 60)}`; }, 1000);
    $("#callStatus").textContent = "00:00";
  };
  $("#declineCall").onclick = endCall;
}
function endCall() { stopRing(); clearInterval(callTimer); $("#fakeCall").hidden = true; }
$("#fakeCallBtn").onclick = () => { $("#sosCancel").click(); fakeCall(); };

// ---------- reports ----------
const REPORTS = [
  ["lights_on", "💡", "Street is lit"], ["broken_light", "🌑", "Dark or broken light"],
  ["shops_open", "🏪", "Shops open now"], ["shops_closed", "🔒", "Shops closed now"],
  ["people_around", "👥", "People around"], ["deserted", "🚶", "Deserted"],
  ["felt_unsafe", "👀", "Felt unsafe"], ["harassment", "🚫", "Harassment"], ["felt_safe", "✅", "Felt safe"],
  ["no_footpath", "🚧", "No footpath"], ["dogs", "🐕", "Stray dogs"], ["flooded", "🌊", "Flooded"],
];
$("#reportBtn").onclick = () => {
  openDrawer(`
    <h2 id="drawerTitle">Report something</h2>
    <p>Anonymous, and it updates scores for everyone straight away. You count once per street for each kind of report.</p>
    <div class="report-grid">${REPORTS.map(([k, e, l]) => `<button data-type="${k}" aria-pressed="false"><span>${e}</span>${l}</button>`).join("")}</div>
    <div class="sticky-actions">
      <button class="btn btn-primary btn-block" id="reportHere" disabled>${icon("locate")}At my location</button>
      <button class="btn btn-block" id="reportMap" disabled>${icon("flag")}Pick the spot on the map</button>
    </div>`);
  let type = null;
  document.querySelectorAll(".report-grid button").forEach((b) => (b.onclick = () => {
    type = b.dataset.type;
    document.querySelectorAll(".report-grid button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    $("#reportHere").disabled = $("#reportMap").disabled = false;
  }));
  $("#reportHere").onclick = async () => {
    closeDrawer();
    reportType = type;
    const p = await currentPosition();
    if (p) sendReport(p); else { reportType = null; toast("Couldn't get your location. Pick the spot on the map instead."); }
  };
  $("#reportMap").onclick = () => { closeDrawer(); reportType = type; $("#reportBtn").classList.add("on"); toast("Tap where it happened", 6000); };
};
// a random id made once on this phone, so people sharing one network address still count as separate reporters
function deviceId() {
  let id = store.get("device", "");
  if (!id) { id = crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now(); store.set("device", id); }
  return id;
}
async function sendReport(pt) {
  const type = reportType;
  reportType = null; $("#reportBtn").classList.remove("on");
  try {
    await api("/api/report", { lat: pt[0], lon: pt[1], type, at: isoLocal(new Date()), device: deviceId() });
    toast("Thanks. Your report is helping others get home safer.");
    if (state.heatOn) loadHeat();
    if (state.routes.length && !state.trip) maybeRoute();
  } catch (e) { toast(e.message); }
}

// ---------- safety map ----------
let heatTimer = null, heatSeq = 0;
const loadHeatSoon = () => { clearTimeout(heatTimer); heatTimer = setTimeout(loadHeat, 400); };
async function loadHeat() {
  if (map.getZoom() < 15) { heatLayer?.remove(); heatLayer = null; return toast("Zoom in to see the safety map"); }
  const seq = ++heatSeq, b = map.getBounds();
  const bbox = [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => v.toFixed(5)).join(",");
  let segs;
  try { segs = await api("/api/heat?" + new URLSearchParams({ bbox, at: whenParam() })); } catch (e) { return toast(e.message); }
  if (seq !== heatSeq || !state.heatOn) return;
  heatLayer?.remove();
  heatLayer = L.layerGroup(segs.map(([a, b, c, d, s, conf]) => conf < MIN_CONF
    ? L.polyline([[a, b], [c, d]], { color: COLOR.none, weight: 3, opacity: 0.8, dashArray: "4 6", interactive: false })
    : L.polyline([[a, b], [c, d]], { color: COLOR[scoreClass(s)], weight: 4, opacity: conf >= 0.5 ? 0.85 : 0.5, interactive: false }))).addTo(map);
  routeLayer.eachLayer((l) => l.bringToFront?.());
}
async function explainStreet(pt) {
  let x;
  try { x = await api("/api/explain?" + new URLSearchParams({ at_pt: pt.join(","), at: whenParam() })); } catch (e) { return toast(e.message); }
  const rows = x.factors.map((f) => `<tr><td>${esc(f.factor)}</td><td>${f.score === null ? '<span class="muted">No data</span>' : f.score}</td><td>${esc(f.sources.join(", ") || "—")}</td></tr>`).join("");
  L.popup({ maxWidth: 320 }).setLatLng(pt).setContent(`<b>${esc(cap(x.name))}</b><br>
    ${x.confidence_level === "none" ? "Not rated" : `Score ${x.score}`} · ${CONF[x.confidence_level]} (${Math.round(x.confidence * 100)}% measured)
    <table class="explain"><tr><th>Factor</th><th>Score</th><th>Data</th></tr>${rows}</table>`).openOn(map);
}
$("#heatBtn").onclick = () => {
  state.heatOn = !state.heatOn;
  $("#heatBtn").setAttribute("aria-pressed", state.heatOn);
  $("#legend").hidden = !state.heatOn;
  if (state.heatOn) { loadHeat(); toast(`Safety map for ${timeLabel().toLowerCase()}. Tap a street to see its data.`); } else { heatLayer?.remove(); heatLayer = null; }
};

// ---------- drawer, settings, onboarding ----------
function openDrawer(html) {
  $("#drawerBody").innerHTML = html;
  $("#drawer").hidden = false;
  if (matchMedia("(pointer: fine)").matches) $("#drawer").querySelector("input, button:not(.close)")?.focus();  // on phones focus pops the keyboard
}
function closeDrawer() { $("#drawer").hidden = true; }
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer" || e.target.closest("[data-close]")) closeDrawer(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer(); });

function contactsHtml() {
  const list = store.get("contacts", []);
  return (list.length ? list.map((c, i) => `<div class="contact"><span class="avatar-s">${esc(c.name[0]?.toUpperCase() || "?")}</span><div><b>${esc(c.name)}</b><small>${esc(c.phone)}</small></div>
    <button class="icon-btn" data-del="${i}" aria-label="Remove ${esc(c.name)}" style="box-shadow:none">${icon("trash")}</button></div>`).join("")
    : `<p class="muted">No contacts yet. SOS and check-ins alert the people you add here.</p>`) +
    `<form class="form" id="contactForm"><input name="name" placeholder="Name" required maxlength="40" aria-label="Contact name">
     <input name="phone" type="tel" placeholder="Phone" required pattern="[\\d\\s\\+\\-\\(\\)]{6,}" aria-label="Contact phone">
     <button class="btn btn-primary" type="submit">${icon("user")}Add contact</button></form>`;
}
function bindContacts(rerender) {
  $("#contactForm").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const list = store.get("contacts", []);
    list.push({ name: f.get("name").trim(), phone: f.get("phone").trim() });
    store.set("contacts", list);
    rerender();
  };
  document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => {
    const list = store.get("contacts", []);
    list.splice(+b.dataset.del, 1);
    store.set("contacts", list);
    rerender();
  }));
}
function openSettings() {
  const sel = (id, val, opts) => `<select id="${id}" class="input">${opts.map(([v, l]) => `<option value="${v}" ${String(v) === String(val) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  openDrawer(`
    <h2 id="drawerTitle">Settings</h2>
    <h3 style="font-size:16px;margin-top:6px">Trusted contacts</h3>
    <div id="contactsBox" style="display:grid;gap:8px">${contactsHtml()}</div>
    <div class="row"><label for="myName">Your name in alerts</label><input id="myName" class="input" style="width:50%" maxlength="40" value="${esc(store.get("myName", ""))}"></div>
    <div class="row"><label for="buffer">Check in if I'm late by</label>${sel("buffer", store.get("buffer", 10), [[5, "5 min"], [10, "10 min"], [15, "15 min"], [30, "30 min"]])}</div>
    <div class="row"><label for="caller">Fake caller name</label><input id="caller" class="input" style="width:50%" maxlength="30" value="${esc(store.get("caller", "Mom"))}"></div>
    <div class="row"><span>Fake call</span><button class="btn" id="fakeIn10">Ring me in 10 s</button></div>
    <div class="row"><label for="extra">Consider routes up to</label>${sel("extra", store.get("extra", ""), [["", "Any reasonable detour"], [5, "5 min longer"], [10, "10 min longer"], [15, "15 min longer"], [20, "20 min longer"]])}</div>
    <div class="row"><label for="theme">Appearance</label>${sel("theme", store.get("theme", "system"), [["system", "Automatic"], ["light", "Light"], ["dark", "Dark"]])}</div>
    <div class="row"><span>Your data</span><button class="btn" id="wipe">Delete everything</button></div>
    <p class="muted">Your contacts and settings stay on this phone. Trip locations are deleted when a trip ends. We never sell location data.</p>`);
  const rerender = () => { $("#contactsBox").innerHTML = contactsHtml(); bindContacts(rerender); };
  bindContacts(rerender);
  $("#myName").onchange = (e) => store.set("myName", e.target.value.trim());
  $("#caller").onchange = (e) => store.set("caller", e.target.value.trim() || "Mom");
  $("#buffer").onchange = (e) => store.set("buffer", +e.target.value);
  $("#theme").onchange = (e) => { store.set("theme", e.target.value); applyTheme(); setBaseMap(); };
  $("#extra").onchange = (e) => { store.set("extra", e.target.value); if (state.to) maybeRoute(); };
  $("#fakeIn10").onclick = () => { closeDrawer(); toast("Fake call in 10 seconds. You can lock your screen."); setTimeout(fakeCall, 10000); };
  $("#wipe").onclick = () => { if (confirm("Delete your contacts and settings from this phone?")) { try { Object.keys(localStorage).filter((k) => k.startsWith("sr.")).forEach((k) => localStorage.removeItem(k)); } catch {} location.reload(); } };
}
$("#settingsBtn").onclick = openSettings;

function showMode() { document.querySelectorAll(".modes [data-mode]").forEach((b) => b.setAttribute("aria-checked", b.dataset.mode === state.mode)); }
document.querySelectorAll(".modes [data-mode]").forEach((b) => (b.onclick = () => {
  if (state.trip || state.mode === b.dataset.mode) return;
  state.mode = b.dataset.mode; store.set("mode", state.mode); showMode();
  if (state.to) maybeRoute();
}));
showMode();

function onboarding(step = 0) {
  const dots = `<div class="steps">${[0, 1, 2].map((i) => `<i class="${i === step ? "on" : ""}"></i>`).join("")}</div>`;
  const pages = [
    `<div class="hero-ico">${icon("shield")}</div><h2 id="drawerTitle">Get there the safer way</h2>
     <p class="stat-line"><b>52%</b> of urban Indian women say they always or often feel unsafe walking alone at night. <span class="muted">(YouGov survey)</span></p>
     <p>SafeRoute checks every street on your walk, ride or drive for lighting, open shops, police, CCTV, isolated stretches and reports from people like you, for the moment you'll be there.</p>
     <button class="btn btn-primary btn-block" data-next>Continue</button>`,
    `<div class="hero-ico">${icon("check")}</div><h2 id="drawerTitle">Private by design</h2>
     <ul class="promise"><li>${icon("check")}No account needed</li><li>${icon("check")}Contacts never leave your phone</li>
     <li>${icon("check")}Live location only while you share a trip, deleted when it ends</li><li>${icon("check")}We never sell your data</li></ul>
     <button class="btn btn-primary btn-block" data-next>Continue</button>`,
    `<div class="hero-ico">${icon("user")}</div><h2 id="drawerTitle">Who should we alert?</h2>
     <p>When you hold SOS or miss a check-in, SafeRoute texts your live location to these people.</p>
     <div id="contactsBox" style="display:grid;gap:8px">${contactsHtml()}</div>
     <button class="btn btn-primary btn-block" id="finishOnboard">${icon("locate")}Done, use my location</button>
     <button class="btn btn-ghost btn-block" id="skipOnboard">Skip for now</button>`,
  ];
  openDrawer(pages[step] + dots);
  $("#drawer .close").hidden = step < 2;
  $("[data-next]")?.addEventListener("click", () => onboarding(step + 1));
  if (step === 2) {
    const rerender = () => { $("#contactsBox").innerHTML = contactsHtml(); bindContacts(rerender); };
    bindContacts(rerender);
    const finish = (locate) => { store.set("onboarded", true); $("#drawer .close").hidden = false; closeDrawer(); if (locate) $("#locateBtn").click(); renderIdle(); };
    $("#finishOnboard").onclick = () => finish(true);
    $("#skipOnboard").onclick = () => finish(false);
  }
}

// ---------- install (PWA) ----------
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone;
addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); state.installPrompt = e; if (!state.to) renderIdle(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

// ---------- start ----------
renderIdle();
if (!store.get("onboarded", false)) onboarding();
else if (navigator.permissions) navigator.permissions.query({ name: "geolocation" }).then((p) => { if (p.state === "granted") { startWatching(); currentPosition().then(() => !state.to && renderIdle()); } }).catch(() => {});
const params = new URLSearchParams(location.search);
if (params.get("sos") === "1") openSOS();
