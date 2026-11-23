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

test("SOS works offline once the app has been opened: helplines and a text with the location", async () => {
  const { ctx, page, errors } = await phone([{ name: "Mum", phone: "+91 98765 43210" }]);
  await page.goto(BASE + "/app/");
  await page.evaluate(() => navigator.serviceWorker.ready);
  if (!(await page.evaluate(() => navigator.serviceWorker.controller))) await page.reload();
  await page.waitForFunction(() => navigator.serviceWorker.controller);
  await ctx.setOffline(true);
  await page.reload();
  await page.focus("#sosBtn");
  await page.keyboard.press("Enter");
  assert.ok(await page.isVisible("#sosOverlay"));
  assert.ok(await page.isVisible("a[href='tel:112'] >> text=Call 112"));
  assert.ok(await page.isVisible("a[href='tel:1091'] >> text=Women helpline 1091"));
  const sms = page.waitForRequest(/^sms:/, { timeout: 15000 });
  await page.click("#sosAlertNow");
  assert.match((await sms).url(), /^sms:\+919876543210\?&body=SOS%20from%20me.*maps\.google\.com%2F%3Fq%3D28\.000000%2C77\.000000/);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("SOS by keyboard: focus starts inside, Tab stays inside, Escape cancels", async () => {
  const { ctx, page, errors } = await phone();
  await page.goto(BASE + "/app/");
  await page.focus("#sosBtn");
  await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.activeElement.id), "sosCancel");
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press(i % 3 ? "Tab" : "Shift+Tab");
    assert.ok(await page.evaluate(() => !!document.activeElement.closest("#sosOverlay")), "focus left the SOS dialog");
  }
  await page.keyboard.press("Escape");
  assert.ok(await page.isHidden("#sosOverlay"));
  assert.equal(await page.evaluate(() => document.activeElement.id), "sosBtn");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("large system text (130%): every route option fits the panel and small chips tap as 44 px", async () => {
  const { ctx, page } = await phone();
  await page.setViewportSize({ width: 317, height: 645 });  // Chrome on Android zooms the page by the system font size
  await routeToPharmacy(page);
  assert.equal(await page.evaluate(() => { const s = document.querySelector("#sheet"); return s.scrollWidth - s.clientWidth; }), 0, "no sideways scrolling in the panel");
  for (const tab of await page.locator(".tab").all()) assert.ok((await tab.boundingBox()).x + (await tab.boundingBox()).width <= 317, "a route option is off screen");
  const hit = await page.evaluate(() => { const r = document.querySelector("#voiceBtn").getBoundingClientRect(); return document.elementFromPoint(r.left + r.width / 2, r.bottom + 2)?.id; });
  assert.equal(hit, "voiceBtn", "a tap just below the round chip still reaches it");
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

test("ride booking: Uber gets pickup, our detour as a stop, then the drop; Ola gets pickup and drop", async () => {
  const { ctx, page, errors } = await phone();
  // pure check of the stop picker on made-up routes: a 1 km bulge north of a straight fastest road
  const picked = await page.goto(BASE + "/app/").then(() => page.evaluate(() => {
    const fastest = { kind: "fastest", mode: "car", coords: [[28, 77], [28, 77.01], [28, 77.02]] };
    const safest = { kind: "safest", mode: "car", coords: [[28, 77], [28.004, 77.005], [28.009, 77.01], [28.004, 77.015], [28, 77.02]] };
    // a 160 m blip off the fastest road near the start, then a 6 km detour far to the north
    const blip = { kind: "safest", mode: "car", coords: [[28, 77], [28.00145, 77.002], [28, 77.004], [28, 77.01], [28.02, 77.02],
      [28.025, 77.03], [28.025, 77.04], [28.02, 77.05], [28, 77.06]] };
    const straight = { kind: "fastest", mode: "car", coords: [[28, 77], [28, 77.06]] };
    return { car: rideStops(safest, fastest), walk: rideStops({ ...safest, mode: "walk" }, fastest), same: rideStops(fastest, fastest),
      long: rideStops(blip, straight).map((x) => x.i) };
  }));
  assert.deepEqual(picked.car.map((x) => x.pt), [[28.009, 77.01]], "one stop, in the middle of the detour");
  assert.deepEqual(picked.long, [5, 7], "the blip gets no stop; the long detour gets two, a third and two thirds along");
  assert.deepEqual(picked.walk, [], "walking routes may use footpaths, so no stops");
  assert.deepEqual(picked.same, []);
  await page.click(".modes [data-mode='car']");
  await routeToPharmacy(page);
  await page.click("#rideBox summary");
  const uber = new URL(await page.getAttribute("#uberLink", "href"));
  assert.equal(uber.origin + uber.pathname, "https://m.uber.com/looking");
  const drops = [...uber.searchParams.keys()].filter((k) => k.startsWith("drop["));
  const last = JSON.parse(uber.searchParams.get(drops.at(-1)));
  assert.deepEqual([last.latitude, last.longitude, last.addressLine1], [28.0007, 77.0025, "City Pharmacy"]);
  assert.ok(JSON.parse(uber.searchParams.get("pickup")).latitude, "pickup has coordinates");
  const ola = new URL(await page.getAttribute("#olaLink", "href"));
  assert.equal(ola.searchParams.get("drop_lat"), "28.0007");
  assert.equal(ola.searchParams.get("landing_page"), "bk");
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "no sideways scrolling");
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

test("in a cab, the vehicle number reaches the friend's live viewer", async () => {
  const { ctx, page, errors } = await phone();
  await page.addInitScript(() => { navigator.share = async (d) => { window.shared = d.text; }; });
  await page.goto(BASE + "/app/");
  await page.click("[data-mode=car]");
  await page.fill("#toInput", "city");
  await page.click("#results li >> text=City Pharmacy");
  await page.waitForSelector("#startBtn", { timeout: 15000 });
  const minutes = await page.evaluate(() => state.routes.find((x) => x.kind === state.sel).minutes);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  const left = +(await page.textContent(".nav-head h3")).match(/(\d+) min/)[1];
  assert.ok(left <= minutes + 1, `a drive is timed at driving pace: ${left} min left on a ${minutes} min route`);
  await page.fill("#vehicleForm input", "dl 1r ab 1234");
  await page.click("#vehicleForm button");
  await page.waitForSelector("text=Your live link now shows DL 1R AB 1234");
  const id = await page.evaluate(() => state.trip.id);
  const viewer = await ctx.newPage();
  await viewer.goto(`${BASE}/t/?id=${id}`);
  await viewer.waitForSelector("text=In vehicle DL 1R AB 1234");
  assert.match(await viewer.textContent("#title"), /is on the way/);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("at low battery the walker is offered a last-location text and friends see the level", async () => {
  const { ctx, page, errors } = await phone([{ name: "Asha", phone: "+91 98100 00000" }]);
  await page.addInitScript(() => {
    navigator.share = async () => {};
    navigator.getBattery = async () => ({ level: 0.12, charging: false });
  });
  await routeToPharmacy(page);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  await page.evaluate(() => onTripPosition([28.0001, 77.0001]));
  await page.waitForSelector("text=Battery at 12%");
  assert.match(await page.getAttribute("#batterySms", "href"), /^sms:\+919810000000\?&body=My%20phone%20is%20at%2012%25.*maps\.google\.com.*%2Ft%2F%3Fid%3D/);
  const viewer = await ctx.newPage();
  await viewer.goto(`${BASE}/t/?id=${await page.evaluate(() => state.trip.id)}`);
  await viewer.waitForSelector("text=Phone battery 12%");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("zoomed out, only open places are pinned so the route line stays visible", async () => {
  const { ctx, page, errors } = await phone();
  await routeToPharmacy(page);
  await page.evaluate(() => { state.routes.find((r) => r.kind === state.sel).places[0].open = null; drawRoutes(); });  // one place without hours
  const pins = async (z) => {
    await page.evaluate((z) => new Promise((r) => { map.once("zoomend", r); map.setZoom(z, { animate: false }); }), z);
    return page.evaluate(() => ({ all: document.querySelectorAll(".gpin").length, notOpen: document.querySelectorAll(".gpin.closed, .gpin.unknown").length }));
  };
  const near = await pins(17), mid = await pins(15), far = await pins(13);
  assert.ok(near.notOpen > 0, "zoomed in, closed and unknown places show too");
  assert.equal(mid.notOpen, 0);
  assert.ok(mid.all > 0 && mid.all < near.all, JSON.stringify({ near, mid }));
  assert.equal(far.all, 0);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("tapping an open place near you routes there", async () => {
  const { ctx, page, errors } = await phone();
  await page.goto(BASE + "/app/");
  await page.click("[data-haven='0']");
  await page.waitForSelector("#startBtn", { timeout: 15000 });
  assert.notEqual(await page.inputValue("#toInput"), "");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("a message shown while search results are open sits below them, not over them", async () => {
  const { ctx, page, errors } = await phone();
  await page.goto(BASE + "/app/");
  await page.evaluate(() => toast("Location is off. You can still pick places on the map.", 10000));  // shown first, as when location is refused
  await page.fill("#toInput", "city");
  await page.waitForSelector("#results li");
  const list = await page.locator("#results").boundingBox(), msg = await page.locator("#toast").boundingBox();
  assert.ok(msg.y >= list.y + list.height, "the message covers the search results");
  // the list grows to half the screen with more results, so the message waits at the bottom
  assert.ok(msg.y > (await page.evaluate(() => innerHeight)) * 0.6, "the message sits where a longer list would cover it");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("SOS opened again while the last alert is still on screen counts down again", async () => {
  const { ctx, page, errors } = await phone([{ name: "Mum", phone: "+91 98765 43210" }]);
  await page.goto(BASE + "/app/");
  await page.focus("#sosBtn");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("#sosSecs"), null, { timeout: 10000 });  // the alert replaced the countdown
  await page.evaluate(() => openSOS());  // as a missed check-in does
  await page.waitForFunction(() => document.querySelector("#sosCount").textContent === "4", null, { timeout: 3000 });
  await page.keyboard.press("Escape");
  assert.ok(await page.isHidden("#sosOverlay"));
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("the friend's viewer rides out a dropped connection instead of saying the trip ended", async () => {
  const post = (path, body) => fetch(BASE + path, { method: "POST", body: JSON.stringify(body) }).then((r) => r.json());
  const t = await post("/api/trip", { dest: [28.0007, 77.0025], eta: 5, name: "Asha" });
  await post(`/api/trip/${t.id}`, { key: t.key, lat: 28.0, lon: 77.0 });
  const { ctx, page } = await phone();
  await page.clock.install();
  let down = true;
  await page.route("**/api/trip/*", (r) => (down ? r.abort("internetdisconnected") : r.continue()));
  await page.goto(`${BASE}/t/?id=${t.id}`);
  await page.waitForSelector("text=Can't reach SafeRoute");
  assert.doesNotMatch(await page.textContent(".card"), /ended/);
  down = false;
  await page.clock.runFor(11000);
  await page.waitForSelector("text=Asha is walking");
  await ctx.close();
});

test("friends see she arrived, not only that the trip ended", async () => {
  const { ctx, page, errors } = await phone();
  await page.addInitScript(() => { navigator.share = async () => {}; });
  await routeToPharmacy(page);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  const viewer = await ctx.newPage();
  await viewer.goto(`${BASE}/t/?id=${await page.evaluate(() => state.trip.id)}`);
  await viewer.waitForSelector("text=is walking");
  await page.click("#safeBtn");
  await viewer.reload();
  await viewer.waitForSelector("text=arrived safely");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("arriving with no signal still ends the trip for friends once the phone reconnects", async () => {
  const { ctx, page, errors } = await phone();
  await page.addInitScript(() => { navigator.share = async () => {}; });
  await routeToPharmacy(page);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  const id = await page.evaluate(() => state.trip.id);
  await page.clock.install();
  let down = true, lost = 0;
  await page.route("**/api/trip/*", (r) => (down ? (lost++, r.abort("internetdisconnected")) : r.continue()));
  await page.click("#safeBtn");
  await page.waitForSelector("text=Glad you're safe");
  assert.ok(lost > 0, "the end of the trip was sent while offline");
  down = false;
  // the failed send's retry timer is set once its rejection arrives, which can be after any single clock jump, so step the clock
  let view;
  for (let i = 0; i < 20 && !view?.ended; i++) {
    await page.clock.runFor(10000);
    await new Promise((r) => setTimeout(r, 150));
    view = await (await fetch(`${BASE}/api/trip/${id}`)).json();
  }
  assert.ok(view.ended, "friends no longer see her as on the way");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("a moment without GPS doesn't stop the live location for the rest of the trip", async () => {
  const { ctx, page, errors } = await phone();
  await page.addInitScript(() => {
    navigator.share = async () => {};
    const watches = (window.watches = {});
    let n = 0;
    const at = (lat, lon) => ({ coords: { latitude: lat, longitude: lon, accuracy: 5 }, timestamp: Date.now() });
    Object.defineProperty(navigator, "geolocation", { value: {
      getCurrentPosition: (ok) => setTimeout(() => ok(at(28.0, 77.0)), 10),
      watchPosition: (ok, err) => { watches[++n] = { ok, err }; setTimeout(() => ok(at(28.0, 77.0)), 10); return n; },
      clearWatch: (id) => delete watches[id],
    } });
  });
  await routeToPharmacy(page);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  // in an underpass the phone reports a timeout; the watch must keep going for when the signal is back
  await page.evaluate(() => Object.values(watches).forEach((w) => w.err({ code: 3, TIMEOUT: 3, PERMISSION_DENIED: 1 })));
  await page.evaluate(() => Object.values(watches).forEach((w) => w.ok({ coords: { latitude: 28.0002, longitude: 77.0006 } })));
  assert.deepEqual(await page.evaluate(() => state.here), [28.0002, 77.0006]);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("coming back to SafeRoute mid-trip keeps the screen on again", async () => {
  const { ctx, page, errors } = await phone();
  await page.addInitScript(() => {
    navigator.share = async () => {};
    window.locks = 0;
    Object.defineProperty(navigator, "wakeLock", { value: { request: async () => { window.locks++; return { release: async () => {} }; } } });
  });
  await routeToPharmacy(page);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  assert.equal(await page.evaluate(() => locks), 1);
  // the browser drops the lock whenever she switches to WhatsApp to send the link, and the phone then sleeps
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForFunction(() => locks === 2);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("sharing the route first, then starting, keeps friends on the same live link", async () => {
  const { ctx, page, errors } = await phone();
  await page.addInitScript(() => { window.shares = []; navigator.share = async (d) => { shares.push(d.url); }; });
  await routeToPharmacy(page);
  await page.click("#shareRouteBtn");
  await page.waitForFunction(() => shares.length === 1);
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  const [link] = await page.evaluate(() => shares);
  assert.equal(link, await page.evaluate(() => tripLink(state.trip.id)), "the link already sent is the one that moves");
  assert.deepEqual(errors, []);
  await ctx.close();
});
