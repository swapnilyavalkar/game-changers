from __future__ import annotations

"""
CricHeroes data fetcher — Selenium-based.

What we know from the library source (reference-repos/cricheroes/cricheroes/cricheroes.py):

  1. CricHeroes is a full SPA — requires Selenium + Chrome to render.
  2. Base URL: https://cricheroes.in/team-profile/{id}/{slug}
  3. Library scrapes 4 tabs: members, matches, stats, leaderboard.
  4. Match object attributes:
       .tournament  – tournament name
       .info        – comma-separated: "Venue, 10 Overs, 12 Jan 2025"
       .score       – list of score strings per team
       .result      – result text (e.g. "Game Changers won by 5 wickets")
       .url         – match page path (e.g. /match-stats/12345678/...)
       .match_date  – parsed date property
       .venue       – parsed venue property
  5. Player object attributes:
       .name            – player name
       .sub_title       – role / batting+bowling style string
       .profile_url     – path like /player-profile/12345678/name
       .profile_pic_url – photo URL
  6. LeaderboardStat: .player_name, .stat (e.g. "452 runs"), .profile

Our additions:
  - Selenium scraper for individual match scorecard pages (per-player batting/bowling)
  - Extract player ID from profile_url
  - Extract overs from match.info
  - Cache raw HTML per match to data/cache/ to avoid re-fetching
"""

import json
import logging
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    TEAM_ID, TEAM_SLUG, TEAM_URL,
    CH_BASE_URL, TEAM_PROFILE_BASE, CH_API_URL, CH_API_KEY,
    PAGE_LOAD_WAIT, SCORECARD_WAIT,
    REQUEST_DELAY_SEC, DATA_DIR,
)
from cricheroes_auth import get_api_session

log = logging.getLogger(__name__)

# Raised by _fetch_scorecard_html_via_requests() when a Cloudflare bot-protection
# challenge is detected in the HTTP response.  Caught by fetch_match_scorecard()
# to skip the Selenium fallback (which will also be blocked).
class _CloudflareBlockedError(Exception):
    pass

CACHE_DIR            = DATA_DIR / "cache"
SCORECARD_CACHE_DIR  = CACHE_DIR / "scorecards"
COMMENTARY_CACHE_DIR = CACHE_DIR / "commentary"
CACHE_DIR.mkdir(exist_ok=True)
SCORECARD_CACHE_DIR.mkdir(exist_ok=True)
COMMENTARY_CACHE_DIR.mkdir(exist_ok=True)


# ── Selenium driver ───────────────────────────────────────────────────────────

def _make_driver(perf_logging: bool = False, max_retries: int = 3) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--log-level=3")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
    if perf_logging:
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return webdriver.Chrome(options=options)
        except Exception as e:
            last_error = e
            log.warning(f"[driver] Chrome launch attempt {attempt}/{max_retries} failed ({e}). Retrying in {attempt * 1.5}s...")
            time.sleep(attempt * 1.5)

    import shutil
    chromedriver_path = shutil.which("chromedriver")
    if chromedriver_path:
        try:
            log.debug(f"[driver] Using system chromedriver: {chromedriver_path}")
            return webdriver.Chrome(service=Service(chromedriver_path), options=options)
        except Exception as e:
            last_error = e

    import os
    os.environ["WDM_SSL_VERIFY"] = "0"
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


# ── Player helpers ────────────────────────────────────────────────────────────

def _player_id_from_url(profile_url: str) -> str:
    """Extract numeric ID from /player-profile/12345678/name-slug"""
    parts = [p for p in (profile_url or "").split("/") if p]
    for i, part in enumerate(parts):
        if part == "player-profile" and i + 1 < len(parts):
            return parts[i + 1]
    # fallback: find first numeric segment
    for part in parts:
        if part.isdigit():
            return part
    return ""


def _map_role(sub_title: str) -> str:
    """
    sub_title examples:
      "Batsman"  |  "Bowler"  |  "All Rounder"  |  "Wicket Keeper Batsman"
      "Right Hand Bat, Right Arm Medium"
    """
    s = (sub_title or "").lower()
    if "wicket" in s or "keeper" in s or "wk" in s:
        return "wicket_keeper"
    if "all" in s and ("round" in s or "rounder" in s):
        return "all_rounder"
    if "bowl" in s:
        return "bowler"
    if "bat" in s:
        return "batsman"
    return "unknown"


def _normalise_player(p) -> dict:
    """Convert a cricheroes Player object → plain dict."""
    pid = _player_id_from_url(getattr(p, "profile_url", "") or "")
    return {
        "cricheroes_id": pid,
        "name":          (p.name or "").strip(),
        "role":          _map_role(p.sub_title),
        "batting_style": _parse_batting_style(p.sub_title),
        "bowling_style": _parse_bowling_style(p.sub_title),
        "profile_url":   p.profile_url,
        "photo_url":     p.profile_pic_url,
    }


def _parse_batting_style(sub_title: str) -> str | None:
    s = (sub_title or "").lower()
    if "left hand" in s or "left-hand" in s:
        return "Left Hand"
    if "right hand" in s or "right-hand" in s:
        return "Right Hand"
    return None


def _parse_bowling_style(sub_title: str) -> str | None:
    s = (sub_title or "")
    # Try to find "Right Arm X" or "Left Arm X"
    m = re.search(r'(right|left)\s+arm\s+\w+', s, re.IGNORECASE)
    return m.group(0).title() if m else None


# ── Match helpers ─────────────────────────────────────────────────────────────

def _extract_overs_from_info(info: str) -> int:
    """
    match.info = "Venue Name, 10 Overs, 12 Jan 2025"
    Parse and return integer overs.
    """
    for part in (info or "").split(","):
        m = re.search(r'(\d+)\s*[Oo]vers?', part.strip())
        if m:
            v = int(m.group(1))
            # Snap to known formats
            for fmt in [6, 10, 15, 20]:
                if v <= fmt:
                    return fmt
            return v
    return 20  # default


def _extract_match_id_from_url(url: str) -> str:
    """Extract match ID from /match-stats/12345678/..."""
    parts = [p for p in (url or "").split("/") if p]
    for i, part in enumerate(parts):
        if part == "match-stats" and i + 1 < len(parts):
            return parts[i + 1]
    for part in parts:
        if part.isdigit() and len(part) > 5:
            return part
    return ""


def _map_result(result_text: str, team_name: str = "gamechangers") -> str:
    s = (result_text or "").lower()
    if "won" in s or "win" in s:
        if team_name.lower() in s:
            return "win"
        return "loss"
    if "lost" in s or "lose" in s:
        return "loss"
    if "no result" in s or "abandoned" in s:
        return "no_result"
    if "tied" in s or "draw" in s:
        return "draw"
    return "no_result"


def _normalise_match(m, team_name: str = "Game Changers") -> dict:
    """Convert a cricheroes Match object → plain dict."""
    mid = _extract_match_id_from_url(getattr(m, "url", "") or "")
    return {
        "cricheroes_match_id": mid,
        "match_date":  str(m.match_date) if hasattr(m, "match_date") else None,
        "opponent":    _extract_opponent(m.score, team_name),
        "overs":       _extract_overs_from_info(m.info),
        "result":      _map_result(m.result, team_name),
        "venue":       m.venue if hasattr(m, "venue") else None,
        "tournament":  m.tournament,
        "match_url":   f"{CH_BASE_URL}{m.url}" if m.url.startswith("/") else m.url,
        "raw_info":    m.info,
        "raw_score":   m.score,
        "raw_result":  m.result,
    }


def _extract_opponent(score_list: list, team_name: str) -> str:
    """score is a list of strings like ['Game Changers 120/5 (10)', 'Rivals 110/8 (10)']"""
    tl = team_name.lower()
    for s in (score_list or []):
        if tl not in s.lower():
            # Strip score, keep team name part
            return re.split(r'\d', s)[0].strip().rstrip()
    return "Unknown"


# ── Dismissal text parser ─────────────────────────────────────────────────────

def _parse_dismissal_text(how_to_out: str) -> dict:
    """
    Parse CricHeroes how_to_out string into structured dismissal info.

    Examples:
      "b Trambak Bhujbal"                      → bowled, bowler=Trambak Bhujbal
      "c Kadar Khan b Swapnil Yavalkar"        → caught, fielder=Kadar Khan, bowler=Swapnil Yavalkar
      "c †Prashant Dalvi b Jalindar Agre"      → caught behind (†=keeper), fielder=Prashant Dalvi
      "lbw b Bowler"                           → lbw, bowler=Bowler
      "run out (Fielder)"                      → run_out, fielder=Fielder
      "st Prashant b Bowler"                   → stumped, fielder=Prashant, bowler=Bowler
      "not out"                                → None (no dismissal)

    Returns: {dismissal_type, bowler_name, fielder_name} — any may be None/empty.
    """
    raw = (how_to_out or "").strip()
    if not raw or raw.lower() in ("not out", "dnb", "absent", "did not bat", "yet to bat", ""):
        return {"dismissal_type": None, "bowler_name": None, "fielder_name": None}

    s = raw.lower()

    # run out
    if s.startswith("run out"):
        fielder = re.search(r'\(([^)]+)\)', raw)
        return {
            "dismissal_type": "run_out",
            "bowler_name":    None,
            "fielder_name":   fielder.group(1).strip() if fielder else None,
        }

    # stumped: "st Fielder b Bowler"
    if s.startswith("st "):
        rest = raw[3:].strip()
        if " b " in rest:
            parts    = rest.split(" b ", 1)
            fielder  = parts[0].strip().lstrip("†").strip()
            bowler   = parts[1].strip()
        else:
            fielder, bowler = rest, None
        return {"dismissal_type": "stumped", "bowler_name": bowler, "fielder_name": fielder}

    # caught: "c Fielder b Bowler" or "c †Keeper b Bowler"
    if s.startswith("c ") and " b " in s:
        rest = raw[2:].strip()
        idx  = rest.lower().rfind(" b ")   # rightmost " b " to handle names with "b"
        if idx >= 0:
            fielder_raw = rest[:idx].strip().lstrip("†").strip()
            bowler      = rest[idx + 3:].strip()
            dtype = "caught_behind" if rest.startswith("†") else "caught"
            return {"dismissal_type": dtype, "bowler_name": bowler, "fielder_name": fielder_raw}

    # lbw: "lbw b Bowler"
    if s.startswith("lbw"):
        m = re.search(r'\bb\s+(.+)', raw, re.IGNORECASE)
        return {
            "dismissal_type": "lbw",
            "bowler_name":    m.group(1).strip() if m else None,
            "fielder_name":   None,
        }

    # bowled: "b Bowler"
    if re.match(r'^b\s+', s):
        bowler = raw[2:].strip()
        return {"dismissal_type": "bowled", "bowler_name": bowler, "fielder_name": None}

    # hit wicket, retired, etc.
    return {"dismissal_type": "other", "bowler_name": None, "fielder_name": None}


# ── Scorecard scraper ─────────────────────────────────────────────────────────

def _parse_fielding_from_dismissals(
    batting_rows: list,
    player_name_lookup: dict,   # lowercase name → cricheroes_id
) -> dict:
    """
    Extract fielder contributions from batting dismissal texts.

    how_to_out patterns (from CricHeroes):
      "c Kadar Khan b Trambak Bhujbal"  → Kadar Khan gets a catch
      "run out (Swapnil Yavalkar)"      → Swapnil gets a run-out
      "st Prashant Dalvi b Jalindar"    → Prashant gets a stumping
      "b Bowler" / "lbw b Bowler"       → no fielder

    Returns: { cricheroes_id: {catches, run_outs, stumpings} }
    """
    fielding: dict = {}

    def _add(name_raw: str, stat_key: str):
        name = name_raw.strip().lower()
        # Exact match first, then try partial prefix match
        pid = player_name_lookup.get(name)
        if not pid:
            for k, v in player_name_lookup.items():
                if k.startswith(name) or name.startswith(k):
                    pid = v
                    break
        if pid:
            if pid not in fielding:
                fielding[pid] = {"catches": 0, "run_outs": 0, "stumpings": 0}
            fielding[pid][stat_key] += 1

    for batter in batting_rows:
        how = (batter.get("how_to_out") or "").strip()
        hl  = how.lower()

        if hl.startswith("c ") and " b " in hl:
            # "c Fielder b Bowler" — catch
            fielder_part = how[2 : how.lower().index(" b ")].strip()
            if fielder_part.lower() not in ("sub", ""):
                _add(fielder_part, "catches")

        elif hl.startswith("run out"):
            # "run out (Fielder)" or "run out Fielder"
            m = re.search(r'\(([^)]+)\)', how)
            if m:
                _add(m.group(1), "run_outs")
            elif " " in how[7:].strip():
                _add(how[7:].strip(), "run_outs")

        elif hl.startswith("st ") and " b " in hl:
            # "st Keeper b Bowler" — stumping
            keeper_part = how[3 : how.lower().index(" b ")].strip()
            _add(keeper_part, "stumpings")

    return fielding


def _parse_scorecard_from_next_data(
    nd: dict,
    match_id: str,
    player_name_lookup: dict | None = None,
) -> dict | None:
    """
    Parse batting/bowling/fielding stats from __NEXT_DATA__ on a CricHeroes scorecard page.

    __NEXT_DATA__.props.pageProps.scorecard is a list of innings dicts:
      [{"team_id": ..., "batting": [...], "bowling": [...], ...}, ...]

    player_name_lookup: optional { lowercase_name: cricheroes_id } for fielding attribution.

    Returns the standard scorecard dict or None if data is missing.
    """
    props    = nd.get("props", {}).get("pageProps", {})
    sc_data  = props.get("scorecard")
    if not sc_data or not isinstance(sc_data, list):
        return None

    mini       = props.get("miniScorecard") or {}
    match_info = props.get("matchInfo") or props.get("match_info") or props.get("match") or {}
    summary_data = (props.get("summaryData") or {}).get("data") or {}
    start_dt   = (
        mini.get("start_datetime") or
        match_info.get("start_datetime") or
        summary_data.get("start_datetime") or ""
    )
    mdate = start_dt[:10] if len(start_dt) >= 10 else None

    # Try multiple locations — CricHeroes has moved this field across versions
    _overs = (
        mini.get("overs") or mini.get("total_overs") or
        match_info.get("overs") or match_info.get("total_overs") or
        props.get("overs") or 0
    )
    # Last resort: infer from sum of bowling overs in a single innings
    if not _overs and sc_data:
        for inn in sc_data:
            if not isinstance(inn, dict):
                continue
            inn_total = sum(
                float(b.get("overs") or 0) + int(b.get("balls") or 0) / 6
                for b in (inn.get("bowling") or [])
                if isinstance(b, dict)
            )
            if inn_total > _overs:
                _overs = inn_total
    overs = int(_overs or 0)

    our_team_id  = str(TEAM_ID)
    # per-player merged stats: cricheroes_id → stat dict
    per_player: dict[str, dict] = {}
    # Strategy data lists populated per innings
    dismissals_list:   list[dict] = []
    opp_bowling_list:  list[dict] = []

    def _get(pid: str, name: str) -> dict:
        if pid not in per_player:
            per_player[pid] = {
                "cricheroes_player_id": pid,
                "player_name": name,
                # batting
                "runs": 0, "balls_faced": 0, "fours": 0, "sixes": 0,
                "batting_sr": 0.0, "is_out": False, "batting_position": None,
                "yet_to_bat": False,
                # bowling
                "overs_bowled": 0.0, "wickets": 0, "runs_conceded": 0,
                "economy": 0.0, "maidens": 0, "dot_balls": 0,
                "did_not_bowl": False,
                # bowling discipline (Phase 1)
                "wides": 0, "no_balls": 0, "boundaries_conceded": 0,
                # fielding
                "catches": 0, "run_outs": 0, "stumpings": 0,
            }
        return per_player[pid]

    for inning in sc_data:
        if not isinstance(inning, dict):
            continue
        inning_team_id = str(inning.get("team_id") or "")
        is_our_batting = (inning_team_id == our_team_id)

        # ── Batting (when Game Changers is batting) ───────────────────────────────
        if is_our_batting:
            # Collect all player IDs who appeared in our batting lineup
            batting_lineup_pids: set[str] = set()
            for pos, batter in enumerate(inning.get("batting") or [], start=1):
                pid = str(batter.get("player_id") or "")
                if not pid:
                    continue
                batting_lineup_pids.add(pid)
                runs  = int(batter.get("runs")  or 0)
                balls = int(batter.get("balls") or 0)
                fours = int(batter.get("4s")    or 0)
                sixes = int(batter.get("6s")    or 0)
                sr    = float(batter.get("SR")  or 0)
                how_out = (batter.get("how_to_out") or "").strip().lower()
                is_dnb  = how_out in ("dnb", "absent", "did not bat", "yet to bat")
                is_out  = bool(how_out and not is_dnb and how_out != "not out" and how_out != "")
                p = _get(pid, batter.get("name") or "")
                if is_dnb:
                    # Player was in team but didn't get to bat (how_to_out says so)
                    p["yet_to_bat"] = True
                else:
                    p["runs"]             = runs
                    p["balls_faced"]      = balls
                    p["fours"]            = fours
                    p["sixes"]            = sixes
                    p["batting_sr"]       = sr if sr else (round(runs / balls * 100, 2) if balls else 0)
                    p["is_out"]           = is_out
                    p["batting_position"] = pos

            # ── to_be_bat: players in team who hadn't batted yet ──────────────
            # CricHeroes stores "Yet to Bat" players in a separate list called
            # "to_be_bat" (NOT in the batting rows). Parse them here.
            for ytb in (inning.get("to_be_bat") or []):
                if not isinstance(ytb, dict):
                    continue
                pid = str(ytb.get("player_id") or "")
                if not pid:
                    continue
                batting_lineup_pids.add(pid)
                p = _get(pid, ytb.get("name") or "")
                p["yet_to_bat"] = True

            # ── Strategy Phase 2: opponent bowlers who dismissed our batters ──
            inning_num = int((inning.get("inning") or {}).get("inning", 1)) if isinstance(inning.get("inning"), dict) else 1
            batting_team_id = str(inning.get("team_id") or "")
            for batter in (inning.get("batting") or []):
                how_out_raw = (batter.get("how_to_out") or "").strip()
                if not how_out_raw or how_out_raw.lower() in ("not out", "dnb", "absent", "did not bat", "yet to bat", ""):
                    continue
                d = _parse_dismissal_text(how_out_raw)
                if not d["dismissal_type"] or not d["bowler_name"]:
                    continue
                batter_pid = str(batter.get("player_id") or "")
                dismissals_list.append({
                    "inning":          inning_num,
                    "batter_name":     (batter.get("name") or "").strip(),
                    "batter_ch_id":    batter_pid or None,
                    "batter_player_id": None,   # resolved in main.py using player_id_map
                    "bowler_name":     d["bowler_name"],
                    "bowler_ch_id":    None,    # opponent bowler — no CH id available
                    "bowler_player_id": None,   # opponent bowler — not in our players table
                    "fielder_name":    d["fielder_name"],
                    "fielder_ch_id":   None,
                    "fielder_player_id": None,
                    "dismissal_type":  d["dismissal_type"],
                    "how_to_out_raw":  how_out_raw,
                    "batting_team_id": batting_team_id,
                    "_our_batting":    True,     # flag: our batter dismissed by opponent
                })

            # Opponent bowling stats (from bowling table of our batting inning)
            for bowler in (inning.get("bowling") or []):
                bname = (bowler.get("name") or "").strip()
                bchid = str(bowler.get("player_id") or "")
                if not bname:
                    continue
                _ov_raw  = float(bowler.get("overs") or 0)
                _ov_int  = int(_ov_raw)
                _ov_dec  = round((_ov_raw - _ov_int) * 10)  # cricket notation: 2.3 → 3 balls
                ov_balls = int(bowler.get("balls") or 0)
                overs_b  = round((_ov_int * 6 + _ov_dec + ov_balls) / 6, 2)
                runs_c   = int(bowler.get("runs") or 0)
                wkts     = int(bowler.get("wickets") or 0)
                wds      = int(bowler.get("wide") or bowler.get("extra_type_run_wide") or 0)
                nbs      = int(bowler.get("noball") or bowler.get("extra_type_run_noball") or 0)
                dots     = int(bowler.get("0s") or 0)
                bdry     = int(bowler.get("4s") or 0) + int(bowler.get("6s") or 0)
                econ     = float(bowler.get("economy_rate") or 0)
                if econ == 0 and overs_b > 0:
                    econ = round(runs_c / overs_b, 2)
                opp_bowling_list.append({
                    "bowler_name":  bname,
                    "bowler_ch_id": bchid or None,
                    "overs":        overs_b,
                    "runs_conceded": runs_c,
                    "wickets":      wkts,
                    "wides":        wds,
                    "no_balls":     nbs,
                    "dot_balls":    dots,
                    "boundaries":   bdry,
                    "economy_rate": econ,
                })

        # ── Bowling + fielding (when Game Changers is fielding) ───────────────────
        else:
            batting_rows = inning.get("batting") or []
            bowling_rows = inning.get("bowling") or []
            bowler_pids: set[str] = set()

            for bowler in bowling_rows:
                pid = str(bowler.get("player_id") or "")
                if not pid:
                    continue
                bowler_pids.add(pid)
                _ov_raw  = float(bowler.get("overs") or 0)
                _ov_int  = int(_ov_raw)
                _ov_dec  = round((_ov_raw - _ov_int) * 10)  # cricket notation: 2.3 → 3 balls
                ov_balls = int(bowler.get("balls")   or 0)
                overs_b  = round((_ov_int * 6 + _ov_dec + ov_balls) / 6, 2)
                wkts     = int(bowler.get("wickets") or 0)
                runs_c   = int(bowler.get("runs")    or 0)
                econ     = float(bowler.get("economy_rate") or 0)
                maidens  = int(bowler.get("maidens") or 0)
                dots     = int(bowler.get("0s")      or 0)
                # Phase 1: bowling discipline
                wds      = int(bowler.get("wide") or bowler.get("extra_type_run_wide") or 0)
                nbs      = int(bowler.get("noball") or bowler.get("extra_type_run_noball") or 0)
                bdry     = int(bowler.get("4s") or 0) + int(bowler.get("6s") or 0)
                if econ == 0 and overs_b > 0:
                    econ = round(runs_c / overs_b, 2)
                p = _get(pid, bowler.get("name") or "")
                p["overs_bowled"]       = overs_b
                p["wickets"]            = wkts
                p["runs_conceded"]      = runs_c
                p["economy"]            = econ
                p["maidens"]            = maidens
                p["dot_balls"]          = dots
                p["wides"]              = wds
                p["no_balls"]           = nbs
                p["boundaries_conceded"] = bdry

            # Mark players who were in our batting lineup (batted OR yet_to_bat)
            # but didn't appear in the bowling table → did_not_bowl = True
            for pid, pdata_row in per_player.items():
                if pid not in bowler_pids:
                    batted      = pdata_row.get("batting_position") is not None
                    yet_to_bat  = pdata_row.get("yet_to_bat", False)
                    if batted or yet_to_bat:
                        pdata_row["did_not_bowl"] = True

            # ── Fielding attribution from dismissal text ──────────────────────
            if player_name_lookup and batting_rows:
                fielding = _parse_fielding_from_dismissals(batting_rows, player_name_lookup)
                for fid, fstats in fielding.items():
                    p = _get(fid, "")
                    p["catches"]   += fstats["catches"]
                    p["run_outs"]  += fstats["run_outs"]
                    p["stumpings"] += fstats["stumpings"]

            # ── Strategy Phase 2: our bowlers dismissing opponent batters ──────
            inning_num_opp = int((inning.get("inning") or {}).get("inning", 2)) if isinstance(inning.get("inning"), dict) else 2
            batting_team_id_opp = str(inning.get("team_id") or "")
            for batter in batting_rows:
                how_out_raw = (batter.get("how_to_out") or "").strip()
                if not how_out_raw or how_out_raw.lower() in ("not out", "dnb", "absent", "did not bat", "yet to bat", ""):
                    continue
                d = _parse_dismissal_text(how_out_raw)
                if not d["dismissal_type"] or not d["bowler_name"]:
                    continue
                # Resolve our bowler's player_id from name
                bowler_pid = None
                if player_name_lookup:
                    bowl_key = d["bowler_name"].strip().lower()
                    bowler_ch = (player_name_lookup.get(bowl_key)
                                 or next((v for k, v in player_name_lookup.items() if bowl_key in k or k in bowl_key), None))
                    if bowler_ch:
                        bowler_pid = bowler_ch   # this is ch_id; main.py resolves to db_id
                batter_pid = str(batter.get("player_id") or "")
                dismissals_list.append({
                    "inning":          inning_num_opp,
                    "batter_name":     (batter.get("name") or "").strip(),
                    "batter_ch_id":    batter_pid or None,
                    "batter_player_id": None,    # opponent batter — not in our players table
                    "bowler_name":     d["bowler_name"],
                    "bowler_ch_id":    bowler_pid,
                    "bowler_player_id": None,    # resolved in main.py via player_id_map
                    "fielder_name":    d["fielder_name"],
                    "fielder_ch_id":   None,
                    "fielder_player_id": None,
                    "dismissal_type":  d["dismissal_type"],
                    "how_to_out_raw":  how_out_raw,
                    "batting_team_id": batting_team_id_opp,
                    "_our_batting":    False,     # flag: our bowler dismissed opponent
                })

    if not per_player:
        return None

    player_stats = list(per_player.values())
    log.info(f"[fetcher] scorecard {match_id}: {len(player_stats)} players from __NEXT_DATA__")
    return {
        "match_id":               match_id,
        "match_date":             mdate,
        "overs":                  overs,
        "player_stats":           player_stats,
        "dismissals":             dismissals_list,
        "opponent_bowling_stats": opp_bowling_list,
    }


def _get_raw_balls_from_next_data(nd: dict) -> list[dict]:
    """
    Extract raw ball dicts from CricHeroes __NEXT_DATA__ commentary structure.
    Handles all known key/nesting variants. Returns un-normalised dicts.
    """
    props = nd.get("props", {}).get("pageProps", {})

    commentary_raw = (
        props.get("commentary")
        or props.get("commentaryData")
        or props.get("ballByBall")
        or props.get("liveCommentary")
        or {}
    )

    all_balls: list[dict] = []
    if isinstance(commentary_raw, list):
        all_balls = commentary_raw
    elif isinstance(commentary_raw, dict):
        for v in commentary_raw.values():
            if isinstance(v, list):
                all_balls.extend(v)
            elif isinstance(v, dict):
                for inner in v.values():
                    if isinstance(inner, list):
                        all_balls.extend(inner)

    # Also check scorecard innings for embedded ball data
    sc_data = props.get("scorecard") or []
    for inning in (sc_data if isinstance(sc_data, list) else []):
        if isinstance(inning, dict):
            for key in ("commentary", "balls", "overs_data", "ball_by_ball"):
                inner = inning.get(key)
                if isinstance(inner, list):
                    all_balls.extend(inner)
                elif isinstance(inner, dict):
                    for v in inner.values():
                        if isinstance(v, list):
                            all_balls.extend(v)

    return [b for b in all_balls if isinstance(b, dict)]


def _parse_over_assignments_from_commentary(nd: dict) -> dict[str, list[int]]:
    """
    Extract per-over bowling assignments from CricHeroes __NEXT_DATA__ commentary.

    CricHeroes stores ball-by-ball data under pageProps.commentary (or similar keys).
    Commentary entry shape (observed):
      { "over": 1, "ball": 1, "bowler_id": "12345", "bowler_name": "...", "batsman_id": ..., ... }

    Returns: { cricheroes_player_id: [1, 3, 5, ...] }  (sorted unique over numbers)
    """
    all_balls = _get_raw_balls_from_next_data(nd)
    if not all_balls:
        return {}

    # Build mapping: player_id → set of over numbers
    assignments: dict[str, set[int]] = {}
    for ball in all_balls:
        bowler_id = str(
            ball.get("bowler_id") or ball.get("bowlerId") or
            ball.get("bowler_player_id") or ball.get("bowling_player_id") or ""
        )
        over_num = (
            ball.get("over") or ball.get("over_no") or ball.get("over_number")
            or ball.get("overNo") or 0
        )
        if bowler_id and over_num:
            try:
                ov = int(over_num)
            except (ValueError, TypeError):
                continue
            assignments.setdefault(bowler_id, set()).add(ov)

    return {pid: sorted(ovs) for pid, ovs in assignments.items()}


_MATCH_TABS = frozenset({
    "summary", "scorecard", "commentary", "live", "mvp", "analysis", "heroes",
    "teams", "gallery", "stats", "full-scorecard",
})


def _match_tab_url(match_url: str, tab: str) -> str:
    """Return the URL of one tab of a match page, whatever tab the link pointed at.

    Team pages link to .../{match}/summary. Appending a tab to that gives
    .../summary/commentary, which CricHeroes rejects, so any trailing tab
    segment is replaced rather than extended.
    """
    base = match_url.split("?")[0].split("#")[0].rstrip("/")
    head, _, last = base.rpartition("/")
    if last in _MATCH_TABS:
        base = head
    return f"{base}/{tab}"


def _build_commentary_url(match_url: str) -> str:
    """Return the commentary variant of a scorecard URL."""
    return _match_tab_url(match_url, "commentary")


def _normalise_ball(b: dict) -> dict:
    """Normalise a raw CricHeroes ball dict to a standard schema."""
    return {
        "over":        int(b.get("over") or b.get("over_no") or b.get("overNo") or 0),
        "ball":        int(b.get("ball") or b.get("ball_no") or b.get("ballNo") or 0),
        "bowler_id":   str(b.get("bowler_id") or b.get("bowlerId") or b.get("bowling_player_id") or ""),
        "bowler_name": str(b.get("bowler_name") or b.get("bowlerName") or b.get("bowler") or ""),
        "batter_id":   str(b.get("batsman_id") or b.get("batsmanId") or b.get("batter_id") or b.get("batting_player_id") or ""),
        "batter_name": str(b.get("batsman_name") or b.get("batsmanName") or b.get("batter_name") or b.get("striker") or ""),
        "runs":        int(b.get("runs") or b.get("run") or b.get("batsman_run") or 0),
        "extras":      int(b.get("extras") or b.get("extra_run") or 0),
        "is_wicket":   bool(b.get("is_wicket") or b.get("isWicket") or b.get("wicket") or False),
        "extras_type": str(b.get("extras_type") or b.get("extrasType") or b.get("extra_type") or ""),
    }


def _parse_balls_from_response(data, _depth: int = 0) -> list[dict]:
    """
    Recursively search a parsed JSON API response for ball-by-ball entries.
    Handles CricHeroes' various response shapes.  _depth guards against infinite recursion.
    """
    if _depth > 5:
        return []

    if isinstance(data, list):
        candidates = [
            item for item in data
            if isinstance(item, dict) and (
                "bowler_id" in item or "bowlerId" in item or "bowling_player_id" in item
                or ("over" in item and "ball" in item and ("runs" in item or "run" in item))
            )
        ]
        if candidates:
            return [_normalise_ball(b) for b in candidates]
        return []

    if not isinstance(data, dict):
        return []

    # Try known container keys first
    for key in ("commentary", "commentaryData", "ball_by_ball", "ballByBall",
                "data", "result", "balls", "liveCommentary", "overs"):
        val = data.get(key)
        if val is None:
            continue
        result = _parse_balls_from_response(val, _depth + 1)
        if result:
            return result

    # Fallback: walk all values (e.g. innings-keyed dicts {"1": [...], "2": [...]})
    for v in data.values():
        if isinstance(v, (list, dict)):
            result = _parse_balls_from_response(v, _depth + 1)
            if result:
                return result

    return []


def _extract_balls_from_perf_log(driver, perf_logs: list) -> list[dict]:
    """Parse Chrome CDP performance log to intercept and decode XHR ball-by-ball responses."""
    request_ids: list[tuple[str, str]] = []
    all_json_urls: list[str] = []   # for diagnostics
    for entry in perf_logs:
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        params = msg.get("params", {})
        url          = params.get("response", {}).get("url", "")
        content_type = params.get("response", {}).get("mimeType", "")
        if "json" not in content_type.lower():
            continue
        if url:
            all_json_urls.append(url)
        if any(kw in url.lower() for kw in ["commentary", "ball", "live", "v2/match", "v1/match", "api/v"]):
            request_ids.append((params.get("requestId", ""), url))

    if all_json_urls:
        log.debug(f"[fetcher] XHR: {len(all_json_urls)} JSON calls: {[u[-60:] for u in all_json_urls[:5]]}")
    else:
        log.debug("[fetcher] XHR: no JSON responses in performance log")

    for request_id, url in request_ids:
        if not request_id:
            continue
        try:
            result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
            body = result.get("body", "")
            if not body:
                continue
            data  = json.loads(body)
            balls = _parse_balls_from_response(data)
            if balls:
                log.info(f"[fetcher] ball-by-ball data found at: {url}")
                return balls
        except Exception as e:
            log.debug(f"[fetcher] response body unavailable for {url}: {e}")

    return []


def _fetch_commentary_balls_api(match_id: str) -> list[dict]:
    """
    Fetch ball-by-ball data via the CricHeroes REST API — no login, no Selenium.

    Endpoint (both innings):
      GET https://api.cricheroes.in/api/v1/scorecard/v2/get-commentary/{match_id}?inning=1
      GET https://api.cricheroes.in/api/v1/scorecard/v2/get-commentary/{match_id}?inning=2

    Response: { status: true, data: { commentary: [...], ... } }

    Ball fields used:
      current_over  — 1-based over number
      ball          — "over.ball_num" string, e.g. "17.5" = over 18, ball 5
      run           — runs off the bat
      extra_run     — extra runs
      extra_type_code — 0=none, 1=wide, 2=no_ball, 3=bye, 4=leg_bye
      is_out        — 1 if wicket fell
      dismiss_player_id — batter's player ID (only set on wicket balls)
      commentary    — text e.g. "BowlerName to BatterName, SIX, ..."

    Returns combined list for both innings, or [] on any failure.
    """
    _EXTRA_TYPE = {"1": "wide", "2": "no_ball", "3": "bye", "4": "leg_bye"}
    _COMMENT_RE = re.compile(r"^(.+?) to (.+?),")

    session   = get_api_session()
    all_balls: list[dict] = []

    for inning in (1, 2):
        url = f"{CH_API_URL}/scorecard/v2/get-commentary/{match_id}?inning={inning}"
        try:
            r = session.get(url, timeout=20)
            if r.status_code != 200:
                log.debug(f"[fetcher] API commentary {match_id} inning {inning}: HTTP {r.status_code}")
                continue
            payload = r.json()
            if not payload.get("status"):
                log.debug(f"[fetcher] API commentary {match_id} inning {inning}: status=false")
                continue
            raw_balls = payload.get("data", {}).get("commentary", [])
            if not isinstance(raw_balls, list):
                continue
            for b in raw_balls:
                if not isinstance(b, dict):
                    continue
                # Parse "BowlerName to BatterName, outcome" from commentary text
                commentary_text = str(b.get("commentary", ""))
                bowler_name, batter_name = "", ""
                m = _COMMENT_RE.match(commentary_text)
                if m:
                    bowler_name = m.group(1).strip()
                    batter_name = m.group(2).strip()

                # Parse ball position from "over_idx.ball_num" string
                ball_str = str(b.get("ball", "0.0"))
                try:
                    parts    = ball_str.split(".")
                    ball_num = int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    ball_num = 0

                etc         = str(b.get("extra_type_code") or "").strip()
                is_out      = bool(b.get("is_out"))
                batter_id   = str(b.get("dismiss_player_id") or "") if is_out else ""

                all_balls.append({
                    "over":        int(b.get("current_over") or 0),
                    "ball":        ball_num,
                    "bowler_id":   "",
                    "bowler_name": bowler_name,
                    "batter_id":   batter_id,
                    "batter_name": batter_name,
                    "runs":        int(b.get("run") or 0),
                    "extras":      int(b.get("extra_run") or 0),
                    "is_wicket":   is_out,
                    "extras_type": _EXTRA_TYPE.get(etc, ""),
                })
        except Exception as e:
            log.debug(f"[fetcher] API commentary error {match_id} inning {inning}: {e}")

    return all_balls


def fetch_commentary_balls(
    match_url: str,
    match_id: str,
) -> list[dict]:
    """
    Fetch ball-by-ball data for a match.

    Tries in order:
      1. JSON cache        — balls_{match_id}.json (instant return)
      2. REST API          — CricHeroes scorecard/v2/get-commentary; no Selenium needed.
                             Covers all 37 matches including older ones whose ball data
                             only loads via AJAX (not in SSR HTML).  Deletes any stale
                             no_data sentinel written by a prior Selenium-only run.
      3. No-data sentinel  — balls_{match_id}_no_data (skip if API already confirmed data)
      4. Cached HTML parse — __NEXT_DATA__ + .commentary-text DOM elements
      5. Fresh Selenium    — last resort; only if HTML not yet cached

    Returns [] if the match has no ball-by-ball data recorded on CricHeroes.
    """
    cache_file = COMMENTARY_CACHE_DIR / f"balls_{match_id}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    _no_balls_sentinel = COMMENTARY_CACHE_DIR / f"balls_{match_id}_no_data"

    # ── Strategy 2: REST API (fastest, covers all matches, no Selenium) ──────────
    api_balls = _fetch_commentary_balls_api(match_id)
    if api_balls:
        try:
            cache_file.write_text(json.dumps(api_balls, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        # Remove any stale no_data sentinel written by a previous Selenium-only run
        _no_balls_sentinel.unlink(missing_ok=True)
        log.info(f"[fetcher] {match_id}: {len(api_balls)} balls from REST API")
        return api_balls

    # Sentinel: if a previous run confirmed no balls exist, skip further work.
    if _no_balls_sentinel.exists():
        return []

    # ── Strategy 4: Parse __NEXT_DATA__ from already-cached commentary HTML ────
    # fetch_match_commentary() caches HTML as commentary_{match_id}_*.html.
    # Reuse those files to extract ball data without another browser session.
    balls = _balls_from_cached_commentary_html(match_id)
    if balls:
        try:
            cache_file.write_text(json.dumps(balls, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        log.info(f"[fetcher] {match_id}: {len(balls)} balls from cached commentary HTML")
        return balls

    # ── Strategy 5: Fetch commentary page fresh with Selenium ───────────────────
    # Only runs if HTML is not yet cached — once cached, Strategy 4 already read it.
    commentary_url = _build_commentary_url(match_url)
    url_slug2  = re.sub(r"[^a-z0-9]", "_", commentary_url.lower())[-80:]
    html_cache2 = COMMENTARY_CACHE_DIR / f"commentary_{match_id}_{url_slug2}.html"

    # Skip Selenium if fetch_match_commentary already cached the commentary HTML
    # and Strategy 1 confirmed it has no balls — Selenium will give the same result.
    if html_cache2.exists():
        log.debug(f"[fetcher] {match_id}: commentary pre-cached, no balls in SSR — skipping Selenium")
        try:
            _no_balls_sentinel.touch()
        except Exception:
            pass
        return balls  # still [] from Strategy 1

    try:
        driver2 = _make_driver(perf_logging=True)
        try:
            log.info(f"[fetcher] commentary balls (fresh Selenium fetch): {commentary_url}")
            driver2.get(commentary_url)
            time.sleep(SCORECARD_WAIT)
            html2 = driver2.page_source
            # Cache so future runs can skip Selenium
            if not html_cache2.exists():
                html_cache2.write_text(html2, encoding="utf-8")

            # First try __NEXT_DATA__ (works for recent matches)
            soup2 = BeautifulSoup(html2, "lxml")
            nd2   = _extract_next_data(soup2)
            if nd2:
                raw2  = _get_raw_balls_from_next_data(nd2)
                balls = [_normalise_ball(b) for b in raw2 if isinstance(b, dict)]
                balls = [b for b in balls if b.get("bowler_name") or b.get("batter_name")]

            # XHR fallback: capture AJAX-loaded ball data via CDP performance log
            # (older matches load ball data dynamically — not present in initial HTML)
            if not balls:
                try:
                    perf_logs = driver2.get_log("performance")
                    balls = _extract_balls_from_perf_log(driver2, perf_logs)
                    if balls:
                        log.info(f"[fetcher] {match_id}: {len(balls)} balls via XHR interception")
                except Exception as _xe:
                    log.debug(f"[fetcher] XHR interception failed for {match_id}: {_xe}")
        finally:
            driver2.quit()
    except Exception as e:
        log.debug(f"[fetcher] fresh commentary fetch failed for {match_id}: {e}")
    time.sleep(REQUEST_DELAY_SEC)

    if balls:
        try:
            cache_file.write_text(json.dumps(balls, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        log.info(f"[fetcher] {match_id}: {len(balls)} balls captured")
    else:
        log.debug(f"[fetcher] {match_id}: no ball-by-ball data found in __NEXT_DATA__")
        # Write sentinel so future runs skip the Selenium session for this match.
        try:
            _no_balls_sentinel.touch()
        except Exception:
            pass

    return balls


def _balls_from_cached_commentary_html(match_id: str) -> list[dict]:
    """
    Look for any cached commentary HTML files for *match_id*.
    Tries __NEXT_DATA__ JSON first; falls back to DOM text parsing (.commentary-text).
    """
    pattern = f"commentary_{match_id}_*.html"
    for html_file in COMMENTARY_CACHE_DIR.glob(pattern):
        try:
            html = html_file.read_text(encoding="utf-8")
            soup = BeautifulSoup(html, "lxml")

            # Try __NEXT_DATA__ first (structured, has player IDs)
            nd = _extract_next_data(soup)
            if nd:
                raw   = _get_raw_balls_from_next_data(nd)
                balls = [_normalise_ball(b) for b in raw if isinstance(b, dict)]
                balls = [b for b in balls if b.get("bowler_name") or b.get("batter_name")]
                if balls:
                    log.debug(f"[fetcher] {match_id}: {len(balls)} balls from __NEXT_DATA__ in {html_file.name}")
                    return balls

            # Fallback: parse rendered .commentary-text DOM elements (name-based, no IDs)
            balls = _parse_balls_from_commentary_html(html)
            if balls:
                log.debug(f"[fetcher] {match_id}: {len(balls)} balls from HTML DOM in {html_file.name}")
                return balls
        except Exception as e:
            log.debug(f"[fetcher] could not parse {html_file.name}: {e}")
    return []


def _parse_balls_from_commentary_html(html: str) -> list[dict]:
    """
    Parse ball-by-ball data from rendered CricHeroes commentary page HTML.

    CricHeroes renders each ball as a .commentary-text element with the format:
      "BowlerName to BatterName, outcome"
    accompanied by a <span> containing the over.ball notation (e.g. "19.5").

    Returns name-based ball dicts (no player IDs — main.py resolves via name lookup).
    """
    soup  = BeautifulSoup(html, "lxml")
    balls: list[dict] = []
    prev_over = -1
    innings   = 1

    for item in soup.select('[class*="commentary-text"]'):
        parent    = item.parent
        over_span = parent.find("span") if parent else None
        over_str  = over_span.get_text().strip() if over_span else ""
        text      = item.get_text().strip()

        try:
            parts    = over_str.split(".")
            over_num = int(parts[0])
            ball_num = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            continue

        # Innings boundary: over counter resets (jumps up after reaching 0)
        if prev_over != -1 and over_num > prev_over + 10:
            innings += 1
        prev_over = over_num

        if " to " not in text:
            continue

        bowler_name, _, rest = text.partition(" to ")
        batter_name, _, outcome = rest.partition(",")
        bowler_name = bowler_name.strip()
        # Strip dismissal detail appended to batter name in OUT balls
        for sep in (" OUT ", " c ", " st "):
            if sep in batter_name:
                batter_name = batter_name[: batter_name.index(sep)]
        batter_name = batter_name.strip()
        outcome     = outcome.strip().lower()

        if not bowler_name or not batter_name:
            continue

        runs, extras_type, is_wicket = _parse_commentary_outcome(outcome)

        balls.append({
            "over":        over_num,
            "ball":        ball_num,
            "innings":     innings,
            "bowler_name": bowler_name,
            "bowler_id":   "",
            "batter_name": batter_name,
            "batter_id":   "",
            "runs":        runs,
            "extras":      runs if extras_type else 0,
            "extras_type": extras_type,
            "is_wicket":   is_wicket,
        })

    return balls


def _parse_commentary_outcome(outcome: str) -> tuple[int, str, bool]:
    """Parse a ball outcome string → (runs, extras_type, is_wicket)."""
    is_wicket   = "out" in outcome
    extras_type = ""

    if "wide" in outcome:
        extras_type = "wide"
        m = re.search(r"(\d+)\s*run", outcome)
        runs = int(m.group(1)) if m else 1
    elif "no ball" in outcome or "noball" in outcome:
        extras_type = "no_ball"
        m = re.search(r"(\d+)\s*run", outcome)
        runs = int(m.group(1)) if m else 0
    elif "leg bye" in outcome:
        extras_type = "leg_bye"
        m = re.search(r"(\d+)\s*run", outcome)
        runs = int(m.group(1)) if m else 0
    elif " bye" in outcome:
        extras_type = "bye"
        m = re.search(r"(\d+)\s*run", outcome)
        runs = int(m.group(1)) if m else 0
    elif "four" in outcome:
        runs = 4
    elif "six" in outcome:
        runs = 6
    else:
        m = re.search(r"^(\d+)\s*run", outcome)
        runs = int(m.group(1)) if m else 0

    return runs, extras_type, is_wicket


def fetch_player_scorecard_stats(
    match_url: str,
    match_id: str,
    player_id: str,
) -> tuple[dict | None, str | None]:
    """
    Fetch a match scorecard and extract stats for ONE specific player.

    Unlike fetch_match_scorecard(), this is NOT team-specific — it searches all innings
    for the given player_id. Used for cross-team player history where the player may
    be on either team.

    Returns: (stats_dict, player_team_id) where:
      stats_dict     — merged batting+bowling+fielding stats, or None if player not found
      player_team_id — the team_id of the inning the player batted in (used to determine result)
    """
    cache_file = SCORECARD_CACHE_DIR / f"scorecard_{match_id}.html"

    def _load_from_selenium():
        log.info(f"[fetcher] fetching scorecard for player {player_id}: {match_url}")
        driver = _make_driver()
        try:
            driver.get(match_url)
            import time as _time
            _time.sleep(SCORECARD_WAIT)
            _html = driver.page_source
            cache_file.write_text(_html, encoding="utf-8")
        finally:
            driver.quit()
        import time as _time2
        _time2.sleep(REQUEST_DELAY_SEC)
        return _html

    if cache_file.exists():
        html = cache_file.read_text(encoding="utf-8")
        # Check if cached HTML has usable scorecard data in __NEXT_DATA__
        _soup_check = BeautifulSoup(html, "lxml")
        _nd_check   = _extract_next_data(_soup_check)
        _sc_check   = (_nd_check or {}).get("props", {}).get("pageProps", {}).get("scorecard")
        if not _sc_check:
            # Cache was written by team-fetch (HTML fallback path) — scorecard: [] is empty.
            # Delete and re-fetch so __NEXT_DATA__ has actual innings data.
            log.info(f"[fetcher] stale cache (empty scorecard) for {match_id} — re-fetching")
            cache_file.unlink(missing_ok=True)
            html = _load_from_selenium()
    else:
        html = _load_from_selenium()

    soup = BeautifulSoup(html, "lxml")
    nd   = _extract_next_data(soup)
    if not nd:
        return None, None

    props   = nd.get("props", {}).get("pageProps", {})
    sc_data = props.get("scorecard")
    if not sc_data or not isinstance(sc_data, list):
        return None, None

    pid_str = str(player_id)
    stats = {
        "cricheroes_player_id": pid_str,
        "runs": 0, "balls_faced": 0, "fours": 0, "sixes": 0,
        "batting_sr": 0.0, "is_out": False, "batting_position": None,
        "yet_to_bat": False,
        "overs_bowled": 0.0, "wickets": 0, "runs_conceded": 0,
        "economy": 0.0, "maidens": 0, "dot_balls": 0,
        "did_not_bowl": False,
        "catches": 0, "run_outs": 0, "stumpings": 0,
        "wides": 0, "no_balls": 0, "boundaries_conceded": 0,
    }
    found          = False
    player_team_id = None

    for inning in sc_data:
        if not isinstance(inning, dict):
            continue
        inning_team_id = str(inning.get("team_id") or "")

        # ── Batting ───────────────────────────────────────────────────────────
        for pos, batter in enumerate(inning.get("batting") or [], start=1):
            if str(batter.get("player_id") or "") != pid_str:
                continue
            found          = True
            player_team_id = player_team_id or inning_team_id
            how_out = (batter.get("how_to_out") or "").strip().lower()
            is_dnb  = how_out in ("dnb", "absent", "did not bat", "yet to bat")
            is_out  = bool(how_out and not is_dnb and how_out not in ("not out", ""))
            if is_dnb:
                stats["yet_to_bat"] = True
            else:
                stats["runs"]             = int(batter.get("runs")  or 0)
                stats["balls_faced"]      = int(batter.get("balls") or 0)
                stats["fours"]            = int(batter.get("4s")    or 0)
                stats["sixes"]            = int(batter.get("6s")    or 0)
                stats["batting_sr"]       = float(batter.get("SR")  or 0)
                stats["is_out"]           = is_out
                stats["batting_position"] = pos

        # ── "Yet to bat" list ─────────────────────────────────────────────────
        for ytb in (inning.get("to_be_bat") or []):
            if not isinstance(ytb, dict):
                continue
            if str(ytb.get("player_id") or "") == pid_str:
                found          = True
                player_team_id = player_team_id or inning_team_id
                stats["yet_to_bat"] = True

        # ── Bowling ───────────────────────────────────────────────────────────
        for bowler in (inning.get("bowling") or []):
            if str(bowler.get("player_id") or "") != pid_str:
                continue
            found    = True
            ov_raw   = float(bowler.get("overs") or 0)
            ov_whole = int(ov_raw)
            ov_extra = round((ov_raw - ov_whole) * 10)   # cricket notation: 2.3 → 3 balls
            ov_balls = int(bowler.get("balls") or 0)      # always 0 per CricHeroes API
            overs_b  = round((ov_whole * 6 + ov_extra + ov_balls) / 6, 2)
            wkts     = int(bowler.get("wickets") or 0)
            runs_c   = int(bowler.get("runs")    or 0)
            econ     = float(bowler.get("economy_rate") or 0)
            if econ == 0 and overs_b > 0:
                econ = round(runs_c / overs_b, 2)
            stats["overs_bowled"]        = overs_b
            stats["wickets"]             = wkts
            stats["runs_conceded"]       = runs_c
            stats["economy"]             = econ
            stats["maidens"]             = int(bowler.get("maidens") or 0)
            stats["dot_balls"]           = int(bowler.get("0s")      or 0)
            stats["wides"]               = int(bowler.get("wide") or bowler.get("extra_type_run_wide") or 0)
            stats["no_balls"]            = int(bowler.get("noball") or bowler.get("extra_type_run_noball") or 0)
            stats["boundaries_conceded"] = int(bowler.get("4s") or 0) + int(bowler.get("6s") or 0)

    if not found:
        return None, None

    # Fix SR if missing
    if stats["batting_sr"] == 0 and stats["balls_faced"] > 0:
        stats["batting_sr"] = round(stats["runs"] / stats["balls_faced"] * 100, 2)

    log.info(f"[fetcher] player {player_id} in match {match_id}: "
             f"{stats['runs']}r/{stats['balls_faced']}b bat | "
             f"{stats['wickets']}w/{stats['overs_bowled']}ov bowl | team={player_team_id}")
    return stats, player_team_id


def fetch_match_commentary(
    match_url: str,
    match_id: str,
) -> dict[str, list[int]]:
    """
    Fetch the commentary/ball-by-ball page for a match and extract which over
    each bowler bowled.

    CricHeroes commentary URL pattern:
      /scorecard/{id}/{slug}/commentary   (or /live or /scorecard)

    Returns: { cricheroes_player_id: [1, 3, 5] }
    Falls back to {} if commentary data is not available.
    """
    # Sentinel: if a previous run already tried all variants and found nothing,
    # skip all HTTP calls for this match — commentary data won't appear later.
    _no_data_sentinel = COMMENTARY_CACHE_DIR / f"commentary_{match_id}_no_data"
    if _no_data_sentinel.exists():
        return {}

    # Try to build the commentary URL from the match/scorecard URL
    # Typical shape: https://cricheroes.com/scorecard/12345678/tournament/teamA-vs-teamB/scorecard
    # Commentary page: replace last path segment with "commentary" or "live"
    commentary_urls: list[str] = []
    for variant in ("commentary", "live", "scorecard"):
        commentary_urls.append(_match_tab_url(match_url, variant))
    commentary_urls = list(dict.fromkeys(commentary_urls))

    # Check each variant — use cache if available
    for c_url in commentary_urls:
        url_slug = re.sub(r"[^a-z0-9]", "_", c_url.lower())[-80:]
        cache_file = COMMENTARY_CACHE_DIR / f"commentary_{match_id}_{url_slug}.html"
        if cache_file.exists():
            html = cache_file.read_text(encoding="utf-8")
        else:
            log.info(f"[fetcher] commentary fetch: {c_url}")
            # ── Strategy 1: plain HTTP — fast, and detects Cloudflare early ──
            try:
                html = _fetch_scorecard_html_via_requests(c_url)
            except _CloudflareBlockedError:
                log.info(f"[fetcher] Cloudflare blocks CI IPs for commentary — skipping: {match_id}")
                return {}
            if html:
                cache_file.write_text(html, encoding="utf-8")
            else:
                # ── Strategy 2: Selenium (local dev / fallback) ────────────────
                driver = _make_driver()
                try:
                    driver.get(c_url)
                    time.sleep(SCORECARD_WAIT)
                    html = driver.page_source
                    cache_file.write_text(html, encoding="utf-8")
                except Exception as e:
                    log.warning(f"[fetcher] commentary page failed ({e})")
                    continue
                finally:
                    driver.quit()
                time.sleep(REQUEST_DELAY_SEC)

        soup = BeautifulSoup(html, "lxml")
        nd   = _extract_next_data(soup)
        if nd:
            result = _parse_over_assignments_from_commentary(nd)
            if result:
                log.info(f"[fetcher] over assignments found for {len(result)} bowlers in {match_id}")
                return result

    log.debug(f"[fetcher] no commentary data found for {match_id}")
    # Write sentinel so future runs skip the HTTP calls for this match.
    try:
        _no_data_sentinel.touch()
    except Exception:
        pass
    return {}


def fetch_match_scorecard(
    match_url: str,
    match_id: str,
    player_name_lookup: dict | None = None,
) -> dict:
    """
    Load a match scorecard page and parse batting/bowling stats.

    Tries __NEXT_DATA__ extraction first (structured, reliable).
    Falls back to HTML table parsing for legacy/cached pages.

    Returns:
    {
      "match_id":     str,
      "overs":        int,
      "player_stats": [ { cricheroes_player_id, player_name, type, ...stats } ]
    }
    """
    cache_file = SCORECARD_CACHE_DIR / f"scorecard_{match_id}.html"
    # Team-page links point at the /summary tab, which has no batting/bowling tables.
    match_url = _match_tab_url(match_url, "scorecard")

    # ── Load from cache if available ──────────────────────────────────────────
    if cache_file.exists():
        html = cache_file.read_text(encoding="utf-8")
        # Validate: stale caches written from the wrong URL (e.g. missing "individual"
        # slug) have __NEXT_DATA__ present but scorecard: [] — detect and evict them.
        _sc = BeautifulSoup(html, "lxml")
        _nd = _extract_next_data(_sc)
        _sc_check = (_nd or {}).get("props", {}).get("pageProps", {}).get("scorecard") if _nd else None
        if _nd and not _sc_check:
            log.info(f"[fetcher] stale cache (empty scorecard) for {match_id} — re-fetching")
            cache_file.unlink(missing_ok=True)
            html = None
        else:
            log.info(f"[fetcher] scorecard cache hit: {match_id}")
    else:
        html = None

    if html is None:
        # ── Strategy 1: plain HTTP (no Selenium) — works in CI when headless Chrome is blocked
        try:
            html = _fetch_scorecard_html_via_requests(match_url)
        except _CloudflareBlockedError:
            # Cloudflare bot-protection detected — Selenium will also be blocked.
            # Return empty result immediately instead of wasting ~11s on Selenium.
            log.info(f"[fetcher] Cloudflare blocks CI IPs for scorecard — skipping: {match_url}")
            return {"match_id": match_id, "overs": 0, "player_stats": []}
        if html:
            cache_file.write_text(html, encoding="utf-8")
        else:
            # ── Strategy 2: Selenium (local dev / fallback) ────────────────────
            log.info(f"[fetcher] requests failed, using Selenium for scorecard: {match_url}")
            driver = _make_driver()
            try:
                driver.get(match_url)
                time.sleep(SCORECARD_WAIT)
                html = driver.page_source
                cache_file.write_text(html, encoding="utf-8")
            finally:
                driver.quit()
            time.sleep(REQUEST_DELAY_SEC)

    # ── Try __NEXT_DATA__ first ────────────────────────────────────────────────
    soup = BeautifulSoup(html, "lxml")
    nd   = _extract_next_data(soup)
    overs_override = 0
    if nd:
        result = _parse_scorecard_from_next_data(nd, match_id, player_name_lookup)
        if result:
            return result
        # scorecard=[] (individual/non-tournament match) — extract overs from summaryData
        props = nd.get("props", {}).get("pageProps", {})
        summary_data = (props.get("summaryData") or {}).get("data") or {}
        overs_override = int(summary_data.get("overs") or 0)

    # ── Fall back to HTML table parsing ───────────────────────────────────────
    return _parse_scorecard_html(html, match_id, overs_override=overs_override)


def _parse_scorecard_html(html: str, match_id: str, overs_override: int = 0) -> dict:
    soup = BeautifulSoup(html, "lxml")
    overs = overs_override if overs_override > 0 else _detect_overs_from_page(soup)
    player_stats = []

    # ── Batting tables ────────────────────────────────────────────────────────
    # CricHeroes batting table has headers (old): Batsman | Dismissal | R | B | 4s | 6s | SR
    #                                      (new): Batters | R | B | 4s | 6s | SR
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(h in headers for h in ["R", "RUNS", "BATSMAN", "BATTERS"]):
            continue
        # Skip if this is a bowling table (has economy/wickets columns or "Bowlers" header)
        if any(h in headers for h in ["WICKETS", "ECON", "ECONOMY", "ECO", "BOWLERS"]):
            continue

        for row in table.find_all("tr")[1:]:   # skip header row
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            # Player link → extract ID
            player_link = cells[0].find("a", href=True)
            pid, pname = "", cells[0].get_text(strip=True)
            if player_link:
                pid   = _player_id_from_url(player_link["href"])
                pname = player_link.get_text(strip=True)

            if not pid and not pname:
                continue

            # Skip extras / totals rows
            if pname.lower() in ("extras", "total", "did not bat", "fall of wickets"):
                continue

            # Column positions vary — try to map by header
            col = {h: i for i, h in enumerate(headers)}
            try:
                runs       = _int_cell(cells, col.get("R", col.get("RUNS", 2)))
                balls      = _int_cell(cells, col.get("B", col.get("BALLS", 3)))
                fours      = _int_cell(cells, col.get("4S", col.get("FOURS", 4)))
                sixes      = _int_cell(cells, col.get("6S", col.get("SIXES", 5)))
                sr         = _float_cell(cells, col.get("SR", col.get("STRIKE RATE", 6)))
                dismissal  = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                is_out     = dismissal.lower() not in ("not out", "dnb", "did not bat", "")
            except (IndexError, ValueError):
                continue

            # Skip JS-unpopulated mini-scorecard rows (runs=0, balls=0, sr=0)
            # These appear on individual/non-tournament matches where CricHeroes renders
            # the R column empty and populates it via JavaScript after page load.
            if runs == 0 and balls == 0 and sr == 0 and pid:
                continue

            # CricHeroes mini-scorecard: R column is empty (JS-rendered) but SR is populated.
            # Back-calculate runs from SR * balls / 100 when runs=0 but balls and SR are known.
            if runs == 0 and balls > 0 and sr > 0:
                runs = round(sr * balls / 100)

            player_stats.append({
                "cricheroes_player_id": pid,
                "player_name":   pname,
                "type":          "batting",
                "runs":          runs,
                "balls_faced":   balls,
                "fours":         fours,
                "sixes":         sixes,
                "batting_sr":    sr if sr else (round(runs / balls * 100, 2) if balls else 0),
                "is_out":        is_out,
                "batting_position": None,
            })

    # ── Bowling tables ────────────────────────────────────────────────────────
    # Headers (old): Bowler | O | M | R | W | Econ
    # Headers (new): Bowlers | O | M | R | W | Eco
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(h in headers for h in ["W", "WICKETS", "WKTS", "BOWLERS"]):
            continue
        if not any(h in headers for h in ["ECON", "ECONOMY", "ER", "ECO", "BOWLERS"]):
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            player_link = cells[0].find("a", href=True)
            pid, pname = "", cells[0].get_text(strip=True)
            if player_link:
                pid   = _player_id_from_url(player_link["href"])
                pname = player_link.get_text(strip=True)

            if not pid and not pname:
                continue

            col = {h: i for i, h in enumerate(headers)}
            try:
                _ov_raw  = _float_cell(cells, col.get("O", col.get("OVERS", 1)))
                _ov_int  = int(_ov_raw)
                _ov_dec  = round((_ov_raw - _ov_int) * 10)  # cricket notation: 2.3 → 3 balls
                overs_b  = round((_ov_int * 6 + _ov_dec) / 6, 2)
                maidens  = _int_cell(cells,   col.get("M", col.get("MAIDENS", 2)))
                runs_c   = _int_cell(cells,   col.get("R", col.get("RUNS", 3)))
                wkts     = _int_cell(cells,   col.get("W", col.get("WICKETS", col.get("WKTS", 4))))
                economy  = _float_cell(cells, col.get("ECON", col.get("ECO", col.get("ECONOMY", col.get("ER", 5)))))
            except (IndexError, ValueError):
                continue

            if economy == 0 and overs_b > 0:
                economy = round(runs_c / overs_b, 2)

            # Skip JS-unpopulated mini-scorecard bowling rows
            if overs_b == 0 and runs_c == 0 and wkts == 0 and pid:
                continue

            # Back-calculate runs_conceded from economy when R column is JS-empty
            if runs_c == 0 and overs_b > 0 and economy > 0:
                runs_c = round(economy * overs_b)

            player_stats.append({
                "cricheroes_player_id": pid,
                "player_name":   pname,
                "type":          "bowling",
                "overs_bowled":  overs_b,
                "wickets":       wkts,
                "runs_conceded": runs_c,
                "economy":       economy,
                "maidens":       maidens,
            })

    log.info(f"[fetcher] scorecard {match_id}: {len(player_stats)} player-stat rows, {overs}-ov")
    return {"match_id": match_id, "overs": overs, "player_stats": player_stats}


def _extract_date_from_info(info: str) -> str | None:
    """
    Parse a date like "12 Jan 2025" or "12 January 2025" from match info text.
    Returns ISO date string "YYYY-MM-DD" or None.
    """
    from datetime import datetime
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y"):
        m = re.search(r'\d{1,2}\s+\w+\s+\d{4}', info or "")
        if m:
            try:
                return datetime.strptime(m.group(0), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _detect_overs_from_page(soup: BeautifulSoup) -> int:
    """Try to find overs in page title, headings, or info sections."""
    for el in soup.select("h1, h2, h3, title, [class*='match-info'], [class*='overs'], [class*='format']"):
        text = el.get_text()
        m = re.search(r'(\d+)\s*[Oo]vers?', text)
        if m:
            v = int(m.group(1))
            for fmt in [6, 10, 15, 20]:
                if v <= fmt:
                    return fmt
    return 20


def _int_cell(cells, idx) -> int:
    if idx is None or idx >= len(cells):
        return 0
    try:
        return int(cells[idx].get_text(strip=True).replace("*", "") or 0)
    except (ValueError, TypeError):
        return 0


def _float_cell(cells, idx) -> float:
    if idx is None or idx >= len(cells):
        return 0.0
    try:
        return float(cells[idx].get_text(strip=True).replace("*", "") or 0)
    except (ValueError, TypeError):
        return 0.0


# ── Leaderboard helpers ───────────────────────────────────────────────────────

def _parse_leaderboard_stat(stat_str: str) -> dict:
    """
    Leaderboard stat strings look like:
      "452 runs"  |  "23 wickets"  |  "12 catches"
    """
    s = stat_str.strip().lower()
    m = re.match(r"(\d+)\s+(.+)", s)
    if not m:
        return {"raw": stat_str}
    val  = int(m.group(1))
    kind = m.group(2).strip()
    return {"value": val, "kind": kind, "raw": stat_str}


# ── Direct scraper (replaces cricheroes library) ──────────────────────────────

def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    """Extract page data embedded by Next.js.

    Older CricHeroes pages carry a __NEXT_DATA__ script. The current App Router
    pages do not; their data is streamed as self.__next_f.push() chunks, which
    _next_data_from_rsc() turns back into the same props.pageProps shape.
    """
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag:
        try:
            return json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            return None
    return _next_data_from_rsc(soup)


_RSC_PUSH_RE   = re.compile(r'self\.__next_f\.push\(\[1,(".*")\]\)\s*$', re.S)
_RSC_INNING_RE = re.compile(r'\[\{"team_id":\d+,"inning":\{')


def _next_data_from_rsc(soup: BeautifulSoup) -> dict | None:
    """Rebuild {"props": {"pageProps": {...}}} from App Router flight data."""
    parts: list[str] = []
    for s in soup.find_all("script"):
        src = s.string or ""
        if "self.__next_f.push" not in src:
            continue
        m = _RSC_PUSH_RE.search(src.strip())
        if not m:
            continue
        try:
            parts.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    if not parts:
        return None
    text    = "".join(parts)
    decoder = json.JSONDecoder()

    page_props: dict = {}
    i = text.find('{"miniScorecard"')
    if i >= 0:
        try:
            obj, _ = decoder.raw_decode(text[i:])
            page_props["miniScorecard"] = obj.get("miniScorecard") or {}
            page_props["summaryData"]   = obj.get("summaryData") or {}
        except json.JSONDecodeError:
            pass

    m = _RSC_INNING_RE.search(text)
    if m:
        try:
            innings, _ = decoder.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            innings = []
        for inn in innings:
            # new format nests the innings metadata under "inning"; the parser expects the number
            meta = inn.get("inning")
            if isinstance(meta, dict):
                inn["inning_info"] = meta
                inn["inning"]      = meta.get("inning")
        page_props["scorecard"] = innings

    if not page_props:
        return None
    return {"props": {"pageProps": page_props}}


_SKILL_MAP = {
    "BAT":  "batsman",
    "BALL": "bowler",
    "ALL":  "all_rounder",
    "WK":   "wicket_keeper",
}


def _parse_next_data_team(nd: dict, team_name_fallback: str) -> tuple[str, str, list, list]:
    """
    Walk __NEXT_DATA__ props for CricHeroes team profile pages.

    Actual pageProps structure (observed 2026):
      pageProps.members.data.members  → list of player dicts
      pageProps.matches.data          → list of match dicts  (on /matches sub-page)
      pageProps.members.data.name     → team name
      pageProps.members.data.team_logo → team logo URL
    """
    props = nd.get("props", {}).get("pageProps", {})

    # ── Team metadata ─────────────────────────────────────────────────────────
    members_resp = props.get("members") or {}
    members_data = (members_resp.get("data") or {}) if isinstance(members_resp, dict) else {}

    team_name = (
        members_data.get("name")
        or members_data.get("short_name")
        or team_name_fallback
    )
    team_logo = members_data.get("team_logo") or members_data.get("logo") or ""

    # ── Players ───────────────────────────────────────────────────────────────
    raw_players = members_data.get("members") or []
    players = []
    for rp in raw_players:
        if not isinstance(rp, dict):
            continue
        pid = str(rp.get("player_id") or "")
        if not pid:
            continue
        skill = (rp.get("player_skill") or "").upper()
        players.append({
            "cricheroes_id": pid,
            "name":          (rp.get("name") or f"Player_{pid}").strip(),
            "role":          _SKILL_MAP.get(skill, "unknown"),
            "batting_style": "",
            "bowling_style": "",
            "profile_url":   f"/player-profile/{pid}",
            "photo_url":     rp.get("profile_photo") or "",
        })

    # ── Matches ───────────────────────────────────────────────────────────────
    matches_resp = props.get("matches") or {}
    raw_matches  = (matches_resp.get("data") or []) if isinstance(matches_resp, dict) else []
    matches = []
    for rm in (raw_matches if isinstance(raw_matches, list) else []):
        if not isinstance(rm, dict):
            continue
        mid = str(rm.get("match_id") or "")
        if not mid:
            continue

        # Opponent = whichever team is NOT Game Changers
        our_id   = str(TEAM_ID)
        team_a_id = str(rm.get("team_a_id") or "")
        opponent  = rm.get("team_b") if team_a_id == our_id else rm.get("team_a")
        opponent  = opponent or "Unknown"

        # Result
        winning_id = str(rm.get("winning_team_id") or "")
        res_text   = (rm.get("match_result") or "").lower()
        if winning_id == our_id:
            result = "win"
        elif winning_id and winning_id != our_id:
            result = "loss"
        elif "tied" in res_text or "tie" in res_text:
            result = "draw"
        else:
            result = "no_result"

        match_date = (rm.get("match_start_time") or rm.get("created_date") or "")[:10]
        # Scorecard URL: /scorecard/{id}/{tournament-slug}/{teamA}-vs-{teamB}/scorecard
        # When tournament_name is empty (individual match) use "individual" as the slug.
        # Without it CricHeroes serves the summary tab → __NEXT_DATA__.scorecard is empty.
        _t   = re.sub(r"[^a-z0-9]+", "-", (rm.get("tournament_name") or "individual").lower()).strip("-") or "individual"
        _ta  = re.sub(r"[^a-z0-9]+", "-", (rm.get("team_a") or "").lower()).strip("-")
        _tb  = re.sub(r"[^a-z0-9]+", "-", (rm.get("team_b") or "").lower()).strip("-")
        match_url = f"https://cricheroes.com/scorecard/{mid}/{_t}/{_ta}-vs-{_tb}/scorecard"

        matches.append({
            "cricheroes_match_id": mid,
            "match_date":  match_date,
            "opponent":    opponent,
            "overs":       int(rm.get("overs") or 20),
            "result":      result,
            "venue":       rm.get("ground_name") or rm.get("city_name") or None,
            "tournament":  rm.get("tournament_name") or "",
            "match_url":   match_url,
            "raw_result":  (rm.get("match_summary") or {}).get("summary") or "",
        })

    return team_name, team_logo, players, matches


def _scrape_players_from_dom(soup: BeautifulSoup) -> list[dict]:
    """
    DOM fallback: find all <a href*="/player-profile/"> links and extract player info.
    Works regardless of surrounding element class names.
    """
    players: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=re.compile(r"/player-profile/")):
        href = a.get("href", "")
        pid  = _player_id_from_url(href)
        if not pid or pid in seen:
            continue
        seen.add(pid)

        # Extract name from the URL slug — reliable because CricHeroes always puts
        # the clean name in the URL path, whereas get_text() would concatenate badge
        # labels like "captain", "Hard Hitter", "Aspirant" into the player name.
        # URL shape: /player-profile/{id}/{first-last}  (may have a sub-path suffix)
        # Known sub-paths that are NOT the player name:
        _SUBPATHS = {"matches", "batting", "bowling", "fielding", "stats", "overview", "career"}
        slug_parts = [p for p in href.split("/") if p]
        # Strip any trailing sub-page path segment (e.g. ".../gajanan-awsare/matches")
        while slug_parts and slug_parts[-1].lower() in _SUBPATHS:
            slug_parts = slug_parts[:-1]
        if len(slug_parts) >= 3:
            name = slug_parts[-1].replace("-", " ").title()
        else:
            # Fallback: first direct text node only
            direct = next(
                (t.strip() for t in a.find_all(string=True, recursive=False) if t.strip()),
                None,
            )
            name = direct or f"Player_{pid}"

        # Try to find role/style text in nearby elements
        sub = ""
        parent = a.parent
        for _ in range(4):
            if parent is None:
                break
            for cls_hint in ["subtitle", "role", "position", "style", "pmd-card-subtitle"]:
                el = parent.find(class_=re.compile(cls_hint, re.I))
                if el:
                    sub = el.get_text(strip=True)
                    break
            if sub:
                break
            parent = parent.parent

        photo_url = ""
        img = a.find("img")
        if not img and a.parent:
            img = a.parent.find("img")
        if img:
            photo_url = img.get("src") or img.get("data-src") or ""

        players.append({
            "cricheroes_id": pid,
            "name":          name,
            "role":          _map_role(sub),
            "batting_style": _parse_batting_style(sub),
            "bowling_style": _parse_bowling_style(sub),
            "profile_url":   href,
            "photo_url":     photo_url,
        })

    return players


def _click_and_scrape_matches(
    driver,
    team_name: str,
    max_matches: int,
    team_url: str | None = None,
) -> list[dict]:
    """
    Navigate to the matches sub-page and scroll to collect all match links.
    CricHeroes uses infinite-scroll — each Keys.END triggers loading ~12 more.

    Strategy:
      1. Read initial __NEXT_DATA__ (first ~12 structured matches).
      2. Keep scrolling; scrape DOM match cards for older matches.
      3. Merge both sources, deduplicate by match_id.
      4. Stop when max_matches reached or no new matches appear after scroll.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    # team_url is "{id}/{slug}" of the team being fetched; fall back to the configured team
    matches_url = f"https://cricheroes.com/team-profile/{team_url or TEAM_URL}/matches"
    log.info(f"[fetcher] Loading matches page: {matches_url}")
    driver.get(matches_url)
    time.sleep(PAGE_LOAD_WAIT)

    all_matches: dict[str, dict] = {}   # match_id → match dict (dedup)

    prev_count = 0
    for scroll_round in range(40):      # 40 scrolls × ~12 per page = ~480 matches max
        soup_now = BeautifulSoup(driver.page_source, "lxml")

        # Structured matches from __NEXT_DATA__ (only populated on initial load)
        nd = _extract_next_data(soup_now)
        if nd:
            _, _, _, nd_matches = _parse_next_data_team(nd, team_name)
            for m in nd_matches:
                mid = m.get("cricheroes_match_id")
                if mid and mid not in all_matches:
                    all_matches[mid] = m

        # DOM-scraped matches (older history loaded via infinite scroll)
        for m in _scrape_matches_from_dom(soup_now, team_name):
            mid = m.get("cricheroes_match_id")
            if mid and mid not in all_matches:
                all_matches[mid] = m

        cur_count = len(all_matches)
        log.info(f"[fetcher] matches scroll {scroll_round+1}: {cur_count} total")

        if cur_count >= max_matches:
            break
        if cur_count == prev_count:
            break       # no new matches after scroll — end of list
        prev_count = cur_count

        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
        time.sleep(2)

    result = list(all_matches.values())
    log.info(f"[fetcher] total matches collected: {len(result)}")
    return result[:max_matches]


def _scrape_matches_from_dom(soup: BeautifulSoup, team_name: str) -> list[dict]:
    """
    DOM fallback: find match links in both old (/match-stats/) and new (/scorecard/) formats.
    Handles CricHeroes infinite-scroll DOM for older match history.
    """
    matches: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=re.compile(r"/(match-stats|scorecard)/")):
        href = a.get("href", "")
        mid  = _extract_match_id_from_url(href)
        if not mid or mid in seen:
            continue
        seen.add(mid)

        # Construct proper scorecard URL for /match-stats/ legacy links
        if "/match-stats/" in href and not href.startswith("http"):
            href = f"https://cricheroes.com{href}"

        # Walk up the DOM looking for a match card container
        card = a.parent
        for _ in range(6):
            if card is None:
                break
            card = card.parent

        result_text  = ""
        info_text    = ""
        score_list   = []
        tournament   = ""

        if card:
            # Result: look for text containing "won", "lost", "tied"
            result_tag = card.find(string=re.compile(r'\bwon\b|\blost\b|\btied\b|\bno result\b', re.I))
            if result_tag:
                result_text = result_tag.strip()

            # Info (venue, overs, date) — look for comma-separated text
            for el in card.find_all(string=True):
                txt = el.strip()
                if re.search(r'\d+\s*[Oo]vers?', txt):
                    info_text = txt
                    break

            # Scores
            for el in card.find_all(string=re.compile(r'\d+/\d+')):
                score_list.append(el.strip())

        full_url = f"{CH_BASE_URL}{href}" if href.startswith("/") else href
        match_date = _extract_date_from_info(info_text)
        matches.append({
            "cricheroes_match_id": mid,
            "match_date":  match_date,
            "opponent":    _extract_opponent(score_list, team_name),
            "overs":       _extract_overs_from_info(info_text),
            "result":      _map_result(result_text, team_name),
            "venue":       info_text.split(",")[0].strip() if "," in info_text else None,
            "tournament":  tournament,
            "match_url":   full_url,
            "raw_result":  result_text,
        })

    return matches


# ── Main fetch pipeline ───────────────────────────────────────────────────────

def fetch_all_data(max_matches: int = 100, months: int = 18,
                   skip_commentary: bool = False,
                   team_id: str | None = None,
                   team_slug: str | None = None,
                   days_hint: int | None = None) -> dict:
    """
    Full pipeline — direct Selenium scraper, bypassing the cricheroes library
    (which used hardcoded DOM IDs that CricHeroes has since removed).

    Strategy:
      1. Load team profile page via Selenium.
      2. Extract __NEXT_DATA__ JSON (Next.js embedded data) — fast and complete.
      3. If __NEXT_DATA__ has no players, fall back to DOM scraping via href patterns.
      4. Fetch individual match scorecards (existing logic, unaffected by library breakage).
      5. Fetch all-time player career stats from player profile pages.

    Returns:
    {
      "team_name":     str,
      "team_logo":     str,
      "players":       [ {cricheroes_id, name, role, ...} ],
      "matches":       [ {cricheroes_match_id, overs, result, ...} ],
      "scorecards":    { match_id: {overs, player_stats} },
      "leaderboard":   { "batting": [...], "bowling": [...], "fielding": [...] },
      "player_profiles": { cricheroes_id: profile_dict },
    }
    """
    # Allow caller to override team (open-source: any team can use this tool)
    global TEAM_ID
    _team_id   = team_id   or TEAM_ID
    _team_slug = team_slug or TEAM_SLUG
    _team_url  = f"{_team_id}/{_team_slug}"

    # Use cricheroes.com directly (cricheroes.in redirects there; go direct to avoid
    # Cloudflare hitting the redirect chain for sub-pages like /matches)
    team_url  = f"https://cricheroes.com/team-profile/{_team_url}"
    team_name = _team_slug.replace("-", " ").title()
    team_logo = ""

    log.info(f"[fetcher] Loading team page: {team_url}")

    players: list[dict] = []
    matches: list[dict] = []
    leaderboard: dict   = {"batting": [], "bowling": [], "fielding": []}

    driver = _make_driver()
    try:
        driver.get(team_url)
        time.sleep(PAGE_LOAD_WAIT)
        soup = BeautifulSoup(driver.page_source, "lxml")

        # ── Try __NEXT_DATA__ for players ──────────────────────────────────────
        nd = _extract_next_data(soup)
        if nd:
            log.info("[fetcher] __NEXT_DATA__ found — parsing team data")
            team_name, team_logo, players, matches = _parse_next_data_team(nd, team_name)
            log.info(f"[fetcher] __NEXT_DATA__: {len(players)} players, {len(matches)} matches")
        else:
            log.warning("[fetcher] __NEXT_DATA__ not found on team page")

        # ── DOM fallback for players ───────────────────────────────────────────
        if not players:
            log.info("[fetcher] Falling back to DOM scraping for players")
            players = _scrape_players_from_dom(soup)
            log.info(f"[fetcher] DOM scrape: {len(players)} players")

        # ── Matches sub-page (always fetch — main page may not have all matches) ─
        matches = _click_and_scrape_matches(driver, team_name, max_matches, team_url=_team_url)

    finally:
        driver.quit()

    # Patch TEAM_ID in module scope so scorecard parser uses the right team filter
    TEAM_ID = _team_id

    # ── CI fallback: seed players from team.json if Selenium was blocked ──────
    # When GitHub Actions IPs are blocked by CricHeroes' bot-detection, headless
    # Chrome returns a challenge page — giving 0 players and 0 matches.
    # Solution: seed from the known player list in team.json, then use the
    # REST API (fetch_team_match_list) to discover matches — no Selenium needed.
    if not players:
        log.warning("[fetcher] Selenium returned 0 players — using team.json player_ids seed")
        players = _load_player_seeds()

    # The team page lists only the ~12 most recent matches, so always merge in the
    # per-player match histories (REST API). Where both sources know a match, the
    # REST entry wins: it carries the reliable date, opponent and result.
    if players:
        if not CH_API_KEY:
            log.error("[fetcher] CH_API_KEY is not set — the REST API returns no matches without it, "
                      "so only the ~12 matches on the team page will be fetched")
        try:
            api_matches = fetch_team_match_list(months=months, players=players, days_hint=days_hint)
        except Exception as _e:
            log.warning(f"[fetcher] REST API match list failed: {_e}")
            api_matches = []
        if api_matches:
            merged = {m["cricheroes_match_id"]: m for m in matches if m.get("cricheroes_match_id")}
            from_page = len(merged)
            merged.update({m["cricheroes_match_id"]: m for m in api_matches})
            matches = list(merged.values())
            log.info(
                f"[fetcher] match list merged: team page {from_page} + REST API {len(api_matches)} "
                f"= {len(matches)} unique matches"
            )
        elif not matches:
            log.warning("[fetcher] no matches from the team page or the REST API")

    # ── Filter by date: keep only matches within requested months ─────────────
    if months and months > 0:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=months * 30)).isoformat()
        before_filter = len(matches)
        matches = [m for m in matches if (m.get("match_date") or "9999") >= cutoff]
        log.info(f"[fetcher] date filter ({months}mo cutoff {cutoff}): {before_filter} → {len(matches)} matches")

    # ── Build player name → cricheroes_id lookup for fielding attribution ─────
    player_name_lookup = {p["name"].lower(): str(p["cricheroes_id"]) for p in players}

    # ── Scorecards (individual match pages) ───────────────────────────────────
    scorecards: dict = {}
    for i, match in enumerate(matches[:max_matches]):
        mid  = match.get("cricheroes_match_id", "")
        murl = match.get("match_url", "")
        if not mid or not murl:
            log.warning(f"[fetcher] skipping match {i+1} — no ID or URL")
            continue

        log.info(f"[fetcher] scorecard {i+1}/{min(len(matches), max_matches)}: {mid}")
        sc = fetch_match_scorecard(murl, mid, player_name_lookup)
        if sc.get("match_date"):
            match["match_date"] = sc["match_date"]

        # Phase 4: enrich player stats with over assignments from commentary.
        # Skip if scorecard returned no player stats — commentary pages hit the
        # same Cloudflare block, so fetching them would just waste ~33s per match.
        if not skip_commentary and sc.get("player_stats"):
            over_map = fetch_match_commentary(murl, mid)
            if over_map:
                for ps in sc.get("player_stats", []):
                    pid = str(ps.get("cricheroes_player_id", ""))
                    if pid and pid in over_map:
                        ps["over_assignments"] = json.dumps(over_map[pid])

            # Ball-by-ball (batter vs bowler): XHR interception via CDP
            balls = fetch_commentary_balls(murl, mid)
            if balls:
                sc["commentary_balls"] = balls
        elif skip_commentary:
            log.debug(f"[fetcher] commentary skipped for {mid} (--skip-commentary)")
        else:
            log.debug(f"[fetcher] commentary skipped for {mid} (no player stats)")

        scorecards[mid] = sc

        # Prefer overs from scorecard page (more reliable than match.info)
        if sc.get("overs"):
            match["overs"] = sc["overs"]

    # ── Player career profiles (all-time stats from CricHeroes) ───────────────
    from player_fetcher import fetch_all_player_profiles
    log.info("[fetcher] Fetching all-time player career stats...")
    player_profiles = fetch_all_player_profiles(players)

    return {
        "team_name":       team_name,
        "team_logo":       team_logo,
        "players":         players,
        "matches":         matches,
        "scorecards":      scorecards,
        "leaderboard":     leaderboard,
        "player_profiles": player_profiles,
    }


# ── Team-centric match list (for fetch-player-history) ───────────────────────

def _load_player_seeds() -> list[dict]:
    """
    Load player seed list from team.json player_ids field.
    Used as fallback when Selenium is blocked in CI (GitHub Actions).
    Returns a list of minimal player dicts compatible with fetch_all_data().
    """
    config_file = Path(__file__).parent.parent / "team.json"
    if not config_file.exists():
        return []
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        seeds = []
        for s in (data.get("player_ids") or []):
            pid = str(s.get("id", ""))
            if not pid:
                continue
            seeds.append({
                "cricheroes_id":  pid,
                "name":           s.get("name", f"Player_{pid}"),
                "role":           s.get("role", "unknown"),
                "batting_style":  None,
                "bowling_style":  None,
                "profile_url":    f"/player-profile/{pid}",
                "photo_url":      "",
            })
        log.info(f"[fetcher] player seeds loaded from team.json: {len(seeds)} players")
        return seeds
    except Exception as e:
        log.warning(f"[fetcher] could not load player seeds from team.json: {e}")
        return []


def _fetch_scorecard_html_via_requests(match_url: str) -> str | None:
    """
    Try to fetch a CricHeroes scorecard page via plain HTTP (no Selenium).

    Returns raw HTML if scorecard/Next.js data was found, None otherwise.
    """
    import requests as _req
    import urllib3
    urllib3.disable_warnings()
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            ),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection":      "keep-alive",
        }
        r = _req.get(match_url, headers=headers, timeout=20, verify=False, allow_redirects=True)
        if r.status_code != 200 or len(r.text) < 5000:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        nd = _extract_next_data(soup)
        if nd or "scorecard" in r.text or "miniScoreCardData" in r.text or "self.__next_f" in r.text:
            log.info(f"[fetcher] requests scorecard OK (no Selenium): {match_url}")
            return r.text
        # __NEXT_DATA__ absent — check for Cloudflare challenge page.
        snippet = r.text[:3000].lower()
        if any(m in snippet for m in (
            "just a moment", "checking your browser", "cf-challenge",
            "cloudflare ray id", "enable javascript", "security check to access",
            "please stand by", "ddos protection by cloudflare",
        )):
            raise _CloudflareBlockedError(match_url)
        return None
    except _CloudflareBlockedError:
        raise
    except Exception as e:
        log.debug(f"[fetcher] requests scorecard failed: {e}")
        return None


def fetch_team_match_list(
    months: int = 18,
    max_matches: int = 500,
    players: list[dict] | None = None,
    days_hint: int | None = None,
) -> list[dict]:
    """
    Fetch all Game Changers matches for the last N months across all team IDs in config.
    """
    import sys, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(__file__)))
    from player_fetcher import fetch_player_match_list
    import db as _db
    from config import TEAM_IDS

    if players is None or len(players) == 0:
        players = _db.get_game_changers_players()
    if not players:
        players = _load_player_seeds()
    all_nc: dict[str, dict] = {}

    _window_label = (
        f"API window: {months} month(s), effective filter: {days_hint} day(s)"
        if days_hint is not None
        else f"{months} months"
    )
    log.info(f"[fetcher] building team match list from {len(players)} player histories "
             f"({_window_label}, team_ids={list(TEAM_IDS)})")

    for player in players:
        pid   = str(player["cricheroes_id"])
        pname = player["name"]
        try:
            pmatches = fetch_player_match_list(pid, pname, months=months)
        except Exception as e:
            log.warning(f"[fetcher] match list error for {pname}: {e}")
            continue

        new_found = 0
        for m in pmatches:
            team_a_id = str(m.get("team_a_id", ""))
            team_b_id = str(m.get("team_b_id", ""))
            if team_a_id not in TEAM_IDS and team_b_id not in TEAM_IDS:
                continue                                 # not a Game Changers team match

            mid = m["match_id"]
            if mid in all_nc:
                continue                                 # already have this match

            winning_id = str(m.get("winning_team_id", ""))
            our_team_id = team_a_id if team_a_id in TEAM_IDS else team_b_id
            opponent   = (m.get("team_b") if team_a_id in TEAM_IDS else m.get("team_a")) or "Unknown"
            result     = ("win"      if winning_id == our_team_id else
                          "no_result" if not winning_id           else "loss")

            all_nc[mid] = {
                "cricheroes_match_id": mid,
                "match_date":  m.get("match_date"),
                "match_url":   m.get("scorecard_url"),
                "opponent":    opponent,
                "overs":       m.get("overs", 0),
                "result":      result,
                "tournament":  m.get("tournament", ""),
                "venue":       None,
            }
            new_found += 1

        log.info(f"[fetcher]   {pname}: {len(pmatches)} matches → {new_found} new Game Changers "
                 f"(running total: {len(all_nc)})")

    result = list(all_nc.values())
    _summary_label = (
        f"in last {days_hint} day(s) (API window: {months} month(s))"
        if days_hint is not None
        else f"in last {months} months"
    )
    log.info(f"[fetcher] team match list: {len(result)} Game Changers matches {_summary_label}")
    return result


def fetch_match_scorecard_all_players(
    match_url: str,
    match_id: str,
) -> list[dict] | None:
    """
    Fetch a match scorecard and return batting/bowling stats for EVERY player
    in BOTH innings — no team filtering.

    Handles stale cache (scorecard: [] written by team HTML-fallback fetch) by
    deleting the cache and re-fetching via Selenium.

    Returns a list of per-player stat dicts, or None if no data is available.
    Each dict contains: cricheroes_player_id, player_name, _team_id, plus all
    batting/bowling/fielding stat fields matching player_match_stats columns.
    """
    cache_file = SCORECARD_CACHE_DIR / f"scorecard_{match_id}.html"

    def _load_fresh():
        log.info(f"[fetcher] fetching scorecard (all players): {match_url}")
        driver = _make_driver()
        try:
            driver.get(match_url)
            time.sleep(SCORECARD_WAIT)
            _html = driver.page_source
            cache_file.write_text(_html, encoding="utf-8")
        finally:
            driver.quit()
        time.sleep(REQUEST_DELAY_SEC)
        return _html

    if cache_file.exists():
        html     = cache_file.read_text(encoding="utf-8")
        _soup_c  = BeautifulSoup(html, "lxml")
        _nd_c    = _extract_next_data(_soup_c)
        _sc_check = (_nd_c or {}).get("props", {}).get("pageProps", {}).get("scorecard") if _nd_c else None
        if not _sc_check:
            log.info(f"[fetcher] stale cache (empty scorecard) for {match_id} — re-fetching")
            cache_file.unlink(missing_ok=True)
            html = _load_fresh()
    else:
        html = _load_fresh()

    soup    = BeautifulSoup(html, "lxml")
    nd      = _extract_next_data(soup)
    if not nd:
        return None
    sc_data = nd.get("props", {}).get("pageProps", {}).get("scorecard")
    if not sc_data or not isinstance(sc_data, list):
        return None

    # Collect all team IDs so we can assign the bowling team correctly
    team_ids: list[str] = []
    for inn in sc_data:
        if isinstance(inn, dict):
            tid = str(inn.get("team_id") or "")
            if tid and tid not in team_ids:
                team_ids.append(tid)

    per_player: dict[str, dict] = {}

    def _get(pid: str, name: str, team_id: str) -> dict:
        if pid not in per_player:
            per_player[pid] = {
                "cricheroes_player_id": pid,
                "player_name":  name,
                "_team_id":     team_id,
                "runs": 0, "balls_faced": 0, "fours": 0, "sixes": 0,
                "batting_sr": 0.0, "is_out": False, "batting_position": None,
                "yet_to_bat": False,
                "overs_bowled": 0.0, "wickets": 0, "runs_conceded": 0,
                "economy": 0.0, "maidens": 0, "dot_balls": 0,
                "did_not_bowl": False,
                "catches": 0, "run_outs": 0, "stumpings": 0,
                "wides": 0, "no_balls": 0, "boundaries_conceded": 0,
            }
        return per_player[pid]

    for inning in sc_data:
        if not isinstance(inning, dict):
            continue
        batting_team_id = str(inning.get("team_id") or "")
        bowling_team_id = next((t for t in team_ids if t != batting_team_id), batting_team_id)

        # ── Batting ───────────────────────────────────────────────────────────
        for pos, batter in enumerate(inning.get("batting") or [], start=1):
            pid = str(batter.get("player_id") or "")
            if not pid:
                continue
            p       = _get(pid, batter.get("name") or "", batting_team_id)
            how_out = (batter.get("how_to_out") or "").strip().lower()
            is_dnb  = how_out in ("dnb", "absent", "did not bat", "yet to bat")
            is_out  = bool(how_out and not is_dnb and how_out not in ("not out", ""))
            if is_dnb:
                p["yet_to_bat"] = True
            else:
                p["runs"]             = int(batter.get("runs")  or 0)
                p["balls_faced"]      = int(batter.get("balls") or 0)
                p["fours"]            = int(batter.get("4s")    or 0)
                p["sixes"]            = int(batter.get("6s")    or 0)
                p["batting_sr"]       = float(batter.get("SR")  or 0)
                p["is_out"]           = is_out
                p["batting_position"] = pos

        # ── Yet to bat ────────────────────────────────────────────────────────
        for ytb in (inning.get("to_be_bat") or []):
            if not isinstance(ytb, dict):
                continue
            pid = str(ytb.get("player_id") or "")
            if not pid:
                continue
            p = _get(pid, ytb.get("name") or "", batting_team_id)
            p["yet_to_bat"] = True

        # ── Bowling (bowlers are from the other team) ─────────────────────────
        for bowler in (inning.get("bowling") or []):
            pid = str(bowler.get("player_id") or "")
            if not pid:
                continue
            p        = _get(pid, bowler.get("name") or "", bowling_team_id)
            ov_raw   = float(bowler.get("overs") or 0)
            ov_whole = int(ov_raw)
            ov_extra = round((ov_raw - ov_whole) * 10)   # cricket notation: 2.3 → 3 balls
            ov_balls = int(bowler.get("balls") or 0)      # always 0 per CricHeroes API
            overs_b  = round((ov_whole * 6 + ov_extra + ov_balls) / 6, 2)
            runs_c   = int(bowler.get("runs")      or 0)
            wkts     = int(bowler.get("wickets")   or 0)
            econ     = float(bowler.get("economy_rate") or 0)
            if econ == 0 and overs_b > 0:
                econ = round(runs_c / overs_b, 2)
            p["overs_bowled"]        = overs_b
            p["wickets"]             = wkts
            p["runs_conceded"]       = runs_c
            p["economy"]             = econ
            p["maidens"]             = int(bowler.get("maidens") or 0)
            p["dot_balls"]           = int(bowler.get("0s")      or 0)
            p["wides"]               = int(bowler.get("wide") or bowler.get("extra_type_run_wide") or 0)
            p["no_balls"]            = int(bowler.get("noball") or bowler.get("extra_type_run_noball") or 0)
            p["boundaries_conceded"] = int(bowler.get("4s") or 0) + int(bowler.get("6s") or 0)

    # Fix SR where missing
    for p in per_player.values():
        if p["batting_sr"] == 0 and p["balls_faced"] > 0:
            p["batting_sr"] = round(p["runs"] * 100 / p["balls_faced"], 2)

    # Mark batters who didn't bowl
    bowler_pids = {pid for pid, p in per_player.items() if p["overs_bowled"] > 0}
    for pid, p in per_player.items():
        if pid not in bowler_pids and (p["batting_position"] is not None or p["yet_to_bat"]):
            p["did_not_bowl"] = True

    return list(per_player.values()) if per_player else None
