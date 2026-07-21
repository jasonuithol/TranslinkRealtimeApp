"""
Ingest Translink's static SEQ GTFS feed into a local SQLite database.

Usage:
    python ingest_gtfs.py                  # downloads the feed, then ingests
    python ingest_gtfs.py SEQ_GTFS.zip     # ingest an already-downloaded zip

The static feed changes roughly weekly; re-run this to refresh.
Only the tables needed for a departures board are loaded.
"""

import csv
import io
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import httpx

GTFS_URL = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
# Overridable so the DB can live on a mounted volume (see Containerfile).
DB_PATH = Path(os.environ.get("GTFS_DB") or Path(__file__).parent / "gtfs.sqlite3")

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


def download_feed(dest: Path) -> Path:
    print(f"Downloading {GTFS_URL} ...")
    with httpx.stream("GET", GTFS_URL, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    print(f"Saved to {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def ingest(zip_path: Path) -> None:
    # Build into a temp file and swap it in atomically. SCHEMA drops every table
    # first, so ingesting over a live DB would leave the running server querying
    # half-dropped tables for the duration. app.py opens a fresh connection per
    # request, so a rename moves readers onto the finished DB between requests.
    tmp_path = Path(str(DB_PATH) + ".tmp")
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    con.executescript(SCHEMA)
    with zipfile.ZipFile(zip_path) as zf:
        for table, cols in TABLES.items():
            fname = f"{table}.txt"
            if fname not in zf.namelist():
                print(f"  (skipping {fname}, not in feed)")
                continue
            print(f"  loading {fname} ...")
            with zf.open(fname) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
                placeholders = ",".join("?" for _ in cols)
                sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
                batch = []
                for row in reader:
                    batch.append(tuple(row.get(c) for c in cols))
                    if len(batch) >= 50_000:
                        con.executemany(sql, batch)
                        batch.clear()
                if batch:
                    con.executemany(sql, batch)
            con.commit()
    n = con.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0]
    con.close()
    os.replace(tmp_path, DB_PATH)
    print(f"Done. {n:,} stop_times rows in {DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        zpath = Path(sys.argv[1])
    else:
        zpath = DB_PATH.parent / "SEQ_GTFS.zip"
        download_feed(zpath)
    ingest(zpath)
