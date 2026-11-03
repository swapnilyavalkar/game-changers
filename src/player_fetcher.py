from __future__ import annotations

"""
Player profile scraper.

Fetches ALL-TIME career stats for each player from their CricHeroes profile page.
Not tournament-specific — pulls the player's full career history.

CricHeroes is a Next.js app, so we first try extracting the embedded
__NEXT_DATA__ JSON (fast, complete). Falls back to Selenium HTML parsing.

Player profile URL pattern:
  https://cricheroes.in/player-profile/{player_id}/{name-slug}

Data returned per player:
  - Career batting: matches, innings, runs, avg, SR, HS, 50s, 100s, 4s, 6s
  - Career bowling: matches, wickets, economy, avg, SR, best, dot_balls
  - Career fielding: catches, run_outs, stumpings
  - Recent matches: last 20 match entries with per-match stats
  - Photo URL, role, batting/bowling style
"""

import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import CH_BASE_URL, SCORECARD_WAIT, REQUEST_DELAY_SEC, DATA_DIR, TEAM_ID as GAME_CHANGERS_TEAM_ID

log = logging.getLogger(__name__)

PLAYER_CACHE_DIR = DATA_DIR / "cache" / "players"
PLAYER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# REST API match-history cache — keyed by (player_id, months, today).
# Files older than 2 days are pruned on each run to avoid unbounded growth.
MATCH_HISTORY_CACHE_DIR = DATA_DIR / "cache" / "match_history"
MATCH_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _prune_match_history_cache(keep_days: int = 2) -> None:
    """Delete match-history cache files older than `keep_days` days."""
    cutoff = date.today() - timedelta(days=keep_days)
    for f in MATCH_HISTORY_CACHE_DIR.glob("*.json"):
        # filename: {player_id}_{months}m_{YYYY-MM-DD}.json
        parts = f.stem.rsplit("_", 1)
        if len(parts) == 2:
            try:
                file_date = date.fromisoformat(parts[1])
                if file_date < cutoff:
                    f.unlink(missing_ok=True)
            except ValueError:
                pass


# ── Driver ────────────────────────────────────────────────────────────────────

def _make_driver(max_retries: int = 3) -> webdriver.Chrome:
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
            return webdriver.Chrome(service=Service(chromedriver_path), options=options)
        except Exception as e:
            last_error = e

    import os
    os.environ["WDM_SSL_VERIFY"] = "0"
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

    # 1. Selenium Manager (built-in since Selenium 4.6) — no external download needed
    try:
        return webdriver.Chrome(options=options)
    except Exception as e:
        log.debug(f"[driver] Selenium Manager failed ({e}), trying fallbacks")

    # 2. chromedriver already on system PATH (e.g. installed via Homebrew)
    import shutil
    chromedriver_path = shutil.which("chromedriver")
    if chromedriver_path:
        log.debug(f"[driver] Using system chromedriver: {chromedriver_path}")
        return webdriver.Chrome(service=Service(chromedriver_path), options=options)

    # 3. webdriver_manager with SSL verification disabled (corporate proxy fallback)
    import os
    os.environ["WDM_SSL_VERIFY"] = "0"
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


# ── URL helpers ───────────────────────────────────────────────────────────────

def _player_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def player_profile_url(player_id: str, name: str) -> str:
    return f"{CH_BASE_URL}/player-profile/{player_id}/{_player_slug(name)}"


# ── Fetch & cache HTML ────────────────────────────────────────────────────────

def _get_player_html(player_id: str, name: str, force_refresh: bool = False) -> str:
    cache_file = PLAYER_CACHE_DIR / f"player_{player_id}.html"

    if cache_file.exists() and not force_refresh:
        log.info(f"[player_fetcher] cache hit: {name} ({player_id})")
        return cache_file.read_text(encoding="utf-8")

    url = player_profile_url(player_id, name)
    log.info(f"[player_fetcher] fetching: {url}")

    driver = _make_driver()
    try:
        driver.get(url)
        time.sleep(SCORECARD_WAIT)
        html = driver.page_source
        cache_file.write_text(html, encoding="utf-8")
    finally:
        driver.quit()

    time.sleep(REQUEST_DELAY_SEC)
    return html


# ── __NEXT_DATA__ extraction ──────────────────────────────────────────────────

def _extract_next_data(html: str) -> dict | None:
    """
    CricHeroes is Next.js — the full page data is embedded in:
      <script id="__NEXT_DATA__" type="application/json">{...}</script>
    This is the fastest and most reliable extraction method.
    """
    soup = BeautifulSoup(html, "lxml")
    tag  = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag:
        return None
    try:
        return json.loads(tag.string)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_player_statement(statement: str) -> dict:
    """
    CricHeroes embeds a prose summary with bold stats in player_statement.
    E.g. "128 turns at the crease ... top score of <b>55*</b> ... average of <b>11.85</b>"
    Extract key numbers via regex.
    """
    import re as _re
    from bs4 import BeautifulSoup as _BS
    text = _BS(statement or "", "lxml").get_text(" ")
    def _find(pattern):
        m = _re.search(pattern, text, _re.IGNORECASE)
        return m.group(1).strip() if m else None

    return {
        "innings":      _try_int(_find(r"(\d[\d,]*)\s+turns?\s+at\s+the\s+crease")),
        "highest":      _try_int((_find(r"top\s+score\s+of\s+([\d*]+)") or "").replace("*", "")),
        "average":      _try_float(_find(r"average\s+of\s+([\d.]+)")),
        "strike_rate":  _try_float(_find(r"strike\s+rate\s+of\s+([\d.]+)")),
        "sixes":        _try_int(_find(r"([\d,]+)\s+sixes")),
        "fours":        _try_int(_find(r"([\d,]+)\s+fours")),
        "overs":        _try_float(_find(r"bowled\s+([\d.]+)\s+overs")),
        "economy":      _try_float(_find(r"economy\s+rate\s+of\s+([\d.]+)")),
    }


def _parse_next_data(nd: dict, player_id: str) -> dict:
    """
    Walk the Next.js page props to extract player stats.
    CricHeroes actual structure (2025+):
      pageProps.playerInfo.data  — basic info + career totals
      pageProps.matches.data     — last N recent matches (team-level summaries)
    """
    props = nd.get("props", {}).get("pageProps", {})

    # ── Player info (CricHeroes 2025+ structure) ──────────────────────────────
    pi = props.get("playerInfo", {}).get("data", {})

    # Fallback to old key patterns for forward-compat
    if not pi:
        pi = (
            props.get("playerData")
            or props.get("playerDetails")
            or props.get("player")
            or props.get("data", {}).get("player")
            or {}
        )

    # ── Parse player_statement prose for detailed batting/bowling stats ────────
    stmt_stats = _parse_player_statement(pi.get("player_statement", ""))

    total_matches  = _try_int(pi.get("total_matches"))
    total_runs     = _try_int(pi.get("total_runs"))
    total_wickets  = _try_int(pi.get("total_wickets"))

    career_batting = {
        "matches":     total_matches,
        "innings":     stmt_stats.get("innings") or 0,
        "runs":        total_runs,
        "highest":     stmt_stats.get("highest") or 0,
        "average":     stmt_stats.get("average") or 0.0,
        "strike_rate": stmt_stats.get("strike_rate") or 0.0,
        "sixes":       stmt_stats.get("sixes") or 0,
        "fours":       stmt_stats.get("fours") or 0,
    }

    career_bowling = {
        "matches":  total_matches,
        "wickets":  total_wickets,
        "overs":    stmt_stats.get("overs") or 0.0,
        "economy":  stmt_stats.get("economy") or 0.0,
    }

    # ── Recent matches from player profile page ───────────────────────────────
    raw_matches = props.get("matches", {}).get("data", [])
    recent_matches = []
    for m in raw_matches[:20]:
        team_a_id = str(m.get("team_a_id", ""))
        is_gc_batting_first = (team_a_id == str(pi.get("player_id", ""))) or True  # always include
        recent_matches.append({
            "date":     (m.get("match_start_time") or "")[:10],
            "opponent": m.get("team_b") if str(m.get("team_a_id")) == str(pi.get("player_id","")) else m.get("team_a", ""),
            "result":   m.get("match_summary", {}).get("summary", ""),
            "overs":    _try_int(m.get("overs")),
        })

    _SKILL_MAP = {"BAT": "batsman", "BALL": "bowler", "ALL": "all_rounder", "WK": "wicket_keeper"}
    skill = _SKILL_MAP.get((pi.get("player_skill") or "").upper(), "")
    raw_role = skill or _safe_str(pi.get("playing_role"))
    # Normalize raw CricHeroes strings (e.g. "All-Rounder", "Top-order batter") to standard values
    _VALID_ROLES = {"batsman", "bowler", "all_rounder", "wicket_keeper", "unknown", ""}
    if raw_role not in _VALID_ROLES:
        s = raw_role.lower()
        if "wicket" in s or "keeper" in s or "wk" in s:
            raw_role = "wicket_keeper"
        elif "all" in s and ("round" in s or "rounder" in s):
            raw_role = "all_rounder"
        elif "bowl" in s:
            raw_role = "bowler"
        elif "bat" in s or "batter" in s:
            raw_role = "batsman"
        else:
            raw_role = "unknown"
    role = raw_role

    return {
        "cricheroes_id":  player_id,
        "name":           _safe_str(pi.get("name")),
        "photo_url":      _safe_str(pi.get("profile_photo")),
        "role":           role,
        "batting_style":  _safe_str(pi.get("batting_hand")),
        "bowling_style":  _safe_str(pi.get("bowling_style")),
        "career_batting":  career_batting,
        "career_bowling":  career_bowling,
        "career_fielding": {},
        "recent_matches":  recent_matches,
        "_source": "next_data",
    }


# ── HTML fallback parsing ─────────────────────────────────────────────────────

def _parse_player_html(html: str, player_id: str, player_name: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Photo
    photo_url = None
    img = soup.select_one("[class*='profile-pic'], [class*='player-image'], [class*='avatar'] img")
    if img:
        photo_url = img.get("src") or img.get("data-src")

    # Role / styles from page text
    role_text = ""
    for el in soup.select("[class*='playing-role'], [class*='player-role'], [class*='role']"):
        role_text = el.get_text(strip=True)
        if role_text:
            break

    # Career stats — look for stat boxes / summary panels
    career_batting  = _scrape_stat_block(soup, keywords=["runs", "innings", "batting avg", "strike rate", "highest"])
    career_bowling  = _scrape_stat_block(soup, keywords=["wickets", "economy", "bowling avg", "best"])
    career_fielding = _scrape_stat_block(soup, keywords=["catches", "run out", "stumping"])

    # Recent matches table
    recent_matches = _scrape_recent_matches_table(soup)

    return {
        "cricheroes_id":  player_id,
        "name":           player_name,
        "photo_url":      photo_url,
        "role":           role_text or "unknown",
        "batting_style":  None,
        "bowling_style":  None,
        "career_batting": career_batting,
        "career_bowling": career_bowling,
        "career_fielding": career_fielding,
        "recent_matches": recent_matches,
        "_source": "html_fallback",
    }


def _scrape_stat_block(soup: BeautifulSoup, keywords: list[str]) -> dict:
    stats = {}
    for el in soup.find_all(True):
        text = el.get_text(strip=True).lower()
        for kw in keywords:
            if kw in text and not el.find_all(True):  # leaf node
                parent = el.parent
                if parent:
                    val_el = parent.find_next_sibling() or el.find_next_sibling()
                    if val_el:
                        raw = val_el.get_text(strip=True)
                        stats[kw.replace(" ", "_")] = _try_num(raw)
    return stats


def _scrape_recent_matches_table(soup: BeautifulSoup) -> list[dict]:
    matches = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any(k in headers for k in ["runs", "r", "wickets", "wkts"]):
            continue
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 3:
                continue
            matches.append({
                "date":       cells[0] if cells else "",
                "opponent":   cells[1] if len(cells) > 1 else "",
                "runs":       _try_num(cells[2]) if len(cells) > 2 else 0,
                "balls":      _try_num(cells[3]) if len(cells) > 3 else 0,
                "wickets":    _try_num(cells[4]) if len(cells) > 4 else 0,
                "economy":    _try_num(cells[5]) if len(cells) > 5 else 0,
            })
            if len(matches) >= 20:
                break
        if matches:
            break
    return matches


# ── Stat normalisers ─────────────────────────────────────────────────────────

def _normalise_batting_stats(raw: dict) -> dict:
    if not raw:
        return {}
    return {
        "matches":      _try_int(raw.get("matches") or raw.get("total_match") or raw.get("m")),
        "innings":      _try_int(raw.get("innings") or raw.get("inning") or raw.get("inn")),
        "runs":         _try_int(raw.get("runs") or raw.get("total_runs") or raw.get("r")),
        "highest":      _try_int(raw.get("highest") or raw.get("highest_run") or raw.get("hs")),
        "average":      _try_float(raw.get("average") or raw.get("avg")),
        "strike_rate":  _try_float(raw.get("strike_rate") or raw.get("sr") or raw.get("strikeRate")),
        "not_outs":     _try_int(raw.get("not_outs") or raw.get("not_out") or raw.get("no")),
        "fifties":      _try_int(raw.get("fifties") or raw.get("50s") or raw.get("half_century")),
        "hundreds":     _try_int(raw.get("hundreds") or raw.get("100s") or raw.get("century")),
        "fours":        _try_int(raw.get("fours") or raw.get("4s")),
        "sixes":        _try_int(raw.get("sixes") or raw.get("6s")),
        "balls_faced":  _try_int(raw.get("balls_faced") or raw.get("ball_faced") or raw.get("bf")),
    }


def _normalise_bowling_stats(raw: dict) -> dict:
    if not raw:
        return {}
    return {
        "matches":      _try_int(raw.get("matches") or raw.get("total_match") or raw.get("m")),
        "innings":      _try_int(raw.get("innings") or raw.get("inn")),
        "wickets":      _try_int(raw.get("wickets") or raw.get("total_wickets") or raw.get("w")),
        "best_innings": _safe_str(raw.get("best_innings") or raw.get("best") or raw.get("bbi")),
        "average":      _try_float(raw.get("average") or raw.get("avg")),
        "economy":      _try_float(raw.get("economy") or raw.get("econ") or raw.get("er")),
        "strike_rate":  _try_float(raw.get("strike_rate") or raw.get("sr")),
        "overs":        _try_float(raw.get("overs") or raw.get("total_overs")),
        "runs":         _try_int(raw.get("runs") or raw.get("runs_conceded")),
        "maidens":      _try_int(raw.get("maidens") or raw.get("maiden")),
        "dot_balls":    _try_int(raw.get("dot_balls") or raw.get("dots")),
    }


def _normalise_fielding_stats(raw: dict) -> dict:
    if not raw:
        return {}
    return {
        "catches":    _try_int(raw.get("catches") or raw.get("catch")),
        "run_outs":   _try_int(raw.get("run_outs") or raw.get("run_out")),
        "stumpings":  _try_int(raw.get("stumpings") or raw.get("stumping")),
        "dismissals": _try_int(raw.get("dismissals") or raw.get("total_dismissal")),
    }


def _normalise_recent_matches(raw: list) -> list[dict]:
    result = []
    for m in (raw or [])[:20]:
        result.append({
            "date":       _safe_str(m.get("date") or m.get("match_date")),
            "opponent":   _safe_str(m.get("opponent") or m.get("opponent_team")),
            "overs":      _try_int(m.get("overs") or m.get("match_overs") or 20),
            "result":     _safe_str(m.get("result")),
            "runs":       _try_int(m.get("runs") or m.get("batting_runs")),
            "balls":      _try_int(m.get("balls") or m.get("balls_faced")),
            "fours":      _try_int(m.get("fours") or m.get("4s")),
            "sixes":      _try_int(m.get("sixes") or m.get("6s")),
            "batting_sr": _try_float(m.get("batting_sr") or m.get("strike_rate")),
            "wickets":    _try_int(m.get("wickets")),
            "economy":    _try_float(m.get("economy") or m.get("econ")),
            "overs_bowled": _try_float(m.get("overs_bowled")),
            "catches":    _try_int(m.get("catches")),
        })
    return result


# ── Main public API ───────────────────────────────────────────────────────────

def fetch_player_profile(
    player_id: str,
    player_name: str,
    force_refresh: bool = False,
) -> dict:
    """
    Fetch all-time career stats for one player.
    Returns a dict with career_batting, career_bowling, career_fielding,
    recent_matches, photo_url, role, styles.
    """
    html = _get_player_html(player_id, player_name, force_refresh)

    # Try Next.js data first
    nd = _extract_next_data(html)
    if nd:
        try:
            result = _parse_next_data(nd, player_id)
            # Fill name from argument if not found in data
            if not result.get("name"):
                result["name"] = player_name
            log.info(f"[player_fetcher] {player_name}: parsed via __NEXT_DATA__")
            return result
        except Exception as e:
            log.warning(f"[player_fetcher] __NEXT_DATA__ parse failed for {player_name}: {e}")

    # Fallback: HTML parsing
    log.info(f"[player_fetcher] {player_name}: falling back to HTML parse")
    return _parse_player_html(html, player_id, player_name)


def _build_match_entry(m: dict) -> tuple[str, dict]:
    """Build a normalised match dict from a CricHeroes match object. Returns (match_id, entry)."""
    mid   = str(m.get("match_id") or "")
    mdate = (m.get("match_start_time") or "")[:10]
    _t    = re.sub(r"[^a-z0-9]+", "-", (m.get("tournament_name") or "individual").lower()).strip("-")
    _ta   = re.sub(r"[^a-z0-9]+", "-", (m.get("team_a") or "").lower()).strip("-")
    _tb   = re.sub(r"[^a-z0-9]+", "-", (m.get("team_b") or "").lower()).strip("-")
    entry = {
        "match_id":        mid,
        "match_date":      mdate,
        "overs":           int(m.get("overs") or 0),
        "team_a":          m.get("team_a", ""),
        "team_b":          m.get("team_b", ""),
        "team_a_id":       str(m.get("team_a_id", "")),
        "team_b_id":       str(m.get("team_b_id", "")),
        "tournament":      m.get("tournament_name", ""),
        "winning_team_id": str(m.get("winning_team_id") or ""),
        "scorecard_url":   f"{CH_BASE_URL}/scorecard/{mid}/{_t}/{_ta}-vs-{_tb}/scorecard",
    }
    return mid, entry


def fetch_player_match_list(
    player_id: str,
    player_name: str,
    months: int = 18,
    auth_session=None,   # kept for backwards compat, no longer used
) -> list[dict]:
    """
    Fetch the list of matches a player has played (last `months` months, all teams).

    Calls api.cricheroes.in/api/v1/player/get-player-match/{id} directly — no
    login or Selenium required.  Paginates until all matches within the cutoff
    window are collected (up to 50 pages).

    Results are cached to data/cache/match_history/ for the current calendar day
    so repeated runs within the same day skip all REST API calls.

    Returns:
      [{ match_id, match_date, overs, team_a, team_b, team_a_id, team_b_id,
         tournament, winning_team_id, scorecard_url }]
    """
    # ── Cache check ───────────────────────────────────────────────────────────
    _prune_match_history_cache()
    cache_file = MATCH_HISTORY_CACHE_DIR / f"{player_id}_{months}m_{date.today().isoformat()}.json"
    if cache_file.exists():
        log.info(f"[player_fetcher] match history cache hit: {player_name} ({player_id})")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    # ── Fresh REST API fetch ──────────────────────────────────────────────────
    from cricheroes_auth import get_api_session, fetch_match_page

    cutoff  = (date.today() - timedelta(days=months * 30)).isoformat()
    session = get_api_session()
    all_matches: dict[str, dict] = {}

    for page_num in range(1, 51):
        if page_num > 1:
            time.sleep(REQUEST_DELAY_SEC)

        page_data = fetch_match_page(session, player_id, page_num)
        if not page_data:
            log.debug(f"[player_fetcher] {player_name}: no data on page {page_num} — stopping")
            break

        page_matches = page_data.get("data", [])
        if not page_matches:
            break

        all_past_cutoff = True
        for m in page_matches:
            mid, entry = _build_match_entry(m)
            mdate = entry["match_date"]
            if mdate and mdate < cutoff:
                continue
            all_past_cutoff = False
            if mid:
                all_matches[mid] = entry

        log.debug(
            f"[player_fetcher] {player_name} page {page_num}: "
            f"{len(page_matches)} returned, total so far={len(all_matches)}"
        )

        if all_past_cutoff:
            log.debug(f"[player_fetcher] {player_name}: reached cutoff at page {page_num}")
            break

        if not page_data.get("page", {}).get("next"):
            break

    result = list(all_matches.values())
    # ── Write cache ───────────────────────────────────────────────────────────
    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=None), encoding="utf-8")
    except Exception as e:
        log.warning(f"[player_fetcher] could not write match history cache: {e}")
    log.info(f"[player_fetcher] {player_name}: {len(result)} matches (last {months} months)")
    return result


def fetch_player_stats_for_match(
    player_id: str,
    player_name: str,
    match: dict,
) -> dict | None:
    """
    Fetch a scorecard and extract batting/bowling/fielding stats for a specific player.

    Uses fetch_player_scorecard_stats() which searches ALL innings regardless of team,
    so it works correctly for cross-team matches where the player is not on Game Changers.

    Returns a dict with 'match_meta' and 'player_stats', or None if player not found.
    """
    from fetcher import fetch_player_scorecard_stats

    stats, player_team_id = fetch_player_scorecard_stats(
        match["scorecard_url"], match["match_id"], player_id
    )
    if stats is None:
        return None

    # ── Determine result from the player's team perspective ───────────────────
    winning_id = match.get("winning_team_id", "")
    team_a_id  = match.get("team_a_id", "")
    team_b_id  = match.get("team_b_id", "")

    if not winning_id:
        result = "no_result"
    elif player_team_id and winning_id == player_team_id:
        result = "win"
    elif player_team_id and winning_id != player_team_id:
        result = "loss"
    else:
        result = "no_result"

    # ── Determine opponent from the player's team perspective ─────────────────
    if player_team_id and player_team_id == team_a_id:
        opponent = match.get("team_b", "Unknown")
    elif player_team_id and player_team_id == team_b_id:
        opponent = match.get("team_a", "Unknown")
    else:
        # Fallback: use team_b as opponent (most likely correct for team_a player)
        opponent = match.get("team_b") or match.get("team_a", "Unknown")

    # Mark as Game Changers match if either team in the scorecard is Game Changers
    team_a_id_str = str(match.get("team_a_id", ""))
    team_b_id_str = str(match.get("team_b_id", ""))
    nc_id_str     = str(GAME_CHANGERS_TEAM_ID)
    is_game_changers  = int(nc_id_str in (team_a_id_str, team_b_id_str, str(player_team_id or "")))

    return {
        "match_meta": {
            "cricheroes_match_id": match["match_id"],
            "match_date":  match.get("match_date"),
            "opponent":    opponent,
            "overs":       match.get("overs", 0),
            "result":      result,
            "tournament":  match.get("tournament", ""),
            "venue":       None,
            "is_game_changers_match": is_game_changers,
        },
        "player_stats": stats,
    }


def fetch_all_player_profiles(
    players: list[dict],
    force_refresh: bool = False,
) -> dict[str, dict]:
    """
    Fetch profiles for all players in the list.
    Returns { cricheroes_id: profile_dict }
    """
    profiles = {}
    total = len(players)
    for i, p in enumerate(players):
        pid  = str(p.get("cricheroes_id", ""))
        name = p.get("name", f"Player_{pid}")
        if not pid:
            log.warning(f"[player_fetcher] skipping player with no ID: {name}")
            continue
        log.info(f"[player_fetcher] {i+1}/{total}: {name}")
        try:
            profiles[pid] = fetch_player_profile(pid, name, force_refresh)
        except Exception as e:
            log.error(f"[player_fetcher] failed for {name}: {e}")
            profiles[pid] = {"cricheroes_id": pid, "name": name, "_error": str(e)}
    return profiles


# ── Helpers ───────────────────────────────────────────────────────────────────

def _try_num(v) -> int | float:
    s = str(v).replace(",", "").replace("*", "").strip()
    try:
        if "." in s:
            return round(float(s), 2)
        return int(s or 0)
    except (ValueError, TypeError):
        return 0


def _try_int(v) -> int:
    try:
        return int(str(v).replace(",", "").replace("*", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _try_float(v) -> float:
    try:
        return round(float(str(v).replace(",", "").replace("*", "").strip() or 0), 2)
    except (ValueError, TypeError):
        return 0.0


def _safe_str(v) -> str:
    return str(v).strip() if v else ""
