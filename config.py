"""
config.py — runtime settings for the CricHeroes analytics tool.

Priority order (highest wins):
  1. CLI flags        --team-id, --team-slug   (passed at runtime)
  2. Env vars         CRICHEROES_TEAM_ID etc.  (GitHub Secrets / CI override)
  3. team.json        committed config file    (primary config — edit this)
  4. Hardcoded defaults below                  (safe fallback, never need changing)

For a new team: fork the repo, edit team.json, commit, push.
GitHub Actions will pick it up with zero extra configuration.
"""

import json
import os
from pathlib import Path

# ── Paths (resolved first — team.json lives next to this file) ───────────────
BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
REPORTS_DIR   = BASE_DIR / "reports"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR    = BASE_DIR / "static"

for _d in [DATA_DIR, REPORTS_DIR]:
    _d.mkdir(exist_ok=True)

# ── Load team.json (the primary config file any team should edit) ─────────────
_TEAM_FILE = BASE_DIR / "team.json"
_t: dict = {}
if _TEAM_FILE.exists():
    try:
        _raw = json.loads(_TEAM_FILE.read_text(encoding="utf-8"))
        # Strip _comment keys — they are documentation only
        _t = {k: v for k, v in _raw.items() if not k.startswith("_")}
    except json.JSONDecodeError as _e:
        import warnings
        warnings.warn(f"[config] team.json is invalid JSON: {_e}. Using defaults.")

def _get(key: str, json_val, default):
    """
    Return the first non-None value from:
      env var → team.json value → hardcoded default
    """
    env_val = os.environ.get(key.upper().replace(".", "_"))
    if env_val is not None:
        return type(default)(env_val)
    if json_val is not None:
        return json_val
    return default


# ── Team identity ─────────────────────────────────────────────────────────────
TEAM_ID   = _get("CRICHEROES_TEAM_ID",   _t.get("team_id"),   "2182806")
TEAM_SLUG = _get("CRICHEROES_TEAM_SLUG", _t.get("team_slug"), "gc-app")
TEAM_NAME = _get("CRICHEROES_TEAM_NAME", _t.get("team_name"), "Game Changers")
TEAM_URL  = f"{TEAM_ID}/{TEAM_SLUG}"

# ── Multi-team configuration ──────────────────────────────────────────────────
TEAMS = _t.get("teams", [
    {"team_id": "2182806", "team_slug": "super-game-changers", "team_name": "Super Game Changers"},
    {"team_id": "2182828", "team_slug": "game-changers", "team_name": "Game Changers"}
])
TEAM_IDS: frozenset = frozenset([str(t["team_id"]) for t in TEAMS])

# ── Formats ───────────────────────────────────────────────────────────────────
FORMATS = [6, 10, 15, 20]

# ── Database — one file per team slug (e.g. gc-app.db, thunder-bolts.db) ──
DB_PATH = DATA_DIR / f"{TEAM_SLUG}.db"

# ── CricHeroes scraper timing ─────────────────────────────────────────────────
CH_BASE_URL       = "https://cricheroes.in"
CH_API_URL        = "https://api.cricheroes.in/api/v1"   # REST API (no login required)
CH_API_KEY        = os.environ.get("CH_API_KEY", "")     # set via env / GitHub Actions secret — never commit
CH_API_UDID       = "3f0d2d7f3c284b088eb2c8814ff7eec9"  # stable device fingerprint
CH_API_DEVICE     = "Chrome: 146.0.0.0"
TEAM_PROFILE_BASE = "https://cricheroes.in/team-profile"
PAGE_LOAD_WAIT    = int(os.environ.get("CH_PAGE_LOAD_WAIT",  5))    # seconds
SCORECARD_WAIT    = int(os.environ.get("CH_SCORECARD_WAIT",  8))    # seconds
REQUEST_DELAY_SEC = float(os.environ.get("CH_REQUEST_DELAY", 2.0))  # seconds

# ── Scoring weights (sum = 1.0) ───────────────────────────────────────────────
_w = _t.get("weights", {})
WEIGHTS = {
    "recent_form":     _get("WEIGHT_RECENT_FORM",     _w.get("recent_form"),     0.40),
    "format_specific": _get("WEIGHT_FORMAT_SPECIFIC", _w.get("format_specific"), 0.30),
    "career_avg":      _get("WEIGHT_CAREER_AVG",      _w.get("career_avg"),      0.20),
    "consistency":     _get("WEIGHT_CONSISTENCY",     _w.get("consistency"),     0.10),
}

# ── Squad composition constraints — (min, max) per role ──────────────────────
def _squad_from_json(key: str, default: dict) -> dict:
    raw = _t.get(key, {})
    result = {}
    for role, val in default.items():
        entry = raw.get(role, val)
        result[role] = tuple(entry) if isinstance(entry, list) else entry
    return result

SQUAD_11 = _squad_from_json("squad_11", {
    "wicket_keeper": (1, 2),
    "batsman":       (3, 5),
    "all_rounder":   (1, 3),
    "bowler":        (2, 4),
})
SQUAD_15 = _squad_from_json("squad_15", {
    "wicket_keeper": (1, 2),
    "batsman":       (4, 6),
    "all_rounder":   (2, 4),
    "bowler":        (3, 5),
})

# ── Recent-form window ────────────────────────────────────────────────────────
RECENT_MATCHES_N = int(_get("RECENT_MATCHES_N", _t.get("recent_matches_n"), 5))

# ── Performance benchmarks ────────────────────────────────────────────────────
# Defaults for local tennis-ball / limited-overs club cricket.
# Tune in team.json under "benchmarks" for your league standard:
#   Local tennis ball : bat avg 20, SR 120, economy good=9  / bad=16
#   Club leather ball : bat avg 15, SR 100, economy good=7  / bad=12
#   State / open      : bat avg 25, SR 130, economy good=6  / bad=10
_b = _t.get("benchmarks", {})
BAT_AVG_BENCHMARK = _get("BAT_AVG_BENCHMARK", _b.get("bat_avg_benchmark"), 20.0)
BAT_SR_BENCHMARK  = _get("BAT_SR_BENCHMARK",  _b.get("bat_sr_benchmark"),  120.0)
BOWL_ECON_GOOD    = _get("BOWL_ECON_GOOD",    _b.get("bowl_econ_good"),    9.0)
BOWL_ECON_BAD     = _get("BOWL_ECON_BAD",     _b.get("bowl_econ_bad"),     16.0)

# ── Form classification thresholds ───────────────────────────────────────────
_ft = _t.get("form_thresholds", {})
FORM_IN_THRESHOLD  = int(_get("FORM_IN_THRESHOLD",  _ft.get("in_form"), 60))
FORM_AVG_THRESHOLD = int(_get("FORM_AVG_THRESHOLD", _ft.get("average"), 40))

# ── CricHeroes account credentials (optional — used for full match pagination) ─
# Set these in a .env file or environment variables; never commit credentials.
# Used by: python main.py login
CRICHEROES_PHONE    = os.environ.get("CRICHEROES_PHONE",    "")
CRICHEROES_PASSWORD = os.environ.get("CRICHEROES_PASSWORD", "")

# ── Blocked players — cricheroes_id strings to permanently exclude ────────────
# These players are never inserted into the DB and are excluded from all outputs.
# Add more IDs comma-separated in the env var BLOCKED_PLAYER_IDS to override.
_blocked_env = os.environ.get("BLOCKED_PLAYER_IDS", "")
_blocked_json = _t.get("blocked_player_ids", [])
BLOCKED_PLAYER_IDS: frozenset = frozenset(
    [s.strip() for s in _blocked_env.split(",") if s.strip()]
    or [str(i) for i in _blocked_json]
    or ["1569002"]   # Jitendra Unecha — removed from team
)

# ── Publishing ────────────────────────────────────────────────────────────────
GITHUB_PAGES_BRANCH = "gh-pages"
SITE_TITLE = os.environ.get("SITE_TITLE", f"{TEAM_NAME} – Player Analytics")
