# Journey planner — design

Jason, 2026-07-28: *"proper journey planner. at bare minimum we need the
ability to optionally ask for a destination and enter 'journey planner mode'.
that will be a lot so theres probably a lot of extra planning and thinking to
do here."* This is that planning and thinking. Nothing here is built yet;
build in the phase order at the bottom.

## What it is

The board today answers "what leaves *here* next?". Planner mode answers
"how do I get *there*?" — the user optionally adds a destination, and the
board becomes an itinerary list (legs, transfer walks, times) while the map
traces the whole journey. No destination set → exactly today's app; the
planner must be additive, never in the way.

Design points that carry over unchanged and are non-negotiable:

- **Self-hosted, no external requests.** No OTP instance, no routing SaaS.
  The planner is a query over the SQLite timetables we already have, plus
  the realtime caches we already poll.
- **Live-first.** A paper-timetable itinerary that realtime says is broken
  is worse than none. Predictions re-cost every itinerary on the 15 s poll.
- **The board and the map always agree.** An itinerary's legs are the board;
  the same legs are the trace on the map.

## Algorithm: RAPTOR

**Recommendation: RAPTOR** (round-based public transit routing), one region
at a time, over in-memory arrays built from the region's SQLite.

Why RAPTOR over the alternatives:

- **Connection Scan (CSA)** is simpler (one sorted array of connections,
  one linear sweep) but natively answers "earliest arrival" only. Riders
  also care about transfers: the 2-minutes-faster itinerary with two extra
  changes is the wrong answer. RAPTOR's rounds *are* the transfer count —
  round k holds the best arrival using k−1 transfers — so the
  time-vs-transfers pareto set falls out for free. That pareto set is the
  product: "18:04 direct, or 17:51 with a change at Roma Street".
- **Dijkstra over a time-expanded graph** needs a graph build we'd have to
  invalidate on every weekly re-ingest, and is slower anyway.
- **OpenTripPlanner / Valhalla / external APIs** violate the self-hosted
  rule and drag in a JVM or a routing daemon for what is, per region, a
  sub-100 ms array scan.

RAPTOR needs no preprocessing beyond grouping trips into "routes" (same
stop sequence) and sorting trips within a route by departure — both cheap,
both derivable straight from `stop_times`. Cap at 4 rounds (3 transfers);
past that nobody trusts the itinerary anyway.

## Data model and scale

RAPTOR wants, per region and per **service date**:

- routes → ordered stop lists (grouped by identical stop sequence)
- route × trip → arrival/departure time arrays (int32 seconds, the
  after-midnight 25:xx encoding kept as-is — it just works as seconds)
- stop → routes-serving-it index
- footpaths (see Transfers)

Filter to the service date's active trips (`active_service_ids` already
exists) — this is what keeps Melbourne feasible. Measured 2026-07-29:

| region | stops | trips (all dates) | stop_times (all dates) |
| --- | --- | --- | --- |
| mel | 28,114 | 312,792 | 11,946,153 |
| syd | 39,258 | 179,003 | 5,139,388 |
| seq | 13,090 | 101,361 | 2,690,530 |
| per | 14,868 | 57,988 | 1,682,626 |
| ade | 9,167 | 26,269 | 984,505 |
| nsw | 1,180 | 58,082 | 661,780 |
| qld | 4,010 | 8,870 | 257,631 |
| dar | 898 | 3,919 | 85,686 |

A single service date is roughly a fifth to a tenth of the all-dates rows
(feeds span weeks of calendar), so worst-case mel is ~1.5–2.5 M stop_times
× ~12 bytes ≈ **20–30 MB in RAM, transient build ~1–3 s** — build lazily on
the first plan request for (region, service_date), keep the last two dates
(today + yesterday for after-midnight trips), drop older. Watch the VPS:
it shares RAM with inventoryquest; if mel planning ever matters there,
measure before enabling every region at once. Invalidation: stamp the
cache with the DB file's mtime — the weekly `--region all` swap changes it,
next plan request rebuilds. (Same staleness discipline as `sibling_stops`'s
lru_cache, but planner arrays MUST rebuild because trip ids rot — see the
HANDOVER bullet on timetable rot.)

## Transfers

GTFS `transfers.txt` is not ingested, and half our feeds don't ship one.
Plan:

1. **Ingest `transfers.txt` when present** (new ingest step, trivial table).
2. **Generate footpaths as fallback**: stops within ~200 m straight-line
   (use the existing haversine; ×1.3 detour factor, 80 m/min walk speed,
   minimum 2 min). Precompute at planner build, not per query. ~200 m keeps
   the footpath count linear-ish; it's the "cross the road / walk to the
   other platform" radius, not a walking-network substitute.
3. **Parent stations**: child platforms of one parent get a flat 3-min
   transfer (Central's 20+ platforms are not 200 m apart pairwise, but you
   do change between them).
4. **Merged-station siblings**: the region-silo fix (HANDOVER 2026-07-29)
   already knows `t:200060` = `m:200060` = `nt:200060` (TSN family) and the
   cross-state `STATION_LINKS`. Same-TSN stops are a 3-min transfer *within*
   a region's planner (t: platform → m: platform at Central). Cross-region
   they are the **border points** for phase 3.

RAPTOR handles footpaths as a relaxation step after each round — standard.

## Realtime

Two stages, shipped separately:

1. **Re-cost (phase 1)**: plan on the schedule, then apply the region's
   TripUpdate cache (`STATE[region]["rt"]`, delay-propagation rules already
   implemented for the board) to every leg. Each leg shows 🛜/📅 exactly
   like board rows. If a delay breaks a transfer (predicted arrival + walk
   > next leg's predicted departure), badge the itinerary "connection at
   risk" and offer one-tap replan. Re-costing is cheap and runs on the 15 s
   poll like everything else.
2. **Realtime-aware search (phase 2)**: apply the delay lookup *inside* the
   RAPTOR scan (adjust each trip's times as its route is scanned), so the
   planner routes *around* the delay instead of reporting it. Skipped stops
   (`skipped` in the rt cache) make a trip unboardable at that stop.

Alerts: legs inherit the board's alert matching (route + stop), so a ⚠ on
a leg is the same machinery, namespaced ids and all.

## API

```
GET /api/r/{region}/plan?from={stop_id}&to={stop_id}[&at={epoch}][&arrive_by=1]
```

Response: up to ~4 pareto itineraries, each
`{legs: [{trip_id, region, route, route_color, headsign, board: {stop_id,
name, time, platform}, alight: {...}, shape_id, realtime, alert_ids}],
walks between legs, total_minutes, transfers}`. Legs carry `region` and
`shape_id` for the same reasons board rows now do — the client fetches
shapes/trip-stops from the leg's own region, machinery that already exists.
`from` defaults to the currently-viewed stop; `to` accepts anything stop
search returns (search already spans regions — a cross-region `to` is
rejected politely until phase 3: "cross-city planning not yet").

No new pollers, no new background state: a plan is computed on request from
data already in RAM (+ the lazily built timetable arrays).

## UI

- **Entry**: a "to…" affordance next to the stop title (and in search: pick
  a stop as destination instead of as origin — long-press / secondary
  action). Setting it enters planner mode: URL gains `&to={stop_id}` (so
  it's shareable/bookmarkable, consistent with `?region=&stop=`).
- **Board in planner mode**: itinerary cards replace departure rows — legs
  stacked with the same route pills, colours and 🛜/📅 marks the board
  already uses, walk rows between legs ("↳ 3 min walk to Platform 16").
  Next 2–4 pareto options, soonest first. Clear ✕ exits back to the plain
  board.
- **Map**: selected itinerary traces all legs (the route-tracing layer
  already draws one shape; a multi-leg trace is a FeatureCollection of the
  legs' shapes in their route colours), vehicles/ghosts for the legs' trips
  shown live.
- **Timeline** (the other TODO): once planner mode exists, tapping a stop
  in a traced service's timeline = "plan me to there". Same-vehicle
  zero-transfer itineraries make the timeline the planner's simplest case,
  and the timeline becomes the natural destination picker.

## Phases

- **Phase 0 — prerequisite: region-silo merge.** DONE 2026-07-29.
- **Phase 1 — single-region, schedule-based**: timetable arrays + RAPTOR +
  generated footpaths + `plan` endpoint + itinerary UI + realtime re-cost
  badges. **DONE 2026-07-30** — `planner.py` (routing core) +
  `/api/r/{region}/plan` (enrichment + TripUpdate re-cost + at-risk
  flags) + planner mode in the UI ("Plan a trip" → destination via
  search or address, itinerary cards, legs traced stop-to-stop on the
  map, `?to=` in the URL). Build cost measured: seq ~4 s, mel ~13 s,
  once per region-day, cached against DB mtime.
- **Phase 2 — realtime-aware search**: delays inside the scan, skipped
  stops, connection-at-risk replan, arrive-by queries.
- **Phase 3 — cross-region**: TSN siblings + STATION_LINKS as border
  transfers; run RAPTOR over the union of two regions' arrays (the XPT →
  Southern Cross → tram case). Timezone discipline: all math in epoch
  seconds (already true), display per leg in that leg's region tz.
- **Phase 4 — timeline integration**: timeline taps as destination picks
  (see TODO).

## Edge cases to keep honest

- After-midnight trips: plan across the two service dates exactly like the
  board (today + yesterday's 25:xx trips).
- Sparse networks: a Broken Hill coach runs once a day — the planner needs
  a search horizon of hours (LOOKAHEAD_MINUTES=90 is a board concept; the
  planner scans to end-of-service-day), and must say "tomorrow 07:15" when
  today is done, not "no route".
- Out-of-service ghosts: `_out_of_service` filtering applies to planner
  trips too — don't route passengers onto a deadhead.
- Board==map: an itinerary leg whose trip can't be placed still *lists*
  (unlike board rows — you plan future legs that haven't started), but only
  currently-running legs draw vehicles. This is a deliberate, documented
  exception to the board==map rule: a plan is about the future by nature.
