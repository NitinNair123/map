"""A page where people answer "which route would you rather walk at this hour, A or B?" for the pairs in ml/data/pairs.jsonl.
Answers go to ml/data/labels_human.csv with the name typed in; nothing else about the person is kept.

Usage: py ml/rate_pairs.py     then open http://localhost:8765 (others on the same Wi-Fi: http://<this PC's address>:8765)
"""
import csv
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairs  # noqa: E402

OUT = os.path.join(os.path.dirname(pairs.OUT), "labels_human.csv")
LEAFLET = os.path.join(pairs.ROOT, "static", "vendor", "leaflet")
LOCK = threading.Lock()
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Which route?</title>
<link rel=stylesheet href=/leaflet/leaflet.css><script src=/leaflet/leaflet.js></script>
<style>body{font:16px system-ui;margin:0 auto;max-width:760px;padding:12px}#map{height:320px}table{border-collapse:collapse;width:100%}
td,th{padding:4px 8px;border-bottom:1px solid #ccc;text-align:right}td:first-child,th:first-child{text-align:left}
button{font:inherit;padding:12px 18px;margin:8px 4px}.a{color:#0b62d6}.b{color:#d9480f}</style>
<p><label>Your name <input id=who></label></p><h2 id=q></h2><div id=map></div><table id=t></table>
<p><button onclick="send('A')" class=a>A feels safer</button><button onclick="send('B')" class=b>B feels safer</button>
<button onclick="send('same')">About the same</button><button onclick="next()">Skip</button></p><p id=n></p>
<script>
let p,map=L.map('map'),lines=[];L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap'}).addTo(map)
who.value=localStorage.who||'';const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))
async function next(){if(!who.value.trim())return alert('Type your name first');localStorage.who=who.value
 p=await(await fetch('/next?who='+encodeURIComponent(who.value))).json();if(!p.id){q.textContent='All done, thank you!';return}
 q.textContent='Walking alone at '+String(p.hour).padStart(2,'0')+':00 on a '+(p.weekend?'weekend':'weekday')+': which route?'
 lines.forEach(l=>l.remove());lines=[L.polyline(p.A_coords,{color:'#0b62d6',weight:5}).addTo(map),L.polyline(p.B_coords,{color:'#d9480f',weight:5,dashArray:'6 6'}).addTo(map)]
 map.fitBounds(lines[0].getBounds().extend(lines[1].getBounds()))
 t.innerHTML='<tr><th><th class=a>A<th class=b>B'+Object.entries(p.labels).map(([k,v])=>'<tr><td>'+esc(v)+'<td>'+p.A[k]+'<td>'+p.B[k]).join('');n.textContent=p.done+' answered by you'}
async function send(s){await fetch('/answer',{method:'POST',body:JSON.stringify({id:p.id,who:who.value,safer:s})});next()}
who.onchange=next;if(who.value)next()
</script>"""


def answers():
    return list(csv.DictReader(open(OUT, encoding="utf-8"))) if os.path.exists(OUT) else []


class Handler(BaseHTTPRequestHandler):
    def send(self, body, kind="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self.send(PAGE, "text/html; charset=utf-8")
        if u.path.startswith("/leaflet/"):
            path = os.path.join(LEAFLET, os.path.basename(u.path) if "/images/" not in u.path else os.path.join("images", os.path.basename(u.path)))
            if os.path.isfile(path):
                kind = {"css": "text/css", "js": "text/javascript", "png": "image/png"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
                return self.send(open(path, "rb").read(), kind)
        if u.path == "/next":
            who = parse_qs(u.query).get("who", [""])[0].strip()
            mine = {int(a["id"]) for a in answers() if a["who"] == who}
            p = next((p for p in pairs.load() if p["id"] not in mine), None)  # everyone starts with the same pairs, so answers can be compared
            return self.send(json.dumps({**p, "labels": pairs.MEASURES, "done": len(mine)} if p else {}))
        self.send("not found", "text/plain", 404)

    def do_POST(self):
        try:
            a = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 2000)))
            row = [int(a["id"]), str(a["who"]).strip()[:40], a["safer"]]
            if a["safer"] not in ("A", "B", "same") or not row[1]:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self.send("bad answer", "text/plain", 400)
        with LOCK:
            new = not os.path.exists(OUT)
            with open(OUT, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["id", "who", "safer"])
                w.writerow(row)
        self.send("{}")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"{len(pairs.load())} pairs. Open http://localhost:8765", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
