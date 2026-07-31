# Design decisions & guardrails

Distilled from HANDOVER.md (retired 2026-07-31 — the full build diary is in
git history) plus later work. This file holds only what is still **binding**:
decisions that must not be accidentally undone, traps that were hit once and
must not be re-hit, and options that were investigated and ruled out. How the
code works today belongs in README.md and the code itself, not here.

## Timetable & departures correctness

- **Parent stations**: train stations and multi-pontoon ferry terminals are
  GTFS parent records; departures live on child platform stops. The
  departures endpoint expands a parent into its children and merges. Realtime
  matching uses each departure's own platform `stop_id`, never the parent's.
  Search hides children and shows the parent once.
- **After-midnight trips**: GTFS times can exceed 24:00, so the endpoint
  queries today's AND yesterday's service dates. That makes a trip running on
  both dates produce two rows, and an absolute realtime arrival overwrites
  *both* copies — the endpoint keeps the copy whose schedule is closest to
  the prediction. CI guards this dedupe.
- **Timezone**: GTFS times are in the *agency's* zone, pinned explicitly —
  a naive datetime under a UTC container put every time 10 h out and showed
  last night's trains as this morning's. `tzdata` is a runtime dependency
  (the slim base image ships no zoneinfo).
- **Graceful degradation**: if a realtime poll fails, cached data is kept and
  the board falls back to scheduled times. The board must never go blank
  because a feed hiccupped.
- **Realtime rules**: SKIPPED stops are dropped; a prediction uses the feed's
  absolute time if present, else scheduled + delay.
- **Static timetables ROT, fast**: TfNSW encodes the timetable version in
  Sydney train trip ids and week-dates in ferry ids, so realtime matching
  decays mode-by-mode within days against a stale static DB while
  `/api/feeds` still reports healthy. Diagnose with the trip **match rate**,
  not feed health. The weekly ingest timer runs `--region all` for exactly
  this reason. (This is also why the planner cache is keyed on DB mtime.)
- **The board shows exactly what the map can place** — a service is listed
  only if it has a live GPS fix or a timetable-estimated position. The two
  views never disagree. `STAGING_WINDOW_S` is the single "is it underway?"
  knob; widening it parks not-yet-moving buses on their origins.

## Regions

- **Overlapping regions and the pan hand-over**: several regions overlap
  geographically (qld towns near Brisbane; nsw spans four states). While a
  stop is anchored, the map hands over to another region only when the move
  is the *user's own* (`movestart` carried an `originalEvent`; programmatic
  fits never do) AND the centre has left the anchored stop more than
  REGION_RADIUS_KM behind. Both halves are load-bearing.
- **Keep overlapping regions' centres well apart**: browse hand-over picks
  the nearest region centre, so nsw's centre is **Dubbo, deliberately not
  Sydney** — a Sydney centre would steal southern-Sydney browsing from syd.
- **Geocode viewboxes overlap — never trust "the region whose lookup found
  it"**: an address's state comes from the geocoder's own address details,
  and address clicks ask *every* region for nearby stops (distance sorts).
  The region stamp once labelled a QLD street "NSW" and reported no stops
  within 100 km.
- **Merged station boards (2026-07-29)**: one physical station can exist in
  several feeds (TfNSW reuses TSNs across syd/nsw; `STATION_LINKS` curates
  cross-state interchanges like Southern Cross). Boards union every
  sibling's rows, then dedupe by (region, trip, stop) AND by physical
  service — TfNSW publishes the Hunter line in two feeds with different
  trip ids, so (platform TSN, route, scheduled minute) is one train; the
  copy with realtime wins.
- **Feed quirks**: Transperth space-pads its CSV *headers* (fieldnames are
  stripped once at load, or every `row.get()` misses); NT pads lat/lon
  *values* with spaces.
- **G-NAF over Nominatim for house numbers**: OSM's residential house-number
  coverage is patchy — Nominatim pinned "397 Christine Avenue" to an
  arbitrary point along a 3 km road. Numbered queries answer from the local
  G-NAF build; a number G-NAF lacks returns the *nearest* number on that
  street, honestly labelled. Nominatim stays as fallback only.
- **Nominatim is a fair-use community service**: identified UA, server-side
  1 req/s lock, 24 h cache, explicit user action only. Keep it that way.

## Frontend identity

- Styled after Brisbane busway passenger information displays: dark board,
  amber LED countdowns (IBM Plex Mono), "DUE" flash under a minute, respects
  `prefers-reduced-motion`. Keep this identity when extending.
- **Colour identifies a service, and nothing else.** Sticky assignment
  (`colorMemory`), new services take the free hue furthest around the wheel.
  It deliberately does NOT encode live-vs-scheduled (that flips as feeds come
  and go); source is shown separately by 🛜/📅. Feed-supplied route colours
  were removed on purpose — an unrelated pink scheduled service read like a
  tracked one.
- **One route on demand.** Drawing every route at once was tried and
  abandoned: vehicles share shapes, identical polylines stacked, and the
  alternating-colour fix read as noise. Landmarks (a route's stops) are
  always grey — nothing about a landmark encodes a service.
- The planner speaks the arrivals board's visual language: same badge
  plates, vehicle colour on text (never borders), monochrome source marks.
  Walk legs are legs of a "walk vehicle" (🚶), not a different species.

## Emoji & fonts (all of these were hit, not theorised)

- DOM emoji get a trailing **U+FE0E** via `asText()` plus
  `font-variant-emoji: text`, or machines with a system colour-emoji font
  ignore the monochrome webfont entirely. Canvas does its own shaping and
  uses the bare codepoint.
- The Noto Emoji `@font-face` carries **no `unicode-range` on purpose**: the
  file is already subset, and a range is a second list that fell out of sync
  twice, silently dropping new glyphs to colour.
- Map icon rasterising constructs the font via an explicit `FontFace` —
  relying on the stylesheet rule bakes a colour glyph into the cached canvas
  when the rule hasn't been matched yet.
- **Regenerating the subset** (to add a glyph): fetch from the Google Fonts
  `css2?family=Noto Emoji&text=<all glyphs>` API with a browser UA, swap the
  woff2, update the codepoint list comment in `fonts.css`, bump its `?v=`.
  The colour-fallback failure cannot be reproduced in a container with no
  system emoji font — check on a real desktop.

## Map traps

- **Never construct the Map inside a hidden container** — MapLibre measures
  at construction; zero size means `load` never fires. The landing parks the
  split off-screen (`position:fixed; left:-200vw`), never `display:none`.
- **`new pmtiles.Protocol({metadata: true})`** — without the flag the style's
  TileJSON request never settles and the map hangs with no error anywhere.
- Headless Firefox cannot render MapLibre (no GPU → no `load`). `?mapdebug=1`
  logs per-layer rendered-feature counts from the viewer's own browser —
  that is the ground truth for "what is this browser drawing".
- `map-style.json` carries `?v=N` — bump it on schema changes; a stale cached
  style against new tiles fails silently layer-by-layer.
- **Why self-built OpenMapTiles, not the earlier Protomaps extract**: the
  Protomaps schema models ocean as the absence of land and the bay blinked
  between water and land while zooming; no restyling could fix it. In
  OpenMapTiles water is a real polygon at every zoom.

## Deployment gotchas (the workflow itself is in README.md)

- **Never health-check via localhost on the VPS**: pasta (rootless podman's
  port forwarder) has stopped answering published ports on 127.0.0.1 from
  the host while the public interface served fine — it killed a whole run of
  deploys against a healthy board. Remote checks probe `hostname -I`.
- State-file component lines are single-line YAML maps ON PURPOSE — the
  deploy scripts rewrite them with sed. Keep the format; no double quotes
  inside reasons.
- **Flip a component to pending only when the change can alter behaviour**
  (code, markup, styles, dependencies, Containerfile). Comment/doc-only
  edits inside shipped files don't warrant a flip or a redeploy — the
  nightly auto-update or the next real change carries them along
  (2026-07-31, after a pointless restart over two comment edits).
- A pending image can fix itself overnight (the VPS also pulls daily via
  podman-auto-update); pending means "not yet confirmed on the target".
- The live quadlet units carry injected keys, so update-vps.sh must never
  overwrite them; deploy.sh merges repo units while preserving injected
  `Environment=` lines.
- Releases don't re-ingest existing timetables (the weekly timer owns
  freshness) and skip copying basemaps the target already has — a failed run
  must not cost a 640 MB upload twice.
- Keys live in `~/.config/translink/keys.env` (chmod 600), sourced by every
  key-taking script — keys stay out of shell history and session logs.
- **This VPS is shared with `~/Projects/Java2026/inventoryquest`**: that
  project owns host provisioning and port 8080; this app uses 8000. Check
  both before anything host-level.

## Data files

- **Patch live SQLite by copy → patch → `PRAGMA integrity_check` →
  `os.replace`, never in place.** The places index was found silently
  corrupt on 2026-07-31 (double-referenced btree pages, ~36k rows lost,
  ~20k duplicated) after the early era of in-place patches; it kept
  answering queries the whole time, so only integrity_check caught it.
  Check integrity before shipping any patched DB to a target.
- **Suburbs come from the nearest G-NAF address** — for OSM places too,
  outranking the mapper's own addr:suburb tag (Officeworks Robina was
  tagged Mudgeeraba) and the old first-wins cell grid (Pizza 1 on the
  Miami/Burleigh Waters line). G-NAF is the authority on locality.

## Ruled out / removed — don't revisit unprompted

- **Bing Maps** (checked 2026-07-21): the free tier retired 30 June 2025 and
  Azure Maps bills tile requests from the first one — no free Microsoft
  option exists. MapLibre + self-built tiles ships instead.
- **TV/D-pad "joystick" mode and the map pan-pad**: shipped briefly, pulled
  the same day at Jason's request (commit f336fe3 and its revert). Don't
  reintroduce.
- **Walking-graph fallback**: if the OSM walk graph ever disappoints, the
  agreed plan is decommissioning InventoryQuest to make room for a real
  routing server (Valhalla) — that trade-off is Jason's call, already made
  in principle; don't propose alternatives first.
