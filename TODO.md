# TODO

## Bugs

- **One stop's icon suppressed near the viewed stop (reported ~2026-07-25)**:
  on the all-stops layer at Varsity Lakes, viewing stop 300051 the station
  marker `place_varsta` (~23 m away) never draws past zoom 15 — it only
  appears when a traced route's landmarks include it. Two fixes did NOT cure
  it: symbol-sort-key priority + cache-busted payload, and
  `icon-ignore-placement: true` on stop-ring/vehicle-dot/ghost-dot. Facts
  established: the stop IS in the `/all-stops?v=2` payload (route_type 2);
  the layer validates and adds in a real MapLibre engine; the failure is
  specific to this icon. Next diagnostic: load `&mapdebug=1` at the Varsity
  view and compare `map.queryRenderedFeatures({layers:["all-stops"]})` vs
  `querySourceFeatures("all-stops")` around that coordinate; also check
  label collision and whether the landmarks-layer twin at the same spot wins
  placement.

- ~~**Region silos split same-station services (reported 2026-07-28)**~~ —
  **FIXED 2026-07-29**: boards now union sibling stops across feeds (bare-TSN
  match within TfNSW's syd/nsw family + a hand-curated `STATION_LINKS` table
  for Southern Cross / Roma Street / Adelaide Central Bus), with a
  physical-service dedupe for trains TfNSW publishes in two feeds at once.
  See the merged-station bullet in DECISIONS.md. Left open: the realtime-preferred
  dedupe arm hasn't been seen live with keys yet — check a keyed board at
  Newcastle Interchange after deploy.

- **Hand-over dead zones render void (noted 2026-07-29)**: the cross-region
  pan hand-over is centre-based (nearest region centre within 150 km), so
  places more than 150 km from *every* centre — Coffs Harbour, Albury,
  Byron Bay — never switch region, and once the camera passes the current
  region's basemap bbox edge the map is blank. The nsw basemap actually
  covers most of these. Fix if it ever matters: fall back to
  viewbox-containment (hand to the region whose geocode viewbox contains
  the centre, smallest viewbox wins) when no centre is within radius.

## Interstate coverage — blocked on data that doesn't exist (yet)

Checked 2026-07-28 while building the `nsw` TrainLink region. None of these
publish open GTFS; re-check occasionally — operators do occasionally see the
light.

- **Greyhound Australia** — no open feed. (The "Greyhound" GTFS on
  Transitland / Mobility Database is the US Greyhound/Flixbus operation —
  don't be fooled twice.)
- **Journey Beyond** (Ghan, Indian Pacific, Overland) — closed booking
  system, no open data. The Overland is the only Melbourne–Adelaide rail,
  so that corridor stays empty until this changes.
- **Firefly Express** (Melbourne–Adelaide–Sydney overnight coach) — nothing
  published.
- **Spirit of Tasmania** — no feed; Tasmania's open data portal is also
  Cloudflare-blocked from this network, so even the Hobart buses are stuck.

## Interstate-adjacent, blocked on a key

- **Canberra / ACT (MyWay+)** — city network to pair with the TrainLink
  Canberra doors (Xplorer 631–636 + the 700-series coaches already serve
  Canberra Station/Airport/Hospital/University). Blocked on registering for
  a MyWay+ API key.

## Big features

- **Journey planner — PHASE 1 SHIPPED 2026-07-30** (single-region
  schedule-based RAPTOR with realtime re-cost; see PLANNER.md for what
  remains: realtime-aware search, cross-region, timeline integration).
  Original brief follows.
- **Journey planner (added 2026-07-28, design fleshed out 2026-07-29)**:
  bare minimum — the board optionally takes a *destination* and enters
  "journey planner mode" instead of just listing departures. **The full
  design now lives in `PLANNER.md`**: RAPTOR per region over lazily-built
  day-filtered timetable arrays (mel ≈ 20–30 MB / 1–3 s build, measured),
  generated ~200 m footpaths + parent-station + TSN-sibling transfers,
  realtime re-cost first then realtime-aware search, `/api/r/{r}/plan`
  endpoint, itinerary-card board + multi-leg map trace, phased 1–4
  (single-region schedule-based → realtime-aware → cross-region via
  STATION_LINKS → timeline integration). Phase 0 (region-silo merge) is
  done — the silo bug fix above was its prerequisite.

- **Make the route timeline interactive (added 2026-07-28)**: the
  timeline/timetable overlay you get when tracing a service is currently
  display-only. Once journey planner mode exists it would be ridiculous
  for it not to be a destination picker: you're looking at the exact
  sequence of stops this service makes — tapping one should mean "plan me
  to there" (and short of full planner mode, even just "show arrival time
  at that stop" / "jump to that stop's board" would earn its keep today).
  Natural first taste of the planner UI: same vehicle, zero transfers.

## UI

- **Home page declutter — phase 1 done 2026-07-29**: all 45 quick-start
  buttons removed at Jason's call ("just get rid of all the buttons for
  now") — the landing is now the goose + search + near-me, search-first.
  The curated stop ids the buttons held (each city's central station and
  favourite interchanges, 18 QLD towns, 12 WA towns, TrainLink's
  `nt:200060`) live in git history at `static/index.html` @ 6a35483,
  `CITY_JUMPS` — mine that list if a future design wants featured cities
  back. Still open: whether the landing wants anything more than search
  (recent stops? a couple of featured cities? state groups?) — that's the
  "visual design" half of the original complaint, to be designed
  properly, not bolted on.

## Older ideas (carried over from the retired HANDOVER.md)

- Multiple pinned stops (home + work) on one screen
- Filter the board by route or direction
