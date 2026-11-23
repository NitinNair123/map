import io
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

import server
import voice


def reply(obj):
    """A fake urlopen() result."""
    r = io.BytesIO(json.dumps(obj).encode())
    r.__enter__ = lambda *a: r
    r.__exit__ = lambda *a: None
    return r


class RulesTest(unittest.TestCase):
    def test_english_request(self):
        f = voice.rules("take me to sector 18 metro, stay on main roads, go past a pharmacy")
        self.assertEqual((f["intent"], f["destination"]), ("route", "sector 18 metro"))
        self.assertEqual(f["prefer"], ["pharmacy"])  # "metro" is the destination here, not a wish
        self.assertTrue(f["main_roads"])

    def test_hinglish_request(self):
        f = voice.rules("raat 11 baje botanical garden jaana hai gali se mat le jaana")
        self.assertEqual((f["destination"], f["time"], f["main_roads"]), ("botanical garden", "23:00", True))
        f = voice.rules("Hauz Khas paidal jaana hai near a metro station")
        self.assertEqual((f["destination"], f["mode"], f["prefer"]), ("hauz khas", "walk", ["transit"]))

    def test_times_and_modes(self):
        f = voice.rules("go to India Gate at 9:30 pm by car")
        self.assertEqual((f["destination"], f["time"], f["mode"]), ("india gate", "21:30", "car"))
        self.assertEqual(voice.rules("to Saket at 12 am")["time"], "00:00")
        self.assertEqual(voice.rules("to Saket at 25 baje")["time"], "")  # impossible times are dropped

    def test_police_is_a_wish_not_a_place(self):
        f = voice.rules("thane ke paas se le chalo")
        self.assertEqual((f["destination"], f["near_police"]), ("", True))

    def test_commands(self):
        self.assertEqual(voice.rules("bachao")["intent"], "sos")
        self.assertEqual(voice.rules("please help me")["intent"], "sos")
        self.assertEqual(voice.rules("help")["intent"], "help")  # how to use the app, never SOS
        self.assertEqual(voice.rules("cancel")["intent"], "cancel")
        self.assertEqual(voice.rules("hmm")["intent"], "unclear")
        self.assertEqual(voice.rules("")["intent"], "unclear")


class CleanTest(unittest.TestCase):
    def test_only_known_values_survive(self):
        f = voice.clean({"intent": "delete", "destination": "<script>x</script> Saket" + "a" * 200, "stops": ["CP", 5, ""] * 3,
                         "mode": "plane", "time": "7pm", "prefer": ["pharmacy", "casino"], "main_roads": "yes", "near_police": True,
                         "asks": ["feeling", "anything"]})
        self.assertEqual(f["intent"], "unclear")
        self.assertNotIn("<", f["destination"])
        self.assertLessEqual(len(f["destination"]), 80)
        self.assertEqual(f["stops"], ["CP", "CP", "CP"])
        self.assertEqual((f["mode"], f["time"], f["prefer"]), ("", "", ["pharmacy"]))
        self.assertEqual((f["main_roads"], f["near_police"], f["asks"]), (False, True, ["feeling"]))
        with self.assertRaises(ValueError):
            voice.clean(["not", "a", "form"])


class GeminiTest(unittest.TestCase):
    FORM = {"intent": "route", "destination": "Connaught Place", "stops": [], "mode": "walk", "time": "23:00",
            "prefer": ["pharmacy"], "main_roads": True, "near_police": False, "asks": []}

    def test_fills_form_through_interactions_api(self):
        sent = {}
        def fake(req, timeout):
            sent.update(url=req.full_url, body=json.loads(req.data), key=req.get_header("X-goog-api-key"))
            return reply({"status": "completed", "steps": [{"type": "user_input"}, {"type": "model_output",
                          "content": [{"type": "text", "text": json.dumps(self.FORM)}]}]})
        with mock.patch("urllib.request.urlopen", fake):
            f = voice.understand("mujhe CP jaana hai raat 11 baje", "k")
        self.assertEqual(f, self.FORM)
        self.assertTrue(sent["url"].endswith("/v1/interactions"))
        self.assertEqual((sent["body"]["model"], sent["body"]["store"], sent["key"]), (voice.MODEL, False, "k"))
        self.assertEqual(sent["body"]["response_format"]["schema"], voice.FORM)

    def test_failure_falls_back_to_rules(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(voice.understand("to Saket", "k"))
            self.assertEqual(voice.handle("to Saket", "k"), (voice.rules("to Saket"), "rules"))
        bad = reply({"steps": [{"type": "model_output", "content": [{"type": "text", "text": "not json"}]}]})
        with mock.patch("urllib.request.urlopen", return_value=bad):
            self.assertIsNone(voice.understand("to Saket", "k"))
        self.assertEqual(voice.handle("to Saket", "")[1], "rules")  # no key: no call at all


class SarvamTest(unittest.TestCase):
    def test_sends_clip_and_returns_text(self):
        sent = {}
        def fake(req, timeout):
            sent.update(url=req.full_url, body=req.data, key=req.get_header("Api-subscription-key"))
            return reply({"request_id": "x", "transcript": "mujhe connaught place jaana hai"})
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(voice.transcribe(b"OPUSDATA", "audio/webm", "s"), "mujhe connaught place jaana hai")
        self.assertEqual((sent["url"], sent["key"]), (voice.SARVAM_URL, "s"))
        for part in (b'name="mode"\r\n\r\ntranslit', b'name="model"\r\n\r\nsaaras:v3', b"OPUSDATA", b'filename="clip.webm"'):
            self.assertIn(part, sent["body"])

    def test_failure_is_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(voice.transcribe(b"x", "audio/webm", "s"))
        self.assertIsNone(voice.transcribe(b"x", "audio/webm", ""))


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [mock.patch.object(server.gemini, "LIVE", server.gemini.Gemini()), mock.patch.object(server, "SARVAM_KEY", "")]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for p in cls.patches:
            p.stop()

    def setUp(self):
        server.hits.clear()

    def post(self, body, kind):
        c = HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request("POST", "/api/voice", body, {"Content-Type": kind})
        r = c.getresponse()
        out = r.status, json.loads(r.read())
        c.close()
        return out

    def test_typed_request(self):
        code, out = self.post(json.dumps({"text": "walk to India Gate on main roads"}), "application/json")
        self.assertEqual(code, 200)
        self.assertEqual((out["by"], out["form"]["destination"], out["form"]["main_roads"]), ("rules", "india gate", True))

    def test_audio(self):
        self.assertEqual(self.post(b"clip", "audio/webm")[0], 503)  # no Sarvam key: the app offers typing
        with mock.patch.object(server, "SARVAM_KEY", "s"), mock.patch.object(voice, "transcribe", return_value="bachao") as t:
            code, out = self.post(b"clip", "audio/webm;codecs=opus")
        self.assertEqual((code, out["text"], out["form"]["intent"]), (200, "bachao", "sos"))
        self.assertEqual(t.call_args[0][:3], (b"clip", "audio/webm", "s"))
        with mock.patch.object(server, "SARVAM_KEY", "s"), mock.patch.object(voice, "transcribe", return_value=None):
            self.assertEqual(self.post(b"clip", "audio/webm")[0], 503)

    def test_rejects_bad_requests(self):
        # the server refuses on Content-Length and closes without reading the body, so a client still sending a real
        # oversize body can hit a broken pipe before it reads the 400 (seen on CI and Windows): send the headers only
        c = HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.putrequest("POST", "/api/voice")
        c.putheader("Content-Type", "audio/webm")
        c.putheader("Content-Length", str(voice.MAX_AUDIO + 1))
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        self.assertEqual(self.post(b"[]", "application/json")[0], 400)
        self.assertEqual(self.post(json.dumps({"nope": 1}), "application/json")[0], 400)

    def test_rate_limit(self):
        for _ in range(server.LIMITS["voice"][0]):
            self.post(json.dumps({"text": "cancel"}), "application/json")
        self.assertEqual(self.post(json.dumps({"text": "cancel"}), "application/json")[0], 429)


if __name__ == "__main__":
    unittest.main()
