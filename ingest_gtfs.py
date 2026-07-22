"""
Ingest a region's static GTFS feed into a local SQLite database.

Usage:
    python ingest_gtfs.py                          # SEQ: download + ingest
    python ingest_gtfs.py SEQ_GTFS.zip             # SEQ: ingest a local zip
    python ingest_gtfs.py --region mel             # Melbourne: download + ingest
    python ingest_gtfs.py --region mel gtfs.zip    # Melbourne: local zip
    python ingest_gtfs.py --region syd --key K     # Sydney: download + ingest
    python ingest_gtfs.py --region syd DIR/        # Sydney: dir of <prefix>.zip

Regions:
  seq  Translink South East Queensland — one flat GTFS zip, ids globally unique.
  mel  PTV Victoria — one outer zip containing a *nested* zip per mode
       (2 = metro train, 3 = tram, 4 = metro bus, …). Ids are only unique
       within a mode, so every id is prefixed "<mode>:" on the way in; the
       realtime pollers apply the same prefix per feed (see app.py REGIONS).
  syd  TfNSW Sydney — one flat "Timetables For Realtime" zip per mode,
       each downloaded separately and each needing the (free) TfNSW key
       (--key or SYD_API_KEY; sent as `Authorization: apikey <key>`). Same
       per-feed prefixing as mel. A source that 404s is skipped with a
       warning, so an endpoint moving doesn't sink the whole ingest.

The static feeds change roughly weekly; re-run this to refresh.
Only the tables needed for a departures board are loaded.
"""

import argparse
import csv
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

BASE = Path(__file__).parent
SEQ_DB = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")

REGIONS = {
    "seq": {
        "url": "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip",
        "db": SEQ_DB,
        # One flat zip, no prefixing.
        "modes": None,
    },
    "mel": {
        "url": "https://data.ptv.vic.gov.au/downloads/gtfs.zip",
        "db": Path(os.environ.get("MEL_GTFS_DB") or SEQ_DB.parent / "gtfs-mel.sqlite3"),
        # Inner zips to load: PTV numbers them by mode. Metropolitan Melbourne:
        # 2 = metro train, 3 = tram, 4 = metro bus. (1/5/6 are regional
        # train/coach/bus — add them here if the board should cover Victoria.)
        "modes": ["2", "3", "4"],
    },
    "syd": {
        "db": Path(os.environ.get("SYD_GTFS_DB") or SEQ_DB.parent / "gtfs-syd.sqlite3"),
        "modes": None,
        # One flat "For Realtime" GTFS zip per mode, downloaded separately.
        # The prefix is the contract with app.py's SYD_* realtime env config:
        # ids in feed "t" become "t:...", and the trip-updates feed declared
        # "t|<url>" maps its ids onto them. Greater Sydney modes only
        # (nswtrains/regionbuses are intercity and out of scope).
        "sources": [
            ("t",  "https://api.transport.nsw.gov.au/v2/gtfs/schedule/sydneytrains"),
            ("m",  "https://api.transport.nsw.gov.au/v2/gtfs/schedule/metro"),
            ("b",  "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses"),
            ("f",  "https://api.transport.nsw.gov.au/v1/gtfs/schedule/ferries/sydneyferries"),
            ("lw", "https://api.transport.nsw.gov.au/v1/gtfs/schedule/lightrail/innerwest"),
            ("lc", "https://api.transport.nsw.gov.au/v1/gtfs/schedule/lightrail/cbdandsoutheast"),
            ("lp", "https://api.transport.nsw.gov.au/v1/gtfs/schedule/lightrail/parramatta"),
        ],
    },
}


def syd_headers(key: str) -> dict:
    """TfNSW auth: `Authorization: apikey <token>` — scheme word really is
    lowercase 'apikey'. Accepts a bare token or a full 'apikey …' value."""
    key = key.strip()
    if not key:
        return {}
    if not key.lower().startswith("apikey "):
        key = f"apikey {key}"
    return {"Authorization": key}

SCHEMA = """
DROP TABLE IF EXISTS stops;
DROP TABLE IF EXISTS routes;
DROP TABLE IF EXISTS trips;
DROP TABLE IF EXISTS stop_times;
DROP TABLE IF EXISTS calendar;
DROP TABLE IF EXISTS calendar_dates;
DROP TABLE IF EXISTS shapes;

CREATE TABLE stops (
    stop_id TEXT PRIMARY KEY,
    stop_name TEXT,
    stop_lat REAL,
    stop_lon REAL,
    location_type INTEGER,   -- 1 = parent station, 0/blank = stop or platform
    parent_station TEXT,
    platform_code TEXT
);
CREATE INDEX idx_stops_parent ON stops (parent_station);
CREATE TABLE routes (
    route_id TEXT PRIMARY KEY,
    route_short_name TEXT,
    route_long_name TEXT,
    route_type INTEGER,
    route_color TEXT
);
CREATE TABLE trips (
    trip_id TEXT PRIMARY KEY,
    route_id TEXT,
    service_id TEXT,
    trip_headsign TEXT,
    direction_id INTEGER,
    shape_id TEXT           -- route geometry, for drawing the path on the map
);
CREATE TABLE stop_times (
    trip_id TEXT,
    arrival_time TEXT,
    departure_time TEXT,
    stop_id TEXT,
    stop_sequence INTEGER
);
CREATE INDEX idx_stop_times_stop ON stop_times (stop_id);
CREATE INDEX idx_stop_times_trip ON stop_times (trip_id);
CREATE TABLE calendar (
    service_id TEXT PRIMARY KEY,
    monday INTEGER, tuesday INTEGER, wednesday INTEGER, thursday INTEGER,
    friday INTEGER, saturday INTEGER, sunday INTEGER,
    start_date TEXT,
    end_date TEXT
);
CREATE TABLE calendar_dates (
    service_id TEXT,
    date TEXT,
    exception_type INTEGER
);
CREATE INDEX idx_caldates ON calendar_dates (date);
-- The path a trip physically follows. Many trips share one shape, so this is
-- keyed by shape_id, not trip_id.
CREATE TABLE shapes (
    shape_id TEXT,
    shape_pt_lat REAL,
    shape_pt_lon REAL,
    shape_pt_sequence INTEGER
);
CREATE INDEX idx_shapes ON shapes (shape_id, shape_pt_sequence);
"""

# table -> columns we keep (matching CSV header names)
TABLES = {
    "stops": ["stop_id", "stop_name", "stop_lat", "stop_lon",
              "location_type", "parent_station", "platform_code"],
    "routes": ["route_id", "route_short_name", "route_long_name", "route_type", "route_color"],
    "trips": ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id",
              "shape_id"],
    "stop_times": ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    "calendar": ["service_id", "monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday", "start_date", "end_date"],
    "calendar_dates": ["service_id", "date", "exception_type"],
    "shapes": ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
}

# Columns that hold feed-local identifiers. For a multi-feed region every value
# here gets the mode prefix, keeping ids unique after the feeds are merged and
# keeping every cross-table reference (trip -> stops, trip -> shape, …) intact.
ID_COLS = {
    "stops": ["stop_id", "parent_station"],
    "routes": ["route_id"],
    "trips": ["trip_id", "route_id", "service_id", "shape_id"],
    "stop_times": ["trip_id", "stop_id"],
    "calendar": ["service_id"],
    "calendar_dates": ["service_id"],
    "shapes": ["shape_id"],
}


def normalize_route_type(value: str | None) -> str | None:
    """Collapse Google's extended route types onto the basic GTFS set.

    PTV publishes extended types (400 = urban railway for metro trains,
    701 = regional bus, 900s = tram); Translink uses the basic 0-4. The whole
    app — rail-station detection, mode emoji, labels — keys on the basic set,
    so normalise once here rather than teaching every consumer both schemes.
    """
    if not value:
        return value
    try:
        t = int(value)
    except ValueError:
        return value
    if t <= 12:
        return value                      # already basic
    if 100 <= t < 200:  return "2"        # railway service
    if 200 <= t < 300:  return "3"        # coach
    if 400 <= t < 500:  return "1"        # urban railway / metro
    if 700 <= t < 800:  return "3"        # bus
    if 900 <= t < 1000: return "0"        # tram
    if 1000 <= t < 1100: return "4"       # water transport
    return "3"                            # anything exotic reads as a bus


def download_feed(url: str, dest: Path, headers: dict | None = None) -> Path:
    print(f"Downloading {url} ...")
    with httpx.stream("GET", url, timeout=300, follow_redirects=True,
                      headers=headers or {}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    print(f"Saved to {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def load_feed_zip(con: sqlite3.Connection, zf: zipfile.ZipFile, prefix: str = "") -> None:
    """Load one flat GTFS zip into the tables, prefixing identifier columns."""
    for table, cols in TABLES.items():
        fname = f"{table}.txt"
        if fname not in zf.namelist():
            print(f"  (skipping {fname}, not in feed)")
            continue
        print(f"  loading {fname}{f' [{prefix}]' if prefix else ''} ...")
        id_idx = [cols.index(c) for c in ID_COLS[table]]
        rt_idx = cols.index("route_type") if table == "routes" else None
        with zf.open(fname) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            placeholders = ",".join("?" for _ in cols)
            sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
            batch = []
            for row in reader:
                vals = [row.get(c) for c in cols]
                if prefix:
                    for i in id_idx:
                        if vals[i]:   # empty parent_station stays empty
                            vals[i] = prefix + vals[i]
                if rt_idx is not None:
                    vals[rt_idx] = normalize_route_type(vals[rt_idx])
                batch.append(tuple(vals))
                if len(batch) >= 50_000:
                    con.executemany(sql, batch)
                    batch.clear()
            if batch:
                con.executemany(sql, batch)
        con.commit()


def ingest(region: str, zips: list[tuple[str, Path]]) -> None:
    # Build into a temp file and swap it in atomically. SCHEMA drops every table
    # first, so ingesting over a live DB would leave the running server querying
    # half-dropped tables for the duration. app.py opens a fresh connection per
    # request, so a rename moves readers onto the finished DB between requests.
    # `zips` is [(prefix, path)]: one flat zip per entry — several for a
    # multi-download region (syd), a single ("", path) otherwise.
    cfg = REGIONS[region]
    db_path = cfg["db"]
    tmp_path = Path(str(db_path) + ".tmp")
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    con.executescript(SCHEMA)

    for prefix, zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            if cfg["modes"] is None:
                load_feed_zip(con, zf, prefix=f"{prefix}:" if prefix else "")
            else:
                # PTV nests one zip per mode inside the outer zip. Inner zips
                # are large (metro bus stop_times especially), so spool each
                # to disk rather than holding it in memory.
                names = zf.namelist()
                for mode in cfg["modes"]:
                    inner_name = next(
                        (n for n in names
                         if n.strip("/").startswith(f"{mode}/") and n.endswith(".zip")),
                        None,
                    )
                    if inner_name is None:
                        print(f"  (mode {mode}: no inner zip found, skipping)")
                        continue
                    print(f"  mode {mode}: {inner_name}")
                    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp_zip:
                        with zf.open(inner_name) as src:
                            shutil.copyfileobj(src, tmp_zip)
                        tmp_zip.flush()
                        with zipfile.ZipFile(tmp_zip.name) as inner:
                            load_feed_zip(con, inner, prefix=f"{mode}:")

    n = con.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0]
    con.close()
    os.replace(tmp_path, db_path)
    print(f"Done. {n:,} stop_times rows in {db_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("zip", nargs="?",
                    help="already-downloaded feed zip (or, for a multi-source "
                         "region, a directory of <prefix>.zip files)")
    ap.add_argument("--region", default="seq", choices=sorted(REGIONS))
    ap.add_argument("--key", default=os.environ.get("SYD_API_KEY", ""),
                    help="API key for regions whose downloads need one "
                         "(syd; default $SYD_API_KEY)")
    args = ap.parse_args()

    cfg = REGIONS[args.region]
    if "sources" in cfg:
        pairs: list[tuple[str, Path]] = []
        if args.zip:
            src_dir = Path(args.zip)
            if not src_dir.is_dir():
                sys.exit(f"{args.region} takes a DIRECTORY of <prefix>.zip files")
            for prefix, _url in cfg["sources"]:
                p = src_dir / f"{prefix}.zip"
                if p.exists():
                    pairs.append((prefix, p))
                else:
                    print(f"  ({prefix}: no {p.name} in {src_dir}, skipping)")
        else:
            headers = syd_headers(args.key)
            if not headers:
                sys.exit("Sydney downloads need a TfNSW open data key: "
                         "--key or SYD_API_KEY (free at "
                         "https://opendata.transport.nsw.gov.au/)")
            for prefix, url in cfg["sources"]:
                dest = cfg["db"].parent / f"{args.region}_{prefix}.zip"
                try:
                    download_feed(url, dest, headers=headers)
                except httpx.HTTPStatusError as e:
                    print(f"  ({prefix}: HTTP {e.response.status_code} from "
                          f"{url} — skipping)")
                    continue
                pairs.append((prefix, dest))
        if not pairs:
            sys.exit("nothing to ingest")
        ingest(args.region, pairs)
    else:
        if args.zip:
            zpath = Path(args.zip)
        else:
            zpath = cfg["db"].parent / f"{args.region}_gtfs.zip"
            download_feed(cfg["url"], zpath)
        ingest(args.region, [("", zpath)])
