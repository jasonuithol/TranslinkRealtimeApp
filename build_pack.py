"""Build an on-device region pack: everything Transport Honker needs to
answer a journey question with no server involved.

The VPS holds whole-of-Australia data — 14 M walk-graph nodes, 10 M
addresses, nine timetables. A phone needs one region's worth, so this
slices each national database to the area a region's own stops cover,
drops what the app never reads, and rewrites the result compactly.

The pack is FOUR files rather than one, because they age at different
rates: the timetable is reissued weekly, the walk graph and addresses
change quarterly, the basemap almost never. Separate files mean the
weekly update is a ~30 MB timetable download, not the whole pack.

    packs/seq/
      manifest.json      versions, sizes, sha256, timetable validity
      timetable.sqlite3  stops, routes, trips, stop_times, shapes
      walk.sqlite3       the walking graph, renumbered densely
      addresses.sqlite3  G-NAF addresses + streets + FTS
      places.sqlite3     named places (POIs) + FTS
      basemap.pmtiles    the map itself (copied, already compact)

Run (the databases live on the data volume, so mount it):

    podman run --rm --user root \\
      -v translink-data:/data -v "$PWD:/repo:ro" -v "$PWD/packs:/packs:z" \\
      localhost/translink-dev:latest \\
      python /repo/build_pack.py --region seq --out /packs

Three deliberate departures from the server schema, all of them paid for
by measurement — a straight copy of the SEQ timetable came to 247 MB, of
which 90% was stop_times and its indexes:

  * Ids are interned. A feed trip_id like "35827515-ATS_HBL 26-41689" is
    25 bytes, and stop_times stores it 2.4 million times over, once in
    the row and again in each index. The pack gives every trip, stop and
    shape a dense INTEGER id and keeps the feed's own id beside it in
    the parent table, where realtime matching still needs it.
  * stop_times is clustered on (trip, seq) as a WITHOUT ROWID table, so
    the table IS the by-trip index rather than carrying one.
  * Times are seconds into the service day, not "HH:MM:SS" text — a
    third of the size and no parsing on the hot path, with >86400 still
    meaning "after midnight, same service day".

Walk-graph node ids are likewise renumbered densely from 1, and its
coordinates are stored as micro-degree INTEGERs rather than 8-byte
REALs. The ported query layer must know all of this; it is written down
in the manifest and in the schema comments below.
"""
import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from regions import REGIONS, DB_PATH, BASEMAP_DIR      # noqa: E402

# How far outside a region's own stops to keep walk graph and addresses.
# A journey starts at your door, which may be a couple of kilometres past
# the outermost bus stop; 0.09 deg is ~10 km.
PAD_DEG = 0.09

# Shape simplification tolerance in metres. Route geometry is drawn on a
# phone screen: 8 m of error is a third of a pixel at the zoom where a
# whole route fits, and the point count roughly halves.
SHAPE_TOLERANCE_M = 8.0

# The spatial bucket, shared by the walk graph and the address table. This
# is ingest_walkgraph.py's grid and must stay identical to it — the pack
# inherits already-computed cell keys from the walk graph, and a proximity
# search that bucketed differently would look in the wrong drawer.
CELL_DEG = 0.01


def cell_key(lat: float, lon: float) -> int:
    return int((lat + 90) / CELL_DEG) * 40000 + int((lon + 180) / CELL_DEG)


def log(msg):
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def secs(hms):
    """GTFS "HH:MM:SS" -> seconds into the service day. Hours past 24 are
    the small hours of the next morning and must stay above 86400, which
    is exactly why this is not a clock time."""
    if not hms:
        return None
    try:
        h, m, s = hms.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except ValueError:
        return None


def fresh_db(path):
    """A new SQLite file, tuned for bulk writing then reading on a phone."""
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                      "PRAGMA cache_size=-200000; PRAGMA temp_store=MEMORY;")
    return con


def finish(con, path, label):
    """Compact, verify, and report. VACUUM is the whole point of the
    exercise: a slice built by INSERT..SELECT is riddled with free pages."""
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("VACUUM")
    con.execute("ANALYZE")
    con.commit()
    con.close()
    size = path.stat().st_size
    log(f"  {label:12} {human(size):>10}")
    return size


# --------------------------------------------------------------------------
# geometry


def _perp_m(p, a, b):
    """Perpendicular distance from p to segment a-b, in metres, flat-earth
    (fine over the tens of metres a shape segment spans)."""
    scale = math.cos(math.radians(p[0])) or 1e-9
    py, px = p[0] * 111_320, p[1] * 111_320 * scale
    ay, ax = a[0] * 111_320, a[1] * 111_320 * scale
    by, bx = b[0] * 111_320, b[1] * 111_320 * scale
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points, tol_m):
    """Douglas-Peucker, iterative — a rail shape can run to thousands of
    points and recursion on that is a stack overflow waiting to happen."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        worst, worst_i = -1.0, -1
        for k in range(i + 1, j):
            d = _perp_m(points[k], points[i], points[j])
            if d > worst:
                worst, worst_i = d, k
        if worst > tol_m:
            keep[worst_i] = True
            stack.append((i, worst_i))
            stack.append((worst_i, j))
    return [p for p, k in zip(points, keep) if k]


# --------------------------------------------------------------------------
# the timetable


def build_timetable(src_path, out, weeks):
    """The region's GTFS, pruned to services that actually run in the
    validity window and to the columns the app reads."""
    con = fresh_db(out)
    con.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
    today = date.today()
    until = today + timedelta(weeks=weeks)
    d0, d1 = today.strftime("%Y%m%d"), until.strftime("%Y%m%d")

    con.executescript("""
        -- `stop`/`trip`/`shape` are pack-local dense ids; `*_id` columns are
        -- the feed's own, kept because realtime messages speak in those.
        CREATE TABLE stops (
            stop INTEGER PRIMARY KEY, stop_id TEXT, stop_name TEXT,
            stop_lat REAL, stop_lon REAL, location_type INTEGER,
            parent_station TEXT, platform_code TEXT);
        CREATE TABLE routes (
            route_id TEXT PRIMARY KEY, route_short_name TEXT,
            route_long_name TEXT, route_type INTEGER, route_color TEXT);
        CREATE TABLE trips (
            trip INTEGER PRIMARY KEY, trip_id TEXT, route_id TEXT,
            service_id TEXT, trip_headsign TEXT, direction_id INTEGER,
            shape INTEGER);
        -- Clustered on (trip, seq): walking a trip's calls in order is a
        -- single contiguous read, and no by-trip index is needed at all.
        -- arr/dep are SECONDS into the service day.
        CREATE TABLE stop_times (
            trip INTEGER, seq INTEGER, stop INTEGER, arr INTEGER, dep INTEGER,
            PRIMARY KEY (trip, seq)) WITHOUT ROWID;
        CREATE TABLE calendar (
            service_id TEXT PRIMARY KEY, monday INTEGER, tuesday INTEGER,
            wednesday INTEGER, thursday INTEGER, friday INTEGER,
            saturday INTEGER, sunday INTEGER, start_date TEXT, end_date TEXT);
        CREATE TABLE calendar_dates (
            service_id TEXT, date TEXT, exception_type INTEGER);
        -- Shape geometry, clustered by shape: drawing a route is one scan.
        CREATE TABLE shapes (
            shape INTEGER, seq INTEGER, lat REAL, lon REAL,
            PRIMARY KEY (shape, seq)) WITHOUT ROWID;
        CREATE TABLE shape_ids (shape INTEGER PRIMARY KEY, shape_id TEXT);
    """)

    # Services that can run between now and the end of the window: either a
    # calendar row overlapping it with at least one weekday set, or an
    # explicit "added" exception inside it. Everything else is history, and
    # on a well-maintained feed that is most of the file.
    con.execute("""
        INSERT INTO calendar SELECT * FROM src.calendar
         WHERE (start_date <= ? AND end_date >= ?
                AND (monday+tuesday+wednesday+thursday+friday
                     +saturday+sunday) > 0)
            OR service_id IN (SELECT service_id FROM src.calendar_dates
                               WHERE exception_type = 1
                                 AND date BETWEEN ? AND ?)""", (d1, d0, d0, d1))
    con.execute("""
        INSERT INTO calendar_dates SELECT * FROM src.calendar_dates
         WHERE date BETWEEN ? AND ?""", (d0, d1))
    # A feed may run purely on calendar_dates with no calendar rows at all.
    con.execute("""
        INSERT OR IGNORE INTO calendar (service_id, monday, tuesday, wednesday,
                thursday, friday, saturday, sunday, start_date, end_date)
        SELECT DISTINCT service_id, 0,0,0,0,0,0,0, ?, ?
          FROM calendar_dates WHERE exception_type = 1""", (d0, d1))

    # --- intern the ids ---------------------------------------------------
    # Dense integers assigned once, here, and used everywhere downstream.
    # The maps are temp tables so the joins below stay in SQL.
    con.executescript("""
        CREATE TEMP TABLE trip_map  (trip_id TEXT PRIMARY KEY, trip INTEGER);
        CREATE TEMP TABLE stop_map  (stop_id TEXT PRIMARY KEY, stop INTEGER);
        CREATE TEMP TABLE shape_map (shape_id TEXT PRIMARY KEY, shape INTEGER);
    """)
    con.execute("""
        INSERT INTO temp.trip_map (trip_id, trip)
        SELECT trip_id, ROW_NUMBER() OVER (ORDER BY route_id, trip_id)
          FROM src.trips
         WHERE service_id IN (SELECT service_id FROM calendar)""")
    # Every stop in the feed gets an id, not just the called-at ones: the
    # map's nearby-stops search and parent-station grouping both want them.
    con.execute("""
        INSERT INTO temp.stop_map (stop_id, stop)
        SELECT stop_id, ROW_NUMBER() OVER (ORDER BY stop_id) FROM src.stops""")
    # DISTINCT must happen in a subquery: a window function is evaluated
    # before DISTINCT, so numbering the outer select would hand each
    # duplicate its own row number and defeat the de-duplication.
    con.execute("""
        INSERT INTO temp.shape_map (shape_id, shape)
        SELECT shape_id, ROW_NUMBER() OVER (ORDER BY shape_id) FROM (
            SELECT DISTINCT t.shape_id FROM src.trips t
              JOIN temp.trip_map m ON m.trip_id = t.trip_id
             WHERE t.shape_id IS NOT NULL AND t.shape_id <> '')""")

    con.execute("""
        INSERT INTO trips (trip, trip_id, route_id, service_id, trip_headsign,
                           direction_id, shape)
        SELECT m.trip, t.trip_id, t.route_id, t.service_id, t.trip_headsign,
               t.direction_id, sm.shape
          FROM src.trips t
          JOIN temp.trip_map m ON m.trip_id = t.trip_id
          LEFT JOIN temp.shape_map sm ON sm.shape_id = t.shape_id""")
    con.execute("""
        INSERT INTO routes SELECT route_id, route_short_name, route_long_name,
               route_type, route_color FROM src.routes
         WHERE route_id IN (SELECT route_id FROM trips)""")
    con.execute("""
        INSERT INTO stops (stop, stop_id, stop_name, stop_lat, stop_lon,
                           location_type, parent_station, platform_code)
        SELECT m.stop, s.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
               s.location_type, s.parent_station, s.platform_code
          FROM src.stops s JOIN temp.stop_map m ON m.stop_id = s.stop_id""")
    con.execute("INSERT INTO shape_ids SELECT shape, shape_id FROM temp.shape_map")

    # Times to integers. Streamed rather than INSERT..SELECT so the
    # conversion happens here rather than in SQL string functions.
    rows, batch = 0, []
    cur = con.execute("""
        SELECT tm.trip, st.stop_sequence, sm.stop, st.arrival_time,
               st.departure_time
          FROM src.stop_times st
          JOIN temp.trip_map tm ON tm.trip_id = st.trip_id
          JOIN temp.stop_map sm ON sm.stop_id = st.stop_id""")
    ins = con.cursor()
    for trip, seq, stop, arr, dep in cur:
        batch.append((trip, seq, stop, secs(arr), secs(dep)))
        if len(batch) >= 50_000:
            ins.executemany("INSERT OR REPLACE INTO stop_times "
                            "VALUES (?,?,?,?,?)", batch)
            rows += len(batch)
            batch.clear()
    if batch:
        ins.executemany("INSERT OR REPLACE INTO stop_times VALUES (?,?,?,?,?)",
                        batch)
        rows += len(batch)
    log(f"    stop_times: {rows:,}")

    # Every stop is kept, including ones nothing calls at in this window.
    # Dropping them looked like free space and was not: a station platform
    # with no service this month is still a place you can walk from, and
    # removing it shrank the station group the planner expands an origin
    # into, silently costing a legitimate Melbourne itinerary. The whole
    # stops table is under a megabyte.

    # Shapes, simplified. Only those a kept trip actually uses.
    before = after = 0
    ins = con.cursor()
    for shape, shape_id in con.execute(
            "SELECT shape, shape_id FROM temp.shape_map").fetchall():
        pts = con.execute(
            "SELECT shape_pt_lat, shape_pt_lon FROM src.shapes WHERE shape_id=? "
            "ORDER BY shape_pt_sequence", (shape_id,)).fetchall()
        if not pts:
            continue
        before += len(pts)
        thin = simplify(pts, SHAPE_TOLERANCE_M)
        after += len(thin)
        ins.executemany(
            "INSERT INTO shapes VALUES (?,?,?,?)",
            [(shape, i, round(la, 5), round(lo, 5))
             for i, (la, lo) in enumerate(thin)])
    if before:
        log(f"    shapes: {before:,} -> {after:,} points "
            f"({100 * after / before:.0f}%)")

    con.executescript("""
        -- The board's hot query: what leaves this stop after time T.
        CREATE INDEX stop_times_stop ON stop_times (stop, dep);
        CREATE UNIQUE INDEX trips_feed_id ON trips (trip_id);
        CREATE UNIQUE INDEX stops_feed_id ON stops (stop_id);
        CREATE INDEX idx_caldates ON calendar_dates (date);
        CREATE INDEX idx_stops_parent ON stops (parent_station);
        CREATE INDEX idx_trips_route ON trips (route_id);
    """)
    con.commit()                      # or DETACH finds the source still busy
    con.execute("DETACH DATABASE src")
    return {"valid_from": d0, "valid_to": d1, "stop_times": rows}


# --------------------------------------------------------------------------
# the walking graph


def build_walk(src_path, out, bbox):
    """Nodes inside the region's box and the edges between them, with ids
    renumbered densely from 1 — the national graph's ids run to 14 million
    and every one of them is stored twice in `edges`."""
    la1, la2, lo1, lo2 = bbox
    con = fresh_db(out)
    con.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
    con.executescript("""
        -- lat/lon are micro-degrees (deg * 1e6) as INTEGERs: a 4-byte
        -- varint each instead of an 8-byte REAL, and 1e-6 deg is 11 cm.
        -- There is no `cell` column and no index on one. Nodes are
        -- numbered in cell order, so a cell's nodes occupy a contiguous
        -- run of ids and `cells` records that run — an id-range scan
        -- reads the same pages a B-tree would have pointed at, without
        -- the 30 MB of B-tree.
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat INTEGER, lon INTEGER);
        CREATE TABLE cells (cell INTEGER PRIMARY KEY, lo INTEGER, hi INTEGER);
        -- Clustered on (a, b): "the edges leaving node a" is a contiguous
        -- range scan of the table itself, so no separate edges_a index.
        CREATE TABLE edges (a INTEGER, b INTEGER, d INTEGER,
                            PRIMARY KEY (a, b)) WITHOUT ROWID;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TEMP TABLE remap (old INTEGER PRIMARY KEY, new INTEGER,
                                 cell INTEGER);
    """)
    # Numbered in cell order so nodes that are near each other in space are
    # near each other on disk — an A* expansion touches far fewer pages.
    con.execute("""
        INSERT INTO temp.remap (old, new, cell)
        SELECT id, ROW_NUMBER() OVER (ORDER BY cell, id), cell FROM src.nodes
         WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
                (la1, la2, lo1, lo2))
    n = con.execute("SELECT count(*) FROM temp.remap").fetchone()[0]
    con.execute("""
        INSERT INTO nodes (id, lat, lon)
        SELECT r.new, CAST(round(s.lat * 1000000) AS INTEGER),
               CAST(round(s.lon * 1000000) AS INTEGER)
          FROM src.nodes s JOIN temp.remap r ON r.old = s.id""")
    con.execute("""
        INSERT INTO cells (cell, lo, hi)
        SELECT cell, min(new), max(new) FROM temp.remap GROUP BY cell""")
    # Both ends must survive the cut, or the edge leads out of the pack.
    con.execute("""
        INSERT OR IGNORE INTO edges (a, b, d)
        SELECT ra.new, rb.new, e.d FROM src.edges e
          JOIN temp.remap ra ON ra.old = e.a
          JOIN temp.remap rb ON rb.old = e.b""")
    e = con.execute("SELECT count(*) FROM edges").fetchone()[0]
    log(f"    nodes: {n:,}   edges: {e:,}")
    con.execute("INSERT INTO meta VALUES ('bbox', ?)",
                (json.dumps([la1, la2, lo1, lo2]),))
    con.execute("INSERT INTO meta VALUES ('coord_scale', '1000000')")
    con.execute("INSERT INTO meta VALUES ('cell_deg', ?)", (str(CELL_DEG),))
    # The source's cell formula is preserved, so the ported nearest-node
    # lookup buckets exactly as the server does.
    for k, v in con.execute("SELECT key, value FROM src.meta"):
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    con.commit()                      # or DETACH finds the source still busy
    con.execute("DETACH DATABASE src")
    return {"nodes": n, "edges": e}


# --------------------------------------------------------------------------
# addresses and places


def build_addresses(src_path, out, bbox):
    la1, la2, lo1, lo2 = bbox
    con = fresh_db(out)
    con.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
    con.create_function("cell_key", 2, cell_key, deterministic=True)
    con.executescript("""
        CREATE TABLE streets (id INTEGER PRIMARY KEY, name TEXT, type TEXT,
                              locality TEXT, state TEXT);
        -- Same trick as the walk graph, and for the same reason: an index
        -- on (lat, lon) over a WITHOUT ROWID table repeats the whole
        -- primary key in every entry and cost 40 MB. Numbering the rows
        -- in cell order turns a proximity search into an id-range scan.
        -- lat/lon are micro-degrees.
        CREATE TABLE addresses (id INTEGER PRIMARY KEY, street_id INTEGER,
                                num INTEGER, lat INTEGER, lon INTEGER);
        CREATE TABLE cells (cell INTEGER PRIMARY KEY, lo INTEGER, hi INTEGER);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TEMP TABLE picked (street_id INTEGER, num INTEGER, lat REAL,
                                  lon REAL, cell INTEGER);
    """)
    con.execute("""
        INSERT INTO temp.picked
        SELECT street_id, num, lat, lon, cell_key(lat, lon) FROM src.addresses
         WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
                (la1, la2, lo1, lo2))
    con.execute("""
        INSERT INTO addresses (id, street_id, num, lat, lon)
        SELECT ROW_NUMBER() OVER (ORDER BY cell, street_id, num), street_id,
               num, CAST(round(lat * 1000000) AS INTEGER),
               CAST(round(lon * 1000000) AS INTEGER)
          FROM temp.picked""")
    con.execute("""
        INSERT INTO cells (cell, lo, hi)
        SELECT p.cell, min(a.id), max(a.id) FROM temp.picked p
          JOIN addresses a ON a.street_id = p.street_id AND a.num = p.num
         GROUP BY p.cell""")
    con.execute("""
        INSERT INTO streets SELECT id, name, type, locality, state
          FROM src.streets
         WHERE id IN (SELECT DISTINCT street_id FROM addresses)""")
    a = con.execute("SELECT count(*) FROM addresses").fetchone()[0]
    s = con.execute("SELECT count(*) FROM streets").fetchone()[0]
    log(f"    addresses: {a:,}   streets: {s:,}")
    # "Number 397 on this street" — the geocoder's lookup after a name match.
    con.execute("CREATE INDEX addresses_street ON addresses (street_id, num)")
    con.execute("INSERT INTO meta VALUES ('coord_scale', '1000000')")
    con.execute("INSERT INTO meta VALUES ('cell_deg', ?)", (str(CELL_DEG),))
    con.execute("""CREATE VIRTUAL TABLE streets_fts USING fts5(
        name, type, locality, state, content='streets', content_rowid='id')""")
    con.execute("INSERT INTO streets_fts(streets_fts) VALUES('rebuild')")
    for k, v in con.execute("SELECT key, value FROM src.meta"):
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    con.commit()
    con.execute("DETACH DATABASE src")
    return {"addresses": a, "streets": s}


def build_places(src_path, out, bbox):
    la1, la2, lo1, lo2 = bbox
    con = fresh_db(out)
    con.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
    con.executescript("""
        CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT, category TEXT,
            suburb TEXT, state TEXT, alt TEXT, lat REAL, lon REAL,
            source TEXT DEFAULT 'osm');
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    con.execute("""
        INSERT INTO places SELECT id, name, category, suburb, state, alt,
               round(lat,6), round(lon,6), source FROM src.places
         WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
                (la1, la2, lo1, lo2))
    p = con.execute("SELECT count(*) FROM places").fetchone()[0]
    log(f"    places: {p:,}")
    con.execute("CREATE INDEX places_lat_lon ON places (lat, lon)")
    con.execute("""CREATE VIRTUAL TABLE places_fts USING fts5(
        name, category, suburb, state, alt, content='places',
        content_rowid='id')""")
    con.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
    for k, v in con.execute("SELECT key, value FROM src.meta"):
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    con.commit()
    con.execute("DETACH DATABASE src")
    return {"places": p}


# --------------------------------------------------------------------------


def region_bbox(db_path, pad=PAD_DEG):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    la1, la2, lo1, lo2 = con.execute(
        "SELECT min(stop_lat), max(stop_lat), min(stop_lon), max(stop_lon) "
        "FROM stops WHERE stop_lat IS NOT NULL").fetchone()
    con.close()
    return (la1 - pad, la2 + pad, lo1 - pad, lo2 + pad)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", required=True, choices=sorted(REGIONS))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--weeks", type=int, default=8,
                    help="how far ahead the timetable must stay valid")
    ap.add_argument("--data", type=Path, default=DB_PATH.parent,
                    help="where the national databases live")
    ap.add_argument("--skip", default="", help="comma-separated parts to skip")
    ap.add_argument("--dist", type=Path,
                    help="also lay the pack out flat here, as it will be "
                         "served (see publish())")
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    rid = args.region
    cfg = REGIONS[rid]
    src_gtfs = cfg["db"]
    if not src_gtfs.exists():
        sys.exit(f"no timetable for {rid}: {src_gtfs}")

    out = args.out / rid
    out.mkdir(parents=True, exist_ok=True)
    bbox = region_bbox(src_gtfs)
    log(f"== pack {rid} ({cfg['name']}) ==")
    log(f"  bbox  lat {bbox[0]:.2f}..{bbox[1]:.2f}  lon {bbox[2]:.2f}..{bbox[3]:.2f}")

    files, stats, t0 = {}, {}, time.time()

    # timetable
    if "timetable" not in skip:
        log("  building timetable…")
        t = time.time()
        p = out / "timetable.sqlite3"
        stats["timetable"] = build_timetable(src_gtfs, p, args.weeks)
        finish(sqlite3.connect(p), p, "timetable")
        log(f"    {time.time() - t:.0f}s")
        files["timetable.sqlite3"] = p

    for name, filename, builder, source in (
            ("walk", "walk.sqlite3", build_walk, args.data / "walkgraph.sqlite3"),
            ("addresses", "addresses.sqlite3", build_addresses,
             args.data / "gnaf.sqlite3"),
            ("places", "places.sqlite3", build_places,
             args.data / "places.sqlite3")):
        if name in skip:
            log(f"  {name}: skipped")
            continue
        if not source.exists():
            log(f"  {name}: no source at {source} — skipped")
            continue
        log(f"  building {name}…")
        t = time.time()
        p = out / filename
        stats[name] = builder(source, p, bbox)
        finish(sqlite3.connect(p), p, name)
        log(f"    {time.time() - t:.0f}s")
        files[filename] = p

    # basemap: already a compact, range-readable archive — just copy it.
    bm = BASEMAP_DIR / cfg["basemap"].name
    if "basemap" not in skip and bm.exists():
        log("  copying basemap…")
        p = out / "basemap.pmtiles"
        shutil.copyfile(bm, p)
        log(f"  {'basemap':12} {human(p.stat().st_size):>10}")
        files["basemap.pmtiles"] = p

    # Rebuilding only the timetable is the weekly routine, so a skipped part
    # must keep its entry rather than vanish from the manifest — otherwise
    # the app would be told the region no longer has streets.
    entries = {}
    old = out / "manifest.json"
    if old.exists():
        for name, e in json.loads(old.read_text()).get("files", {}).items():
            if name not in files and (out / name).exists():
                entries[name] = e
    for name, p in files.items():
        if p is not None:
            entries[name] = {"bytes": p.stat().st_size, "sha256": sha256(p)}

    manifest = {
        "region": rid,
        "name": cfg["name"],
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bbox": [round(v, 4) for v in bbox],
        "center": cfg["center"],
        "tz": str(cfg["tz"]),
        "stats": stats,
        "files": entries,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    total = sum(f["bytes"] for f in manifest["files"].values())
    log(f"\n  {'TOTAL':12} {human(total):>10}   in {time.time() - t0:.0f}s")
    log(f"  -> {out}")

    if args.dist:
        publish(out, args.dist, rid)


def publish(pack_dir, dist, rid):
    """Lay the pack out the way it will be served.

    A GitHub release has no directories — every asset sits in one flat
    namespace — so the served name is "<region>-<file>". Building that
    layout here rather than at upload time means a local static server
    and the real release are byte-identical as far as the app is
    concerned, and the download code has one URL rule to know.
    """
    dist.mkdir(parents=True, exist_ok=True)
    for src in sorted(pack_dir.iterdir()):
        if src.is_dir():
            continue
        dest = dist / f"{rid}-{src.name}"
        dest.unlink(missing_ok=True)
        try:
            dest.hardlink_to(src)          # same filesystem: costs nothing
        except OSError:
            shutil.copyfile(src, dest)

    # index.json: what the app reads first, to learn which regions exist
    # and which one covers where the rider is standing.
    regions = []
    for mf in sorted(dist.glob("*-manifest.json")):
        m = json.loads(mf.read_text())
        regions.append({
            "id": m["region"], "name": m["name"], "bbox": m["bbox"],
            "center": m["center"], "tz": m["tz"], "built": m["built"],
            "bytes": sum(f["bytes"] for f in m["files"].values()),
        })
    (dist / "index.json").write_text(json.dumps(
        {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "regions": regions}, indent=2))
    log(f"  published {len(regions)} region(s) -> {dist}")


if __name__ == "__main__":
    main()
