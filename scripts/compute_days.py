"""Print how many days back to fetch, based on the newest match already in data.json."""
import json
import sys
from datetime import date, datetime

DEFAULT_DAYS = 90   # no data.json yet -> initial backfill window
BUFFER_DAYS = 2     # re-check recent days in case CricHeroes posted scorecards late

path = sys.argv[1]
try:
    d = json.load(open(path, encoding="utf-8"))
    dates = [m["date"] for p in d["players"] for m in (p.get("recent_matches") or []) if m.get("date")]
    last = datetime.strptime(max(dates), "%Y-%m-%d").date()
    days = max(1, (date.today() - last).days + BUFFER_DAYS)
except (OSError, ValueError, KeyError, TypeError):
    days = DEFAULT_DAYS
print(days)
