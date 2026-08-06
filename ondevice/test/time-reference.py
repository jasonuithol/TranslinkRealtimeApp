"""Epochs for awkward clock times, straight from the server's own function.

gtfs_time_to_epoch is what every scheduled time on the board goes through.
This prints its answer for the two mornings a year that are not 24 hours
long, so the TypeScript port can be held to it exactly.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gtfsdb import gtfs_time_to_epoch                      # noqa: E402

CASES = []
for tz_name, dates in (
    # Spring forward: 2 am -> 3 am, so 02:30 never happens.
    ("Australia/Melbourne", ["20261003", "20261004", "20261005"]),
    # Fall back: 3 am -> 2 am, so 02:30 happens twice.
    ("Australia/Melbourne", ["20270403", "20270404", "20270405"]),
    # A zone that never moves, as a control.
    ("Australia/Brisbane", ["20261004", "20270404"]),
):
    for ymd in dates:
        for hms in ("00:00:00", "01:30:00", "02:30:00", "03:30:00", "12:00:00",
                    "23:59:59", "24:00:00", "25:10:00", "26:30:00", "27:45:30"):
            CASES.append((tz_name, ymd, hms))

out = []
for tz_name, ymd, hms in CASES:
    tz = ZoneInfo(tz_name)
    service_date = datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=tz)
    h, m, s = (int(x) for x in hms.split(":"))
    out.append({"tz": tz_name, "ymd": ymd, "secs": h * 3600 + m * 60 + s,
                "hms": hms, "epoch": gtfs_time_to_epoch(hms, service_date, tz)})
json.dump(out, sys.stdout)
