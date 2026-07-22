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
# Basemap: built by a SEPARATE image (Java/Planetiler), see basemap/
podman build -f basemap/Containerfile -t translink-basemap .
podman run --rm -v translink-data:/data -v translink-basemap-cache:/cache translink-basemap
podman run -d -p 8000:8000 -v translink-data:/data translink-departures
```

The basemap step is optional and slow-moving — refresh it occasionally, not
weekly like the timetable. First build downloads ~2 GB of sources (Australia
OSM extract, Natural Earth, water polygons) into the cache volume and takes
~10 min; rebuilds reuse the cache. Output: 64 MB `seq.pmtiles` on the data
volume.

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

1. `ingest_gtfs.py` loads seven GTFS tables into SQLite: stops, routes,
   trips, stop_times, calendar, calendar_dates, shapes. Only the columns
   needed for a departures board are kept. Adding shapes took the ingest
   from 12s to 16s and the database from 254 MB to 303 MB.
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
- **Timezone:** GTFS times are in the *agency's* local time, so
  `gtfs_time_to_epoch` pins them to `Australia/Brisbane` rather than trusting
  the host clock. A naive `datetime` reads them in the system zone, which under
  a UTC container put every scheduled time 10 hours out — and that shift landed
  the after-midnight (24:xx) services squarely in the lookahead window, so the
  board confidently showed last night's trains as this morning's departures.
  `tzdata` is a runtime dependency: the slim base image ships no system zoneinfo.
- **Deduplication:** querying both service dates means a trip running on each
  produces two rows. The stale one normally falls outside the window, but an
  absolute realtime arrival overwrites *both* copies with the same prediction,
  so both survive — which is why doubled-up rows only ever appeared on services
  carrying realtime, and never on trains. The endpoint keeps the copy whose
  schedule is closest to the prediction. CI guards this.
- **Platform labels** come from `platform_code`, falling back to a regex
  on the child stop name ("... platform 3").

## Regions (multi-network support — branch `regions`, NOT yet deployed)

One board, many networks. `app.py` has a `REGIONS` registry — per region: a
static GTFS SQLite DB, lists of GTFS-RT feeds (each with an id prefix), a
timezone, a basemap file, a geocoder bbox and a map centre — plus a `STATE` map
holding that region's realtime caches and feed-health stats. Pollers are
spawned per region per configured feed kind; a region with no realtime
configured is *static-only* and still fully works: the board shows scheduled
times and the map shows timetable-estimated ghosts.

- API is region-scoped under `/api/r/{region}/…`; every original `/api/…` path
  remains as an alias for `seq`, so old bookmarks and the deployed VPS keep
  working. `/api/regions` lists ingested regions for the frontend switcher.
- A region is only offered once its DB exists — no Melbourne ingest, no
  switcher, zero behaviour change for a SEQ-only deployment.
- **Melbourne (`mel`)**: PTV's static GTFS is one outer zip with a *nested* zip
  per mode (2 = metro train, 3 = tram, 4 = metro bus; ids only unique within a
  mode). `ingest_gtfs.py --region mel` loads modes 2/3/4 and prefixes every id
  `<mode>:`; the RT poller config carries the same prefix per feed so realtime
  ids land on the ingested ones. PTV uses Google's *extended* route types
  (400 = metro rail, 701 = bus, 900s = tram) — `normalize_route_type()`
  collapses them to the basic 0-4 set at ingest so rail-station detection, mode
  emoji and labels all just work. 11.6 M stop_times; ~4 min ingest.
- **Melbourne realtime needs a (free) registered key** and the host has moved
  between VIC data portals, so it is entirely env-driven — unset means
  static-only: `MEL_TRIP_UPDATES="2|https://…;3|https://…"` (mode prefix per
  feed), same for `MEL_VEHICLE_POSITIONS` / `MEL_ALERTS`, `MEL_API_KEY`,
  `MEL_API_KEY_HEADER` (default `Ocp-Apim-Subscription-Key`).
- Frontend: region comes from `?region=` / localStorage; all API calls go
  through `api(path)`; the eyebrow shows the region name and (when 2+ regions
  are ingested) a `⇄` switch button that reloads into the other region. The
  map style is one file — the client points its `omt` source at the region's
  pmtiles from `/api/r/{region}/config` (`basemap_url`, `center`). Board times
  render in the *region's* timezone (`tz` from config), not the viewer's.
- Basemaps per region: `REGION=mel podman run … translink-basemap` (bbox
  presets in `basemap/build-basemap.sh`). Melbourne DB: `/data/gtfs-mel.sqlite3`
  (`MEL_GTFS_DB` to override); basemap `mel.pmtiles`.

## Keys & credentials needed for full functionality

| What | Status | Unlocks | How |
| --- | --- | --- | --- |
| **VIC open data API key** | ☑ OBTAINED (2026-07-22), not yet deployed | Melbourne realtime: live vehicle dots (incl. train occupancy), realtime arrival times, ⚠ disruption alerts | The "Data Platform API Token" from the portal profile. Auth header is `KeyID` (verified; the OpenAPI specs wrongly say Ocp-Apim-Subscription-Key). Paste into the commented `MEL_*` lines in `deploy/translink.container` — incl. `MEL_API_KEY_HEADER=KeyID` — then daemon-reload + restart |
| **HTTPS on the VPS** | ☐ NOT SET UP (not a key, but gates a feature) | The "near me" geolocation button — browsers block geolocation on plain http (localhost excepted) | Reverse proxy + Let's Encrypt, or Caddy; shared host with inventoryquest, so coordinate ports |

Everything else runs keyless by design: SEQ static+realtime (Translink, open),
PTV static GTFS (public), Nominatim geocoding (identified UA + enforced rate
limit), GHCR image pulls (public), Protomaps→self-built basemaps (none).

## Data endpoints

- SEQ static: `https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip`
- SEQ realtime: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates`
- SEQ realtime: `.../SEQ/VehiclePositions` (live GPS, drives the map)
- SEQ realtime: `.../SEQ/Alerts` (disruptions — the ⚠ marks)
- MEL static: `https://data.ptv.vic.gov.au/downloads/gtfs.zip` (292 MB, keyless)
- MEL realtime: env-configured, needs a registered key (see Regions above)
- Geocoding: `nominatim.openstreetmap.org`, proxied via `/api/r/{region}/geocode`
  — identified UA, server-enforced 1 req/s, 24 h cache, bounded to the region
  bbox, explicit user action only (the "Search as an address" row). Fair-use
  community service: keep it that way.

## Disruption alerts

`poll_alerts` (5-min cycle) keeps only alerts *active now* (the feed carries
future planned works too). SEQ keys them by route_id (mostly) and stop_id —
no trip-level entries. `/api/…/departures` attaches `alert_ids` per row and
one deduplicated response-level `alerts` map (a network-wide alert can span
half the board; it is sent once). Frontend: an amber ⚠ (U+26A0, in the
monochrome subset) beside the source mark — amber deliberately, a warning is
not part of the service's colour identity — opens a popup built with DOM APIs
only (feed text is untrusted). `/api/feeds` reports alert counts per region.

## Nearest stops ("which stop is closest to home?")

Two entry points, one dropdown: the **near me** button (browser geolocation —
NOTE: browsers require HTTPS for geolocation, localhost excepted, so on a plain
http VPS the button degrades with an explanatory message) and typed **address
search**, which runs automatically: a query starting with a house number, or a
6+-character query matching no stop name, geocodes without any extra click
(longer 500 ms debounce keeps mid-typing away from the shared geocoder). Both
feed `/api/r/{region}/stops/nearby?lat&lon` — bbox prefilter + haversine, child
platforms collapsed to their parent station, distances in the dropdown.

## OUTSTANDING BUG — one stop's icon suppressed near the viewed stop

Reported on the all-stops layer at Varsity Lakes: viewing "Varsity Lakes
station, stop A" (300051), the station marker (`place_varsta`, ~23 m away)
does not draw even past zoom 15 — it only appears when a route whose landmarks
include it is traced. Everything else on the layer renders. Two fixes did NOT
cure it: (1) `symbol-sort-key` priority + cache-busted payload; (2)
`icon-ignore-placement: true` on stop-ring/vehicle-dot/ghost-dot (the
cross-layer collision-suppression theory — markers no longer claim collision
space, verified present in the deployed build, still reproduces per user).

Facts established: `place_varsta` IS in the `/all-stops?v=2` payload with
route_type 2; the layer definition validates and adds in a real MapLibre
engine (headless blank-style test); the failure is specific to this icon, not
the layer. Next diagnostic step when picked up again: load with `&mapdebug=1`
at the Varsity view and read the per-layer counts, and
`map.queryRenderedFeatures({layers:["all-stops"]})` vs
`querySourceFeatures("all-stops")` around that coordinate to separate
"feature missing from source" from "feature present but not placed".
Also worth checking: text/label collision (labels still participate), and
whether the landmarks-layer paired stop at the same spot wins placement and
the all-stops twin is then culled as a same-layer duplicate.

## Stop landmarks on the map

Three grey landmark tiers, all clickable to select (tram stops draw 🚉,
buses 🚏, ferries ⛴, stations 🏫 — all on the same layers, same zoom rules):

- **Paired stops, always**: `/api/…/departures` returns `paired` — every
  parentless non-station stop within ~120 m of the viewed one — because a
  street-side stop is almost always half of an opposite-directions pair and
  "going the other way" is the next thing a rider looks for.
- **The traced route's stops** while a service is selected (as before).
- **Every stop in the network past zoom 15** (`all-stops` layer): fetched
  lazily on first zoom-in from `/api/…/all-stops` (12 k stops / 1.3 MB SEQ,
  24 k / 2.5 MB MEL), each tagged with its dominant mode via one GROUP BY over
  stop_times — ~15 s on Melbourne's 11.6 M rows, so the cache is warmed in a
  startup thread rather than hanging the first zoomed-in view. Overlap-thinned,
  rail stations excluded (they have their own layer).

**Feed QC.** Each poll logs a one-line summary (`[vp] N vehicles: P positioned,
C cached, …`, `[tu] N trip updates`) and stores it for `/api/feeds`, so the
health of both realtime feeds is inspectable without reading logs. Drops are
*counted*, not silently skipped: `without_position` (a live vehicle with no
coordinates — impossible to map; has always been 0 on this feed, so non-zero is
an alarm and gets a WARNING line), `without_trip_id`, and `duplicate_trip_id`
(two vehicles sharing one trip_id — the cache is keyed by trip_id so the later
wins; this, not any drop, is why `cached` < `positioned`). Verified 0 vehicles
are lost: a 61-row sweep of route 765 across 12 stops found every GPS-broadcasting
trip shown as a live dot.

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
amber LED-style countdowns (IBM Plex Mono), green pulsing dot = live
prediction, "DUE" flashes under 1 minute. Respects `prefers-reduced-motion`.
Keep this identity when extending.

Badges originally carried each route's official colour from the feed. They no
longer do: colour on this board now means one thing only — *this service has a
live position, and here it is on the map*. A feed colour on a row with nothing
on the map competes with that, and in practice collided with the vehicle
palette (an unrelated pink scheduled service reading like a tracked one).
Anything without a live position is plain white.

## Landing page & cross-region search (2026-07-23)

First load (no stop in the URL) shows **only a logo and the search**: a 🪿
goose (U+1FABF, in the monochrome emoji subset, inked LED-amber) above the
search box. `body.landing` (toggled by `syncChrome()`, present in the markup so
a cold load paints it directly) hides the header/status — but the board/map
split is **not** `display:none`-d: MapLibre never fires `load` in a zero-size
container, so the split is parked off-screen (`position:fixed; left:-200vw`,
still sized, `visibility:hidden`) and the map warms underneath the landing.
Selecting a stop drops the class and the existing `awaiting`-drop resize takes
over. Verified headless: landing → click stop → map draws without reload.

The landing names no region, so search fans out over **every ingested region**
(`/api/regions` now returns `state`: QLD/VIC) — stop-name, nearby and geocode
alike, current region's results listed first. Every stop row shows its state
in the sub-line and its stop glyph (from `route_type`, now returned by
`stops/search` and `stops/nearby` via the all-stops dominant-mode cache)
right-justified at the row's edge. Picking a stop in another region reloads
via `?region=X&stop=Y` (per-network map style/caches make a clean start
correct). Geolocation asks all regions and sorts by distance, so a traveller's
saved region doesn't hide the city they're standing in. The two concurrent
geocode calls are serialised server-side behind `_geocode_lock` to keep the
1 req/s Nominatim promise.

The dropdown can never spill past the bottom of the screen: `fitResults()`
caps `max-height` to the gap between the dropdown's top and the viewport
bottom (`visualViewport` where available, so the phone keyboard counts) and
overflow scrolls inside; re-run on every populate and on resize.

`?q=<text>` deep-links a search (prefills the box and fires it) — added for
headless UI tests, works for sharing too.

## Map view with live vehicle positions — built

A MapLibre map sits under the board, showing the live GPS of the vehicles
running the services listed. Everything is self-hosted; the page makes no
external requests at all.

- `app.py` polls `.../SEQ/VehiclePositions` alongside TripUpdates and keys it
  by `trip_id`. `/api/departures/{stop_id}` returns a `vehicles` array holding
  positions for **only the trips on the board**, so the map can never show a
  vehicle the user has no row for.
- Basemap: a **self-built OpenMapTiles `.pmtiles`** of SEQ (64 MB, maxzoom 14),
  built by the separate Planetiler image in `basemap/` onto the data volume.
  Served by StaticFiles, which answers the HTTP range requests pmtiles.js uses
  to read the archive without downloading it whole.
  **Why self-built OpenMapTiles and not the earlier Protomaps extract:** the
  Protomaps schema models the ocean as the *absence* of the land (`earth`)
  polygon and only fills inland water at some zooms — so the bay/river blinked
  between water and land as you zoomed, and no recolouring, higher-maxzoom
  re-extract, or layer re-ordering could fix it (all three were tried). In the
  OpenMapTiles schema water is a real polygon on every zoom (verified: 14.4% of
  a z10 Moreton Bay tile), so land is the background and water is explicit.
- `/api/config` reports whether a basemap exists; without one the map hides
  and the board works unchanged. CI asserts that path.
- `static/map-style.json` is a hand-written dark style matching the board,
  against OpenMapTiles layer names (`water`, `transportation`, `place`, …); CI
  checks every `source-layer` it references exists in the tileset. The style
  URL carries `?v=N` — bump it when the style changes schema, because browsers
  heuristically cached it before it was served `no-cache`, and a stale style
  against new tiles fails silently layer-by-layer (water happened to render,
  roads and labels didn't; the console shows "Source layer X does not exist").
  `?mapdebug=1` logs per-layer rendered-feature counts to the console from the
  viewer's own browser — the dev harness cannot render MapLibre, so that probe
  is the ground truth for "what is this browser actually drawing".
  Glyphs, MapLibre and pmtiles.js are vendored under `static/vendor/`.

The map auto-fits to the stop plus every tracked vehicle (`fitView`), capped at
zoom 15 so a cluster of nearby vehicles does not dive to street level. It only
moves when it has to — something out of view, or the view much wider than
needed — because re-animating every 15 s poll while someone is reading the
board is worse than a slightly stale frame. Any camera move the app did not
initiate is treated as the user taking over and disables auto-fit until the
next stop selection; `programmatic` is what distinguishes the two, since the
zoom buttons move the map with no DOM event to test for.

Page chrome earns its space. With no stop chosen there is no heading — it would
only read "Next service" — and the search is open. Once a stop is chosen the
heading becomes the stop name with a "Change stop" button beside it, and the
search folds away; that button, or the X in the search itself, toggles it back.
`syncChrome()` owns all of it, so there is one place to reason about which of
those three states is showing.

Layout: board and map sit side by side above 900px and stack below it. The map
column is sticky so it stays in view against a long board.

**Colour identifies a service, and nothing else.** Every departure gets one —
not just the tracked ones — carried by the row stripe, the badge glyph and
number, the countdown, and (where there is a live position) the map marker. It
deliberately does *not* encode live-vs-scheduled: that status flips as feeds
come and go, and a colour that changes underneath the reader is worse than no
colour. Where the arrival figure came from is shown separately, by 🛜 realtime
or 📅 timetable after the number.

The palette is twelve hues at 30° spacing. **Assignment is sticky**: a trip_id
keeps its colour in `colorMemory` (insertion-ordered, capped at 300, oldest
evicted) so it survives refreshes, the board advancing, and the service gaining
or losing a live position. A returning trip gets its original colour back if
nothing on screen has taken it meanwhile.

New services take the free colour *furthest around the wheel* from those
already on screen, rather than the next index along — so an arrival is as
distinct as the remaining palette allows. Two earlier versions were worse:
assignment by `trip_id` hash over a fourteen-colour palette gave unique entries
that still looked alike (three pinks, two purples), and index-striding gave good
separation but recoloured every row whenever the board advanced.

**Route paths** come from GTFS `shapes.txt`, ingested into a `shapes` table with
`trips.shape_id` joining them. Each tracked vehicle carries a `shape_id`, and
the geometry is served separately by `GET /api/shape/{shape_id}` — cached a day,
and cached again in the client by id. It is deliberately *not* on the departures
response: the geometry never changes, and inlining it put 174 KB on every 15s
poll for a station board versus 30 KB without. **No route is drawn until a service is picked.** Clicking a board row or a map
vehicle traces that one route, in that service's colour; clicking it again, or
clicking empty map, clears it. Every departure carries a `shape_id` — not just
the tracked ones — so a scheduled service can be traced too.

Drawing them all at once was tried and abandoned. Vehicles routinely share a
shape (seven on one Cityglider path), so identical polylines stacked and only
the last colour ever showed. Splitting shared paths into alternating coloured
runs fixed the invisibility but read as noise. One route on demand answers the
question you actually have — *where is this service going?* — and the whole
alternating-run machinery went with it.

**Landmarks are the stops themselves, always grey**, and appear only for the
selected service — `GET /api/trip-stops/{trip_id}`, fetched on selection and
cached alongside the shape. They were previously on the departures response for
every tracked service at once: 191 grey pins on a station board, resent every
15s, and clutter rather than context. The one stop being viewed is white and
always shown. Nothing about a landmark encodes a service, so no landmark ever
takes a service colour.

The route badge is a split plate — mode glyph | route number — on black with a
white border and divider, both halves inked in the vehicle's colour, as is the
countdown. The glyphs are real emoji (🚆 rail, 🚍 bus, 🚊 tram, ⛴ ferry) drawn
in **Noto Emoji, the monochrome family** — not Noto Color Emoji, which paints
its own palette and ignores CSS `color`. Self-hosted, subset to just those four
codepoints (2.7 KB) via the Google Fonts `text=` parameter.

The map cannot use the same trick: MapLibre renders label text from pre-built
glyph PBFs and ours cover latin only, so an emoji codepoint would not render at
all. Instead `ensureVehicleIcon` rasterises the glyph to a canvas in the
vehicle's colour and registers it with `map.addImage`, one image per
(glyph, colour) pair, referenced by `icon-image`.

Two traps in that rasterising, both silent:

- **Canvas gets the font via an explicitly constructed `FontFace`**, not the
  stylesheet family. Going through the `@font-face` rule means depending on it
  having been parsed and matched when we rasterise; when it has not, canvas
  falls back to the system colour-emoji font and bakes a full-colour glyph into
  the cached image. This was observed, not theorised.
- The four glyphs are astral codepoints, and canvas font matching need not
  honour the rule's `unicode-range`. The JS-constructed face carries no range,
  so the question does not arise.

`live` and a *live* map marker are different things: `live` (🛜) means
TripUpdates gave a time prediction, a solid marker means VehiclePositions gave a
GPS fix. These are two independent feeds. Plenty of services have the first and
not the second — verified: swept 26 stops, and every "live-but-no-dot" row was
genuinely absent from the raw VehiclePositions feed (0 were present-but-dropped),
so it is a coverage split, not a matching bug. The trip_id namespaces of the two
RT feeds and the static schedule are identical.

### Ghost markers — schedule-estimated positions

Because a trip can be on the board with no GPS, the map would show far fewer
markers than the board has rows (e.g. 2 dots for 10 arrivals), which read as a
bug. So for every shown trip *without* a live position, the backend dead-reckons
one from the timetable: `estimate_ghost_positions()` anchors the trip's clock to
the board departure we already resolved (`midnight = board_scheduled −
seconds_into_day(board_hms)`, correct across the 24:00 boundary and for either
service date), then `_interpolate_along()` linearly interpolates between the two
scheduled stops that bracket *now*. A not-yet-departed trip is placed at its
origin only if it leaves within `STAGING_WINDOW_S` (10 min) — "staging to start";
earlier than that it has no position at all. A run that has departed is placed
en route; one past its last stop is gone.

**The board shows exactly what the map can place.** This is the key invariant:
`/api/departures` computes a position for every candidate first (live GPS, else
the timetable estimate), keeps only the ones it can place, and *then* cuts to
`MAX_RESULTS`. A service the map cannot show — a run whose bus has not started,
still finishing an earlier trip under another trip_id that this feed gives no way
to follow — is listed on neither the board nor the map. So the two never
disagree, and a service near the time boundary moves in and out of *both*
together. `vehicles` and `ghosts` are then just the shown set split by whether
the position is a live fix or an estimate.

`STAGING_WINDOW_S` is the single "is it underway?" knob. It barely affects busy
hubs (they have far more than `MAX_RESULTS` trackable services at any window) —
it only sets how sparse a route-*origin* stop looks, where most listed services
haven't started. Widen it to list further ahead, at the cost of drawing
not-yet-moving buses guessed onto their origins (the 301440 pile: arrivals 55,
70, 85 min out all "parked" at the origin was the failure that a tight window
prevents). A trip can read 🛜 *live* (TripUpdates predicts a time) while its bus
has not left the origin, so `live` on a row never by itself means "mappable".

The frontend draws them on their own `ghosts` source in dedicated layers —
`ghost-halo` (a hollow ring), `ghost-dot` (the same colour/emoji as the live
marker but `icon-opacity: 0.5`), `ghost-label` (`~N min`) and `ghost-ping` — all
below the real vehicle layers and the viewed stop, because an estimate must
never outrank a fix. Clicking one, or its board row, selects/pings it just like a
live vehicle; the popup says "estimated from timetable (no live GPS)". `fitView`
frames ghosts too, so the board and map finally show the same set. The grey
route landmarks are clickable: each carries its `stop_id`, and a click calls
`selectStop()` — picking a stop off the traced route is the same as searching
for it. Route landmarks (`landmarks` layer) are the selected route's stops —
stations included, no special layer — `icon-allow-overlap: false` so a
densely-stopped route thins out.

Train stations are **not** treated specially any more (they briefly had an
always-on `rail-stations` layer; retired). They are stops like any other: on
the `all-stops` layer past zoom 15, tagged rail in the payload (parent-station
records carry no stop_times, so `all_stops()` tags them from `rail_stations()`
instead of the dominant-mode lookup) so they draw 🏫; `/api/…/rail-stations`
still exists for compatibility but the frontend no longer calls it.

Emoji in the DOM are written with a trailing **U+FE0E (VARIATION SELECTOR-15)**
via `asText()`, and their elements set `font-variant-emoji: text`. These
codepoints default to *emoji presentation*, and a browser hands those to the
system colour-emoji font regardless of `font-family` — so on a machine with
Noto Color Emoji installed the monochrome face is ignored and the glyph comes
out full colour. U+FE0E asks for text presentation, which lets the font stack
apply. Canvas does its own shaping and needs neither, so the map icons use the
bare codepoint.

Note this cannot be reproduced in a container with no system emoji font
installed: with nothing to fall back to, the webfont wins and everything looks
correct. Check on a real desktop.

The `@font-face` for Noto Emoji carries **no `unicode-range`**, on purpose. The
file is already subset to exactly the glyphs used, so a range is just a second
list to keep in sync — and it fell out of sync twice, silently dropping the
browser back to the colour-emoji font for any codepoint added to the subset but
forgotten in the range.

**Regenerating the subset** (to add a glyph): fetch a fresh subset from the
Google Fonts `css2?family=Noto Emoji&text=<all glyphs>` API (a browser
User-Agent gets woff2), swap in the woff2, and update the codepoint list in the
`fonts.css` comment. The page, `fonts.css` and the `.woff2` are served
`Cache-Control: no-cache` (a middleware in `app.py`), so a regenerated subset is
picked up on the next reload rather than a stale cached font dropping the new
glyph to colour. `fonts.css` and its `?v=` on the woff2 exist to dislodge caches
predating that header; bump `?v` if you ever suspect a browser is still stuck.

Two bugs worth not reintroducing:

- **Do not construct the Map inside a hidden container.** MapLibre measures
  its container at construction; `display:none` gives it zero size and it then
  never fires `load`. Reveal `#map-wrap` *before* `new maplibregl.Map()`. This
  is also why the "search for a stop" state covers the map with an opaque
  `.map-empty` overlay rather than hiding it: the map is built and loading
  underneath at a reduced height, so it draws immediately once a stop is
  picked. `selectStop` drops the `awaiting` class and calls `map.resize()`.
  Before a stop is chosen the board and map show a matched pair of empty-state
  panels: same size (a shared `.placeholder` at `--ph-height`, equal-width
  columns), each with a line of text over a monochrome glyph naming its job —
  🕰 U+1F570 for arrivals, 🌏 U+1F30F for the map. Those two codepoints were
  added to the `NotoEmoji-mode.woff2` subset so they tint grey like the other
  glyphs rather than falling back to a colour-emoji font.
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
