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
Every card explains its score ("12 places open", "200 m on unlit road") and is marked "low data" when OSM coverage is too thin to trust.

## Optional: Gemini (`gemini.js`)
Copy `config.example.js` to `config.js` (gitignored) and add a Gemini API key. With it, each search also:
- **Fills missing shop hours**: named shops/amenities within 60 m of the routes that OSM has no `opening_hours` for are sent in one batched request (max 15), and each answer is cached in the browser so a place is asked about once.
- **Checks live events**: one Google-Search-grounded request for VIP movement, barricades, closures and protests along the trip, cached per area per hour. Each alert lowers scores within its radius (−30), shows on the map and as a warning on the card.

Uses `gemini-2.5-flash-lite` with thinking off and capped output. If Gemini fails, routes are scored on map data alone.
The key is visible to anyone who loads the page, so restrict it to your domain in Google Cloud.
Event alerts reach the scorer as `hazards: [{pt, r, what, penalty}]` in `scoreRoutes` options.

## Features
- Safest / Balanced / Fastest options, with the route coloured green, amber or red along its length
- Pick a departure time, so open-shop and lighting checks match that time
- Open places and police stations marked as safe havens on the map
- **Share my trip**, plus an **SOS** button that shares your location via the share sheet or SMS and offers to call 112

## Not built yet
Live location tracking, auto check-in, community reports, crime data and income data all need a backend or city-specific datasets. `pointScore` in `score.js` is where new factors go.
