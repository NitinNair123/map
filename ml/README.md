# AI street model (experimental, not connected to the app)

Nothing in the app imports this folder. It learns from the same free sources the app uses and writes its results to `ml/out/`.

1. `pip install -r ml/requirements.txt`
2. Keep `ml/collect.py` running (laptop: double-click the .bat; server: see safe-route/ai-model/collector-on-vps.md): logs Delhi OTD bus counts and TomTom traffic per hour. Counts only.
3. `py ml/dataset.py` exports training rows. `--all` covers the whole OSM import, `--world` adds ~40 other cities (marked `region=other`, never scored), `--redo` rebuilds blocks after new logs or code.
4. `py ml/train.py` trains the heads, scores them once on 20% of areas locked away, and lists which parameters helped. Output: `ml/out/report.md`.
5. Stage 2 (after base training): `py ml/sim.py` races the route choices; `py ml/world.py` is the virtual world, a walker rewarded for arriving and punished for events on the real street graph (event chances are labelled assumptions in `ml/sim.py`). Resumes after a pause. Both run from the AI Studio.
6. `python -m unittest ml.test_ml`

## Parameters (about 100 per street segment, all measured; unknown stays unknown, never zero)
- **Safety infrastructure:** lamps, CCTV (OSM + Mapillary), guard posts, SOS phones, police distance, crossings, blackspots, waterlogging
- **Street itself:** road class, sidewalk, lanes, speed, unpaved, rough surface, underpass, footbridge, length, dead ends, junction gaps, degree
- **Surroundings:** buildings (100 m / 300 m / 500 m), land use, ESA WorldCover shares (built, trees, fields, bare, water at 50 m and 250 m), NASA night lights, walls, fences, drains and canals, parking
- **Eyes and help:** places by type (food, shop, pharmacy, hospital, fuel) with counts at 250/500 m and nearest distance, nightlife, transit stops, toilets, vending machines, shelters
- **Neighbourhood:** road length, main-road / footpath / service shares, junctions, dead-end share within 300 m; distance to the nearest main road; neighbouring streets' lamps and places
- **Time:** hour, weekday/weekend, daylight, timetabled departures (GTFS), logged buses, logged TomTom speed ratio, opening hours
- **Recorded events (when data exists):** incidents and app reports, decayed like the app does

Left out on purpose: Google Places hours (Google's terms restrict storing and training on them), Gemini hours (estimates), NCRB district crime (too coarse to place on a street), Ookla (non-commercial licence), building heights (too sparse in OSM).
No manual audits are used. The model predicts lighting and activity, not "safe".
