"""Download roads and safety-relevant places for an area from OpenStreetMap.

Usage: python fetch_osm.py SOUTH WEST NORTH EAST [out.json]
Keep the box campus or neighbourhood sized (a few km across).
"""
import json
import sys
import urllib.parse
import urllib.request

QUERY = """
[out:json][timeout:120];
way["highway"~"^(trunk|primary|secondary|tertiary|primary_link|secondary_link|tertiary_link|unclassified|residential|living_street|pedestrian|service|footway|path|cycleway|steps|track)$"]["foot"!="no"]["access"!~"^(private|no)$"]({bbox});
out body geom;
(
  node["highway"~"^(street_lamp|bus_stop)$"]({bbox});
  nwr["man_made"="surveillance"]({bbox});
  nwr["amenity"]({bbox});
  nwr["shop"]({bbox});
  nwr["tourism"="hotel"]({bbox});
);
out center tags;
"""


def main():
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    s, w, n, e = (float(v) for v in sys.argv[1:5])
    out = sys.argv[5] if len(sys.argv) > 5 else "data/osm.json"
    data = urllib.parse.urlencode({"data": QUERY.format(bbox=f"{s},{w},{n},{e}")}).encode()
    req = urllib.request.Request("https://overpass-api.de/api/interpreter", data, {"User-Agent": "safe-route-mvp"})
    with urllib.request.urlopen(req, timeout=180) as r:
        osm = json.load(r)
    with open(out, "w") as f:
        json.dump(osm, f)
    ways = sum(el["type"] == "way" and "geometry" in el for el in osm["elements"])
    print(f"saved {len(osm['elements'])} elements ({ways} roads) to {out}")


if __name__ == "__main__":
    main()
