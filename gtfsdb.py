# reading the static timetable: services, departures, ghosts
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import math
import re
import sqlite3
from datetime import datetime, timedelta

from fastapi import HTTPException

from regions import (AGENCY_TZ, LOOKAHEAD_MINUTES, MAX_RESULTS,
                     STAGING_WINDOW_S, region_cfg)



# ---------------------------------------------------------------------------
# Static GTFS helpers
# ---------------------------------------------------------------------------


def db(region: str = "seq") -> sqlite3.Connection:
    cfg = region_cfg(region)
    if not cfg["db"].exists():
        raise HTTPException(
            500, f"{cfg['db'].name} not found - run ingest_gtfs.py --region {region}")
    con = sqlite3.connect(cfg["db"])
    con.row_factory = sqlite3.Row
    return con


def active_service_ids(con: sqlite3.Connection, service_date: datetime) -> set[str]:
    """Service IDs running on a given service date (calendar + exceptions)."""
    ymd = service_date.strftime("%Y%m%d")
    weekday_col = ["monday", "tuesday", "wednesday", "thursday",
                   "friday", "saturday", "sunday"][service_date.weekday()]
    ids = {
        r["service_id"]
        for r in con.execute(
            f"SELECT service_id FROM calendar "
            f"WHERE {weekday_col}=1 AND start_date<=? AND end_date>=?",
            (ymd, ymd),
        )
    }
    for r in con.execute(
        "SELECT service_id, exception_type FROM calendar_dates WHERE date=?", (ymd,)
    ):
        if int(r["exception_type"]) == 1:
            ids.add(r["service_id"])
        else:
            ids.discard(r["service_id"])
    return ids


def gtfs_time_to_epoch(hms: str, service_date: datetime, tz=AGENCY_TZ) -> int:
    """GTFS times can exceed 24:00:00 for after-midnight trips.

    The offset is applied to midnight *in the agency's timezone*: a naive
    datetime would be interpreted in the host's zone, so a UTC container would
    read every scheduled time 10 hours late. Each region passes its own zone
    (Melbourne has DST; Brisbane does not).
    """
    h, m, s = (int(x) for x in hms.split(":"))
    midnight = service_date.astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight + timedelta(hours=h, minutes=m, seconds=s)).timestamp())


def scheduled_departures(con, stop_ids: list[str], service_date: datetime,
                         tz=AGENCY_TZ) -> list[dict]:
    sids = active_service_ids(con, service_date)
    if not sids or not stop_ids:
        return []
    stop_marks = ",".join("?" for _ in stop_ids)
    svc_marks = ",".join("?" for _ in sids)
    rows = con.execute(
        f"""
        SELECT st.trip_id, st.departure_time, st.stop_id, st.stop_sequence,
               t.trip_headsign,
               r.route_id, r.route_short_name, r.route_long_name, r.route_type,
               r.route_color,
               s.platform_code, s.stop_name AS platform_stop_name
        FROM stop_times st
        JOIN trips t  ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        JOIN stops s  ON s.stop_id = st.stop_id
        WHERE st.stop_id IN ({stop_marks}) AND t.service_id IN ({svc_marks})
        """,
        (*stop_ids, *sids),
    ).fetchall()
    out = []
    for r in rows:
        # TfNSW timetables non-revenue runs as ordinary trips on routes / with
        # headsigns literally named "Out Of Service" — a passenger board must
        # not offer them.
        if _out_of_service(r["trip_headsign"]) or \
           _out_of_service(r["route_short_name"]) or \
           _out_of_service(r["route_long_name"]):
            continue
        out.append(
            {
                "trip_id": r["trip_id"],
                "stop_id": r["stop_id"],
                "stop_sequence": r["stop_sequence"],
                "scheduled": gtfs_time_to_epoch(r["departure_time"], service_date, tz),
                "headsign": r["trip_headsign"],
                "route_id": r["route_id"],
                "route": r["route_short_name"] or r["route_long_name"],
                "route_type": r["route_type"],
                "route_color": r["route_color"],
                "platform": platform_label(r["platform_code"], r["platform_stop_name"]),
            }
        )
    return out


def _out_of_service(text: str | None) -> bool:
    return bool(text) and "out of service" in text.lower()


PLATFORM_RE = re.compile(r"platform\s+(\w+)", re.IGNORECASE)


def platform_label(platform_code: str | None, stop_name: str | None) -> str | None:
    if platform_code:
        return platform_code
    if stop_name:
        m = PLATFORM_RE.search(stop_name)
        if m:
            return m.group(1)
    return None


def _seconds_into_day(hms: str) -> int:
    """GTFS clock string to seconds past the service day's midnight. Values can
    exceed 24h for after-midnight trips; kept as an offset so it composes with a
    single midnight anchor and stays monotonic across the 24:00 boundary."""
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def _interpolate_along(
    nodes: list[tuple[int, float, float]], now: int, stage_window: int = 0
):
    """nodes = [(epoch, lat, lon), ...] in schedule order. Return the point the
    timetable places the vehicle at `now`, linearly interpolated between the two
    stops that bracket it — or None when the trip is not on the road.

    Before the origin departure the trip has no en-route position. It is drawn at
    its origin only if it leaves within `stage_window` seconds ("staging to
    start"); earlier than that it gets no marker, so a run scheduled to start in
    40 minutes does not pile a phantom bus on the route start. After the final
    stop the trip has finished and returns None."""
    first = nodes[0][0]
    if now < first:
        return {"lat": nodes[0][1], "lon": nodes[0][2]} if first - now <= stage_window else None
    if now >= nodes[-1][0]:
        return None
    for (t0, a0, o0), (t1, a1, o1) in zip(nodes, nodes[1:]):
        if t0 <= now < t1:
            f = (now - t0) / (t1 - t0) if t1 > t0 else 0.0
            return {"lat": a0 + (a1 - a0) * f, "lon": o0 + (o1 - o0) * f}
    return None


def estimate_ghost_positions(con, deps: list[dict], now_epoch: int) -> dict:
    """Where the timetable *says* each trip should be right now — for trips with
    no live GPS. Interpolates along the trip's scheduled stops, anchored to the
    board departure we already resolved (so the service date, incl. after-
    midnight runs, is correct without re-deriving it). Returns {trip_id: {lat,
    lon}}. An estimate, not a fix: it assumes the service is running to time."""
    if not deps:
        return {}
    trip_ids = [d["trip_id"] for d in deps]
    marks = ",".join("?" for _ in trip_ids)
    rows = con.execute(
        f"""
        SELECT st.trip_id, st.stop_id, st.stop_sequence,
               st.departure_time, st.arrival_time, s.stop_lat, s.stop_lon
        FROM stop_times st
        JOIN stops s ON s.stop_id = st.stop_id
        WHERE st.trip_id IN ({marks})
          AND s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
        ORDER BY st.trip_id, st.stop_sequence
        """,
        trip_ids,
    ).fetchall()

    by_trip: dict = {}
    for r in rows:
        by_trip.setdefault(r["trip_id"], []).append(r)

    anchor = {d["trip_id"]: d for d in deps}
    out: dict = {}
    for tid, strows in by_trip.items():
        d = anchor[tid]
        # Anchor the whole trip's clock to real epochs using the one stop whose
        # epoch we already know: midnight = board_scheduled - board_offset.
        board = next((r for r in strows if r["stop_id"] == d["stop_id"]), None)
        board_hms = board and (board["departure_time"] or board["arrival_time"])
        if not board_hms:
            continue
        midnight = d["scheduled"] - _seconds_into_day(board_hms)

        nodes = []
        for r in strows:
            hms = r["departure_time"] or r["arrival_time"]
            if not hms:
                continue
            nodes.append((midnight + _seconds_into_day(hms), r["stop_lat"], r["stop_lon"]))
        if len(nodes) < 2:
            continue
        pos = _interpolate_along(nodes, now_epoch, STAGING_WINDOW_S)
        if pos:
            out[tid] = pos
    return out
