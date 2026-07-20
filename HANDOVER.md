# Handover: Translink "Next Service" Board

Context document for continuing development. This project was built and
unit-tested in claude.ai (July 2026). Drop this file in the project root —
if you rename it `CLAUDE.md`, Claude Code loads it automatically at the
start of every session.

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
`ingest_gtfs.py` (the feed changes roughly weekly; a weekly cron is fine).
The schema is dropped and rebuilt on every run, so schema changes never
need migrations.

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

## Testing status — important

All logic was verified with **mock GTFS data and synthetic protobuf
feeds** only; the build sandbox could not reach Translink's servers. The
first run against the real feed is where surprises may appear. Check:

- `ingest_gtfs.py` against the real SEQ_GTFS.zip (~hundreds of MB
  unzipped; ingest is batched but confirm memory/time are acceptable)
- Real `trip_id` values match between static feed and TripUpdates
- Real train stations resolve correctly (search "Central", pick the
  station, confirm platforms merge and platform numbers display)
- Route colors/badges for real train lines (long names use `.badge-wide`)

## Frontend design intent

Styled after Brisbane busway passenger information displays: dark board,
amber LED-style countdowns (IBM Plex Mono), route badges in each route's
official color from the feed, green pulsing dot = live prediction, "DUE"
flashes under 1 minute. Respects `prefers-reduced-motion`. Keep this
identity when extending.

## Agreed next-step ideas (not yet built)

- Service alerts for the selected stop (Alerts feed)
- Map view with live vehicle positions (VehiclePositions feed)
- Multiple pinned stops (home + work) on one screen
- Filter by route or direction
