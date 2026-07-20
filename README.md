# Translink "Next Service" Board

A live departures board for any Translink stop in South East Queensland,
using Translink's open GTFS (schedule) and GTFS-RT (realtime) feeds.
No API key required.

## Setup

```bash
pip install -r requirements.txt

# Download and ingest the static timetable (~2 min, produces gtfs.sqlite3)
python ingest_gtfs.py

# Run the server
uvicorn app:app --reload
```

Open http://localhost:8000, search for a stop (e.g. "Cultural Centre"),
and the board will show upcoming services, refreshing every 15 seconds.
You can bookmark a stop directly: `http://localhost:8000/?stop=000026`.

## How it works

- `ingest_gtfs.py` downloads `SEQ_GTFS.zip` from Translink and loads the
  tables needed for a departures board into SQLite. The feed changes
  roughly weekly — re-run it periodically (a weekly cron job is fine).
- `app.py` polls the GTFS-RT **TripUpdates** protobuf feed every 30s in a
  background task and keeps a `{trip_id: {stop_id: prediction}}` cache.
- `/api/departures/{stop_id}` pulls today's scheduled departures for the
  stop (including after-midnight "25:xx" trips from yesterday's service
  date), overlays realtime predictions where available, drops skipped
  stops, and returns the next services sorted by predicted time.
- Each departure is flagged `realtime: true/false` so the UI can show a
  live indicator vs. "scheduled".

## Feeds used

- Static GTFS: `https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip`
- TripUpdates: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates`

Also available if you want to extend it: `.../SEQ/VehiclePositions` (live
GPS, for a map view) and `.../SEQ/Alerts` (disruptions).

Data © Translink / Queensland Government, published under their open data
terms (CC BY 4.0 at time of writing — check the data.qld.gov.au dataset
page for current licensing and attribution requirements).

## Ideas for next steps

- Show service alerts for the stop from the Alerts feed
- A map view with live vehicle positions approaching your stop
- Multiple pinned stops (home + work) on one screen
- Filter by route or direction
