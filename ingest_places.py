"""
Build a local business/POI index from the OpenStreetMap Australia extract:
every NAMED shop, cafe, supermarket, medical centre, pub, gym, servo,
shopping centre &c., so "bunnings burleigh" resolves instantly and locally —
Nominatim (rate-limited, remote) stays the fallback for anything OSM's
community hasn't mapped. Run ingest_overture.py AFTERWARDS to merge in
Overture Maps' places (small businesses OSM lacks) — rebuilding here
resets to OSM-only, so re-run the Overture merge each time.

    python ingest_places.py /cache/data/sources/australia.osm.pbf

Same throwaway-container recipe as ingest_walkgraph.py (osmium + libexpat
are deliberately not in the app image):

    podman run --rm --user root -v translink-data:/data \
      -v translink-basemap-cache:/cache translink-departures \
      sh -c 'apt-get update -qq && apt-get install -y -qq libexpat1 && \
             pip install --quiet osmium && \
             python ingest_places.py /cache/data/sources/australia.osm.pbf'

Kept small the same way the walking graph is: only places within ~2 km of
any stop in any region's GTFS DB. Output places.sqlite3:
    places(id, name, category, suburb, lat, lon)
    places_fts    FTS5 over name/category/suburb
"""

import os
import sqlite3
import sys
from pathlib import Path

import osmium

BASE = Path(__file__).parent
GTFS_DB = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")
DB_PATH = Path(os.environ.get("PLACES_DB")
               or GTFS_DB.parent / "places.sqlite3")

POI_KEYS = ("shop", "amenity", "tourism", "leisure", "office",
            "healthcare", "craft")
# Named examples of these are street furniture, not destinations.
NOISE = {"parking", "parking_space", "bench", "waste_basket", "shelter",
         "bicycle_parking", "vending_machine", "recycling",
         "drinking_water", "atm", "post_box", "charging_station",
         "telephone", "grit_bin", "hunting_stand"}

CELL = 0.01
BUFFER_CELLS = 2


def cell_key(lat: float, lon: float) -> int:
    return int((lat + 90) / CELL) * 40000 + int((lon + 180) / CELL)


def stop_buffer_cells() -> set:
    cells = set()
    dbs = sorted(GTFS_DB.parent.glob("gtfs*.sqlite3"))
    if not dbs:
        sys.exit(f"No gtfs*.sqlite3 in {GTFS_DB.parent} — ingest regions first")
    for db in dbs:
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
    print(f"stop buffer: {len(cells):,} cells from {len(dbs)} region DBs")
    return cells


def poi_of(tags):
    """(category, name, suburb) for a named, non-noise POI; else None."""
    name = tags.get("name")
    if not name:
        return None
    for key in POI_KEYS:
        val = tags.get(key)
        if val and val not in ("yes", "no") and val not in NOISE:
            # Only a FALLBACK for places beyond G-NAF's reach — see
            # address_buckets() for why G-NAF outranks the mapper's tag.
            suburb = tags.get("addr:suburb") or tags.get("addr:city") or ""
            return val.replace("_", " "), name, suburb
    return None


def address_buckets():
    """cell -> [(lat, lon, street_id)] plus street -> (locality, state).
    The NEAREST G-NAF address decides a place's suburb, and G-NAF is the
    authority even when OSM carries its own addr:suburb tag — mapper tags
    sit on the wrong side of a boundary often enough (Officeworks Robina
    tagged Mudgeeraba), and the earlier first-wins cell grid mislabelled
    boundary shops the same way (Pizza 1 on the Miami/Burleigh Waters
    line). "bunnings burleigh" is how people actually search — the suburb
    has to be the one the searcher would say."""
    gnaf = GTFS_DB.parent / "gnaf.sqlite3"
    if not gnaf.exists():
        print("no gnaf.sqlite3 — places keep only OSM's own addr:suburb tags")
        return {}, {}
    con = sqlite3.connect(gnaf)
    street_loc = {sid: (loc.title(), st) for sid, loc, st in
                  con.execute("SELECT id, locality, state FROM streets")}
    buckets = {}
    for sid, lat, lon in con.execute(
            "SELECT street_id, lat, lon FROM addresses"):
        buckets.setdefault(cell_key(lat, lon), []).append((lat, lon, sid))
    con.close()
    print(f"address buckets: {len(buckets):,} cells from G-NAF")
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


class Collector(osmium.SimpleHandler):
    def __init__(self, keep, con, buckets, street_loc):
        super().__init__()
        self.keep = keep
        self.con = con
        self.buckets = buckets
        self.street_loc = street_loc
        self.batch = []
        self.count = 0

    def add(self, lat, lon, poi):
        base = cell_key(lat, lon)
        if base not in self.keep:
            return
        category, name, suburb = poi
        state = ""
        hit = nearest_locality(self.buckets, self.street_loc, lat, lon)
        if hit:
            suburb, state = hit   # G-NAF wins; the OSM tag is the fallback
        alt = name.replace("'", "").replace("\u2019", "")
        self.batch.append((name, category, suburb, state,
                           alt if alt != name else "", lat, lon))
        self.count += 1
        if len(self.batch) >= 20_000:
            self.flush()

    def flush(self):
        self.con.executemany(
            "INSERT INTO places (name, category, suburb, state, alt, lat, lon) "
            "VALUES (?,?,?,?,?,?,?)", self.batch)
        self.batch.clear()

    def node(self, n):
        poi = poi_of(n.tags)
        if poi and n.location.valid():
            self.add(n.location.lat, n.location.lon, poi)

    def way(self, w):
        poi = poi_of(w.tags)
        if not poi:
            return
        pts = [(n.location.lat, n.location.lon)
               for n in w.nodes if n.location.valid()]
        if not pts:
            return
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        self.add(lat, lon, poi)


def build(pbf: Path) -> None:
    keep = stop_buffer_cells()
    tmp = DB_PATH.with_suffix(".sqlite3.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode=OFF;  PRAGMA synchronous=OFF;
        CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT,
                             category TEXT, suburb TEXT, state TEXT,
                             alt TEXT, lat REAL, lon REAL,
                             source TEXT DEFAULT 'osm');
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    buckets, street_loc = address_buckets()
    print(f"Reading {pbf} (named places near stops)…")
    h = Collector(keep, con, buckets, street_loc)
    idx = "sparse_file_array," + str(tmp) + ".nodecache"
    h.apply_file(str(pbf), locations=True, idx=idx)
    h.flush()
    Path(str(tmp) + ".nodecache").unlink(missing_ok=True)
    print(f"  kept {h.count:,} places")
    print("Building the FTS index…")
    con.executescript("""
        CREATE VIRTUAL TABLE places_fts USING fts5(
            name, category, suburb, state, alt,
            content='places', content_rowid='id');
        INSERT INTO places_fts (rowid, name, category, suburb, state, alt)
            SELECT id, name, category, suburb, state, alt FROM places;
    """)
    con.execute("INSERT INTO meta VALUES ('source', ?)", (pbf.name,))
    n = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)
    print(f"Done. {n:,} places in {DB_PATH} "
          f"({DB_PATH.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).exists():
        sys.exit("Usage: python ingest_places.py <australia.osm.pbf>")
    build(Path(sys.argv[1]))
