"""Checks for the AI street model pipeline. Run from the repo folder: python -m unittest ml.test_ml"""
import gzip
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import features  # noqa: E402
from safety import Area  # noqa: E402
from test_safety import fixture  # noqa: E402


class Features(unittest.TestCase):
    def setUp(self):
        self.area = Area(fixture())
        self.rows = {r["edge_id"]: r for r in features.static_rows(self.area)}

    def test_lit_label_comes_from_the_tag_and_never_feeds_the_inputs(self):
        footway = next(r for r in self.rows.values() if r["highway"] == features.HIGHWAYS.index("footway"))
        self.assertEqual((footway["lit"], footway["lit_source"]), (0, "osm"))
        self.assertNotIn("lit", features.STATIC)
        self.assertTrue(all(r["lit"] is None for r in self.rows.values() if r["highway"] == features.HIGHWAYS.index("residential")))

    def test_inputs_count_what_is_mapped(self):
        market = [r for r in self.rows.values() if r["highway"] == features.HIGHWAYS.index("residential")]
        self.assertTrue(all(r["n_lamp"] >= 3 for r in market))
        self.assertTrue(sum(r["places_shop"] for r in market) >= 5)
        self.assertTrue(all(set(features.STATIC) <= set(r) for r in self.rows.values()))

    def test_open_share_follows_published_hours(self):
        slots = features.slot_rows(self.area)
        at = {(r["hour"], r["weekend"]): r["open_share"] for r in slots if r["open_known"] and r["edge_id"] in
              {k for k, v in self.rows.items() if v["places_shop"]}}
        self.assertEqual(at[(12, 0)], 1.0)  # shops open 08:00-23:00
        self.assertEqual(at[(3, 0)], 0.0)
        self.assertTrue(all("open" not in c for c in features.STATIC + features.TIME))

    def test_recorded_events_are_unknown_until_loaded_never_zero(self):
        import math
        import time
        self.assertTrue(all(math.isnan(r["incident_hits"]) and math.isnan(r["report_points"]) for r in self.rows.values()))
        features.INCIDENTS = features.REPORTS = True
        try:
            when = time.time()
            area = Area(fixture(), incidents=[{"lat": 28.0005, "lon": 77.0015, "type": "assault", "hour": 22, "ts": when}],
                        reports=[{"lat": 28.0005, "lon": 77.0015, "type": "harassment", "ts": when, "hour": 22, "weekday": 0, "by": "a"}])
            rows = features.static_rows(area)
        finally:
            features.INCIDENTS = features.REPORTS = False
        self.assertTrue(any(r["incident_hits"] > 0.9 for r in rows))  # an assault today counts about 1
        self.assertTrue(any(r["report_points"] <= -39 for r in rows))
        self.assertTrue(all(r["report_points"] == 0 for r in rows if r["incident_hits"] == 0))

    def test_a_source_that_keeps_failing_is_skipped(self):
        import dataset
        calls = []

        def down(*a):
            calls.append(a)
            raise OSError("timed out")
        f = dataset.breaker(down, "Test", limit=3)
        for _ in range(5):
            with self.assertRaises(OSError):
                f(1, 2, 3, 4)
        self.assertEqual(len(calls), 3)  # the last two never waited on the network

    def test_studio_restarts_a_crashed_stage_itself_but_never_one_you_paused(self):
        import studio
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "crash.py")
            with open(script, "w") as f:
                f.write("import sys; sys.exit(1)")
            old = studio.LOGS, studio.ROOT, studio.STAGES
            studio.LOGS, studio.ROOT, studio.STAGES = d, d, [("build", "Build", [script], "")]
            try:
                r = studio.Runner()
                r.start("build")
                r.procs["build"].wait()
                self.assertEqual(r.status("build"), "failed")
                r.heal()  # first sight of the crash: waits before restarting
                self.assertEqual(len(r.restarts.get("build", [])), 0)
                r.failed_at["build"] = 0  # the pause is over
                r.heal()
                self.assertEqual(len(r.restarts["build"]), 1)
                self.assertIn("Restarted by itself", r.notes["build"])
                self.assertIn("automatic restart 1/", open(os.path.join(d, "build.log")).read())
                r.procs["build"].wait()
                for _ in range(studio.MAX_RESTARTS):  # a stage that keeps crashing is given up on, not restarted forever
                    r.failed_at["build"] = 0
                    r.heal()
                    r.procs["build"].wait()
                self.assertIn("Gave up", r.notes["build"])
                self.assertNotIn("build", r.wanted)
                r.start("build")
                r.procs["build"].wait()
                r.pause("build")  # paused by hand: no restart whatever its exit code
                r.failed_at["build"] = 0
                n = len(r.restarts["build"])
                r.heal()
                self.assertEqual(len(r.restarts["build"]), n)
            finally:
                studio.LOGS, studio.ROOT, studio.STAGES = old

    def test_studio_reads_progress_from_logs_and_finds_its_old_stages(self):
        import subprocess
        import studio
        with tempfile.TemporaryDirectory() as d:
            old = studio.LOGS
            studio.LOGS = d
            try:
                open(os.path.join(d, "build.log"), "w").write("=== started a ===\n[1/9] block x: 5 streets, 9 with a lit label\n"
                                                              "=== started b ===\n[2/9] block y: 7 streets, 1 with a lit label\nmapillary: 3 failures in a row\n")
                open(os.path.join(d, "train.log"), "w").write("Training lit: 9 rows\nlit trial 1 of 4: score 0.5\nlit trial 2 of 4: score 0.4\n")
                a = studio.activity()
                self.assertEqual((a["build"]["blocks"], a["build"]["streets"], a["build"]["lit"]), (2, 7, 1))  # this run only
                self.assertEqual((a["train"]["head"], a["train"]["curve"], a["train"]["of"]), ("lit", [0.5, 0.4], 4))
                self.assertIn("build", a["problems"])
                if sys.platform.startswith("linux"):
                    p = subprocess.Popen(["sleep", "30"])
                    try:
                        r = studio.Runner()
                        r.last["pids"]["sim"] = (p.pid, studio.started_at(p.pid))
                        self.assertEqual(r.status("sim"), "running")  # started by an earlier Studio, still going
                        r.last["pids"]["sim"] = (p.pid, 0)
                        self.assertNotEqual(r.status("sim"), "running")  # same pid, other start time: not ours
                    finally:
                        p.kill()
            finally:
                studio.LOGS = old

    def test_mapillary_asks_one_tile_at_a_time(self):
        import json
        import tiles
        asked = []

        def fake(req, timeout):
            box = req.full_url.split("bbox=")[1].split("&")[0]
            asked.append(box)
            w, s, e, n = map(float, box.replace("%2C", ",").split(","))
            return json.dumps({"data": [{"id": "same", "object_value": "object--street-light",
                                         "geometry": {"coordinates": [(w + e) / 2, (s + n) / 2]}, "last_seen_at": None}]}).encode()
        real, token = tiles._read, os.environ.get("MAPILLARY_TOKEN")
        tiles._read, os.environ["MAPILLARY_TOKEN"] = fake, "x"
        try:
            out = tiles.mapillary(28.0, 77.0, 28.03, 77.03)
        finally:
            tiles._read = real
            os.environ.pop("MAPILLARY_TOKEN") if token is None else os.environ.__setitem__("MAPILLARY_TOKEN", token)
        self.assertEqual(len(set(asked)), 9)  # a 3x3 block is nine one-tile requests
        self.assertEqual(len(out["elements"]), 1)  # the same id from every tile counts once

    def test_mapillary_second_token_takes_over_when_refused(self):
        import io
        import json
        import urllib.error
        import tiles
        used = []

        def fake(req, timeout):
            used.append(req.full_url)
            if "first%7Ctoken" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, io.BytesIO(b""))
            return json.dumps({"data": []}).encode()
        real, saved = tiles._read, {k: os.environ.get(k) for k in ("MAPILLARY_TOKEN", "MAPILLARY_TOKEN_2")}
        tiles._read, os.environ["MAPILLARY_TOKEN"], os.environ["MAPILLARY_TOKEN_2"] = fake, "first|token", "second|token"
        try:
            tiles._mapillary_cell(28.0, 77.0, 28.01, 77.01)
        finally:
            tiles._read = real
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual(len(used), 2)
        self.assertIn("second%7Ctoken", used[1])

    def test_downloads_are_counted_as_they_happen(self):
        import dataset
        import tiles
        tiles.STATS.clear()

        def down(*a):
            raise OSError("timed out")
        with tempfile.TemporaryDirectory() as d:
            ts = tiles.TileStore(d, fetch=lambda *a: {"elements": [{"type": "node", "id": 1, "lat": 28.001, "lon": 77.001}]},
                                 extras={"mapillary": dataset.breaker(down, "Mapillary", limit=1)})
            ts._fetch_chunk([(2800, 7700)])
            ts._fetch_chunk([(2800, 7700)], "mapillary")
            ts._fetch_chunk([(2801, 7700)], "mapillary")  # refused by the breaker: no download tried
        st = tiles.stats_snapshot()
        self.assertEqual({k: st[""][k] for k in ("requests", "ok", "failed", "items", "busy")}, {"requests": 1, "ok": 1, "failed": 0, "items": 1, "busy": {}})
        self.assertEqual({k: st["mapillary"][k] for k in ("requests", "ok", "failed", "skipped")}, {"requests": 1, "ok": 0, "failed": 1, "skipped": 1})
        self.assertIn("timed out", st["mapillary"]["last_error"])
        tiles.STATS.clear()

    def test_overture_strips_on_disk_answer_blocks_inside_them(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        import tiles

        def table(rows):
            return pa.table({"id": [r[0] for r in rows], "bbox": [{"xmin": r[2], "xmax": r[2], "ymin": r[1], "ymax": r[1]} for r in rows]})
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.parquet"), os.path.join(d, "b.parquet")
            pq.write_table(table([("x", 28.05, 77.05), ("edge", 28.1, 77.1)]), a)
            pq.write_table(table([("edge", 28.1, 77.1), ("y", 28.15, 77.2)]), b)
            old = dict(tiles.OVERTURE_LOCAL)
            tiles.OVERTURE_LOCAL["buildings"] = [(28.0, 77.0, 28.1, 77.5, a), (28.1, 77.0, 28.2, 77.5, b)]
            try:
                rows = tiles._local("buildings", 28.04, 77.0, 28.16, 77.3, [])
                self.assertEqual(sorted(r["id"] for r in rows), ["edge", "x", "y"])  # across both strips, the shared one once
                self.assertIsNone(tiles._local("buildings", 28.15, 77.0, 28.25, 77.3, []))  # sticks out: goes online
                self.assertEqual(len(tiles.overture_buildings(28.0, 77.0, 28.06, 77.06)["elements"]), 1)
            finally:
                tiles.OVERTURE_LOCAL.clear()
                tiles.OVERTURE_LOCAL.update(old)
                tiles._strip_cache.clear()

    def test_overture_prefetch_saves_each_file_and_resumes(self):
        import pyarrow as pa
        import pyarrow.dataset as ds
        import pyarrow.parquet as pq
        import tiles
        box = lambda lat, lon: {"xmin": lon, "xmax": lon, "ymin": lat, "ymax": lat}
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            for k, rows in enumerate([[("a", 28.05, 77.05), ("far", 40.0, 10.0)], [("b", 28.15, 77.1)], [("c", 28.12, 77.2)]]):
                pq.write_table(pa.table({"id": [r[0] for r in rows], "bbox": [box(r[1], r[2]) for r in rows]}), os.path.join(src, f"p{k}.parquet"))
            data, real, calls = ds.dataset(src, format="parquet"), tiles._dataset, []
            tiles._dataset = lambda *a: data
            real_scan = ds.ParquetFileFormat
            out = os.path.join(d, "out")
            try:
                tiles.overture_prefetch(out, 28.0, 77.0, 28.2, 77.5, 0.1, kinds=(("buildings", []),), progress=lambda *a: calls.append(a), workers=2)
                self.assertEqual(len(os.listdir(os.path.join(out, "parts"))), 3)
                strips = {f: pq.read_table(os.path.join(out, f)).column("id").to_pylist() for f in os.listdir(out) if f.endswith(".parquet")}
                self.assertEqual(sorted(sum(strips.values(), [])), ["a", "b", "c"])  # "far" is outside the area
                self.assertEqual(calls[-1][1:], (3, 3))
                for f in os.listdir(out):  # a stopped run keeps its parts: delete the strips and rerun without downloading
                    if f.endswith(".parquet"):
                        os.remove(os.path.join(out, f))
                tiles._dataset = lambda *a: data
                before = {k: dict(v) for k, v in tiles.STATS.items()}.get("buildings", {}).get("requests", 0)
                tiles.overture_prefetch(out, 28.0, 77.0, 28.2, 77.5, 0.1, kinds=(("buildings", []),), workers=2)
                self.assertEqual(tiles.STATS["buildings"]["requests"], before)  # every file came from its part
                self.assertEqual(len([f for f in os.listdir(out) if f.endswith(".parquet")]), 2)
            finally:
                tiles._dataset = real
                tiles.OVERTURE_LOCAL.clear()
                tiles.STATS.clear()

    def test_studio_live_numbers_and_stall_warning(self):
        import json
        import time
        import studio
        now = time.time()
        live = {"pid": 1, "started": now - 900, "phase": "Measuring streets block by block", "blocks_total": 10, "blocks_on_disk": 4,
                "blocks_built": 2, "blocks_failed": 0, "streets": 600, "lit": 3, "first_block_at": now - 600, "block_seconds": [200, 400],
                "current": {"n": 7, "block": [1, 2], "started": now - 250}, "prefetch": None, "last_event": now - 250, "updated": now,
                "sources": {"buildings": {"requests": 3, "ok": 2, "failed": 0, "skipped": 0, "bytes": 5, "items": 9, "seconds": 20,
                                          "busy": {"28.00,77.00": now - 250}, "last": now - 250, "last_error": None}}}
        with tempfile.TemporaryDirectory() as d:
            old = studio.LOGS
            studio.LOGS = d
            try:
                json.dump(live, open(os.path.join(d, "build_live.json"), "w"))
                L = studio.live(True)
                self.assertEqual((L["streets_per_s"], L["block_avg_s"], L["eta_s"]), (1.0, 300, 1800))
                self.assertIn("Overture buildings", L["stalled"])
                self.assertEqual(L["waits"][0]["source"], "Overture buildings")
                self.assertIsNone(studio.live(False)["stalled"])  # paused: frozen numbers, no warning
                live.update(updated=now - 60)
                json.dump(live, open(os.path.join(d, "build_live.json"), "w"))
                self.assertIn("frozen", studio.live(True)["stalled"])
            finally:
                studio.LOGS = old

    def test_studio_groups_cover_every_input_once(self):
        import studio
        cols = [c for g in studio.GROUPS.values() for c in g.split()]
        self.assertEqual(sorted(cols), sorted(features.STATIC + features.TIME))

    def test_bus_rate_averages_watched_hours_only(self):
        bus = {"counts": {(28600, 77201, 0, 22): 6}, "watched": {(0, 22): 2}}
        self.assertEqual(features.bus_rate(bus, (28.6005, 77.2015), 0, 22), 3.0)
        self.assertIsNone(features.bus_rate(bus, (28.6005, 77.2015), 0, 9))  # never watched: unknown, not zero
        self.assertIsNone(features.bus_rate(bus, (51.5, -0.12), 0, 22))  # London: the feed doesn't cover it, so no count is not zero


class Routes(unittest.TestCase):
    def test_labelling_with_no_pairs_fails_and_with_pairs_writes_labels(self):
        import csv
        import json
        import label_llm
        real = (label_llm.pairs.load, label_llm.OUT, label_llm.ollama, sys.argv)
        pair = lambda i: {"id": i, "hour": 22, "weekend": 0, "A": {k: 1 for k in label_llm.pairs.FEATURES}, "B": {k: 0 for k in label_llm.pairs.FEATURES}}
        with tempfile.TemporaryDirectory() as d:
            label_llm.OUT, sys.argv = os.path.join(d, "labels.csv"), ["label_llm.py"]
            try:
                label_llm.pairs.load = lambda path=None: []
                with self.assertRaises(SystemExit) as stop:  # no pairs: the stage must fail, not say "done"
                    label_llm.main()
                self.assertIn("No route pairs", str(stop.exception))
                label_llm.pairs.load = lambda path=None: [pair(1), pair(2)]
                label_llm.ollama = lambda text, model: json.dumps({"safer": "A" if "A = 1, B = 0" in text else "B", "reason": "more of everything"})
                label_llm.main()
                rows = list(csv.DictReader(open(label_llm.OUT)))
                self.assertEqual([r["safer"] for r in rows], ["A", "A"])
                label_llm.ollama = lambda text, model: "no idea"  # nothing usable: fails too
                label_llm.pairs.load = lambda path=None: [pair(3)]
                with self.assertRaises(SystemExit):
                    label_llm.main()
            finally:
                label_llm.pairs.load, label_llm.OUT, label_llm.ollama, sys.argv = real

    def test_pair_holds_measures_but_never_the_score(self):
        import random
        import pairs
        from test_safety import NIGHT
        grid = __import__("test_safety").fixture()
        p = pairs.make_pair(Area(grid), (28.0, 77.0), (28.0, 77.003), NIGHT, random.Random(0))
        self.assertIsNotNone(p)
        self.assertEqual(set(p["A"]), set(pairs.FEATURES))
        self.assertFalse({"score", "kind", "rank"} & set(p["A"]))

    def test_language_model_answer_must_survive_the_swap(self):
        import json
        import label_llm
        p = {"hour": 22, "weekend": 0, "A": {k: 1 for k in label_llm.pairs.FEATURES}, "B": {k: 0 for k in label_llm.pairs.FEATURES}}
        likes_ones = lambda t: json.dumps({"safer": "A" if "A = 1" in t else "B", "reason": "more of everything"})
        self.assertEqual(label_llm.judge(p, likes_ones)[0], "A")
        self.assertIsNone(label_llm.judge(p, lambda t: json.dumps({"safer": "A"}))[0])  # always says A: position bias, thrown out
        self.assertIsNone(label_llm.judge(p, lambda t: "no idea")[0])

    def test_ranker_learns_what_the_answers_reward(self):
        import random
        import pairs
        import rank
        rng = random.Random(1)
        route = lambda: {k: rng.uniform(0, 100) for k in pairs.FEATURES}
        pl = [{"id": i, "block": f"{i % 8}_0", "daylight": i % 2, "A": route(), "B": route()} for i in range(400)]
        labels = {p["id"]: "A" if p["A"]["lit_pct"] - p["A"]["isolated_m"] > p["B"]["lit_pct"] - p["B"]["isolated_m"] else "B" for p in pl}
        saved, info = rank.train(pl, labels)
        self.assertGreater(info["accuracy_on_unseen_areas"], 0.9)
        self.assertGreater(saved["weights"]["lit_pct"], 0)
        self.assertLess(saved["weights"]["isolated_m"], 0)
        self.assertGreater(rank.score(saved, {**pl[0]["B"], "lit_pct": 100, "isolated_m": 0}, {**pl[0]["B"], "lit_pct": 0, "isolated_m": 100}, 0), 0)
        self.assertIn("skipped", rank.train(pl[:20], labels)[1])  # too few answers: says so, learns nothing


class Simulation(unittest.TestCase):
    def test_dark_footway_is_more_dangerous_and_safest_route_meets_fewer_events(self):
        import random
        import sim
        from test_safety import A, B, NIGHT
        area = Area(fixture())
        footway = next(e for e in area.edges if e["highway"] == "footway")
        market = next(e for e in area.edges if e["highway"] == "residential")
        self.assertGreater(sim.danger(area, footway, NIGHT)[0], sim.danger(area, market, NIGHT)[0])
        res = sim.simulate(area, [(A, B, NIGHT)] * 50, random.Random(0))
        self.assertEqual(res["safest"]["trips"], 50)
        self.assertLess(res["safest"]["expected_per_1000"], res["fastest"]["expected_per_1000"])
        self.assertGreater(res["safest"]["extra_minutes"], 0)

    def test_stage_two_uses_base_training_outputs(self):
        import random
        import pairs
        import sim
        from test_safety import A, B, NIGHT
        area = Area(fixture())
        footway = next(e for e in area.edges if e["highway"] == "footway")
        lane = next(e for e in area.edges if e["highway"] == "service")
        lane["near"]["lamp"] = []  # no lit tag and no lamps: lighting unknown
        key = lambda e: "-".join(sorted((str(e["a"]), str(e["b"]))))
        bare = sim.danger(area, lane, NIGHT)
        filled = sim.danger(area, lane, NIGHT, {key(lane): 1.0})
        self.assertLess(filled[1], bare[1])  # an estimate fills the gap
        self.assertLess(filled[0], bare[0])  # and "likely lit" lowers the danger
        self.assertEqual(sim.danger(area, footway, NIGHT, {key(footway): 1.0}), sim.danger(area, footway, NIGHT))  # never overrules a tag
        zero = {k: 0.0 for k in pairs.FEATURES + [f"{k}_dark" for k in pairs.FEATURES]}
        shortest = {"features": pairs.FEATURES, "weights": {**zero, "length_m": -1.0, "length_m_dark": -1.0}}
        res = sim.simulate(area, [(A, B, NIGHT)] * 10, random.Random(0), ranker=shortest)
        self.assertEqual(res["learned"]["expected_per_1000"], res["fastest"]["expected_per_1000"])  # learned "shorter is better" walks the fastest way


class VirtualWorld(unittest.TestCase):
    def test_walker_learns_to_dodge_the_danger_it_is_punished_for(self):
        import random
        import numpy as np
        import world
        from test_safety import NIGHT
        w0 = world.World(Area(fixture()))
        rng = random.Random(0)
        w = np.array([world.START.get(k, 0.0) for k in world.FEATS])
        base = 0.0
        trips = [w0.trip(rng) for _ in range(60)]
        before = world.evaluate(w0, w, [(a, b, NIGHT) for a, b, _ in trips], random.Random(1))
        for i in range(30):
            eps = []
            for _ in range(world.BATCH):
                a, b, _ = w0.trip(rng)
                steps, reward, ok, *_ = world.episode(w0, w, (a, b, NIGHT), rng)
                eps.append({"steps": steps, "reward": reward})
            w, base = world.learn(eps, w, base)
        after = world.evaluate(w0, w, [(a, b, NIGHT) for a, b, _ in trips], random.Random(1))
        self.assertLess(after["events"], before["events"])  # meets fewer events after learning than before, on the same trips
        self.assertLess(w[world.FEATS.index("danger")], 0)  # dangerous streets are avoided

    def test_resume_state_round_trips(self):
        import world
        with tempfile.TemporaryDirectory() as d:
            world.STATE = os.path.join(d, "s.json")
            s = world.load_state()
            s["episodes"] = 50
            world.save(s)
            self.assertEqual(world.load_state()["episodes"], 50)


class Surroundings(unittest.TestCase):
    def test_context_counts_and_nearest_distances(self):
        area = Area(fixture())
        rows = features.static_rows(area)
        market = [r for r in rows if r["highway"] == features.HIGHWAYS.index("residential")]
        self.assertTrue(all(r["lamps_100"] >= 3 and r["d_lamp"] < 60 for r in market))
        self.assertTrue(all(r["d_pharmacy"] <= 500 for r in rows))  # nothing within 500 m reads as the cap, which is 500
        self.assertTrue(all(r["d_cctv"] == 500 and r["cctv_500"] == 0 for r in rows))  # no cameras mapped near: "none within 500 m"
        self.assertTrue(all(r["road_km_300"] > 0 and 0 <= r["dead_end_share_300"] <= 1 for r in rows))
        self.assertTrue(any(r["d_main"] == 500 for r in rows))  # the fixture has no main road

    def test_buildings_not_loaded_are_unknown_not_zero(self):
        import math
        from test_safety import node
        self.assertTrue(all(math.isnan(r["bld_100"]) and math.isnan(r["buildings_ha"]) for r in features.static_rows(Area(fixture()))))
        els = fixture()["elements"] + [node("b1", (28.0004, 77.0005), building="yes")]
        self.assertTrue(any(r["bld_500"] >= 1 for r in features.static_rows(Area({"elements": els}))))

    def test_extra_osm_objects_near_a_street(self):
        import math
        import osm_extra
        row = {"lat": 28.0, "lon": 77.0}
        osm = {"elements": [
            {"type": "way", "id": 1, "tags": {"barrier": "wall"}, "geometry": [{"lat": 28.0001, "lon": 77.0}, {"lat": 28.0003, "lon": 77.0}]},
            {"type": "way", "id": 2, "tags": {"barrier": "fence"}, "geometry": [{"lat": 28.01, "lon": 77.0}, {"lat": 28.0101, "lon": 77.0}]},
            {"type": "way", "id": 3, "tags": {"waterway": "drain"}, "geometry": [{"lat": 28.001, "lon": 77.0}, {"lat": 28.0012, "lon": 77.0}]},
            {"type": "node", "id": 4, "lat": 28.0005, "lon": 77.0, "tags": {"amenity": "toilets"}},
            {"type": "way", "id": 5, "center": {"lat": 28.0002, "lon": 77.0}, "tags": {"amenity": "parking"}},
        ]}
        self.assertTrue(osm_extra.add([row], osm))
        self.assertAlmostEqual(row["wall_m_50"], 22, delta=1)  # 0.0002 degrees of latitude
        self.assertEqual((row["fence_m_50"], row["water_m_50"], row["water_m_150"]), (0, 0, 22))  # the far fence is out; the drain is 110 m away
        self.assertEqual((row["toilets_250"], row["parking_100"]), (1, 1))
        failed = {"lat": 28.0, "lon": 77.0}
        self.assertFalse(osm_extra.add([failed], None))
        self.assertTrue(all(math.isnan(failed[c]) for c in osm_extra.COLUMNS))  # a block that couldn't be fetched is unknown, not zero

    @staticmethod
    def fake_clock():
        """time.time() and time.sleep() on a clock that only sleep() and each simulated request move forward."""
        now = [1000.0]
        return unittest.mock.patch("time.time", lambda: now[0]), unittest.mock.patch("time.sleep", lambda s: now.__setitem__(0, now[0] + s)), now

    def test_overpass_falls_back_to_a_healthy_mirror_and_rests_a_bad_one(self):
        import osm_extra
        calls = []

        def ask(box, url):
            calls.append(url)
            if url == osm_extra.HOSTS[0]:
                osm_extra._mark(url, False)
                raise OSError("HTTP Error 502: Bad Gateway")
            osm_extra._mark(url, True)
            return {"elements": [{"type": "way", "id": 1}]}
        old, osm_extra._ask, osm_extra.HEALTH = osm_extra._ask, ask, {}
        try:
            clock, nap, _ = self.fake_clock()
            with clock, nap:
                self.assertEqual(osm_extra.fetch((28.0, 77.0, 28.03, 77.03))["elements"][0]["id"], 1)
                self.assertEqual(calls[:2], osm_extra.HOSTS[:2])  # the first mirror failed, the next answered
                osm_extra._mark(osm_extra.HOSTS[0], False)  # a second failure in a row rests it
                calls.clear()
                osm_extra.fetch((28.0, 77.0, 28.03, 77.03))
                self.assertNotIn(osm_extra.HOSTS[0], calls)
        finally:
            osm_extra._ask = old

    def test_overpass_asks_for_quarters_when_the_whole_box_fails_and_gives_up_as_unknown(self):
        import osm_extra
        boxes = []

        def ask(box, url):
            boxes.append(box)
            if box[2] - box[0] > 0.02:  # only small boxes get through
                osm_extra._mark(url, False)
                raise OSError("The read operation timed out")
            osm_extra._mark(url, True)
            return {"elements": [{"type": "way", "id": 7}, {"type": "node", "id": len(boxes)}]}
        old, osm_extra._ask, osm_extra.HEALTH = osm_extra._ask, ask, {}
        try:
            clock, nap, _ = self.fake_clock()
            with clock, nap:
                osm = osm_extra.fetch((28.0, 77.0, 28.03, 77.03), deadline=600)
            self.assertEqual(len([e for e in osm["elements"] if e["id"] == 7]), 1)  # a way in two quarters counts once
            self.assertEqual(len([e for e in osm["elements"] if e["type"] == "node"]), 4)
            osm_extra.HEALTH = {}
            def down(box, url):
                osm_extra._mark(url, False)
                raise OSError("down")
            osm_extra._ask = down
            clock, nap, now = self.fake_clock()
            with clock, nap:
                self.assertIsNone(osm_extra.fetch((28.0, 77.0, 28.03, 77.03)))  # None, so the columns stay NaN
                self.assertLessEqual(now[0] - 1000, osm_extra.DEADLINE_S + 20)  # and it gave up within its deadline
        finally:
            osm_extra._ask = old

    def test_a_resting_source_is_tried_again_and_blocks_missing_a_source_are_found(self):
        import dataset
        state = {"down": True}

        def src(*a):
            if state["down"]:
                raise OSError("timed out")
            return {"elements": []}
        f = dataset.breaker(src, "Test", limit=2, cool=300)
        for _ in range(4):
            with self.assertRaises(OSError):
                f(1)
        self.assertGreater(f.state["until"], 0)  # resting now
        f.state["until"] = 0  # the rest is over
        state["down"] = False
        self.assertEqual(f(1), {"elements": []})
        self.assertEqual(f.state["fails"], 0)
        with tempfile.TemporaryDirectory() as d:
            for name, sources in (("1_1", "OpenStreetMap, OSM walls, drains, parking, toilets, Mapillary"), ("1_2", "OpenStreetMap, Mapillary")):
                for kind in ("edges", "slots"):
                    with gzip.open(os.path.join(d, f"{name}.{kind}.csv.gz"), "wt", newline="") as fh:
                        fh.write("edge_id,sources\n" + (f"a,\"{sources}\"\n" if kind == "edges" else "a,x\n"))
            order = [((1, 1), {(1, 1)}), ((1, 2), {(1, 2)}), ((1, 3), {(1, 3)})]  # block 1_3 isn't written yet: not a gap
            self.assertEqual([b for _, b, _ in dataset.gaps(d, order, ["OSM walls", "Mapillary"])], [(1, 2)])
            self.assertEqual(dataset.gaps(d, order, ["Mapillary"]), [])


class LandCover(unittest.TestCase):
    def test_tile_names_match_esa_worldcover(self):
        import rasters
        self.assertEqual(rasters.tile_name(28.47, 77.50), "N27E075")  # Delhi NCR
        self.assertEqual(rasters.tile_name(51.5, -0.12), "N51W003")  # London
        self.assertEqual(rasters.tile_name(-33.87, 151.2), "S36E150")  # Sydney

    def test_shares_count_a_square_and_leave_unknown_at_the_edge(self):
        import numpy as np
        import rasters
        grid = np.full((40, 40), 50, np.uint8)  # all built-up
        grid[:, 20:] = 10  # right half trees
        got = rasters.shares(grid, np.array([20, 20, 1]), np.array([10, 30, 1]), 5)
        self.assertEqual(got["built"][0], 1.0)
        self.assertEqual(got["tree"][1], 1.0)
        self.assertTrue(np.isnan(got["built"][2]))  # the square leaves the map: unknown, not zero
        mixed = rasters.shares(grid, np.array([20]), np.array([20]), 5)
        self.assertAlmostEqual(mixed["built"][0] + mixed["tree"][0], 1.0)


class Training(unittest.TestCase):
    def setUp(self):
        try:
            import numpy as np
            import pandas as pd
            import train
        except ImportError:
            self.skipTest("pip install -r ml/requirements.txt")
        self.np, self.pd, self.train = np, pd, train
        rng = np.random.default_rng(0)
        n = 4000
        e = pd.DataFrame({c: rng.random(n) for c in features.STATIC})
        e["highway"], e["sidewalk"] = rng.integers(0, 5, n), rng.integers(0, 3, n)
        e["edge_id"], e["tile"] = [str(i) for i in range(n)], [f"{i % 40}_0" for i in range(n)]
        e["block"] = e["tile"].map(train.block_of)
        e["region"] = "ncr"
        e["lit"] = (e["n_lamp"] + 0.2 * rng.random(n) > 0.6).astype(float)  # lamps decide it, as on a real street
        e["lit_source"] = "osm"
        self.e, self.rng = e, rng

    def test_head_with_no_rows_is_skipped_not_a_crash(self):
        saved, info = self.train.run_head("bus_per_hour", "reg", self.e.iloc[0:0], ["n_lamp"], "lit")
        self.assertIsNone(saved)
        self.assertIn("skipped", info)

    def test_unknown_columns_and_single_class_folds_do_not_crash_training(self):
        np, pd = self.np, self.pd
        X = pd.DataFrame({"a": self.rng.random(60), "b": np.nan, "highway": self.rng.integers(0, 3, 60)})
        y = np.r_[np.ones(59), 0].astype(int)
        m = self.train.make("clf", list(X.columns), {"max_iter": 5}).fit(X, y)  # column b is entirely unknown: left out
        self.assertEqual(m.predict_proba(X).shape, (60, 2))
        ones = self.train.make("clf", list(X.columns), {"max_iter": 5}).fit(X, np.ones(60, int))  # one class only
        self.assertTrue((ones.predict_proba(X)[:, 1] == 1).all())
        zeros = self.train.make("clf", list(X.columns), {"max_iter": 5}).fit(X, np.zeros(60, int))
        self.assertTrue((zeros.predict_proba(X)[:, 1] == 0).all())

    def test_lit_head_is_scored_once_on_locked_blocks(self):
        saved, m = self.train.train_lit(self.e, trials=2, budget=60)
        t = m["locked_test"]
        self.assertGreater(t["auroc"], 0.9)
        self.assertLess(t["error_rate"], t["always_guess_majority_error"])
        locked = set(self.e["block"][self.train.locked(self.e["block"])])
        self.assertTrue(locked and locked.isdisjoint(set(self.e["block"][~self.train.locked(self.e["block"])])))
        self.assertEqual(m["chosen"], "ncr_only")
        self.assertEqual(list(m["most_useful_parameters"])[0], "n_lamp")  # the one column that decides the synthetic label
        self.assertIn("nb_places", m["parameters_with_no_measurable_effect"])  # noise columns are reported as not helping
        p = saved["calibrator"].predict(saved["model"].predict_proba(self.e[features.STATIC])[:, 1])
        self.assertTrue(((p >= 0) & (p <= 1)).all())

    def test_other_regions_can_help_but_are_never_scored(self):
        np, pd = self.np, self.pd
        other = self.e.copy()
        other["region"], other["edge_id"] = "other", "o" + other["edge_id"]
        other["tile"] = [f"{900 + i % 40}_0" for i in range(len(other))]
        other["block"] = other["tile"].map(self.train.block_of)
        _, m = self.train.train_lit(pd.concat([self.e, other]), trials=2, budget=60)
        self.assertEqual(set(m["variants_cv_score_on_ncr (lower is better)"]), {"ncr_only", "ncr_plus_other_regions"})
        self.assertEqual(m["locked_test"]["rows"], int(self.train.locked(self.e["block"]).sum()))

    def test_hourly_head_beats_the_hour_average(self):
        np, pd = self.np, self.pd
        e = self.e
        s = pd.DataFrame({"edge_id": np.repeat(e["edge_id"].to_numpy(), 2), "hour": np.tile([9, 22], len(e)), "weekend": 0,
                          "daylight": 0.5, "gtfs_deps": 0.0})
        s["open_share"] = np.where(s["hour"] == 9, 1.0, 0.0) * np.repeat(e["places_shop"].to_numpy() > 0.5, 2)
        _, m = self.train.train_hourly(e, s, "open_share", trials=2, budget=60)
        self.assertTrue(m["beats_baseline"])

    def test_too_little_data_is_reported_not_guessed(self):
        _, m = self.train.train_lit(self.e.head(50), trials=1)
        self.assertIn("skipped", m)


class TrafficRebuild(unittest.TestCase):
    def test_only_blocks_saved_before_newer_traffic_that_reaches_them_are_rebuilt(self):
        import dataset
        with tempfile.TemporaryDirectory() as d:
            traffic = os.path.join(d, "traffic.csv")
            open(traffic, "w").write("x\n")
            old, fresh = (0, 0), (5, 5)
            for b, age in ((old, -100), (fresh, 100), ((9, 9), -100)):
                p = os.path.join(d, f"{b[0]}_{b[1]}.slots.csv.gz")
                open(p, "w").write("x")
                t = os.path.getmtime(traffic) + age
                os.utime(p, (t, t))
            order = [(old, ["a"]), (fresh, ["b"]), ((9, 9), ["c"]), ((7, 7), ["d"])]  # (7,7) was never saved
            cell = lambda b: (b[0] * dataset.CHUNK * 10, b[1] * dataset.CHUNK * 10)
            tr = {cell(old): 1, cell(fresh): 1}  # no traffic reaches block (9, 9)
            self.assertEqual(dataset.older_than_traffic(d, order, tr, traffic), [(1, old, ["a"])])
            self.assertEqual(dataset.older_than_traffic(d, order, {}, traffic), [])


class ServerLogs(unittest.TestCase):
    def test_merge_keeps_every_row_once(self):
        import merge_logs
        with tempfile.TemporaryDirectory() as d:
            ours, theirs = os.path.join(d, "a.csv"), os.path.join(d, "b.csv")
            open(ours, "w").write("h\n1\n2\n")
            open(theirs, "w").write("h\n2\n3\n3\n")
            self.assertEqual(merge_logs.merge(ours, theirs), 1)
            self.assertEqual(open(ours).read(), "h\n1\n2\n3\n")
            self.assertEqual(merge_logs.merge(ours, theirs), 0)  # pulling again adds nothing
            os.remove(ours)
            merge_logs.merge(ours, theirs)
            self.assertEqual(open(ours).read(), "h\n2\n3\n")

    def test_logger_needs_only_the_standard_library(self):
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        own = {"buses", "traffic", "spots"}
        for f in ["collect.py", "spots.py", os.path.join("..", "buses.py"), os.path.join("..", "traffic.py")]:
            tree = ast.parse(open(os.path.join(here, f), encoding="utf-8").read())
            mods = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
            mods |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom)}
            self.assertFalse(mods - own - set(sys.stdlib_module_names), f)  # the server has plain Python only




if __name__ == "__main__":
    unittest.main()
