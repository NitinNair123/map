// The app in a phone-sized Chromium against fixture_server.py.
// Usage: npm install --no-save playwright leaflet@1.9.4 && npx playwright install chromium && node --test browser.test.cjs
const { spawn } = require("node:child_process");
const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const { chromium, devices } = require("playwright");
const path = require("node:path");
const LEAFLET = path.dirname(require.resolve("leaflet/package.json"));

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
  // fully offline: Leaflet from the npm package, no fonts or map pictures
  await ctx.route("https://unpkg.com/leaflet@1.9.4/**", (r) => r.fulfill({ path: path.join(LEAFLET, new URL(r.request().url()).pathname.split("leaflet@1.9.4/")[1]) }));
  await ctx.route(/^https:\/\/(?!unpkg)/, (r) => r.abort());
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
  await page.click("#startBtn");
  await page.waitForSelector("text=Sharing live");
  assert.ok(await page.isVisible("text=Keep SafeRoute open with the screen on"));
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

test("friend's viewer follows a live trip and shows when it ends", async () => {
  const post = (path, body) => fetch(BASE + path, { method: "POST", body: JSON.stringify(body) }).then((r) => r.json());
  const t = await post("/api/trip", { dest: [28.0007, 77.0025], eta: 5, name: "Asha" });
  await post(`/api/trip/${t.id}`, { key: t.key, lat: 28.0, lon: 77.0 });
  const { ctx, page } = await phone();
  await page.goto(`${BASE}/t/?id=${t.id}`);
  await page.waitForSelector("text=Asha is walking");
  await post(`/api/trip/${t.id}`, { key: t.key, ended: true });
  await page.reload();
  await page.waitForSelector("text=Asha ended the trip");
  await ctx.close();
});
