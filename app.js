import { scoreRoutes, dist } from './score.js';
import { awarenessText, SOURCE } from './ncrb.js';

const OSRM = 'https://routing.openstreetmap.de/routed-foot/route/v1/foot';
const OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter'];
const $ = (id) => document.getElementById(id);

const map = L.map('map').setView([28.6139, 77.209], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
const layer = L.layerGroup().addTo(map);

const pad = (n) => String(n).padStart(2, '0');
const now = new Date();
$('when').value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;

const status = (msg) => { $('status').textContent = msg; };
const color = (s) => (s >= 65 ? '#1a9850' : s >= 40 ? '#f39c12' : '#d73027');

const here = () => new Promise((res, rej) => navigator.geolocation
  ? navigator.geolocation.getCurrentPosition((p) => res([p.coords.latitude, p.coords.longitude]), () => rej(new Error('Location permission denied')), { enableHighAccuracy: true, timeout: 10000 })
  : rej(new Error('No geolocation on this device')));

async function geocode(q) {
  const r = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`);
  const [hit] = await r.json();
  if (!hit) throw new Error(`Couldn't find “${q}”`);
  return [+hit.lat, +hit.lon];
}

async function osrm(points) {
  const coords = points.map(([la, lo]) => `${lo},${la}`).join(';');
  const r = await fetch(`${OSRM}/${coords}?alternatives=true&overview=full&geometries=geojson`);
  const j = await r.json();
  return j.code === 'Ok' ? j.routes.map((x) => ({ line: x.geometry.coordinates.map(([lo, la]) => [la, lo]), distance: x.distance, duration: x.duration })) : [];
}

// OSRM rarely returns walking alternatives, so also route via points pushed sideways off the midpoint.
async function candidates(a, b) {
  const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const dLat = b[0] - a[0], dLon = b[1] - a[1];
  const vias = [0.15, -0.15, 0.3, -0.3].map((k) => [mid[0] - dLon * k, mid[1] + dLat * k]);
  const all = (await Promise.all([osrm([a, b]), ...vias.map((v) => osrm([a, v, b]))])).flat();
  const uniq = [];
  for (const r of all) {
    const m = r.line[Math.floor(r.line.length / 2)];
    if (!uniq.some((u) => Math.abs(u.distance - r.distance) < 30 && dist(u.line[Math.floor(u.line.length / 2)], m) < 60)) uniq.push(r);
  }
  return uniq;
}

async function overpass(routes) {
  const pts = routes.flatMap((r) => r.line);
  const pad = 0.004; // ~400 m, covers the police search radius
  const bbox = [Math.min(...pts.map((p) => p[0])) - pad, Math.min(...pts.map((p) => p[1])) - pad, Math.max(...pts.map((p) => p[0])) + pad, Math.max(...pts.map((p) => p[1])) + pad].join(',');
  const q = `[out:json][timeout:25];(
    node["highway"="street_lamp"](${bbox}); way["highway"]["lit"](${bbox}); way["tunnel"]["highway"](${bbox});
    nw["shop"](${bbox}); nw["amenity"~"^(restaurant|cafe|fast_food|pharmacy|hospital|clinic|police|fuel|atm|bank|bar|pub|nightclub|bus_station)$"](${bbox});
    node["man_made"="surveillance"](${bbox}); node["highway"="bus_stop"](${bbox}); node["railway"="station"](${bbox});
    way["landuse"~"^(construction|brownfield|greenfield|cemetery|railway|industrial)$"](${bbox}); way["leisure"="park"](${bbox});
    way["residential"="informal"](${bbox}); way["informal"="yes"](${bbox}); );out geom;`;
  let lastErr;
  for (const url of OVERPASS) {
    try {
      const r = await fetch(url, { method: 'POST', body: new URLSearchParams({ data: q }) });
      if (r.ok) return (await r.json()).elements;
      lastErr = new Error(`Map data server busy (${r.status})`);
    } catch (e) { lastErr = e; }
  }
  throw lastErr;
}

let results = [];
function show(i) {
  layer.clearLayers();
  results.forEach((r, j) => j !== i && L.polyline(r.line, { color: '#888', weight: 4, opacity: 0.5 }).addTo(layer).on('click', () => show(j)));
  const r = results[i];
  for (let k = 1; k < r.samples.length; k++) {
    L.polyline([r.samples[k - 1][0], r.samples[k][0]], { color: color(r.samples[k][1]), weight: 7 }).addTo(layer);
  }
  r.havens.forEach((h) => L.circleMarker(h.pt, { radius: 5, color: h.tags.amenity === 'police' ? '#1f4e9c' : '#1a9850', fillOpacity: 0.9 }).bindPopup(h.name || 'Open place').addTo(layer));
  map.fitBounds(L.polyline(r.line).getBounds(), { padding: [20, 20] });
  document.querySelectorAll('.card').forEach((c, j) => c.classList.toggle('sel', j === i));
}

function render() {
  $('results').innerHTML = '';
  results.forEach((r, i) => {
    const el = document.createElement('div');
    el.className = 'card';
    const mins = Math.round(r.duration / 60), km = (r.distance / 1000).toFixed(1);
    el.innerHTML = `<h3><span>${r.label || 'Option'} · ${mins} min · ${km} km</span><span class="score" style="color:${color(r.score)}">${r.score}</span></h3>
      <div class="muted">weakest stretch ${r.weakest}${r.lowData ? ' · ⚠️ low data, score is a guess' : ''}</div><div class="chips"></div>
      <div class="muted ncrb" title="${SOURCE}">ℹ️ ${awarenessText(r.line)}</div>`;
    const chips = el.querySelector('.chips');
    for (const t of r.reasons) chips.append(Object.assign(document.createElement('span'), { textContent: `✓ ${t}` }));
    for (const t of r.warnings) chips.append(Object.assign(document.createElement('span'), { textContent: `! ${t}`, className: 'w' }));
    el.onclick = () => show(i);
    $('results').append(el);
  });
  show(0);
  $('tripActions').hidden = false;
}

$('f').onsubmit = async (e) => {
  e.preventDefault();
  try {
    status('Finding places…');
    const [a, b] = await Promise.all([$('from').value.trim() ? geocode($('from').value) : here(), geocode($('to').value)]);
    if (dist(a, b) > 10000) throw new Error('Walking routes over 10 km are not supported.');
    status('Finding routes…');
    const routes = await candidates(a, b);
    if (!routes.length) throw new Error('No walking route found.');
    status(`Checking lights, shops, police and more along ${routes.length} routes…`);
    const elements = await overpass(routes);
    results = scoreRoutes(routes, elements, new Date($('when').value), { avoidInformal: $('avoidInformal').checked });
    status('');
    render();
  } catch (err) {
    status(`⚠️ ${err.message}`);
  }
};

async function shareLocation(prefix) {
  let text = prefix;
  try { const [la, lo] = await here(); text += ` My location: https://maps.google.com/?q=${la},${lo}`; } catch { text += ' (location unavailable)'; }
  if (navigator.share) { try { await navigator.share({ text }); return; } catch { /* cancelled: fall through to SMS */ } }
  location.href = `sms:?&body=${encodeURIComponent(text)}`;
}

$('share').onclick = () => shareLocation(`I'm walking to ${$('to').value} now, about ${Math.round((results.find((r) => r.label === 'Safest') || results[0]).duration / 60)} min.`);
$('sos').onclick = async () => {
  await shareLocation('🚨 SOS — I need help.');
  if (confirm('Call 112 now?')) location.href = 'tel:112';
};
