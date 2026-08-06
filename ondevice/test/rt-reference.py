"""Decode live GTFS-Realtime feeds with the generated protobuf library.

Saves the raw bytes alongside the decoded result, so the hand-written
TypeScript reader can be held to the real library on real feed data —
including whatever odd fields an agency happens to publish today.
"""
import json
import sys
from pathlib import Path

import httpx
from google.transit import gtfs_realtime_pb2

OUT = Path(sys.argv[1])
FEEDS = {
    "seq-tu": "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates",
    "seq-vp": "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions",
}

summary = {}
for name, url in FEEDS.items():
    try:
        raw = httpx.get(url, timeout=30).content
    except Exception as exc:
        print(f"  {name}: fetch failed ({exc})", file=sys.stderr)
        continue
    (OUT / f"{name}.pb").write_bytes(raw)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)

    tus, vps = [], []
    for e in feed.entity:
        if e.HasField("trip_update"):
            tu = e.trip_update
            stus = []
            for s in tu.stop_time_update:
                ev = s.arrival if s.HasField("arrival") else (
                    s.departure if s.HasField("departure") else None)
                stus.append({
                    "stop_sequence": s.stop_sequence if s.HasField("stop_sequence") else None,
                    "stop_id": s.stop_id if s.HasField("stop_id") else None,
                    "time": (ev.time if ev is not None and ev.HasField("time") else None),
                    "delay": (ev.delay if ev is not None and ev.HasField("delay") else None),
                })
            tus.append({
                "trip_id": tu.trip.trip_id,
                "start_date": tu.trip.start_date if tu.trip.HasField("start_date") else None,
                "schedule_relationship": (tu.trip.schedule_relationship
                                          if tu.trip.HasField("schedule_relationship") else None),
                "stop_time_updates": stus,
            })
        if e.HasField("vehicle"):
            v = e.vehicle
            vps.append({
                "trip_id": v.trip.trip_id if v.HasField("trip") and v.trip.trip_id else None,
                "lat": round(v.position.latitude, 5) if v.HasField("position") else None,
                "lon": round(v.position.longitude, 5) if v.HasField("position") else None,
            })
    summary[name] = {"bytes": len(raw), "trip_updates": tus, "vehicles": vps}
    print(f"  {name}: {len(raw):,} bytes, {len(tus)} trip updates, {len(vps)} vehicles",
          file=sys.stderr)

(OUT / "ref-rt.json").write_text(json.dumps(summary))
