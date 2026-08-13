# the arrivals board: sibling stops, candidates, departures
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from regions import (AGENCY_TZ, STATION_LINKS, TSN_REGIONS, LOOKAHEAD_MINUTES, MAX_RESULTS, REGIONS,
                     STAGING_WINDOW_S, STATE, region_cfg)
from gtfsdb import (active_service_ids, db, estimate_ghost_positions,
                    gtfs_time_to_epoch, platform_label, scheduled_departures,
                    _out_of_service, _seconds_into_day)

router = APIRouter()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/r/{region}/stops/search")
@router.get("/api/stops/search")
def search_stops(q: str, region: str = "seq"):
    """Match stops by name. Parent stations rank first; their individual
    platforms are hidden so a station appears once (select the station to
    see all platforms combined)."""
    con = db(region)
    rows = con.execute(
        """
        SELECT stop_id, stop_name, location_type
        FROM stops
        WHERE stop_name LIKE ?
          AND (parent_station IS NULL OR parent_station = '')
        ORDER BY (location_type = 1) DESC, stop_name
        LIMIT 25
        """,
        (f"%{q}%",),
    ).fetchall()
    con.close()
    modes = stop_modes(region)
    return [
        {
            "stop_id": r["stop_id"],
            "stop_name": r["stop_name"],
            "is_station": r["location_type"] == 1,
            "route_type": modes.get(r["stop_id"], 3),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Which way a stop faces
#
# Most bus and tram stops come in pairs with the SAME name, one either side
# of the road, and a rider standing between them cannot tell from a list
# which is which. The direction services travel from a stop does tell them:
# the bearing to the next stop on a trip that calls there.
#
# One query per stop, memoised for the life of the process — the answer is a
# property of the timetable, and the timetable is swapped atomically on
# re-ingest (the cache key carries the region, and a restart follows an
# ingest anyway).
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _bearing(lat1, lon1, lat2, lon2) -> float:
    """Initial bearing from one point to another, in degrees from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


@lru_cache(maxsize=8192)
def stop_heading(region: str, stop_id: str) -> tuple | None:
    """(compass point, bearing in degrees) for a service leaving this stop.

    The degrees are the useful half: the client draws an arrow pointing that
    way, because "SW" asks a rider to know which way south-west is while an
    arrow just shows them. The letters ride along for the tooltip and for
    anyone reading it with a screen reader.

    None for a terminus (nothing after it), for a stop nothing calls at, and
    for stations — a platform's direction is already given by its number and
    a station as a whole does not have one.
    """
    con = db(region)
    try:
        row = con.execute(
            """SELECT s1.stop_lat, s1.stop_lon, s2.stop_lat, s2.stop_lon
                 FROM stop_times a
                 JOIN stop_times b ON b.trip_id = a.trip_id
                                  AND b.stop_sequence > a.stop_sequence
                 JOIN stops s1 ON s1.stop_id = a.stop_id
                 JOIN stops s2 ON s2.stop_id = b.stop_id
                WHERE a.stop_id = ?
                  AND s1.stop_lat IS NOT NULL AND s2.stop_lat IS NOT NULL
                ORDER BY a.stop_sequence, b.stop_sequence LIMIT 1""",
            (stop_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    la1, lo1, la2, lo2 = row
    # Two stops at the same spot (a shared bay) give a meaningless bearing.
    if abs(la1 - la2) < 1e-7 and abs(lo1 - lo2) < 1e-7:
        return None
    deg = _bearing(la1, lo1, la2, lo2)
    return COMPASS[int((deg + 22.5) % 360 // 45)], round(deg)



@router.get("/api/r/{region}/stops/nearby")
@router.get("/api/stops/nearby")
def nearby_stops(lat: float, lon: float, limit: int = 10, region: str = "seq"):
    """Closest stops to a point, for 'which stop is nearest to me/home?'.
    Same visibility rule as name search: child platforms are hidden and the
    parent station is returned once."""
    # ~2.2 km box prefilter; haversine only on the survivors.
    dlat = 0.02
    dlon = 0.02 / max(0.2, math.cos(math.radians(lat)))
    con = db(region)
    rows = con.execute(
        """
        SELECT stop_id, stop_name, stop_lat, stop_lon, location_type
        FROM stops
        WHERE (parent_station IS NULL OR parent_station = '')
          AND stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?
        """,
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    ).fetchall()
    con.close()

    def haversine_m(la, lo):
        p1, p2 = math.radians(lat), math.radians(la)
        a = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2)
             * math.sin(math.radians(lo - lon) / 2) ** 2)
        return 2 * 6371000 * math.asin(math.sqrt(a))

    modes = stop_modes(region)
    out = sorted(
        (
            {
                "stop_id": r["stop_id"],
                "stop_name": r["stop_name"],
                "is_station": r["location_type"] == 1,
                "heading": None,        # filled in below, for the survivors
                "bearing": None,        # degrees from north, for the arrow
                "route_type": modes.get(r["stop_id"], 3),
                "dist_m": round(haversine_m(r["stop_lat"], r["stop_lon"])),
                # For drawing the surrounds of a dropped pin on the map.
                "lat": r["stop_lat"],
                "lon": r["stop_lon"],
            }
            for r in rows
            if r["stop_lat"] is not None
        ),
        key=lambda s: s["dist_m"],
    )
    out = out[: max(1, min(limit, 25))]
    # Only the survivors get a heading: it is a query each, and the ones that
    # lost the distance sort are never shown. A station is left without one —
    # its platforms carry the direction, and the station itself has none.
    for st in out:
        if st["is_station"]:
            continue
        hd = stop_heading(region, st["stop_id"])
        if hd:
            st["heading"], st["bearing"] = hd
    return out

def _heading_fields(region: str, stop) -> dict:
    """The heading pair as payload fields, or empty for a station."""
    if stop["location_type"] == 1:
        return {}
    hd = stop_heading(region, stop["stop_id"])
    return {"heading": hd[0], "bearing": hd[1]} if hd else {}


# ---------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def sibling_stops(region: str, stop_id: str, parent: str = "") -> tuple:
    """Same-physical-station stops in sibling feeds, as (region, stop_id)
    pairs, the anchor itself excluded. Cached: the answer changes only when a
    timetable is re-ingested, and a stale sibling merely 404s its (skipped)
    board lookup below."""
    sibs: list[tuple[str, str]] = []
    if region in TSN_REGIONS and ":" in stop_id:
        tsn = stop_id.split(":", 1)[1]
        for rgn in TSN_REGIONS:
            if not REGIONS[rgn]["db"].exists():
                continue
            con = db(rgn)
            try:
                sibs += [
                    (rgn, r["stop_id"])
                    for r in con.execute(
                        "SELECT stop_id FROM stops "
                        "WHERE substr(stop_id, instr(stop_id, ':') + 1) = ?",
                        (tsn,),
                    )
                ]
            finally:
                con.close()
    for link in STATION_LINKS:
        if {(region, stop_id), (region, parent)} & link:
            sibs += [s for s in link if REGIONS[s[0]]["db"].exists()]
    return tuple(s for s in dict.fromkeys(sibs) if s != (region, stop_id))


def _candidate_rows(region: str, stop_id: str):
    """One region's departure candidates for one stop: scheduled rows merged
    with realtime, deduped across the two service dates, every row tagged with
    its source region. Returns (stop_row, rows); 404s if the stop is unknown
    in this region's timetable."""
    cfg, st = region_cfg(region), STATE[region]
    con = db(region)
    stop = con.execute(
        "SELECT stop_id, stop_name, location_type, stop_lat, stop_lon, "
        "parent_station FROM stops WHERE stop_id=?",
        (stop_id,),
    ).fetchone()
    if stop is None:
        con.close()
        raise HTTPException(404, f"Unknown stop_id {stop_id}")

    # A parent station (e.g. a train station) has no stop_times of its own;
    # departures live on its child platform stops, so query all of them.
    stop_ids = [stop_id]
    children = [
        r["stop_id"]
        for r in con.execute(
            "SELECT stop_id FROM stops WHERE parent_station=?", (stop_id,)
        )
    ]
    stop_ids.extend(children)

    now = datetime.now(cfg["tz"])
    # include yesterday's service date to catch after-midnight (25:xx) trips
    tz = cfg["tz"]
    sched = scheduled_departures(con, stop_ids, now, tz) + scheduled_departures(
        con, stop_ids, now - timedelta(days=1), tz
    )
    con.close()

    now_epoch = int(now.timestamp())
    horizon = now_epoch + LOOKAHEAD_MINUTES * 60
    results = []
    for dep in sched:
        rt_trip = st["rt"].get(dep["trip_id"]) or {}
        rt = rt_trip.get("stops", {}).get(dep["stop_id"])
        realtime = False
        best = dep["scheduled"]
        if rt:
            if rt.get("skipped"):
                continue
            if rt.get("arrival"):
                best, realtime = rt["arrival"], True
            elif rt.get("delay") is not None:
                best, realtime = dep["scheduled"] + rt["delay"], True
        elif rt_trip.get("seq"):
            # No update for this exact stop: propagate per the GTFS-RT spec —
            # the latest update at-or-before this stop's sequence carries its
            # delay forward. Melbourne trams/buses publish only the next stop
            # or two, so most of a run's stops rely on this.
            prop = None
            seq = dep["stop_sequence"]
            for s, delay in rt_trip["seq"]:
                if seq is not None and int(s) <= int(seq):
                    prop = delay
                else:
                    break
            if prop is not None:
                best, realtime = dep["scheduled"] + prop, True
        if now_epoch - 60 <= best <= horizon:
            results.append(
                {
                    **dep,
                    "region": region,
                    "predicted": best,
                    "minutes": max(0, round((best - now_epoch) / 60)),
                    "realtime": realtime,
                }
            )

    # Both service dates are queried, so a trip whose service runs on each of
    # them produces two rows 24h apart. The stale one normally falls outside the
    # window — but an absolute realtime arrival overwrites *both* copies with
    # the same prediction, so both survive. That is why doubled-up rows only
    # ever appeared on services carrying realtime. Keep the copy whose schedule
    # sits closest to the prediction; that is the run actually being reported.
    best: dict = {}
    for r in results:
        key = (r["trip_id"], r["stop_id"])
        prev = best.get(key)
        if prev is None or abs(r["predicted"] - r["scheduled"]) < abs(
            prev["predicted"] - prev["scheduled"]
        ):
            best[key] = r
    return stop, list(best.values())


@router.get("/api/r/{region}/departures/{stop_id}")
@router.get("/api/departures/{stop_id}")
def departures(stop_id: str, region: str = "seq"):
    stop, results = _candidate_rows(region, stop_id)

    # Union the same physical station's boards from sibling feeds (Central's
    # suburban trains + metro + light rail + TrainLink; Southern Cross's
    # V/Line + the XPT). Track which station ids were queried per region —
    # alerts below are matched against that region's own station ids.
    station_of: dict = {region: {stop_id}}
    for rgn, sid in sibling_stops(region, stop_id, stop["parent_station"] or ""):
        try:
            _, more = _candidate_rows(rgn, sid)
        except HTTPException:
            continue                 # sibling id gone after a re-ingest: skip
        results.extend(more)
        station_of.setdefault(rgn, set()).add(sid)

    # A parent among the siblings expands to children a sibling already
    # covered, so the same physical departure can arrive twice — keep one.
    seen_rows: dict = {}
    for r in results:
        seen_rows.setdefault((r["region"], r["trip_id"], r["stop_id"]), r)
    results = list(seen_rows.values())

    # TfNSW also publishes some services in two feeds at once (the Hunter
    # line rides in both sydneytrains and nswtrains): different trip ids,
    # but the same platform TSN, route and minute is one physical train.
    # Keep the copy that carries realtime, else the anchor region's.
    def _phys_key(r):
        sid = r["stop_id"]
        return (sid.split(":", 1)[1] if ":" in sid else sid,
                r["route"], r["scheduled"])

    def _phys_pref(r):  # lower sorts better
        return (not r["realtime"], r["region"] != region)

    phys: dict = {}
    for r in results:
        k = _phys_key(r)
        if k not in phys or _phys_pref(r) < _phys_pref(phys[k]):
            phys[k] = r
    results = list(phys.values())

    now_epoch = int(time.time())
    results.sort(key=lambda d: d["predicted"])

    # Board == map: a departure is listed only if we can put it on the map. Work
    # out a position for every candidate first — live GPS, else a timetable
    # estimate (which yields a point only when the trip is en route or staging
    # within STAGING_WINDOW_S) — then keep just the ones we can place, and cut to
    # MAX_RESULTS from those. A not-yet-departed run with no position is shown in
    # neither the board nor the map, so the two never disagree and a wobbling
    # time boundary moves a service in and out of both together.
    # Positions come from each row's own region: its VP cache, its DB for the
    # dead-reckoning. Keyed (region, trip_id) — trip ids only promise
    # uniqueness within one feed.
    gps: dict = {}
    estimated: dict = {}
    by_region: dict = {}
    for d in results:
        by_region.setdefault(d["region"], []).append(d)
    for rgn, rows in by_region.items():
        vp = STATE[rgn]["vp"]
        got = {d["trip_id"]: vp.get(d["trip_id"]) for d in rows}
        got = {tid: v for tid, v in got.items() if v}
        need_estimate = [d for d in rows if d["trip_id"] not in got]
        con_e = db(rgn)
        est = estimate_ghost_positions(con_e, need_estimate, now_epoch)
        con_e.close()
        for tid, v in got.items():
            gps[(rgn, tid)] = v
        for tid, v in est.items():
            estimated[(rgn, tid)] = v

    trackable = [
        d for d in results
        if (d["region"], d["trip_id"]) in gps
        or (d["region"], d["trip_id"]) in estimated
    ]
    shown = trackable[:MAX_RESULTS]

    vehicles = [
        {
            "trip_id": d["trip_id"],
            "region": d["region"],
            "route": d["route"],
            "route_color": d["route_color"],
            "headsign": d["headsign"],
            "minutes": d["minutes"],
            **gps[(d["region"], d["trip_id"])],
        }
        for d in shown
        if (d["region"], d["trip_id"]) in gps
    ]
    # The rest are en route or staging: a ghost, dead-reckoned from the timetable
    # and drawn distinct from a live fix.
    ghosts = [
        {
            "trip_id": d["trip_id"],
            "region": d["region"],
            "route": d["route"],
            "headsign": d["headsign"],
            "minutes": d["minutes"],
            "lat": estimated[(d["region"], d["trip_id"])]["lat"],
            "lon": estimated[(d["region"], d["trip_id"])]["lon"],
            "estimated": True,
        }
        for d in shown
        if (d["region"], d["trip_id"]) not in gps
    ]

    # The stop across the road. A street-side stop (bus, tram) is almost always
    # one of a pair — same road, opposite directions — and "I want to go the
    # other way" is the next thing a rider looks for. Return every stop within
    # ~120 m that is not this stop or one of its own platforms, so the map can
    # keep them visible in grey. Stations pair with nothing (their platforms
    # are already merged).
    paired = []
    if stop["stop_lat"] is not None and stop["location_type"] != 1:
        con_p = db(region)
        dlat = 0.0011  # ~120 m
        dlon = 0.0011 / max(0.2, math.cos(math.radians(stop["stop_lat"])))
        own = {stop_id} | {
            r["stop_id"]
            for r in con_p.execute(
                "SELECT stop_id FROM stops WHERE parent_station=?", (stop_id,)
            )
        }
        for r in con_p.execute(
            """
            SELECT stop_id, stop_name, stop_lat, stop_lon
            FROM stops
            WHERE (parent_station IS NULL OR parent_station = '')
              AND location_type IS NOT 1
              AND stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?
            """,
            (stop["stop_lat"] - dlat, stop["stop_lat"] + dlat,
             stop["stop_lon"] - dlon, stop["stop_lon"] + dlon),
        ):
            if r["stop_id"] in own:
                continue
            rt = con_p.execute(
                """
                SELECT rt.route_type, COUNT(*) AS c FROM stop_times st
                JOIN trips t ON t.trip_id = st.trip_id
                JOIN routes rt ON rt.route_id = t.route_id
                WHERE st.stop_id = ? GROUP BY rt.route_type
                ORDER BY c DESC LIMIT 1
                """,
                (r["stop_id"],),
            ).fetchone()
            paired.append(
                {
                    "stop_id": r["stop_id"],
                    "stop_name": r["stop_name"],
                    "lat": r["stop_lat"],
                    "lon": r["stop_lon"],
                    "route_type": rt["route_type"] if rt else None,
                }
            )
        con_p.close()
        paired = paired[:6]   # a busy corner, not the whole precinct

    # Disruption alerts, matched by route and by stop (SEQ publishes no
    # trip-level alerts). Each row carries indices into a single response-level
    # map, so an alert spanning half the board is sent once, not twelve times.
    used_alerts: dict = {}
    for dep in shown:
        rgn = dep["region"]
        al = STATE[rgn]["al"]
        ids = list(al["by_route"].get(dep["route_id"], []))
        for sid in (dep["stop_id"], *station_of.get(rgn, ())):
            ids.extend(al["by_stop"].get(sid, []))
        # Ids are namespaced by region — each region numbers its own alert
        # list from zero, and a merged board draws from several at once.
        dep["alert_ids"] = [f"{rgn}:{i}" for i in sorted(set(ids))]
        for key in dep["alert_ids"]:
            used_alerts[key] = al["alerts"][int(key.split(":", 1)[1])]

    # Tag every departure with the shape its trip follows, so any row on the
    # board can have its route drawn on demand — not just the tracked ones. The
    # geometry itself is fetched separately and cached by the client: it never
    # changes, and resending thousands of points on each 15s poll would dwarf
    # the part of the payload that does.
    shape_of: dict = {}
    for rgn in {d["region"] for d in shown}:
        trips = [d["trip_id"] for d in shown if d["region"] == rgn]
        con3 = db(rgn)
        marks = ",".join("?" for _ in trips)
        for r in con3.execute(
            f"SELECT trip_id, shape_id FROM trips WHERE trip_id IN ({marks})",
            trips,
        ):
            shape_of[(rgn, r["trip_id"])] = r["shape_id"]
        con3.close()
    for d in shown:
        d["shape_id"] = shape_of.get((d["region"], d["trip_id"]))
    for v in vehicles:
        v["shape_id"] = shape_of.get((v["region"], v["trip_id"]))

    # Feed status spans every region on the board: a keyless mel board that
    # borrows nsw's live XPT rows must not claim "timetable only".
    merged = list(station_of)
    ages_rt = [STATE[r]["rt_fetch"] for r in merged if STATE[r]["rt_fetch"]]
    ages_vp = [STATE[r]["vp_fetch"] for r in merged if STATE[r]["vp_fetch"]]
    return {
        "stop": {**dict(stop), **_heading_fields(region, stop)},
        "generated_at": now_epoch,
        # False = NO merged region has realtime feeds configured (no key):
        # the status must say "timetable only", not "waiting for first fetch".
        "realtime_configured": any(
            bool(REGIONS[r]["trip_updates"] or REGIONS[r]["vehicle_positions"])
            for r in merged
        ),
        "realtime_feed_age": (
            round(time.time() - max(ages_rt)) if ages_rt else None
        ),
        "vehicle_feed_age": (
            round(time.time() - max(ages_vp)) if ages_vp else None
        ),
        "departures": shown,
        "vehicles": vehicles,
        "ghosts": ghosts,
        "alerts": used_alerts,
        "paired": paired,
    }

def _stop_name(con, stop_id: str) -> str:
    r = con.execute("SELECT stop_name FROM stops WHERE stop_id=?",
                    (stop_id,)).fetchone()
    return r["stop_name"] if r else stop_id


@router.get("/api/r/{region}/trip-stops/{trip_id}")
@router.get("/api/trip-stops/{trip_id}")
def trip_stops(trip_id: str, region: str = "seq"):
    """The stops one trip calls at, in order.

    Fetched only when a service is selected, and static for the life of a
    timetable — so it is served apart from the departures poll and cached.
    """
    con = db(region)
    stops = [
        {
            "stop_id": r["stop_id"],
            "stop_name": r["stop_name"],
            "lat": r["stop_lat"],
            "lon": r["stop_lon"],
            "route_type": r["route_type"],
            # GTFS wall-clock in the region's timezone, "HH:MM:SS", hours may
            # exceed 24 on after-midnight runs. Kept as text: an epoch would
            # need the service date, and this response is cached for a day.
            "sched": r["departure_time"] or r["arrival_time"],
            # For pairing with trip-times' delay propagation list.
            "seq": r["stop_sequence"],
        }
        for r in con.execute(
            """
            SELECT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, r.route_type,
                   st.departure_time, st.arrival_time, st.stop_sequence
            FROM stop_times st
            JOIN stops s  ON s.stop_id = st.stop_id
            JOIN trips t  ON t.trip_id = st.trip_id
            JOIN routes r ON r.route_id = t.route_id
            WHERE st.trip_id = ?
              AND s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
            ORDER BY st.stop_sequence
            """,
            (trip_id,),
        )
    ]
    con.close()
    if not stops:
        raise HTTPException(404, f"No stops for trip {trip_id}")
    return JSONResponse(
        {"trip_id": trip_id, "stops": stops},
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/r/{region}/trip-times/{trip_id}")
@router.get("/api/trip-times/{trip_id}")
def trip_times(trip_id: str, region: str = "seq"):
    """Live per-stop arrivals for one trip, from the TripUpdates cache.

    Kept apart from trip-stops: that list is static for the life of a
    timetable and cached for a day, while this overlay changes every poll.
    Empty when the trip has no realtime — the stop list then shows the
    schedule alone."""
    region_cfg(region)
    st = STATE[region]
    rt = st["rt"].get(trip_id) or {}

    # Only the selected trip's own vehicle. Route-mates were tried and were
    # confusing on the timeline — each stop shows one arrival time, so a
    # second vehicle beside it reads as a contradiction.
    vehicles = []
    v = st["vp"].get(trip_id)
    if v and v.get("lat") is not None:
        vehicles = [{"trip_id": trip_id, "lat": v["lat"], "lon": v["lon"]}]

    return {
        "trip_id": trip_id,
        "stops": {
            sid: {"t": rec.get("arrival"), "delay": rec.get("delay"),
                  "skipped": bool(rec.get("skipped"))}
            for sid, rec in rt.get("stops", {}).items()
        },
        # Sorted (stop_sequence, delay) pairs: an update applies to every
        # later stop until the next update — the same propagation rule the
        # board uses for feeds that only publish the next stop or two.
        "delays": rt.get("seq", []),
        "vehicles": vehicles,
    }


@router.get("/api/r/{region}/shape/{shape_id}")
@router.get("/api/shape/{shape_id}")
def shape(shape_id: str, region: str = "seq"):
    """Geometry of one route path, as [lon, lat] pairs.

    Static for the life of a timetable, so it is served apart from the
    departures poll and marked cacheable.
    """
    con = db(region)
    pts = [
        [r["shape_pt_lon"], r["shape_pt_lat"]]
        for r in con.execute(
            "SELECT shape_pt_lon, shape_pt_lat FROM shapes "
            "WHERE shape_id=? ORDER BY shape_pt_sequence",
            (shape_id,),
        )
    ]
    con.close()
    if len(pts) < 2:
        raise HTTPException(404, f"No geometry for shape {shape_id}")
    return JSONResponse(
        {"shape_id": shape_id, "points": pts},
        headers={"Cache-Control": "public, max-age=86400"},
    )


_rail_stations_cache: dict = {}    # region -> list


def rail_stations(region: str = "seq") -> list[dict]:
    """Every rail station in the feed, one marker per physical station. Rail
    platforms (served by route_type 1/2) are collapsed to their parent station,
    so Varsity Lakes is one point, not two platforms. Static for the life of a
    timetable, so it is computed once per region and cached."""
    if region not in _rail_stations_cache:
        con = db(region)
        ids = [
            r["sid"]
            for r in con.execute(
                """
                SELECT DISTINCT COALESCE(NULLIF(s.parent_station, ''), s.stop_id) AS sid
                FROM stops s
                WHERE s.stop_id IN (
                    SELECT DISTINCT st.stop_id FROM stop_times st
                    WHERE st.trip_id IN (
                        SELECT t.trip_id FROM trips t
                        JOIN routes r ON r.route_id = t.route_id
                        WHERE r.route_type IN (1, 2)
                    )
                )
                """
            )
        ]
        out = []
        if ids:
            marks = ",".join("?" for _ in ids)
            out = [
                {
                    "stop_id": r["stop_id"],
                    "name": r["stop_name"],
                    "lat": r["stop_lat"],
                    "lon": r["stop_lon"],
                }
                for r in con.execute(
                    f"SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops "
                    f"WHERE stop_id IN ({marks}) "
                    f"AND stop_lat IS NOT NULL AND stop_lon IS NOT NULL",
                    ids,
                )
            ]
        con.close()
        _rail_stations_cache[region] = out
    return _rail_stations_cache[region]


_all_stops_cache: dict = {}    # region -> list


def all_stops(region: str) -> list[dict]:
    """Every parentless stop with its dominant mode, for the zoomed-in map
    layer that shows the whole street furniture. One GROUP BY over stop_times
    is seconds on the big Melbourne DB, so computed once per region and cached;
    static for the life of a timetable."""
    if region not in _all_stops_cache:
        con = db(region)
        # Dominant route_type per stop in one pass. Parent stations carry no
        # stop_times of their own, so each child's counts also accrue to its
        # parent — otherwise every ferry terminal and bus interchange (parents
        # whose platforms hold the departures) would default to "bus stop".
        parent_of = {
            r["stop_id"]: r["parent_station"]
            for r in con.execute(
                "SELECT stop_id, parent_station FROM stops "
                "WHERE parent_station IS NOT NULL AND parent_station != ''")
        }
        counts: dict = {}
        for r in con.execute(
            """
            SELECT st.stop_id AS sid, rt.route_type AS rtype, COUNT(*) AS c
            FROM stop_times st
            JOIN trips t ON t.trip_id = st.trip_id
            JOIN routes rt ON rt.route_id = t.route_id
            GROUP BY st.stop_id, rt.route_type
            """
        ):
            for sid in {r["sid"], parent_of.get(r["sid"])} - {None, ""}:
                counts[(sid, r["rtype"])] = counts.get((sid, r["rtype"]), 0) + r["c"]
        dominant = {}
        for (sid, rtype), c in counts.items():
            if sid not in dominant or c > dominant[sid][1]:
                dominant[sid] = (rtype, c)
        # Rail stations are parent records with no stop_times of their own
        # (their platforms carry the departures), so the dominant-mode lookup
        # misses them and they would read as bus stops. Tag them rail instead —
        # otherwise every stop is just a stop, drawn at the same zoom.
        rail_ids = {s["stop_id"] for s in rail_stations(region)}
        out = [
            {
                "stop_id": r["stop_id"],
                "name": r["stop_name"],
                "lat": r["stop_lat"],
                "lon": r["stop_lon"],
                "route_type": 2 if r["stop_id"] in rail_ids
                              else dominant.get(r["stop_id"], (3, 0))[0],
            }
            for r in con.execute(
                "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops "
                "WHERE (parent_station IS NULL OR parent_station = '') "
                "AND stop_lat IS NOT NULL AND stop_lon IS NOT NULL"
            )
        ]
        con.close()
        _all_stops_cache[region] = out
    return _all_stops_cache[region]


_stop_mode_cache: dict = {}


def stop_modes(region: str) -> dict:
    """stop_id -> dominant route_type, derived from the all_stops cache. Lets
    the search results carry the right mode glyph without their own GROUP BY."""
    if region not in _stop_mode_cache:
        _stop_mode_cache[region] = {
            s["stop_id"]: s["route_type"] for s in all_stops(region)
        }
    return _stop_mode_cache[region]


@router.get("/api/r/{region}/all-stops")
@router.get("/api/all-stops")
def all_stops_endpoint(region: str = "seq"):
    """Every stop in the network, fetched lazily by the map the first time the
    user zooms in far enough to want them."""
    return JSONResponse(
        {"stops": all_stops(region)},
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/r/{region}/rail-stations")
@router.get("/api/rail-stations")
def rail_stations_endpoint(region: str = "seq"):
    """Train stations, drawn on the map as navigation landmarks regardless of
    which stop or route is selected — if the map can show one, it should."""
    return JSONResponse(
        {"stations": rail_stations(region)},
        headers={"Cache-Control": "public, max-age=86400"},
    )
