"""
Journey planning core: RAPTOR over an in-memory timetable built from one
region's GTFS SQLite. Pure routing — no HTTP, no realtime; app.py enriches
the returned legs (route names, platforms, shapes) and re-costs them with
live TripUpdates. See PLANNER.md for the full design.

The timetable is day-filtered (today's active services plus yesterday's
after-midnight tail, shifted -24 h into today's clock) and cached per
region; the cache key carries the DB file's mtime, so the weekly re-ingest
invalidates it — trip ids ROT (see DECISIONS.md), a stale plan would reference
trips realtime can't match.

All times are seconds since midnight of the service date in the region's
timezone; the caller converts to/from epochs.
"""

import math
import sqlite3
import threading
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

MAX_ROUNDS = 4            # up to 3 transfers; beyond that nobody trusts it
WALK_SPEED_MPS = 80 / 60  # 80 m/min
WALK_DETOUR = 1.3         # straight-line -> street distance fudge
WALK_RADIUS_M = 200       # footpath generation radius
MIN_TRANSFER_S = 120      # even "across the platform" costs this
STATION_TRANSFER_S = 180  # between platforms of one parent / TSN siblings
INF = float("inf")


def _secs(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _out_of_service(text) -> bool:
    return bool(text) and "out of service" in text.lower()


def _active_service_ids(con, ymd: str, weekday: int) -> set:
    col = ["monday", "tuesday", "wednesday", "thursday",
           "friday", "saturday", "sunday"][weekday]
    ids = {r[0] for r in con.execute(
        f"SELECT service_id FROM calendar WHERE {col}=1 "
        f"AND start_date<=? AND end_date>=?", (ymd, ymd))}
    for sid, ex in con.execute(
            "SELECT service_id, exception_type FROM calendar_dates WHERE date=?",
            (ymd,)):
        if int(ex) == 1:
            ids.add(sid)
        else:
            ids.discard(sid)
    return ids


class Timetable:
    """RAPTOR's view of one region-day.

    stop_ids[i]      GTFS stop_id for stop index i
    routes[r]        (stop_idx_tuple, trips) — trips sorted by first departure,
                     each trip = (trip_id, arr_tuple, dep_tuple)
    stop_routes[i]   [(route_idx, position of i in that route's stops), ...]
    foot[i]          [(stop_idx, seconds), ...]
    group_of[id]     stop_id -> parent/station group members (for src/dst
                     expansion), including the id itself
    """

    def __init__(self):
        self.stop_ids = []
        self.stop_index = {}
        self.routes = []
        self.stop_routes = defaultdict(list)
        self.foot = defaultdict(list)
        self.group_of = {}


def _haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def _build(db_path: Path, dates, tsn_family: bool) -> Timetable:
    """dates: [(ymd, weekday, offset_seconds)] — today at 0, yesterday at
    -86400 (only its after-midnight tail survives the shift filter)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    tt = Timetable()

    # --- stops (all of them: footpaths + group expansion want positions) ---
    stop_pos = {}
    parent_children = defaultdict(list)
    for r in con.execute("SELECT stop_id, stop_lat, stop_lon, parent_station "
                         "FROM stops"):
        sid = r["stop_id"]
        tt.stop_index[sid] = len(tt.stop_ids)
        tt.stop_ids.append(sid)
        if r["stop_lat"] is not None and r["stop_lon"] is not None:
            stop_pos[sid] = (r["stop_lat"], r["stop_lon"])
        if r["parent_station"]:
            parent_children[r["parent_station"]].append(sid)

    # --- trip metadata for the out-of-service filter --------------------
    oos_routes = {r["route_id"] for r in con.execute(
        "SELECT route_id FROM routes WHERE lower(coalesce(route_short_name,'')) "
        "LIKE '%out of service%' OR lower(coalesce(route_long_name,'')) "
        "LIKE '%out of service%'")}
    trip_meta = {}
    for r in con.execute("SELECT trip_id, route_id, trip_headsign FROM trips"):
        trip_meta[r["trip_id"]] = (r["route_id"], r["trip_headsign"])

    # --- per-trip stop_times, both service dates -------------------------
    by_trip = {}
    for ymd, weekday, offset in dates:
        sids = _active_service_ids(con, ymd, weekday)
        if not sids:
            continue
        marks = ",".join("?" for _ in sids)
        rows = con.execute(
            f"SELECT st.trip_id, st.stop_id, st.arrival_time, st.departure_time "
            f"FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id "
            f"WHERE t.service_id IN ({marks}) "
            f"ORDER BY st.trip_id, st.stop_sequence",
            tuple(sids))
        cur_trip, cur = None, None
        for trip_id, stop_id, arr, dep in rows:
            if trip_id != cur_trip:
                cur_trip = trip_id
                cur = by_trip.setdefault((trip_id, offset), ([], [], []))
            meta = trip_meta.get(trip_id)
            if meta and (meta[0] in oos_routes or _out_of_service(meta[1])):
                continue
            try:
                a = _secs(arr or dep) + offset
                d = _secs(dep or arr) + offset
            except (ValueError, AttributeError):
                continue
            idx = tt.stop_index.get(stop_id)
            if idx is None:
                continue
            cur[0].append(idx)
            cur[1].append(a)
            cur[2].append(d)

    # --- group into RAPTOR routes (identical stop sequences) -------------
    grouped = defaultdict(list)
    for (trip_id, offset), (stops, arrs, deps) in by_trip.items():
        if len(stops) < 2:
            continue
        if offset < 0 and arrs[-1] < 0:
            continue  # yesterday's trip finished before today's midnight
        grouped[tuple(stops)].append((deps[0], trip_id, tuple(arrs), tuple(deps)))
    for stop_seq, trips in grouped.items():
        trips.sort()
        r_idx = len(tt.routes)
        tt.routes.append((stop_seq,
                          [(tid, arrs, deps) for _, tid, arrs, deps in trips]))
        seen = set()
        for pos, s in enumerate(stop_seq):
            if s not in seen:     # loops: board at the first visit
                tt.stop_routes[s].append((r_idx, pos))
                seen.add(s)

    # --- footpaths: proximity ---------------------------------------------
    # Grid-bucket, then exact haversine within 3x3 neighbouring cells.
    CELL = 0.002  # ~220 m of latitude
    grid = defaultdict(list)
    for sid, (lat, lon) in stop_pos.items():
        grid[(int(lat / CELL), int(lon / CELL))].append(sid)
    for (gy, gx), members in grid.items():
        neighbours = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neighbours.extend(grid.get((gy + dy, gx + dx), ()))
        for sid in members:
            la, lo = stop_pos[sid]
            i = tt.stop_index[sid]
            for oid in neighbours:
                if oid <= sid:      # each unordered pair once
                    continue
                lb, lg = stop_pos[oid]
                dist = _haversine_m(la, lo, lb, lg)
                if dist <= WALK_RADIUS_M:
                    secs = max(MIN_TRANSFER_S,
                               int(dist * WALK_DETOUR / WALK_SPEED_MPS))
                    j = tt.stop_index[oid]
                    tt.foot[i].append((j, secs))
                    tt.foot[j].append((i, secs))

    # --- station groups: parent+children, and TfNSW TSN siblings ----------
    groups = defaultdict(set)
    for parent, children in parent_children.items():
        g = set(children)
        if parent in tt.stop_index:
            g.add(parent)
        key = f"p:{parent}"
        groups[key] |= g
    if tsn_family:
        by_tsn = defaultdict(set)
        for sid in tt.stop_ids:
            if ":" in sid:
                by_tsn[sid.split(":", 1)[1]].add(sid)
        for tsn, members in by_tsn.items():
            if len(members) > 1:
                groups[f"t:{tsn}"] |= members
    for members in groups.values():
        idxs = [tt.stop_index[s] for s in members if s in tt.stop_index]
        for i in idxs:
            for j in idxs:
                if i != j:
                    tt.foot[i].append((j, STATION_TRANSFER_S))
        for s in members:
            tt.group_of.setdefault(s, set()).update(members)

    con.close()
    return tt


# ---------------------------------------------------------------------------
# Cache: one timetable per region, keyed by (db mtime, today's date).
_cache: dict = {}
_lock = threading.Lock()


def get_timetable(region: str, db_path: Path, dates, tsn_family=False) -> Timetable:
    key = (region, db_path.stat().st_mtime, dates[0][0])
    with _lock:
        hit = _cache.get(region)
        if hit and hit[0] == key:
            return hit[1]
    tt = _build(db_path, dates, tsn_family)
    with _lock:
        _cache[region] = (key, tt)
        # A long-lived process crosses midnights: keep only current builds.
        for rgn in [r for r, (k, _) in _cache.items() if k[2] != dates[0][0]]:
            if rgn != region:
                _cache.pop(rgn, None)
    return tt


# ---------------------------------------------------------------------------
def _expand(tt: Timetable, stop_id: str) -> list:
    """A parent (or grouped) stop means 'any of its platforms'."""
    ids = tt.group_of.get(stop_id, {stop_id})
    return [tt.stop_index[s] for s in ids if s in tt.stop_index]


def plan(tt: Timetable, from_stop, to_stop, dep_sec: int,
         max_itineraries: int = 4, sources=None, targets=None) -> list:
    """RAPTOR: earliest arrival per transfer count. Returns up to
    max_itineraries pareto itineraries (more rides only if strictly faster),
    each a list of legs:
      {"kind": "ride", "trip_id", "board": stop_id, "alight": stop_id,
       "dep_sec", "arr_sec"}
      {"kind": "walk", "from": stop_id, "to": stop_id, "secs"}

    Origin: either from_stop (a stop id, expanded to its station group) or
    `sources` = [(stop_id, penalty_secs), ...] — an ADDRESS origin, seeded
    with each walkable stop pre-charged its walking time, so a closer stop
    with a later service competes fairly with a further one leaving sooner.
    Destination: symmetrically, to_stop or `targets` — a place/address
    destination, where each candidate alighting stop carries the walk time
    from it to the door, and arrival means arrival AT THE DOOR.
    """
    if sources is not None:
        src_pens = {}
        for sid, pen in sources:
            for i in _expand(tt, sid):
                if pen < src_pens.get(i, INF):
                    src_pens[i] = pen
    else:
        src_pens = {i: 0 for i in _expand(tt, from_stop)}
    if targets is not None:
        dst_pen = {}
        for sid, pen in targets:
            for i in _expand(tt, sid):
                if pen < dst_pen.get(i, INF):
                    dst_pen[i] = pen
    else:
        dst_pen = {i: 0 for i in _expand(tt, to_stop)}
    if not src_pens or not dst_pen:
        return []
    dst_set = set(dst_pen)

    # tau[k][stop] = earliest arrival using k rides; parent for rebuild
    tau = [dict() for _ in range(MAX_ROUNDS + 1)]
    best = {}
    parent = [dict() for _ in range(MAX_ROUNDS + 1)]

    def settle(k, stop, t, via):
        if t < best.get(stop, INF) and t < tau[k].get(stop, INF):
            tau[k][stop] = t
            best[stop] = t
            parent[k][stop] = via
            return True
        return False

    marked = set()
    for s, pen in src_pens.items():
        t0 = dep_sec + pen
        if t0 < tau[0].get(s, INF):
            tau[0][s] = t0
            best[s] = t0
            marked.add(s)
    # walking straight from the origin counts as round 0
    for s in list(marked):
        for v, w in tt.foot.get(s, ()):
            if settle(0, v, tau[0][s] + w, ("walk", s, 0, w)):
                marked.add(v)

    for k in range(1, MAX_ROUNDS + 1):
        # routes serving any improved stop
        queue = {}
        for s in marked:
            for r_idx, pos in tt.stop_routes.get(s, ()):
                if r_idx not in queue or pos < queue[r_idx]:
                    queue[r_idx] = pos
        marked = set()

        for r_idx, start_pos in queue.items():
            stop_seq, trips = tt.routes[r_idx]
            trip = None          # (tid, arrs, deps)
            board_pos = None
            for pos in range(start_pos, len(stop_seq)):
                s = stop_seq[pos]
                if trip is not None:
                    arr = trip[1][pos]
                    if arr < best.get(s, INF):
                        if settle(k, s, arr,
                                  ("ride", trip[0], stop_seq[board_pos],
                                   trip[2][board_pos], pos, board_pos, r_idx)):
                            marked.add(s)
                # can we catch an earlier (or first) trip here?
                t_prev = tau[k - 1].get(s)
                if t_prev is not None and pos < len(stop_seq) - 1:
                    if trip is None or t_prev < trip[2][pos]:
                        lo, hi = 0, len(trips)
                        while lo < hi:
                            mid = (lo + hi) // 2
                            if trips[mid][2][pos] < t_prev:
                                lo = mid + 1
                            else:
                                hi = mid
                        if lo < len(trips) and (
                                trip is None or trips[lo][2][pos] < trip[2][pos]):
                            trip = trips[lo]
                            board_pos = pos
                        elif (lo < len(trips) and trip is not None
                              and trips[lo][0] == trip[0]):
                            # The SAME trip is also catchable here. Board at
                            # whichever stop leaves the latest set-off — most
                            # slack between reaching the stop and the bus
                            # going — instead of the first stop scanned,
                            # which rode people past their nearest stop.
                            t_b = tau[k - 1].get(stop_seq[board_pos])
                            if (t_b is not None
                                    and trip[2][pos] - t_prev
                                        > trip[2][board_pos] - t_b):
                                board_pos = pos

        # footpath relaxation within the round
        for s in list(marked):
            t = tau[k][s]
            for v, w in tt.foot.get(s, ()):
                if settle(k, v, t + w, ("walk", s, k, w)):
                    marked.add(v)
        if not marked:
            break

    # --- pareto reconstruction -------------------------------------------
    def rebuild(kk, stop):
        """Walk the parent chain from (round, dest stop) back to a seed.
        Returns (legs, set_off) — set_off is when you must leave the door
        to make it — or (None, None) on a broken chain."""
        legs = []
        guard = 0
        while True:
            guard += 1
            if guard > 50:
                return None, None
            via = parent[kk].get(stop)
            if via is None:
                break               # reached a source stop
            if via[0] == "walk":
                _, prev, kw, w = via
                legs.append({"kind": "walk", "from": tt.stop_ids[prev],
                             "to": tt.stop_ids[stop], "secs": w})
                stop, kk = prev, kw
            else:
                _, tid, board_stop_idx, dep_s, pos, board_pos, r_idx = via
                arr_s = None
                for trip in tt.routes[r_idx][1]:
                    if trip[0] == tid:
                        arr_s = trip[1][pos]
                        break
                legs.append({"kind": "ride", "trip_id": tid,
                             "board": tt.stop_ids[board_stop_idx],
                             "alight": tt.stop_ids[stop],
                             "dep_sec": dep_s, "arr_sec": arr_s})
                stop, kk = board_stop_idx, kk - 1
        if not legs:
            return None, None
        legs.reverse()
        # When you must leave the door: the first ride's departure, minus
        # every walk before it (footpath legs and the seed stop's own
        # walk-from-the-door penalty).
        set_off = dep_sec
        for l in legs:
            if l["kind"] == "ride":
                set_off = l["dep_sec"]
                break
        lead = src_pens.get(stop, 0)
        for l in legs:
            if l["kind"] == "ride":
                break
            lead += l["secs"]
        return legs, set_off - lead

    out = []
    seen_arr = INF
    for k in range(1, MAX_ROUNDS + 1):
        # Arrival means arrival at the DOOR: the walk from each candidate
        # alighting stop weighs into which one wins.
        arr = min((tau[k].get(d, INF) + dst_pen[d] for d in dst_set),
                  default=INF)
        if arr >= seen_arr or arr == INF:
            continue
        seen_arr = arr
        # Latest-set-off tie-break: several alighting stops can reach the
        # door at the same time — keep the journey you can leave for
        # LATEST (shortest door-to-door), then the shortest final walk.
        best_cand = None
        for d in dst_set:
            if tau[k].get(d, INF) + dst_pen[d] != arr:
                continue
            legs, set_off = rebuild(k, d)
            if legs is None:
                continue
            rank = (set_off, -dst_pen[d])
            if best_cand is None or rank > best_cand[0]:
                best_cand = (rank, legs)
        if best_cand:
            legs = best_cand[1]
            out.append({"legs": legs, "arr_sec": arr,
                        "dep_sec": next((l["dep_sec"] for l in legs
                                         if l["kind"] == "ride"), dep_sec),
                        "rides": sum(1 for l in legs if l["kind"] == "ride")})
        if len(out) >= max_itineraries:
            break
    return out
