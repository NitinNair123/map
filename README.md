# SafeRoute

Walking directions that pick the **safest** route, not the fastest, for the time you plan to leave.

```
npm start      # serves the app at http://localhost:3000 (any static server works)
npm test       # scoring unit tests (Node 18+)
```

No build step, no backend, no API keys. Uses free OpenStreetMap services: Nominatim (search),
routing.openstreetmap.de (walking routes), Overpass (map features). Their usage limits apply,
so self-host them before a real launch.

## How a route is scored (`score.js`)

The route is sampled every 25 m. Each point gets 0–100, with different weights for day and night (night = 19:00–06:00):

| Factor | Day | Night | OSM source |
|---|---|---|---|
| Lighting (lamp ≤30 m, or road `lit=yes/no`) | 0 | 40% | `highway=street_lamp`, `lit` |
| Places **open at that time** ≤60 m | 45% | 35% | `shop`, `amenity` + `opening_hours` |
| Police ≤400 m / CCTV ≤50 m | 30% | 15% | `amenity=police`, `man_made=surveillance` |
| Transit stop ≤150 m | 25% | 10% | `highway=bus_stop`, stations |
| Isolated land, parks at night, tunnels | −25 | −25 | `landuse=construction/brownfield/...`, `leisure=park`, `tunnel` |
| Bars / liquor shops open (night) | – | −15 | `amenity=bar/pub/nightclub`, `shop=alcohol` |
| Informal settlement (toggle, on by default) | −30 | −30 | `residential=informal`, `informal=yes` |

Route score = 0.6 × average + 0.4 × worst 100 m stretch − a penalty for detours over 1.3× the shortest route.
Each **priority** you set (0–3 for pharmacies, hospitals, ATMs, food, police, transit stops) adds up to 4.5 points per category for matching places passed, capped at +15 total so safety still leads.
Every card explains its score ("12 places open", "200 m on unlit road") and is marked "low data" when OSM coverage is too thin to trust.

## Features
- Safest / Balanced / Fastest options, with the route coloured green, amber or red along its length
- Pick a departure time, so open-shop and lighting checks match that time
- Add stops; every alternative goes through them in order
- Personal priorities, saved on the device
- **Live trip**: follows your location and re-scores the rest of each route every minute; if another route becomes 10+ points safer (shops closed, night fell, a plug-in signal changed) it switches and vibrates
- Open places and police stations marked as safe havens on the map
- **Share my trip**, plus an **SOS** button that shares your location via the share sheet or SMS and offers to call 112

## Not built yet
Auto check-in and community reports need a backend.

## Plugging in new signals
Other data (event closures, crime awareness, …) plugs in without touching the scorer:

```js
import { addSignal } from './score.js';
const off = addSignal(([lat, lon], { date, night }) =>
  inBarrierZone(lat, lon) ? { delta: -60, warning: 'VIP movement barrier' } : null);
```

`delta` is added to that 25 m sample's score (use `0` for awareness-only), `reason` / `warning` become chips with the metres affected.
The live trip picks up new signals at its next one-minute check. Signals must describe physical or situational conditions only, never demographics, income or area labels.
