"""Какие дороги OpenStreetMap рядом с каждой точкой (для оценки трафика). Пишет data/roads.json."""
import json, sys, time, urllib.parse, urllib.request
sys.path.insert(0, "bot")
import engine as E

out = {}
for sp, (name, lat, lon) in E.SPOTS.items():
    q = f'[out:json][timeout:60];(way(around:150,{lat},{lon})[highway];way(around:150,{lat},{lon})[amenity=parking];);out tags center;'
    url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(q)
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=E.UA), timeout=90) as r:
                j = json.loads(r.read().decode()); break
        except Exception as e:
            print(sp, e); time.sleep(10); j = {"elements": []}
    rows = []
    for el in j.get("elements", []):
        c = el.get("center") or {}
        d = ((c.get("lat", lat) - lat) * 111000) ** 2 + ((c.get("lon", lon) - lon) * 78600) ** 2
        rows.append({"dist_m": round(d ** .5), **{k: v for k, v in el.get("tags", {}).items()
                     if k in ("highway", "name", "ref", "service", "lanes", "maxspeed", "surface", "access", "amenity", "lit", "oneway")}})
    out[sp] = sorted(rows, key=lambda r: r["dist_m"])
    time.sleep(3)
open("data/roads.json", "w").write(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps({k: [(r["dist_m"], r.get("highway") or r.get("amenity"), r.get("name") or r.get("ref") or "") for r in v[:8]] for k, v in out.items()}, ensure_ascii=False, indent=1))
