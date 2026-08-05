# the walking graph: routes along real streets, and their names
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import math
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from regions import DB_PATH
from geocoding import GNAF_DB

router = APIRouter()


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


@router.post("/api/streets")
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


@router.get("/api/walkroute")
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
