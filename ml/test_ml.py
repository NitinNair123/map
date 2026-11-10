"""Checks for the AI street model pipeline. Run from the repo folder: python -m unittest ml.test_ml"""
import os
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
