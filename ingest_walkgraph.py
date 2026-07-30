"""
Build a walkable street graph from the OpenStreetMap Australia extract, for
drawing planner walk legs along actual streets (no dedicated footpath dataset
exists — OSM's ways ARE the footpath data: footways, paths, residential
streets, service roads).

    python ingest_walkgraph.py /cache/data/sources/australia.osm.pbf

Needs the `osmium` package, which is deliberately NOT in the app image
(30 MB of C++ for a quarterly-ish job), and libexpat, which the slim base
image lacks. Run it in a throwaway container as root:

    podman run --rm --user root -v translink-data:/data \
      -v translink-basemap-cache:/cache translink-departures \
      sh -c 'apt-get update -qq && apt-get install -y -qq libexpat1 && \
             pip install --quiet osmium && \
             python ingest_walkgraph.py /cache/data/sources/australia.osm.pbf'

(The pbf is already there — the basemap builder caches it.)

Size control: walk legs only exist near stops (200 m transfers, <=1 km
address-origin walks), so only ways passing within ~2 km of ANY stop in any
region's GTFS DB are kept. The outback drops out; the artifact stays small
enough to ship like a basemap (walkgraph-db in deploy/state).

Output walkgraph.sqlite3:
    nodes(id, lat, lon, cell)   cell = 0.01-degree grid key for nearest-node
    edges(a, b, d)              both directions, d in metres
"""

import math
import os
import sqlite3
import sys
from pathlib import Path

import osmium

BASE = Path(__file__).parent
GTFS_DB = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")
DB_PATH = Path(os.environ.get("WALKGRAPH_DB")
               or GTFS_DB.parent / "walkgraph.sqlite3")

# Ways a pedestrian can use. Motorways/trunks excluded; everything else that
# OSM commonly tags walkable, kept unless explicitly foot=no / private.
WALKABLE = {
    "footway", "path", "pedestrian", "steps", "living_street", "track",
    "residential", "service", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link", "cycleway",
    "road", "corridor",
}

CELL = 0.01           # ~1.1 km grid for nearest-node lookup
BUFFER_CELLS = 2      # stop cell +-2 cells ≈ 2-3 km kept radius


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


def haversine_m(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


class WayCollector(osmium.SimpleHandler):
    def __init__(self, keep_cells, con):
        super().__init__()
        self.keep = keep_cells
        self.con = con
        self.node_batch = {}
        self.edge_batch = []
        self.ways = 0

    def flush(self, force=False):
        if not force and len(self.edge_batch) < 50_000:
            return
        self.con.executemany(
            "INSERT OR IGNORE INTO nodes VALUES (?,?,?,?)",
            [(nid, la, lo, cell_key(la, lo))
             for nid, (la, lo) in self.node_batch.items()])
        self.con.executemany("INSERT INTO edges VALUES (?,?,?)",
                             self.edge_batch)
        self.node_batch.clear()
        self.edge_batch.clear()

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in WALKABLE:
            return
        if w.tags.get("foot") == "no":
            return
        if w.tags.get("access") in ("private", "no") \
                and w.tags.get("foot") not in ("yes", "designated"):
            return
        pts = [(n.ref, n.location.lat, n.location.lon)
               for n in w.nodes if n.location.valid()]
        if len(pts) < 2:
            return
        # keep the way if ANY of its nodes is near a stop
        if not any(cell_key(la, lo) in self.keep for _, la, lo in pts):
            return
        self.ways += 1
        for (aid, ala, alo), (bid, bla, blo) in zip(pts, pts[1:]):
            d = max(1, int(haversine_m(ala, alo, bla, blo)))
            self.node_batch[aid] = (ala, alo)
            self.node_batch[bid] = (bla, blo)
            self.edge_batch.append((aid, bid, d))
            self.edge_batch.append((bid, aid, d))
        self.flush()


def build(pbf: Path) -> None:
    keep = stop_buffer_cells()
    tmp = DB_PATH.with_suffix(".sqlite3.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode=OFF;  PRAGMA synchronous=OFF;
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL,
                            cell INTEGER);
        CREATE TABLE edges (a INTEGER, b INTEGER, d INTEGER);
        CREATE TABLE meta  (key TEXT PRIMARY KEY, value TEXT);
    """)
    print(f"Reading {pbf} (walkable ways near stops)…")
    h = WayCollector(keep, con)
    idx = "sparse_file_array," + str(tmp) + ".nodecache"
    h.apply_file(str(pbf), locations=True, idx=idx)
    h.flush(force=True)
    Path(str(tmp) + ".nodecache").unlink(missing_ok=True)
    print(f"  kept {h.ways:,} ways")
    print("Indexing…")
    con.executescript("""
        CREATE INDEX edges_a ON edges (a);
        CREATE INDEX nodes_cell ON nodes (cell);
    """)
    con.execute("INSERT INTO meta VALUES ('source', ?)", (pbf.name,))
    n_nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)
    print(f"Done. {n_nodes:,} nodes / {n_edges:,} directed edges "
          f"in {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).exists():
        sys.exit("Usage: python ingest_walkgraph.py <australia.osm.pbf>")
    build(Path(sys.argv[1]))
