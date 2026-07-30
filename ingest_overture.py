"""
Deepen places.sqlite3 with Overture Maps' places layer: open data (CDLA
Permissive 2.0) sourced largely from Meta's business pages — the small
shops OSM's community hasn't mapped ("Pizza 1", Miami QLD: present at
confidence 0.99). Run AFTER ingest_places.py; this MERGES into its output.

    python ingest_overture.py

Needs `duckdb` (not in the app image). Throwaway container:

    podman run --rm --user root -v translink-data:/data translink-departures \
      sh -c 'pip install --quiet duckdb && python ingest_overture.py'

The release tag below rotates monthly — list current ones with:
    curl -s 'https://overturemaps-us-west-2.s3.amazonaws.com/?list-type=2&prefix=release/&delimiter=/'

Filters: Australia bbox, confidence >= 0.5 (Overture's own score — dead
chains and junk sit lower; "Eagle Boys Pizza" scores 0.35), within ~2 km
of any stop, and not a duplicate of an existing entry (same normalised
name within ~150 m). Suburb/state from Overture's own address when
present, else the G-NAF locality grid. Atomic swap, like every ingest.
"""

import math
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

RELEASE = "2026-07-22.0"
S3 = (f"s3://overturemaps-us-west-2/release/{RELEASE}/"
      f"theme=places/type=place/*.parquet")
AU = (112.0, -44.5, 155.0, -9.0)          # lonmin, latmin, lonmax, latmax
MIN_CONFIDENCE = 0.5
DEDUPE_M = 150

STATE_ABBR = {"queensland": "QLD", "new south wales": "NSW",
              "victoria": "VIC", "south australia": "SA",
              "western australia": "WA", "northern territory": "NT",
              "tasmania": "TAS", "australian capital territory": "ACT"}

BASE = Path(__file__).parent
GTFS_DB = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")
DB_PATH = Path(os.environ.get("PLACES_DB")
               or GTFS_DB.parent / "places.sqlite3")

CELL = 0.01
BUFFER_CELLS = 2


def cell_key(lat, lon):
    return int((lat + 90) / CELL) * 40000 + int((lon + 180) / CELL)


def stop_buffer_cells():
    cells = set()
    for db in sorted(GTFS_DB.parent.glob("gtfs*.sqlite3")):
        try:
            con = sqlite3.connect(db)
            rows = con.execute("SELECT stop_lat, stop_lon FROM stops "
                               "WHERE stop_lat IS NOT NULL").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for lat, lon in rows:
            base = cell_key(lat, lon)
            for dy in range(-BUFFER_CELLS, BUFFER_CELLS + 1):
                for dx in range(-BUFFER_CELLS, BUFFER_CELLS + 1):
                    cells.add(base + dy * 40000 + dx)
    if not cells:
        sys.exit("No stops found — ingest regions first")
    print(f"stop buffer: {len(cells):,} cells")
    return cells


def address_buckets():
    """Nearest G-NAF address decides a place's suburb — a coarse
    cell-grid mislabelled suburb-boundary shops (Pizza 1 sat on the
    Miami/Burleigh Waters line and got the wrong side)."""
    gnaf = GTFS_DB.parent / "gnaf.sqlite3"
    if not gnaf.exists():
        return {}, {}
    con = sqlite3.connect(gnaf)
    street_loc = {sid: (loc.title(), st) for sid, loc, st in
                  con.execute("SELECT id, locality, state FROM streets")}
    buckets = defaultdict(list)
    for sid, lat, lon in con.execute(
            "SELECT street_id, lat, lon FROM addresses"):
        buckets[cell_key(lat, lon)].append((lat, lon, sid))
    con.close()
    print(f"address buckets: {len(buckets):,} cells")
    return buckets, street_loc


def nearest_locality(buckets, street_loc, lat, lon, limit_m=400.0):
    base = cell_key(lat, lon)
    best, bd = None, limit_m * limit_m
    for delta in (-40001, -40000, -39999, -1, 0, 1, 39999, 40000, 40001):
        for ala, alo, sid in buckets.get(base + delta, ()):
            d2 = ((ala - lat) * 111000.0) ** 2 + ((alo - lon) * 88000.0) ** 2
            if d2 < bd:
                bd, best = d2, sid
    return street_loc[best] if best is not None else None


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def haversine_m(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def main():
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} missing — run ingest_places.py first")
    keep = stop_buffer_cells()
    buckets, street_loc = address_buckets()

    tmp = DB_PATH.with_suffix(".sqlite3.tmp")
    shutil.copyfile(DB_PATH, tmp)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    try:
        con.execute("ALTER TABLE places ADD COLUMN source TEXT DEFAULT 'osm'")
    except sqlite3.OperationalError:
        pass  # already merged once before

    # Existing entries, cell-bucketed, for the near-duplicate check.
    existing = defaultdict(list)
    for name, lat, lon in con.execute("SELECT name, lat, lon FROM places"):
        existing[cell_key(lat, lon)].append((norm(name), lat, lon))

    def is_dup(nname, lat, lon):
        base = cell_key(lat, lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for en, ela, elo in existing.get(base + dy * 40000 + dx, ()):
                    if haversine_m(lat, lon, ela, elo) > DEDUPE_M:
                        continue
                    if en == nname or (len(nname) >= 4 and
                                       (en.startswith(nname)
                                        or nname.startswith(en))):
                        return True
        return False

    print(f"Querying Overture {RELEASE} places over Australia "
          "(S3 parquet; this transfers a while)…")
    duck = duckdb.connect()
    duck.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    cur = duck.execute(f"""
        SELECT names.primary, categories.primary, confidence,
               bbox.ymin, bbox.xmin,
               addresses[1].locality, addresses[1].region
        FROM read_parquet('{S3}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {AU[0]} AND {AU[2]}
          AND bbox.ymin BETWEEN {AU[1]} AND {AU[3]}
          AND confidence >= {MIN_CONFIDENCE}
          AND names.primary IS NOT NULL""")

    fetched = kept = dups = outside = 0
    batch = []
    while rows := cur.fetchmany(50_000):
        for name, category, conf, lat, lon, loc, region in rows:
            fetched += 1
            base = cell_key(lat, lon)
            if base not in keep:
                outside += 1
                continue
            nname = norm(name)
            if not nname or is_dup(nname, lat, lon):
                dups += 1
                continue
            # Overture's locality is often the CITY ("Gold Coast"), not
            # the suburb ("Miami") people search by — the G-NAF grid wins;
            # Overture's variant stays searchable via the alt column.
            state = (region or "").split("-")[-1]
            state = STATE_ABBR.get(state.lower(),
                                   state.upper() if len(state) <= 3 else state)
            ov_loc = (loc or "").title()
            hit = nearest_locality(buckets, street_loc, lat, lon)
            suburb = hit[0] if hit else ov_loc
            if hit and not state:
                state = hit[1]
            cat = (category or "place").replace("_", " ")
            alt_bits = []
            stripped = name.replace("'", "").replace("’", "")
            if stripped != name:
                alt_bits.append(stripped)
            if ov_loc and ov_loc != suburb:
                alt_bits.append(ov_loc)
            batch.append((name, cat, suburb, state,
                          " ".join(alt_bits), lat, lon, "overture"))
            existing[base].append((nname, lat, lon))
            kept += 1
        if batch:
            con.executemany(
                "INSERT INTO places (name, category, suburb, state, alt, "
                "lat, lon, source) VALUES (?,?,?,?,?,?,?,?)", batch)
            batch.clear()
        print(f"  {fetched:,} fetched / {kept:,} merged "
              f"/ {dups:,} dup / {outside:,} outside", flush=True)
    con.commit()

    print("Rebuilding the FTS index…")
    con.executescript("""
        DROP TABLE places_fts;
        CREATE VIRTUAL TABLE places_fts USING fts5(
            name, category, suburb, state, alt,
            content='places', content_rowid='id');
        INSERT INTO places_fts (rowid, name, category, suburb, state, alt)
            SELECT id, name, category, suburb, state, alt FROM places;
    """)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('overture', ?)",
                (RELEASE,))
    total = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)
    print(f"Done. +{kept:,} Overture places, {total:,} total "
          f"in {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
