"""The women's-safety rater on made-up files shaped like the real downloads: labels parse, points snap to streets,
W learns a planted pattern and beats the road-type baseline on locked blocks, L learns a planted scene pattern and
beats the average on unseen cities, and score writes per-street values. Run: python -m unittest ml.test_rater"""
import json
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rater  # noqa: E402
from features import HIGHWAYS, STATIC  # noqa: E402


def write_world(root, rng):
    blocks, inc = os.path.join(root, "blocks"), os.path.join(root, "incoming")
    os.makedirs(blocks)
    # 60 blocks of streets on a grid around Delhi; "shops" drives women's share
    streets = []
    for b in range(60):
        lat0, lon0 = 28.45 + 0.03 * (b // 8), 77.0 + 0.03 * (b % 8)
        n = 40
        e = pd.DataFrame({c: rng.random(n) for c in STATIC})
        e["highway"] = rng.choice([HIGHWAYS.index("primary"), HIGHWAYS.index("residential")], n)
        e["sidewalk"] = rng.choice([0, 1, 2], n)
        e["places_shop"] = rng.integers(0, 10, n)
        e["lat"], e["lon"] = lat0 + rng.random(n) * 0.025, lon0 + rng.random(n) * 0.025
        e.insert(0, "edge_id", [f"{b}-{i}" for i in range(n)])
        e["tile"] = f"{3 * b}_{3 * (b % 7)}"
        e.to_csv(os.path.join(blocks, f"{b}.edges.csv.gz"), index=False)
        streets.append(e)
    s = pd.concat(streets)
    pick = s.sample(900, random_state=1)
    share = 0.05 + 0.05 * pick["places_shop"].to_numpy() / 2  # 0.05 .. 0.275
    people = rng.integers(2, 15, len(pick))
    women = rng.binomial(people, share)
    # StreetSight-shaped rows: first 300 points, two annotators each
    os.makedirs(os.path.join(inc, "streetsight"))
    ss = pick.iloc[:300]
    rows = []
    for k, (_, r) in enumerate(ss.iterrows()):
        for ann in (1, 2):
            rows.append({"task_id": k, "city": "Delhi", "lat": r["lat"] + 5e-5, "lon": r["lon"],
                         "women_count_num": women[k] if ann == 1 else "", "men_count_num": people[k] - women[k]})
    rows.append({"task_id": 999, "city": "Mumbai", "lat": 19.0, "lon": 72.8, "women_count_num": 3, "men_count_num": 3})
    pd.DataFrame(rows).to_csv(os.path.join(inc, "streetsight", "combined_annotations_clean.csv"), index=False)
    # women-count-shaped rows: next 600 points, three workers, one says men > people
    os.makedirs(os.path.join(inc, "women_count"))
    rows = []
    for k, (_, r) in enumerate(pick.iloc[300:].iterrows()):
        i = 300 + k
        for wk in range(3):
            men = people[i] - women[i] if wk < 2 else people[i] + 3
            rows.append({"Input.file_id": f"f{i}", "Input.exif_lat": r["lat"], "Input.exif_long": r["lon"], "Input.lat": r["lat"],
                         "Input.long": r["lon"], "Input.exif_date": "2016-12-03", "Input.exif_time": "15:17:02",
                         "Answer.total_people": people[i], "Answer.total_men": men})
    pd.DataFrame(rows).to_csv(os.path.join(inc, "women_count", "batch_results.csv"), index=False)
    # SPECS-shaped: 5 cities x 40 images; women's score rises with sidewalk share and falls with wall share
    sp = os.path.join(inc, "specs_perception")
    os.makedirs(sp)
    n = 200
    seg = pd.DataFrame({"uuid": [f"u{i}" for i in range(n)], "Sidewalk": rng.integers(0, 5000, n), "Wall": rng.integers(0, 5000, n),
                        "Road": rng.integers(5000, 9000, n), "Sky": rng.integers(1000, 4000, n)})
    sh = seg[["Sidewalk", "Wall"]].div(seg[["Sidewalk", "Wall", "Road", "Sky"]].sum(1), axis=0)
    score = 5 + 8 * sh["Sidewalk"] - 8 * sh["Wall"] + rng.normal(0, 0.15, n)
    seg.to_csv(os.path.join(sp, "abuja_segmentation.csv"), index=False)
    pd.DataFrame({"uuid": seg["uuid"], "city": np.repeat(["A", "B", "C", "D", "E"], 40), "Image number": range(n)}).to_csv(os.path.join(sp, "metadata.csv"))
    q = pd.concat([pd.DataFrame({"country": "All", "gender": g, "Image": range(n), "Question": "safe", "Score": score + (0.3 if g == "male" else 0)})
                   for g in ("female", "male")])
    q.to_csv(os.path.join(sp, "gender_qscores.csv"), index=False)
    # Delhi street images with the same scene columns, on top of streets
    st = os.path.join(inc, "streetscapes")
    os.makedirs(st)
    d = s.sample(50, random_state=2)
    seg2 = pd.DataFrame({"uuid": [f"d{i}" for i in range(50)], "source": "Mapillary", "orig_id": 1, "Sidewalk": 3000, "Wall": 100, "Road": 6000, "Sky": 2000})
    seg2.to_csv(os.path.join(st, "segmentation.csv"), index=False)
    pd.DataFrame({"uuid": seg2["uuid"], "lat": d["lat"].to_numpy(), "lon": d["lon"].to_numpy()}).to_csv(os.path.join(st, "images.csv"), index=False)
    return blocks, inc


class RaterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        rater.BLOCKS, rater.IN = write_world(cls.tmp.name, np.random.default_rng(0))
        rater.OUT = os.path.join(cls.tmp.name, "out")
        rater.IMAGES = os.path.join(rater.IN, "images")
        rater.CLIP = os.path.join(rater.IMAGES, "clip_b32.csv.gz")
        rater.EXTRA = os.path.join(rater.IMAGES, "mapillary_extra.csv")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_labels(self):
        d = rater.labels()
        self.assertEqual(set(d["source"]), {"streetsight", "women_count"})
        self.assertNotIn(19.0, d["lat"].round().tolist())  # Mumbai row dropped
        self.assertTrue((d["women"] <= d["people"]).all() and (d["women"] >= 0).all())
        self.assertTrue((d.loc[d.source == "women_count", "hour"] == 15).all())

    def test_snap_distance(self):
        pts = pd.DataFrame({"lat": [28.46, 10.0], "lon": [77.01, 10.0]})
        s = rater.snap(pts, 1e8, ["edge_id", "tile"])
        self.assertEqual(len(rater.snap(pts.iloc[[1]], 30, ["edge_id"])), 0)  # far away: dropped, not forced onto a street
        self.assertEqual(len(s), 2)

    def test_clip_inputs_join(self):
        os.makedirs(rater.IMAGES, exist_ok=True)
        pd.DataFrame({"uuid": ["u1", "u2", "d3"], "clip_0": [0.1, 0.2, 0.3]}).to_csv(rater.CLIP, index=False)
        try:
            f = rater.scene()
            self.assertEqual(sorted(f["uuid"]), ["d3", "u1", "u2"])  # only photos with an embedding
            self.assertIn("clip_0", f.columns)
            self.assertEqual(rater.specs()[1], ["clip_0"])  # with photos, L learns from CLIP numbers only
        finally:
            os.remove(rater.CLIP)

    def test_fetch_more_thins_and_resumes(self):
        import io
        from unittest import mock
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "JPEG")
        listing = {"data": [{"id": str(k), "computed_geometry": {"coordinates": [77.0 + k * 1e-5, 28.45]}, "captured_at": k,
                             "thumb_1024_url": f"http://x/{k}"} for k in range(5)]}  # 5 photos within ~5 m: one kept

        def get(url, tries=3):
            return json.dumps(listing).encode() if "graph.mapillary" in url else buf.getvalue()
        os.environ["MAPILLARY_TOKEN"] = "t"
        try:
            with mock.patch.object(rater, "_get", side_effect=get) as g:
                rater.cmd_fetch_more(limit=2)
                first = g.call_count
                rater.cmd_fetch_more(limit=2)  # limit reached: nothing listed again
                self.assertEqual(g.call_count, first)
        finally:
            del os.environ["MAPILLARY_TOKEN"]
        extra = pd.read_csv(rater.EXTRA)
        self.assertEqual(len(extra), 2)  # two squares, newest photo (id 4) in each
        self.assertTrue((extra["uuid"] == "mly_4").all())
        os.remove(rater.EXTRA)

    def test_fetch_kartaview_skips_covered_spots_and_drops_username(self):
        import io
        from unittest import mock
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "JPEG")
        img = pd.read_csv(os.path.join(rater.IN, "streetscapes", "images.csv"))
        near = img.iloc[0]  # a spot that already has a Delhi photo
        items = [{"id": 1, "lat": near["lat"], "lng": near["lon"], "shot_date": "2021", "username": "someone"},
                 {"id": 2, "lat": 28.9, "lng": 77.5, "shot_date": "2021", "username": "someone"},
                 {"id": 3, "lat": 28.9, "lng": 77.5, "shot_date": "2020", "username": "someone"}]

        def get(url, tries=3, form=None):
            if "nearby-photos" in url:
                return json.dumps({"currentPageItems": items}).encode()
            if "/2.0/photo/" in url:
                return json.dumps({"result": {"data": {"imageLthUrl": "http://x/p.jpg"}}}).encode()
            return buf.getvalue()
        with mock.patch.object(rater, "_get", side_effect=get):
            rater.cmd_fetch_kartaview()
        extra = pd.read_csv(rater.EXTRA)
        self.assertEqual(sorted(set(extra["uuid"])), ["kv_2"])  # id 1 sits on a covered spot, id 3 is an older duplicate
        self.assertNotIn("username", extra.columns)
        os.remove(rater.EXTRA)

    def test_train_and_score(self):
        rater.cmd_train()
        m = json.load(open(os.path.join(rater.OUT, "metrics.json")))
        self.assertTrue(m["W"]["passes"], m["W"]["locked"])
        self.assertTrue(m["L"]["passes"], m["L"])
        self.assertGreater(m["L"]["pooled_spearman"], 0.8)
        rater.cmd_score()
        out = pd.read_csv(os.path.join(rater.OUT, "delhi_looks_safe.csv"))
        self.assertGreater(len(out), 40)
        self.assertTrue(out["looks_safe"].between(0, 100).all())
        self.assertEqual(list(out.columns), ["edge_id", "lat", "lon", "looks_safe", "images"])
        self.assertTrue(out["lat"].between(28.4, 28.8).all())  # the street's midpoint, not the photo's


if __name__ == "__main__":
    unittest.main()
