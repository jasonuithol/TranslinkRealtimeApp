"""Check that a region pack answers the same questions the server does.

build_pack.py does not just copy rows — it interns ids, rewrites times as
integers, stores coordinates as micro-degrees and replaces two spatial
indexes with cell-range tables. Every one of those is a chance to be
smaller and wrong. This re-asks the questions the app actually asks, of
both the pack and the national databases, and compares the answers.

    podman run --rm --user root \\
      -v translink-data:/data -v "$PWD:/repo:ro" -v "$PWD/packs:/packs:z" \\
      localhost/translink-dev:latest \\
      python /repo/verify_pack.py --region seq --pack /packs/seq

Exit code 0 = the pack agrees with the source on every check.
"""
import argparse
import math
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_pack import CELL_DEG, cell_key, secs                # noqa: E402
from regions import REGIONS, DB_PATH                           # noqa: E402

SCALE = 1_000_000
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'  ' + detail if detail else ''}",
          flush=True)
    if not ok:
        FAILS.append(name)


def ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------


def check_cell_ranges(con, table, label):
    """The whole point of dropping the spatial index is that a cell's rows
    are a contiguous id range. If numbering and grouping ever disagree,
    proximity search silently returns the wrong neighbourhood."""
    total = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    covered = con.execute("SELECT sum(hi - lo + 1) FROM cells").fetchone()[0]
    check(f"{label}: cell ranges cover every row",
          total == covered, f"{covered:,} of {total:,}")
    # Ranges must not overlap: sorted by lo, each lo must exceed the last hi.
    bad = con.execute("""
        SELECT count(*) FROM (
          SELECT lo, hi, LAG(hi) OVER (ORDER BY lo) prev FROM cells)
         WHERE prev IS NOT NULL AND lo <= prev""").fetchone()[0]
    check(f"{label}: cell ranges do not overlap", bad == 0, f"{bad} overlaps")


def check_walk(pack, src, samples=40):
    con, s = ro(pack / "walk.sqlite3"), ro(src)
    check_cell_ranges(con, "nodes", "walk")

    # Nearest-node, the pack's way (cell -> id range) against the source's
    # way (indexed cell lookup). Same node, or the pack is snapping walks
    # to the wrong street corner.
    rows = con.execute(
        "SELECT lat, lon FROM nodes ORDER BY id LIMIT ? OFFSET 5000",
        (samples,)).fetchall()
    agree = 0
    for r in rows:
        lat, lon = r["lat"] / SCALE, r["lon"] / SCALE
        base = cell_key(lat, lon)
        cells = [base + dy * 40000 + dx
                 for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
        marks = ",".join("?" * len(cells))
        pk = con.execute(
            f"SELECT n.lat, n.lon FROM cells c JOIN nodes n "
            f"ON n.id BETWEEN c.lo AND c.hi WHERE c.cell IN ({marks})",
            cells).fetchall()
        sr = s.execute(
            f"SELECT lat, lon FROM nodes WHERE cell IN ({marks})",
            cells).fetchall()
        coslat = math.cos(math.radians(lat)) ** 2
        def near(cands, scale):
            return min(((c["lat"] / scale - lat) ** 2
                        + (c["lon"] / scale - lon) ** 2 * coslat)
                       for c in cands) if cands else None
        a, b = near(pk, SCALE), near(sr, 1)
        if a is not None and b is not None and abs(a - b) < 1e-12:
            agree += 1
    check("walk: nearest-node matches the national graph",
          agree == len(rows), f"{agree}/{len(rows)}")

    # Every edge must land on a node that exists, or A* walks off the map.
    dangling = con.execute("""
        SELECT count(*) FROM edges e
         WHERE NOT EXISTS (SELECT 1 FROM nodes WHERE id = e.a)
            OR NOT EXISTS (SELECT 1 FROM nodes WHERE id = e.b)""").fetchone()[0]
    check("walk: no edge points at a missing node", dangling == 0,
          f"{dangling} dangling")
    con.close(); s.close()


def check_addresses(pack, src, samples=30):
    con, s = ro(pack / "addresses.sqlite3"), ro(src)
    check_cell_ranges(con, "addresses", "addresses")

    # A sampled address must sit where G-NAF says it sits, to the 11 cm
    # the micro-degree rounding allows.
    rows = con.execute("""
        SELECT a.street_id, a.num, a.lat, a.lon, s.name, s.type, s.locality
          FROM addresses a JOIN streets s ON s.id = a.street_id
         ORDER BY a.id LIMIT ? OFFSET 700000""", (samples,)).fetchall()
    worst, missing = 0.0, 0
    for r in rows:
        src_row = s.execute(
            "SELECT lat, lon FROM addresses WHERE street_id=? AND num=?",
            (r["street_id"], r["num"])).fetchone()
        if not src_row:
            missing += 1
            continue
        worst = max(worst,
                    abs(src_row["lat"] - r["lat"] / SCALE) * 111_320,
                    abs(src_row["lon"] - r["lon"] / SCALE) * 111_320)
    check("addresses: coordinates match G-NAF", missing == 0 and worst < 0.2,
          f"worst {worst * 100:.1f} cm, {missing} missing")

    # The street name index has to still find a street by name.
    hit = con.execute(
        "SELECT count(*) FROM streets_fts WHERE streets_fts MATCH ?",
        ("christine",)).fetchone()[0]
    check("addresses: street search returns hits", hit > 0, f"{hit} streets")

    # ...and the by-street-and-number lookup the geocoder finishes with.
    st = con.execute("""SELECT id, name FROM streets
                         WHERE name = 'CHRISTINE' LIMIT 1""").fetchone()
    if st:
        n = con.execute("SELECT count(*) FROM addresses WHERE street_id=?",
                        (st["id"],)).fetchone()[0]
        check("addresses: numbers hang off their street", n > 0, f"{n} numbers")
    con.close(); s.close()


def check_timetable(pack, src, samples=25):
    con, s = ro(pack / "timetable.sqlite3"), ro(src)

    # Interning must be lossless: a sampled trip's calls, in order, with
    # the same stops at the same times as the feed.
    trips = con.execute(
        "SELECT trip, trip_id FROM trips ORDER BY trip LIMIT ? OFFSET 400",
        (samples,)).fetchall()
    mismatch = []
    for t in trips:
        packed = con.execute("""
            SELECT s.stop_id, st.arr, st.dep FROM stop_times st
              JOIN stops s ON s.stop = st.stop
             WHERE st.trip = ? ORDER BY st.seq""", (t["trip"],)).fetchall()
        orig = s.execute("""
            SELECT stop_id, arrival_time, departure_time FROM stop_times
             WHERE trip_id = ? ORDER BY stop_sequence""",
            (t["trip_id"],)).fetchall()
        if len(packed) != len(orig):
            mismatch.append(f"{t['trip_id']}: {len(packed)} vs {len(orig)} calls")
            continue
        for p, o in zip(packed, orig):
            if (p["stop_id"] != o["stop_id"]
                    or p["arr"] != secs(o["arrival_time"])
                    or p["dep"] != secs(o["departure_time"])):
                mismatch.append(f"{t['trip_id']} at {o['stop_id']}")
                break
    check("timetable: trips keep their calls, stops and times",
          not mismatch, "; ".join(mismatch[:2]))

    # Every stop_times row must resolve through both interning tables.
    orphan_t = con.execute("""SELECT count(*) FROM stop_times st WHERE NOT EXISTS
        (SELECT 1 FROM trips WHERE trip = st.trip)""").fetchone()[0]
    orphan_s = con.execute("""SELECT count(*) FROM stop_times st WHERE NOT EXISTS
        (SELECT 1 FROM stops WHERE stop = st.stop)""").fetchone()[0]
    check("timetable: no orphaned trip or stop references",
          orphan_t == 0 and orphan_s == 0, f"{orphan_t} trips, {orphan_s} stops")

    # Times past midnight must stay past midnight, not wrap to morning.
    late = con.execute(
        "SELECT count(*) FROM stop_times WHERE dep >= 86400").fetchone()[0]
    check("timetable: after-midnight services stay above 24:00",
          late > 0, f"{late:,} calls")

    # A shape must still be drawable, and still resemble the original line.
    sh = con.execute("""SELECT s.shape, i.shape_id FROM shapes s
          JOIN shape_ids i ON i.shape = s.shape GROUP BY s.shape
         HAVING count(*) > 50 LIMIT 1""").fetchone()
    if sh:
        pts = con.execute("SELECT lat, lon FROM shapes WHERE shape=? ORDER BY seq",
                          (sh["shape"],)).fetchall()
        orig = s.execute("""SELECT shape_pt_lat, shape_pt_lon FROM shapes
             WHERE shape_id=? ORDER BY shape_pt_sequence""",
            (sh["shape_id"],)).fetchall()
        # Endpoints are never dropped by Douglas-Peucker.
        ends_ok = (abs(pts[0]["lat"] - orig[0][0]) < 1e-4
                   and abs(pts[-1]["lat"] - orig[-1][0]) < 1e-4)
        check("timetable: simplified shapes keep their endpoints", ends_ok,
              f"{len(orig)} -> {len(pts)} points")

    # The board's hot query must actually use the index we kept it for.
    plan = " ".join(r[3] for r in con.execute("""
        EXPLAIN QUERY PLAN SELECT * FROM stop_times
         WHERE stop = 1 AND dep > 30000 ORDER BY dep LIMIT 20"""))
    check("timetable: the board's query uses stop_times_stop",
          "stop_times_stop" in plan, plan[:70])
    con.close(); s.close()


def check_places(pack):
    con = ro(pack / "places.sqlite3")
    n = con.execute("SELECT count(*) FROM places").fetchone()[0]
    hit = con.execute("SELECT count(*) FROM places_fts WHERE places_fts MATCH ?",
                      ("officeworks",)).fetchone()[0]
    check("places: search index survived the rebuild", n > 0 and hit > 0,
          f"{n:,} places, {hit} matching 'officeworks'")
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", required=True, choices=sorted(REGIONS))
    ap.add_argument("--pack", required=True, type=Path)
    ap.add_argument("--data", type=Path, default=DB_PATH.parent)
    args = ap.parse_args()
    random.seed(7)

    print(f"== verify {args.pack} against {args.data} ==")
    if (args.pack / "timetable.sqlite3").exists():
        check_timetable(args.pack, REGIONS[args.region]["db"])
    if (args.pack / "walk.sqlite3").exists():
        check_walk(args.pack, args.data / "walkgraph.sqlite3")
    if (args.pack / "addresses.sqlite3").exists():
        check_addresses(args.pack, args.data / "gnaf.sqlite3")
    if (args.pack / "places.sqlite3").exists():
        check_places(args.pack)

    print()
    if FAILS:
        print(f"  {len(FAILS)} CHECK(S) FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("  pack agrees with the source on every check")


if __name__ == "__main__":
    main()
