# Google Calendar API — Fetching Events

Pull events from Google Calendar with date/time, name/description, and location (if available). Read-only to start, with an easy upgrade path to write access.

## Auth and Scopes

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Enable the **Google Calendar API** (APIs & Services → Library).
3. Configure the OAuth consent screen (External is fine; add yourself as a test user).
4. Create credentials → **OAuth client ID** → **Desktop app**.
5. Download the JSON and save it as `credentials.json` next to the script.

The scope determines your access level:

| Scope | Access |
|---|---|
| `https://www.googleapis.com/auth/calendar.readonly` | Read-only |
| `https://www.googleapis.com/auth/calendar.events` | Read + create/edit events |

You can start with read-only and swap the scope later — just delete the cached `token.json` and re-run to re-authorize.

## The Data

Events come from the `events.list` endpoint. Each event object includes:

- `summary` — the event name
- `description` — only present if set
- `location` — only present if set
- `start` / `end` — **gotcha:** timed events have `start.dateTime`, but all-day events have `start.date` instead, so check both.

## Install

```bash
pip install google-api-python-client google-auth-oauthlib
```

## Script: fetch_calendar_events.py

First run opens a browser to authorize; the token is cached in `token.json`.

```python
import datetime
import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
MAX_RESULTS = 25


def get_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return creds


def main():
    service = build("calendar", "v3", credentials=get_credentials())

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = service.events().list(
        calendarId="primary",       # or a specific calendar's ID
        timeMin=now,                # only future events; omit for past too
        maxResults=MAX_RESULTS,
        singleEvents=True,          # expands recurring events into instances
        orderBy="startTime",
    ).execute()

    events = result.get("items", [])
    if not events:
        print("No upcoming events found.")
        return

    for event in events:
        # Timed events have "dateTime"; all-day events have "date"
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))

        name = event.get("summary", "(no title)")
        description = event.get("description", "")
        location = event.get("location", "")

        print(f"{start} -> {end}")
        print(f"  Name: {name}")
        if description:
            print(f"  Description: {description}")
        if location:
            print(f"  Location: {location}")
        print()


if __name__ == "__main__":
    main()
```

## Notes

- `singleEvents=True` matters if you have recurring events — without it you get the recurrence rule instead of the actual dated instances.
- To use a calendar other than your main one, call `service.calendarList().list()` once to find its ID and pass that as `calendarId`.
- **Upgrading to write access:** change `SCOPES` to `["https://www.googleapis.com/auth/calendar.events"]`, delete `token.json`, and re-authorize. Creating an event is then:

```python
service.events().insert(
    calendarId="primary",
    body={
        "summary": "Team meeting",
        "location": "Room 3B",
        "description": "Quarterly planning",
        "start": {"dateTime": "2026-08-01T10:00:00+10:00"},
        "end": {"dateTime": "2026-08-01T11:00:00+10:00"},
    },
).execute()
```

Same shape as what you're reading.
