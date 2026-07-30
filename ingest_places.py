"""
Build a local business/POI index from the OpenStreetMap Australia extract:
every NAMED shop, cafe, supermarket, medical centre, pub, gym, servo,
shopping centre &c., so "bunnings burleigh" resolves instantly and locally —
Nominatim (rate-limited, remote) stays the fallback for anything OSM's
community hasn't mapped.

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
            suburb = tags.get("addr:suburb") or tags.get("addr:city") or ""
            return val.replace("_", " "), name, suburb
    return None


def locality_grid():
    """cell -> (suburb, state), derived from G-NAF: most OSM POIs carry no
    addr:suburb, but "bunnings burleigh" is how people actually search —
    the suburb has to come from somewhere, and we own 10M geocoded
    addresses that know theirs."""
    gnaf = GTFS_DB.parent / "gnaf.sqlite3"
    grid = {}
    if not gnaf.exists():
        print("no gnaf.sqlite3 — places keep only OSM's own addr:suburb tags")
        return grid
    con = sqlite3.connect(gnaf)
    rows = con.execute(
        "SELECT s.locality, s.state, a.lat, a.lon "
        "FROM addresses a JOIN streets s ON s.id = a.street_id")
    for locality, state, lat, lon in rows:
        grid.setdefault(cell_key(lat, lon), (locality.title(), state))
    con.close()
    print(f"locality grid: {len(grid):,} cells from G-NAF")
    return grid


class Collector(osmium.SimpleHandler):
    def __init__(self, keep, con, grid):
        super().__init__()
        self.keep = keep
        self.con = con
        self.grid = grid
        self.batch = []
        self.count = 0

    def add(self, lat, lon, poi):
        base = cell_key(lat, lon)
        if base not in self.keep:
            return
        category, name, suburb = poi
        state = ""
        hit = self.grid.get(base)
        if hit is None:
            for d in (1, -1, 40000, -40000, 40001, -40001, 39999, -39999):
                hit = self.grid.get(base + d)
                if hit:
                    break
        if hit:
            if not suburb:
                suburb = hit[0]   # OSM's own addr:suburb wins when present
            state = hit[1]
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
                             alt TEXT, lat REAL, lon REAL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    grid = locality_grid()
    print(f"Reading {pbf} (named places near stops)…")
    h = Collector(keep, con, grid)
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
