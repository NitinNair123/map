"""SafeRoute AI Studio: one page on this PC to start, pause and watch every stage of training.

Stages run as separate processes. Pause stops one; the build, the route pairs and the Qwen labels carry on where they
stopped when started again, while the trainers (minutes each) start over. "Run everything" goes through the stages in
order and stops at the first pause or failure.

The page has one Play/Pause button. A watchdog restarts a stage that crashed, or a build that has finished nothing for
20 minutes, by itself (5 times an hour at most, with growing pauses); a stage you paused is never restarted.

The picture shows the real model: parameter groups on the left feed the street and route models, which feed stage 2.
They are gradient-boosted trees and a learned weighting, not a neural network. Lines grow with how much each group
helped on areas the model never saw, once a training run has measured it.

Usage: py ml/studio.py     then open http://localhost:8770 (this PC only)
"""
import json
import os
import re
import signal
import csv
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "ml", "out")
LOGS = os.path.join(OUT, "studio")
STAGES = [  # id, name, command, what it does
    ("build", "Build the dataset", ["ml/dataset.py", "--all"], "Streets and their 104 parameters, block by block. Resumes."),
    ("train", "Base training: streets", ["ml/train.py"], "Lit, open places, buses and traffic, scored on unseen areas."),
    ("pairs", "Make route pairs", ["ml/pairs.py", "--n", "2000"], "Two real routes per simulated trip, up to 2,000. Resumes."),
    ("qwen", "Qwen labels the pairs", ["ml/label_llm.py"], "A language model picks the safer route of each pair: an Ollama cloud model, 6 at a time, when Ollama is signed in with cloud on; else local Qwen. Resumes."),
    ("claude", "Claude labels the pairs", ["ml/label_claude.py"],
     "Claude picks the safer route of every pair: its hand answers, and its written Safetipin-based criteria for the rest. Re-runs on new pairs."),
    ("ab", "A/B answers page", ["ml/rate_pairs.py"], "People answer the same pairs at http://localhost:8765."),
    ("rank", "Train the route model (Qwen + Claude)", ["ml/rank_v2.py"],
     "Learns from Qwen's and Claude's answers (people's too, once any exist), with safety-sign rules; scored on unseen areas."),
    ("sim", "Stage 2: simulated walks", ["ml/sim.py", "--trips", "500"], "Races the route choices through simulated trips."),
    ("world", "Stage 2: virtual world", ["ml/world.py", "--episodes", "3000"],
     "A walker is rewarded for arriving and punished for events on the real street graph. Event chances are assumptions. Resumes."),
]
CHAIN = ["build", "train", "pairs", "qwen", "claude", "rank", "sim", "world"]  # the A/B page is a server, started on its own
GROUPS = {  # the parameter groups of the spec; a test checks every model input sits in exactly one
    "Street itself": "highway sidewalk length lanes maxspeed unpaved rough underpass overbridge gap_m dead_ends n_crossing degree",
    "Lighting and cameras": "n_lamp lamps_per_100m n_cctv lamps_100 lamps_250 cctv_250 cctv_500 d_cctv d_lamp nb_lamps_per_100m nightlight",
    "Help nearby": "police_m n_guard n_sos_phone d_guard d_sos d_hospital d_pharmacy d_fuel",
    "People around": "places_food places_shop places_pharmacy places_hospital places_fuel places_other places_total places_250 places_500 d_place "
                     "d_food nb_places places_mapped_500m n_nightlife nightlife_250 nightlife_500 d_nightlife",
    "Transit": "gtfs_stops n_transit transit_250 transit_500 d_transit",
    "Hazards and reports": "n_blackspot n_waterlogging d_blackspot d_waterlogging incident_hits report_points",
    "Beside the road": "buildings_ha bld_100 bld_500 zone_brownfield zone_cemetery zone_construction zone_farmland zone_greenfield "
                       "zone_industrial zone_park zone_quarry zone_railway zone_scrub zone_wood wc_built_50 wc_tree_50 wc_field_50 "
                       "wc_bare_50 wc_water_50 wc_built_250 wc_tree_250 wc_field_250 wc_bare_250 wc_water_250 wall_m_50 fence_m_50 "
                       "water_m_50 water_m_150 parking_100 toilets_250 vending_250 shelter_250",
    "Street network": "road_km_300 main_share_300 foot_share_300 service_share_300 junctions_300 dead_end_share_300 d_main nb_main_share",
    "Time": "hour weekend daylight gtfs_deps",
}
ROUTE_GROUPS = {  # route measures of ml/pairs.py -> the same groups
    "lit_pct": "Lighting and cameras", "dark_pct": "Lighting and cameras", "no_light_data_pct": "Lighting and cameras", "cctv": "Lighting and cameras",
    "police_m": "Help nearby", "help_gap_m": "Help nearby", "pharmacy_found": "Help nearby", "hospital_found": "Help nearby", "fuel_found": "Help nearby",
    "open_now": "People around", "nothing_open_m": "People around", "food_found": "People around", "shop_found": "People around",
    "other_found": "People around", "transit_found": "Transit", "main_pct": "Street network", "isolated_m": "Street network",
    "length_m": "Street itself", "unrated_pct": "Street itself", "extra_length": "Street itself",  # extra_length: route model v2
}
HEADS = [("lit", "Lit at night"), ("open_share", "Places open"), ("bus_per_hour", "Buses passing"), ("traffic_ratio", "Traffic"), ("route", "Safer route choice")]
PROGRESS = re.compile(r"\[?(\d+)\s*(?:of|/)\s*(\d+)")


def started_at(pid):
    """When process pid started (epoch s), or None if it isn't running. Lets a restarted Studio find stages it started before."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        code, times = ctypes.c_ulong(), [ctypes.c_ulonglong() for _ in range(4)]
        ok = k.GetExitCodeProcess(h, ctypes.byref(code)) and k.GetProcessTimes(h, *map(ctypes.byref, times))
        k.CloseHandle(h)
        return times[0].value / 1e7 - 11644473600 if ok and code.value == 259 else None  # 259 = still running
    try:  # Linux: start time from /proc (elsewhere a stage started by an earlier Studio just isn't found)
        ticks = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19])
        boot = time.time() - float(open("/proc/uptime").read().split()[0])
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


class Runner:
    def __init__(self):
        self.procs, self.paused, self.lock = {}, set(), threading.Lock()
        self.wanted, self.restarts, self.failed_at, self.notes = set(), {}, {}, {}  # watchdog: stages that should be running, and its record
        os.makedirs(LOGS, exist_ok=True)
        self.state_path = os.path.join(LOGS, "state.json")
        try:
            self.last = json.load(open(self.state_path))
        except (OSError, ValueError):
            self.last = {}
        self.last.setdefault("pids", {})
        self.wanted |= {sid for sid, *_ in STAGES if sid != "ab" and self.orphan(sid)}  # carried on from an earlier Studio

    def save(self):
        json.dump(self.last, open(self.state_path, "w"))

    def orphan(self, sid):
        """A stage started by an earlier Studio that is still running: matched by pid and start time, so a reused pid never counts."""
        pid, when = self.last["pids"].get(sid, (None, None))
        t = pid and started_at(pid)
        return pid if t and abs(t - when) < 10 else None

    def start(self, sid):
        stage = next(s for s in STAGES if s[0] == sid)
        with self.lock:
            if sid in self.procs and self.procs[sid].poll() is None or self.orphan(sid):
                return
            self.paused.discard(sid)
            self.wanted.add(sid)
            log = open(os.path.join(LOGS, f"{sid}.log"), "a", encoding="utf-8")
            log.write(f"\n=== started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log.flush()
            self.procs[sid] = subprocess.Popen([sys.executable, *stage[2]], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                               env={**os.environ, "PYTHONUNBUFFERED": "1"})
            self.last["pids"][sid] = (self.procs[sid].pid, time.time())
            self.save()

    def pause(self, sid):
        with self.lock:
            self.wanted.discard(sid)  # the watchdog leaves a stage you paused alone
            self.notes.pop(sid, None)
            p = self.procs.get(sid)
            if p and p.poll() is None:
                self.paused.add(sid)
                p.terminate()
            elif self.orphan(sid):
                os.kill(self.orphan(sid), signal.SIGTERM)  # on Windows this ends the process, like terminate()
                self.last[sid] = "paused"
                self.save()

    def status(self, sid):
        p = self.procs.get(sid)
        if p is None:
            return "running" if self.orphan(sid) else self.last.get(sid, "idle")
        if p.poll() is None:
            return "running"
        s = "paused" if sid in self.paused else "done" if p.returncode == 0 else "failed"
        if self.last.get(sid) != s:
            self.last[sid] = s
            self.save()
        return s

    def run_all(self):
        def chain():
            for sid in CHAIN:
                if sid == "qwen" and not ollama()["model"]:
                    continue  # no Qwen yet: the routes learn from people's answers only
                self.start(sid)
                while self.status(sid) == "running" or (self.status(sid) == "failed" and sid in self.wanted and self.can_retry(sid)):
                    time.sleep(2)  # (a failed stage is being restarted by the watchdog)
                if self.status(sid) != "done":
                    return
        threading.Thread(target=chain, daemon=True).start()

    def pause_all(self):
        for sid, *_ in STAGES:
            self.pause(sid)

    def any_running(self):
        return any(self.status(sid) == "running" for sid, *_ in STAGES)

    def can_retry(self, sid):
        now = time.time()
        self.restarts[sid] = [x for x in self.restarts.get(sid, []) if now - x < 3600]
        return len(self.restarts[sid]) < MAX_RESTARTS

    def heal(self):
        """One pass of the watchdog: restart a wanted stage that crashed, or a build that has gone HUNG_S without finishing
        anything while still reporting (a stuck download), after a pause that grows with each restart."""
        now = time.time()
        for sid, *_ in STAGES:
            if sid not in self.wanted or sid == "ab":
                continue
            status, why = self.status(sid), None
            if status == "failed":
                why = "it stopped with an error"
            elif status == "running" and sid == "build" and self.hung() and sid in self.procs:
                self.procs[sid].terminate()  # not a pause: it comes back below once it has exited
                why = f"it finished nothing for {HUNG_S // 60} min"
                time.sleep(1)
                status = self.status(sid)
            if why is None or status == "running":
                self.failed_at.pop(sid, None)
                continue
            if status == "done" or sid not in self.wanted:
                continue
            if not self.can_retry(sid):
                self.notes[sid] = f"Gave up after {MAX_RESTARTS} automatic restarts in an hour ({why}). The log shows the error."
                self.wanted.discard(sid)
                continue
            n = len(self.restarts[sid])
            first = self.failed_at.setdefault(sid, now)
            if now - first < min(30 * 2 ** n, 600):
                continue
            self.restarts[sid].append(now)
            self.failed_at.pop(sid, None)
            self.notes[sid] = f"Restarted by itself ({n + 1} of {MAX_RESTARTS} this hour) because {why}."
            with open(os.path.join(LOGS, f"{sid}.log"), "a", encoding="utf-8") as log:
                log.write(f"\n=== automatic restart {n + 1}/{MAX_RESTARTS}: {why} ===\n")
            self.start(sid)

    def hung(self):
        try:
            L = json.load(open(os.path.join(LOGS, "build_live.json")))
        except (OSError, ValueError):
            return False
        now = time.time()
        return L.get("phase") != "Done" and now - L["updated"] < 15 and now - L["last_event"] > HUNG_S

    def watch(self):
        def loop():
            while True:
                try:
                    self.heal()
                except Exception as err:  # the watchdog itself must never die
                    print(f"watchdog: {err!r}", flush=True)
                time.sleep(15)
        threading.Thread(target=loop, daemon=True).start()


def tail(sid, n=12, size=6000):
    path = os.path.join(LOGS, f"{sid}.log")
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - size))
        lines = f.read().decode("utf-8", "replace").splitlines()
    return lines[-n:]


def progress(lines):
    for line in reversed(lines):
        if line.startswith("==="):
            return None
        m = PROGRESS.search(line)
        if m and int(m.group(2)):
            return min(1.0, int(m.group(1)) / int(m.group(2)))
    return None


def ollama():
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
        return {"running": True, "model": next((n for n in names if n.startswith("qwen3")), None)}
    except (OSError, ValueError):
        return {"running": False, "model": None}


def network():
    """The 104 inputs by group, the models, and what training measured: each input's usefulness on unseen areas and
    each group's share in each model. Everything is 0 until a training run has measured it."""
    group_of = {c: g for g, cols in GROUPS.items() for c in cols.split()}
    links, useful = {}, {}
    try:
        for m in json.load(open(os.path.join(OUT, "metrics.json"))):
            for col, drop in (m.get("most_useful_parameters") or {}).items():
                if drop > 0 and col in group_of:
                    k = (group_of[col], m["head"])
                    links[k] = links.get(k, 0) + drop
                    useful[col] = max(useful.get(col, 0), drop)
    except (OSError, ValueError, TypeError):
        pass
    try:
        v2 = os.path.join(OUT, "route_ranker_v2.json")
        if os.path.exists(v2):
            r = json.load(open(v2))
            w = {**r["day"], **{f"{k}_dark": v for k, v in r["night"].items()}}
        else:
            w = json.load(open(os.path.join(OUT, "route_ranker.json")))["weights"]
        for col, v in w.items():
            g = ROUTE_GROUPS.get(col.removesuffix("_dark"))
            if g:
                links[(g, "route")] = links.get((g, "route"), 0) + abs(v)
    except (OSError, ValueError, KeyError):
        pass
    top, best = {}, max(useful.values(), default=0)
    for (g, h), v in links.items():
        top[h] = max(top.get(h, 0), v)
    return {"groups": [{"name": g, "params": [{"name": c, "useful": round(useful.get(c, 0) / best, 3) if best else 0} for c in cols.split()]}
                       for g, cols in GROUPS.items()],
            "heads": [{"id": h, "name": n, "measured": h in top} for h, n in HEADS],
            "route_groups": sorted(set(ROUTE_GROUPS.values())),
            "links": [{"from": g, "to": h, "w": round(v / top[h], 3)} for (g, h), v in links.items() if top.get(h)]}


BLOCK = re.compile(r"\[(\d+)/(\d+)\] block \S+: (\d+) streets, (\d+) with a lit label")
TRIAL = re.compile(r"^(\w+) trial (\d+) of (\d+): score ([\d.]+)")
WORLD = re.compile(r"^\[(\d+)/(\d+)\] episodes: mean reward (-?[\d.]+)")
PROBLEM = re.compile(r"Traceback|Error|failures in a row|skipped \(")


def this_run(sid):
    """Log lines of the stage's latest run."""
    lines = tail(sid, 4000, 400000)
    starts = [i for i, l in enumerate(lines) if l.startswith("=== started")]
    return lines[starts[-1] + 1:] if starts else lines


def activity():
    """What each stage is doing right now, read from its log: the picture moves only on these real events."""
    build = {"blocks": 0, "of": 0, "streets": 0, "lit": 0}
    for line in this_run("build"):
        m = BLOCK.search(line)
        if m:
            build.update(blocks=int(m[1]), of=int(m[2]), streets=build["streets"] + int(m[3]), lit=build["lit"] + int(m[4]))
    build["blocks_on_disk"] = len([f for f in os.listdir(os.path.join(ROOT, "ml", "data", "blocks")) if f.endswith(".edges.csv.gz")]) \
        if os.path.isdir(os.path.join(ROOT, "ml", "data", "blocks")) else 0
    train = {"head": None, "curve": [], "of": 0}
    for line in this_run("train"):
        if line.startswith("Training "):
            train = {"head": line.split()[1].rstrip(":"), "curve": [], "of": 0}
        m = TRIAL.match(line)
        if m and m[1] == train["head"]:
            train["curve"].append(float(m[4]))
            train["of"] = int(m[3])
    world = {"curve": [], "of": 0}
    for line in this_run("world"):
        m = WORLD.match(line)
        if m:
            world = {"curve": world["curve"] + [float(m[3])], "of": int(m[2]), "done": int(m[1])}
    problems = {sid: [l for l in this_run(sid) if PROBLEM.search(l)][-3:] for sid, *_ in STAGES}
    return {"build": build, "train": train, "world": world, "problems": {k: v for k, v in problems.items() if v}}


SOURCE_NAMES = {"": "OpenStreetMap", "nightlights": "NASA night lights", "mapillary": "Mapillary", "geoapify": "Geoapify",
                "places": "Overture places", "buildings": "Overture buildings", "roads": "Overture streets",
                "osm_extra": "OSM walls, drains, parking (Overpass)"}
MAX_RESTARTS = 5  # automatic restarts of one stage per hour
HUNG_S = 1200  # a running build that has finished nothing for this long is stuck, not slow
STALL_S = 180  # nothing finished for this long while the build runs: say so


def live(running):
    """The build's real counters (ml/dataset.py writes them every second) with rates, ETA and a stall check. None
    before the first build with live counters has run."""
    try:
        L = json.load(open(os.path.join(LOGS, "build_live.json")))
    except (OSError, ValueError):
        return None
    now, secs = time.time(), L.get("block_seconds") or []
    avg = sum(secs) / len(secs) if secs else None
    left = max(0, L["blocks_total"] - L["blocks_on_disk"])
    elapsed = now - L["first_block_at"] if L.get("first_block_at") else None
    waits = sorted(({"source": SOURCE_NAMES.get(k, k), "box": box, "for": round(now - t)} for k, s in L["sources"].items()
                    for box, t in s["busy"].items()), key=lambda x: -x["for"])
    sources = [{"name": SOURCE_NAMES.get(k, k), **{f: s[f] for f in ("requests", "ok", "failed", "skipped", "bytes", "items")},
                "avg_s": round(s["seconds"] / (s["ok"] + s["failed"]), 1) if s["ok"] + s["failed"] else None,
                "waiting": len(s["busy"]), "last_error": s["last_error"], "id": k} for k, s in L["sources"].items()]
    stalled = None
    if running:
        if now - L["updated"] > 15:
            stalled = f"The build hasn't reported for {now - L['updated']:.0f} s. It may be frozen: Pause it and Start it again."
        elif now - L["last_event"] > STALL_S:
            stalled = f"Nothing has finished for {(now - L['last_event']) / 60:.0f} min." + \
                      (f" Still waiting on {waits[0]['source']} ({waits[0]['for'] // 60} min). A slow or blocked connection can do this." if waits else "")
    cur = L.get("current") if running else None
    waits = waits if running else []  # a stopped build waits on nothing
    return {"running": running, "phase": L["phase"] if running else "Stopped" if L["phase"] != "Done" else "Done",
            "updated_ago": round(now - L["updated"]), "last_event_ago": round(now - L["last_event"]),
            "blocks_total": L["blocks_total"], "blocks_on_disk": L["blocks_on_disk"], "blocks_built": L["blocks_built"],
            "blocks_failed": L["blocks_failed"], "streets": L["streets"], "lit": L["lit"],
            "streets_per_s": round(L["streets"] / elapsed, 2) if elapsed and L["streets"] else 0,
            "block_avg_s": round(avg) if avg else None, "eta_s": round(left * avg) if avg and running else None,
            "current": {"n": cur["n"], "for": round(now - cur["started"])} if cur else None, "prefetch": L.get("prefetch"),
            "sources": sources, "waits": waits, "stalled": stalled}


def reports():
    out = {}
    for name in ("report.md", "route_report.md", "sim_report.md", "world_report.md"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            out[name] = open(path, encoding="utf-8").read()[:20000]
    return out


def route_model():
    """What the route model v2 learned from and how it scored, for the 'Route model training' card. None before its first run."""
    try:
        r = json.load(open(os.path.join(OUT, "route_ranker_v2.json")))
    except (OSError, ValueError):
        return None
    ids = {"people": set(), "qwen": set(), "claude_hand": set(), "claude_rubric": set()}  # distinct pairs with an A or B answer
    for name, key in (("labels_llm.csv", "qwen"), ("labels_claude.csv", None), ("labels_human.csv", "people")):
        path = os.path.join(ROOT, "ml", "data", name)
        if not os.path.exists(path):
            continue
        for row in csv.DictReader(open(path, encoding="utf-8")):
            if row.get("safer") in ("A", "B"):
                ids.setdefault(key or row.get("source") or "claude_rubric", set()).add(row["id"])
    counts = {k: len(v) for k, v in ids.items()}
    m = r.get("metrics", {})
    return {"counts": counts, "agree": m.get("qwen_agrees_with_claude"), "held": m.get("held_out_areas", {}),
            "checks": m.get("sanity_checks", {}), "left_out": m.get("qwen_and_claude_disagree_left_out", 0),
            "updated": os.path.getmtime(os.path.join(OUT, "route_ranker_v2.json")),
            "top": sorted(((k, r["day"][k], r["night"][k]) for k in r["features"]), key=lambda t: -abs(t[1]) - abs(t[2]))[:6]}


RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    def send(self, body, kind="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def do_GET(self):
        if self.path == "/":
            return self.send(PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            stages = []
            for sid, name, _, what in STAGES:
                lines = tail(sid)
                stages.append({"id": sid, "name": name, "what": what, "status": RUNNER.status(sid), "progress": progress(lines), "tail": lines,
                               "note": RUNNER.notes.get(sid), "restarts": len(RUNNER.restarts.get(sid, []))})
            return self.send(json.dumps({"stages": stages, "ollama": ollama(), "network": network(), "activity": activity(), "reports": reports(), "route": route_model(),
                                         "live": live(RUNNER.status("build") == "running"), "now": time.time(),
                                         "running": RUNNER.any_running()}))
        self.send("not found", "text/plain", 404)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.send("forbidden", "text/plain", 403)  # another web page can't start jobs on this PC
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 500)) or b"{}")
        except ValueError:
            return self.send("bad request", "text/plain", 400)
        sid = body.get("id")
        if self.path in ("/api/start", "/api/pause") and sid not in {s[0] for s in STAGES}:
            return self.send("unknown stage", "text/plain", 400)
        if self.path == "/api/start":
            RUNNER.start(sid)
        elif self.path == "/api/pause":
            RUNNER.pause(sid)
        elif self.path == "/api/run-all":
            RUNNER.run_all()
        elif self.path == "/api/pause-all":
            RUNNER.pause_all()
        else:
            return self.send("not found", "text/plain", 404)
        self.send("{}")

    def log_message(self, *a):
        pass


PORT = 8770
PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>SafeRoute AI Studio</title>
<style>
:root{--bg:#0b1020;--panel:#141a2e;--line:#26304d;--ink:#e8ecf7;--quiet:#94a0bf;--accent:#5b8cff;--good:#35c48b;--warn:#f0b43c;--bad:#ef5b5b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:0}main{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(320px,1fr);gap:16px;padding:16px 20px}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
button{font:inherit;border:1px solid var(--line);background:#1c2440;color:var(--ink);border-radius:8px;padding:7px 14px;cursor:pointer}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.big{font-size:17px;font-weight:600;padding:9px 26px;min-width:150px}button.big.pausing{background:var(--warn);border-color:var(--warn);color:#1a1300}
.fixed{color:var(--warn);font-size:12px;margin-top:4px}
canvas{width:100%;height:600px;display:block}@media(max-width:600px){canvas{height:480px}}
.now{font-size:14px;margin:0 0 6px;min-height:20px}.problem{color:var(--bad);font-size:12px;margin-top:4px;white-space:pre-wrap}.stage{border-top:1px solid var(--line);padding:10px 0}.stage:first-child{border-top:0}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between}.name{font-weight:600}.what{color:var(--quiet);font-size:13px}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--quiet)}
.running{color:var(--accent);border-color:var(--accent)}.done{color:var(--good);border-color:var(--good)}.failed{color:var(--bad);border-color:var(--bad)}.paused{color:var(--warn);border-color:var(--warn)}
.bar{height:4px;background:var(--line);border-radius:4px;margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent);width:0}
pre{white-space:pre-wrap;font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--quiet);margin:6px 0 0;max-height:150px;overflow:auto}
.note{color:var(--quiet);font-size:13px}
.stall{background:#3a1820;border:1px solid var(--bad);color:#ffd0d0;border-radius:8px;padding:8px 10px;margin:0 0 8px;font-weight:600}
.phase{font-size:14px;margin-bottom:8px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:10px}
.stat{background:#10162a;border:1px solid var(--line);border-radius:8px;padding:6px 9px}.stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums}.stat span{color:var(--quiet);font-size:12px}
table.src{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}table.src th{color:var(--quiet);font-weight:500;text-align:right;padding:3px 6px}
table.src td{text-align:right;padding:3px 6px;border-top:1px solid var(--line)}table.src td:first-child,table.src th:first-child{text-align:left}
td.bad{color:var(--bad)}td.wait{color:var(--warn)}.flashrow{animation:fr 1.2s}@keyframes fr{from{background:#1e3a2a}to{background:transparent}}
.tablewrap{overflow-x:auto}details summary{cursor:pointer;color:var(--quiet);font-size:13px}
</style>
<header><h1>SafeRoute AI Studio</h1><div><span id=qwen class=pill></span>
<button id=toggle class="primary big" onclick="toggleAll()">Play</button></div></header>
<main><section><div class=card><div id=now class=now>Loading...</div><div id=live></div><canvas id=net></canvas>
<p class=note>Each small dot is one of the 104 things measured about every street, coloured by group. They feed the four street models
and the route model in the middle, which feed stage 2 on the right. Nothing here moves on a timer: dots fire into a group only when a download for it really finishes (red when one fails),
every dot flashes when the build saves a block of streets, lines fire into a model each time training tries a setting, and the curve below is that model's real score on areas it
never saw. After training, a dot grows with how much it helped. The models are gradient-boosted trees and a learned weighting, not a neural network.</p></div>
<div class=card style="margin-top:16px"><h3 style="margin:0 0 8px">Route model training: Qwen + Claude</h3><div id=route class=note>Not trained yet: press Play, or Start on "Claude labels the pairs" then "Train the route model".</div></div>
<div class=card style="margin-top:16px"><h3 style="margin:0 0 8px">Results</h3><div id=reports class=note>No results yet.</div></div></section>
<section class=card id=stages></section></main>
<script>
const $=s=>document.querySelector(s);let S=null,pulses=[],t0=performance.now();
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function toggleAll(){post(S&&S.running?'/api/pause-all':'/api/run-all')}
async function post(u,b){await fetch(u,{method:'POST',body:JSON.stringify(b||{})});load()}
async function load(){try{S=await(await fetch('/api/state')).json()}catch(e){$('#now').textContent='The Studio isn\'t answering. Is "Start SafeRoute AI Studio" still open?';return}render();renderLive();renderRoute();events();$('#now').textContent=nowText()}
function renderRoute(){const R=S.route,el=$('#route');if(!R)return;const c=R.counts,h=R.held||{},pct=v=>v==null?'–':Math.round(v*100)+'%';
 const st=(v,l)=>`<div class=stat><b>${v}</b><span>${l}</span></div>`,ag=x=>x&&x.agree!=null?pct(x.agree)+` <span style="font-size:12px">of ${x.n}</span>`:'–';
 const chk=Object.entries(R.checks||{}).map(([k,v])=>`<span class="pill ${v.ok?'done':'failed'}" title="day ${v.day}, night ${v.night}">${v.ok?'✓':'✗'} ${esc(k)}</span>`).join(' ');
 el.innerHTML=`<p style="margin:0 0 8px">Every run, Qwen and Claude each pick the safer route of every pair; the model learns from both
 (people's answers count most, once anyone answers on the A/B page). Pairs where Qwen and Claude disagree are left out.</p>
 <div class=stats>${st(c.qwen,'pairs Qwen answered')}${st(c.claude_hand,'pairs Claude judged by hand')}${st(c.claude_rubric,"pairs by Claude's written criteria")}${st(c.people,'pairs people answered')}
 ${st(pct(R.agree),'Qwen agrees with Claude')}${st(R.left_out,'left out (they disagree)')}</div>
 <div class=stats>${st(ag(h.vs_qwen),'matches Qwen, unseen areas')}${st(ag(h.vs_claude_hand),"matches Claude's hand calls")}${st(ag(h.vs_people),'matches people')}${st(pct(h.baseline_always_the_shorter_route),'always-shorter baseline')}</div>
 <div style="margin:4px 0 8px">Sanity checks: ${chk}</div>
 <div>Biggest weights (day / night): ${R.top.map(([k,d,n])=>`${esc(k)} ${d>0?'+':''}${d.toFixed(1)} / ${n>0?'+':''}${n.toFixed(1)}`).join(' · ')}</div>
 <p style="margin:8px 0 0">Trained ${dur(S.now-R.updated)} ago. These are Qwen's and Claude's judgments from measured numbers, not people's, and not proof a route is safe.</p>`}
const dur=s=>s==null?'–':s<90?Math.round(s)+' s':s<5400?Math.round(s/60)+' min':s<172800?(s/3600).toFixed(1)+' h':(s/86400).toFixed(1)+' days';
const size=b=>b<1e6?Math.round(b/1e3)+' kB':(b/1e6).toFixed(1)+' MB';
let liveRows={};
function renderLive(){const L=S.live,el=$('#live');
 if(!L){el.innerHTML='<p class=note>Live numbers appear here once the build runs.</p>';return}
 const st=(v,l)=>`<div class=stat><b>${v}</b><span>${l}</span></div>`,w=L.waits;
 const rows=L.sources.map(s=>{const was=liveRows[s.id],fl=was&&(s.ok>was.ok||s.failed>was.failed);liveRows[s.id]=s;
  const wt=w.filter(x=>x.source==s.name);
  return `<tr class="${fl?'flashrow':''}"><td>${esc(s.name)}</td><td>${s.requests}</td><td>${s.ok}</td><td class="${s.failed?'bad':''}">${s.failed}${s.skipped?` <span title="refused after 3 failures in a row">+${s.skipped} skipped</span>`:''}</td>
   <td>${s.items.toLocaleString()}</td><td>${size(s.bytes)}</td><td>${s.avg_s==null?'–':s.avg_s+' s'}</td><td class="${wt.length?'wait':''}">${wt.length?'for '+dur(wt[0].for):'–'}</td></tr>`}).join('');
 const err=L.sources.filter(s=>s.last_error).map(s=>`${s.name}: ${s.last_error}`);
 el.innerHTML=(L.stalled?`<div class=stall>⚠ ${esc(L.stalled)}</div>`:'')+
  `<div class=phase><b>${esc(L.phase)}</b>${L.running?` · updated ${L.updated_ago} s ago · last download finished ${dur(L.last_event_ago)} ago`:' · numbers from the last run'}</div>`+
  (L.prefetch&&L.prefetch.of&&!L.blocks_built?`<div class=note style="margin-bottom:8px">Overture ${esc(L.prefetch.kind)}: ${L.prefetch.done} of ${L.prefetch.of} worldwide files scanned for this area (a one-time download; after it every block reads them from disk)</div><div class=bar style="margin:-4px 0 10px"><i style="width:${Math.round(100*L.prefetch.done/L.prefetch.of)}%"></i></div>`:'')+
  `<div class=stats>${st(L.streets.toLocaleString(),'streets this run')}${st(L.streets_per_s,'streets per second')}
   ${st(L.blocks_on_disk+' / '+L.blocks_total,'blocks saved')}${st(L.current?'#'+L.current.n+' · '+dur(L.current.for):'–','block now, for')}
   ${st(L.block_avg_s?dur(L.block_avg_s):'–','per block (this run)')}${st(L.eta_s!=null?dur(L.eta_s):'–','time left at that pace')}
   ${L.blocks_failed?st(L.blocks_failed,'blocks skipped'):''}</div>`+
  (rows?`<div class=tablewrap><table class=src><tr><th>Source</th><th>Requests</th><th>OK</th><th>Failed</th><th>Items</th><th>Data</th><th>Avg</th><th>Waiting</th></tr>${rows}</table></div>
   <p class=note style="margin:6px 0 0">Requests count every try, failures included. Overture's data is the size saved to disk, as its reader doesn't report bytes read.</p>`:'<p class=note>No downloads yet this run.</p>')+
  (err.length?`<details><summary>Last errors</summary><pre>${esc(err.join('\n'))}</pre></details>`:'')}
function render(){
 const tg=$('#toggle');tg.textContent=S.running?'⏸ Pause':'▶ Play';tg.className='primary big'+(S.running?' pausing':'');
 const q=S.ollama;$('#qwen').textContent=q.model?'Qwen ready: '+q.model:q.running?'Ollama running, Qwen not pulled':'Qwen not installed';
 $('#qwen').className='pill '+(q.model?'done':'paused');
 $('#stages').innerHTML='<h3 style="margin:0 0 8px">Stages</h3>'+S.stages.map(s=>`<div class=stage><div class=row><div><div class=name>${esc(s.name)}</div><div class=what>${esc(s.what)}</div></div>
  <div class=row><span class="pill ${s.status}">${s.status}</span>${s.status=='running'?`<button onclick="post('/api/pause',{id:'${s.id}'})">Pause</button>`:`<button onclick="post('/api/start',{id:'${s.id}'})">${s.status=='paused'?'Resume':'Start'}</button>`}</div></div>
  ${s.note?`<div class=fixed>🔧 ${esc(s.note)}</div>`:''}
  ${(S.activity.problems[s.id]||[]).map(l=>`<div class=problem>⚠ ${esc(l)}</div>`).join('')}
  <div class=bar><i style="width:${s.progress==null?(s.status=='done'?100:0):Math.round(s.progress*100)}%"></i></div>
  ${s.tail.length?`<details${s.status=='running'?' open':''}><summary>Log</summary><pre>${esc(s.tail.join('\n'))}</pre></details>`:''}</div>`).join('');
 const r=Object.entries(S.reports);if(r.length)$('#reports').innerHTML=r.map(([k,v])=>`<details><summary>${esc(k)}</summary><pre>${esc(v)}</pre></details>`).join('');
}
// ---- the network view: moves only on real events from the logs ----
const cv=$('#net'),cx=cv.getContext('2d'),HUE=[210,48,150,330,275,0,95,185,25];
let fx=[],flash={},prev=null,headFlash={};
const HEADID={lit:'lit',open_share:'open_share',bus_per_hour:'bus_per_hour',traffic_ratio:'traffic_ratio'};
function running(id){return S.stages.some(s=>s.id==id&&s.status=='running')}
function layout(){const W=cv.clientWidth,H=cv.clientHeight-110,n=S.network,P={in:[],groups:[],heads:{}},PER=W<600?6:8;
 const rows=n.groups.map(g=>Math.ceil(g.params.length/PER)),unit=H/(rows.reduce((a,b)=>a+b,0)+n.groups.length*.9);
 const nar=W<600;let y=unit*(nar?1.1:.6);const x0=nar?14:W*.2,dx=nar?W*.05:W*.024;
 n.groups.forEach((g,gi)=>{const top=y;g.params.forEach((p,i)=>{const r=Math.floor(i/PER);
   P.in.push({x:x0+(i%PER)*dx,y:top+r*unit,g:g.name,gi,p:p.name,u:p.useful,key:g.name+'.'+p.name})});
  P.groups.push({name:g.name,n:g.params.length,x:nar?x0-4:x0-10,y:nar?top-unit*.75:top+(rows[gi]-1)*unit/2,gi,nar});y+=rows[gi]*unit+unit*.9});
 n.heads.forEach((h,i)=>P.heads[h.id]={x:W*(nar?.62:.66),nar,y:H*(i+.5)/n.heads.length,r:15,name:h.name,measured:h.measured});
 P.sim={x:W*.92,y:H/2,r:17};return P}
function linkW(g,h){const l=S.network.links.find(l=>l.from==g&&l.to==h);return l?l.w:0}
function feeds(n,h){return h!='route'||S.network.route_groups.includes(n.g)}
function bez(a,b,u){const m=(a.x+b.x)/2;return{x:(1-u)**3*a.x+3*(1-u)**2*u*m+3*(1-u)*u*u*m+u**3*b.x,y:(1-u)**3*a.y+3*(1-u)**2*u*a.y+3*(1-u)*u*u*b.y+u**3*b.y}}
function fire(a,b,col,speed){fx.push({a,b,s:0,v:speed||(.5+Math.random()*.6),col})}
const SRC_GROUPS={'':['Street itself','Street network'],nightlights:['Lighting and cameras'],mapillary:['Lighting and cameras'],
 geoapify:['People around','Help nearby'],places:['People around','Help nearby'],buildings:['Beside the road'],roads:['Street itself','Street network']};
let prevLive=null,prevProg={};
function events(){const A=S.activity,P=layout(),L=S.live;
 if(L&&prevLive){const was=Object.fromEntries(prevLive.sources.map(s=>[s.id,s]));  // a download finished: dots into the groups it feeds
  L.sources.forEach(s=>{const p=was[s.id]||{ok:0,failed:0},g=SRC_GROUPS[s.id]||[];if(s.ok<=p.ok&&s.failed<=p.failed)return;const bad=s.failed>p.failed;
   P.in.filter(n=>g.includes(n.g)).forEach(n=>{if(!bad)flash[n.key]=performance.now()+Math.random()*400;
    fire({x:0,y:n.y+(Math.random()-.5)*30},n,bad?'rgba(239,91,91,.95)':`hsl(${HUE[n.gi]},90%,70%)`,.9+Math.random()*.5)})})}
 prevLive=L;
 S.stages.forEach(s=>{const p=prevProg[s.id];  // a later stage logged more progress: dots into the route model or stage 2
  if(p!=null&&s.progress!=null&&s.progress>p&&['pairs','qwen','claude','rank','sim'].includes(s.id)){const to=s.id=='sim'?P.sim:P.heads.route,c=P.in.filter(n=>feeds(n,'route'));
   for(let i=0;i<24;i++)fire(s.id=='sim'?P.heads[S.network.heads[i%S.network.heads.length].id]:c[Math.floor(Math.random()*c.length)],to,'rgba(160,195,255,.95)')}
  prevProg[s.id]=s.progress});
 if(!prev){prev=A;return}
 if(A.build.blocks>prev.build.blocks||A.build.blocks_on_disk>prev.build.blocks_on_disk)  // a block of streets was measured
  P.in.forEach(n=>{flash[n.key]=performance.now()+Math.random()*900;fire({x:0,y:n.y+(Math.random()-.5)*40},n,'hsl('+HUE[n.gi]+',90%,70%)',.8+Math.random())});
 const t=A.train;if(t.head&&P.heads[t.head]&&(t.curve.length>(prev.train.head==t.head?prev.train.curve.length:0))){  // training tried one more setting
  headFlash[t.head]=performance.now();P.in.forEach(n=>fire(n,P.heads[t.head],'rgba(160,195,255,.95)'))}
 prev=A}
function draw(now){requestAnimationFrame(draw);if(!S)return;const dpr=devicePixelRatio||1,W=cv.clientWidth,Hc=cv.clientHeight;
 if(cv.width!=Math.round(W*dpr)||cv.height!=Math.round(Hc*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(Hc*dpr)}
 cx.setTransform(dpr,0,0,dpr,0,0);cx.clearRect(0,0,W,Hc);const P=layout(),t=S.activity.train,heads=S.network.heads;
 const hot={};if(running('train')&&t.head)hot[t.head]=1;if(['pairs','qwen','claude','rank','ab'].some(running))hot.route=1;
 // links: input -> model, model -> stage 2
 P.in.forEach(n=>heads.forEach(h=>{if(!feeds(n,h.id))return;const b=P.heads[h.id],w=linkW(n.g,h.id),on=hot[h.id];
  cx.strokeStyle=on?`hsla(${HUE[n.gi]},80%,65%,${.07+.3*w})`:`rgba(148,160,191,${.025+.25*w*n.u+.1*w})`;cx.lineWidth=.5+1.5*w;
  cx.beginPath();cx.moveTo(n.x,n.y);cx.bezierCurveTo((n.x+b.x)/2,n.y,(n.x+b.x)/2,b.y,b.x,b.y);cx.stroke()}));
 heads.forEach(h=>{const a=P.heads[h.id],b=P.sim;cx.strokeStyle=(running('sim')||running('world'))?'rgba(91,140,255,.6)':'rgba(148,160,191,.2)';cx.lineWidth=1.2;
  cx.beginPath();cx.moveTo(a.x,a.y);cx.bezierCurveTo((a.x+b.x)/2,a.y,(a.x+b.x)/2,b.y,b.x,b.y);cx.stroke()});
 // no idle animation: dots travel only after events() saw a real download, block, training trial or progress step
 fx=fx.filter(p=>(p.s+=p.v/60)<1);fx.forEach(p=>{const q=bez(p.a,p.b,p.s);cx.fillStyle=p.col;cx.globalAlpha=Math.sin(p.s*Math.PI);
  cx.beginPath();cx.arc(q.x,q.y,1.9,0,7);cx.fill()});cx.globalAlpha=1;if(fx.length>2500)fx=fx.slice(-2500);
 // input dots
 cx.font='12px system-ui';cx.textBaseline='middle';
 P.groups.forEach(g=>{cx.fillStyle=`hsl(${HUE[g.gi]},70%,75%)`;cx.textAlign=g.nar?'left':'right';cx.fillText(`${g.name} (${g.n})`,g.x,g.y)});
 P.in.forEach(n=>{const f=flash[n.key]?Math.max(0,1-Math.abs(now-flash[n.key])/500):0,r=2.3+3.2*n.u+2*f;
  cx.fillStyle=`hsla(${HUE[n.gi]},85%,${45+30*f+20*n.u}%,${.55+.45*Math.max(f,n.u)})`;cx.beginPath();cx.arc(n.x,n.y,r,0,7);cx.fill();
  if(f>.3){cx.fillStyle=`hsla(${HUE[n.gi]},90%,70%,${.25*f})`;cx.beginPath();cx.arc(n.x,n.y,r+5,0,7);cx.fill()}});
 // models
 heads.forEach(h=>{const n=P.heads[h.id],on=hot[h.id],f=headFlash[h.id]?Math.max(0,1-(now-headFlash[h.id])/700):0;
  if(on||f){cx.fillStyle=`rgba(91,140,255,${.12+.35*f})`;cx.beginPath();cx.arc(n.x,n.y,n.r+7+8*f,0,7);cx.fill()}
  cx.fillStyle=on?'#3d6fe8':'#1c2440';cx.strokeStyle=h.measured?'#35c48b':on?'#a8c2ff':'#3a4670';cx.lineWidth=2;
  cx.beginPath();cx.arc(n.x,n.y,n.r,0,7);cx.fill();cx.stroke();
  if(t.head==h.id&&t.of){cx.strokeStyle='#a8c2ff';cx.lineWidth=3;cx.beginPath();cx.arc(n.x,n.y,n.r+4,-Math.PI/2,-Math.PI/2+2*Math.PI*t.curve.length/t.of);cx.stroke()}
  cx.fillStyle='#e8ecf7';if(n.nar){cx.textAlign='center';cx.fillText(h.name,n.x,n.y+n.r+12)}else{cx.textAlign='left';cx.fillText(h.name,n.x+n.r+9,n.y)}});
 const sm=P.sim;cx.fillStyle=(running('sim')||running('world'))?'#3d6fe8':'#1c2440';cx.strokeStyle='#3a4670';cx.beginPath();cx.arc(sm.x,sm.y,sm.r,0,7);cx.fill();cx.stroke();
 cx.fillStyle='#e8ecf7';cx.textAlign='center';cx.fillText('Stage 2',sm.x,sm.y+sm.r+14);
 curve(W,Hc)}
function curve(W,Hc){const y0=Hc-100,h=80,x0=W<600?8:40,w=W-x0-20,t=S.activity.train;cx.strokeStyle='#26304d';cx.lineWidth=1;cx.strokeRect(x0,y0,w,h);
 const wd=S.activity.world;if(wd.curve.length&&(running('world')||!running('train'))){const c=wd.curve,lo=Math.min(...c),hi=Math.max(...c),sp=hi-lo||1,X=i=>x0+8+(w-16)*i/Math.max(1,(wd.of?wd.of/25:c.length)-1),Y=v=>y0+h-8-(h-32)*(v-lo)/sp;
  cx.strokeStyle='#a8c2ff';cx.lineWidth=2;cx.beginPath();c.forEach((v,i)=>i?cx.lineTo(X(i),Y(v)):cx.moveTo(X(i),Y(v)));cx.stroke();cx.fillStyle='#e8ecf7';
  cx.fillText(`Virtual world: ${wd.done} of ${wd.of} episodes, reward per walk ${c[c.length-1].toFixed(3)} (higher is better; event chances are assumptions)`,x0+8,y0+10);return}
 cx.textAlign='left';cx.fillStyle='#94a0bf';cx.font='12px system-ui';
 if(!t.curve.length){cx.fillText(W<600?'Learning curve: shows during training.':'Learning curve: appears when base training runs (each point is one setting tried, scored on areas held out).',x0+8,y0+h/2);return}
 const lo=Math.min(...t.curve),hi=Math.max(...t.curve),sp=hi-lo||1,X=i=>x0+8+(w-16)*i/Math.max(1,(t.of||t.curve.length)-1),Y=v=>y0+h-8-(h-32)*(v-lo)/sp;
 let best=Infinity;cx.strokeStyle='#35c48b';cx.lineWidth=2;cx.beginPath();t.curve.forEach((v,i)=>{best=Math.min(best,v);i?cx.lineTo(X(i),Y(best)):cx.moveTo(X(i),Y(best))});cx.stroke();
 t.curve.forEach((v,i)=>{cx.fillStyle='#a8c2ff';cx.beginPath();cx.arc(X(i),Y(v),2.6,0,7);cx.fill()});
 cx.fillStyle='#e8ecf7';cx.fillText(`${(S.network.heads.find(x=>x.id==t.head)||{}).name||t.head}: setting ${t.curve.length} of ${t.of}, best ${best.toFixed(4)}${W<600?'':' (score on unseen areas, lower is better; green = best so far)'}`,x0+8,y0+10)}
function nowText(){const A=S.activity,b=A.build,t=A.train,parts=[];
 if(running('build')&&S.live)parts.push(S.live.current?`Building block #${S.live.current.n} of ${S.live.blocks_total} (for ${dur(S.live.current.for)})`:S.live.phase);
 else if(running('build'))parts.push(`Measuring streets: block ${b.blocks||'1'}${b.of?' of '+b.of:''}, ${b.streets.toLocaleString()} streets this run (${b.lit.toLocaleString()} with a lit label), ${b.blocks_on_disk} blocks saved in total`);
 if(running('train'))parts.push(t.head?`Training ${(S.network.heads.find(x=>x.id==t.head)||{}).name||t.head}: setting ${t.curve.length} of ${t.of||'?'}`:'Training: loading the streets');
 ['pairs','qwen','claude','rank','sim','world'].forEach(id=>{const s=S.stages.find(x=>x.id==id);if(s.status=='running')parts.push(s.name+(s.progress!=null?' '+Math.round(s.progress*100)+'%':''))});
 return parts.join(' · ')||'Nothing running. Press Run everything or Start on a stage.'}
load();setInterval(load,2000);requestAnimationFrame(draw);
</script></html>"""


if __name__ == "__main__":
    print(f"SafeRoute AI Studio: open http://localhost:{PORT}", flush=True)
    RUNNER.watch()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
