# Next Service — a live departures board for Brisbane & Melbourne

A "next service arriving in X minutes" board with a live map, for any public
transport stop in **South East Queensland (Translink)** or **metropolitan
Melbourne (PTV)** — trains, trams, buses and ferries. One codebase, one UI,
a region switcher in the header.

- **Live departures board** — realtime arrival predictions overlaid on the
  timetable, refreshing every 15 s, with per-service colours that stay stable
  as the board advances. 🛜 marks a realtime prediction, 📅 timetable-only.
- **Live map** (MapLibre, fully self-hosted vector basemap) — every listed
  service is on the map: a **solid marker** where the vehicle broadcasts GPS,
  a **ghost marker** dead-reckoned from the timetable where it doesn't. Click
  a row to trace its route; click any grey stop to jump to it; zoom in past
  street level to see every stop in the network.
- **Disruption alerts** — an amber ⚠ on affected rows opens the details.
- **Find your stop** — by name, by address (auto-geocoded via a rate-limited
  Nominatim proxy), or by the *near me* button (browser geolocation; needs
  HTTPS off-localhost).
- **The board and the map always agree**: a service is listed only if it can
  be placed on the map (live GPS, en route per the timetable, or about to
  depart its origin).

## Regions

| Region | Static GTFS | Realtime | Key needed |
| --- | --- | --- | --- |
| `seq` — Translink South East Queensland | public | TripUpdates + VehiclePositions + Alerts | none |
| `mel` — PTV Melbourne (metro train, tram, metro bus) | public | TripUpdates + VehiclePositions + Alerts per mode | free key from [opendata.transport.vic.gov.au](https://opendata.transport.vic.gov.au/) |

Melbourne works **without** a key as a schedule-only board (arrival times from
the timetable, map positions estimated). With a key, it's fully live — set the
`MEL_*` environment variables (see `deploy/translink.container` for the exact,
verified endpoints; the auth header is `KeyID`).

A region is offered in the UI once its timetable has been ingested. The API is
region-scoped under `/api/r/{region}/…`; bare `/api/…` paths alias to SEQ.

## Quick start (local, containerised)

```bash
podman build -t translink-departures .
podman volume create translink-data

# Timetables (SEQ ~1 min after download; Melbourne's zip is 292 MB)
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py --region mel

# Self-built vector basemaps (Planetiler, separate builder image; the first
# build downloads ~2 GB of OSM/Natural Earth sources into the cache volume)
podman build -f basemap/Containerfile -t translink-basemap .
podman run --rm -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=mel -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap

podman run -d -p 8000:8000 -v translink-data:/data translink-departures
```

Open http://localhost:8000. Or, for development: `./deploy/run-local.sh
[VIC-API-KEY]` builds and runs a dev container on :8002 (key optional —
enables Melbourne realtime).

Bare-metal also works: `pip install -r requirements.txt`, `python
ingest_gtfs.py`, `uvicorn app:app --reload`. The basemap is optional — without
one the map hides and the board works unchanged.

## How it works

- `ingest_gtfs.py` loads a region's GTFS into SQLite (atomic swap, safe under
  a running server). The Melbourne adapter merges PTV's nested per-mode zips,
  prefixing ids `<mode>:` and normalising extended route types to basic GTFS.
- `app.py` (FastAPI) polls each region's GTFS-RT feeds in background tasks:
  TripUpdates (arrival predictions, with spec-correct **delay propagation** to
  later stops — Melbourne trams publish only the next stop or two),
  VehiclePositions (the map's solid markers) and Alerts (the ⚠ popup).
- Services without GPS get a **ghost position** interpolated along their
  scheduled stops; runs that haven't left their origin (within a 10-min
  staging window) are held off both the board and the map.
- The basemap is a self-built OpenMapTiles `.pmtiles` (Planetiler over the
  Geofabrik Australia extract), served by HTTP range requests — the page makes
  **no external requests at all** at runtime (the only exception: the optional
  server-side geocoding proxy calls Nominatim).
- `/api/feeds` exposes per-region feed health (counts, drops, staleness) for
  quick QC. `?mapdebug=1` logs per-layer rendered-feature counts.

## Deployment

CI (GitHub Actions) smoke-tests every push against a mock feed and publishes
`ghcr.io/jasonuithol/translink-departures`; the server container runs under
rootless Podman with Quadlet units (`deploy/`) and `AutoUpdate=registry`, so a
push to `main` rolls out on its own. A weekly systemd timer re-ingests the SEQ
timetable.

- `deploy/install-vps.sh` — first-time install (run as root on the VPS)
- `deploy/release-vps.sh root@host` — one local command: ship basemap + run
  the update remotely
- `deploy/update-vps.sh` — image pull, Melbourne ingest, basemap install,
  restart, health checks
- `deploy/enable-mel-vps.sh root@host KEY` — switch on Melbourne realtime

## Data licensing

- SEQ data © Translink / Queensland Government (CC BY 4.0 at time of writing).
- Victorian data © Department of Transport and Planning (CC BY 4.0 at time of
  writing) via the Transport Victoria Open Data portal.
- Basemap © OpenMapTiles © OpenStreetMap contributors; geocoding by
  OpenStreetMap/Nominatim.

Check each portal for current licensing and attribution requirements.

See `HANDOVER.md` for the full design log: every architectural decision, bug
post-mortem and outstanding item.
