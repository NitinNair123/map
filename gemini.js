// Optional Gemini helpers. Without a key (config.js) every function returns nothing and the app works as before.
import { sample, dist } from './score.js';

const MODEL = 'gemini-2.5-flash-lite'; // cheapest model that supports Google Search grounding
const key = () => globalThis.SAFEROUTE_CONFIG?.geminiKey;
export const hasGemini = () => !!key();

const cache = {
  get(k) { try { return JSON.parse(localStorage.getItem(`gem:${k}`)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(`gem:${k}`, JSON.stringify(v)); } catch { /* storage full or blocked */ } },
};

// Grounded answers can't use JSON mode, so pull the first JSON value out of the reply text.
export function parseJson(text) {
  const m = String(text).match(/[[{][\s\S]*[\]}]/);
  if (!m) return null;
  try { return JSON.parse(m[0]); } catch { return null; }
}

async function ask(prompt, maxOutputTokens) {
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-goog-api-key': key() },
    body: JSON.stringify({
      contents: [{ parts: [{ text: prompt }] }],
      tools: [{ google_search: {} }],
      generationConfig: { temperature: 0, maxOutputTokens, thinkingConfig: { thinkingBudget: 0 } },
    }),
  });
  if (!r.ok) throw new Error(`Gemini ${r.status}`);
  const j = await r.json();
  return parseJson((j.candidates?.[0]?.content?.parts || []).map((p) => p.text || '').join(''));
}

// Fills tags.opening_hours on named places near the routes that OSM has no hours for.
// One request per search at most (MAX places), and each place is only ever asked about once.
const MAX = 15;
export async function fillShopHours(elements, routes, area) {
  if (!hasGemini()) return 0;
  const pts = routes.flatMap((r) => sample(r.line, 50));
  const at = (e) => (e.lat != null ? [e.lat, e.lon] : e.geometry?.[0] && [e.geometry[0].lat, e.geometry[0].lon]);
  const near = (e) => at(e) && pts.some((p) => dist(p, at(e)) <= 60);
  const want = elements.filter((e) => e.tags?.name && (e.tags.shop || e.tags.amenity) && !e.tags.opening_hours && e.tags.amenity !== 'police' && near(e));
  let filled = 0;
  const batch = [];
  for (const e of want) {
    const hit = cache.get(`h:${e.type}/${e.id}`);
    if (hit === null) { if (batch.length < MAX) batch.push(e); continue; }
    if (hit) { e.tags.opening_hours = hit; filled++; }
  }
  if (batch.length) {
    const list = batch.map((e, i) => `${i}|${e.tags.name}|${e.tags.shop || e.tags.amenity}`).join('\n');
    const ans = await ask(`Opening hours of these places near ${area}. Lines are index|name|type.
${list}
Reply only JSON {"index":"OSM opening_hours like Mo-Sa 10:00-21:00"}; use "" when unsure.`, 40 * batch.length);
    batch.forEach((e, i) => {
      const oh = typeof ans?.[i] === 'string' ? ans[i].trim() : '';
      cache.set(`h:${e.type}/${e.id}`, oh); // "" = unknown, so we don't ask again
      if (oh) { e.tags.opening_hours = oh; filled++; }
    });
  }
  return filled;
}

// Live intel (VIP movement, barricades, closures, protests) around the trip, as hazards for scoreRoutes.
// Cached per ~1 km area per hour of departure.
export async function eventHazards(a, b, date, area) {
  if (!hasGemini()) return [];
  const k = `e:${[...a, ...b].map((x) => x.toFixed(2)).join(',')}:${date.toISOString().slice(0, 13)}`;
  const hit = cache.get(k);
  if (hit) return hit;
  const when = date.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
  const ans = await ask(`Search latest news for ${when} along a walk from ${a.join(',')} to ${b.join(',')} (${area}):
VIP/convoy movement, police barricades, road closures, protests, large events.
Reply only JSON [{"lat":0,"lon":0,"radius_m":300,"what":"under 8 words"}] for items with a known place; [] if none.`, 300);
  const out = (Array.isArray(ans) ? ans : [])
    .filter((h) => Number.isFinite(h?.lat) && Number.isFinite(h?.lon))
    .slice(0, 8)
    .map((h) => ({ pt: [h.lat, h.lon], r: Math.min(Math.max(+h.radius_m || 300, 100), 1500), what: String(h.what || 'Reported disruption').slice(0, 60), penalty: 30 }));
  cache.set(k, out);
  return out;
}
