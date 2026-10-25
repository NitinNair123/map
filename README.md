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
requests, and caches them in `data/tiles/` for 7 days. It tries three Overpass mirrors in turn, so one busy
server doesn't break a trip. Place search uses Photon (komoot), and walks are capped at 10 km.

Run the tests with `python -m unittest test_safety`.

To install it on a phone, the site must be served over **HTTPS** (browsers require that for location and
install). For a demo, run `npx localtunnel --port 8000` or `cloudflared tunnel --url http://localhost:8000`
and open the https link on the phone. Then use "Add to Home Screen".

### Data sources (all free)

- **OpenStreetMap, imported once** (recommended). `pip install osmium`, then `python import_osm.py`. It downloads
  the Geofabrik extracts that cover Delhi NCR (a few hundred MB, once) and writes every street, footpath, lamp,
  `lit` tag, shop, opening hour, police station and camera into `data/tiles/`. Trips there then load instantly
  and never wait for the slow Overpass servers. Re-run it monthly. For another region, pass a `.osm.pbf` file or
  URL and `--bbox south,west,north,east`.
- **Overture Maps** (`pip install pyarrow`): streets outside the imported area, about 88 million places and
  building footprints. Places marked closed, with low confidence, or stacked on a locality centre are dropped.
- **NASA night lights** (no key): VIIRS Black Marble brightness from 2016, about 500 m per pixel, from NASA GIBS.
  It describes the neighbourhood, not the lane, so it is the weakest lighting evidence.
- **Mapillary** (free token): street lights and CCTV cameras seen in street-level photos. Put the token in
  `data/mapillary_token.txt` (gitignored) or the `MAPILLARY_TOKEN` environment variable.
- **Bus and metro timetables**: put any GTFS zip in `data/gtfs/`, for example Delhi's Open Transit Data
  (https://otd.delhi.gov.in, free registration) or the DMRC metro feed. The app then knows whether a stop is
  actually served at the hour you walk.
- **Street survey**: `data/survey.csv` with columns `lat,lon,type,when`, one row per thing you checked on a
  walk, e.g. `28.4741,77.4998,lights_on,2026-10-27T21:30`. Types: `lights_on, broken_light, lights_off,
  shops_open, shops_closed, people_around, deserted, felt_unsafe, harassment, felt_safe`. `survey_template.csv`
  has the header. A survey of the campus streets after dark gives them high confidence from day one.
- **Reports from the app** use the same types and are saved to `data/reports.json`. Delete it to reset them.
- `data/incidents.csv` (`lat,lon,type,when`) holds incident history, for example a campus security log. Types:
  `assault, robbery, harassment, stalking, snatching, theft, other`.
- Live rain from Open-Meteo (no key).

## How the score works

The rating lives in `rating.py`. Every street segment gets five factors. Each factor uses only measured data,
and each piece of evidence has a quality: 1.0 for someone checking the street, 0.9 for a timetable, 0.8 for
mapped tags, hours, buildings and police, 0.7 for lamps, 0.5 for "nothing mapped here" and 0.3 for satellite
night lights. Survey and report data older than 180 days counts for less.

| Factor | Day | Dark | Evidence, best first |
|---|---|---|---|
| Lighting | 0 | 30 | survey and reports, OSM `lit` tag, lamps (OSM, Mapillary), NASA night lights |
| People and open places | 30 | 30 | survey and reports at this hour, published opening hours, buses and trains due this hour, nothing mapped nearby |
| Buildings and surroundings | 20 | 15 | building footprints around the street; parks, building sites, rail lines and scrub next to it |
| Police and CCTV | 10 | 10 | police station within 400/800 m, cameras within 50 m |
| Footpath and ways out | 40 | 15 | road type, footpaths, underpasses, foot overbridges at night, dead ends, long stretches without side streets |

Weights blend from day to night with the real sun position, so dusk counts as dusk.

- **Score** (0–100) is the average of the factors that have data, weighted by factor weight and evidence
  quality. A factor with no data is left out. Nothing is guessed or copied from a nearby street.
- **Confidence** (0–1) is how much of the full weight is measured: `Σ weight × quality / Σ weight`.
  High is 0.7 or more, medium 0.5, low 0.35. Below 0.35 a street is **Not rated** and shows grey.
- Bars after dark, harassment and felt-unsafe reports, incidents (by severity, halving every 180 days,
  stronger at the same hour) and rain lower the score. They never raise confidence.
- **Routes.** The search runs over the whole network with `cost = length × (1 + λ·(1 − credit/100))`, where
  λ is 10 for Safest, 3 for Balanced and 0 for Fastest. A street's credit is its score scaled by confidence
  (full at 0.7), and zero when it is not rated, so the safest route prefers streets known to be good. A route's
  score is 60% the average of its rated streets plus 40% its weakest one, minus penalties for isolated
  stretches, long gaps with nowhere open and detours over 1.3× the fastest route. Each route also reports how
  much of it is unrated, which sources were used, and how well each factor was measured.
- With the safety map on, tap any street to see its score, confidence and the data behind each factor.

No rating is 100% accurate. Scores compare routes and mean *safer*, never *safe*.

## App features

- Live navigation: next-turn banner, spoken directions, map follows you, automatic re-route when you go off route, and a Simulate walk demo.
- Place search anywhere, biased towards where you're looking, or a pin dropped on the map.
- A departure time picker, because a 1 am walk scores differently from a 7 pm one.
- A safety map overlay for the chosen hour.
- **Live trip.** Friends follow you on a map link. The link ends on arrival, and live trips expire after 12 hours.
- **Check-ins.** If you leave the route, stop for 4 minutes or run late, the app asks "Are you OK?". No answer in 60 s starts SOS.
- **Hold-to-SOS** (1.5 s). It texts your trusted contacts a live link, puts a one-tap call to 112 on screen, lists the nearest open places, and offers a loud siren and a fake call.
- A safe-place card while walking, showing the nearest place that's open now.
- Anonymous reports (lit or dark, shops open or closed, people around or deserted, felt unsafe, harassment, felt safe), rate-limited to 10 per hour per device.
- Onboarding, trusted contacts and settings stored only on the phone, and light and dark themes.
- Installable, with offline app shell, home-screen icon and an SOS shortcut.

## Files

- `rating.py` is the rating: factors, evidence quality, confidence and route credit.
- `safety.py` has the street graph, sun position, opening-hours parser and routing.
- `tiles.py` fetches map tiles on demand (Overpass, Overture, Mapillary, NASA night lights) and caches them.
- `import_osm.py` imports OpenStreetMap for a whole region once. `gtfs.py` reads bus and metro timetables.
- `server.py` is the web server and JSON API: `/api/route`, `/api/heat`, `/api/explain`, `/api/search`, `/api/havens`, `/api/report`, `/api/trip`.
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
