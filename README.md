# Next Service — a live departures board for Brisbane, Melbourne, Sydney, Adelaide, Perth, Darwin, regional Queensland & the NSW TrainLink interstate network

A "next service arriving in X minutes" board with a live map, for any public
transport stop in **Queensland (Translink — South East Queensland plus
eighteen regional town networks, Cairns to Warwick)**, **Victoria (PTV +
V/Line)**, **Sydney (TfNSW)**, **regional NSW & interstate (NSW TrainLink —
XPTs to Melbourne and Brisbane, Xplorers to Canberra, plus the connecting
coaches)**, **South Australia (Adelaide Metro)**, **Western Australia
(Transperth/PTA)** or the **Northern Territory** —
trains, metro, trams, buses and ferries. One codebase, one UI, a region switcher in the header, and a search
that spans every region (each result row carries its state). Every time on
the board is the network's own wall clock, with the timezone (AEST, ACST,
AWST, …) badged beside the stop name.

- **Live departures board** — realtime arrival predictions overlaid on the
  timetable, refreshing every 15 s, with per-service colours that stay stable
  as the board advances. 🛜 marks a realtime prediction, 📅 timetable-only.
- **One board per physical station, however many feeds serve it** — TfNSW
  publishes suburban trains, metro, light rail and TrainLink as separate
  feeds, and the big interstate interchanges (Southern Cross, Roma Street,
  Adelaide Central Bus) span two states' data: boards union all of them, so
  Central shows the XPT beside the suburbans and Southern Cross shows it
  beside the V/Line services.
- **Live map** (MapLibre, fully self-hosted vector basemap) — every listed
  service is on the map: a **solid marker** where the vehicle broadcasts GPS,
  a **ghost marker** dead-reckoned from the timetable where it doesn't. Click
  a row to trace its route; click any grey stop to jump to it; zoom in past
  street level to see every stop in the network.
- **Disruption alerts** — an amber ⚠ on affected rows opens the details.
- **Find your stop** — by name, by address, or by the *near me* button
  (browser geolocation; needs HTTPS off-localhost). An address or located
  point drops a pin on the map with the surrounding stops framed around it.
  House numbers resolve **exactly** via a local build of Geoscape's G-NAF
  (every numbered address in Australia, self-hosted — see
  `ingest_gnaf.py`), with a rate-limited Nominatim proxy as the fallback
  for landmarks and unnumbered queries.
- **The board and the map always agree**: a service is listed only if it can
  be placed on the map (live GPS, en route per the timetable, or about to
  depart its origin).

## Screenshots  
  
![Home Page](screenshot-home.png)

![Arrivals](screenshot-arrivals.png)

![Route Timetable](screenshot-timeline.png)

![Map](screenshot-map.png)
  
## Regions

| Region | Static GTFS | Realtime | Key needed |
| --- | --- | --- | --- |
| `seq` — Translink South East Queensland | public | TripUpdates + VehiclePositions + Alerts | none |
| `qld` — Translink regional Queensland (18 town networks merged: Cairns, Townsville, Mackay, Toowoomba, Rockhampton, Bundaberg, Hervey Bay, Gladstone, Gympie, Warwick, Bowen, Whitsundays, Innisfail, Maleny, Kilcoy, Magnetic Island + ferry, North Stradbroke Island) | public | TripUpdates + VehiclePositions + Alerts for Cairns, Bowen, Innisfail, Maryborough–Hervey Bay & Stradbroke; the rest schedule-only | none |
| `mel` — PTV Melbourne + V/Line Victoria (metro & regional train, tram, metro bus, regional coach) | public | TripUpdates + VehiclePositions + Alerts per mode | free key from [opendata.transport.vic.gov.au](https://opendata.transport.vic.gov.au/) (realtime only) |
| `syd` — TfNSW Sydney (train, metro, bus, ferry, light rail) | keyed | TripUpdates + VehiclePositions + Alerts per mode | free key from [opendata.transport.nsw.gov.au](https://opendata.transport.nsw.gov.au/) (static **and** realtime) |
| `nsw` — NSW TrainLink regional & interstate (XPT to Melbourne/Brisbane, Xplorer to Canberra/Armidale/Broken Hill, connecting coaches) | keyed | TripUpdates + VehiclePositions + Alerts | same TfNSW key as `syd` |
| `ade` — Adelaide Metro South Australia (train, tram, bus, country coaches, Kangaroo Island ferry) | public | TripUpdates + VehiclePositions + Alerts | none |
| `per` — Transperth / PTA Western Australia (train incl. Australind, bus, ferry; regional town buses out to Broome, Carnarvon, Kalgoorlie, Esperance, Albany) | public | none published — schedule-only | none |
| `dar` — NT Transport Darwin (bus: Darwin, Palmerston, rural to Adelaide River & Kakadu) | public | none published — schedule-only | none |

Melbourne works **without** a key as a schedule-only board (arrival times from
the timetable, map positions estimated). With a key, it's fully live — set the
`MEL_*` environment variables (see `deploy/translink.container` for the exact,
verified endpoints; the auth header is `KeyID`).

Sydney needs its key even for the timetable download (`Authorization: apikey
<key>` — pass `--key` or `SYD_API_KEY` to the ingest). Realtime is the `SYD_*`
environment variables, pre-wired in `deploy/translink.container`; run
`deploy/probe-syd.sh <key>` first to confirm TfNSW's current v1/v2 endpoint
split before enabling.

NSW TrainLink (`nsw`) is the interstate region: the XPTs to Melbourne and
Brisbane, the Xplorers to Canberra, Armidale and Broken Hill, and the coach
network that feeds them — live GPS included (the trains carry 4Trak
trackers). It uses the **same** TfNSW key as `syd` for both the timetable
download and the `NSW_*` realtime variables; `deploy/enable-syd-vps.sh`
switches both regions on in one go, and `deploy/probe-syd.sh` probes the
`nswtrains` endpoints alongside Sydney's.

Perth and Darwin publish no realtime feeds at all (the NT Bus Tracker app is
live, but its backend isn't open data), so both run as permanent
schedule-only boards: timetable arrival times, dead-reckoned ghost markers,
and a "timetable only" status line saying so.

Regional Queensland is one merged region: eighteen town networks, each a
small keyless zip on the same Translink host as SEQ's, ingested Sydney-style
under a `<town>:` prefix. Five towns have keyless realtime feeds (Cairns,
Bowen, Innisfail, Maryborough–Hervey Bay, North Stradbroke Island); services
in the other towns appear as timetable ghosts.

A region is offered in the UI once its timetable has been ingested. The API is
region-scoped under `/api/r/{region}/…`; bare `/api/…` paths alias to SEQ.

## Quick start (local, containerised)

```bash
podman build -t translink-departures .
podman volume create translink-data

# Timetables (SEQ ~1 min after download; Melbourne's zip is 292 MB;
# Sydney downloads one zip per mode and needs your TfNSW key;
# NSW TrainLink is one zip on the same TfNSW key;
# Adelaide, Perth and Darwin are one small public zip each;
# regional QLD downloads 18 small keyless zips into one merged region)
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py --region mel
podman run --rm -v translink-data:/data -e SYD_API_KEY=<key> translink-departures python ingest_gtfs.py --region syd
podman run --rm -v translink-data:/data -e SYD_API_KEY=<key> translink-departures python ingest_gtfs.py --region nsw
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py --region ade
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py --region per
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py --region dar
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py --region qld

# Self-built vector basemaps (Planetiler, separate builder image; the first
# build downloads ~2 GB of OSM/Natural Earth sources into the cache volume)
podman build -f basemap/Containerfile -t translink-basemap .
podman run --rm -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=mel -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=syd -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=ade -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=per -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=dar -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=qld -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run --rm -e REGION=nsw -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap  # biggest bbox: Broken Hill–Melbourne–Brisbane

podman run -d -p 8000:8000 -v translink-data:/data translink-departures
```

Open http://localhost:8000. Or, for development: `./deploy/run-local.sh`
builds and runs a dev container on :8002 with realtime for any region whose
key it can find. Keys can be passed as arguments, but the nicer way is a
one-time `~/.config/translink/keys.env` (`NSW_KEY=…`, `VIC_KEY=…`, optional
`VPS_HOST=…`, `chmod 600`) — every key-taking script in `deploy/` reads it
as a fallback, so nothing needs pasting twice. See `deploy/load-keys.sh`.

Bare-metal also works: `pip install -r requirements.txt`, `python
ingest_gtfs.py`, `uvicorn app:app --reload`. The basemap is optional — without
one the map hides and the board works unchanged.

## How it works

- `ingest_gtfs.py` loads a region's GTFS into SQLite (atomic swap, safe under
  a running server). The Melbourne adapter merges PTV's nested per-mode zips,
  prefixing ids `<mode>:` and normalising extended route types to basic GTFS;
  the Sydney adapter downloads TfNSW's separate per-mode "For Realtime" zips
  (authenticated) and merges them the same way.
- `realtime.py` polls each region's GTFS-RT feeds in background tasks:
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

## Code layout

Server side:

- `app.py` — the FastAPI app itself: lifecycle, middleware, and the routers
  it is assembled from. The work lives one module per part of the job:

| File | What it holds |
| --- | --- |
| `regions.py` | the networks: their databases, feeds, timezones, keys, shared state |
| `realtime.py` | the GTFS-RT pollers and the caches they fill |
| `gtfsdb.py` | reading the static timetable, and dead-reckoning ghosts |
| `boards.py` | stop search, sibling stations, the arrivals board, trips |
| `geocoding.py` | text → place (G-NAF, places, Nominatim) and back again |
| `walking.py` | the walking graph: routes along streets, and their names |
| `planning.py` | the journey-planner endpoint |

- `planner.py` — the RAPTOR journey-planner core (pure timetable maths;
  `planning.py` wraps it with realtime and walk legs).
- `ingest_gtfs.py` / `ingest_gnaf.py` / `ingest_places.py` /
  `ingest_overture.py` / `ingest_walkgraph.py` — offline builders for the
  SQLite databases in the data volume (timetables, addresses, businesses/POIs,
  the walking street graph). Run recipes are in each file's header.

The client is `static/index.html` — now just the markup — plus
`static/app.css` and thirteen scripts in `static/js/`, split at the original
single file's section seams. They load **in order** as classic scripts
sharing one global scope, so a file may only reference earlier files at load
time; `boot.js` must stay last:

| File | What it holds |
| --- | --- |
| `state.js` | URL params, shared page state, region config |
| `planner-mode.js` | journey-planner mode: destination state and chips |
| `search.js` | stop / address / place search |
| `map-core.js` | map shared state, colour pools, mode glyphs, colour assignment |
| `map-init.js` | `initMap`: basemap, sources, layers, handlers |
| `map-stops.js` | shape / trip-stop fetchers, stop landmark layers |
| `map-trip.js` | route drawing, trip selection, the trip stops panel |
| `map-fx.js` | emoji font + icon rasterising; board↔map linking (vehicle ping, row flash) |
| `map-vehicles.js` | vehicles on the map: camera moves, view fitting, markers, edge markers |
| `alerts.js` | the disruption-alert modal |
| `board.js` | departures board rendering |
| `planner.js` | journey planner: fetch, itinerary cards, route drawing |
| `boot.js` | resize handling and page boot |

`static/fonts/` is the self-hosted font subset (including the Noto Emoji
glyph subset — see the comment in `fonts.css` before adding glyphs) and
`static/vendor/` the vendored MapLibre/pmtiles and map glyphs.

## Deployment

`./tools/test.sh` is the test suite: it builds the image from `git archive
HEAD` — a clean checkout, so an uncommitted file cannot make it pass — then
ingests a mock feed, re-ingests it to prove the atomic swap, serves the
board, queries it, and runs one regression per bug that shipped once. About
ten seconds. `deploy.sh` runs it as part of every deploy.

GitHub Actions runs the same checks on a clean machine as a second opinion,
but nothing waits for it and it publishes nothing: the image is built and
pushed from the workstation, and a late run republishing `:latest` would
overwrite a newer build with an older one. The server container runs under
rootless Podman with Quadlet units (`deploy/`). A weekly systemd timer
re-ingests **every region's** timetable (`--region all`) — not a nicety: TfNSW rotates Sydney's
train trip ids to a new timetable version within days, after which realtime
predictions silently stop matching a stale static feed.

Day to day there are exactly two deploy commands:

```bash
./deploy/deploy.sh local    # deploy everything pending to the local container
./deploy/deploy.sh vps      # deploy everything pending to the VPS
```

`deploy/state/<target>.yaml` is the single source of truth: one line per
deployable component (`image`, `quadlet-units`, `basemap-<region>`,
`enable-*`), each `deployed`, `pending` (changed since the target last got
it, with a reason), or `blocked` (waiting on a key/decision). Whoever
changes a deployable asset flips its component to pending in the same
change; `deploy.sh` deploys exactly what's pending and flips it back. The
image is built from HEAD, so `deploy.sh` refuses to run with uncommitted
image inputs — there would be no way for them to reach the artifact. A VPS
deploy pushes that image to GHCR for the VPS to pull; a local deploy needs
no registry at all.
`./deploy/status.sh` shows what's stale where; `--dry-run` prints the plan.

The machinery underneath (all callable standalone for surgical work):

- `deploy/install-vps.sh` — first-time install (run as root on the VPS)
- `deploy/release-vps.sh` — ship basemaps + run the update remotely
- `deploy/update-vps.sh` — image pull, ingests, basemap install, restart,
  health checks
- `deploy/enable-mel-vps.sh` / `deploy/enable-syd-vps.sh` — switch on a
  region's realtime (keys from `~/.config/translink/keys.env`)
- `deploy/probe-syd.sh` — verify every TfNSW endpoint before enabling
- `deploy/mark-deployed.sh` — flip state components after a manual step

## Data licensing

- SEQ data © Translink / Queensland Government (CC BY 4.0 at time of writing).
- Victorian data © Department of Transport and Planning (CC BY 4.0 at time of
  writing) via the Transport Victoria Open Data portal.
- NSW data © Transport for NSW (CC BY 4.0 at time of writing) via the TfNSW
  Open Data Hub.
- SA data © Adelaide Metro / Government of South Australia (CC BY 4.0 at time
  of writing) via gtfs.adelaidemetro.com.au.
- WA data © Public Transport Authority of Western Australia via
  transperth.wa.gov.au — check the PTA's site for its current terms of use.
- NT data © Northern Territory Government (CC BY 4.0 at time of writing) via
  the NTG Open Data Portal / dipl.nt.gov.au.
- Basemap © OpenMapTiles © OpenStreetMap contributors; geocoding by
  OpenStreetMap/Nominatim.
- Address data derived from the Geoscape Geocoded National Address File
  (G-NAF) © Geoscape Australia, licensed by the Commonwealth of Australia
  under the Open G-NAF EULA (data.gov.au). G-NAF must not be used for the
  sending of unsolicited mail.
- Place and business data includes data from Overture Maps Foundation
  (overturemaps.org), licensed under CDLA-Permissive 2.0, and from
  OpenStreetMap contributors.
- The goose honk (`static/sounds/honk.mp3`) is a Canada goose (*Branta
  canadensis*) recorded by the US National Park Service (Wind Cave
  National Park bird list) — a US federal government work in the public
  domain, via Wikimedia Commons:
  https://commons.wikimedia.org/wiki/File:Branta_canadensis.ogg

Check each portal for current licensing and attribution requirements.

See `DECISIONS.md` for the binding design decisions, traps hit once and not
to be re-hit, and options ruled out; `TODO.md` for outstanding bugs and
ideas. (The original build diary, `HANDOVER.md`, is retired — it lives on in
git history.)
