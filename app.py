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
import math
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
# Overridable so the DBs can live on a mounted volume (see Containerfile).
DB_PATH = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")
# Basemaps for the map view: self-built OpenMapTiles .pmtiles, built by
# basemap/build-basemap.sh onto the same volume as the timetables. Absent is
# fine — the frontend hides the map rather than failing.
BASEMAP_DIR = Path(os.environ.get("BASEMAP_DIR") or BASE / "basemap")
POLL_SECONDS = 30
# Alerts change on the scale of hours, not seconds.
ALERT_POLL_SECONDS = 300
LOOKAHEAD_MINUTES = 90
MAX_RESULTS = 12
# A service is shown — on the board AND the map, which must agree — only if we
# can place it: it has live GPS, it is en route, or it is staging to leave its
# origin within this window. A run that hasn't started (its bus still finishing
# an earlier trip under another trip_id, which this feed gives no way to follow)
# has no position and appears in neither. This is the single "is it underway?"
# threshold; widen it to list departures further ahead, at the cost of drawing
# not-yet-moving buses guessed onto their origin.
STAGING_WINDOW_S = 10 * 60
# GTFS times are in the agency's local time, NOT the host's. A naive datetime
# would be read in the system zone; under a UTC container clock that shifted
# every SEQ scheduled time by 10 hours. Each region pins its own zone.
AGENCY_TZ = ZoneInfo("Australia/Brisbane")   # SEQ; kept module-level for tests


def _env_rt_feeds(env_prefix: str, kind: str) -> list[dict]:
    """Keyed regions' GTFS-R endpoints, entirely env-driven.

    Melbourne's and Sydney's realtime feeds need a (free) registered API key
    and the hosts have moved between portals over the years, so nothing is
    hardcoded. Format (same for SYD_*):

        MEL_TRIP_UPDATES="2|https://host/metrotrain-tripupdates;3|https://host/yarratrams-tripupdates"
        MEL_VEHICLE_POSITIONS / MEL_ALERTS  — same shape

    The `2|` is the feed prefix: these regions' static GTFS is one feed per
    mode with ids that are only unique within the feed, so the ingest prefixes
    every id with "<prefix>:" and each realtime feed declares which feed it
    speaks for. Unset means static-only — the board and the timetable ghosts
    still work.
    """
    raw = os.environ.get(f"{env_prefix}_{kind}", "").strip()
    feeds = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        prefix, _, url = part.partition("|")
        feeds.append({"url": url, "prefix": f"{prefix}:" if prefix else ""})
    return feeds


def _mel_headers() -> dict:
    key = os.environ.get("MEL_API_KEY", "").strip()
    if not key:
        return {}
    return {os.environ.get("MEL_API_KEY_HEADER", "Ocp-Apim-Subscription-Key"): key}


def _syd_headers() -> dict:
    """TfNSW auth: `Authorization: apikey <token>` (their scheme word really
    is lowercase 'apikey'). Accepts the bare token in SYD_API_KEY and adds
    the scheme, or a full 'apikey …' value as-is."""
    key = os.environ.get("SYD_API_KEY", "").strip()
    if not key:
        return {}
    if not key.lower().startswith("apikey "):
        key = f"apikey {key}"
    return {"Authorization": key}


# ---------------------------------------------------------------------------
# Regions. One board, many networks: each region is a GTFS static DB, a set of
# GTFS-RT feeds, a timezone, and a basemap. The API is region-scoped under
# /api/r/{region}/…; the original /api/… paths remain as aliases for SEQ.
# ---------------------------------------------------------------------------
REGIONS: dict = {
    "seq": {
        "name": "Translink · South East Queensland",
        "state": "QLD",
        "tz": ZoneInfo("Australia/Brisbane"),
        "db": DB_PATH,
        "basemap": BASEMAP_DIR / "seq.pmtiles",
        "trip_updates": [
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates",
             "prefix": ""}],
        "vehicle_positions": [
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions",
             "prefix": ""}],
        "alerts": [
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/Alerts",
             "prefix": ""}],
        "headers": {},
        "geocode_viewbox": "151.8,-28.3,153.6,-26.0",
        "center": [153.026, -27.4705],
    },
    "mel": {
        "name": "PTV · Melbourne",
        "state": "VIC",
        "tz": ZoneInfo("Australia/Melbourne"),
        "db": Path(os.environ.get("MEL_GTFS_DB") or DB_PATH.parent / "gtfs-mel.sqlite3"),
        "basemap": BASEMAP_DIR / "mel.pmtiles",
        "trip_updates": _env_rt_feeds("MEL", "TRIP_UPDATES"),
        "vehicle_positions": _env_rt_feeds("MEL", "VEHICLE_POSITIONS"),
        "alerts": _env_rt_feeds("MEL", "ALERTS"),
        "headers": _mel_headers(),
        "geocode_viewbox": "144.4,-38.5,145.8,-37.4",
        "center": [144.9631, -37.8136],
    },
    "syd": {
        "name": "TfNSW · Sydney",
        "state": "NSW",
        "tz": ZoneInfo("Australia/Sydney"),
        "db": Path(os.environ.get("SYD_GTFS_DB") or DB_PATH.parent / "gtfs-syd.sqlite3"),
        "basemap": BASEMAP_DIR / "syd.pmtiles",
        "trip_updates": _env_rt_feeds("SYD", "TRIP_UPDATES"),
        "vehicle_positions": _env_rt_feeds("SYD", "VEHICLE_POSITIONS"),
        "alerts": _env_rt_feeds("SYD", "ALERTS"),
        "headers": _syd_headers(),
        "geocode_viewbox": "150.5,-34.25,151.4,-33.35",
        "center": [151.2093, -33.8688],
    },
}

# A region is offered to the frontend only once its timetable exists, so a
# deployment that never ingested Melbourne simply doesn't show the switcher.
def available_regions() -> list[str]:
    return [rid for rid, cfg in REGIONS.items() if cfg["db"].exists()]


# Per-region runtime state: realtime caches and feed-health stats. Shapes:
#   rt: {trip_id: {stop_id: {"arrival": epoch|None, "delay": s|None, "skipped"}}}
#   vp: {trip_id: {"lat","lon","bearing","status","timestamp"}}
#   al: {"alerts":[...], "by_route":{}, "by_stop":{}}   (see poll_alerts)
STATE: dict = {
    rid: {
        "rt": {}, "rt_fetch": None, "rt_stats": {},
        "vp": {}, "vp_fetch": None, "vp_stats": {},
        "al": {"alerts": [], "by_route": {}, "by_stop": {}},
        "al_fetch": None, "al_stats": {},
    }
    for rid in REGIONS
}


async def _fetch_feeds(client, cfg: dict, kind: str) -> list[tuple[str, object]]:
    """Fetch every configured GTFS-RT feed of one kind for a region. Returns
    (prefix, FeedMessage) pairs; the prefix maps feed-local ids onto the
    prefixed ids the region's ingest wrote (empty for single-feed regions)."""
    out = []
    for feed_cfg in cfg[kind]:
        resp = await client.get(feed_cfg["url"], headers=cfg["headers"])
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        out.append((feed_cfg["prefix"], feed))
    return out


async def poll_trip_updates(rid: str) -> None:
    cfg, st = REGIONS[rid], STATE[rid]
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                cache: dict = {}
                n_updates = n_no_trip = 0
                for prefix, feed in await _fetch_feeds(client, cfg, "trip_updates"):
                    for entity in feed.entity:
                        if not entity.HasField("trip_update"):
                            continue
                        tu = entity.trip_update
                        trip_id = tu.trip.trip_id
                        n_updates += 1
                        if not trip_id:
                            n_no_trip += 1
                        stops: dict = {}
                        # (stop_sequence, delay) pairs for spec-correct delay
                        # propagation: an update applies to every later stop
                        # until the next update. SEQ and Melbourne trains
                        # enumerate all remaining stops so the exact lookup
                        # suffices, but Melbourne trams/buses publish only the
                        # next stop or two — without propagation every later
                        # stop on the run showed as scheduled.
                        seqs: list = []
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
                            stops[prefix + stu.stop_id] = rec
                            if stu.HasField("stop_sequence") and rec["delay"] is not None:
                                seqs.append((stu.stop_sequence, rec["delay"]))
                        seqs.sort()
                        cache[prefix + trip_id] = {"stops": stops, "seq": seqs}
                st["rt"] = cache
                st["rt_fetch"] = time.time()
                st["rt_stats"] = {"trip_updates": n_updates, "without_trip_id": n_no_trip}
                print(f"[tu:{rid}] {n_updates} trip updates ({len(cache)} trips cached)"
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
            except Exception as exc:  # keep serving scheduled times on failure
                print(f"[poll:{rid}] realtime fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


async def poll_vehicle_positions(rid: str) -> None:
    """Live GPS for the map view. Keyed by trip_id so a departure on the board
    can be matched to the vehicle actually running it."""
    cfg, st = REGIONS[rid], STATE[rid]
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                cache: dict = {}
                n_total = n_pos = n_no_pos = n_no_trip = 0
                for prefix, feed in await _fetch_feeds(client, cfg, "vehicle_positions"):
                    for entity in feed.entity:
                        if not entity.HasField("vehicle"):
                            continue
                        v = entity.vehicle
                        n_total += 1
                        # Count the drops rather than skip them silently — this
                        # is "are we losing live vehicles?", answered.
                        if not v.trip.trip_id:
                            n_no_trip += 1
                            continue
                        if not v.HasField("position"):
                            n_no_pos += 1
                            continue
                        n_pos += 1
                        pos = v.position
                        cache[prefix + v.trip.trip_id] = {
                            "lat": pos.latitude,
                            "lon": pos.longitude,
                            "bearing": pos.bearing if pos.HasField("bearing") else None,
                            "status": v.current_status,
                            "timestamp": v.timestamp or None,
                        }
                st["vp"] = cache
                st["vp_fetch"] = time.time()
                # Two vehicles can carry the same trip_id (a trip handed between
                # buses, or overlapping runs); the cache is keyed by trip_id, so
                # the later one wins. That collapse — not a dropped position — is
                # what separates `positioned` from `cached`.
                dup = n_pos - len(cache)
                st["vp_stats"] = {
                    "vehicles": n_total,
                    "positioned": n_pos,
                    "cached": len(cache),
                    "duplicate_trip_id": dup,
                    "without_position": n_no_pos,
                    "without_trip_id": n_no_trip,
                }
                print(f"[vp:{rid}] {n_total} vehicles: {n_pos} positioned, {len(cache)} cached"
                      + (f", {dup} dup trip_id" if dup else "")
                      + (f", {n_no_pos} WITHOUT position" if n_no_pos else "")
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
                # A live vehicle with no coordinates cannot go on the map. It
                # has never happened on these feeds; if it starts, the alarm:
                if n_no_pos:
                    print(f"[vp:{rid}] WARNING: {n_no_pos} live vehicles broadcast "
                          f"with no position and were dropped from the map")
            except Exception as exc:  # the board must survive a map outage
                print(f"[poll:{rid}] vehicle positions fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


# GTFS-RT Alert.Effect enum -> a short human label for the popup.
ALERT_EFFECT = {
    1: "No service", 2: "Reduced service", 3: "Significant delays",
    4: "Detour", 5: "Additional service", 6: "Modified service",
    7: "Service change", 8: "Service change", 9: "Stop moved",
}


def _alert_text(translated) -> str:
    return translated.translation[0].text if translated.translation else ""


async def poll_alerts(rid: str) -> None:
    """Service disruptions. Only alerts active *now* are kept — the feed also
    carries future planned works, which would swamp the board with warnings
    about next month."""
    cfg, st = REGIONS[rid], STATE[rid]
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                now = time.time()
                alerts: list = []
                by_route: dict = {}
                by_stop: dict = {}
                n_total = n_inactive = 0
                for prefix, feed in await _fetch_feeds(client, cfg, "alerts"):
                    for entity in feed.entity:
                        if not entity.HasField("alert"):
                            continue
                        a = entity.alert
                        n_total += 1
                        # No active_period at all means "always active".
                        if a.active_period and not any(
                            (p.start or 0) <= now and (not p.end or now <= p.end)
                            for p in a.active_period
                        ):
                            n_inactive += 1
                            continue
                        idx = len(alerts)
                        alerts.append(
                            {
                                "header": _alert_text(a.header_text),
                                "description": _alert_text(a.description_text),
                                "effect": ALERT_EFFECT.get(a.effect, "Service change"),
                            }
                        )
                        for ie in a.informed_entity:
                            if ie.route_id:
                                by_route.setdefault(prefix + ie.route_id, []).append(idx)
                            if ie.stop_id:
                                by_stop.setdefault(prefix + ie.stop_id, []).append(idx)
                st["al"] = {"alerts": alerts, "by_route": by_route, "by_stop": by_stop}
                st["al_fetch"] = time.time()
                st["al_stats"] = {"alerts": n_total, "active": len(alerts),
                                  "not_yet_active": n_inactive}
                print(f"[al:{rid}] {n_total} alerts: {len(alerts)} active"
                      + (f", {n_inactive} outside their active period" if n_inactive else ""))
            except Exception as exc:  # alerts are an enhancement, never fatal
                print(f"[poll:{rid}] alerts fetch failed: {exc}")
            await asyncio.sleep(ALERT_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One poller per region per configured feed kind. A region with no realtime
    # (static-only Melbourne, say) simply gets no tasks — the board and the
    # timetable-estimated ghosts work regardless.
    tasks = []
    for rid, cfg in REGIONS.items():
        if not cfg["db"].exists():
            continue
        if cfg["trip_updates"]:
            tasks.append(asyncio.create_task(poll_trip_updates(rid)))
        if cfg["vehicle_positions"]:
            tasks.append(asyncio.create_task(poll_vehicle_positions(rid)))
        if cfg["alerts"]:
            tasks.append(asyncio.create_task(poll_alerts(rid)))
        # Warm the all-stops cache off the request path: the dominant-mode
        # GROUP BY takes ~15 s on Melbourne's 11.6 M stop_times, which is a bad
        # thing to hang the first zoomed-in map view on.
        tasks.append(asyncio.create_task(
            asyncio.to_thread(lambda r=rid: all_stops(r))))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Translink Next Service", lifespan=lifespan)


@app.middleware("http")
async def revalidate_unhashed_assets(request, call_next):
    """The page, its stylesheet and the fonts have no content hash in their
    names, so StaticFiles' bare ETag/Last-Modified lets browsers cache them
    heuristically and miss an update — most sharply a font subset change, which
    silently drops a newly-added glyph back to the colour-emoji font. `no-cache`
    forces a conditional request each load (a cheap 304 while unchanged), so the
    whole chain — index.html -> fonts.css -> the woff2 — is picked up on the next
    reload. The basemap and vendored JS are left cacheable: large and stable."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".css", ".woff2", ".json")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Static GTFS helpers
# ---------------------------------------------------------------------------


def region_cfg(region: str) -> dict:
    cfg = REGIONS.get(region)
    if cfg is None:
        raise HTTPException(404, f"Unknown region {region!r}")
    return cfg


def db(region: str = "seq") -> sqlite3.Connection:
    cfg = region_cfg(region)
    if not cfg["db"].exists():
        raise HTTPException(
            500, f"{cfg['db'].name} not found - run ingest_gtfs.py --region {region}")
    con = sqlite3.connect(cfg["db"])
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


def gtfs_time_to_epoch(hms: str, service_date: datetime, tz=AGENCY_TZ) -> int:
    """GTFS times can exceed 24:00:00 for after-midnight trips.

    The offset is applied to midnight *in the agency's timezone*: a naive
    datetime would be interpreted in the host's zone, so a UTC container would
    read every scheduled time 10 hours late. Each region passes its own zone
    (Melbourne has DST; Brisbane does not).
    """
    h, m, s = (int(x) for x in hms.split(":"))
    midnight = service_date.astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight + timedelta(hours=h, minutes=m, seconds=s)).timestamp())


def scheduled_departures(con, stop_ids: list[str], service_date: datetime,
                         tz=AGENCY_TZ) -> list[dict]:
    sids = active_service_ids(con, service_date)
    if not sids or not stop_ids:
        return []
    stop_marks = ",".join("?" for _ in stop_ids)
    svc_marks = ",".join("?" for _ in sids)
    rows = con.execute(
        f"""
        SELECT st.trip_id, st.departure_time, st.stop_id, st.stop_sequence,
               t.trip_headsign,
               r.route_id, r.route_short_name, r.route_long_name, r.route_type,
               r.route_color,
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
                "stop_sequence": r["stop_sequence"],
                "scheduled": gtfs_time_to_epoch(r["departure_time"], service_date, tz),
                "headsign": r["trip_headsign"],
                "route_id": r["route_id"],
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


def _interpolate_along(
    nodes: list[tuple[int, float, float]], now: int, stage_window: int = 0
):
    """nodes = [(epoch, lat, lon), ...] in schedule order. Return the point the
    timetable places the vehicle at `now`, linearly interpolated between the two
    stops that bracket it — or None when the trip is not on the road.

    Before the origin departure the trip has no en-route position. It is drawn at
    its origin only if it leaves within `stage_window` seconds ("staging to
    start"); earlier than that it gets no marker, so a run scheduled to start in
    40 minutes does not pile a phantom bus on the route start. After the final
    stop the trip has finished and returns None."""
    first = nodes[0][0]
    if now < first:
        return {"lat": nodes[0][1], "lon": nodes[0][2]} if first - now <= stage_window else None
    if now >= nodes[-1][0]:
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
        pos = _interpolate_along(nodes, now_epoch, STAGING_WINDOW_S)
        if pos:
            out[tid] = pos
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/r/{region}/stops/search")
@app.get("/api/stops/search")
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


@app.get("/api/r/{region}/stops/nearby")
@app.get("/api/stops/nearby")
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
                "route_type": modes.get(r["stop_id"], 3),
                "dist_m": round(haversine_m(r["stop_lat"], r["stop_lon"])),
            }
            for r in rows
            if r["stop_lat"] is not None
        ),
        key=lambda s: s["dist_m"],
    )
    return out[: max(1, min(limit, 25))]


# Nominatim is a free community service with a firm usage policy: identify the
# app, at most 1 req/s, cache results. The proxy exists so the frontend stays
# fully self-hosted and the policy is enforced in one place.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "TranslinkNextArrivalApp/1.0 (https://github.com/jasonuithol/TranslinkRealtimeApp)"
_geocode_cache: dict = {}          # (region, q) -> (fetched_at, results)
_geocode_last_call = 0.0
# The frontend searches every region at once, so two geocode requests arrive
# together; the lock serialises them so the 1 req/s promise to Nominatim holds.
_geocode_lock = asyncio.Lock()
GEOCODE_CACHE_S = 24 * 3600


def _geocode_label(r: dict, state: str) -> str:
    """A short label from Nominatim's structured address: 'lead, suburb STATE'.
    display_name is the full hierarchy ('12 X St, Suburb, Brisbane City,
    Queensland, 4006, Australia') — far more than a result row needs."""
    a = r.get("address") or {}
    # A named place (a stadium, a school) reads better as its name than as its
    # street address; plain house hits have no name and use number + road.
    lead = r.get("name") or ""
    if not lead and a.get("house_number") and a.get("road"):
        lead = f"{a['house_number']} {a['road']}"
    if not lead:
        lead = a.get("road") or r.get("display_name", "").split(",")[0].strip()
    suburb = next((a[k] for k in ("suburb", "neighbourhood", "village",
                                  "town", "locality", "city_district")
                   if a.get(k)), None)
    bits = [lead]
    if suburb and suburb.casefold() != lead.casefold():
        bits.append(suburb)
    return f"{', '.join(bits)} {state}".strip()


@app.get("/api/r/{region}/geocode")
@app.get("/api/geocode")
async def geocode(q: str, region: str = "seq"):
    """Address -> candidate points, proxied through Nominatim (OpenStreetMap).
    Pair with /api/stops/nearby to answer 'which stops are closest to home?'.
    Bounded to the region's bbox, so "Main St" resolves to the Main St here."""
    global _geocode_last_call
    cfg = region_cfg(region)
    q = q.strip()
    if len(q) < 4:
        return []
    hit = _geocode_cache.get((region, q.lower()))
    if hit and time.time() - hit[0] < GEOCODE_CACHE_S:
        return hit[1]
    async with _geocode_lock:
        # Enforce the 1 req/s policy even if the client misbehaves.
        wait = 1.0 - (time.time() - _geocode_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _geocode_last_call = time.time()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={
                        "q": q, "format": "jsonv2", "limit": 5,
                        "countrycodes": "au", "addressdetails": 1,
                        "viewbox": cfg["geocode_viewbox"], "bounded": 1,
                    },
                    headers={"User-Agent": NOMINATIM_UA},
                )
                resp.raise_for_status()
                results = [
                    {
                        "label": _geocode_label(r, cfg["state"]),
                        "lat": float(r["lat"]),
                        "lon": float(r["lon"]),
                    }
                    for r in resp.json()
                ]
        except Exception as exc:
            print(f"[geocode] lookup failed: {exc}")
            raise HTTPException(502, "address lookup unavailable")
    _geocode_cache[(region, q.lower())] = (time.time(), results)
    # An unbounded cache only grows by distinct queries typed; trim anyway.
    if len(_geocode_cache) > 500:
        _geocode_cache.pop(next(iter(_geocode_cache)))
    return results


@app.get("/api/r/{region}/departures/{stop_id}")
@app.get("/api/departures/{stop_id}")
def departures(stop_id: str, region: str = "seq"):
    cfg, st = region_cfg(region), STATE[region]
    con = db(region)
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

    # Board == map: a departure is listed only if we can put it on the map. Work
    # out a position for every candidate first — live GPS, else a timetable
    # estimate (which yields a point only when the trip is en route or staging
    # within STAGING_WINDOW_S) — then keep just the ones we can place, and cut to
    # MAX_RESULTS from those. A not-yet-departed run with no position is shown in
    # neither the board nor the map, so the two never disagree and a wobbling
    # time boundary moves a service in and out of both together.
    gps = {d["trip_id"]: st["vp"].get(d["trip_id"]) for d in results}
    gps = {tid: v for tid, v in gps.items() if v}
    need_estimate = [d for d in results if d["trip_id"] not in gps]
    con_e = db(region)
    estimated = estimate_ghost_positions(con_e, need_estimate, now_epoch)
    con_e.close()

    trackable = [d for d in results if d["trip_id"] in gps or d["trip_id"] in estimated]
    shown = trackable[:MAX_RESULTS]

    vehicles = [
        {
            "trip_id": d["trip_id"],
            "route": d["route"],
            "route_color": d["route_color"],
            "headsign": d["headsign"],
            "minutes": d["minutes"],
            **gps[d["trip_id"]],
        }
        for d in shown
        if d["trip_id"] in gps
    ]
    # The rest are en route or staging: a ghost, dead-reckoned from the timetable
    # and drawn distinct from a live fix.
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
        for d in shown
        if d["trip_id"] not in gps
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
        own = set(stop_ids)
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
    al = st["al"]
    for dep in shown:
        ids = list(al["by_route"].get(dep["route_id"], []))
        for sid in (dep["stop_id"], stop_id):
            ids.extend(al["by_stop"].get(sid, []))
        ids = sorted(set(ids))
        dep["alert_ids"] = ids
        for i in ids:
            used_alerts[str(i)] = al["alerts"][i]

    # Tag every departure with the shape its trip follows, so any row on the
    # board can have its route drawn on demand — not just the tracked ones. The
    # geometry itself is fetched separately and cached by the client: it never
    # changes, and resending thousands of points on each 15s poll would dwarf
    # the part of the payload that does.
    tracked_trips = [v["trip_id"] for v in vehicles]
    shown_trips = [d["trip_id"] for d in shown]
    if shown_trips:
        con3 = db(region)
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
            round(time.time() - st["rt_fetch"]) if st["rt_fetch"] else None
        ),
        "vehicle_feed_age": (
            round(time.time() - st["vp_fetch"]) if st["vp_fetch"] else None
        ),
        "departures": shown,
        "vehicles": vehicles,
        "ghosts": ghosts,
        "alerts": used_alerts,
        "paired": paired,
    }


@app.get("/api/r/{region}/trip-stops/{trip_id}")
@app.get("/api/trip-stops/{trip_id}")
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


@app.get("/api/r/{region}/shape/{shape_id}")
@app.get("/api/shape/{shape_id}")
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


@app.get("/api/r/{region}/all-stops")
@app.get("/api/all-stops")
def all_stops_endpoint(region: str = "seq"):
    """Every stop in the network, fetched lazily by the map the first time the
    user zooms in far enough to want them."""
    return JSONResponse(
        {"stops": all_stops(region)},
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/r/{region}/rail-stations")
@app.get("/api/rail-stations")
def rail_stations_endpoint(region: str = "seq"):
    """Train stations, drawn on the map as navigation landmarks regardless of
    which stop or route is selected — if the map can show one, it should."""
    return JSONResponse(
        {"stations": rail_stations(region)},
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/regions")
def regions():
    """The regions this deployment can serve (timetable ingested), for the
    frontend's region switcher. The first entry is the default."""
    # center: [lon, lat] — the frontend's pan-across-regions check measures
    # the map centre against each region's home city to swap providers.
    return [
        {"id": rid, "name": REGIONS[rid]["name"], "state": REGIONS[rid]["state"],
         "center": REGIONS[rid]["center"]}
        for rid in available_regions()
    ] or [{"id": "seq", "name": REGIONS["seq"]["name"],
           "state": REGIONS["seq"]["state"], "center": REGIONS["seq"]["center"]}]


@app.get("/api/r/{region}/config")
@app.get("/api/config")
def config(region: str = "seq"):
    """Per-region frontend config: whether a basemap is present (without one
    the map hides and the board works unchanged), where it is, and where the
    map should sit before a stop is chosen."""
    cfg = region_cfg(region)
    return {
        "basemap": cfg["basemap"].exists(),
        "basemap_url": f"/basemap/{cfg['basemap'].name}",
        "name": cfg["name"],
        "center": cfg["center"],
        # So the frontend prints times in the network's local clock, not the
        # viewer's — a Brisbane browser looking at Melbourne must show AEDT.
        "tz": str(cfg["tz"]),
    }


@app.get("/api/feeds")
def feeds():
    """Realtime feed health for QC, per region: how many trip updates and
    vehicle positions the last poll saw, how many were dropped and why, and how
    stale each cache is. `without_position` is the count of live vehicles with
    no coordinates — the ones that cannot be mapped."""
    now = time.time()
    return {
        rid: {
            "trip_updates": {
                **st["rt_stats"],
                "age_s": round(now - st["rt_fetch"]) if st["rt_fetch"] else None,
            },
            "vehicle_positions": {
                **st["vp_stats"],
                "age_s": round(now - st["vp_fetch"]) if st["vp_fetch"] else None,
            },
            "alerts": {
                **st["al_stats"],
                "age_s": round(now - st["al_fetch"]) if st["al_fetch"] else None,
            },
        }
        for rid, st in STATE.items()
        if rid in available_regions()
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
