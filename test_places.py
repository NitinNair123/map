import unittest
from types import SimpleNamespace

import places
from safety import is_open
from datetime import datetime


def poi(name, pt=(28.6315, 77.2167), hours=None, group="food", named=True):
    return {"name": name, "pt": pt, "hours": hours, "group": group, "named": named}


def hit(lat=28.6315, lon=77.2167, periods=None):
    return {"location": {"latitude": lat, "longitude": lon}, "regularOpeningHours": {"periods": periods or []}}


class Fake(places.Places):
    def __init__(self, *answers):
        super().__init__("key")
        self.answers, self.asked = list(answers), []

    def search(self, p):
        self.asked.append(p["name"])
        return self.answers.pop(0)


P = lambda d, oh, om, cd, ch, cm=0: {"open": {"day": d, "hour": oh, "minute": om}, "close": {"day": cd, "hour": ch, "minute": cm}}


class TestPlaces(unittest.TestCase):
    def test_to_osm(self):
        # Google counts days from Sunday; a late-night period closes on the next day
        oh = places.to_osm([P(1, 10, 30, 1, 21), P(6, 18, 0, 0, 2), P(0, 11, 0, 0, 20)])
        self.assertEqual(oh, "Mo 10:30-21:00; Sa 18:00-02:00; Su 11:00-20:00")
        self.assertTrue(is_open(oh, datetime(2026, 11, 2, 12)))  # Monday noon
        self.assertFalse(is_open(oh, datetime(2026, 11, 3, 12)))  # Tuesday: no period, closed
        self.assertEqual(places.to_osm([{"open": {"day": 0, "hour": 0, "minute": 0}}]), "24/7")
        self.assertEqual(places.to_osm(None), "")

    def test_no_key_does_nothing(self):
        area = SimpleNamespace(pois=[poi("Wenger's")], _cache={})
        self.assertEqual(places.Places().fill_hours(area, lambda p: True), 0)

    def test_fills_only_named_untimed_nearby_places_and_rejects_far_matches(self):
        g = Fake(hit(periods=[P(1, 10, 0, 1, 22)]), hit(lat=28.64))  # second match is ~1 km away
        pois = [poi("Wenger's"), poi("Saravana Bhavan"), poi("Has hours", hours="24/7"), poi("shop", named=False),
                poi("Far away", pt=(28.7, 77.3)), poi("Bus stop", group="transit")]
        area = SimpleNamespace(pois=pois, _cache={"old": 1})
        self.assertEqual(g.fill_hours(area, lambda p: p[0] < 28.65), 1)
        self.assertEqual(g.asked, ["Wenger's", "Saravana Bhavan"])
        self.assertEqual((pois[0]["hours"], pois[0]["google_hours"]), ("Mo 10:00-22:00", True))
        self.assertIsNone(pois[1]["hours"])
        self.assertEqual(area._cache, {})

    def test_cache_is_reused_and_failed_calls_are_retried(self):
        g = Fake(None, hit(periods=[P(1, 10, 0, 1, 22)]))
        area = lambda: SimpleNamespace(pois=[poi("Wenger's")], _cache={})
        self.assertEqual(g.fill_hours(area(), lambda p: True), 0)  # call failed: not cached
        self.assertEqual(g.fill_hours(area(), lambda p: True), 1)
        self.assertEqual(g.fill_hours(area(), lambda p: True), 1)  # from the cache, no third call
        self.assertEqual(len(g.asked), 2)


if __name__ == "__main__":
    unittest.main()
