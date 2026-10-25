# SafeRoute

Walking directions that pick the **safest** route, not just the fastest. Every road segment gets a
0–100 safety score for the time you're walking, and you get three options: Safest, Balanced and Fastest,
each with the reasons behind its score.

## Run it

Needs Python 3.10+ and nothing else.

```sh
mkdir -p data
python fetch_osm.py SOUTH WEST NORTH EAST   # bounding box of your area, e.g. your campus
python server.py                             # then open http://localhost:8000
```

Keep the box a few km across at most. You can get the coordinates by drawing a box on
https://bboxfinder.com. Tap the map to set a start, tap again for a destination, and pick a time.

Run the tests with `python -m unittest test_safety`.

## How the score works

Each segment is scored from OpenStreetMap data near it, at the chosen time:

| Factor | Day | Night | From |
|---|---|---|---|
| Lighting | 5 | 30 | `lit=*` tags, street lamps within 20 m |
| Open places (eyes on the street) | 30 | 30 | shops, cafes, pharmacies, petrol pumps that are **open at that time** |
| Security | 15 | 15 | police station within 400/800 m, CCTV within 50 m |
| Path type | 35 | 15 | main road vs. lane, footway, track or steps |
| Transport nearby | 15 | 10 | bus stops and taxi stands within 150 m |

Bars and liquor shops lower the score at night. Community reports move it up or down and fade by half
every 30 days.

Routes search the whole road graph with `cost = length × (1 + λ·(1 − score/100))`, where λ is 10 for
Safest, 3 for Balanced and 0 for Fastest. A route's score is 60% its average plus 40% its weakest segment,
minus penalties for long isolated stretches and for detours over 1.3× the fastest route. Routes over
mostly unmapped streets are flagged as low data.

## In the app

- Route cards with score, extra minutes and "why this route" reasons, plus the weakest stretch dashed on the map.
- Safety map overlay for the chosen hour.
- Anonymous reports: broken light, felt unsafe, harassment, felt safe. They change scores straight away.
- SOS button with a call to 112 and an SMS with your location to a trusted contact.
- Share trip, which sends your location and destination to a friend.

## Files

- `fetch_osm.py` downloads roads, lamps, CCTV and places from the Overpass API.
- `safety.py` holds scoring, opening-hours parsing and routing.
- `server.py` is the web server and JSON API (`/api/route`, `/api/heat`, `/api/report`).
- `static/index.html` is the whole front end (Leaflet).
