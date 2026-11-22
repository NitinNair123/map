# AI street model (experimental, not connected to the app)

Nothing in the app imports this folder. It learns from the same free sources the app uses and writes its results to `ml/out/`.

1. `pip install -r ml/requirements.txt`
2. Keep `ml/collect.py` running (laptop: double-click the .bat; server: see safe-route/ai-model/collector-on-vps.md): logs Delhi OTD bus counts and TomTom traffic per hour. Counts only.
3. `py ml/dataset.py` exports training rows. `--all` covers the whole OSM import, `--world` adds ~40 other cities (marked `region=other`, never scored), `--redo` rebuilds blocks after new logs or code.
4. `py ml/train.py` trains the heads, scores them once on 20% of areas locked away, and lists which parameters helped. Output: `ml/out/report.md`.
5. `py ml/unified.py retrain` builds the one street model (`ml/out/unified/current/street_model.pkl`): the four parts learn from each other, may answer "unknown", and a part replaces the one in use only if it passes the release gate. `train`, `check`, `promote`, `rollback` and `status` are in the file's header. Tests: `py -m unittest ml.test_unified`.
5. Route model: `py ml/pairs.py`, then `py ml/label_llm.py` (Qwen) and `py ml/label_claude.py` (Claude: its hand answers in `ml/data/labels_claude_hand.csv`, its written criteria in `ml/eval_claude/criteria.md` for the rest), then `py ml/rank_v2.py`. People's answers from `py ml/rate_pairs.py` count most once there are any. The Studio (`py ml/studio.py`) runs all of these on Play and shows a "Route model training: Qwen + Claude" card.
6. `python -m unittest ml.test_ml ml.test_route_v2`

The lit model's estimates are only used (by `ml/sim.py` and `ml/world.py`) once it beats a plain guess on its locked test; see `ml/eval_claude/report.md` for the independent evaluation.

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
