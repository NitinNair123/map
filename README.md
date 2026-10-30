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

For Delhi NCR, run `pip install osmium` and `python import_osm.py` once first. All OpenStreetMap detail is
then on disk and trips load instantly (see Data sources). Everywhere else it works on demand: when someone plans a walk, the server fetches the streets around it, scores them and
routes. With `pip install pyarrow` the streets come from Overture Maps (OSM plus TomTom, about 15 s for a new
area). OSM detail such as lamps, `lit` tags and opening hours then loads from Overpass in the background
and is used from the next trip. Without pyarrow, everything comes from Overpass, which can take minutes. It fetches the area as about 1 km tiles along the trip, in one or two
requests, and caches them in `data/tiles/` for 7 days (Overture and Mapillary 30 days; Mapillary loads in the background). It tries three Overpass mirrors in turn, so one busy
server doesn't break a trip. Place search uses the offline index first, then Photon (komoot), and trips are capped at 50 km.

Run the tests with `python -m unittest test_safety test_server` (scoring, routing and every API endpoint, offline).
The browser tests drive the app on a phone-sized screen against `fixture_server.py`:
`npm install --no-save playwright && npx playwright install chromium && node --test browser.test.cjs`.
GitHub Actions runs both on every pull request (`.github/workflows/test.yml`).

## Checking the scores against real audits

The scores are only as good as their match with what people find on the street. To check them, walk a set of
spots at night with a safety audit checklist (Safetipin's or Jagori's), without looking at SafeRoute first.
Rate each spot from 1 (unsafe) to 5 (safe) and note whether the lights were on. Fill in `data/audits.csv` in the
format of `audits_template.csv` (`lat,lon,when,safety,lit`), then run `python validate.py data/audits.csv`.
It prints how closely the scores follow the auditors' ratings (Spearman rank correlation, from at least 30 spots)
and how often the lighting matches. It also writes every spot with its score to `data/audits_scored.csv`.
In the app, "No data" means nothing is mapped there, which is not the same as a bad score. "Why this score?" lists
each data source and when it was downloaded.

To install it on a phone, the site must be served over **HTTPS** (browsers require that for location and
install). For a demo, run `npx localtunnel --port 8000` or `cloudflared tunnel --url http://localhost:8000`
and open the https link on the phone. Then use "Add to Home Screen".

### Data sources (all free)

- **OpenStreetMap, imported once** (recommended). `pip install osmium`, then `python import_osm.py`. It downloads
  the Geofabrik extracts that cover Delhi NCR (a few hundred MB, once) and writes every street, footpath, lamp,
  `lit` tag, shop, opening hour, police station and camera into `data/tiles/`, with road surface, lanes, speed
  limits, pedestrian crossings, guard posts, emergency phones and fire stations, plus a count of building
  outlines along every street. It also builds `data/tiles/search.db`, so place search works offline. Trips there
  then load instantly and never wait for the slow Overpass servers. The server re-imports on its own in the
  background when the import is a month old (`python import_osm.py --refresh` does the same by hand). For another
  region, pass a `.osm.pbf` file or URL and `--bbox south,west,north,east`.
- **Overture Maps** (`pip install pyarrow`): streets outside the imported area, about 88 million places and
  building footprints. Places marked closed, with low confidence, or stacked on a locality centre are dropped.
- **NASA night lights** (no key): VIIRS Black Marble brightness from 2016, about 500 m per pixel, from NASA GIBS.
  It describes the neighbourhood, not the lane, so it is the weakest lighting evidence.
- **Mapillary** (free token): street lights and CCTV cameras seen in street-level photos. Put the token in
  `data/mapillary_token.txt` (gitignored) or the `MAPILLARY_TOKEN` environment variable.
- **Bus and metro timetables**: put any GTFS zip in `data/gtfs/`, for example Delhi's Open Transit Data
  (https://otd.delhi.gov.in, free registration) or the DMRC metro feed. The app then knows whether a stop is
  actually served at the hour you walk.
- **Live buses** (the same free Open Transit Data key, in `data/otd_key.txt` or `OTD_KEY`): bus positions every
  10 s. Each street counts the different buses that drove past in the last 30 minutes; parked buses don't count,
  and a street with no bus is not marked empty (most aren't on a bus route). Counts start 30 minutes after the
  server starts and only apply to trips leaving now.
- **Live traffic** (free TomTom key from https://developer.tomtom.com, in `data/tomtom_key.txt` or `TOMTOM_KEY`):
  up to 6 main roads near each route are looked up (cached 10 minutes, at most 2,000 calls a day). Slow, heavy
  traffic counts as a weak sign the road isn't deserted; free-flowing traffic counts as nothing.
- **Gemini** (key from https://aistudio.google.com, in `data/gemini_key.txt` or `GEMINI_KEY`; it stays on the
  server): for each trip, named shops within 300 m of it that publish no opening hours are looked up in one
  Google-Search-grounded call (at most 15 places), and the answers are kept in `data/gemini_hours.json` for 30 days.
  A second call per area and hour looks for VIP movement, barricades, closures and protests. Each alert must name a
  street or landmark that place search finds near the trip, or it is dropped. Alerts plug in through `safety.SIGNALS`:
  ones within 400 m take 20 points off those streets around that time, wider ones only show a warning. At most 300 calls a day.
- **SOS ambulance alert**: "Ambulance alert with my medical info" on the SOS screen. With the Gemini key above, the
  text is reworded so allergies and conditions come first (no search, no thinking, 4 s timeout). It only rewords;
  the user still presses Send. With no key, a slow or failed call, or a reply that drops a location link, the plain
  text is used. The SOS screen also has Call ambulance 108 and a Blinkit ambulance link (Blinkit has no public API;
  its ambulance runs in parts of Gurugram and is booked in the Blinkit app).
- **Busy / empty taps**: while navigating, one tap reports "people around", "deserted", "shops open" or
  "shops shut" at your spot. The route card shows how many people said so at this time of day.
- **Street survey**: `data/survey.csv` with columns `lat,lon,type,when`, one row per thing you checked on a
  walk, e.g. `28.4741,77.4998,lights_on,2026-10-27T21:30`. Types: `lights_on, broken_light, lights_off,
  shops_open, shops_closed, people_around, deserted, felt_unsafe, harassment, felt_safe`. `survey_template.csv`
  has the header. A survey of the campus streets after dark gives them high confidence from day one.
- **Reports from the app** use the same types plus `no_footpath, dogs, flooded`, and are saved in `data/reports.db`
  (an old `reports.json` is moved in once). Only a salted hash of the sender's address is kept (the salt is in
  `data/report_salt.txt`), for the limit of 10 reports an hour and so each person counts once per street per type.
- **Official lists** in `data/layers/`, each a CSV with `lat,lon` and an optional `name`, for example from an RTI
  reply or a city open-data portal: `streetlights.csv`, `cctv.csv`, `police.csv`, `hospitals.csv`, `blackspots.csv`
  (road accident blackspots from the traffic police) and `waterlogging.csv` (streets that flood). Files are read
  again as soon as they change; no restart needed.
- `data/incidents.csv` (`lat,lon,type,when`) holds incident history, for example a campus security log. Types:
  `assault, robbery, harassment, stalking, snatching, theft, other`.
- Live rain from Open-Meteo (no key).
- **Geoapify places** (free key from https://myprojects.geoapify.com, 3,000 requests a day): shops, food, pharmacies,
  hospitals, petrol pumps, police and transit with opening hours. Put the key in `data/geoapify_key.txt` (gitignored)
  or the `GEOAPIFY_KEY` environment variable. Places Geoapify took from OpenStreetMap keep their OSM id, so they
  merge with the OSM copy instead of counting twice; it mostly helps outside the imported area.
- **The map itself** is OpenFreeMap's vector style (free, no key) drawn by MapLibre, with plain OpenStreetMap
  tiles as the fallback when a phone has no WebGL or the style can't load.
- **Past crime, as background** (`python crime.py`, once a year): NCRB's district tables of crimes against women
  (India Data Portal mirror) and the district outlines from OpenStreetMap, saved to `data/crime/districts.json`.
  The route card then shows the district's police-recorded cases of assault on women (IPC 354) and insulting a
  woman's modesty (IPC 509) for the latest year. It never changes a score: the figures are yearly, district-wide,
  only count reported cases and say nothing about one street tonight. In Delhi they are per Delhi Police district.

## How the score works

The rating lives in `rating.py`. Every street segment gets five factors. Each factor uses only measured data,
and each piece of evidence has a quality: 1.0 for someone checking the street, 0.9 for a timetable, 0.8 for
mapped tags, hours, buildings and police, 0.7 for lamps, 0.5 for "nothing mapped here", 0.35 for being a main road
and 0.3 for satellite night lights. Survey and report data older than 180 days counts for less.

| Factor | Day | Dark | Evidence, best first |
|---|---|---|---|
| Lighting | 0 | 30 | survey and reports (only those made after dark), OSM `lit` tag, lamps (OSM, and Mapillary objects seen in a photo in the last 5 years), NASA night lights |
| People and open places | 30 | 30 | survey and reports at this hour, published opening hours, buses and trains due this hour, nothing mapped within 60 m when shops are mapped within 500 m (with none within 500 m the block counts as unmapped, not empty), main road (weak: traffic passes, but that alone doesn't make a street safe) |
| Buildings and surroundings | 20 | 15 | building footprints around the street; parks, building sites, rail lines and scrub next to it |
| Police and CCTV | 10 | 10 | police station within 400/800 m, cameras within 50 m, guard posts, emergency phones |
| Footpath and ways out | 40 | 15 | "no footpath" reports, road type, footpaths, rough or unpaved surface, four lanes or 50 km/h without a footpath (crossings help), accident blackspots, underpasses, foot overbridges at night, dead ends, long stretches without side streets |

Weights blend from day to night with the real sun position, so dusk counts as dusk.

- **Score** (0–100) is the average of the factors that have data, weighted by factor weight and evidence
  quality. A factor with no data is left out. Nothing is guessed or copied from a nearby street.
- **Confidence** (0–1) is how much of the full weight is measured: `Σ weight × quality / Σ weight`.
  High is 0.7 or more, medium 0.5, low 0.35. Below 0.35 a street is **Not rated** and shows grey.
- Bars after dark, harassment, felt-unsafe, stray-dog and flooding reports, incidents (by severity, halving every
  180 days, stronger at the same hour), rain, and listed waterlogging streets when it rains lower the score. They
  never raise confidence.
- Not used, on purpose: "typical opening hours" by shop type, guessed lighting or footfall by road type, and
  pulling unknown streets towards an average score. Those are guesses, so a street without data stays unrated.
  No data about who lives in an area (income, religion, caste) is used.
- **Routes.** SafeRoute finds many different ways, scores every one in full and ranks them. Candidates come
  from searches over the whole network with `cost = length × (1 + λ·(1 − credit/100))` for λ = 0, 1, 3, 6,
  10 and 20, plus detours found by making the streets already used costlier and searching again. A street's
  credit is its score scaled by confidence (full at 0.7), and zero when it is not rated. Candidates over 1.8×
  the shortest, or sharing 90% of their length with one already found, are dropped. A route's score is 60% the
  average of its rated streets plus 40% its weakest one, minus penalties for isolated stretches, long gaps
  with nowhere open and detours over 1.3× the fastest route. Safest is the top score, Balanced the top score
  at most 25% longer than the fastest, and Fastest the shortest. Each route also reports how
  much of it is unrated, which sources were used, and how well each factor was measured.
- **Timed to when you get there.** Each street is rated for the 5 minutes in which the trip reaches it, so a
  shop that shuts before you pass it counts as closed, and a long walk crosses into night as it really does.
- **Walk, bike or car.** Bikes skip steps and footpaths (unless signed for bikes) and keep to one-way rules
  (unless `oneway:bicycle=no`); cars keep to roads and one-way streets. Times assume 1.3 m/s on foot, 15 km/h
  by bike and 25 km/h by car in city traffic. The same evidence is weighed differently: day / dark weights are
  bike lighting 0/30, people 30/25, buildings 20/15, police 10/10, footpath 40/20; car lighting 0/15, people
  45/40, buildings 20/15, police 25/20, footpath 10/10 (in a car, help points that are open matter most).
- **Stops.** Up to 5 stops, visited in order. Each leg is searched on its own and the k-th way of every leg is joined into
  the k-th candidate, so the whole trip is scored and ranked like a trip without stops.
- **Your priorities.** Settings let you ask for more medical stores, hospitals, places to eat, shops, petrol pumps or
  bus and metro stops on the way (0–3 each, kept on the phone). Every open one of those passed adds 1.5 points × its
  weight, at most 3 counted per kind and at most +10 in all, so safety still decides. The card says what was counted.
- **Plug-in signals.** Other features add evidence without touching the rating: append `fn(pt, when)` to
  `safety.SIGNALS`, returning `None` or `{"delta": points, "warning": text}` (or `"reason"`). `delta` is added to
  that street's score for the moment the trip gets there (0 for awareness only), and the text shows on the route
  card with how much of the route it covers. A signal that raises an error is skipped. Physical and situational
  conditions only, never who lives somewhere.
- **Detour limit.** Settings can cap candidates at 5, 10, 15 or 20 minutes longer than the fastest way.
- **A closer look.** Every route lists the medical stores, hospitals, petrol pumps, shops, stops and cameras
  along it, how many are open as you pass, the longest stretch without an open help point (medical store,
  hospital or petrol pump open then, or police within 150 m) and the longest stretch with nothing open. A strip
  under the routes draws each one start to end with its open places and its long empty or unlit stretches.
- With the safety map on, tap any street to see its score, confidence and the data behind each factor.

No rating is 100% accurate. Scores compare routes and mean *safer*, never *safe*.

## App features

- Live navigation: next-turn banner, spoken directions, map follows you, automatic re-route when you go off route, and a Simulate walk demo.
- **Live re-scoring.** Every 3 minutes while navigating, the way ahead is scored again for the current time (shops shut,
  night falls, new reports, live buses and traffic, plug-in signals). If another way from where you are is now 10 or more
  points safer than the rest of your route, the app switches to it, says so and vibrates.
- Stops between start and destination (the + button), announced as you reach each one.
- Place search anywhere, biased towards where you're looking, or a pin dropped on the map.
- A departure time picker, because a 1 am walk scores differently from a 7 pm one.
- A safety map overlay for the chosen hour.
- **Live trip.** Friends follow you on a map link. The link ends on arrival, and live trips expire after 12 hours.
- **Check-ins.** If you leave the route, stop for 4 minutes or run late, the app asks "Are you OK?". No answer in 60 s starts SOS.
- **Hold-to-SOS** (1.5 s). It texts your trusted contacts a live link, puts a one-tap call to 112 on screen, lists the nearest open places, and offers a loud siren and a fake call.
- A safe-place card while walking, showing the nearest place that's open now.
- Anonymous reports (lit or dark, shops open or closed, people around or deserted, felt unsafe, harassment, felt safe, no footpath, stray dogs, flooded), rate-limited to 10 per hour, one vote per person per street.
- Onboarding, trusted contacts and settings stored only on the phone, and light and dark themes.
- Installable, with offline app shell, home-screen icon and an SOS shortcut.

## Files

- `rating.py` is the rating: factors, evidence quality, confidence and route credit.
- `safety.py` has the street graph, sun position, opening-hours parser and routing.
- `tiles.py` fetches map tiles on demand (Overpass, Overture, Mapillary, NASA night lights) and caches them.
- `import_osm.py` imports OpenStreetMap for a whole region and builds the offline search index. `gtfs.py` reads bus
  and metro timetables. `reports.py` keeps the app's reports. `crime.py` downloads and looks up the district crime figures.
- `server.py` is the web server and JSON API: `/api/route`, `/api/heat`, `/api/explain`, `/api/search`, `/api/havens`, `/api/report`, `/api/trip`.
- `static/index.html` is the website.
- `static/app/` is the app (`index.html`, `app.css`, `app.js`).
- `static/t/` is the live trip viewer.
- `static/theme.css` holds the shared design tokens.
- `static/manifest.webmanifest`, `static/sw.js` and `static/icons/` make up the installable app.

## Before selling it

- Set your contact email in `static/index.html` (search for `hello@example.com`).
- Host it behind HTTPS: see [DEPLOY.md](DEPLOY.md) for an Ubuntu VPS with Caddy. The server only answers phones on your own
  network, local tunnels and a proxy on the same machine, so leave `SAFEROUTE_PUBLIC` off behind Caddy; set
  `SAFEROUTE_PUBLIC=1` only when a proxy on another machine forwards to it. Live trips are kept in memory, so use a single server process or move them to Redis.
- Add a privacy policy and terms page.
- For app-store listings, wrap `/app/` with Capacitor.
