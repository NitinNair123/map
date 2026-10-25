# SafeRoute

Walking directions that pick the **safest** route, not just the fastest. Every street gets a 0–100 safety
score for the exact time you're walking, and you choose between Safest, Balanced and Fastest routes, each
with plain reasons behind its score. It's built for walking home at night, especially for women.

It has three parts, all served by one small Python server:

- **Website** (`/`): the landing page, with how it works, what we check, safety tools, privacy and a pitch for campuses.
- **App** (`/app/`): an installable phone app (PWA) that also works on desktop.
- **Live trip viewer** (`/t/?id=…`): the page friends open to follow your walk. They don't need the app.

## Run it

Needs Python 3.10+ and an internet connection, and nothing else.

```sh
python server.py          # then open http://localhost:8000   (on Windows: py server.py)
```

It works anywhere. When someone plans a walk, the server fetches the streets around it from
OpenStreetMap, scores them and routes. It fetches the area as about 1 km tiles along the trip, in one or two
requests, and caches them in `data/tiles/` for 7 days. It tries three Overpass mirrors in turn, so one busy
server doesn't break a trip. Place search uses Photon (komoot), and walks are capped at 10 km.

Run the tests with `python -m unittest test_safety`.

To install it on a phone, the site must be served over **HTTPS** (browsers require that for location and
install). For a demo, run `npx localtunnel --port 8000` or `cloudflared tunnel --url http://localhost:8000`
and open the https link on the phone. Then use "Add to Home Screen".

### Optional data

- **Mapillary** (free): street lights and CCTV cameras detected in street-level photos. Get a client token at
  https://www.mapillary.com/dashboard/developers and start the server with it, for example
  `MAPILLARY_TOKEN='MLY|…' python server.py`, or in PowerShell `$env:MAPILLARY_TOKEN="MLY|…"; py server.py`.

- `data/incidents.csv` holds incident history from police open data, news or a campus security log.
  The columns are `lat,lon,type,when`, for example `28.61,77.21,harassment,2026-09-14T22:30`.
  The types are `assault, robbery, harassment, stalking, snatching, theft, other`.
- `data/reports.json` is written by the app when people send reports. Delete it to reset them.

## How the score works

Each road segment is scored from what's near it at the chosen time. Weights blend smoothly from day to
night using the real sun position for your area, so dusk counts as dusk.

| Factor | Day | Dark | What it uses |
|---|---|---|---|
| Lighting | 5 | 25 | `lit=*` tags, street lamps within 20 m, broken-light reports; untagged streets get a guess from road type, built-up land and lit shopfronts |
| Open places | 25 | 25 | shops, cafes, offices, schools, temples, clinics, pharmacies, petrol pumps **open at that time** (OSM `opening_hours`), plus housing and market areas |
| Police & CCTV | 10 | 12 | police station within 400/800 m, cameras within 50 m |
| Footpath & road | 25 | 13 | road type, sidewalks, underpasses (bad), foot overbridges (bad at night) |
| Surroundings | 15 | 15 | parks, construction sites, rail lines, scrub, cemeteries and industrial land next to the road |
| Ways out | 10 | 5 | dead ends and long stretches without side streets |
| Transport | 10 | 5 | bus stops, stations and taxi stands within 150 m |

On top of that:

- Bars and liquor shops subtract points after dark.
- Community reports (felt unsafe, harassment, felt safe, broken light) count most when recent and halve every 30 days.
- Incidents lose up to 35 points. They weigh by severity, halve every 180 days, and count more at the same time of day.
- Live rain from Open-Meteo (free, no key) subtracts 5 points when you're leaving now.

**Routes.** The search runs over the whole street network with `cost = length × (1 + λ·(1 − score/100))`,
where λ is 10 for Safest, 3 for Balanced and 0 for Fastest. A route's score is 60% its average plus 40%
its weakest segment, minus penalties for:

- isolated stretches,
- more than 300 m with nowhere open to duck into,
- detours longer than 1.3× the fastest route.

Every route comes with reasons, warnings, a factor breakdown, the weakest stretch and why it's weak, and a
low-data flag when the streets are poorly mapped.

## App features

- Live navigation: next-turn banner, spoken directions, map follows you, automatic re-route when you go off route, and a Simulate walk demo.
- Place search anywhere, biased towards where you're looking, or a pin dropped on the map.
- A departure time picker, because a 1 am walk scores differently from a 7 pm one.
- A safety map overlay for the chosen hour.
- **Live trip.** Friends follow you on a map link. The link ends on arrival, and live trips expire after 12 hours.
- **Check-ins.** If you leave the route, stop for 4 minutes or run late, the app asks "Are you OK?". No answer in 60 s starts SOS.
- **Hold-to-SOS** (1.5 s). It texts your trusted contacts a live link, puts a one-tap call to 112 on screen, lists the nearest open places, and offers a loud siren and a fake call.
- A safe-place card while walking, showing the nearest place that's open now.
- Anonymous reports, rate-limited to 10 per hour per device.
- Onboarding, trusted contacts and settings stored only on the phone, and light and dark themes.
- Installable, with offline app shell, home-screen icon and an SOS shortcut.

## Files

- `tiles.py` fetches OpenStreetMap tiles on demand (with mirror fallback), caches them, and builds scored areas.
- `safety.py` has the scoring, sun position, opening-hours parser, street graph and routing.
- `server.py` is the web server and JSON API: `/api/route`, `/api/heat`, `/api/search`, `/api/havens`, `/api/report`, `/api/trip`.
- `static/index.html` is the website.
- `static/app/` is the app (`index.html`, `app.css`, `app.js`).
- `static/t/` is the live trip viewer.
- `static/theme.css` holds the shared design tokens.
- `static/manifest.webmanifest`, `static/sw.js` and `static/icons/` make up the installable app.

## Before selling it

- Set your contact email in `static/index.html` (search for `hello@example.com`).
- Host it behind HTTPS. Live trips are kept in memory, so use a single server process or move them to Redis.
- Add a privacy policy and terms page.
- For app-store listings, wrap `/app/` with Capacitor.
