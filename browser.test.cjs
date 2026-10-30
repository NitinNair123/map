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
