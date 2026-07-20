"""
"Next service arriving" backend for Translink SEQ.

- Polls the GTFS-RT TripUpdates feed every POLL_SECONDS in the background.
- /api/departures/{stop_id} merges today's scheduled departures (from the
  static GTFS in gtfs.sqlite3, built by ingest_gtfs.py) with realtime
  predictions, and returns clean JSON for the frontend.
- /api/stops/search?q=... finds stop IDs by name.

Run:  uvicorn app:app --reload
"""

import asyncio
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.transit import gtfs_realtime_pb2

BASE = Path(__file__).parent
DB_PATH = BASE / "gtfs.sqlite3"
TRIP_UPDATES_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
POLL_SECONDS = 30
LOOKAHEAD_MINUTES = 90
MAX_RESULTS = 12

# ---------------------------------------------------------------------------
# Realtime cache: {trip_id: {stop_id: {"arrival": epoch|None, "delay": s|None}}}
# ---------------------------------------------------------------------------
rt_cache: dict = {}
rt_last_fetch: float | None = None


async def poll_trip_updates() -> None:
    global rt_cache, rt_last_fetch
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(TRIP_UPDATES_URL)
                resp.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(resp.content)

                cache: dict = {}
                for entity in feed.entity:
                    if not entity.HasField("trip_update"):
                        continue
                    tu = entity.trip_update
                    trip_id = tu.trip.trip_id
                    stops: dict = {}
                    for stu in tu.stop_time_update:
                        rec: dict = {"arrival": None, "delay": None}
                        ev = None
                        if stu.HasField("arrival"):
                            ev = stu.arrival
                        elif stu.HasField("departure"):
                            ev = stu.departure
                        if ev is not None:
                            if ev.HasField("time") and ev.time:
                                rec["arrival"] = ev.time
                            if ev.HasField("delay"):
                                rec["delay"] = ev.delay
                        rec["skipped"] = (
                            stu.schedule_relationship
                            == stu.ScheduleRelationship.SKIPPED
                        )
                        stops[stu.stop_id] = rec
                    cache[trip_id] = stops
                rt_cache = cache
                rt_last_fetch = time.time()
            except Exception as exc:  # keep serving scheduled times on failure
                print(f"[poll] realtime fetch failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_trip_updates())
    yield
    task.cancel()


app = FastAPI(title="Translink Next Service", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static GTFS helpers
# ---------------------------------------------------------------------------


def db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(500, "gtfs.sqlite3 not found - run ingest_gtfs.py first")
    con = sqlite3.connect(DB_PATH)
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


def gtfs_time_to_epoch(hms: str, service_date: datetime) -> int:
    """GTFS times can exceed 24:00:00 for after-midnight trips."""
    h, m, s = (int(x) for x in hms.split(":"))
    return int(
        (
            service_date.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(hours=h, minutes=m, seconds=s)
        ).timestamp()
    )


def scheduled_departures(con, stop_ids: list[str], service_date: datetime) -> list[dict]:
    sids = active_service_ids(con, service_date)
    if not sids or not stop_ids:
        return []
    stop_marks = ",".join("?" for _ in stop_ids)
    svc_marks = ",".join("?" for _ in sids)
    rows = con.execute(
        f"""
        SELECT st.trip_id, st.departure_time, st.stop_id, t.trip_headsign,
               r.route_short_name, r.route_long_name, r.route_type, r.route_color,
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
        out.append(
            {
                "trip_id": r["trip_id"],
                "stop_id": r["stop_id"],
                "scheduled": gtfs_time_to_epoch(r["departure_time"], service_date),
                "headsign": r["trip_headsign"],
                "route": r["route_short_name"] or r["route_long_name"],
                "route_type": r["route_type"],
                "route_color": r["route_color"],
                "platform": platform_label(r["platform_code"], r["platform_stop_name"]),
            }
        )
    return out


PLATFORM_RE = re.compile(r"platform\s+(\w+)", re.IGNORECASE)


def platform_label(platform_code: str | None, stop_name: str | None) -> str | None:
    if platform_code:
        return platform_code
    if stop_name:
        m = PLATFORM_RE.search(stop_name)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/stops/search")
def search_stops(q: str):
    """Match stops by name. Parent stations rank first; their individual
    platforms are hidden so a station appears once (select the station to
    see all platforms combined)."""
    con = db()
    rows = con.execute(
        """
        SELECT stop_id, stop_name, location_type
        FROM stops
        WHERE stop_name LIKE ?
          AND (parent_station IS NULL OR parent_station = '')
        ORDER BY (location_type = 1) DESC, stop_name
        LIMIT 25
        """,
        (f"%{q}%",),
    ).fetchall()
    con.close()
    return [
        {
            "stop_id": r["stop_id"],
            "stop_name": r["stop_name"],
            "is_station": r["location_type"] == 1,
        }
        for r in rows
    ]


@app.get("/api/departures/{stop_id}")
def departures(stop_id: str):
    con = db()
    stop = con.execute(
        "SELECT stop_id, stop_name, location_type FROM stops WHERE stop_id=?",
        (stop_id,),
    ).fetchone()
    if stop is None:
        con.close()
        raise HTTPException(404, f"Unknown stop_id {stop_id}")

    # A parent station (e.g. a train station) has no stop_times of its own;
    # departures live on its child platform stops, so query all of them.
    stop_ids = [stop_id]
    children = [
        r["stop_id"]
        for r in con.execute(
            "SELECT stop_id FROM stops WHERE parent_station=?", (stop_id,)
        )
    ]
    stop_ids.extend(children)

    now = datetime.now()
    # include yesterday's service date to catch after-midnight (25:xx) trips
    sched = scheduled_departures(con, stop_ids, now) + scheduled_departures(
        con, stop_ids, now - timedelta(days=1)
    )
    con.close()

    now_epoch = int(now.timestamp())
    horizon = now_epoch + LOOKAHEAD_MINUTES * 60
    results = []
    for dep in sched:
        rt = rt_cache.get(dep["trip_id"], {}).get(dep["stop_id"])
        realtime = False
        best = dep["scheduled"]
        if rt:
            if rt.get("skipped"):
                continue
            if rt.get("arrival"):
                best, realtime = rt["arrival"], True
            elif rt.get("delay") is not None:
                best, realtime = dep["scheduled"] + rt["delay"], True
        if now_epoch - 60 <= best <= horizon:
            results.append(
                {
                    **dep,
                    "predicted": best,
                    "minutes": max(0, round((best - now_epoch) / 60)),
                    "realtime": realtime,
                }
            )

    results.sort(key=lambda d: d["predicted"])
    return {
        "stop": dict(stop),
        "generated_at": now_epoch,
        "realtime_feed_age": (
            round(time.time() - rt_last_fetch) if rt_last_fetch else None
        ),
        "departures": results[:MAX_RESULTS],
    }


# Frontend
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
