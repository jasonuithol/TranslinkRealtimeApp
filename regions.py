# regions: the networks, their feeds, and shared realtime state
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import os
from fastapi import HTTPException
from pathlib import Path
from zoneinfo import ZoneInfo

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
def region_cfg(region: str) -> dict:
    cfg = REGIONS.get(region)
    if cfg is None:
        raise HTTPException(404, f"Unknown region {region!r}")
    return cfg


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
