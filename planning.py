# the journey planner endpoint: RAPTOR plus realtime and walks
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import math
import sqlite3
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

import planner as journey
from regions import REGIONS, TSN_REGIONS, STATE, region_cfg
from boards import nearby_stops, sibling_stops, _stop_name
from gtfsdb import db, platform_label
from geocoding import reverse_geocode, _point_label
from walking import walkroute

router = APIRouter()


def _service_dates(tz, at=None):
    """The service dates a journey may ride: today's, plus yesterday's
    after-midnight tail (GTFS 24:xx times) shifted back a day. Shared by
    the plan endpoint and the startup warm so both build the same cache
    key — a mismatch would build the timetable twice."""
    now = datetime.now(tz) if at is None else datetime.fromtimestamp(at, tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = midnight - timedelta(days=1)
    return ((midnight.strftime("%Y%m%d"), midnight.weekday(), 0),
            (yesterday.strftime("%Y%m%d"), yesterday.weekday(), -86400))


def _walk_pen_secs(alat, alon, blat, blon, crow_m):
    """Walking time between two points, costed on the REAL street graph
    when it can answer — the crow-fly ×1.3 guess mis-prices lake suburbs
    badly enough to board the wrong stop (Varsity Lakes, 2026-08-02).
    walkroute() is LRU-cached, so the planner's ≤20 candidate walks per
    plan are only ever computed once each. dist_m excludes the door→node
    snap hops; add them straight-line."""
    try:
        r = walkroute(alat, alon, blat, blon)
        pts = r["points"]
        def _sl(la1, lo1, la2, lo2):
            p1, p2 = math.radians(la1), math.radians(la2)
            a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
                 * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
            return 2 * 6371000 * math.asin(math.sqrt(a))
        d = (r["dist_m"]
             + _sl(alat, alon, pts[1][1], pts[1][0])
             + _sl(pts[-2][1], pts[-2][0], blat, blon))
        return max(60, int(d / journey.WALK_SPEED_MPS))
    except HTTPException:
        return max(60, int(crow_m * journey.WALK_DETOUR
                           / journey.WALK_SPEED_MPS))


# ---------------------------------------------------------------------------
# Journey planner (PLANNER.md phase 1): single-region, schedule-based RAPTOR
# with realtime re-costing. planner.py owns the routing; this endpoint owns
# service-date arithmetic, leg enrichment and the TripUpdate overlay.


def _rt_time_for(st_rt: dict, trip_id: str, stop_id: str, seq: int | None,
                 scheduled: int) -> tuple[int, bool]:
    """One stop-time re-costed from the realtime cache: (epoch, is_live).
    Mirrors the board's rules — exact stop entry first, else the latest
    delay at-or-before this stop's sequence propagates forward."""
    rt_trip = st_rt.get(trip_id) or {}
    rt = rt_trip.get("stops", {}).get(stop_id)
    if rt:
        if rt.get("arrival"):
            return rt["arrival"], True
        if rt.get("delay") is not None:
            return scheduled + rt["delay"], True
    if rt_trip.get("seq") and seq is not None:
        prop = None
        for s, delay in rt_trip["seq"]:
            if int(s) <= int(seq):
                prop = delay
            else:
                break
        if prop is not None:
            return scheduled + prop, True
    return scheduled, False


@router.get("/api/r/{region}/plan")
@router.get("/api/plan")
def plan_endpoint(to: str | None = None,
                  from_stop: str | None = Query(None, alias="from"),
                  from_lat: float | None = None, from_lon: float | None = None,
                  from_label: str | None = None,
                  to_lat: float | None = None, to_lon: float | None = None,
                  to_label: str | None = None,
                  at: int | None = None, region: str = "seq"):
    """Origin AND destination are each either a stop (?from= / ?to=) or a
    POINT (?from_lat…/?to_lat… — a searched address, a business, a
    geolocation). Point ends walk to/from every stop in range, pre-charged
    with walking time, and the itinerary's first/last leg is that walk —
    the journey starts at the front door and ends at the pizza joint, not
    at whatever stop happens to be nearest."""
    cfg, st = region_cfg(region), STATE[region]
    if from_stop is None and (from_lat is None or from_lon is None):
        raise HTTPException(400, "Give either from= or from_lat=&from_lon=")
    if to is None and (to_lat is None or to_lon is None):
        raise HTTPException(400, "Give either to= or to_lat=&to_lon=")
    con = db(region)
    try:
        names = {}
        check_ids = [s for s in (from_stop, to) if s is not None]
        for sid in check_ids:
            row = con.execute(
                "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops "
                "WHERE stop_id=?", (sid,)).fetchone()
            if row is None:
                raise HTTPException(404, f"Unknown stop_id {sid}")
            names[sid] = dict(row)
        if from_stop is not None and from_stop == to:
            raise HTTPException(400, "Origin and destination are the same stop")

        tz = cfg["tz"]
        now = (datetime.now(tz) if at is None
               else datetime.fromtimestamp(at, tz))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dates = _service_dates(tz, at)
        tt = journey.get_timetable(region, cfg["db"], dates,
                                   tsn_family=(region in TSN_REGIONS))
        dep_sec = int((now - midnight).total_seconds())

        sources = None
        seed_pen: dict = {}
        if from_stop is None:
            near = nearby_stops(from_lat, from_lon, limit=10, region=region)
            near = [s for s in near if s["dist_m"] <= 1000]
            if not near:
                raise HTTPException(404, "No stops within walking range "
                                         "of the origin")
            sources = []
            for s in near:
                pen = _walk_pen_secs(from_lat, from_lon,
                                     s["lat"], s["lon"], s["dist_m"])
                sources.append((s["stop_id"], pen))
                # reconstruction may end on any member of the seed's group
                for member in tt.group_of.get(s["stop_id"], {s["stop_id"]}):
                    if pen < seed_pen.get(member, 1 << 30):
                        seed_pen[member] = pen
            names["__origin__"] = {
                "stop_id": None,
                "stop_name": _point_label(from_lat, from_lon, from_label,
                                          "Your address"),
                "stop_lat": from_lat, "stop_lon": from_lon,
            }
        targets = None
        t_pen: dict = {}
        if to is None:
            near = nearby_stops(to_lat, to_lon, limit=10, region=region)
            near = [s for s in near if s["dist_m"] <= 1000]
            if not near:
                raise HTTPException(404, "No stops within walking range "
                                         "of the destination")
            targets = []
            for s in near:
                pen = _walk_pen_secs(s["lat"], s["lon"],
                                     to_lat, to_lon, s["dist_m"])
                targets.append((s["stop_id"], pen))
                for member in tt.group_of.get(s["stop_id"], {s["stop_id"]}):
                    if pen < t_pen.get(member, 1 << 30):
                        t_pen[member] = pen
            names["__dest__"] = {
                "stop_id": None,
                "stop_name": _point_label(to_lat, to_lon, to_label,
                                          "Your destination"),
                "stop_lat": to_lat, "stop_lon": to_lon,
            }

        raw = journey.plan(tt, from_stop, to, dep_sec,
                           sources=sources, targets=targets)
        # Point ends owe their walks as visible legs: address -> first stop,
        # and last stop -> the destination's door.
        for it in raw:
            if not it["legs"]:
                continue
            if sources is not None:
                first = it["legs"][0]
                seed = first["board"] if first["kind"] == "ride" else first["from"]
                pen = seed_pen.get(seed)
                if pen:
                    it["legs"].insert(0, {"kind": "walk", "from": None,
                                          "to": seed, "secs": pen})
            if targets is not None:
                last = it["legs"][-1]
                end = last["alight"] if last["kind"] == "ride" else last["to"]
                pen = t_pen.get(end)
                if pen:
                    it["legs"].append({"kind": "walk", "from": end,
                                       "to": None, "secs": pen})
            # Chained walks (origin walk + a station-group hop, footpath
            # sequences) read as one walk to a rider — merge them.
            merged = []
            for leg in it["legs"]:
                if (leg["kind"] == "walk" and merged
                        and merged[-1]["kind"] == "walk"):
                    merged[-1] = {"kind": "walk", "from": merged[-1]["from"],
                                  "to": leg["to"],
                                  "secs": merged[-1]["secs"] + leg["secs"]}
                else:
                    merged.append(dict(leg))
            it["legs"] = merged

        mid_epoch = int(midnight.timestamp())
        itineraries = []
        for it in raw:
            legs = []
            prev_arr_epoch = None      # rt-aware arrival of the previous ride
            pending_walk = 0
            at_risk = False
            for leg in it["legs"]:
                if leg["kind"] == "walk":
                    pending_walk += leg["secs"]
                    legs.append({
                        "kind": "walk", "secs": leg["secs"],
                        "from_name": (names["__origin__"]["stop_name"]
                                      if leg["from"] is None
                                      else _stop_name(con, leg["from"])),
                        "to_name": (names["__dest__"]["stop_name"]
                                    if leg["to"] is None
                                    else _stop_name(con, leg["to"])),
                    })
                    continue
                tmeta = con.execute(
                    "SELECT t.trip_headsign, t.shape_id, r.route_id, "
                    "r.route_short_name, r.route_long_name, r.route_type, "
                    "r.route_color "
                    "FROM trips t JOIN routes r ON r.route_id = t.route_id "
                    "WHERE t.trip_id=?", (leg["trip_id"],)).fetchone()
                seqs = {r["stop_id"]: r["stop_sequence"] for r in con.execute(
                    "SELECT stop_id, stop_sequence FROM stop_times "
                    "WHERE trip_id=? AND stop_id IN (?,?)",
                    (leg["trip_id"], leg["board"], leg["alight"]))}
                dep_epoch = mid_epoch + leg["dep_sec"]
                arr_epoch = mid_epoch + leg["arr_sec"]
                rt_dep, dep_live = _rt_time_for(
                    st["rt"], leg["trip_id"], leg["board"],
                    seqs.get(leg["board"]), dep_epoch)
                rt_arr, arr_live = _rt_time_for(
                    st["rt"], leg["trip_id"], leg["alight"],
                    seqs.get(leg["alight"]), arr_epoch)
                # A delay on the previous ride can break this connection.
                risk = (prev_arr_epoch is not None
                        and prev_arr_epoch + pending_walk > rt_dep - 30)
                at_risk = at_risk or risk
                board = con.execute(
                    "SELECT stop_name, platform_code, stop_lat, stop_lon "
                    "FROM stops WHERE stop_id=?", (leg["board"],)).fetchone()
                alight = con.execute(
                    "SELECT stop_name, platform_code, stop_lat, stop_lon "
                    "FROM stops WHERE stop_id=?", (leg["alight"],)).fetchone()
                legs.append({
                    "kind": "ride",
                    "trip_id": leg["trip_id"],
                    "region": region,
                    "route": (tmeta["route_short_name"]
                              or tmeta["route_long_name"]) if tmeta else "",
                    "route_type": tmeta["route_type"] if tmeta else None,
                    "route_color": tmeta["route_color"] if tmeta else None,
                    "headsign": tmeta["trip_headsign"] if tmeta else "",
                    "shape_id": tmeta["shape_id"] if tmeta else None,
                    "board": leg["board"],
                    "board_name": board["stop_name"] if board else leg["board"],
                    "board_platform": platform_label(
                        board["platform_code"] if board else None,
                        board["stop_name"] if board else None),
                    "board_lat": board["stop_lat"] if board else None,
                    "board_lon": board["stop_lon"] if board else None,
                    "alight": leg["alight"],
                    "alight_name": alight["stop_name"] if alight else leg["alight"],
                    "alight_lat": alight["stop_lat"] if alight else None,
                    "alight_lon": alight["stop_lon"] if alight else None,
                    "dep": rt_dep, "arr": rt_arr,
                    "sched_dep": dep_epoch, "sched_arr": arr_epoch,
                    "realtime": dep_live or arr_live,
                    "at_risk": risk,
                })
                prev_arr_epoch = rt_arr
                pending_walk = 0
            arrive = next((l["arr"] for l in reversed(legs)
                           if l["kind"] == "ride"), None)
            depart = next((l["dep"] for l in legs if l["kind"] == "ride"), None)
            # Both walks belong to the journey. "arrive" means at the pizza
            # joint, not at the last bus stop; "depart" means leaving your
            # door, not the moment the bus pulls out — the walk to the stop
            # is time you must spend, and a journey that quietly started at
            # the bus stop under-reported itself by the whole first walk.
            if arrive is not None:
                for l in reversed(legs):
                    if l["kind"] == "ride":
                        break
                    arrive += l["secs"]
            if depart is not None:
                for l in legs:
                    if l["kind"] == "ride":
                        break
                    depart -= l["secs"]
            if depart is None:
                continue
            itineraries.append({
                "depart": depart, "arrive": arrive,
                "minutes": max(1, round((arrive - depart) / 60)),
                "transfers": it["rides"] - 1,
                "at_risk": at_risk,
                "legs": legs,
            })
    finally:
        con.close()

    # A journey you could simply WALK. RAPTOR only produces journeys with
    # rides, so a 1.2 km hop once got a 44-minute bus+train proposal.
    # When the door-to-door walk is routable and sane (≤35 min), it
    # competes as an itinerary — and any transit journey it beats outright
    # (arriving no earlier despite the effort of riding) is dominated and
    # dropped.
    o_pt = names["__origin__"] if from_stop is None else names[from_stop]
    d_pt = names["__dest__"] if to is None else names[to]
    if (o_pt.get("stop_lat") is not None and d_pt.get("stop_lat") is not None):
        dl = (d_pt["stop_lat"] - o_pt["stop_lat"]) * 111000.0
        dn = ((d_pt["stop_lon"] - o_pt["stop_lon"]) * 111000.0
              * math.cos(math.radians(o_pt["stop_lat"])))
        straight = math.hypot(dl, dn)
        if straight <= 2400:
            wsecs = _walk_pen_secs(o_pt["stop_lat"], o_pt["stop_lon"],
                                   d_pt["stop_lat"], d_pt["stop_lon"], straight)
            if wsecs <= 2100:
                now_epoch = mid_epoch + dep_sec
                walk_arr = now_epoch + wsecs
                itineraries = [it for it in itineraries
                               if it["arrive"] < walk_arr]
                itineraries.append({
                    "depart": now_epoch, "arrive": walk_arr,
                    "minutes": max(1, round(wsecs / 60)),
                    "transfers": 0, "at_risk": False,
                    "legs": [{"kind": "walk", "secs": wsecs,
                              "from_name": o_pt["stop_name"],
                              "to_name": d_pt["stop_name"]}],
                })

    return {
        "from": names["__origin__"] if from_stop is None else names[from_stop],
        "to": names["__dest__"] if to is None else names[to],
        "generated_at": int(time.time()),
        "realtime_configured": bool(cfg["trip_updates"]),
        "itineraries": itineraries,
    }
