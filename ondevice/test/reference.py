"""The server's answer, for the query layer to be measured against.

Runs the real gtfsdb.py over the real national database and prints, as
JSON, exactly what the port has to reproduce from a pack: which services
run on a date, and every scheduled departure at a set of stops.

    podman run --rm --user root -v translink-data:/data -v "$PWD:/repo:ro" \\
      localhost/translink-dev:latest \\
      python /repo/ondevice/test/reference.py --region seq --date 20260806
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gtfsdb import active_service_ids, db, scheduled_departures   # noqa: E402
from regions import REGIONS                                        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="seq")
    ap.add_argument("--date", required=True, help="YYYYMMDD service date")
    ap.add_argument("--stops", default="", help="comma-separated feed stop ids")
    ap.add_argument("--sample", type=int, default=0,
                    help="instead, take this many stops spread across the feed")
    args = ap.parse_args()

    cfg = REGIONS[args.region]
    tz = cfg["tz"]
    service_date = datetime.strptime(args.date, "%Y%m%d").replace(tzinfo=tz)
    con = db(args.region)

    if args.stops:
        stop_ids = [s for s in args.stops.split(",") if s]
    else:
        # Spread across the table rather than the first N: the head of a GTFS
        # stops table is often one depot's worth of near-identical stops.
        n = args.sample or 20
        total = con.execute("SELECT count(*) FROM stops").fetchone()[0]
        step = max(1, total // n)
        stop_ids = [r[0] for r in con.execute(
            "SELECT stop_id FROM stops WHERE (parent_station IS NULL "
            "OR parent_station = '') ORDER BY stop_id")][::step][:n]

    services = sorted(active_service_ids(con, service_date))
    out = {"region": args.region, "date": args.date, "tz": str(tz),
           "service_count": len(services), "stops": {}}
    for sid in stop_ids:
        deps = scheduled_departures(con, [sid], service_date, tz)
        out["stops"][sid] = sorted(
            [{"trip_id": d["trip_id"], "scheduled": d["scheduled"],
              "route": d["route"], "route_type": d["route_type"],
              "headsign": d["headsign"], "platform": d["platform"],
              "stop_sequence": d["stop_sequence"]}
             for d in deps],
            key=lambda d: (d["scheduled"], d["trip_id"]))
    con.close()
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
