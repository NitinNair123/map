# AI street model (experimental, not connected to the app)

Nothing in the app imports this folder. It learns from the same free sources the app uses and writes its results to `ml/out/`.

1. `pip install -r ml/requirements.txt`
2. Keep `py ml/collect.py` running (best on the always-on server): logs Delhi OTD bus counts and TomTom traffic per hour. Counts only.
3. `py ml/dataset.py` exports training rows from OSM, NASA night lights, Mapillary, Geoapify, Overture, GTFS, layers, reports and the logs. `--all` covers the whole OSM import; `--redo` rebuilds after new logs.
4. `py ml/train.py` trains the heads and checks them on held-out ~1 km tiles. Output: `ml/out/report.md`.
5. `python -m unittest ml.test_ml`

Left out on purpose: Google Places hours (terms forbid training) and Gemini hours (estimates). NCRB district crime is not a feature.
No manual audits are used. The model predicts lighting and activity, not "safe".
