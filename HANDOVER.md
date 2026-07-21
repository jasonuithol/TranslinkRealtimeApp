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
amber LED-style countdowns (IBM Plex Mono), green pulsing dot = live
prediction, "DUE" flashes under 1 minute. Respects `prefers-reduced-motion`.
Keep this identity when extending.

Badges originally carried each route's official colour from the feed. They no
longer do: colour on this board now means one thing only — *this service has a
live position, and here it is on the map*. A feed colour on a row with nothing
on the map competes with that, and in practice collided with the vehicle
palette (an unrelated pink scheduled service reading like a tracked one).
Anything without a live position is plain white.

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
colour. Where the arrival figure came from is shown separately, by 📶 realtime
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

`live` and a map marker are different things: `live` (📶) means TripUpdates gave
a time prediction, a marker means VehiclePositions gave a GPS fix. Plenty of
services have the first and not the second, so a row can read 📶 and still have
nothing on the map — and, less often, the reverse.

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
