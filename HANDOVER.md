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
volume. The full per-region set is ~1.5 GB (nsw alone is 590 MB), so when
releasing to the VPS name just the rebuilt ones:
`BASEMAP_REGIONS="nsw" ./deploy/release-vps.sh root@…` — the VPS keeps its
existing copies of the others.

On the VPS it is managed by **Quadlet** units in `deploy/`, installed by
`deploy/install-vps.sh` — see that script's header for what it assumes about
the host. `.github/workflows/ci.yml` smoke-tests against `mock_gtfs.zip` and
publishes to `ghcr.io/jasonuithol/translink-departures`; the server container
is `AutoUpdate=registry`, so a push to `main` rolls out on its own.

**Deployment is two commands, driven by state files** (2026-07-29, Jason:
"the deployment logic is a shitshow… I want to run a local deploy, and a
remote deploy and THATS ALL"): `./deploy/deploy.sh local` and
`./deploy/deploy.sh vps` (`--dry-run` prints the plan). Each reads
`deploy/state/<target>.yaml` — one line per component (`image`,
`quadlet-units`, `basemap-<region>`, `enable-syd-nsw`, `enable-mel`,
`https`), each `deployed` / `pending` (with a reason) / `blocked` — and
deploys exactly the pending ones, flipping them back via
`deploy/mark-deployed.sh`. THE RULE: **whoever changes a deployable asset
flips its component to pending in every affected target file, in the same
change** — code → `image`; a bbox or basemap rebuild → `basemap-<region>`;
unit files → `quadlet-units`; env/key wiring → the `enable-*` component.
No knobs: deploy.sh derives BASEMAP_REGIONS etc. from the yaml and calls
the old scripts as machinery. The image is the ONE component that deploys
via GitHub (push → CI → GHCR → pull), so deploy.sh gates it: uncommitted
image inputs (app.py, static/, ingest_gtfs.py, requirements.txt,
Containerfile) abort the run, an unpushed HEAD offers to push, and it
waits for CI to go green before pulling. deploy.sh also closed the gap
where quadlet unit changes had NO deploy path (only first-time
install-vps.sh wrote them): it syncs repo units to the VPS while
preserving the live units' injected `Environment=` lines (keys, feed
URLs) and `PublishPort` — merge logic is a python3 heredoc inside
deploy.sh, tested against fixture files. Keys/host come from
`~/.config/translink/keys.env` (NSW_KEY, VIC_KEY, VPS_HOST; chmod 600;
created by Jason 2026-07-29 in a plain terminal so keys stay out of
session transcripts; every key-taking script sources deploy/load-keys.sh
as arg fallback, and run-local.sh's old `""` skip-arg became `none` since
empty now falls back to the store). Component lines are single-line YAML
maps ON PURPOSE — mark-deployed.sh and deploy.sh rewrite/parse them with
sed; keep the format, no double-quotes inside reasons. Image subtlety:
the VPS also pulls daily via podman-auto-update, so a pending image can
fix itself overnight — pending means "not yet confirmed on the target";
a deploy run confirms. **Never health-check via localhost on the VPS**
(2026-07-29): pasta, rootless podman's port forwarder, stopped answering
published ports on 127.0.0.1 from the host itself while the public
interface served fine — every deploy's verification tail failed against a
healthy board, which was the true cause of the "board did not come up"
serial deploy deaths (NOT slow cache warm-up, though a post-ingest warm
is genuinely slow too). All remote checks now probe
`$(hostname -I | awk '{print $1}')` as PROBE_HOST. Also from that
incident: releases no longer re-ingest existing timetables (INGEST_*
default `auto` = only-if-missing; the weekly timer owns freshness) and
release-vps.sh skips copying a basemap the target already has
(size-compared) so a failed run can't cost the 640 MB nsw upload twice.

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
  per mode (1 = V/Line train, 2 = metro train, 3 = tram, 4 = metro bus,
  5 = V/Line coach; ids only unique within a mode). `ingest_gtfs.py --region
  mel` loads modes 1/2/3/4/5 and prefixes every id `<mode>:`; the RT poller
  config carries the same prefix per feed so realtime ids land on the ingested
  ones (V/Line has its own `vline/*` RT feeds under prefix 1). PTV uses
  Google's *extended* route types (400 = metro rail, 204 = coach, 701 = bus,
  900s = tram) — `normalize_route_type()` collapses them to the basic 0-4 set
  at ingest so rail-station detection, mode emoji and labels all just work.
  PTV also ships the same `vic:rail:*` station registry in every rail mode's
  zip, so a station served by both V/Line and Metro would exist twice after
  prefixing — `merge_shared_stations()` collapses those onto the metro (`2:`)
  parent post-ingest so Southern Cross is one search result boarding both.
  11.8 M stop_times; ~4 min ingest.
- **Melbourne realtime needs a (free) registered key** and the host has moved
  between VIC data portals, so it is entirely env-driven — unset means
  static-only: `MEL_TRIP_UPDATES="2|https://…;3|https://…"` (mode prefix per
  feed), same for `MEL_VEHICLE_POSITIONS` / `MEL_ALERTS`, `MEL_API_KEY`,
  `MEL_API_KEY_HEADER` (default `Ocp-Apim-Subscription-Key`).
- Frontend: region comes from `?region=` / localStorage; all API calls go
  through `api(path)`. There is NO visible region UI (the eyebrow row and ⇄
  switcher were removed when the third region made "the other region" a
  meaningless button): the region changes by *searching* (cross-region
  results) or by *panning the map* into another network. Pan swap: every
  `moveend` (zoom ≥ 7) measures the map centre against each region's home
  city from `/api/regions`; within 150 km of another region's city →
  `switchRegion(id, "lon,lat,zoom")` reloads with the camera in `?at=`.
  Arriving with `at` and no stop is **map-browsing mode**: not the landing —
  the map opens exactly where the user was looking (all-stops clickable,
  board placeholder until they pick one). Programmatic camera moves never
  leave a region, so the check runs unconditionally; regions are ~700 km
  apart so there is no flapping. The map style is one file — the client
  points its `omt` source at the region's pmtiles from
  `/api/r/{region}/config` (`basemap_url`, `center`). Board times render in
  the *region's* timezone (`tz` from config), not the viewer's.
- Basemaps per region: `REGION=mel podman run … translink-basemap` (bbox
  presets in `basemap/build-basemap.sh`). Melbourne DB: `/data/gtfs-mel.sqlite3`
  (`MEL_GTFS_DB` to override); basemap `mel.pmtiles`.
- **Sydney (`syd`, added 2026-07-23 — endpoints key-verified same day: every
  realtime/alerts feed 200s exactly as wired; the one surprise was the trains
  *schedule* zip staying on /v1 while trains realtime is /v2, fixed)**: TfNSW
  publishes one flat "Timetables For Realtime" zip *per mode*, each download
  authenticated (`Authorization: apikey <key>`, free key from
  opendata.transport.nsw.gov.au). `ingest_gtfs.py --region syd` (key via
  `--key`/`SYD_API_KEY`) downloads and merges them with per-feed prefixes —
  `t:` Sydney Trains, `m:` metro, `b:` buses, `f:` ferries, `lw:`/`lc:`/`lp:`
  light rail Inner West / CBD-SE / Parramatta; a source that 404s is skipped
  with a warning (TfNSW moves endpoints between /v1 and /v2). For testing
  without a key it also accepts a *directory* of `<prefix>.zip` files.
  Realtime is `SYD_*` env, same `prefix|url;…` format as Melbourne (generic
  `_env_rt_feeds`); pre-wired URLs in `deploy/translink.container` and
  `deploy/run-local.sh` follow TfNSW's current v1/v2 split (trains/metro/LR-IW
  on v2, rest on v1) — **run `deploy/probe-syd.sh <key>` to verify before
  enabling**, and fix any moved URL in env/ingest, nothing else hardcodes
  them. Known gap: TfNSW's light-rail *alerts* are one feed for all LR lines,
  which cannot map onto three per-line prefixes — LR alerts are not wired.
  Ferry stops draw ☸ (vehicles stay ⛴) — `landmarkGlyph` case 4, glyph in the
  emoji subset since v5. Verified end-to-end against mock per-mode zips:
  ingest → three-region search (NSW rows, ☸/🏫 icons) → ferry board with ⛴
  badges and harbour ghosts on the self-built `syd.pmtiles`.
- **Adelaide (`ade`, added 2026-07-26)**: the easy region — Adelaide Metro is
  SEQ-shaped end to end. One flat public GTFS zip (~18 MB, ids globally
  unique, basic route types already: 0 tram / 2 rail / 3 bus / 4 ferry), and
  public **keyless** GTFS-RT at `gtfs.adelaidemetro.com.au/v1/realtime/`
  {trip_updates, vehicle_positions, service_alerts} — hardcoded in `REGIONS`
  like SEQ's, no env needed anywhere. Watch-outs: the feed is state-wide, not
  metro — country coaches to Ceduna/Port Lincoln and the SEALNK Kangaroo
  Island ferry — so the `ade` basemap bbox covers settled SA (learned from
  the Geelong blank-map bug: the basemap must cover the *network*, not the
  city). Adelaide Railway Station is a single flat stop `6665` (no parent
  station, all 1,679 rail trips terminate there); the tram stop outside is a
  separate parent `50093`. Their portal calls the RT feeds "BETA".
  ~1 M stop_times; DB `/data/gtfs-ade.sqlite3` (`ADE_GTFS_DB` to override).
- **Perth (`per`) and Darwin (`dar`), added 2026-07-26**: the first
  *permanently schedule-only* regions — neither WA nor NT publishes any
  GTFS-RT (NT's Bus Tracker app is live but its backend isn't open data;
  checked 2026-07-26). `REGIONS` entries carry empty feed lists, which the
  existing keyless-Melbourne machinery already understood: pollers stay off,
  `realtime_configured` false, board status reads "timetable only", every
  vehicle is a timetable ghost. Both static zips are public and keyless.
  Watch-outs: **Transperth space-pads its CSV headers** ("stop_id,
  stop_name, …") in stops/shapes/transfers — `load_feed_zip` strips
  fieldnames once, or every `row.get()` misses and the tables load as NULLs.
  The WA feed is state-wide (PTA regional town buses: Broome, Carnarvon,
  Kalgoorlie, Esperance, Albany, plus the Australind train) — ~1.68 M
  stop_times, our biggest DB, and the `per` basemap bbox covers the whole
  populated crescent. Darwin's feed reaches Adelaide River and Kakadu
  (Ubirr); NT pads its lat/lon *values* with spaces (SQLite's REAL affinity
  copes). No parent stations in dar at all; Perth Stn is parent `56`, the
  city terminus in dar is "Darwin Harry Chan Ave" flat stop `46`. Zip URLs:
  `dipl.nt.gov.au/data-feeds/bus-gtfs/google-transit-darwin.zip` (redirects
  to dli.nt.gov.au) and
  `www.transperth.wa.gov.au/TimetablePDFs/GoogleTransit/Production/google_transit.zip`.
- **Regional Queensland (`qld`), added 2026-07-27**: eighteen Translink
  town networks merged into ONE region — Cairns (`cns`), Townsville (`tsv`),
  Mackay (`mky`), Toowoomba (`twb`), Rockhampton–Yeppoon (`rky`), Bundaberg
  (`bun`), Maryborough–Hervey Bay (`mhb`), Gladstone (`glt`), Gympie
  (`gym`), Warwick (`war`), Bowen (`bow`), Whitsundays (`wht`), Innisfail
  (`inn`), Kilcoy (`kil`), Maleny (`mal`), Magnetic Island bus (`mag`) +
  ferry (`mif`), North Stradbroke Island (`nsi`). Each is a small keyless
  zip at `gtfsrt.api.translink.com.au/GTFS/<CODE>_GTFS.zip`, ingested
  syd-style under a `<town>:` prefix (ids are only unique within a town).
  **Five towns have keyless GTFS-RT** (all three kinds, verified live
  2026-07-27): CNS, BOW, INN, MHB, NSI at
  `gtfsrt.api.translink.com.au/api/realtime/<CODE>/{TripUpdates,VehiclePositions,Alerts}`
  — configured with per-feed prefixes exactly like Sydney's, plus
  `req_gap_s: 0.25` to pace the 5-feed bursts against the host SEQ also
  polls. The other towns' endpoints return valid-but-empty FeedMessages, so
  they're left unconfigured; their services show as timetable ghosts.
  ~229 K stop_times; DB `/data/gtfs-qld.sqlite3` (`QLD_GTFS_DB` override).
  Cairns has a real parent station `cns:place_cnsstn` (Cairns City, Lake
  St); most towns' hubs are flat stops (see CITY_JUMPS for the curated
  per-town door stops). Basemap bbox is the whole eastern seaboard strip,
  Cairns to Warwick. **Regions can now overlap geographically** — several
  qld towns (Stradbroke, Kilcoy, Maleny, Toowoomba, Gympie, Warwick) sit
  within 150 km of Brisbane, which made the map's cross-region pan hand-over
  fire off a qld board's own fit-to-stop and dump the user into seq
  browsing mode (the "clicked Stradbroke, lost the goose" bug). First fix
  was a blanket `!stopId` gate — hand-over only while browsing — which
  over-corrected: with a board open (the app's normal state) panning from
  one city to another never switched region, so the map ran off the
  basemap into blank void ("map and route issues again", 2026-07-29,
  aggravated by merged boards offering long-haul XPT/Hunter rows worth
  tracing). Current rule (2026-07-29): while a stop is anchored, hand over
  only when (a) the move is the *user's own* — `movestart` carried an
  `originalEvent`; programmatic fits never do — AND (b) the map centre has
  left the anchored stop more than REGION_RADIUS_KM behind. Fit-to-stop
  stays safe (programmatic), browsing near your own stop stays safe
  (distance guard covers every qld-within-150km-of-Brisbane case), but a
  deliberate drag to another city hands over, dropping the stop and
  carrying the camera — Selenium-verified all three ways plus browsing
  mode. Known remaining gap: dead zones >150 km from every region centre
  (Coffs, Albury, Byron) never hand over and render void once past the
  bbox edge; a viewbox-containment fallback would fix it if it ever
  matters.
- **NSW TrainLink interstate (`nsw`), added 2026-07-28**: the answer to
  "any interstate travel options?" — TfNSW's `nswtrains` feed is the
  regional trains (XPT Sydney–Melbourne and Sydney–Brisbane, Xplorer to
  Canberra, Armidale, Moree and Broken Hill) plus the connecting coach
  network, and it has full GTFS-RT (the trains carry 4Trak GPS). The syd
  ingest comment had explicitly parked it as out of scope; it's now its own
  region rather than part of `syd` because its stops span Broken Hill to
  Brisbane to Melbourne — folding it in would have blown out syd's basemap
  bbox and geocode viewbox. Single source ingested under an `nt:` prefix
  (one feed, but prefixed so the `NSW_*` env realtime config addresses it
  syd-style). **Same TfNSW key as syd** for static AND realtime — the
  ingest reads `SYD_API_KEY`, `enable-syd-vps.sh` writes both regions'
  quadlet env, `probe-syd.sh` probes the nswtrains endpoints (v1 AND v2 —
  TfNSW moves modes between versions; probed live 2026-07-28: TU and VP on
  v1, alerts on v2, exactly as configured — note v1 alerts 401s rather
  than 404s). Basemap bbox `138.4,-38.4,153.7,-27.2` is the biggest tile
  set in the app — most of south-east Australia, INCLUDING Adelaide,
  because the Broken Hill coach terminates at Adelaide Central Bus Station
  (4 feed stops sit in SA). Overlaps syd, mel, seq, ade and qld geographically;
  the `!stopId`-gated moveend hand-over (see the qld bullet) is what makes
  that safe. The region `center` is **Dubbo, deliberately not Sydney**: the
  browse hand-over picks the nearest region center, so a Sydney-CBD center
  would steal southern-Sydney browsing from syd. Keep overlapping regions'
  centers well apart. Home button: single "TrainLink" door at `nt:200060` (Central —
  TSN verified against the sydneytrains/metro feeds, re-check after the
  first real ingest); more doors (Canberra, Albury, Wagga, Dubbo) wait for
  the landing-page declutter.
- **Merged station boards across region silos (2026-07-29)**: Jason's bug —
  "I can see XPT routes or normal sydney routes, but not both. please merge
  them." A board was anchored to one region's DB and pollers, so Central
  showed suburban trains (`syd`) or TrainLink (`nsw`), never both — and the
  intra-syd split was worse than reported: `t:200060`, `m:200060` and
  `lc:200060` are three separate parent stations, so the Central board
  didn't even show Sydney Metro. Fix, server side (`app.py`):
  `sibling_stops()` finds the same physical station in sibling feeds — a
  bare-TSN suffix match across the TfNSW family (`TSN_REGIONS = syd, nsw`;
  TfNSW reuses TSNs across all its feeds) plus a hand-curated
  `STATION_LINKS` table for cross-state interchanges with no shared id
  (Southern Cross `2:vic:rail:SSS`↔`nt:22180`, Roma Street
  `place_romsta`↔`nt:40001`, Adelaide Central Bus `102954`↔`nt:G50001`;
  matched against the anchor stop AND its parent). `departures()` unions
  every sibling board's candidate rows, tags each row/vehicle/ghost with its
  source `region`, then dedupes twice: (region, trip, stop) for
  parent/child sibling overlap, and a **physical-service dedupe** — TfNSW
  publishes the Hunter line in BOTH sydneytrains and nswtrains with
  different trip ids, so (platform TSN, route, scheduled minute) is one
  train; the copy with realtime wins, else the anchor region's. Placement,
  alerts and shape-tagging all run per-region (VP caches and DBs are
  per-region; alert ids are now namespaced `region:index` since each region
  numbers its own alert list from zero). `realtime_configured`/feed ages
  span every merged region — a keyless mel board borrowing live nsw rows
  must not claim "timetable only". Client side (`index.html`): every
  per-trip fetch (shape, trip-stops, trip-times) goes to `row.region` via
  `tripRegion()`/`apiIn()`, not the board's region; traced-route landmarks
  carry their region and `selectStop()` does a full cross-region navigation
  when a landmark belongs to a sibling region (per-region caches make a
  reload correct). `sibling_stops` is lru_cached — the answer only changes
  on re-ingest, and a stale sibling just 404s its skipped board lookup.
  Verified on keyless tpanel: Newcastle Interchange merges Hunter trains +
  TrainLink coach with zero duplicate pairs, tracing the nt: coach from the
  syd board loads its timeline from the nsw API, seq boards byte-identical
  to the old image. NOT yet verified with live realtime keys (the
  realtime-preferred arm of the physical dedupe) — eyeball a keyed tdev or
  the VPS after deploy.
- **Journey planner phase 1 (2026-07-30)**: PLANNER.md's design, built.
  `planner.py` is the pure routing core: a day-filtered in-memory
  timetable per region (today's services + yesterday's after-midnight
  tail shifted -24 h), RAPTOR routes grouped by identical stop sequence,
  generated footpaths (≤200 m, ×1.3 detour at 80 m/min, min 2 min),
  station groups (parent+children, and TSN siblings in the TfNSW family)
  as 3-min transfers, 4 rounds max, sparse-dict tau with pareto
  reconstruction. Cached per region keyed on DB mtime + service date —
  trip-id rot makes a stale plan unmatchable by realtime. Costs: seq
  ~4 s / mel ~13 s to build, sub-50 ms per query after. app.py's
  `/api/r/{region}/plan` enriches legs (route meta, platforms, shape_id)
  and re-costs each board/alight with the SAME TripUpdate rules the
  board uses (exact stop entry, else sequence propagation); a delayed
  arrival that eats the transfer walk flags the itinerary "connection at
  risk". Client: "Plan a trip" in the titlebar → search doubles as the
  destination picker (`pickingDest`; an address destination = its
  nearest stop, same region; cross-region picks are refused politely
  per phase 1) → `?to=`/`&toname=` in the URL, itinerary cards instead
  of departure rows, ride legs drawn STOP-TO-STOP from the cached
  trip-stops response (good-enough geometry — a full shape would draw
  the whole route, not the ridden slice), transfer-point landmarks, a
  green destination Marker, camera fitted once per plan. Known phase-1
  limits: no vehicles on planner maps, planning is schedule-based
  (realtime only re-costs), single region.
- **Walking graph for planner walk legs (2026-07-30)**: Jason asked for
  walks drawn "along the sides of roads" — there is no separate footpath
  dataset; OSM's ways ARE it. `ingest_walkgraph.py` (osmium, run in a
  throwaway container — see its header; osmium + libexpat deliberately
  not in the app image) streams the SAME australia.osm.pbf the basemap
  builder caches, keeps walkable highway types within ~2 km of any stop
  in any region DB (the outback drops out), and writes
  walkgraph.sqlite3: 14.4 M nodes / 31.7 M directed edges / 1.98 GB.
  `/api/walkroute` runs A* straight off the edges index (~18 ms for a
  500 m walk, in-memory LRU for the 15 s polls); the client draws walk
  legs as dashed grey lines along the returned geometry, with a
  straight dash as fallback while loading / where the graph has no
  answer / on deployments without the DB. Ships as `walkgraph-db`
  through ship_data_db() in release-vps.sh (size-guarded — never
  re-copy 2 GB) and install_data_db() in update-vps.sh. Slimming
  headroom if 2 GB ever hurts: BUFFER_CELLS 2→1, or contract degree-2
  node chains into edge geometry. If graph quality ever disappoints,
  Jason's fallback plan is decommissioning InventoryQuest to make room
  for a real Valhalla — don't suggest it first.
- **Local G-NAF geocoder (2026-07-29)**: OSM's residential house-number
  coverage is patchy — "397 Christine Avenue, Varsity Lakes" has no house
  number in OSM, so Nominatim pinned an arbitrary point along a 3 km road
  (Jason: "street numbers don't seem to be taken into account?"). Fix:
  `ingest_gnaf.py` builds `gnaf.sqlite3` (~streets + numbered addresses +
  FTS5 street index) from Geoscape's full quarterly G-NAF PSV zip
  (data.gov.au, ~1.7 GB download; the URL rotates quarterly — the script
  header says how to find the new one). `gnaf_geocode()` in app.py answers
  any query leading with a house number: tokens (street-type abbreviations
  expanded: ave→avenue &c.) FTS-match streets, current region's state
  ranks first, exact number wins, and a number G-NAF lacks returns the
  NEAREST number on that street, honestly labelled — next door beats a
  random mid-road point. Nominatim remains the fallback for landmarks,
  unnumbered queries, and deployments without the DB (absent file =
  exactly the old behaviour). Ships like a basemap: built locally,
  `gnaf-db` component in deploy/state, SHIP_GNAF path in release-vps.sh
  with the size guard, installed atomically by update-vps.sh. Refresh a
  few times a year, not weekly. G-NAF EULA: attribution in README, no
  unsolicited-mail use.
- **Geocode viewboxes OVERLAP — never trust "the region whose lookup found
  it" (2026-07-29)**: nsw's viewbox spans four states, so an address search
  returns the same street from several regions' Nominatim lookups. Clicking
  the nsw copy of a Varsity Lakes QLD address searched only nsw's stops —
  nearest TrainLink stop ~100 km away — and reported "no stops near that
  address" ("that's bullshit" — Jason, correctly). Fixes: address clicks
  pass rid=null to nearbyStops (ask every region, distance sorts); address
  rows dedupe by label client-side; and `_geocode_label` takes the state
  from Nominatim's OWN address details (mapped via _STATE_ABBR), falling
  back to the region's state — the region stamp had labelled that QLD
  street "NSW".
- **Static timetables ROT, fast (found 2026-07-28)**: TfNSW encodes the
  timetable *version* in Sydney train trip ids (`…790.119.…` → `…790.122.…`
  in five days) and week-dates in ferry trip ids, so realtime matching
  decays mode-by-mode against a stale static DB — a July-23 syd ingest had
  trains at 0/556 matched and ferries 8/161 by July-28, while buses (plain
  numeric ids) still matched 5391/5392. Symptom: feeds report healthy
  (thousands of TUs polled) but board rows stay 📅. Diagnose with the trip
  match rate, not /api/feeds. Cure: the weekly ingest timer now runs
  `ingest_gtfs.py --region all` (every region, atomic swaps, one failure
  doesn't sink the rest; keyed regions need SYD_API_KEY in the ingest
  quadlet, which enable-syd-vps.sh writes).
- **TV/D-pad "joystick" input mode — built and REMOVED (2026-07-27)**: an
  arrow-key-cycling input mode with a dwell-to-click map reticle shipped
  briefly and was pulled the same day at Jason's request ("joystick mode
  sucks") — see commit f336fe3 and its revert for the whole apparatus.
  The map's pan-pad control (▲◀▶▼ + centre-on-stop ⚲) was removed along
  with it; the map pans by drag / MapLibre's own keyboard support only.
  Don't reintroduce any of this unprompted.
- **Timezone badge (2026-07-26)**: six networks span three Australian
  timezones, so the titlebar shows the region's clock ("AWST", "ACST", …)
  next to the stop name — derived client-side from the config's IANA `tz`
  via `Intl.DateTimeFormat(…, {timeZoneName: "short"})`, so DST renames
  (AEST↔AEDT) come free. Times were already rendered in the region's zone;
  the badge just says so.

## Keys & credentials needed for full functionality

| What | Status | Unlocks | How |
| --- | --- | --- | --- |
| **VIC open data API key** | ☑ OBTAINED (2026-07-22), not yet deployed | Melbourne realtime: live vehicle dots (incl. train occupancy), realtime arrival times, ⚠ disruption alerts | The "Data Platform API Token" from the portal profile. Auth header is `KeyID` (verified; the OpenAPI specs wrongly say Ocp-Apim-Subscription-Key). Paste into the commented `MEL_*` lines in `deploy/translink.container` — incl. `MEL_API_KEY_HEADER=KeyID` — then daemon-reload + restart |
| **HTTPS on the VPS** | ☐ NOT SET UP (not a key, but gates a feature) | The "near me" geolocation button — browsers block geolocation on plain http (localhost excepted) | Reverse proxy + Let's Encrypt, or Caddy; shared host with inventoryquest, so coordinate ports |
| **NSW open data API key** | ☑ DONE (2026-07-23): obtained, deployed, Sydney fully LIVE in production (all seven feeds clean after the 429-pacing fix) | ALL of Sydney: even the static timetable download is authenticated | Held by Jason (in the VPS quadlet, not the repo). TfNSW throttles per-second bursts — `req_gap_s` + staggered pollers keep under it |

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
- SYD static + realtime: `https://api.transport.nsw.gov.au/{v1,v2}/gtfs/…`,
  ALL keyed (`Authorization: apikey <key>`); exact per-mode URLs live in
  `ingest_gtfs.py` (static) and the `SYD_*` env (realtime) — probe first
- ADE static + realtime: `https://gtfs.adelaidemetro.com.au/v1/…` (keyless)
- PER static: `https://www.transperth.wa.gov.au/TimetablePDFs/GoogleTransit/Production/google_transit.zip`
  (keyless; no realtime exists)
- DAR static: `https://dipl.nt.gov.au/data-feeds/bus-gtfs/google-transit-darwin.zip`
  (keyless, redirects to dli.nt.gov.au; no realtime exists)
- QLD regional static: `https://gtfsrt.api.translink.com.au/GTFS/<CODE>_GTFS.zip`
  (keyless; 18 codes, see ingest_gtfs.py). Realtime for CNS/BOW/INN/MHB/NSI:
  `https://gtfsrt.api.translink.com.au/api/realtime/<CODE>/{TripUpdates,VehiclePositions,Alerts}`
- NSW TrainLink static: `https://api.transport.nsw.gov.au/v1/gtfs/schedule/nswtrains`
  (keyed, same key as SYD). Realtime: `{v1,v2}/gtfs/{realtime,vehiclepos,alerts}/nswtrains`
  via the `NSW_*` env — run `deploy/probe-syd.sh` to confirm the v1/v2 split
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
