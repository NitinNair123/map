// Pure scoring logic: no DOM, no network. Imported by app.js and score.test.js.

const DAYS = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
const toMin = (hm) => { const [h, m] = hm.split(':').map(Number); return h * 60 + m; };

// Returns rules [{days:Set, ranges:[[a,b]]}] or null if the string is beyond this parser.
// shortcut: handles "24/7", "Mo-Fr 09:00-21:00; Sa off" style only; swap for the opening_hours npm lib if real data trips it.
export function parseOpeningHours(s) {
  if (!s) return null;
  s = s.trim();
  if (s === '24/7') return [{ days: new Set(DAYS), ranges: [[0, 1440]] }];
  const rules = [];
  for (let part of s.split(';')) {
    part = part.trim();
    if (!part || /^(PH|SH)\b/.test(part)) continue;
    const m = part.match(/^((?:Mo|Tu|We|Th|Fr|Sa|Su)(?:\s*[-,]\s*(?:Mo|Tu|We|Th|Fr|Sa|Su))*)?\s*(.*)$/);
    const days = new Set();
    if (m[1]) {
      for (const tok of m[1].replace(/\s/g, '').split(',')) {
        const [a, b] = tok.split('-').map((d) => DAYS.indexOf(d));
        for (let i = a; ; i = (i + 1) % 7) { days.add(DAYS[i]); if (b === undefined || i === b) break; }
      }
    } else DAYS.forEach((d) => days.add(d));
    const times = m[2].trim();
    if (/^(off|closed)$/i.test(times)) { rules.push({ days, ranges: [] }); continue; }
    if (times === '') { rules.push({ days, ranges: [[0, 1440]] }); continue; }
    const ranges = [];
    for (const r of times.split(',')) {
      const t = r.trim().match(/^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\+?$/);
      if (!t) return null;
      ranges.push([toMin(t[1]), toMin(t[2])]);
    }
    rules.push({ days, ranges });
  }
  return rules.length ? rules : null;
}

// Later rules override earlier ones for the days they name (OSM semantics).
function rangesFor(rules, day) {
  let out = null;
  for (const r of rules) if (r.days.has(day)) out = r.ranges;
  return out || [];
}

export function isOpenAt(rules, date) {
  const t = date.getHours() * 60 + date.getMinutes();
  const d = date.getDay();
  for (const [a, b] of rangesFor(rules, DAYS[d])) {
    if (a < b ? t >= a && t < b : t >= a) return true;
  }
  for (const [a, b] of rangesFor(rules, DAYS[(d + 6) % 7])) {
    if (a > b && t < b) return true; // overnight spill from yesterday
  }
  return false;
}

// Guess for places with no opening_hours tag. shortcut: city-agnostic defaults; tune per city.
const DEFAULT_HOURS = {
  shop: '10:00-21:00', restaurant: '11:00-23:00', fast_food: '10:00-23:00', cafe: '09:00-22:00',
  pharmacy: '09:00-22:00', bank: 'Mo-Sa 10:00-16:00', fuel: '06:00-23:00', atm: '24/7',
  hospital: '24/7', clinic: '09:00-20:00', police: '24/7', bar: '18:00-01:00', pub: '18:00-01:00', nightclub: '21:00-03:00',
};

export function placeOpen(tags, date) {
  const kind = tags.shop ? 'shop' : tags.amenity;
  const rules = parseOpeningHours(tags.opening_hours) || parseOpeningHours(DEFAULT_HOURS[kind]);
  return rules ? isOpenAt(rules, date) : false;
}

// ---------- geometry ----------
const M_LAT = 110540;
const mLon = (lat) => 111320 * Math.cos((lat * Math.PI) / 180);
export function dist(a, b) {
  const dx = (b[1] - a[1]) * mLon(a[0]), dy = (b[0] - a[0]) * M_LAT;
  return Math.hypot(dx, dy);
}
function distToSeg(p, a, b) {
  const k = mLon(p[0]);
  const ax = (a[1] - p[1]) * k, ay = (a[0] - p[0]) * M_LAT, bx = (b[1] - p[1]) * k, by = (b[0] - p[0]) * M_LAT;
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L)) : 0;
  return Math.hypot(ax + t * dx, ay + t * dy);
}
function distToLine(p, line) {
  let d = Infinity;
  for (let i = 1; i < line.length; i++) d = Math.min(d, distToSeg(p, line[i - 1], line[i]));
  return line.length === 1 ? dist(p, line[0]) : d;
}
function inPoly(p, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [yi, xi] = poly[i], [yj, xj] = poly[j];
    if ((yi > p[0]) !== (yj > p[0]) && p[1] < ((xj - xi) * (p[0] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
const nearPoly = (p, poly, r) => inPoly(p, poly) || distToLine(p, poly) <= r;

// Points every `step` metres along a [lat,lon] polyline.
export function sample(line, step = 25) {
  const out = [line[0]];
  let carry = 0;
  for (let i = 1; i < line.length; i++) {
    const a = line[i - 1], b = line[i], L = dist(a, b);
    let s = step - carry;
    while (s <= L) { out.push([a[0] + ((b[0] - a[0]) * s) / L, a[1] + ((b[1] - a[1]) * s) / L]); s += step; }
    carry = L - (s - step);
  }
  return out;
}

// ---------- OSM features ----------
const ISOLATED = /^(construction|brownfield|greenfield|cemetery|railway|industrial)$/;
const NIGHTLIFE = /^(bar|pub|nightclub)$/;

// Overpass `out geom` JSON -> buckets used by the scorer.
export function classify(elements, date) {
  const f = { lamps: [], litWays: [], open: [], police: [], cctv: [], transit: [], isolated: [], parks: [], informal: [], tunnels: [], nightlife: [] };
  for (const e of elements) {
    const t = e.tags || {};
    const geom = e.geometry ? e.geometry.map((g) => [g.lat, g.lon]) : e.lat != null ? [[e.lat, e.lon]] : null;
    if (!geom || !geom.length) continue;
    const pt = geom.length === 1 ? geom[0] : [geom.reduce((s, g) => s + g[0], 0) / geom.length, geom.reduce((s, g) => s + g[1], 0) / geom.length];
    const item = { pt, geom, name: t.name || t.shop || t.amenity || '', tags: t };
    if (t.highway === 'street_lamp') f.lamps.push(item);
    if (t.highway && t.lit && geom.length > 1) f.litWays.push(item);
    if (t.tunnel && t.tunnel !== 'no' && geom.length > 1) f.tunnels.push(item);
    if (t.man_made === 'surveillance') f.cctv.push(item);
    if (t.highway === 'bus_stop' || t.amenity === 'bus_station' || t.railway === 'station') f.transit.push(item);
    if (t.amenity === 'police') f.police.push(item);
    if (ISOLATED.test(t.landuse || '') && geom.length > 2) f.isolated.push(item);
    if (t.leisure === 'park' && geom.length > 2) f.parks.push(item);
    if ((t.residential === 'informal' || t.informal === 'yes') && geom.length > 2) f.informal.push(item);
    const nightlife = NIGHTLIFE.test(t.amenity || '') || t.shop === 'alcohol';
    if (nightlife && placeOpen(t, date)) f.nightlife.push(item);
    else if ((t.shop || t.amenity) && t.amenity !== 'police' && placeOpen(t, date)) f.open.push(item);
  }
  return f;
}

// shortcut: fixed 19:00–06:00 night window; use a sunrise/sunset calc if you go beyond one latitude band.
export const isNight = (date) => date.getHours() >= 19 || date.getHours() < 6;

const WEIGHTS = {
  day: { light: 0, active: 0.45, security: 0.3, transit: 0.25 },
  night: { light: 0.4, active: 0.35, security: 0.15, transit: 0.1 },
};

function pointScore(p, f, night, opts, seen) {
  const near = (list, r) => list.filter((x) => dist(p, x.pt) <= r);

  let light = 1;
  if (night) {
    const lamps = near(f.lamps, 30);
    lamps.forEach((x) => seen.lamps.add(x));
    const way = f.litWays.find((w) => distToLine(p, w.geom) <= 15);
    light = lamps.length ? 1 : way ? (way.tags.lit === 'no' ? 0 : 0.8) : 0.4; // 0.4 = unknown
    if (!lamps.length && !way) seen.unknownLight++;
    if (light === 0) seen.dark++;
  }
  const open = near(f.open, 60);
  open.forEach((x) => seen.open.add(x));
  const active = Math.min(open.length / 4, 1);

  const police = near(f.police, 400), cctv = near(f.cctv, 50);
  police.forEach((x) => seen.police.add(x));
  cctv.forEach((x) => seen.cctv.add(x));
  const security = police.length ? 1 : cctv.length ? 0.6 : 0;
  const transit = near(f.transit, 150).length ? 1 : 0;

  const isolated = f.isolated.some((x) => nearPoly(p, x.geom, 30)) || (night && f.parks.some((x) => nearPoly(p, x.geom, 20)))
    || f.tunnels.some((x) => distToLine(p, x.geom) <= 15);
  const nightlife = night && near(f.nightlife, 50).length > 0;
  const informal = opts.avoidInformal && f.informal.some((x) => inPoly(p, x.geom));
  if (isolated) seen.isolated++;
  if (nightlife) seen.nightlife++;
  if (informal) seen.informal++;

  const w = WEIGHTS[night ? 'night' : 'day'];
  const base = 100 * (w.light * light + w.active * active + w.security * security + w.transit * transit);
  return Math.max(0, Math.min(100, base - 25 * isolated - 15 * nightlife - 30 * informal));
}

// routes: [{line:[[lat,lon],...], distance (m), duration (s)}]
export function scoreRoutes(routes, elements, date, opts = { avoidInformal: true }) {
  const f = classify(elements, date);
  const night = isNight(date);
  const STEP = 25;
  const fastest = Math.min(...routes.map((r) => r.distance));
  const scored = routes.map((r) => {
    const seen = { lamps: new Set(), open: new Set(), police: new Set(), cctv: new Set(), unknownLight: 0, dark: 0, isolated: 0, nightlife: 0, informal: 0 };
    const pts = sample(r.line, STEP);
    const scores = pts.map((p) => pointScore(p, f, night, opts, seen));
    const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
    let weakest = 100; // worst 100 m window: one dark alley sinks the route
    for (let i = 0; i + 4 <= scores.length || i === 0; i++) {
      const win = scores.slice(i, i + 4);
      weakest = Math.min(weakest, win.reduce((a, b) => a + b, 0) / win.length);
    }
    let run = 0, longestBad = 0;
    for (const s of scores) { run = s < 35 ? run + STEP : 0; longestBad = Math.max(longestBad, run); }
    const detour = Math.max(0, r.distance / fastest - 1.3) * 50;
    const score = Math.round(Math.max(0, 0.6 * avg + 0.4 * weakest - detour));

    const reasons = [];
    if (seen.open.size) reasons.push(`${seen.open.size} places open on the way`);
    if (night && seen.lamps.size) reasons.push(`${seen.lamps.size} street lamps`);
    if (seen.police.size) reasons.push(`police nearby (${[...seen.police].map((x) => x.name).filter(Boolean)[0] || 'station'})`);
    if (seen.cctv.size) reasons.push(`${seen.cctv.size} CCTV cameras`);
    const warnings = [];
    if (longestBad >= 75) warnings.push(`${longestBad} m low-safety stretch`);
    if (seen.dark) warnings.push(`${seen.dark * STEP} m on unlit road`);
    if (seen.isolated) warnings.push(`${seen.isolated * STEP} m past isolated land / park / tunnel`);
    if (seen.nightlife) warnings.push(`${seen.nightlife * STEP} m past bars / liquor shops`);
    if (seen.informal) warnings.push(`${seen.informal * STEP} m through informal settlement`);
    const lowData = night ? seen.unknownLight / pts.length > 0.6 : seen.open.size === 0;

    return { ...r, score, avg: Math.round(avg), weakest: Math.round(weakest), samples: pts.map((p, i) => [p, scores[i]]),
      reasons, warnings, lowData, havens: [...seen.open, ...seen.police] };
  });

  const bySafe = [...scored].sort((a, b) => b.score - a.score);
  const fast = scored.find((r) => r.distance === fastest);
  bySafe[0].label = 'Safest';
  if (!fast.label) fast.label = 'Fastest';
  // Balanced: best safety per extra metre, among the rest.
  const rest = scored.filter((r) => !r.label);
  if (rest.length) rest.sort((a, b) => (b.score - 40 * (b.distance / fastest - 1)) - (a.score - 40 * (a.distance / fastest - 1)))[0].label = 'Balanced';
  return bySafe;
}
