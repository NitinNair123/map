"""Voice requests: Sarvam turns speech into text, Gemini fills one strict form, and rules check every field.

The AI never picks routes or talks about safety: it only fills in where to go and what the person wants on the way.
The route still comes from the rules in safety.py, and the app reads back only what the route measured.
Without keys, or when a service fails or is slow, rules() reads the text instead, so voice keeps working.
Keys: data/sarvam_key.txt (SARVAM_KEY) and the Gemini key gemini.py uses. Neither ever reaches the browser.

Safetipin's audit parameters (criteria.md) are what people ask for in their own words. What the person asks for maps
onto what the app can steer or measure: main roads and police steer the route (safety.py WISHES); open shops, transit,
pharmacies and hospitals steer it through "prefer"; lighting, walk path and openness are read back from the route's
measures; gender usage and "feeling" are not measured, and the app says so instead of guessing.
"""
import json
import re
import urllib.request
import uuid

SARVAM_URL = "https://api.sarvam.ai/speech-to-text"
# Interactions API on v1: generateContent and the gemini-2.5 models are legacy (google-genai 3.x, Nov 2026)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1/interactions"
MODEL = "gemini-3.5-flash-lite"
MAX_AUDIO = 600_000  # bytes: about 2 minutes of the browser's opus, far more than one request needs
MAX_TEXT = 300
GROUPS = ("pharmacy", "hospital", "food", "shop", "fuel", "transit")  # safety.PRIORITIES
INTENTS = ("route", "sos", "cancel", "help", "unclear")
ASKS = ("lighting", "walk_path", "openness", "people", "gender_usage", "feeling")  # Safetipin words we can only report on

FORM = {  # the one form Gemini fills; no free-text field, so it can't add claims of its own
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "destination": {"type": "string", "description": "Place name exactly as said, romanised; empty if none"},
        "stops": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "Named places to pass through, in order"},
        "mode": {"type": "string", "enum": ["", "walk", "bike", "car"]},
        "time": {"type": "string", "description": "Departure as 24-hour HH:MM, empty for now"},
        "prefer": {"type": "array", "items": {"type": "string", "enum": list(GROUPS)}, "description": "Kinds of places wanted along the way"},
        "main_roads": {"type": "boolean", "description": "Wants main roads, or to avoid lanes, galis, shortcuts or empty roads"},
        "near_police": {"type": "boolean"},
        "asks": {"type": "array", "items": {"type": "string", "enum": list(ASKS)},
                 "description": "Other things asked about: lit streets, footpath, open/visible surroundings, crowds, women around, feeling safe"},
    },
    "required": ["intent", "destination", "stops", "mode", "time", "prefer", "main_roads", "near_police", "asks"],
}
INSTRUCTION = (
    "You fill in a form for a walking-route app in Delhi NCR from one spoken request in English, Hindi or Hinglish. "
    "Only copy what the person said into the fields; never invent a place. intent is sos only for a cry for help or danger "
    "(bachao, help me, SOS); help means they ask how to use the app. 'Busy', 'crowded' or 'shops open' means prefer shop and "
    "food. 'Main road se', 'avoid gali', 'no shortcuts', 'not empty roads' means main_roads. Thana/police means near_police. "
    "Metro or bus means prefer transit, unless it is the destination. Raat/night times are PM.")


def transcribe(audio, mime, key, timeout=8):
    """Romanised text of one short clip ("mujhe connaught place jaana hai"), or None."""
    if not key or not audio:
        return None
    boundary = uuid.uuid4().hex
    fields = {"model": "saaras:v3", "mode": "translit", "language_code": "unknown"}  # translit: Latin script, so place search can read it
    codec = "mp4" if "mp4" in mime else "ogg" if "ogg" in mime else "webm"  # Chrome/Android records webm, Safari mp4
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.{codec}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    body += audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(SARVAM_URL, body, {"api-subscription-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(json.load(r).get("transcript") or "").strip()[:MAX_TEXT]
    except Exception as err:  # the reason only: never the audio or the words
        print("Sarvam:", type(err).__name__, getattr(err, "code", ""), flush=True)
        return None


def understand(text, key, timeout=4):
    """Gemini's filled form for the text, checked by clean(), or None."""
    if not key or not text:
        return None
    body = {"model": MODEL, "input": text, "system_instruction": INSTRUCTION, "store": False,  # not kept by Google after the reply
            "response_mime_type": "application/json", "response_format": {"type": "text", "mime_type": "application/json", "schema": FORM},
            "generation_config": {"thinking_level": "minimal", "max_output_tokens": 300}}
    req = urllib.request.Request(GEMINI_URL, json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            steps = json.load(r).get("steps") or []
        out = [s for s in steps if s.get("type") == "model_output"]
        return clean(json.loads("".join(c.get("text", "") for c in (out[-1] if out else {}).get("content", []) if c.get("type") == "text")))
    except Exception as err:
        print("Gemini voice:", type(err).__name__, getattr(err, "code", ""), flush=True)
        return None


def place(v):
    """A place name safe to search for: plain text, short."""
    return re.sub(r"[\x00-\x1f<>{}\[\]\\]", "", v)[:80].strip() if isinstance(v, str) else ""


def clean(f):
    """Only known values survive; anything else becomes the empty default. Raises on a non-object."""
    if not isinstance(f, dict):
        raise ValueError("form is not an object")
    t = f.get("time") if isinstance(f.get("time"), str) else ""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", t.strip())
    lst = lambda k: f.get(k) if isinstance(f.get(k), list) else []
    return {
        "intent": f.get("intent") if f.get("intent") in INTENTS else "unclear",
        "destination": place(f.get("destination")),
        "stops": [p for p in map(place, lst("stops")) if p][:5],
        "mode": f.get("mode") if f.get("mode") in ("walk", "bike", "car") else "",
        "time": f"{int(m[1]):02d}:{m[2]}" if m else "",
        "prefer": [g for g in GROUPS if g in lst("prefer")],
        "main_roads": f.get("main_roads") is True,
        "near_police": f.get("near_police") is True,
        "asks": [a for a in ASKS if a in lst("asks")],
    }


# ---- rules: used when there is no key, or the AI fails or is slow ----
WORDS = {  # word (regex) -> what it means; Hinglish as Sarvam's translit mode writes it
    "pharmacy": r"pharmacy|chemist|medical( store| shop)?|dawai|dawa",
    "hospital": r"hospital|aspatal|clinic",
    "food": r"restaurant|dhaba|food|khana|cafe",
    "shop": r"shops?|market|bazaa?r|dukaa?n|busy|crowded|bheed|log ho",
    "fuel": r"petrol( pump)?|fuel|pump",
    "transit": r"metro|bus stop|bus stand|station",
}
MODES = {"walk": r"walk(ing)?|paidal|pedal|on foot", "bike": r"bike|cycle|scooty|scooter", "car": r"car|cab|taxi|gaadi|auto"}
STOP_AT = r"(?:,|\.|\bvia\b|\bpast\b|\bthrough\b|\bat\b|\bby\b|\bavoid|\bwith\b|\bnear\b|\bon\b|\band\b|\bse\b|\btak\b|\bko\b|\bmain road|\braat\b|\bnight\b|\bjaa?na\b|\bchalo\b|\ble chalo\b|\bpahuncha|\bpaidal\b|\bwalk|\bat\s*\d|\d{1,2}\s*(?:am|pm|baje)|$)"


def rules(text):
    """The same form, from word lists. Understands far less than Gemini, but never makes anything up."""
    t = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    f = {"intent": "route", "destination": "", "stops": [], "mode": "", "time": "", "prefer": [], "main_roads": False,
         "near_police": False, "asks": []}
    if re.search(r"\b(sos|bachao|help me|save me|emergency)\b", t):
        return {**f, "intent": "sos"}
    if re.fullmatch(r"\s*(cancel|stop|ruko|rehne do|band karo)\s*", t):
        return {**f, "intent": "cancel"}
    if re.fullmatch(r"\s*help\s*", t):
        return {**f, "intent": "help"}
    td = re.sub(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)\b|\b(?:raat|night|subah|shaam|abhi|aaj|kal|paidal|pedal|on foot)\b", " ", t)  # times and modes aren't places
    m = (re.search(r"\b(?:take me to|go to|navigate to|route to|directions to|to)\s+(.+?)\s*" + STOP_AT, td)
         or re.search(r"(?:mujhe|hame|humein|mai|main)?\s*(.+?)\s+(?:jaa?na|chalo|le chalo|pahuncha)", td))
    dest = re.sub(r"^(?:mujhe|hame|humein|please|i want to go|i wanna go)\s+", "", m[1].strip()) if m else ""
    dest = re.sub(r"\s+(?:ke paas.*|se|tak|ko)$", "", re.sub(r"^(?:the|a)\s+", "", dest))
    f["destination"] = "" if re.fullmatch(r"(?:police|thana|thane|chowki)", dest) else place(dest)  # "near police" is a wish, not a place
    for g, w in WORDS.items():
        if re.search(rf"\b({w})\b", t.replace(f["destination"], " ") if f["destination"] else t):
            f["prefer"].append(g)
    for mode, w in MODES.items():
        if re.search(rf"\b({w})\b", t):
            f["mode"] = f["mode"] or mode
    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|baje)\b", t)
    if tm:
        h, unit = int(tm[1]), tm[3]
        if unit == "pm" or (unit == "baje" and h < 12 and re.search(r"\b(raat|night|shaam|evening)\b", t)):
            h = h % 12 + 12
        elif unit == "am":
            h %= 12
        f["time"] = f"{h:02d}:{tm[2] or '00'}"  # clean() drops an impossible time
    f["main_roads"] = bool(re.search(r"main road|highway|badi sadak|bade raste|avoid (?:lanes?|gali|galis|galiyan|shortcuts?|empty)|gali se (?:mat|nahi)|no shortcuts?|not empty", t))
    f["near_police"] = bool(re.search(r"\b(police|thana|thane|chowki)\b", t))
    f["asks"] = [a for a, w in (("lighting", r"\blit\b|light|roshni|andhera|dark"), ("walk_path", r"footpath|pavement|sidewalk"),
                                ("people", r"crowd|people|log\b|bheed"), ("gender_usage", r"\bwomen\b|ladies|auratein"))
                 if re.search(w, t)]
    if not f["destination"] and not (f["prefer"] or f["main_roads"] or f["near_police"]):
        f["intent"] = "unclear"
    return clean(f)


def handle(text, gemini_key):
    """(form, who filled it) for one request's text."""
    form = understand(text, gemini_key)
    return (form, "ai") if form else (rules(text), "rules")
