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
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.transit import gtfs_realtime_pb2

import planner as journey

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
    "qld": {
        "name": "Translink · Regional Queensland",
        "state": "QLD",
        "tz": ZoneInfo("Australia/Brisbane"),
        "db": Path(os.environ.get("QLD_GTFS_DB") or DB_PATH.parent / "gtfs-qld.sqlite3"),
        "basemap": BASEMAP_DIR / "qld.pmtiles",
        # Eighteen town networks merged into one DB, each ingested under a
        # "<town>:" prefix (see ingest_gtfs.py). Translink publishes keyless
        # GTFS-RT for five of them — Cairns, Bowen, Innisfail, Maryborough-
        # Hervey Bay and North Stradbroke Island (verified live 2026-07-27);
        # the other towns fall back to timetable ghosts, per-trip, for free.
        "trip_updates": [
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/CNS/TripUpdates",
             "prefix": "cns:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/BOW/TripUpdates",
             "prefix": "bow:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/INN/TripUpdates",
             "prefix": "inn:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/MHB/TripUpdates",
             "prefix": "mhb:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/NSI/TripUpdates",
             "prefix": "nsi:"},
        ],
        "vehicle_positions": [
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/CNS/VehiclePositions",
             "prefix": "cns:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/BOW/VehiclePositions",
             "prefix": "bow:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/INN/VehiclePositions",
             "prefix": "inn:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/MHB/VehiclePositions",
             "prefix": "mhb:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/NSI/VehiclePositions",
             "prefix": "nsi:"},
        ],
        "alerts": [
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/CNS/Alerts",
             "prefix": "cns:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/BOW/Alerts",
             "prefix": "bow:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/INN/Alerts",
             "prefix": "inn:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/MHB/Alerts",
             "prefix": "mhb:"},
            {"url": "https://gtfsrt.api.translink.com.au/api/realtime/NSI/Alerts",
             "prefix": "nsi:"},
        ],
        "headers": {},
        # Five feeds per kind against the host seq also polls: pace them a
        # little rather than firing the burst back-to-back every cycle.
        "req_gap_s": 0.25,
        # The towns dot the whole eastern seaboard, Cairns down to Warwick.
        "geocode_viewbox": "144.5,-28.5,153.6,-16.4",
        "center": [145.7710, -16.9203],
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
        # TfNSW throttles per-second bursts (429s): pace the seven feeds of
        # each kind instead of firing them back-to-back.
        "req_gap_s": 0.5,
        "geocode_viewbox": "150.5,-34.25,151.4,-33.35",
        "center": [151.2093, -33.8688],
    },
    "nsw": {
        "name": "NSW TrainLink · Regional NSW & Interstate",
        "state": "NSW",
        "tz": ZoneInfo("Australia/Sydney"),
        "db": Path(os.environ.get("NSW_GTFS_DB") or DB_PATH.parent / "gtfs-nsw.sqlite3"),
        "basemap": BASEMAP_DIR / "nsw.pmtiles",
        # The interstate region: TrainLink XPTs to Melbourne & Brisbane,
        # Xplorers to Canberra/Armidale/Broken Hill, plus the connecting
        # coaches. Same TfNSW key as syd (the ingest reads SYD_API_KEY);
        # realtime is env-driven like syd's — set NSW_TRIP_UPDATES etc.
        # (see deploy/enable-syd-vps.sh, which writes both). Unset = the
        # region still works schedule-only.
        "trip_updates": _env_rt_feeds("NSW", "TRIP_UPDATES"),
        "vehicle_positions": _env_rt_feeds("NSW", "VEHICLE_POSITIONS"),
        "alerts": _env_rt_feeds("NSW", "ALERTS"),
        "headers": _syd_headers(),
        # Stops span Adelaide (the Broken Hill coach terminus) to Brisbane to
        # Melbourne — the viewbox is most of south-east Australia, deliberately.
        "geocode_viewbox": "138.4,-38.4,153.7,-27.2",
        # Dubbo, NOT Sydney: the map's browse hand-over picks the nearest
        # region center, and a Sydney center here would steal southern-CBD
        # browsing from syd (Central is 2.5 km from syd's own center). An
        # inland center means rural panning lands on TrainLink and city
        # panning stays with the city networks.
        "center": [148.6012, -32.2465],
    },
    "ade": {
        "name": "Adelaide Metro · Adelaide",
        "state": "SA",
        "tz": ZoneInfo("Australia/Adelaide"),
        "db": Path(os.environ.get("ADE_GTFS_DB") or DB_PATH.parent / "gtfs-ade.sqlite3"),
        "basemap": BASEMAP_DIR / "ade.pmtiles",
        # Adelaide Metro's GTFS-RT is public and keyless, SEQ-style: one flat
        # feed of each kind, ids matching the unprefixed static feed.
        # Verified live 2026-07-26 (gtfs.adelaidemetro.com.au, "BETA").
        "trip_updates": [
            {"url": "https://gtfs.adelaidemetro.com.au/v1/realtime/trip_updates",
             "prefix": ""}],
        "vehicle_positions": [
            {"url": "https://gtfs.adelaidemetro.com.au/v1/realtime/vehicle_positions",
             "prefix": ""}],
        "alerts": [
            {"url": "https://gtfs.adelaidemetro.com.au/v1/realtime/service_alerts",
             "prefix": ""}],
        "headers": {},
        "geocode_viewbox": "138.4,-35.4,139.1,-34.4",
        "center": [138.6007, -34.9212],
    },
    "per": {
        "name": "Transperth · Perth",
        "state": "WA",
        "tz": ZoneInfo("Australia/Perth"),
        "db": Path(os.environ.get("PER_GTFS_DB") or DB_PATH.parent / "gtfs-per.sqlite3"),
        "basemap": BASEMAP_DIR / "per.pmtiles",
        # No public GTFS-RT exists for WA (checked 2026-07-26) — schedule-only:
        # timetable times, ghost markers. Empty lists keep the pollers off and
        # flip the board's status to "timetable only".
        "trip_updates": [],
        "vehicle_positions": [],
        "alerts": [],
        "headers": {},
        "geocode_viewbox": "115.6,-32.7,116.1,-31.4",
        "center": [115.8605, -31.9505],
    },
    "dar": {
        "name": "NT Transport · Darwin",
        "state": "NT",
        "tz": ZoneInfo("Australia/Darwin"),
        "db": Path(os.environ.get("DAR_GTFS_DB") or DB_PATH.parent / "gtfs-dar.sqlite3"),
        "basemap": BASEMAP_DIR / "dar.pmtiles",
        # The NT Bus Tracker app is realtime, but its backend is not published
        # as GTFS-RT (checked 2026-07-26) — schedule-only, like keyless mel.
        "trip_updates": [],
        "vehicle_positions": [],
        "alerts": [],
        "headers": {},
        "geocode_viewbox": "130.7,-12.9,131.2,-12.2",
        "center": [130.8411, -12.4634],
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


async def _fetch_feeds(client, cfg: dict, kind: str) -> tuple[list, list]:
    """Fetch every configured GTFS-RT feed of one kind for a region. Returns
    ((prefix, FeedMessage) pairs, error strings); the prefix maps feed-local
    ids onto the prefixed ids the region's ingest wrote (empty for
    single-feed regions). One failing feed must not sink the others — Sydney
    polls seven per kind, and an all-or-nothing cycle turned one flaky
    light-rail endpoint into a fully scheduled region."""
    out, errors = [], []
    gap = cfg.get("req_gap_s") or 0
    for i, feed_cfg in enumerate(cfg[kind]):
        try:
            if gap and i:
                await asyncio.sleep(gap)
            resp = await client.get(feed_cfg["url"], headers=cfg["headers"])
            resp.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
            out.append((feed_cfg["prefix"], feed))
        except Exception as exc:
            errors.append(f"{feed_cfg['prefix'] or '(no prefix)'} "
                          f"{feed_cfg['url']}: {exc}")
    return out, errors


async def poll_trip_updates(rid: str) -> None:
    cfg, st = REGIONS[rid], STATE[rid]
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                cache: dict = {}
                n_updates = n_no_trip = 0
                feeds, errors = await _fetch_feeds(client, cfg, "trip_updates")
                for prefix, feed in feeds:
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
                # All feeds down: keep the previous cache (stale beats empty,
                # and rt_fetch's growing age shows the staleness); partial
                # results commit — trips on a dead feed drop to scheduled.
                if feeds:
                    st["rt"] = cache
                    st["rt_fetch"] = time.time()
                st["rt_stats"] = {"trip_updates": n_updates,
                                  "without_trip_id": n_no_trip,
                                  **({"errors": errors} if errors else {})}
                print(f"[tu:{rid}] {n_updates} trip updates ({len(cache)} trips cached)"
                      + (f", {n_no_trip} without trip_id" if n_no_trip else ""))
                if errors:
                    print(f"[poll:{rid}] trip-update feed(s) failed: "
                          + " | ".join(errors))
            except Exception as exc:  # keep serving scheduled times on failure
                print(f"[poll:{rid}] realtime fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


async def poll_vehicle_positions(rid: str) -> None:
    """Live GPS for the map view. Keyed by trip_id so a departure on the board
    can be matched to the vehicle actually running it."""
    cfg, st = REGIONS[rid], STATE[rid]
    # Offset from the trip-updates poller so the two bursts never align —
    # at boot all pollers fired at once, and TfNSW 429'd the tail of the
    # ~18 near-simultaneous requests.
    await asyncio.sleep(POLL_SECONDS / 3)
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                cache: dict = {}
                n_total = n_pos = n_no_pos = n_no_trip = 0
                feeds, errors = await _fetch_feeds(client, cfg, "vehicle_positions")
                for prefix, feed in feeds:
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
                if feeds:
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
                    **({"errors": errors} if errors else {}),
                }
                if errors:
                    print(f"[poll:{rid}] vehicle-position feed(s) failed: "
                          + " | ".join(errors))
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
    # Third slot in the boot stagger (see poll_vehicle_positions).
    await asyncio.sleep(2 * POLL_SECONDS / 3)
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                now = time.time()
                alerts: list = []
                by_route: dict = {}
                by_stop: dict = {}
                n_total = n_inactive = 0
                feeds, errors = await _fetch_feeds(client, cfg, "alerts")
                for prefix, feed in feeds:
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
                if feeds:
                    st["al"] = {"alerts": alerts, "by_route": by_route, "by_stop": by_stop}
                    st["al_fetch"] = time.time()
                st["al_stats"] = {"alerts": n_total, "active": len(alerts),
                                  "not_yet_active": n_inactive,
                                  **({"errors": errors} if errors else {})}
                print(f"[al:{rid}] {n_total} alerts: {len(alerts)} active"
                      + (f", {n_inactive} outside their active period" if n_inactive else ""))
                if errors:
                    print(f"[poll:{rid}] alert feed(s) failed: "
                          + " | ".join(errors))
            except Exception as exc:  # alerts are an enhancement, never fatal
                print(f"[poll:{rid}] alerts fetch failed: {exc}")
            await asyncio.sleep(ALERT_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One poller per region per configured feed kind. A region with no realtime
    # (static-only Melbourne, say) simply gets no tasks — the board and the
    # timetable-estimated ghosts work regardless.
    tasks = []
    warm_rids = []
    for rid, cfg in REGIONS.items():
        if not cfg["db"].exists():
            continue
        if cfg["trip_updates"]:
            tasks.append(asyncio.create_task(poll_trip_updates(rid)))
        if cfg["vehicle_positions"]:
            tasks.append(asyncio.create_task(poll_vehicle_positions(rid)))
        if cfg["alerts"]:
            tasks.append(asyncio.create_task(poll_alerts(rid)))
        warm_rids.append(rid)
    # Warm the all-stops caches off the request path: the dominant-mode
    # GROUP BY takes ~15 s on Melbourne's 11.6 M stop_times, which is a bad
    # thing to hang the first zoomed-in map view on. ONE thread, regions in
    # sequence — three concurrent warms starved the event loop on the small
    # VPS for minutes and the deploy health check read it as "never came up".
    def _warm_all(rids=tuple(warm_rids)):
        for r in rids:
            all_stops(r)
    tasks.append(asyncio.create_task(asyncio.to_thread(_warm_all)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Translink Next Service", lifespan=lifespan)

# The packaged phone app (Transport Honker) runs from its own local origin
# and calls this API cross-origin. Everything here is a public read-only
# transit endpoint, so a permissive policy costs nothing; there are no
# cookies or credentials to protect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_unhashed_assets(request, call_next):
    """The page, its stylesheet and the fonts have no content hash in their
    names, so StaticFiles' bare ETag/Last-Modified lets browsers cache them
    heuristically and miss an update — most sharply a font subset change, which
    silently drops a newly-added glyph back to the colour-emoji font. `no-cache`
    forces a conditional request each load (a cheap 304 while unchanged), so the
    whole chain — index.html -> app.css/js -> fonts.css -> the woff2 — is picked
    up on the next reload. The app's own scripts under /static/js are part of
    that chain (they were index.html until they were split out). The basemap
    and vendored JS are left cacheable: large and stable."""
    response = await call_next(request)
    path = request.url.path
    if (path == "/" or path.endswith((".css", ".woff2", ".json"))
            or path.startswith("/static/js/")):
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
        # TfNSW timetables non-revenue runs as ordinary trips on routes / with
        # headsigns literally named "Out Of Service" — a passenger board must
        # not offer them.
        if _out_of_service(r["trip_headsign"]) or \
           _out_of_service(r["route_short_name"]) or \
           _out_of_service(r["route_long_name"]):
            continue
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


def _out_of_service(text: str | None) -> bool:
    return bool(text) and "out of service" in text.lower()


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
                # For drawing the surrounds of a dropped pin on the map.
                "lat": r["stop_lat"],
                "lon": r["stop_lon"],
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


_STATE_ABBR = {
    "Queensland": "QLD", "New South Wales": "NSW", "Victoria": "VIC",
    "South Australia": "SA", "Western Australia": "WA",
    "Northern Territory": "NT", "Tasmania": "TAS",
    "Australian Capital Territory": "ACT",
}

# ---------------------------------------------------------------------------
# Local G-NAF geocoder: every street-numbered address in Australia with
# house-level coordinates (built by ingest_gnaf.py; ~700 MB, quarterly).
# OSM's residential house-number coverage is patchy — Nominatim pinned
# "397 Christine Avenue" to an arbitrary point along a 3 km road. When the
# DB is present and the query leads with a house number, this answers
# instead; Nominatim stays the fallback for landmarks and unnumbered
# queries, and for deployments that never ingested G-NAF.
GNAF_DB = Path(os.environ.get("GNAF_DB") or DB_PATH.parent / "gnaf.sqlite3")

_STREET_ABBR = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "dr": "drive", "drv": "drive", "ct": "court", "crt": "court",
    "cres": "crescent", "cr": "crescent", "pde": "parade", "hwy": "highway",
    "tce": "terrace", "pl": "place", "blvd": "boulevard", "bvd": "boulevard",
    "ln": "lane", "cct": "circuit", "esp": "esplanade", "gr": "grove",
    "gdns": "gardens", "pkwy": "parkway",
}


# Local business/POI index (ingest_places.py — named OSM places near
# stops). Answers "bunnings burleigh" instantly; Nominatim remains the
# fallback for anything the OSM community hasn't mapped.
PLACES_DB = Path(os.environ.get("PLACES_DB") or DB_PATH.parent / "places.sqlite3")


def _near_dist(near):
    """Squared flat-earth distance to `near` ((lat, lon) or None) as a sort
    key builder; without a near point every candidate ties at zero."""
    if not near:
        return lambda r: 0.0
    nlat, nlon = near
    coslat = math.cos(math.radians(nlat)) or 1e-9
    def d(r):
        return ((r["lat"] - nlat) ** 2
                + ((r["lon"] - nlon) * coslat) ** 2)
    return d


def places_geocode(q: str, viewbox: str, limit: int = 8,
                   near: tuple | None = None) -> list | None:
    """Named-place lookup. None = no DB or nothing matched — fall through.
    Results inside the current region's viewbox rank first (like the
    Nominatim viewbox bias), nearest to `near` first within that; category
    and suburb ride along in the label."""
    if not PLACES_DB.exists():
        return None
    tokens = [t for t in re.findall(r"[A-Za-z0-9']+", q.lower()) if t]
    if len(tokens) < 1 or len(q.strip()) < 3:
        return None
    # Every token must match somewhere (name/category/suburb); the last one
    # as a prefix, so mid-word queries behave like search-as-you-type.
    fts = " ".join(f'"{t}"' for t in tokens[:-1])
    fts = (fts + " " if fts else "") + f'"{tokens[-1]}"*'
    lon1, lat1, lon2, lat2 = (float(x) for x in viewbox.split(","))
    con = sqlite3.connect(PLACES_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT p.name, p.category, p.suburb, p.state, p.lat, p.lon "
            "FROM places_fts f JOIN places p ON p.id = f.rowid "
            # A wide pool: a chain has hundreds of branches and the near
            # sort can only rank candidates the query returned.
            "WHERE places_fts MATCH ? ORDER BY bm25(places_fts) LIMIT 250",
            (fts,)).fetchall()
    except sqlite3.OperationalError:
        return None   # pathological FTS query string
    finally:
        con.close()
    if not rows:
        return None
    def in_box(r):
        return (min(lat1, lat2) <= r["lat"] <= max(lat1, lat2)
                and min(lon1, lon2) <= r["lon"] <= max(lon1, lon2))
    # An exact name match beats places that merely CONTAIN the query
    # ("Pacific Fair" the mall above "Roll'd Pacific Fair" the tenant).
    qnorm = "".join(tokens)
    def exact(r):
        return re.sub(r"[^a-z0-9]", "", r["name"].lower()) == qnorm
    dist = _near_dist(near)
    rows.sort(key=lambda r: (not in_box(r), not exact(r), dist(r)))
    out, seen = [], set()
    for r in rows:
        bits = [r["category"]]
        if r["suburb"]:
            bits.append(f"{r['suburb']} {r['state']}".strip())
        label = f"{r['name']} — {', '.join(bits)}"
        if label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "lat": r["lat"], "lon": r["lon"]})
        if len(out) >= limit:
            break
    return out or None


def gnaf_geocode(q: str, state_bias: str, limit: int = 8,
                 near: tuple | None = None) -> list | None:
    """House-number lookup. None means 'not applicable' (no DB, or the query
    doesn't lead with a house number) — the caller falls back to Nominatim.
    An address short of its exact number returns the NEAREST number on that
    street, honestly labelled — next door beats a random point mid-road."""
    if not GNAF_DB.exists():
        return None
    m = re.match(r"^\s*(?:\d+\s*/\s*)?(\d+)\s+(.+)", q)
    if not m:
        return None
    num = int(m.group(1))
    tokens = [_STREET_ABBR.get(t, t)
              for t in (t.lower() for t in re.findall(r"[A-Za-z']+", m.group(2)))]
    if not tokens:
        return None
    con = sqlite3.connect(GNAF_DB)
    con.row_factory = sqlite3.Row
    try:
        # Every token must appear somewhere across name/type/locality/state;
        # the current region's state ranks first, like the Nominatim viewbox
        # bias. bm25 keeps "Christine Avenue" above "Christine Court".
        fts = " ".join(f'"{t}"' for t in tokens)
        streets = con.execute(
            "SELECT s.id, s.name, s.type, s.locality, s.state "
            "FROM streets_fts f JOIN streets s ON s.id = f.rowid "
            "WHERE streets_fts MATCH ? "
            "ORDER BY (s.state = ?) DESC, bm25(streets_fts) LIMIT 150",
            (fts, state_bias),
        ).fetchall()
        out = []
        for s in streets:
            a = con.execute(
                "SELECT num, lat, lon FROM addresses WHERE street_id = ? "
                "ORDER BY ABS(num - ?) LIMIT 1", (s["id"], num)).fetchone()
            if a is None:
                continue
            title = f"{a['num']} {s['name'].title()}"
            if s["type"]:
                title += f" {s['type'].title()}"
            out.append({
                "label": f"{title}, {s['locality'].title()} {s['state']}",
                "lat": a["lat"], "lon": a["lon"],
                "_exact": a["num"] == num,
            })
        # Rank the WHOLE street pool, then trim — cutting at `limit` before
        # sorting let the bm25 street order decide which candidates existed
        # at all, and the one exact "397 Christine Avenue" fell off the list
        # while its Robina/Kuraby namesakes made it in.
        #
        # With a bias point, exactness is worth kilometres, not everything:
        # someone near Toowong typing "12 High Street" means the High Street
        # THERE (nearest number 10), not an exact 12 in Charleville — but an
        # exact 397 in the next suburb must beat a 480 marginally closer.
        # Unbiased, exact numbers simply come first.
        if near:
            dist = _near_dist(near)
            def rank(r):
                km = math.sqrt(dist(r)) * 111.0
                return km + (0.0 if r["_exact"] else 25.0)
            out.sort(key=rank)
        else:
            out.sort(key=lambda r: not r["_exact"])
        del out[limit:]
        for r in out:
            r.pop("_exact")
        return out or None
    except sqlite3.Error as exc:
        print(f"[gnaf] lookup failed: {exc}")
        return None
    finally:
        con.close()


def _geocode_label(r: dict, state: str) -> str:
    """A short label from Nominatim's structured address: 'lead, suburb STATE'.
    display_name is the full hierarchy ('12 X St, Suburb, Brisbane City,
    Queensland, 4006, Australia') — far more than a result row needs.

    The state comes from the ADDRESS, not the region: the nsw region's
    geocode viewbox spans four states (Broken Hill to Brisbane to
    Melbourne), and stamping the region's state labelled a Varsity Lakes
    QLD street as NSW. The region state is only the fallback."""
    a = r.get("address") or {}
    state = _STATE_ABBR.get(a.get("state") or "", state)
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


@app.get("/api/r/{region}/rgeocode")
@app.get("/api/rgeocode")
def reverse_geocode(lat: float, lon: float, region: str = "seq"):
    """A point -> the address a person would say. Dropped pins otherwise
    travel as "dropped pin", and a journey card reading "Walk to dropped
    pin" tells a rider nothing. Nearest G-NAF address wins; beyond ~150 m
    the answer is hedged ("near ..."), and with nothing within ~600 m
    there is honestly no address to give."""
    if not GNAF_DB.exists():
        return {"label": None}
    con = sqlite3.connect(GNAF_DB)
    try:
        for span_m, hedge in ((150.0, ""), (600.0, "near ")):
            dlat = span_m / 111000.0
            dlon = span_m / (111000.0 * max(0.2, math.cos(math.radians(lat))))
            rows = con.execute(
                "SELECT a.num, a.lat, a.lon, s.name, s.type, s.locality, s.state "
                "FROM addresses a JOIN streets s ON s.id = a.street_id "
                "WHERE a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?",
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
            best, bd = None, span_m ** 2
            for num, ala, alo, name, typ, loc, st in rows:
                d2 = (((ala - lat) * 111000.0) ** 2
                      + ((alo - lon) * 111000.0
                         * math.cos(math.radians(lat))) ** 2)
                if d2 < bd:
                    bd, best = d2, (num, name, typ, loc, st)
            if best:
                num, name, typ, loc, st = best
                street = name.title() + (f" {typ.title()}" if typ else "")
                # Beyond the close range the house number would be a lie;
                # the street and suburb are still true.
                head = f"{num} {street}" if not hedge else street
                return {"label": f"{hedge}{head}, {loc.title()} {st}",
                        "exact": not hedge}
    except sqlite3.Error as exc:
        print(f"[rgeocode] failed: {exc}")
    finally:
        con.close()
    return {"label": None}


@app.get("/api/r/{region}/geocode")
@app.get("/api/geocode")
async def geocode(q: str, region: str = "seq",
                  near_lat: float | None = None,
                  near_lon: float | None = None):
    """Address -> candidate points, proxied through Nominatim (OpenStreetMap).
    Pair with /api/stops/nearby to answer 'which stops are closest to home?'.
    Bounded to the region's bbox, so "Main St" resolves to the Main St here."""
    global _geocode_last_call
    cfg = region_cfg(region)
    q = q.strip()
    if len(q) < 4:
        return []
    # An optional bias point: the searcher's location (origin search) or the
    # journey's departure (destination search) — near candidates rank first.
    near = ((near_lat, near_lon)
            if near_lat is not None and near_lon is not None else None)
    # House-numbered queries answer from the local G-NAF when it's ingested —
    # exact house coordinates, no rate limit, no network.
    local = gnaf_geocode(q, cfg["state"], near=near)
    if local:
        return local
    # Then named places (businesses, POIs) from the local OSM index.
    placed = places_geocode(q, cfg["geocode_viewbox"], near=near)
    if placed:
        return placed
    hit = _geocode_cache.get((region, q.lower()))
    if hit and time.time() - hit[0] < GEOCODE_CACHE_S:
        return sorted(hit[1], key=_near_dist(near)) if near else hit[1]
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
                        "q": q, "format": "jsonv2", "limit": 10,
                        "countrycodes": "au", "addressdetails": 1,
                        # The viewbox only BIASES ranking now (no bounded=1):
                        # local streets sort first, but the query is
                        # nationwide — so the client asks ONE region instead
                        # of fanning out to nine, each serialised behind the
                        # 1 req/s Nominatim policy. Nine bounded lookups made
                        # every uncached address search take ~9 seconds.
                        "viewbox": cfg["geocode_viewbox"],
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
    # The cache stays bias-free (keyed on the query alone); the near sort is
    # applied per request on the way out.
    return sorted(results, key=_near_dist(near)) if near else results


# ---------------------------------------------------------------------------
# One physical station, several timetables. TfNSW publishes its networks as
# separate feeds — ingested here as `syd` (suburban t:/m:/b:/f:/l*: prefixes)
# and `nsw` (TrainLink, nt:) — but reuses the bare TSN across all of them:
# t:200060, m:200060, lc:200060 and nt:200060 are all Central Station. A board
# must show the station, not our ingest topology, so departures union every
# sibling stop sharing the TSN, across both regions.
TSN_REGIONS = ("syd", "nsw")

# Cross-state interchanges share no id in any feed — hand-curated. Each set
# lists (region, stop_id) pairs that are one physical place; use parents where
# they exist (a board expands a parent into its child platforms on its own).
STATION_LINKS = [
    # Southern Cross: V/Line + Metro parent ↔ TrainLink XPT & coach terminal
    {("mel", "2:vic:rail:SSS"), ("nsw", "nt:22180")},
    # Roma Street (Brisbane): QR rail parent ↔ TrainLink XPT & coach terminal
    {("seq", "place_romsta"), ("nsw", "nt:40001")},
    # Adelaide Central Bus Station: the Broken Hill coach's far end
    {("ade", "102954"), ("nsw", "nt:G50001")},
]


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


@app.get("/api/r/{region}/departures/{stop_id}")
@app.get("/api/departures/{stop_id}")
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
        "stop": dict(stop),
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


# ---------------------------------------------------------------------------
# Walking routes: A* over the local street graph (ingest_walkgraph.py — OSM
# ways near stops). Powers the planner's dashed walk legs; absent graph or
# uncovered area returns 404 and the client draws a straight line instead.
WALKGRAPH_DB = Path(os.environ.get("WALKGRAPH_DB")
                    or DB_PATH.parent / "walkgraph.sqlite3")
_WG_CELL = 0.01

def _wg_cell(lat: float, lon: float) -> int:
    return int((lat + 90) / _WG_CELL) * 40000 + int((lon + 180) / _WG_CELL)


def _wg_nearest(con, lat: float, lon: float):
    """Nearest graph node, searching outward ring by ring (3 rings ≈ 3 km)."""
    base = _wg_cell(lat, lon)
    for ring in range(0, 3):
        cells = [base + dy * 40000 + dx
                 for dy in range(-ring, ring + 1)
                 for dx in range(-ring, ring + 1)]
        marks = ",".join("?" for _ in cells)
        rows = con.execute(
            f"SELECT id, lat, lon FROM nodes WHERE cell IN ({marks})",
            cells).fetchall()
        if rows:
            coslat = math.cos(math.radians(lat)) ** 2
            return min(rows, key=lambda r: (r["lon"] - lon) ** 2 * coslat
                                           + (r["lat"] - lat) ** 2)
    return None


def _walk_streets(points, step=30.0):
    """Name the streets a walk path traverses: the nearest G-NAF address to
    each ~30 m sample of the path names the street underfoot (the walking
    graph itself stores no names). Consecutive samples merge into runs;
    sub-60 m blips (clipping a corner at an intersection) are dropped.
    Empty where addresses are out of reach (parks, bush) or without the
    G-NAF DB — the caller treats streets as decoration, never structure."""
    if not GNAF_DB.exists() or len(points) < 2:
        return []
    STEP = float(step)
    def sl(a, b):
        return math.hypot((b[1] - a[1]) * 111000.0,
                          (b[0] - a[0]) * 88000.0)
    samples, carry = [points[0]], 0.0
    for a, b in zip(points, points[1:]):
        seg = sl(a, b)
        if seg <= 0:
            continue
        t = STEP - carry
        while t <= seg:
            f = t / seg
            samples.append((a[0] + (b[0] - a[0]) * f,
                            a[1] + (b[1] - a[1]) * f))
            t += STEP
        carry = (carry + seg) % STEP
    con = sqlite3.connect(GNAF_DB)
    try:
        names = []
        for lon, lat in samples:
            rows = con.execute(
                "SELECT s.name, s.type, a.lat, a.lon FROM addresses a "
                "JOIN streets s ON s.id = a.street_id "
                "WHERE a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?",
                (lat - 0.0006, lat + 0.0006,
                 lon - 0.00075, lon + 0.00075)).fetchall()
            best, bd = None, 60.0 ** 2
            for nm, ty, ala, alo in rows:
                d2 = (((ala - lat) * 111000.0) ** 2
                      + ((alo - lon) * 88000.0) ** 2)
                if d2 < bd:
                    bd, best = d2, f"{nm.title()} {ty.title()}" if ty else nm.title()
            names.append(best)
    finally:
        con.close()
    # collapse into runs (keeping each run's sample coords — the map draws
    # the street's name along them), drop intersection blips, merge
    runs = []
    for n, pt in zip(names, samples):
        if runs and runs[-1][0] == n:
            runs[-1][1] += 1
            runs[-1][2].append(pt)
        else:
            runs.append([n, 1, [pt]])
    kept = [r for r in runs if r[0] is not None and r[1] >= 2]
    merged = []
    for n, c, pts in kept:
        if merged and merged[-1]["name"] == n:
            merged[-1]["m"] += int(c * STEP)
            merged[-1]["line"].extend(pts)
        else:
            merged.append({"name": n, "m": int(c * STEP),
                           "line": [list(p) for p in pts]})
    return merged


_streets_cache: dict = {}


@app.post("/api/streets")
async def streets_for_path(request: Request):
    """Street names along ANY line the client draws — ride traces and
    planner leg slices alike (walk legs get theirs from /api/walkroute).
    Adaptive sampling keeps a 30 km bus shape to ~800 G-NAF probes."""
    body = await request.json()
    raw = body.get("points") or []
    if not (isinstance(raw, list) and 2 <= len(raw) <= 4000):
        raise HTTPException(400, "points: 2..4000 [lon,lat] pairs")
    try:
        pts = [(float(p[0]), float(p[1])) for p in raw]
    except (TypeError, ValueError, IndexError):
        raise HTTPException(400, "points: 2..4000 [lon,lat] pairs")
    total = sum(math.hypot((b[1] - a[1]) * 111000.0, (b[0] - a[0]) * 88000.0)
                for a, b in zip(pts, pts[1:]))
    if total > 80000:
        return {"streets": []}   # an intercity shape; naming it helps no one
    key = (round(pts[0][0], 5), round(pts[0][1], 5),
           round(pts[-1][0], 5), round(pts[-1][1], 5), len(pts))
    hit = _streets_cache.get(key)
    if hit is None:
        hit = _walk_streets(pts, step=max(30.0, total / 800.0))
        if len(_streets_cache) > 256:
            _streets_cache.pop(next(iter(_streets_cache)))
        _streets_cache[key] = hit
    return {"streets": hit}


_walkroute_cache: dict = {}


@app.get("/api/walkroute")
def walkroute(from_lat: float, from_lon: float, to_lat: float, to_lon: float):
    if not WALKGRAPH_DB.exists():
        raise HTTPException(404, "No walking graph on this deployment")
    import heapq

    def hav(la1, lo1, la2, lo2):
        p1, p2 = math.radians(la1), math.radians(la2)
        a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
             * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
        return 2 * 6371000 * math.asin(math.sqrt(a))

    straight = hav(from_lat, from_lon, to_lat, to_lon)
    if straight > 2500:
        raise HTTPException(400, "Walk too long to route")
    key = (round(from_lat, 5), round(from_lon, 5),
           round(to_lat, 5), round(to_lon, 5))
    hit = _walkroute_cache.get(key)
    if hit is not None:
        if hit == "none":
            raise HTTPException(404, "No walking route found")
        return hit

    con = sqlite3.connect(WALKGRAPH_DB)
    con.row_factory = sqlite3.Row
    try:
        src = _wg_nearest(con, from_lat, from_lon)
        dst = _wg_nearest(con, to_lat, to_lon)
        if src is None or dst is None:
            _walkroute_cache[key] = "none"
            raise HTTPException(404, "Area not covered by the walking graph")
        # A*, adjacency read straight off the edges index per expansion.
        goal = (dst["lat"], dst["lon"])
        open_q = [(0, src["id"])]
        g = {src["id"]: 0}
        came: dict = {}
        pos = {src["id"]: (src["lat"], src["lon"]),
               dst["id"]: (dst["lat"], dst["lon"])}
        found = False
        expansions = 0
        while open_q and expansions < 30000:
            _, cur = heapq.heappop(open_q)
            if cur == dst["id"]:
                found = True
                break
            expansions += 1
            for e in con.execute("SELECT b, d FROM edges WHERE a=?", (cur,)):
                ng = g[cur] + e["d"]
                if ng >= g.get(e["b"], 1 << 30):
                    continue
                g[e["b"]] = ng
                came[e["b"]] = cur
                if e["b"] not in pos:
                    r = con.execute("SELECT lat, lon FROM nodes WHERE id=?",
                                    (e["b"],)).fetchone()
                    pos[e["b"]] = (r["lat"], r["lon"])
                heapq.heappush(open_q, (
                    ng + hav(*pos[e["b"]], *goal), e["b"]))
        if not found:
            _walkroute_cache[key] = "none"
            raise HTTPException(404, "No walking route found")
        path = [dst["id"]]
        while path[-1] in came:
            path.append(came[path[-1]])
        path.reverse()
        points = ([[from_lon, from_lat]]
                  + [[pos[n][1], pos[n][0]] for n in path]
                  + [[to_lon, to_lat]])
        out = {"points": points, "dist_m": g[dst["id"]],
               "streets": _walk_streets(points)}
    finally:
        con.close()
    if len(_walkroute_cache) > 512:
        _walkroute_cache.pop(next(iter(_walkroute_cache)))
    _walkroute_cache[key] = out
    return out


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


@app.get("/api/r/{region}/plan")
@app.get("/api/plan")
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
        yesterday = midnight - timedelta(days=1)
        dates = ((midnight.strftime("%Y%m%d"), midnight.weekday(), 0),
                 (yesterday.strftime("%Y%m%d"), yesterday.weekday(), -86400))
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
                "stop_id": None, "stop_name": from_label or "Your address",
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
                "stop_id": None, "stop_name": to_label or "Your destination",
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
            # A trailing walk to the destination's door counts: "arrive"
            # means at the pizza joint, not at the last bus stop.
            if arrive is not None:
                for l in reversed(legs):
                    if l["kind"] == "ride":
                        break
                    arrive += l["secs"]
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


def _stop_name(con, stop_id: str) -> str:
    r = con.execute("SELECT stop_name FROM stops WHERE stop_id=?",
                    (stop_id,)).fetchone()
    return r["stop_name"] if r else stop_id


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


@app.get("/api/r/{region}/trip-times/{trip_id}")
@app.get("/api/trip-times/{trip_id}")
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
