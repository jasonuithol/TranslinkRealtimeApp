"""The server's geocoding and walking answers, for the port to match.

Runs the real geocoding.py and walking.py over the national databases and
prints, as JSON, what the on-device versions have to reproduce.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import geocoding                                    # noqa: E402
import walking                                      # noqa: E402

# Around Varsity Lakes / Robina: dense addresses, real businesses, and the
# namesake-street trap that the ranking rules exist for.
NEAR = (-27.83308, 153.3695)
SEQ_VIEWBOX = "152.0,-28.3,153.6,-26.0"

ADDRESSES = ["397 christine avenue", "480 christine ave", "12 high street",
             "5 bayswater avenue", "1 cavill avenue", "100 scottsdale drive"]
PLACES = ["officeworks", "bunnings burleigh", "pacific fair", "varsity lakes station"]
POINTS = [(-27.83308, 153.3695), (-28.08739, 153.40420), (-27.4705, 153.026),
          (-28.00, 153.42), (-27.9, 153.30)]
WALKS = [((-28.08739, 153.40420), (-28.08315, 153.39625)),
         ((-27.83308, 153.3695), (-27.83100, 153.37400)),
         ((-27.4705, 153.026), (-27.4665, 153.0280))]

out = {"addresses": {}, "places": {}, "rgeocode": {}, "walks": {}}

for q in ADDRESSES:
    for label, near in (("near", NEAR), ("plain", None)):
        r = geocoding.gnaf_geocode(q, "QLD", 8, near)
        out["addresses"][f"{q}|{label}"] = r

for q in PLACES:
    for label, near in (("near", NEAR), ("plain", None)):
        r = geocoding.places_geocode(q, SEQ_VIEWBOX, 8, near)
        out["places"][f"{q}|{label}"] = r

for lat, lon in POINTS:
    out["rgeocode"][f"{lat},{lon}"] = geocoding.reverse_geocode(lat=lat, lon=lon)

for (a, b) in WALKS:
    key = f"{a[0]},{a[1]}->{b[0]},{b[1]}"
    try:
        r = walking.walkroute(from_lat=a[0], from_lon=a[1], to_lat=b[0], to_lon=b[1])
        out["walks"][key] = {"dist_m": r["dist_m"], "n_points": len(r["points"]),
                             "streets": [{"name": s["name"], "m": s["m"]}
                                         for s in r["streets"]]}
    except Exception as exc:
        out["walks"][key] = {"error": type(exc).__name__}

json.dump(out, sys.stdout)
