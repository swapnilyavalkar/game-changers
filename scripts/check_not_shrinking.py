"""Exit 1 if the new data.json covers less than the one currently published.

Usage: check_not_shrinking.py <published data.json> <new data.json>
Set ALLOW_SHRINK=1 to publish a deliberately smaller file (e.g. after reclassifying matches).
"""
import json
import os
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    players = d.get("players") or []
    return {
        "players": len(players),
        "team_matches": (d.get("team") or {}).get("nct_matches", 0),
        "player_match_rows": sum(len(p.get("recent_matches") or []) for p in players),
    }


try:
    old = load(sys.argv[1])
except OSError:
    print("nothing published yet - skipping the check")
    sys.exit(0)

new = load(sys.argv[2])
print("published:", old)
print("new      :", new)
shrunk = [k for k in old if new[k] < old[k]]
if shrunk and os.environ.get("ALLOW_SHRINK") != "1":
    print("::error::new data is smaller than the published data in: " + ", ".join(shrunk) + " - not publishing")
    sys.exit(1)
print("OK - new data is not smaller than the published data")
