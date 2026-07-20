# Handover: Translink "Next Service" Board

Context document for continuing development. Originally written when the
project was built in claude.ai (July 2026); updated 2026-07-21 after the
first real-feed run and containerisation.

## What this is

A "next service arriving in X minutes" departures board for any Translink
stop or station in South East Queensland. Buses, trains, ferries and trams
are all supported — they share one GTFS feed. No API key is required for
any Translink data.

## Stack

- **Backend:** Python / FastAPI (`app.py`), SQLite for the static timetable
- **Frontend:** single vanilla-JS page (`static/index.html`), no build step
- **Data:** Translink open GTFS (static zip) + GTFS-RT TripUpdates (protobuf)

## How to run

```bash
pip install -r requirements.txt
python ingest_gtfs.py        # downloads SEQ_GTFS.zip, builds gtfs.sqlite3
uvicorn app:app --reload     # then open http://localhost:8000
```

`gtfs.sqlite3` is a build artifact — regenerate it any time with
`ingest_gtfs.py` (the feed changes roughly weekly). The schema is dropped and
rebuilt on every run, so schema changes never need migrations. `ingest_gtfs.py`
builds into a `.tmp` file and `os.replace()`s it into position, so a refresh
can run against a live server without the board seeing half-dropped tables.

Both scripts honour a `GTFS_DB` env var for the database path, defaulting to
the path above. The container sets it to `/data/gtfs.sqlite3`.

## Deployment

Runs as a container; the same image serves the board and runs the ingest.

```bash
podman build -t translink-departures .
podman volume create translink-data
podman run --rm -v translink-data:/data translink-departures python ingest_gtfs.py
podman run --rm -v translink-data:/data translink-departures ./fetch_basemap.sh
podman run -d -p 8000:8000 -v translink-data:/data translink-departures
```

The basemap step is optional and slow-moving — refresh it occasionally, not
weekly like the timetable.

On the VPS it is managed by **Quadlet** units in `deploy/`, installed by
`deploy/install-vps.sh` — see that script's header for what it assumes about
the host. `.github/workflows/ci.yml` smoke-tests against `mock_gtfs.zip` and
publishes to `ghcr.io/jasonuithol/translink-departures`; the server container
is `AutoUpdate=registry`, so a push to `main` rolls out on its own.

> **This host is shared with `~/Projects/Java2026/inventoryquest`.** That
> project's `scripts/provision-vps.sh` owns host provisioning (the `deploy`
> user, rootless Podman, subuid ranges, linger, `podman-auto-update.timer`),
> and it holds port 8080. This app uses 8000. Check both before assigning a
> port or changing anything host-level.

## Architecture

1. `ingest_gtfs.py` loads six GTFS tables into SQLite: stops, routes,
   trips, stop_times, calendar, calendar_dates. Only the columns needed
   for a departures board are kept.
2. `app.py` runs a background task (started via FastAPI lifespan) that
   polls the TripUpdates protobuf feed every 30 s and keeps an in-memory
   cache shaped `{trip_id: {stop_id: {arrival, delay, skipped}}}`.
3. `GET /api/departures/{stop_id}` merges scheduled times with the
   realtime cache and returns the next services (90 min lookahead, max 12),
   each flagged `realtime: true/false`.
4. `GET /api/stops/search?q=` finds stops by name.
5. The frontend polls the departures endpoint every 15 s. A stop can be
   bookmarked via `/?stop=<stop_id>`.

## Key design decisions (don't accidentally undo these)

- **Parent stations:** train stations (and multi-pontoon ferry terminals)
  are GTFS parent records (`location_type=1`); their departures live on
  child platform stops (`parent_station` FK). The departures endpoint
  expands a parent into all its children and merges the results. Realtime
  matching uses each departure's own platform `stop_id`, not the parent's.
  Search hides child platforms and shows the parent once.
- **After-midnight trips:** GTFS times can exceed 24:00 (e.g. `25:10` =
  1:10 am under the previous service date). The endpoint queries both
  today's and yesterday's service dates to catch these.
- **Graceful degradation:** if the realtime poll fails, the cached data is
  kept and the board falls back to scheduled times. The board must never
  go blank because the feed hiccupped.
- **SKIPPED stops** in TripUpdates are dropped from results.
- **Delay vs absolute time:** a realtime prediction uses the feed's
  absolute `arrival.time` if present, otherwise `scheduled + delay`.
- **Platform labels** come from `platform_code`, falling back to a regex
  on the child stop name ("... platform 3").

## Data endpoints

- Static: `https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip`
- Realtime: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates`
- Not yet used: `.../SEQ/VehiclePositions` (live GPS), `.../SEQ/Alerts`
  (disruptions)

Data © Translink / Queensland Government under their open data terms
(CC BY 4.0 at time of writing — verify on data.qld.gov.au).

## Verified against the real feed (2026-07-21)

This was originally built and unit-tested in claude.ai against **mock GTFS
data and synthetic protobuf feeds** only — the build sandbox could not reach
Translink's servers. It has since been run end to end against the live feed,
via the container and Quadlet units, and every open question from that first
handover is now closed:

| Original concern | Measured |
| --- | --- |
| Feed "hundreds of MB unzipped" | **23.8 MB** zip → 254 MB SQLite, 266 MB volume |
| Ingest memory/time on a small VPS | **12 seconds**; completes under a **64 MB** cap |
| Real `trip_id`s match between feeds | **2,219 / 2,243 live trips matched (98.9%)** |
| Train stations resolve, platforms merge | Central station merged platforms 1–6 |
| Route colors/badges for real lines | Correct, from the feed |

Two things worth knowing that only showed up on real data:

- **Realtime coverage is sparse and bus-heavy.** The TripUpdates feed carries
  only ~2,200 of 84,440 trips at any moment, dominated by buses (BT ~1165,
  SBL ~313) with roughly 105 rail trips. A train station will often show
  every departure as `scheduled` — that is correct behaviour, not a bug, and
  not a `trip_id` mismatch. Confirm the merge itself is alive by checking a
  busy bus stop (e.g. stop `1153`, West End Cityglider) rather than a station.
- `TimeoutStartSec=1800` in `translink-ingest.container` is ~150× the measured
  runtime. Deliberately generous; tighten it if you want faster failure.

The mock fixture (`mock_gtfs.zip`) is still the CI smoke-test input — see
`.github/workflows/ci.yml`. There is no unit-test suite; the original tests
were written in claude.ai and were not preserved.

## Frontend design intent

Styled after Brisbane busway passenger information displays: dark board,
amber LED-style countdowns (IBM Plex Mono), route badges in each route's
official color from the feed, green pulsing dot = live prediction, "DUE"
flashes under 1 minute. Respects `prefers-reduced-motion`. Keep this
identity when extending.

## Map view with live vehicle positions — built

A MapLibre map sits under the board, showing the live GPS of the vehicles
running the services listed. Everything is self-hosted; the page makes no
external requests at all.

- `app.py` polls `.../SEQ/VehiclePositions` alongside TripUpdates and keys it
  by `trip_id`. `/api/departures/{stop_id}` returns a `vehicles` array holding
  positions for **only the trips on the board**, so the map can never show a
  vehicle the user has no row for.
- Basemap: a **Protomaps `.pmtiles`** extract of SEQ built by
  `fetch_basemap.sh` onto the data volume — 22 MB at maxzoom 13, ~11 s to
  build. Served by StaticFiles, which answers the HTTP range requests
  pmtiles.js uses to read the archive without downloading it whole.
- `/api/config` reports whether a basemap exists; without one the map hides
  and the board works unchanged. CI asserts that path.
- `static/map-style.json` is a hand-written dark style matching the board.
  Glyphs, MapLibre and pmtiles.js are vendored under `static/vendor/`.

Two bugs worth not reintroducing:

- **Do not construct the Map inside a hidden container.** MapLibre measures
  its container at construction; `display:none` gives it zero size and it then
  never fires `load`. Reveal `#map-wrap` *before* `new maplibregl.Map()`.
- **`new pmtiles.Protocol({metadata: true})` — the flag is required.** The
  style's source uses `url:`, so MapLibre asks the protocol for TileJSON.
  Without the flag pmtiles ignores that request and the promise never settles:
  the style hangs forever with no error on any channel.

Note that headless Firefox cannot verify the map — with no GPU it never
completes a render pass, so MapLibre's `load` never fires and the canvas stays
blank even though the style loads. Check the map in a real browser.

### Bing Maps: ruled out, don't revisit

**Bing Maps was requested, conditional on being free. It is not.** As checked
on 2026-07-21:

- Bing Maps for Enterprise **free (Basic) tier retired 30 June 2025**. New
  free keys are not issued. Existing *Enterprise* contracts run to 30 June
  2028, and renewals from August 2026 can no longer get a full 12-month term.
- The Microsoft-recommended successor is **Azure Maps**, but it does not
  solve the cost condition either: Gen1 pricing retires 15 September 2026,
  and under Gen2 **map tile requests do not draw on the free transaction
  allowance** — they bill at ~$0.50 per 1,000 transactions (1 transaction ≈
  15 tiles) from the first request.

So a free Microsoft mapping option no longer exists. MapLibre + Protomaps was
chosen instead and is what ships — see above.

## Other next-step ideas (not yet built)

- Service alerts for the selected stop (Alerts feed)
- Multiple pinned stops (home + work) on one screen
- Filter by route or direction
