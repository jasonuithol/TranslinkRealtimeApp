# turning text into places and places into text
# Split from app.py; each module owns one part of the API and is
# wired together in app.py, which owns the FastAPI object itself.
import asyncio
import math
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from regions import DB_PATH, region_cfg

router = APIRouter()


# Nominatim is a free community service with a firm usage policy: identify the
# app, at most 1 req/s, cache results. The proxy exists so the frontend stays
# fully self-hosted and the policy is enforced in one place.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "TranslinkNextArrivalApp/1.0 (https://github.com/jasonuithol/TranslinkRealtimeApp)"
_geocode_cache: dict = {}          # (region, q) -> (fetched_at, results)
_geocode_last_call = 0.0
# The frontend searches every region at once, so two geocode requests arrive
# together; the lock serialises them so the 1 req/s promise to Nominatim holds.
_geocode_lock = asyncio.Lock()
GEOCODE_CACHE_S = 24 * 3600


_STATE_ABBR = {
    "Queensland": "QLD", "New South Wales": "NSW", "Victoria": "VIC",
    "South Australia": "SA", "Western Australia": "WA",
    "Northern Territory": "NT", "Tasmania": "TAS",
    "Australian Capital Territory": "ACT",
}

# ---------------------------------------------------------------------------
# Local G-NAF geocoder: every street-numbered address in Australia with
# house-level coordinates (built by ingest_gnaf.py; ~700 MB, quarterly).
# OSM's residential house-number coverage is patchy — Nominatim pinned
# "397 Christine Avenue" to an arbitrary point along a 3 km road. When the
# DB is present and the query leads with a house number, this answers
# instead; Nominatim stays the fallback for landmarks and unnumbered
# queries, and for deployments that never ingested G-NAF.
GNAF_DB = Path(os.environ.get("GNAF_DB") or DB_PATH.parent / "gnaf.sqlite3")

_STREET_ABBR = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "dr": "drive", "drv": "drive", "ct": "court", "crt": "court",
    "cres": "crescent", "cr": "crescent", "pde": "parade", "hwy": "highway",
    "tce": "terrace", "pl": "place", "blvd": "boulevard", "bvd": "boulevard",
    "ln": "lane", "cct": "circuit", "esp": "esplanade", "gr": "grove",
    "gdns": "gardens", "pkwy": "parkway",
}


# Local business/POI index (ingest_places.py — named OSM places near
# stops). Answers "bunnings burleigh" instantly; Nominatim remains the
# fallback for anything the OSM community hasn't mapped.
PLACES_DB = Path(os.environ.get("PLACES_DB") or DB_PATH.parent / "places.sqlite3")


def _near_dist(near):
    """Squared flat-earth distance to `near` ((lat, lon) or None) as a sort
    key builder; without a near point every candidate ties at zero."""
    if not near:
        return lambda r: 0.0
    nlat, nlon = near
    coslat = math.cos(math.radians(nlat)) or 1e-9
    def d(r):
        return ((r["lat"] - nlat) ** 2
                + ((r["lon"] - nlon) * coslat) ** 2)
    return d


def places_geocode(q: str, viewbox: str, limit: int = 8,
                   near: tuple | None = None) -> list | None:
    """Named-place lookup. None = no DB or nothing matched — fall through.
    Results inside the current region's viewbox rank first (like the
    Nominatim viewbox bias), nearest to `near` first within that; category
    and suburb ride along in the label."""
    if not PLACES_DB.exists():
        return None
    tokens = [t for t in re.findall(r"[A-Za-z0-9']+", q.lower()) if t]
    if len(tokens) < 1 or len(q.strip()) < 3:
        return None
    # Every token must match somewhere (name/category/suburb); the last one
    # as a prefix, so mid-word queries behave like search-as-you-type.
    fts = " ".join(f'"{t}"' for t in tokens[:-1])
    fts = (fts + " " if fts else "") + f'"{tokens[-1]}"*'
    lon1, lat1, lon2, lat2 = (float(x) for x in viewbox.split(","))
    con = sqlite3.connect(PLACES_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT p.name, p.category, p.suburb, p.state, p.lat, p.lon "
            "FROM places_fts f JOIN places p ON p.id = f.rowid "
            # A wide pool: a chain has hundreds of branches and the near
            # sort can only rank candidates the query returned.
            "WHERE places_fts MATCH ? ORDER BY bm25(places_fts) LIMIT 250",
            (fts,)).fetchall()
    except sqlite3.OperationalError:
        return None   # pathological FTS query string
    finally:
        con.close()
    if not rows:
        return None
    def in_box(r):
        return (min(lat1, lat2) <= r["lat"] <= max(lat1, lat2)
                and min(lon1, lon2) <= r["lon"] <= max(lon1, lon2))
    # An exact name match beats places that merely CONTAIN the query
    # ("Pacific Fair" the mall above "Roll'd Pacific Fair" the tenant).
    qnorm = "".join(tokens)
    def exact(r):
        return re.sub(r"[^a-z0-9]", "", r["name"].lower()) == qnorm
    dist = _near_dist(near)
    rows.sort(key=lambda r: (not in_box(r), not exact(r), dist(r)))
    out, seen = [], set()
    for r in rows:
        bits = [r["category"]]
        if r["suburb"]:
            bits.append(f"{r['suburb']} {r['state']}".strip())
        label = f"{r['name']} — {', '.join(bits)}"
        if label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "lat": r["lat"], "lon": r["lon"]})
        if len(out) >= limit:
            break
    return out or None


def gnaf_geocode(q: str, state_bias: str, limit: int = 8,
                 near: tuple | None = None) -> list | None:
    """House-number lookup. None means 'not applicable' (no DB, or the query
    doesn't lead with a house number) — the caller falls back to Nominatim.
    An address short of its exact number returns the NEAREST number on that
    street, honestly labelled — next door beats a random point mid-road."""
    if not GNAF_DB.exists():
        return None
    m = re.match(r"^\s*(?:\d+\s*/\s*)?(\d+)\s+(.+)", q)
    if not m:
        return None
    num = int(m.group(1))
    tokens = [_STREET_ABBR.get(t, t)
              for t in (t.lower() for t in re.findall(r"[A-Za-z']+", m.group(2)))]
    if not tokens:
        return None
    con = sqlite3.connect(GNAF_DB)
    con.row_factory = sqlite3.Row
    try:
        # Every token must appear somewhere across name/type/locality/state;
        # the current region's state ranks first, like the Nominatim viewbox
        # bias. bm25 keeps "Christine Avenue" above "Christine Court".
        fts = " ".join(f'"{t}"' for t in tokens)
        streets = con.execute(
            "SELECT s.id, s.name, s.type, s.locality, s.state "
            "FROM streets_fts f JOIN streets s ON s.id = f.rowid "
            "WHERE streets_fts MATCH ? "
            "ORDER BY (s.state = ?) DESC, bm25(streets_fts) LIMIT 150",
            (fts, state_bias),
        ).fetchall()
        out = []
        for s in streets:
            a = con.execute(
                "SELECT num, lat, lon FROM addresses WHERE street_id = ? "
                "ORDER BY ABS(num - ?) LIMIT 1", (s["id"], num)).fetchone()
            if a is None:
                continue
            title = f"{a['num']} {s['name'].title()}"
            if s["type"]:
                title += f" {s['type'].title()}"
            out.append({
                "label": f"{title}, {s['locality'].title()} {s['state']}",
                "lat": a["lat"], "lon": a["lon"],
                "_exact": a["num"] == num,
            })
        # Rank the WHOLE street pool, then trim — cutting at `limit` before
        # sorting let the bm25 street order decide which candidates existed
        # at all, and the one exact "397 Christine Avenue" fell off the list
        # while its Robina/Kuraby namesakes made it in.
        #
        # With a bias point, exactness is worth kilometres, not everything:
        # someone near Toowong typing "12 High Street" means the High Street
        # THERE (nearest number 10), not an exact 12 in Charleville — but an
        # exact 397 in the next suburb must beat a 480 marginally closer.
        # Unbiased, exact numbers simply come first.
        if near:
            dist = _near_dist(near)
            def rank(r):
                km = math.sqrt(dist(r)) * 111.0
                return km + (0.0 if r["_exact"] else 25.0)
            out.sort(key=rank)
        else:
            out.sort(key=lambda r: not r["_exact"])
        del out[limit:]
        for r in out:
            r.pop("_exact")
        return out or None
    except sqlite3.Error as exc:
        print(f"[gnaf] lookup failed: {exc}")
        return None
    finally:
        con.close()


def _geocode_label(r: dict, state: str) -> str:
    """A short label from Nominatim's structured address: 'lead, suburb STATE'.
    display_name is the full hierarchy ('12 X St, Suburb, Brisbane City,
    Queensland, 4006, Australia') — far more than a result row needs.

    The state comes from the ADDRESS, not the region: the nsw region's
    geocode viewbox spans four states (Broken Hill to Brisbane to
    Melbourne), and stamping the region's state labelled a Varsity Lakes
    QLD street as NSW. The region state is only the fallback."""
    a = r.get("address") or {}
    state = _STATE_ABBR.get(a.get("state") or "", state)
    # A named place (a stadium, a school) reads better as its name than as its
    # street address; plain house hits have no name and use number + road.
    lead = r.get("name") or ""
    if not lead and a.get("house_number") and a.get("road"):
        lead = f"{a['house_number']} {a['road']}"
    if not lead:
        lead = a.get("road") or r.get("display_name", "").split(",")[0].strip()
    suburb = next((a[k] for k in ("suburb", "neighbourhood", "village",
                                  "town", "locality", "city_district")
                   if a.get(k)), None)
    bits = [lead]
    if suburb and suburb.casefold() != lead.casefold():
        bits.append(suburb)
    return f"{', '.join(bits)} {state}".strip()


@router.get("/api/r/{region}/rgeocode")
@router.get("/api/rgeocode")
def reverse_geocode(lat: float, lon: float, region: str = "seq"):
    """A point -> the address a person would say. Dropped pins otherwise
    travel as "dropped pin", and a journey card reading "Walk to dropped
    pin" tells a rider nothing. Nearest G-NAF address wins; beyond ~150 m
    the answer is hedged ("near ..."), and with nothing within ~600 m
    there is honestly no address to give."""
    if not GNAF_DB.exists():
        return {"label": None}
    con = sqlite3.connect(GNAF_DB)
    try:
        for span_m, hedge in ((150.0, ""), (600.0, "near ")):
            dlat = span_m / 111000.0
            dlon = span_m / (111000.0 * max(0.2, math.cos(math.radians(lat))))
            rows = con.execute(
                "SELECT a.num, a.lat, a.lon, s.name, s.type, s.locality, s.state "
                "FROM addresses a JOIN streets s ON s.id = a.street_id "
                "WHERE a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?",
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
            best, bd = None, span_m ** 2
            for num, ala, alo, name, typ, loc, st in rows:
                d2 = (((ala - lat) * 111000.0) ** 2
                      + ((alo - lon) * 111000.0
                         * math.cos(math.radians(lat))) ** 2)
                if d2 < bd:
                    bd, best = d2, (num, name, typ, loc, st)
            if best:
                num, name, typ, loc, st = best
                street = name.title() + (f" {typ.title()}" if typ else "")
                # Beyond the close range the house number would be a lie;
                # the street and suburb are still true.
                head = f"{num} {street}" if not hedge else street
                return {"label": f"{hedge}{head}, {loc.title()} {st}",
                        "exact": not hedge}
    except sqlite3.Error as exc:
        print(f"[rgeocode] failed: {exc}")
    finally:
        con.close()
    return {"label": None}


# Labels a caller may send that name nothing. A shared URL carrying one
# (or none at all) should still produce a journey a rider can follow.
_PLACEHOLDER_LABELS = {"", "dropped pin", "destination",
                       "your address", "your destination",
                       "near me", "your location", "you"}


def _point_label(lat, lon, given, fallback):
    if given and given.strip().lower() not in _PLACEHOLDER_LABELS:
        return given
    return reverse_geocode(lat=lat, lon=lon).get("label") or fallback


@router.get("/api/r/{region}/geocode")
@router.get("/api/geocode")
async def geocode(q: str, region: str = "seq",
                  near_lat: float | None = None,
                  near_lon: float | None = None):
    """Address -> candidate points, proxied through Nominatim (OpenStreetMap).
    Pair with /api/stops/nearby to answer 'which stops are closest to home?'.
    Bounded to the region's bbox, so "Main St" resolves to the Main St here."""
    global _geocode_last_call
    cfg = region_cfg(region)
    q = q.strip()
    if len(q) < 4:
        return []
    # An optional bias point: the searcher's location (origin search) or the
    # journey's departure (destination search) — near candidates rank first.
    near = ((near_lat, near_lon)
            if near_lat is not None and near_lon is not None else None)
    # House-numbered queries answer from the local G-NAF when it's ingested —
    # exact house coordinates, no rate limit, no network.
    local = gnaf_geocode(q, cfg["state"], near=near)
    if local:
        return local
    # Then named places (businesses, POIs) from the local OSM index.
    placed = places_geocode(q, cfg["geocode_viewbox"], near=near)
    if placed:
        return placed
    hit = _geocode_cache.get((region, q.lower()))
    if hit and time.time() - hit[0] < GEOCODE_CACHE_S:
        return sorted(hit[1], key=_near_dist(near)) if near else hit[1]
    async with _geocode_lock:
        # Enforce the 1 req/s policy even if the client misbehaves.
        wait = 1.0 - (time.time() - _geocode_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _geocode_last_call = time.time()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={
                        "q": q, "format": "jsonv2", "limit": 10,
                        "countrycodes": "au", "addressdetails": 1,
                        # The viewbox only BIASES ranking now (no bounded=1):
                        # local streets sort first, but the query is
                        # nationwide — so the client asks ONE region instead
                        # of fanning out to nine, each serialised behind the
                        # 1 req/s Nominatim policy. Nine bounded lookups made
                        # every uncached address search take ~9 seconds.
                        "viewbox": cfg["geocode_viewbox"],
                    },
                    headers={"User-Agent": NOMINATIM_UA},
                )
                resp.raise_for_status()
                results = [
                    {
                        "label": _geocode_label(r, cfg["state"]),
                        "lat": float(r["lat"]),
                        "lon": float(r["lon"]),
                    }
                    for r in resp.json()
                ]
        except Exception as exc:
            print(f"[geocode] lookup failed: {exc}")
            raise HTTPException(502, "address lookup unavailable")
    _geocode_cache[(region, q.lower())] = (time.time(), results)
    # An unbounded cache only grows by distinct queries typed; trim anyway.
    if len(_geocode_cache) > 500:
        _geocode_cache.pop(next(iter(_geocode_cache)))
    # The cache stays bias-free (keyed on the query alone); the near sort is
    # applied per request on the way out.
    return sorted(results, key=_near_dist(near)) if near else results
