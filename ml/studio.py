"""SafeRoute AI Studio: one page on this PC to start, pause and watch every stage of training.

Stages run as separate processes. Pause stops one; the build, the route pairs and the Qwen labels carry on where they
stopped when started again, while the trainers (minutes each) start over. "Run everything" goes through the stages in
order and stops at the first pause or failure.

The picture shows the real model: parameter groups on the left feed the street and route models, which feed stage 2.
They are gradient-boosted trees and a learned weighting, not a neural network. Lines grow with how much each group
helped on areas the model never saw, once a training run has measured it.

Usage: py ml/studio.py     then open http://localhost:8770 (this PC only)
"""
import json
import os
import re
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
    ("qwen", "Qwen labels the pairs", ["ml/label_llm.py"], "Local Qwen picks the safer route of each pair. Resumes."),
    ("ab", "A/B answers page", ["ml/rate_pairs.py"], "People answer the same pairs at http://localhost:8765."),
    ("rank", "Base training: routes", ["ml/rank.py"], "Learns how much each measure matters from the answers."),
    ("sim", "Stage 2: simulated walks", ["ml/sim.py", "--trips", "500"], "Races the route choices through simulated trips."),
]
CHAIN = ["build", "train", "pairs", "qwen", "rank", "sim"]  # the A/B page is a server, started on its own
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
    "length_m": "Street itself", "unrated_pct": "Street itself",
}
HEADS = [("lit", "Lit at night"), ("open_share", "Places open"), ("bus_per_hour", "Buses passing"), ("traffic_ratio", "Traffic"), ("route", "Safer route choice")]
PROGRESS = re.compile(r"\[?(\d+)\s*(?:of|/)\s*(\d+)")


class Runner:
    def __init__(self):
        self.procs, self.paused, self.lock = {}, set(), threading.Lock()
        os.makedirs(LOGS, exist_ok=True)
        self.state_path = os.path.join(LOGS, "state.json")
        try:
            self.last = json.load(open(self.state_path))
        except (OSError, ValueError):
            self.last = {}

    def start(self, sid):
        stage = next(s for s in STAGES if s[0] == sid)
        with self.lock:
            if sid in self.procs and self.procs[sid].poll() is None:
                return
            self.paused.discard(sid)
            log = open(os.path.join(LOGS, f"{sid}.log"), "a", encoding="utf-8")
            log.write(f"\n=== started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log.flush()
            self.procs[sid] = subprocess.Popen([sys.executable, *stage[2]], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                               env={**os.environ, "PYTHONUNBUFFERED": "1"})

    def pause(self, sid):
        with self.lock:
            p = self.procs.get(sid)
            if p and p.poll() is None:
                self.paused.add(sid)
                p.terminate()

    def status(self, sid):
        p = self.procs.get(sid)
        if p is None:
            return self.last.get(sid, "idle")
        if p.poll() is None:
            return "running"
        s = "paused" if sid in self.paused else "done" if p.returncode == 0 else "failed"
        if self.last.get(sid) != s:
            self.last[sid] = s
            json.dump(self.last, open(self.state_path, "w"))
        return s

    def run_all(self):
        def chain():
            for sid in CHAIN:
                if sid == "qwen" and not ollama()["model"]:
                    continue  # no Qwen yet: the routes learn from people's answers only
                self.start(sid)
                while self.status(sid) == "running":
                    time.sleep(2)
                if self.status(sid) != "done":
                    return
        threading.Thread(target=chain, daemon=True).start()

    def pause_all(self):
        for sid in list(self.procs):
            self.pause(sid)


def tail(sid, n=12):
    path = os.path.join(LOGS, f"{sid}.log")
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 6000))
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
    """Nodes and weighted links from what training measured; links are thin and equal until it has."""
    group_of = {c: g for g, cols in GROUPS.items() for c in cols.split()}
    links = {}
    try:
        for m in json.load(open(os.path.join(OUT, "metrics.json"))):
            for col, drop in (m.get("most_useful_parameters") or {}).items():
                if drop > 0 and col in group_of:
                    k = (group_of[col], m["head"])
                    links[k] = links.get(k, 0) + drop
    except (OSError, ValueError, TypeError):
        pass
    try:
        w = json.load(open(os.path.join(OUT, "route_ranker.json")))["weights"]
        for col, v in w.items():
            g = ROUTE_GROUPS.get(col.removesuffix("_dark"))
            if g:
                links[(g, "route")] = links.get((g, "route"), 0) + abs(v)
    except (OSError, ValueError, KeyError):
        pass
    top = {}
    for (g, h), v in links.items():
        top[h] = max(top.get(h, 0), v)
    return {"groups": [{"name": g, "count": len(c.split())} for g, c in GROUPS.items()],
            "heads": [{"id": h, "name": n, "measured": h in top} for h, n in HEADS],
            "links": [{"from": g, "to": h, "w": round(v / top[h], 3)} for (g, h), v in links.items() if top.get(h)]}


def reports():
    out = {}
    for name in ("report.md", "route_report.md", "sim_report.md"):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            out[name] = open(path, encoding="utf-8").read()[:20000]
    return out


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
                stages.append({"id": sid, "name": name, "what": what, "status": RUNNER.status(sid), "progress": progress(lines), "tail": lines})
            return self.send(json.dumps({"stages": stages, "ollama": ollama(), "network": network(), "reports": reports()}))
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
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
canvas{width:100%;height:440px;display:block}.stage{border-top:1px solid var(--line);padding:10px 0}.stage:first-child{border-top:0}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between}.name{font-weight:600}.what{color:var(--quiet);font-size:13px}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--quiet)}
.running{color:var(--accent);border-color:var(--accent)}.done{color:var(--good);border-color:var(--good)}.failed{color:var(--bad);border-color:var(--bad)}.paused{color:var(--warn);border-color:var(--warn)}
.bar{height:4px;background:var(--line);border-radius:4px;margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent);width:0}
pre{white-space:pre-wrap;font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--quiet);margin:6px 0 0;max-height:150px;overflow:auto}
.note{color:var(--quiet);font-size:13px}details summary{cursor:pointer;color:var(--quiet);font-size:13px}
</style>
<header><h1>SafeRoute AI Studio</h1><div><span id=qwen class=pill></span>
<button class=primary onclick="post('/api/run-all')">Run everything</button> <button onclick="post('/api/pause-all')">Pause all</button></div></header>
<main><section><div class=card><canvas id=net></canvas>
<p class=note>Left: the parameter groups each street is measured on. Middle: the models. Right: stage 2. Light pulses run along the parts
that are training now. A line grows with how much that group helped on areas the model never saw (after training measures it).
The models are gradient-boosted trees and a learned weighting, not a neural network.</p></div>
<div class=card style="margin-top:16px"><h3 style="margin:0 0 8px">Results</h3><div id=reports class=note>No results yet.</div></div></section>
<section class=card id=stages></section></main>
<script>
const $=s=>document.querySelector(s);let S=null,pulses=[],t0=performance.now();
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function post(u,b){await fetch(u,{method:'POST',body:JSON.stringify(b||{})});load()}
async function load(){S=await(await fetch('/api/state')).json();render()}
function render(){
 const q=S.ollama;$('#qwen').textContent=q.model?'Qwen ready: '+q.model:q.running?'Ollama running, Qwen not pulled':'Qwen not installed';
 $('#qwen').className='pill '+(q.model?'done':'paused');
 $('#stages').innerHTML='<h3 style="margin:0 0 8px">Stages</h3>'+S.stages.map(s=>`<div class=stage><div class=row><div><div class=name>${esc(s.name)}</div><div class=what>${esc(s.what)}</div></div>
  <div class=row><span class="pill ${s.status}">${s.status}</span>${s.status=='running'?`<button onclick="post('/api/pause',{id:'${s.id}'})">Pause</button>`:`<button onclick="post('/api/start',{id:'${s.id}'})">${s.status=='paused'?'Resume':'Start'}</button>`}</div></div>
  <div class=bar><i style="width:${s.progress==null?(s.status=='done'?100:0):Math.round(s.progress*100)}%"></i></div>
  ${s.tail.length?`<details${s.status=='running'?' open':''}><summary>Log</summary><pre>${esc(s.tail.join('\n'))}</pre></details>`:''}</div>`).join('');
 const r=Object.entries(S.reports);if(r.length)$('#reports').innerHTML=r.map(([k,v])=>`<details><summary>${esc(k)}</summary><pre>${esc(v)}</pre></details>`).join('');
}
// ---- the network view ----
const cv=$('#net'),cx=cv.getContext('2d');
function layout(){const W=cv.clientWidth,H=cv.clientHeight,g=S.network.groups,h=S.network.heads,pos={};
 g.forEach((n,i)=>pos['g:'+n.name]={x:W*.3,y:H*(i+.5)/g.length,r:6+Math.sqrt(n.count)*1.6,label:n.name+' ('+n.count+')',side:-1});
 h.forEach((n,i)=>pos['h:'+n.id]={x:W*.62,y:H*(i+.5)/h.length,r:13,label:n.name,side:1});
 pos.sim={x:W*.88,y:H/2,r:16,label:'Stage 2',side:0};return pos}
function edges(){const n=S.network,L=[],w={};n.links.forEach(l=>w[l.from+'|'+l.to]=l.w);
 n.groups.forEach(g=>n.heads.forEach(h=>{if(h.id=='route'&&!['Lighting and cameras','Help nearby','People around','Transit','Street network','Street itself'].includes(g.name))return;
  L.push({a:'g:'+g.name,b:'h:'+h.id,w:w[g.name+'|'+h.id]??(h.measured?0:.15),head:h.id})}));
 n.heads.forEach(h=>L.push({a:'h:'+h.id,b:'sim',w:.5,head:'sim'}));return L}
function active(){const run=new Set(S.stages.filter(s=>s.status=='running').map(s=>s.id)),a=new Set();
 if(run.has('build'))a.add('input');if(run.has('train'))['lit','open_share','bus_per_hour','traffic_ratio'].forEach(x=>a.add(x));
 if(run.has('pairs')||run.has('qwen')||run.has('ab')||run.has('rank'))a.add('route');if(run.has('sim'))a.add('sim');return a}
function draw(now){requestAnimationFrame(draw);if(!S)return;const dpr=devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;
 if(cv.width!=W*dpr){cv.width=W*dpr;cv.height=H*dpr}cx.setTransform(dpr,0,0,dpr,0,0);cx.clearRect(0,0,W,H);
 const P=layout(),E=edges(),A=active(),t=(now-t0)/1000;
 E.forEach(e=>{const a=P[e.a],b=P[e.b],on=A.has(e.head)||(A.has('input')&&e.b!='sim');
  cx.strokeStyle=on?`rgba(91,140,255,${.25+.6*e.w})`:`rgba(148,160,191,${.08+.35*e.w})`;cx.lineWidth=.6+4*e.w;
  cx.beginPath();cx.moveTo(a.x,a.y);cx.bezierCurveTo((a.x+b.x)/2,a.y,(a.x+b.x)/2,b.y,b.x,b.y);cx.stroke();
  if(on&&Math.random()<.02+.08*e.w)pulses.push({e,s:0,v:.4+Math.random()*.5})});
 pulses=pulses.filter(p=>(p.s+=p.v/60)<1);pulses.forEach(p=>{const a=P[p.e.a],b=P[p.e.b],u=p.s,m=(a.x+b.x)/2,
  x=(1-u)**3*a.x+3*(1-u)**2*u*m+3*(1-u)*u*u*m+u**3*b.x,y=(1-u)**3*a.y+3*(1-u)**2*u*a.y+3*(1-u)*u*u*b.y+u**3*b.y;
  cx.fillStyle='rgba(160,195,255,.95)';cx.beginPath();cx.arc(x,y,2.4,0,7);cx.fill()});
 Object.entries(P).forEach(([k,n])=>{const id=k.slice(2),on=k=='sim'?A.has('sim'):k[0]=='h'?A.has(id):A.has('input'),glow=on?.5+.5*Math.sin(t*4):0;
  if(on){cx.fillStyle=`rgba(91,140,255,${.15+.2*glow})`;cx.beginPath();cx.arc(n.x,n.y,n.r+8+4*glow,0,7);cx.fill()}
  cx.fillStyle=on?'#5b8cff':'#1c2440';cx.strokeStyle=on?'#a8c2ff':'#3a4670';cx.lineWidth=1.5;cx.beginPath();cx.arc(n.x,n.y,n.r,0,7);cx.fill();cx.stroke();
  const h=k[0]=='h'&&S.network.heads.find(x=>x.id==id);if(h&&h.measured){cx.strokeStyle='#35c48b';cx.lineWidth=2;cx.beginPath();cx.arc(n.x,n.y,n.r+3,0,7);cx.stroke()}
  cx.fillStyle='#e8ecf7';cx.font='12px system-ui';cx.textBaseline='middle';cx.textAlign=n.side<0?'right':n.side>0?'left':'center';
  cx.fillText(n.label,n.x+(n.side<0?-n.r-8:n.side>0?n.r+8:0),n.side?n.y:n.y+n.r+14)})}
load();setInterval(load,2000);requestAnimationFrame(draw);
</script></html>"""


if __name__ == "__main__":
    print(f"SafeRoute AI Studio: open http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
