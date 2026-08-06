"""The server's journeys, for the ported RAPTOR to be measured against.

Builds planner.py's timetable for a region-day and plans a set of trips
across it, printing the itineraries as JSON: legs, boarding and alighting
stops, and the seconds-since-midnight the whole algorithm works in.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import planner as journey                                  # noqa: E402
from regions import REGIONS, TSN_REGIONS                   # noqa: E402

REGION = sys.argv[1] if len(sys.argv) > 1 else "seq"
YMD = sys.argv[2]
WEEKDAY = int(sys.argv[3])          # Monday=0, as planner.py expects
cfg = REGIONS[REGION]

# Today only. The port is compared on identical inputs, and a single date
# keeps the comparison about the algorithm rather than the calendar.
dates = [(YMD, WEEKDAY, 0)]
tt = journey.get_timetable(REGION, cfg["db"], dates,
                           tsn_family=(REGION in TSN_REGIONS))

# Stop-to-stop pairs across the network, at several times of day, plus the
# address-origin form (sources/targets with walking penalties) that the
# real planner endpoint uses for a dropped pin.
STOP_PAIRS = json.loads(sys.argv[4]) if len(sys.argv) > 4 else []

out = {"region": REGION, "ymd": YMD, "weekday": WEEKDAY,
       "stops": len(tt.stop_ids), "routes": len(tt.routes),
       "foot_pairs": sum(len(v) for v in tt.foot.values()),
       "plans": {}}

for spec in STOP_PAIRS:
    key = json.dumps(spec, sort_keys=True)
    kwargs = {"dep_sec": spec["dep_sec"], "max_itineraries": 4}
    if "sources" in spec:
        kwargs["sources"] = [tuple(s) for s in spec["sources"]]
        kwargs["targets"] = [tuple(t) for t in spec["targets"]]
        its = journey.plan(tt, None, None, **kwargs)
    else:
        its = journey.plan(tt, spec["from"], spec["to"], **kwargs)
    out["plans"][key] = [
        {"arr_sec": it["arr_sec"], "dep_sec": it["dep_sec"], "rides": it["rides"],
         "legs": it["legs"]}
        for it in its]

json.dump(out, sys.stdout)
