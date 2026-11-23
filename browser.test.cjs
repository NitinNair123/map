// The app in a phone-sized Chromium against fixture_server.py.
// Usage: npm install --no-save playwright && npx playwright install chromium && node --test browser.test.cjs
const { spawn } = require("node:child_process");
const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const { chromium, devices } = require("playwright");

const PORT = 8765, BASE = `http://127.0.0.1:${PORT}`;
const A = { latitude: 28.0, longitude: 77.0 };
let srv, browser;

before(async () => {
  srv = spawn(process.platform === "win32" ? "py" : "python3", ["fixture_server.py", String(PORT)], { stdio: "inherit" });
  for (let i = 0; i < 50; i++) {
    try { if ((await fetch(BASE + "/app/")).ok) break; } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
  browser = await chromium.launch();
});
after(async () => { await browser?.close(); srv?.kill(); });

async function phone(contacts = []) {
  const ctx = await browser.newContext({ ...devices["Pixel 7"], geolocation: A, permissions: ["geolocation"] });
  await ctx.addInitScript((c) => { localStorage.setItem("sr.onboarded", "true"); localStorage.setItem("sr.contacts", JSON.stringify(c)); }, contacts);
  // fully offline: Leaflet and MapLibre are served by the app itself; no fonts or map pictures
  await ctx.route(/^https:\/\//, (r) => r.abort());
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  return { ctx, page, errors };
}

async function routeToPharmacy(page) {
  await page.goto(BASE + "/app/");
  await page.fill("#toInput", "city");
  await page.click("#results li >> text=City Pharmacy");
  await page.waitForSelector("#startBtn", { timeout: 15000 });
}

test("plan a route and start a trip on a phone screen", async () => {
  const { ctx, page, errors } = await phone();
  await routeToPharmacy(page);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "no sideways scrolling");
  assert.match(await page.textContent("#sheetBody"), /Downloaded: OpenStreetMap today/);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  assert.ok(await page.isVisible("text=Keep SafeRoute open with the screen on"));
  assert.equal(await page.locator("[data-quick]").count(), 4, "busy / deserted / shops open / shut taps while walking");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("route error shows a message and a retry button", async () => {
  const { ctx, page } = await phone();
  await page.route("**/api/route?*", (r) => r.fulfill({ status: 503, contentType: "application/json", body: '{"error":"Map data servers are busy"}' }));
  await page.goto(BASE + "/app/");
  await page.fill("#toInput", "city");
  await page.click("#results li >> text=City Pharmacy");
  await page.waitForSelector("text=Map data servers are busy");
  assert.ok(await page.isVisible("#retryRoute"));
  await ctx.close();
});

test("SOS: hold to open, then the text is written for the user to send", async () => {
  const { ctx, page, errors } = await phone([{ name: "Mum", phone: "+91 98765 43210" }]);
  await page.goto(BASE + "/app/");
  await page.hover("#sosBtn");
  await page.mouse.down();
  await page.waitForTimeout(1700);
  await page.mouse.up();
  assert.ok(await page.isVisible("#sosOverlay"));
  assert.equal(await page.textContent("#sosTitle"), "SOS message");
  await page.click("#sosAlertNow");
  await page.waitForSelector("text=Press Send in your messages app");
  assert.match(await page.textContent("#sosSub"), /only goes out when you press Send.*shows where you are/);
  await page.click("#sosCancel");
  assert.ok(await page.isHidden("#sosOverlay"), "cancel closes SOS");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("SOS ambulance alert carries the medical info from settings", async () => {
  const { ctx, page, errors } = await phone();
  await ctx.addInitScript(() => {
    localStorage.setItem("sr.medical", JSON.stringify({ blood: "B+", allergies: "penicillin" }));
    navigator.share = async (d) => { window.shared = d.text; };
  });
  await page.goto(BASE + "/app/");
  await page.focus("#sosBtn");
  await page.keyboard.press("Enter");
  await page.click("#sosMedical");
  await page.waitForFunction(() => window.shared);
  assert.match(await page.evaluate(() => window.shared), /ambulance\. Blood group: B\+\. Allergies: penicillin\. Live location: .*maps\.google\.com/);
  assert.equal(await page.getAttribute("a[href='tel:108']", "href"), "tel:108");
  await page.click("#sosCancel");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("friend's viewer follows a live trip and shows when it ends", async () => {
  const post = (path, body) => fetch(BASE + path, { method: "POST", body: JSON.stringify(body) }).then((r) => r.json());
  const t = await post("/api/trip", { dest: [28.0007, 77.0025], eta: 5, name: "Asha" });
  await post(`/api/trip/${t.id}`, { key: t.key, lat: 28.0, lon: 77.0 });
  const { ctx, page } = await phone();
  const referers = [];
  await ctx.route(/tile\.openstreetmap\.org/, (r) => { referers.push(r.request().headers().referer); r.abort(); });
  await page.goto(`${BASE}/t/?id=${t.id}`);
  await page.waitForSelector("text=Asha is walking");
  // OpenStreetMap refuses map pictures without a referrer; it must get the site, never the page with the trip id
  assert.ok(referers.length && referers.every((r) => r === BASE + "/"), JSON.stringify(referers.slice(0, 2)));
  await post(`/api/trip/${t.id}`, { key: t.key, ended: true });
  await page.reload();
  await page.waitForSelector("text=Asha ended the trip");
  await ctx.close();
});

test("stops and priorities go with the route request", async () => {
  const { ctx, page, errors } = await phone();
  await routeToPharmacy(page);
  await page.click("#settingsBtn");
  await page.locator("#prio-pharmacy").fill("3");
  await page.locator("#prio-pharmacy").dispatchEvent("change");
  assert.match(await page.textContent("#prio-pharmacy-v"), /Most/);
  await page.keyboard.press("Escape");
  await page.evaluate(() => document.querySelector("#drawer").hidden = true);
  const asked = page.waitForRequest((r) => r.url().includes("/api/route?") && r.url().includes("via="), { timeout: 15000 });
  await page.click("#addStopBtn");
  await page.fill("[data-stop='0']", "city");
  await page.click("#results li >> text=City Pharmacy");
  const url = new URL((await asked).url());
  assert.equal(url.searchParams.get("via"), "28.0007,77.0025");
  assert.equal(url.searchParams.get("prefer"), "pharmacy:3");
  await page.waitForSelector("#startBtn", { timeout: 15000 });
  await page.click("[data-del-stop='0']");
  assert.equal(await page.locator("[data-stop]").count(), 0);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("live trip switches when another way becomes clearly safer", async () => {
  const { ctx, page, errors } = await phone();
  await routeToPharmacy(page);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  const before = await page.evaluate(() => state.trip.route.coords);
  // the next check finds the other way 50 points better than the rest of this one
  await page.route("**/api/route?*", async (r) => {
    const res = await (await r.fetch()).json();
    const mine = res.routes.find((x) => JSON.stringify(x.coords) === JSON.stringify(before));
    const other = res.routes.find((x) => JSON.stringify(x.coords) !== JSON.stringify(before));
    mine.score = 40; other.score = 90;
    res.routes = [other, ...res.routes.filter((x) => x !== other)];
    r.fulfill({ json: res });
  });
  await page.evaluate(() => { state.trip.lastRecheck = 0; onPosition({ coords: { latitude: 28.0, longitude: 77.0 } }, true); });
  await page.waitForSelector("text=Safer way found");
  assert.notDeepEqual(await page.evaluate(() => state.trip.route.coords), before);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("voice: a typed request sets the wishes, routes on main roads and reads back what was measured", async () => {
  const { ctx, page, errors } = await phone();
  await page.goto(BASE + "/app/");
  await page.click("#voiceBtn");
  assert.ok(await page.isVisible("#recBtn"), "the mic is offered where recording works");
  await page.fill("#voiceText", "walk to city pharmacy at 11 pm, stay on main roads, past a chemist");
  const req = page.waitForRequest((r) => r.url().includes("/api/route?"), { timeout: 15000 });
  await page.click("#voiceSend");
  const q = new URL((await req).url()).searchParams;
  assert.equal(q.get("wish"), "main");
  assert.match(q.get("prefer"), /pharmacy:3/);
  assert.equal(q.get("mode"), "walk");
  assert.match(q.get("at"), /T23:00/);
  await page.waitForSelector("#startBtn", { timeout: 15000 });
  assert.match(await page.textContent("#wishChip"), /Main roads · Medical stores/);
  await page.waitForSelector("#toast:not([hidden])");
  assert.match(await page.textContent("#toast"), /route: \d+ minutes, \d+ percent on main roads/);
  await page.click("#wishChip");  // one tap removes the voice wishes and routes again without them
  assert.ok(await page.isHidden("#wishChip"));
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("voice: a cry for help opens the SOS countdown and never sends by itself", async () => {
  const { ctx, page, errors } = await phone([{ name: "Mum", phone: "+91 98765 43210" }]);
  await page.goto(BASE + "/app/");
  await page.click("#voiceBtn");
  await page.fill("#voiceText", "bachao");
  await page.click("#voiceSend");
  await page.waitForSelector("#sosOverlay:not([hidden])");
  assert.ok(await page.isVisible("#sosCancel"), "the countdown can be cancelled");
  await page.click("#sosCancel");
  assert.ok(await page.isHidden("#sosOverlay"));
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("voice: a spoken request is recorded and sent to the server as audio", async () => {
  const mic = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] });
  const ctx = await mic.newContext({ ...devices["Pixel 7"], geolocation: A, permissions: ["geolocation", "microphone"] });
  await ctx.addInitScript(() => localStorage.setItem("sr.onboarded", "true"));
  await ctx.route(/^https:\/\//, (r) => r.abort());
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let sent = null;
  await page.route("**/api/voice", (r) => {  // Sarvam and Gemini aren't reachable from tests: answer as the server would
    sent = { type: r.request().headers()["content-type"], bytes: r.request().postDataBuffer()?.length || 0 };
    r.fulfill({ contentType: "application/json", body: JSON.stringify({ text: "mujhe city pharmacy jaana hai", by: "ai", form: {
      intent: "route", destination: "city pharmacy", stops: [], mode: "", time: "", prefer: [], main_roads: false, near_police: true, asks: ["feeling"] } }) });
  });
  await page.goto(BASE + "/app/");
  await page.click("#voiceBtn");
  await page.click("#recBtn");
  await page.waitForSelector("text=Listening…");
  await page.waitForTimeout(800);
  const req = page.waitForRequest((r) => r.url().includes("/api/route?"), { timeout: 15000 });
  await page.click("#recBtn");
  assert.equal(new URL((await req).url()).searchParams.get("wish"), "police");
  assert.match(sent.type, /^audio\//);
  assert.ok(sent.bytes > 0, "the clip has sound in it");
  await page.waitForSelector("#toast:not([hidden])");
  assert.match(await page.textContent("#toast"), /police|no police station mapped/);
  assert.match(await page.textContent("#toast"), /can't measure .* how it feels/);
  assert.deepEqual(errors, []);
  await mic.close();
});

test("save home once, then one tap routes there; it can be forgotten in settings", async () => {
  const { ctx, page, errors } = await phone();
  await page.goto(BASE + "/app/");
  await page.click("[data-go=home]");
  await page.fill("#toInput", "city");
  await page.click("#results li >> text=City Pharmacy");
  await page.waitForSelector("#startBtn", { timeout: 15000 });
  assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem("sr.saved")).home.label), "City Pharmacy");
  await page.reload();
  await page.click("[data-go=home]");
  await page.waitForSelector("#startBtn", { timeout: 15000 });
  assert.equal(await page.inputValue("#toInput"), "Home");
  await page.fill("#toInput", "ho");
  await page.waitForSelector("#results li >> text=Home");
  await page.reload();
  await page.click("#settingsBtn");
  await page.click("[data-unsave=home]");
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem("sr.saved")).home), undefined);
  await page.click("#drawer [data-close]");
  assert.ok(await page.isVisible("text=Set home"), "the start screen offers to set it again");
  assert.equal(await page.getAttribute("a[href='tel:1091']", "href"), "tel:1091", "SOS offers the women helpline");
  assert.deepEqual(errors, []);
  await ctx.close();
});
