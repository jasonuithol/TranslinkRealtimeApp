# TODO

## Bugs

- ~~**Region silos split same-station services (reported 2026-07-28)**~~ —
  **FIXED 2026-07-29**: boards now union sibling stops across feeds (bare-TSN
  match within TfNSW's syd/nsw family + a hand-curated `STATION_LINKS` table
  for Southern Cross / Roma Street / Adelaide Central Bus), with a
  physical-service dedupe for trains TfNSW publishes in two feeds at once.
  See the 2026-07-29 HANDOVER.md bullet. Left open: the realtime-preferred
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

- **Clean up the home page** — 45 quick-start buttons and counting
  (16 QLD towns under Cairns, 12 WA under Perth); Jason: "all the buttons
  look like a mess from the Yahoo! days". Both the volume and the look
  need work — it's a late-90s link directory right now. Ideas: collapsible
  per-city town stacks, top-N with "more…", group by state, or ditch the
  button wall for something search-first with a handful of featured
  cities. Once done, give TrainLink its extra doors (Canberra, Albury,
  Wagga, Dubbo).
