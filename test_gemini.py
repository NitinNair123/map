import unittest
from datetime import datetime
from types import SimpleNamespace

import gemini


def poi(name, pt=(28.6315, 77.2167), hours=None, group="food", named=True):
    return {"name": name, "pt": pt, "hours": hours, "group": group, "named": named}


class Fake(gemini.Gemini):
    def __init__(self, *answers):
        super().__init__("key")
        self.answers, self.prompts = list(answers), []

    def ask(self, prompt, max_tokens):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class TestGemini(unittest.TestCase):
    def test_parse_json(self):
        self.assertEqual(gemini.parse_json('Here:\n```json\n{"0": "24/7"}\n```'), {"0": "24/7"})
        self.assertEqual(gemini.parse_json("[]"), [])
        self.assertIsNone(gemini.parse_json("no idea"))

    def test_no_key_does_nothing(self):
        g = gemini.Gemini()
        area = SimpleNamespace(pois=[poi("Wenger's")], _cache={})
        self.assertEqual(g.fill_hours(area, lambda p: True, "x"), 0)
        self.assertEqual(g.find_events((28.6, 77.2), (28.61, 77.21), datetime(2026, 10, 30, 20), "x", lambda q: None), [])

    def test_fills_only_named_untimed_places_near_the_trip(self):
        g = Fake({"0": "Mo-Su 10:30-22:30", "1": "not sure"})
        pois = [poi("Wenger's"), poi("Saravana Bhavan"), poi("Has hours", hours="24/7"), poi("shop", named=False),
                poi("Far away", pt=(28.7, 77.3)), poi("Bus stop", group="transit")]
        area = SimpleNamespace(pois=pois, _cache={"old": 1})
        self.assertEqual(g.fill_hours(area, lambda p: p[0] < 28.65, "Connaught Place, New Delhi"), 1)
        self.assertEqual(pois[0]["hours"], "Mo-Su 10:30-22:30")
        self.assertIsNone(pois[1]["hours"])  # an answer that isn't opening hours is dropped
        self.assertEqual(len(g.prompts), 1)
        self.assertNotIn("Far away", g.prompts[0])
        self.assertEqual(area._cache, {})  # re-rate with the new hours
        # the next trip uses the saved answers without asking again, even in a fresh area
        again = SimpleNamespace(pois=[poi("Wenger's"), poi("Saravana Bhavan")], _cache={})
        self.assertEqual(g.fill_hours(again, lambda p: True, "x"), 1)
        self.assertEqual(len(g.prompts), 1)

    def test_failed_call_is_asked_again(self):
        g = Fake(None, {"0": "Mo-Sa 10:00-21:00"})
        area = SimpleNamespace(pois=[poi("Wenger's")], _cache={})
        self.assertEqual(g.fill_hours(area, lambda p: True, "x"), 0)
        self.assertEqual(g.fill_hours(area, lambda p: True, "x"), 1)

    def test_events_need_a_findable_place(self):
        g = Fake([{"place": "Janpath and Tolstoy Marg crossing", "radius_m": 200, "what": "VIP convoy barricades"},
                  {"place": "Somewhere vague", "what": "protest"},
                  {"what": "no place given"},
                  {"place": "India Gate", "radius_m": 99999, "what": "Parade"}])
        found = {"Janpath and Tolstoy Marg crossing": (28.6280, 77.2190), "India Gate": (28.6129, 77.2295)}
        when = datetime(2026, 10, 30, 20)
        alerts = g.find_events((28.63, 77.21), (28.61, 77.23), when, "CP to India Gate", found.get)
        self.assertEqual([h["place"] for h in alerts], ["Janpath and Tolstoy Marg crossing", "India Gate"])
        self.assertEqual([h["local"] for h in alerts], [True, False])
        self.assertEqual(alerts[1]["r"], 3000)
        # local alerts cost points near them around that time; area-wide ones only warn
        self.assertEqual(g.penalty((28.6281, 77.2191), when), gemini.PENALTY)
        self.assertEqual(g.penalty((28.6281, 77.2191), datetime(2026, 10, 31, 9)), 0)
        self.assertEqual(g.penalty((28.6130, 77.2296), when), 0)
        # cached for the same area and hour
        self.assertEqual(g.find_events((28.63, 77.21), (28.61, 77.23), when, "x", found.get), alerts)
        self.assertEqual(len(g.prompts), 1)


if __name__ == "__main__":
    unittest.main()
