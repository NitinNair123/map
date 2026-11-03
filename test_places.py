import unittest
import urllib.error
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import places
from safety import is_open


def poi(name, pt=(28.6315, 77.2167), hours=None, group="food", named=True):
    return {"name": name, "pt": pt, "hours": hours, "group": group, "named": named}


def hit(name, lat=28.6315, lon=77.2167, periods=None):
    return {"displayName": {"text": name}, "location": {"latitude": lat, "longitude": lon}, "regularOpeningHours": {"periods": periods or []}}


class Fake(places.Places):
    """Answers every circle with the same Google places, or with None (a failed call) while fail > 0."""
    def __init__(self, found, fail=0):
        super().__init__("key")
        self.found, self.fail, self.centers = found, fail, []

    def nearby(self, center):
        self.centers.append(center)
        if self.fail:
            self.fail -= 1
            return None
        return self.found


P = lambda d, oh, om, cd, ch, cm=0: {"open": {"day": d, "hour": oh, "minute": om}, "close": {"day": cd, "hour": ch, "minute": cm}}
WEEKDAYS = [P(1, 10, 0, 1, 22)]


class TestPlaces(unittest.TestCase):
    def test_to_osm(self):
        # Google counts days from Sunday; a late-night period closes on the next day
        oh = places.to_osm([P(1, 10, 30, 1, 21), P(6, 18, 0, 0, 2), P(0, 11, 0, 0, 20)])
        self.assertEqual(oh, "Mo 10:30-21:00; Sa 18:00-02:00; Su 11:00-20:00")
        self.assertTrue(is_open(oh, datetime(2026, 11, 2, 12)))  # Monday noon
        self.assertFalse(is_open(oh, datetime(2026, 11, 3, 12)))  # Tuesday: no period, closed
        self.assertEqual(places.to_osm([{"open": {"day": 0, "hour": 0, "minute": 0}}]), "24/7")
        self.assertEqual(places.to_osm(None), "")

    def test_same_name(self):
        self.assertTrue(places.same_name("Cottage Emporium", "Central Cottage Industries Emporium"))
        self.assertFalse(places.same_name("Nepal Excursions", "Hotel Alka Premier"))

    def test_no_key_does_nothing(self):
        area = SimpleNamespace(pois=[poi("Wenger's")], _cache={})
        self.assertEqual(places.Places().fill_hours(area, lambda p: True), 0)

    def test_one_call_answers_every_place_in_its_circle(self):
        g = Fake([hit("Wenger's", periods=WEEKDAYS), hit("Saravana Bhavan Restaurant", lat=28.6318, periods=WEEKDAYS),
                  hit("Hotel Alka", lat=28.6316, periods=WEEKDAYS)])
        pois = [poi("Wenger's"), poi("Saravana Bhavan", pt=(28.6317, 77.2167)), poi("Kwality", pt=(28.6316, 77.2168)),
                poi("Has hours", hours="24/7"), poi("shop", named=False), poi("Far away", pt=(28.7, 77.3)), poi("Bus stop", group="transit")]
        area = SimpleNamespace(pois=pois, _cache={"old": 1})
        self.assertEqual(g.fill_hours(area, lambda p: p[0] < 28.65), 2)
        self.assertEqual(len(g.centers), 1)
        self.assertEqual((pois[0]["hours"], pois[0]["google_hours"]), ("Mo 10:00-22:00", True))
        self.assertEqual(pois[1]["hours"], "Mo 10:00-22:00")
        self.assertIsNone(pois[2]["hours"], "Kwality isn't in Google's answer; Hotel Alka next door doesn't count")
        self.assertEqual(g.hours["Kwality|28.6316|77.2168"][0], "")
        self.assertEqual(area._cache, {})

    def test_a_far_match_is_not_used(self):
        g = Fake([hit("Wenger's", lat=28.6325, periods=WEEKDAYS)])  # ~110 m away
        pois = [poi("Wenger's")]
        g.fill_hours(SimpleNamespace(pois=pois, _cache={}), lambda p: True)
        self.assertIsNone(pois[0]["hours"])

    def test_circles_cover_the_trip_and_are_capped(self):
        g = Fake([])
        pois = [poi(f"Shop {i}", pt=(28.6 + i * 0.0005, 77.2167)) for i in range(200)]  # every 55 m for 11 km
        g.fill_hours(SimpleNamespace(pois=pois, _cache={}), lambda p: True)
        self.assertEqual(len(g.centers), places.MAX_CALLS)
        self.assertTrue(all(places.dist(a, b) > places.RADIUS_M for a, b in zip(g.centers, g.centers[1:])))

    def test_full_answers_leave_unmatched_places_for_later(self):
        g = Fake([hit(f"Other {i}") for i in range(20)])
        g.fill_hours(SimpleNamespace(pois=[poi("Wenger's")], _cache={}), lambda p: True)
        self.assertNotIn("Wenger's|28.6315|77.2167", g.hours)

    def test_cache_is_reused_and_failed_calls_are_retried(self):
        g = Fake([hit("Wenger's", periods=WEEKDAYS)], fail=1)
        area = lambda: SimpleNamespace(pois=[poi("Wenger's")], _cache={})
        self.assertEqual(g.fill_hours(area(), lambda p: True), 0)  # call failed: not cached
        self.assertEqual(g.fill_hours(area(), lambda p: True), 1)
        self.assertEqual(g.fill_hours(area(), lambda p: True), 1)  # from the cache, no third call
        self.assertEqual(len(g.centers), 2)

    def test_quota_exhausted_stops_calls_for_the_day(self):
        g = places.Places("key")
        err = urllib.error.HTTPError(places.URL, 429, "Too Many Requests", {}, None)
        with mock.patch.object(places.urllib.request, "urlopen", mock.Mock(side_effect=err)) as call:
            self.assertIsNone(g.nearby((28.6, 77.2)))
            self.assertIsNone(g.nearby((28.6, 77.2)))
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
