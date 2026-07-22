"""
"Next service arriving" backend for Translink SEQ.

- Polls the GTFS-RT TripUpdates feed every POLL_SECONDS in the background.
- /api/departures/{stop_id} merges today's scheduled departures (from the
  static GTFS in gtfs.sqlite3, built by ingest_gtfs.py) with realtime
  predictions, and returns clean JSON for the frontend.
- /api/stops/search?q=... finds stop IDs by name.

Run:  uvicorn app:app --reload
"""

import asyncio
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.transit import gtfs_realtime_pb2

BASE = Path(__file__).parent
# Overridable so the DB can live on a mounted volume (see Containerfile).
DB_PATH = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")
TRIP_UPDATES_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
VEHICLE_POSITIONS_URL = (
    "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions"
)
# Basemap for the map view: a Protomaps .pmtiles extract of SEQ, built by
# fetch_basemap.sh onto the same volume as the timetable. Absent is fine — the
# frontend hides the map rather than failing.
BASEMAP_DIR = Path(os.environ.get("BASEMAP_DIR") or BASE / "basemap")
BASEMAP_FILE = BASEMAP_DIR / "seq.pmtiles"
POLL_SECONDS = 30
LOOKAHEAD_MINUTES = 90
MAX_RESULTS = 12
# GTFS times are in the agency's local time, NOT the host's. Pinning this makes
# the board correct under a UTC container clock, which is the normal case in a
# container and was previously shifting every scheduled time by 10 hours.
# Brisbane has no DST, but being explicit costs nothing.
AGENCY_TZ = ZoneInfo("Australia/Brisbane")

# ---------------------------------------------------------------------------
# Realtime cache: {trip_id: {stop_id: {"arrival": epoch|None, "delay": s|None}}}
# ---------------------------------------------------------------------------
rt_cache: dict = {}
rt_last_fetch: float | None = None

# Vehicle positions: {trip_id: {"lat", "lon", "bearing", "status", "timestamp"}}
vp_cache: dict = {}
vp_last_fetch: float | None = None

# Per-poll feed health, so drops are counted rather than silently skipped and
# can be inspected at /api/feeds. Translink's VehiclePositions feed has always
# carried a position on every entity, so `without_position` is a canary: if it
# ever goes non-zero, vehicles are missing from the map and the log will say so.
tu_stats: dict = {}
vp_stats: dict = {}


async def poll_trip_updates() -> None:
    global rt_cache, rt_last_fetch, tu_stats
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(TRIP_UPDATES_URL)
                resp.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(resp.content)

                cache: dict = {}
                n_updates = n_no_trip = 0
                for entity in feed.entity:
                    if not entity.HasField("trip_update"):
                        continue
                    tu = entity.trip_update
                    trip_id = tu.trip.trip_id
                    n_updates += 1
                    if not trip_id:
                        n_no_trip += 1
                    stops: dict = {}
                    for stu in tu.stop_time_update:
                        rec: dict = {"arrival": None, "delay": None}
                        ev = None
                        if stu.HasField("arrival"):
                            ev = stu.arrival
                        elif stu.HasField("departure"):
                            ev = stu.departure
                        if ev is not None:
                            if ev.HasField("time") and ev.time:
                                rec["arrival"] = ev.time
                            if ev.HasField("delay"):
                                rec["delay"] = ev.delay
                        rec["skipped"] = (
                            stu.schedule_relationship
                            == stu.ScheduleRelationship.SKIPPED
                        )
                        stops[stu.stop_id] = rec
                    cache[trip_id] = stops
                rt_cache = cache
                rt_last_fetch = time.time()
                tu_stats = {"trip_updates": n_updates, "without_trip_id": n_no_trip}
                print(f"[tu] {n_updates} trip updates ({len(cache)} trips cached)"
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
            except Exception as exc:  # keep serving scheduled times on failure
                print(f"[poll] realtime fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


async def poll_vehicle_positions() -> None:
    """Live GPS for the map view. Keyed by trip_id so a departure on the board
    can be matched to the vehicle actually running it."""
    global vp_cache, vp_last_fetch, vp_stats
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(VEHICLE_POSITIONS_URL)
                resp.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(resp.content)

                cache: dict = {}
                n_total = n_pos = n_no_pos = n_no_trip = 0
                for entity in feed.entity:
                    if not entity.HasField("vehicle"):
                        continue
                    v = entity.vehicle
                    n_total += 1
                    # Count the drops rather than skip them silently — this is
                    # the "are we losing live vehicles?" question, answered.
                    if not v.trip.trip_id:
                        n_no_trip += 1
                        continue
                    if not v.HasField("position"):
                        n_no_pos += 1
                        continue
                    n_pos += 1
                    pos = v.position
                    cache[v.trip.trip_id] = {
                        "lat": pos.latitude,
                        "lon": pos.longitude,
                        "bearing": pos.bearing if pos.HasField("bearing") else None,
                        "status": v.current_status,
                        "timestamp": v.timestamp or None,
                    }
                vp_cache = cache
                vp_last_fetch = time.time()
                # Two vehicles can carry the same trip_id (a trip handed between
                # buses, or overlapping runs); the cache is keyed by trip_id, so
                # the later one wins. That collapse — not a dropped position — is
                # what separates `positioned` from `cached`.
                dup = n_pos - len(cache)
                vp_stats = {
                    "vehicles": n_total,
                    "positioned": n_pos,
                    "cached": len(cache),
                    "duplicate_trip_id": dup,
                    "without_position": n_no_pos,
                    "without_trip_id": n_no_trip,
                }
                print(f"[vp] {n_total} vehicles: {n_pos} positioned, {len(cache)} cached"
                      + (f", {dup} dup trip_id" if dup else "")
                      + (f", {n_no_pos} WITHOUT position" if n_no_pos else "")
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
                # A live vehicle with no coordinates cannot go on the map. It has
                # never happened on this feed; if it starts, this is the alarm.
                if n_no_pos:
                    print(f"[vp] WARNING: {n_no_pos} live vehicles broadcast with "
                          f"no position and were dropped from the map")
            except Exception as exc:  # the board must survive a map outage
                print(f"[poll] vehicle positions fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(poll_trip_updates()),
        asyncio.create_task(poll_vehicle_positions()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Translink Next Service", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static GTFS helpers
# ---------------------------------------------------------------------------


def db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(500, "gtfs.sqlite3 not found - run ingest_gtfs.py first")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def active_service_ids(con: sqlite3.Connection, service_date: datetime) -> set[str]:
    """Service IDs running on a given service date (calendar + exceptions)."""
    ymd = service_date.strftime("%Y%m%d")
    weekday_col = ["monday", "tuesday", "wednesday", "thursday",
                   "friday", "saturday", "sunday"][service_date.weekday()]
    ids = {
        r["service_id"]
        for r in con.execute(
            f"SELECT service_id FROM calendar "
            f"WHERE {weekday_col}=1 AND start_date<=? AND end_date>=?",
            (ymd, ymd),
        )
    }
    for r in con.execute(
        "SELECT service_id, exception_type FROM calendar_dates WHERE date=?", (ymd,)
    ):
        if int(r["exception_type"]) == 1:
            ids.add(r["service_id"])
        else:
            ids.discard(r["service_id"])
    return ids


def gtfs_time_to_epoch(hms: str, service_date: datetime) -> int:
    """GTFS times can exceed 24:00:00 for after-midnight trips.

    The offset is applied to midnight *in the agency's timezone*: a naive
    datetime would be interpreted in the host's zone, so a UTC container would
    read every scheduled time 10 hours late.
    """
    h, m, s = (int(x) for x in hms.split(":"))
    midnight = service_date.astimezone(AGENCY_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight + timedelta(hours=h, minutes=m, seconds=s)).timestamp())


def scheduled_departures(con, stop_ids: list[str], service_date: datetime) -> list[dict]:
    sids = active_service_ids(con, service_date)
    if not sids or not stop_ids:
        return []
    stop_marks = ",".join("?" for _ in stop_ids)
    svc_marks = ",".join("?" for _ in sids)
    rows = con.execute(
        f"""
        SELECT st.trip_id, st.departure_time, st.stop_id, t.trip_headsign,
               r.route_short_name, r.route_long_name, r.route_type, r.route_color,
               s.platform_code, s.stop_name AS platform_stop_name
        FROM stop_times st
        JOIN trips t  ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        JOIN stops s  ON s.stop_id = st.stop_id
        WHERE st.stop_id IN ({stop_marks}) AND t.service_id IN ({svc_marks})
        """,
        (*stop_ids, *sids),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "trip_id": r["trip_id"],
                "stop_id": r["stop_id"],
                "scheduled": gtfs_time_to_epoch(r["departure_time"], service_date),
                "headsign": r["trip_headsign"],
                "route": r["route_short_name"] or r["route_long_name"],
                "route_type": r["route_type"],
                "route_color": r["route_color"],
                "platform": platform_label(r["platform_code"], r["platform_stop_name"]),
            }
        )
    return out


PLATFORM_RE = re.compile(r"platform\s+(\w+)", re.IGNORECASE)


def platform_label(platform_code: str | None, stop_name: str | None) -> str | None:
    if platform_code:
        return platform_code
    if stop_name:
        m = PLATFORM_RE.search(stop_name)
        if m:
            return m.group(1)
    return None


def _seconds_into_day(hms: str) -> int:
    """GTFS clock string to seconds past the service day's midnight. Values can
    exceed 24h for after-midnight trips; kept as an offset so it composes with a
    single midnight anchor and stays monotonic across the 24:00 boundary."""
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def _interpolate_along(nodes: list[tuple[int, float, float]], now: int):
    """nodes = [(epoch, lat, lon), ...] in schedule order. Return the point the
    timetable places the vehicle at `now`, linearly interpolated between the two
    stops that bracket it — or None when the trip is not on the road: before it
    departs its origin, or after it reaches its final stop. Not clamping to the
    origin is deliberate: a run scheduled to start in 40 minutes is not sitting
    at the depot to be drawn — showing it there just piles phantom buses on the
    route start."""
    if now < nodes[0][0] or now >= nodes[-1][0]:
        return None
    for (t0, a0, o0), (t1, a1, o1) in zip(nodes, nodes[1:]):
        if t0 <= now < t1:
            f = (now - t0) / (t1 - t0) if t1 > t0 else 0.0
            return {"lat": a0 + (a1 - a0) * f, "lon": o0 + (o1 - o0) * f}
    return None


def estimate_ghost_positions(con, deps: list[dict], now_epoch: int) -> dict:
    """Where the timetable *says* each trip should be right now — for trips with
    no live GPS. Interpolates along the trip's scheduled stops, anchored to the
    board departure we already resolved (so the service date, incl. after-
    midnight runs, is correct without re-deriving it). Returns {trip_id: {lat,
    lon}}. An estimate, not a fix: it assumes the service is running to time."""
    if not deps:
        return {}
    trip_ids = [d["trip_id"] for d in deps]
    marks = ",".join("?" for _ in trip_ids)
    rows = con.execute(
        f"""
        SELECT st.trip_id, st.stop_id, st.stop_sequence,
               st.departure_time, st.arrival_time, s.stop_lat, s.stop_lon
        FROM stop_times st
        JOIN stops s ON s.stop_id = st.stop_id
        WHERE st.trip_id IN ({marks})
          AND s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
        ORDER BY st.trip_id, st.stop_sequence
        """,
        trip_ids,
    ).fetchall()

    by_trip: dict = {}
    for r in rows:
        by_trip.setdefault(r["trip_id"], []).append(r)

    anchor = {d["trip_id"]: d for d in deps}
    out: dict = {}
    for tid, strows in by_trip.items():
        d = anchor[tid]
        # Anchor the whole trip's clock to real epochs using the one stop whose
        # epoch we already know: midnight = board_scheduled - board_offset.
        board = next((r for r in strows if r["stop_id"] == d["stop_id"]), None)
        board_hms = board and (board["departure_time"] or board["arrival_time"])
        if not board_hms:
            continue
        midnight = d["scheduled"] - _seconds_into_day(board_hms)

        nodes = []
        for r in strows:
            hms = r["departure_time"] or r["arrival_time"]
            if not hms:
                continue
            nodes.append((midnight + _seconds_into_day(hms), r["stop_lat"], r["stop_lon"]))
        if len(nodes) < 2:
            continue
        pos = _interpolate_along(nodes, now_epoch)
        if pos:
            out[tid] = pos
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/stops/search")
def search_stops(q: str):
    """Match stops by name. Parent stations rank first; their individual
    platforms are hidden so a station appears once (select the station to
    see all platforms combined)."""
    con = db()
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
    return [
        {
            "stop_id": r["stop_id"],
            "stop_name": r["stop_name"],
            "is_station": r["location_type"] == 1,
        }
        for r in rows
    ]


@app.get("/api/departures/{stop_id}")
def departures(stop_id: str):
    con = db()
    stop = con.execute(
        "SELECT stop_id, stop_name, location_type, stop_lat, stop_lon "
        "FROM stops WHERE stop_id=?",
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

    now = datetime.now(AGENCY_TZ)
    # include yesterday's service date to catch after-midnight (25:xx) trips
    sched = scheduled_departures(con, stop_ids, now) + scheduled_departures(
        con, stop_ids, now - timedelta(days=1)
    )
    con.close()

    now_epoch = int(now.timestamp())
    horizon = now_epoch + LOOKAHEAD_MINUTES * 60
    results = []
    for dep in sched:
        rt = rt_cache.get(dep["trip_id"], {}).get(dep["stop_id"])
        realtime = False
        best = dep["scheduled"]
        if rt:
            if rt.get("skipped"):
                continue
            if rt.get("arrival"):
                best, realtime = rt["arrival"], True
            elif rt.get("delay") is not None:
                best, realtime = dep["scheduled"] + rt["delay"], True
        if now_epoch - 60 <= best <= horizon:
            results.append(
                {
                    **dep,
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
    results = list(best.values())

    results.sort(key=lambda d: d["predicted"])
    shown = results[:MAX_RESULTS]

    # Live positions for exactly the trips on the board, so the map never shows
    # a vehicle the user has no row for. Most trips have no GPS at any moment.
    vehicles = []
    for dep in shown:
        v = vp_cache.get(dep["trip_id"])
        if not v:
            continue
        vehicles.append(
            {
                "trip_id": dep["trip_id"],
                "route": dep["route"],
                "route_color": dep["route_color"],
                "headsign": dep["headsign"],
                "minutes": dep["minutes"],
                **v,
            }
        )

    # Every remaining board trip has a schedule but no GPS. Dead-reckon a
    # position from its timetable so the map can show it as a *ghost* — distinct
    # from a live fix — instead of leaving the row with no marker at all. This is
    # what reconciles the board and the map: live where GPS exists, estimated
    # where only the timetable does.
    gps_trips = {v["trip_id"] for v in vehicles}
    need_estimate = [d for d in shown if d["trip_id"] not in gps_trips]
    con_e = db()
    estimated = estimate_ghost_positions(con_e, need_estimate, now_epoch)
    con_e.close()
    ghosts = [
        {
            "trip_id": d["trip_id"],
            "route": d["route"],
            "headsign": d["headsign"],
            "minutes": d["minutes"],
            "lat": estimated[d["trip_id"]]["lat"],
            "lon": estimated[d["trip_id"]]["lon"],
            "estimated": True,
        }
        for d in need_estimate
        if d["trip_id"] in estimated
    ]

    # Tag every departure with the shape its trip follows, so any row on the
    # board can have its route drawn on demand — not just the tracked ones. The
    # geometry itself is fetched separately and cached by the client: it never
    # changes, and resending thousands of points on each 15s poll would dwarf
    # the part of the payload that does.
    tracked_trips = [v["trip_id"] for v in vehicles]
    shown_trips = [d["trip_id"] for d in shown]
    if shown_trips:
        con3 = db()
        marks = ",".join("?" for _ in shown_trips)
        shape_of = {
            r["trip_id"]: r["shape_id"]
            for r in con3.execute(
                f"SELECT trip_id, shape_id FROM trips WHERE trip_id IN ({marks})",
                shown_trips,
            )
        }
        con3.close()
        for d in shown:
            d["shape_id"] = shape_of.get(d["trip_id"])
        for v in vehicles:
            v["shape_id"] = shape_of.get(v["trip_id"])

    return {
        "stop": dict(stop),
        "generated_at": now_epoch,
        "realtime_feed_age": (
            round(time.time() - rt_last_fetch) if rt_last_fetch else None
        ),
        "vehicle_feed_age": (
            round(time.time() - vp_last_fetch) if vp_last_fetch else None
        ),
        "departures": shown,
        "vehicles": vehicles,
        "ghosts": ghosts,
    }


@app.get("/api/trip-stops/{trip_id}")
def trip_stops(trip_id: str):
    """The stops one trip calls at, in order.

    Fetched only when a service is selected, and static for the life of a
    timetable — so it is served apart from the departures poll and cached.
    """
    con = db()
    stops = [
        {
            "stop_id": r["stop_id"],
            "stop_name": r["stop_name"],
            "lat": r["stop_lat"],
            "lon": r["stop_lon"],
            "route_type": r["route_type"],
        }
        for r in con.execute(
            """
            SELECT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, r.route_type
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


@app.get("/api/shape/{shape_id}")
def shape(shape_id: str):
    """Geometry of one route path, as [lon, lat] pairs.

    Static for the life of a timetable, so it is served apart from the
    departures poll and marked cacheable.
    """
    con = db()
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


@app.get("/api/config")
def config():
    """The frontend asks whether a basemap is present before building the map,
    so a deployment without one degrades to a board-only page."""
    return {"basemap": BASEMAP_FILE.exists()}


@app.get("/api/feeds")
def feeds():
    """Realtime feed health for QC: how many trip updates and vehicle positions
    the last poll saw, how many were dropped and why, and how stale each cache
    is. `without_position` is the count of live vehicles with no coordinates —
    the ones that cannot be mapped."""
    now = time.time()
    return {
        "trip_updates": {
            **tu_stats,
            "age_s": round(now - rt_last_fetch) if rt_last_fetch else None,
        },
        "vehicle_positions": {
            **vp_stats,
            "age_s": round(now - vp_last_fetch) if vp_last_fetch else None,
        },
    }


# Frontend
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
# StaticFiles serves HTTP range requests, which is how pmtiles.js reads the
# archive — it fetches byte ranges rather than the whole 22 MB file.
# check_dir=False: the basemap is optional, and the volume may not have one yet.
app.mount(
    "/basemap",
    StaticFiles(directory=BASEMAP_DIR, check_dir=False),
    name="basemap",
)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
