// Voice requests: "take me to Sector 18 metro, main roads, past a pharmacy" or "mujhe CP jaana hai, main road se".
// The server turns speech into text (Sarvam) and fills a form (Gemini, or word rules as a fallback); this file applies
// the form with the same functions the buttons use. The route is still picked by the app's rules, and the read-back
// only says what the route measured. A cry for help opens the SOS screen with its countdown: voice never sends it.
const WISH_NAME = { main: "Main roads", police: "Near police" };
const MAX_REC_MS = 10000;
let recorder = null;

function say(text) {  // the assistant answers the person who just spoke, so it talks even when directions are muted
  if (!window.speechSynthesis) return;
  speechSynthesis.cancel();
  speechSynthesis.speak(Object.assign(new SpeechSynthesisUtterance(text), { lang: "en-IN", rate: 1.02 }));
}

function showWishes() {
  const names = [...state.wishes.map((w) => WISH_NAME[w]), ...Object.keys(state.voicePrefer).map((g) => PRIORITY[g])];
  $("#wishChip").hidden = !names.length;
  $("#wishLabel").textContent = names.join(" · ");
}
$("#wishChip").onclick = () => { state.wishes = []; state.voicePrefer = {}; showWishes(); maybeRoute(); };

function voicePanel(status) {
  const canRecord = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  openDrawer(`
    <h2 id="drawerTitle">Where do you want to go?</h2>
    <p class="muted" id="voiceStatus" aria-live="polite">${esc(status || (canRecord ? "Tap the mic and speak. Say what you'd like on the way too, like main roads, a pharmacy or open shops." : "Type where you want to go and what you'd like on the way."))}</p>
    ${canRecord ? `<button class="btn btn-primary btn-block" id="recBtn">${icon("mic")}Tap to speak</button>` : ""}
    <div class="field" style="margin-top:12px"><input id="voiceText" class="input" placeholder="Or type it: walk to India Gate on main roads" autocomplete="off" aria-label="Type your request"></div>
    <button class="btn btn-block" id="voiceSend">Go</button>
    <div id="voiceChoices"></div>
    <p class="muted" style="margin-top:12px;font-size:12px">Your voice goes to Sarvam (India) to become text, and the words to Google Gemini to understand them. SafeRoute doesn't keep either. If you're in danger, call 112.</p>`);
  $("#voiceSend").onclick = () => { const t = $("#voiceText").value.trim(); if (t) send({ text: t }); };
  $("#voiceText").onkeydown = (e) => { if (e.key === "Enter") $("#voiceSend").click(); };
  if (canRecord) $("#recBtn").onclick = () => (recorder ? recorder.stop() : record());
}
const status = (text) => { const s = $("#voiceStatus"); if (s) s.textContent = text; };

async function record() {
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { status("The microphone is blocked. Allow it in your browser settings, or type instead."); return; }
  const chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => chunks.push(e.data);
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    clearTimeout(recorder.timer);
    const type = recorder.mimeType || "audio/webm";
    recorder = null;
    $("#recBtn") && ($("#recBtn").innerHTML = `${icon("mic")}Tap to speak`);
    send(new Blob(chunks, { type }));
  };
  speechSynthesis?.cancel();
  recorder.start();
  recorder.timer = setTimeout(() => recorder?.stop(), MAX_REC_MS);
  $("#recBtn").innerHTML = `${icon("mic")}Listening… tap when done`;
  status("Listening…");
}

async function send(what) {
  status("Understanding…");
  let res;
  try {
    if (what instanceof Blob) {
      const r = await fetch("/api/voice", { method: "POST", headers: { "Content-Type": what.type }, body: what });
      res = await r.json().catch(() => ({ error: "Unexpected response from the server" }));
      if (!r.ok || res.error) throw new Error(res.error || `Request failed (${r.status})`);
    } else res = await api("/api/voice", what, 15000);
  } catch (err) { status(`${err.message}`); return; }
  if (res.text) $("#voiceText").value = res.text;
  apply(res.form);
}

function departAt(hhmm) {
  const [h, m] = hhmm.split(":").map(Number), d = new Date();
  d.setHours(h, m, 0, 0);
  if (d < Date.now() - 5 * 60000) d.setDate(d.getDate() + 1);  // "11 pm" said at midnight means tomorrow
  return d;
}

async function findPlace(q) {
  const c = map.getCenter().wrap(), near = state.here || [c.lat, c.lng];
  return api(`/api/search?q=${encodeURIComponent(q)}&near=${near.join(",")}`).catch(() => []);
}

async function apply(f) {
  if (f.intent === "sos") { closeDrawer(); openSOS(); return; }  // the countdown and Cancel show; nothing is sent by voice
  if (f.intent === "cancel") { closeDrawer(); say("Okay, cancelled."); return; }
  if (f.intent === "help") {
    const h = "Say where you want to go, and what you'd like on the way: main roads, near police, a pharmacy, open shops, or a time like 11 pm.";
    status(h); say(h); return;
  }
  if (f.mode && !state.trip) { state.mode = f.mode; store.set("mode", f.mode); showMode(); }
  if (f.time) { state.when = departAt(f.time); $("#timeLabel").textContent = timeLabel(); }
  if (f.main_roads || f.near_police) state.wishes = [...(f.main_roads ? ["main"] : []), ...(f.near_police ? ["police"] : [])];
  if (f.prefer.length) state.voicePrefer = Object.fromEntries(f.prefer.map((g) => [g, 3]));
  showWishes();
  if (!f.destination) {
    if (state.to && f.intent === "route") { closeDrawer(); await maybeRoute(); return readBack(f); }  // a wish for the trip already set
    const q = "Sorry, where do you want to go?";
    status(q); say(q); return;
  }
  const found = (await findPlace(f.destination)).slice(0, 3);
  if (!found.length) { const m = `I couldn't find ${f.destination}. Try saying it again, or type it.`; status(m); say(m); return; }
  const go = async (p) => {
    setPlace("to", p.pt, p.name);
    state.stops = [];
    for (const s of f.stops) { const [hit] = await findPlace(s); if (hit) state.stops.push({ pt: hit.pt, label: hit.name }); }
    renderStops();
    closeDrawer();
    await maybeRoute();
    readBack(f);
  };
  if (found.length === 1) return go(found[0]);
  status("Which one?");
  say(`Which one? ${found.map((p, i) => `${i + 1}, ${p.name}`).join(". ")}`);
  $("#voiceChoices").innerHTML = found.map((p, i) => `<button class="btn btn-block" data-pick="${i}">${i + 1}. ${esc(p.name)}<small class="muted"> ${p.km != null ? `${p.km} km` : ""}</small></button>`).join("");
  document.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => go(found[+b.dataset.pick])));
}

// Only what the route measured, in our own words. Safetipin's other parameters are named honestly when not measured.
function readBack(f) {
  const r = state.routes.find((x) => x.kind === state.sel);
  if (!r) return;  // the sheet already shows why there's no route
  const s = r.stats || {}, parts = [`${cap(r.kind)} route: ${r.minutes} minutes`];
  if (s.main_pct != null) parts.push(`${s.main_pct} percent on main roads`);
  if (f.near_police) parts.push(s.police_m != null ? `police within ${fmtDist(s.police_m)}` : "no police station mapped near it");
  for (const g of f.prefer) { const n = (r.places || []).filter((p) => p.group === g).length; parts.push(`${n || "no"} ${PRIORITY[g].toLowerCase()} on the way`); }
  if (f.asks.includes("lighting") && s.lit_pct != null) parts.push(`${s.lit_pct} percent is mapped as lit`);
  if (f.asks.includes("people") && s.open_now != null) parts.push(`${s.open_now} places open as you pass`);
  if (f.asks.includes("openness") && s.isolated_m) parts.push(`${fmtDist(s.isolated_m)} with nothing around`);
  let text = parts.join(", ") + ".";
  if (r.warnings?.length) text += ` Watch out: ${r.warnings[0]}.`;
  if (f.main_roads && s.main_pct != null && s.main_pct < 50) text += " That's the most main road I could find without a long detour.";
  if (f.asks.some((a) => ["walk_path", "gender_usage", "feeling"].includes(a))) text += " I can't measure footpaths, who is on the street, or how it feels, so trust your instincts.";
  toast(text, 8000);
  say(text);
}

$("#voiceBtn").onclick = () => voicePanel();
