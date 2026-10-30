// SOS sheet: emergency calls, a prefilled ambulance message, and a medical profile kept only on this device.
const KEY = 'sosProfile';
const FIELDS = ['name', 'blood', 'conditions', 'allergies', 'meds', 'contacts', 'geminiKey'];
const GEMINI = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent';

export function emergencyText(p, loc) {
  const line = (label, v) => (v ? `${label}: ${v}. ` : '');
  return `🚑 MEDICAL EMERGENCY. I need an ambulance. ${line('Name', p.name)}${line('Blood group', p.blood)}`
    + `${line('Conditions', p.conditions)}${line('Allergies', p.allergies)}${line('Medication', p.meds)}`
    + (loc ? `Location: https://maps.google.com/?q=${loc[0]},${loc[1]}` : 'Location unavailable.');
}

// Gemini only rewords the message; it never sends or calls anything. Falls back to the template on any failure.
async function geminiText(p, loc, fallback) {
  if (!p.geminiKey) return fallback;
  const prompt = 'Rewrite this as a short, clear emergency message for an ambulance dispatcher or family member. '
    + 'Put the most critical medical facts first (allergies, conditions that change treatment). Keep every fact and the location link exactly; add nothing. '
    + `Plain text, under 400 characters.\n\n${fallback}`;
  try {
    const r = await fetch(`${GEMINI}?key=${encodeURIComponent(p.geminiKey)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
      signal: AbortSignal.timeout(5000),
    });
    const text = (await r.json()).candidates?.[0]?.content?.parts?.[0]?.text?.trim();
    // A reworded message that lost the location is worse than the template.
    return text && (!loc || text.includes(`${loc[0]},${loc[1]}`)) ? text : fallback;
  } catch { return fallback; }
}

const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };

export function initSOS(here) {
  const $ = (id) => document.getElementById(id);
  const dlg = $('sosSheet');
  const profile = load();
  for (const f of FIELDS) $(`p_${f}`).value = profile[f] || '';

  // Build the message as soon as the sheet opens, so the share tap still counts as a user gesture.
  let pending;
  const build = async () => {
    let loc = null;
    try { loc = await here(); } catch { /* still send without location */ }
    return geminiText(profile, loc, emergencyText(profile, loc));
  };
  $('sos').onclick = () => { pending = build(); dlg.showModal(); };
  $('sosClose').onclick = () => dlg.close();
  $('profileForm').onsubmit = (e) => {
    e.preventDefault();
    for (const f of FIELDS) profile[f] = $(`p_${f}`).value.trim();
    pending = build();
    try { localStorage.setItem(KEY, JSON.stringify(profile)); $('profileMsg').textContent = 'Saved on this device.'; } catch { $('profileMsg').textContent = 'Could not save (private browsing?).'; }
  };

  $('sosAlert').onclick = async () => {
    $('sosMsg').textContent = 'Preparing message…';
    const text = await pending;
    $('sosMsg').textContent = '';
    if (navigator.share) { try { await navigator.share({ text }); return; } catch { /* cancelled: fall through to SMS */ } }
    const to = (profile.contacts || '').split(/[,;\s]+/).filter(Boolean).join(',');
    location.href = `sms:${to}?&body=${encodeURIComponent(text)}`;
  };
}
