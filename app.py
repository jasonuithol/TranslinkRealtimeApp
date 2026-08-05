"""
"Next Service": a live departures board and journey planner for eight
Australian transit networks.

This module owns the FastAPI application itself — its lifecycle, its
middleware, and the routers it is assembled from. The work lives next
door, one module per part of the job:

    regions.py    the networks: their databases, feeds, timezones, keys
    realtime.py   the GTFS-RT pollers and the caches they fill
    gtfsdb.py     reading the static timetable, and dead-reckoning ghosts
    boards.py     stop search, sibling stations, the arrivals board, trips
    geocoding.py  text -> place (G-NAF, places, Nominatim) and back again
    walking.py    the walking graph: routes along streets, and their names
    planning.py   the journey planner endpoint
    planner.py    the RAPTOR core, which knows nothing of HTTP

Run:  uvicorn app:app --reload
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import planner as journey
from regions import (BASE, BASEMAP_DIR, REGIONS, STATE, TSN_REGIONS,
                     available_regions, region_cfg)
from realtime import poll_alerts, poll_trip_updates, poll_vehicle_positions
from boards import all_stops
from planning import _service_dates
import boards, geocoding, planning, walking


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One poller per region per configured feed kind. A region with no realtime
    # (static-only Melbourne, say) simply gets no tasks — the board and the
    # timetable-estimated ghosts work regardless.
    tasks = []
    warm_rids = []
    for rid, cfg in REGIONS.items():
        if not cfg["db"].exists():
            continue
        if cfg["trip_updates"]:
            tasks.append(asyncio.create_task(poll_trip_updates(rid)))
        if cfg["vehicle_positions"]:
            tasks.append(asyncio.create_task(poll_vehicle_positions(rid)))
        if cfg["alerts"]:
            tasks.append(asyncio.create_task(poll_alerts(rid)))
        warm_rids.append(rid)
    # Warm the all-stops caches off the request path: the dominant-mode
    # GROUP BY takes ~15 s on Melbourne's 11.6 M stop_times, which is a bad
    # thing to hang the first zoomed-in map view on. ONE thread, regions in
    # sequence — three concurrent warms starved the event loop on the small
    # VPS for minutes and the deploy health check read it as "never came up".
    # ...and then the planner's timetable, on the same one thread. A cold
    # container makes the first journey request pay for the whole RAPTOR
    # build (seq ~4 s, mel ~13 s) — the container restarts on every deploy,
    # so that bill lands on the first person to drop a destination pin.
    # Only the regions named in PLANNER_WARM are built: each timetable is
    # tens of MB, and warming all nine would cost more RAM than the VPS
    # should spend on cities nobody is looking at.
    warm_planner = [r.strip() for r in
                    os.environ.get("PLANNER_WARM", "seq").split(",") if r.strip()]

    def _warm_all(rids=tuple(warm_rids), plan_rids=tuple(warm_planner)):
        # Order matters more than completeness. The map is on screen at
        # once (so the home region's stops come first), then the planner —
        # the click that otherwise blocks a rider for ~9 s — and only then
        # the other cities' stop caches, which nobody is looking at yet.
        home = plan_rids[0] if plan_rids else None
        if home in rids:
            all_stops(home)
        for r in plan_rids:
            cfg = REGIONS.get(r)
            if not cfg or not cfg["db"].exists():
                continue
            try:
                t0 = time.time()
                journey.get_timetable(r, cfg["db"], _service_dates(cfg["tz"]),
                                      tsn_family=(r in TSN_REGIONS))
                print(f"[warm] {r} planner timetable ready "
                      f"in {time.time() - t0:.1f}s")
            except Exception as exc:      # a cold cache is not fatal
                print(f"[warm] {r} planner timetable failed: {exc}")
        for r in rids:
            if r != home:
                all_stops(r)
    tasks.append(asyncio.create_task(asyncio.to_thread(_warm_all)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Translink Next Service", lifespan=lifespan)

# The packaged phone app (Transport Honker) runs from its own local origin
# and calls this API cross-origin. Everything here is a public read-only
# transit endpoint, so a permissive policy costs nothing; there are no
# cookies or credentials to protect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_unhashed_assets(request, call_next):
    """The page, its stylesheet and the fonts have no content hash in their
    names, so StaticFiles' bare ETag/Last-Modified lets browsers cache them
    heuristically and miss an update — most sharply a font subset change, which
    silently drops a newly-added glyph back to the colour-emoji font. `no-cache`
    forces a conditional request each load (a cheap 304 while unchanged), so the
    whole chain — index.html -> app.css/js -> fonts.css -> the woff2 — is picked
    up on the next reload. The app's own scripts under /static/js are part of
    that chain (they were index.html until they were split out). The basemap
    and vendored JS are left cacheable: large and stable."""
    response = await call_next(request)
    path = request.url.path
    if (path == "/" or path.endswith((".css", ".woff2", ".json"))
            or path.startswith("/static/js/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# The API is assembled from the routers each module exposes; every path is
# unchanged, including the bare /api/... aliases for the default region.
app.include_router(boards.router)
app.include_router(geocoding.router)
app.include_router(walking.router)
app.include_router(planning.router)

@app.get("/api/regions")
def regions():
    """The regions this deployment can serve (timetable ingested), for the
    frontend's region switcher. The first entry is the default."""
    # center: [lon, lat] — the frontend's pan-across-regions check measures
    # the map centre against each region's home city to swap providers.
    return [
        {"id": rid, "name": REGIONS[rid]["name"], "state": REGIONS[rid]["state"],
         "center": REGIONS[rid]["center"]}
        for rid in available_regions()
    ] or [{"id": "seq", "name": REGIONS["seq"]["name"],
           "state": REGIONS["seq"]["state"], "center": REGIONS["seq"]["center"]}]


@app.get("/api/r/{region}/config")
@app.get("/api/config")
def config(region: str = "seq"):
    """Per-region frontend config: whether a basemap is present (without one
    the map hides and the board works unchanged), where it is, and where the
    map should sit before a stop is chosen."""
    cfg = region_cfg(region)
    return {
        "basemap": cfg["basemap"].exists(),
        "basemap_url": f"/basemap/{cfg['basemap'].name}",
        "name": cfg["name"],
        "center": cfg["center"],
        # So the frontend prints times in the network's local clock, not the
        # viewer's — a Brisbane browser looking at Melbourne must show AEDT.
        "tz": str(cfg["tz"]),
    }


@app.get("/api/feeds")
def feeds():
    """Realtime feed health for QC, per region: how many trip updates and
    vehicle positions the last poll saw, how many were dropped and why, and how
    stale each cache is. `without_position` is the count of live vehicles with
    no coordinates — the ones that cannot be mapped."""
    now = time.time()
    return {
        rid: {
            "trip_updates": {
                **st["rt_stats"],
                "age_s": round(now - st["rt_fetch"]) if st["rt_fetch"] else None,
            },
            "vehicle_positions": {
                **st["vp_stats"],
                "age_s": round(now - st["vp_fetch"]) if st["vp_fetch"] else None,
            },
            "alerts": {
                **st["al_stats"],
                "age_s": round(now - st["al_fetch"]) if st["al_fetch"] else None,
            },
        }
        for rid, st in STATE.items()
        if rid in available_regions()
    }


# Frontend
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
# StaticFiles serves HTTP range requests, which is how pmtiles.js reads the
# archive — it fetches byte ranges rather than the whole 22 MB file.
# check_dir=False: the basemap is optional, and the volume may not have one yet.

app.mount(
    "/basemap",
    StaticFiles(directory=BASEMAP_DIR, check_dir=False),
    name="basemap",
)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
