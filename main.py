#!/usr/bin/env python3
from __future__ import annotations
"""
Game Changers — Cricket Analytics CLI
Usage:
  python main.py fetch          # Pull latest data from CricHeroes
  python main.py snapshot       # Save weekly snapshot to DB
  python main.py report         # Generate all HTML/PDF reports
  python main.py select 20      # Print squad for a specific format
  python main.py run-all        # fetch + snapshot + report
  python main.py init-db        # Initialise the SQLite database
"""

import json
import logging
import sys
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

# Ensure src/ is on the path
sys.path.insert(0, "src")

from config import FORMATS, DB_PATH, BLOCKED_PLAYER_IDS
import db
import fetcher as fetch_module
import analyzer
import selector as sel_module
import dashboard_generator

console = Console()

_LOG_DIR  = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / f"fetch-player-history-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)

PROGRESS_FILE = Path("data") / "fetch_progress.json"
log = logging.getLogger(__name__)


# ── CLI root ──────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Game Changers analytics automation."""
    pass


# ── init-db ───────────────────────────────────────────────────────────────────

@cli.command("init-db")
@click.option("--force-db-delete", is_flag=True, default=False,
              help="DROP all tables and recreate from scratch. DESTRUCTIVE — all data will be lost.")
def cmd_init_db(force_db_delete):
    """Create (or migrate) the SQLite database."""
    if force_db_delete:
        console.print("[bold red]WARNING:[/] --force-db-delete will DELETE all data in the database.")
        console.print(f"[red]Database path:[/] {DB_PATH}")
        confirm = click.confirm("Are you absolutely sure you want to delete all data?", default=False)
        if not confirm:
            console.print("[yellow]Aborted.[/]")
            return
        db.init_db(drop_all=True)
        console.print("[bold green]Database recreated.[/]")
    else:
        db.init_db()
    console.print(f"[green]Database ready:[/] {DB_PATH}")


# ── fetch ─────────────────────────────────────────────────────────────────────

@cli.command("fetch")
@click.option("--max-matches", default=200, help="Max matches to fetch scorecards for.")
@click.option("--months", default=18, type=int, help="How many months of history to pull (default: 18).")
@click.option("--days", default=None, type=int,
              help="Fetch only the last N days of matches (overrides --months). "
                   "Use --days 1 for today's matches only.")
@click.option("--team-id", default=None, help="CricHeroes team ID (overrides config / env var).")
@click.option("--team-slug", default=None, help="CricHeroes team URL slug (overrides config / env var).")
@click.option("--skip-commentary", is_flag=True, default=False,
              help="Skip ball-by-ball commentary fetch (much faster; over assignments will be empty).")
def cmd_fetch(max_matches, months, days, team_id, team_slug, skip_commentary):
    """Pull latest data from CricHeroes and store in SQLite."""
    from config import TEAMS
    db.init_db()

    teams_to_fetch = [
        {"team_id": team_id, "team_slug": team_slug}
    ] if (team_id and team_slug) else TEAMS

    # --days overrides --months: compute the minimum months needed and set a date cutoff
    if days is not None:
        effective_months = max(1, math.ceil(days / 30))
        cutoff_date      = (date.today() - timedelta(days=days)).isoformat()
        console.print(f"[bold gold1]Fetching data from CricHeroes for {len(teams_to_fetch)} team(s) (last {days} day(s))...[/]")
    else:
        effective_months = months
        cutoff_date      = None
        console.print(f"[bold gold1]Fetching data from CricHeroes for {len(teams_to_fetch)} team(s) (last {months} months)...[/]")

    for t_entry in teams_to_fetch:
        t_id = t_entry["team_id"]
        t_slug = t_entry.get("team_slug")
        t_name = t_entry.get("team_name", t_slug)
        console.print(f"\n[bold cyan]=== Fetching Team: {t_name} ({t_id}) ===[/]")

        raw = fetch_module.fetch_all_data(
            max_matches=max_matches,
            months=effective_months,
            skip_commentary=skip_commentary,
            team_id=t_id,
            team_slug=t_slug,
            days_hint=days,
        )
        players         = raw["players"]
        all_matches     = raw["matches"]
        matches         = (
            [m for m in all_matches if m.get("match_date", "") >= cutoff_date]
            if cutoff_date else all_matches
        )
        scorecards      = raw["scorecards"]
        leaderboard     = raw.get("leaderboard", {})
        player_profiles = raw.get("player_profiles", {})

        # ── Supplement player data from career profiles ───────────────────────────
        for p in players:
            ch_id = str(p.get("cricheroes_id", ""))
            prof  = player_profiles.get(ch_id, {})
            if prof:
                if not p.get("batting_style") and prof.get("batting_style"):
                    p["batting_style"] = prof["batting_style"]
                if not p.get("bowling_style") and prof.get("bowling_style"):
                    p["bowling_style"] = prof["bowling_style"]
                if not p.get("photo_url") and prof.get("photo_url"):
                    p["photo_url"] = prof["photo_url"]
                if p.get("role", "unknown") == "unknown" and prof.get("role") and prof["role"] != "unknown":
                    p["role"] = prof["role"]

        # ── Supplement player roles from leaderboard if unknown ──────────────────
        lb_bowlers  = {lb["player_name"].lower() for lb in leaderboard.get("bowling",  [])}
        lb_batsmen  = {lb["player_name"].lower() for lb in leaderboard.get("batting",  [])}
        lb_fielders = {lb["player_name"].lower() for lb in leaderboard.get("fielding", [])}
        for p in players:
            if p.get("role", "unknown") == "unknown":
                n = p["name"].lower()
                if n in lb_bowlers and n in lb_batsmen:
                    p["role"] = "all_rounder"
                elif n in lb_bowlers:
                    p["role"] = "bowler"
                elif n in lb_batsmen:
                    p["role"] = "batsman"

        # ── Persist players ───────────────────────────────────────────────────────
        player_id_map   = {}   # cricheroes_id (str) → db id (int)
        player_name_map = {}   # normalised name (str) → db id (int)  — fallback for HTML-parsed balls
        for p in players:
            if str(p.get("cricheroes_id", "")) in BLOCKED_PLAYER_IDS:
                continue
            p["is_game_changers"] = 1   # players from team fetch are registered team members
            pid = db.upsert_player(p)
            player_id_map[p["cricheroes_id"]] = pid
            player_name_map[p["name"].strip().lower()] = pid
        console.print(f"  [green]Players:[/] {len(players)} upserted for {t_name}")

        # ── Persist matches + stats ───────────────────────────────────────────────
        for m in matches:
            mid    = m["cricheroes_match_id"]
            sc     = scorecards.get(mid, {})
            m["overs"]              = sc.get("overs") or m.get("overs") or 20
            m["scorecard_raw"]      = None  # raw HTML cached to data/cache/ — not stored in DB
            m["is_game_changers_match"] = 1     # all matches fetched here are team matches
            db_match_id = db.upsert_match(m)

            per_player: dict[str, dict] = {}
            for stat in sc.get("player_stats", []):
                ch_pid = stat.get("cricheroes_player_id", "")
                if not ch_pid:
                    continue
                if ch_pid in BLOCKED_PLAYER_IDS:
                    continue
                db_pid = player_id_map.get(ch_pid)
                if not db_pid:
                    db_pid = db.upsert_player({
                        "cricheroes_id": ch_pid,
                        "name": stat.get("player_name", f"Player_{ch_pid}"),
                        "role": "unknown",
                    })
                    player_id_map[ch_pid] = db_pid

                if ch_pid not in per_player:
                    per_player[ch_pid] = {"player_id": db_pid, "match_id": db_match_id}

                for k in ["runs","balls_faced","fours","sixes","batting_sr",
                          "is_out","batting_position","yet_to_bat",
                          "overs_bowled","wickets","runs_conceded","economy","maidens","dot_balls",
                          "did_not_bowl","wides","no_balls","boundaries_conceded",
                          "catches","run_outs","stumpings","over_assignments"]:
                    if stat.get(k) is not None:
                        per_player[ch_pid][k] = stat[k]

            for row in per_player.values():
                db.upsert_player_match_stats(row)

            for d in sc.get("dismissals", []):
                d["match_id"] = db_match_id
                if d.get("_our_batting") and d.get("batter_ch_id"):
                    d["batter_player_id"] = player_id_map.get(d["batter_ch_id"])
                if not d.get("_our_batting") and d.get("bowler_ch_id"):
                    d["bowler_player_id"] = player_id_map.get(d["bowler_ch_id"])
                db.upsert_dismissal(d)

            for obs in sc.get("opponent_bowling_stats", []):
                obs["match_id"] = db_match_id
                db.upsert_opponent_bowling_stat(obs)

            bvb_agg: dict[tuple, dict] = {}
            for ball in sc.get("commentary_balls", []):
                batter_name  = ball.get("batter_name", "").strip()
                bowler_name  = ball.get("bowler_name", "").strip()
                batter_ch_id = ball.get("batter_id", "")
                bowler_ch_id = ball.get("bowler_id", "")
                if not batter_name or not bowler_name:
                    continue
                batter_pid = player_id_map.get(batter_ch_id)
                if not batter_pid:
                    batter_pid = player_name_map.get(batter_name.lower())
                if not batter_pid:
                    continue
                key = (batter_name, bowler_name)
                if key not in bvb_agg:
                    bvb_agg[key] = {
                        "batter_player_id": batter_pid,
                        "batter_name":      batter_name,
                        "batter_ch_id":     batter_ch_id or None,
                        "bowler_name":      bowler_name,
                        "bowler_ch_id":     bowler_ch_id or None,
                        "runs_scored": 0, "balls_faced": 0,
                        "fours": 0, "sixes": 0, "dismissals": 0, "dot_balls": 0,
                    }
                r   = int(ball.get("runs", 0))
                ext = (ball.get("extras_type") or "").lower()
                wkt = bool(ball.get("is_wicket"))
                is_extra_ball = ext in ("wide", "no_ball", "noball", "nb", "wd")
                bvb_agg[key]["runs_scored"] += r
                if not is_extra_ball:
                    bvb_agg[key]["balls_faced"] += 1
                    if r == 4 and not ext:
                        bvb_agg[key]["fours"] += 1
                    elif r == 6 and not ext:
                        bvb_agg[key]["sixes"] += 1
                    if r == 0 and not ext:
                        bvb_agg[key]["dot_balls"] += 1
                if wkt:
                    bvb_agg[key]["dismissals"] += 1

            for row in bvb_agg.values():
                row["match_id"] = db_match_id
                db.upsert_batter_vs_bowler(row)

        console.print(f"  [green]Matches:[/] {len(matches)} upserted for {t_name}")

    console.print("\n[bold green]Fetch complete across all teams.[/]")


# ── login ─────────────────────────────────────────────────────────────────────

@cli.command("login")
@click.option("--phone", default=None, envvar="CRICHEROES_PHONE",
              help="CricHeroes phone / email (or set CRICHEROES_PHONE env var).")
@click.option("--password", default=None, envvar="CRICHEROES_PASSWORD",
              help="CricHeroes password (or set CRICHEROES_PASSWORD env var).")
@click.option("--force", is_flag=True, default=False,
              help="Force re-login even if a valid session already exists.")
def cmd_login(phone, password, force):
    """
    Log in to CricHeroes and save the session cookie.

    Opens a visible browser window at cricheroes.in/login.
    If --phone and --password are provided (or set as env vars), the form is
    auto-filled; otherwise you can log in manually.

    The session is saved to data/session.json and reused by fetch-player-history
    to unlock full match-history pagination (all 18 months, not just 12).
    """
    sys.path.insert(0, "src")
    from cricheroes_auth import get_session, login_interactive

    if not force:
        existing = get_session()
        if existing:
            console.print("[green]Valid session already exists.[/] Use --force to re-login.")
            return

    console.print("[bold gold1]Opening CricHeroes login...[/]")
    console.print("  A browser window will open. Log in, then wait for it to close automatically.")
    try:
        session = login_interactive(
            email_or_phone=phone,
            password=password,
            headless=False,
        )
        console.print("[bold green]Login successful.[/] Session saved to data/session.json.")
        console.print("Run [bold]python main.py fetch-player-history[/] to fetch full match history.")
    except Exception as e:
        console.print(f"[red]Login failed:[/] {e}")


# ── fetch-player-history ──────────────────────────────────────────────────────

@cli.command("fetch-player-history")
@click.option("--months", default=18, type=int,
              help="How many months of match history to fetch (default: 18).")
@click.option("--players", "player_names", multiple=True,
              help="Only store stats for these players (full name, case-insensitive). "
                   "Repeat or comma-separate. Default: all players in each match.")
@click.option("--workers", default=2, type=click.IntRange(1, 3),
              help="Parallel scorecard workers 1-3 (default 2).")
@click.option("--delay-min", default=2.5, type=float,
              help="Min seconds between scorecard requests per worker (default 2.5).")
@click.option("--delay-max", default=5.0, type=float,
              help="Max seconds between scorecard requests per worker (default 5.0).")
@click.option("--resume", is_flag=True, default=False,
              help="Skip matches fully processed in the previous run "
                   "(reads data/fetch_progress.json).")
def cmd_fetch_player_history(months, player_names, workers, delay_min, delay_max, resume):
    """
    Fetch stats for ALL players in all team matches for the last N months.

    Uses the team from team.json as the source.  For every team match, fetches
    the scorecard and stores batting/bowling/fielding stats for every player in
    that match — both Game Changers players and opponents.

    Use --players to limit which players' stats are stored (e.g. to refresh one
    player without re-processing all matches).

    Examples:
      python main.py fetch-player-history --months 18
      python main.py fetch-player-history --players "Swapnil Yavalkar"
      python main.py fetch-player-history --resume
    """
    import threading
    import random
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    db.init_db()

    # Optional: only store stats for these players by name
    player_filter = None
    if player_names:
        player_filter = {n.strip().lower() for arg in player_names for n in arg.split(",")}
        console.print(f"[dim]Player filter:[/] {', '.join(sorted(player_filter))}")

    # --resume: skip match IDs completed in the last run
    completed_match_ids: set[str] = set()
    if resume and PROGRESS_FILE.exists():
        try:
            progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            completed_match_ids = set(str(i) for i in progress.get("completed_ids", []))
            if completed_match_ids:
                console.print(f"[yellow]--resume:[/] {len(completed_match_ids)} match(es) already done")
        except Exception as e:
            console.print(f"[yellow]Could not read progress file: {e}[/]")
    elif not resume and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    sys.path.insert(0, "src")

    # ── Step 1: get team match list via Selenium ──────────────────────────────
    console.print(f"[bold gold1]Fetching team match list (last {months} months)...[/]")
    try:
        team_matches = fetch_module.fetch_team_match_list(months=months)
    except Exception as e:
        console.print(f"[red]Failed to fetch team match list: {e}[/]")
        return

    if not team_matches:
        console.print("[yellow]No matches found for this team.[/]")
        return

    # Filter out already-completed matches
    if completed_match_ids:
        team_matches = [m for m in team_matches if m["cricheroes_match_id"] not in completed_match_ids]

    console.print(f"[bold gold1]{len(team_matches)} match(es) to process "
                  f"({workers} worker(s), {delay_min}-{delay_max}s jitter)...[/]")
    console.print(f"[dim]Log file: {_LOG_FILE}[/]")

    db_lock       = threading.Lock()
    console_lock  = threading.Lock()
    total_new_ref = [0]

    def process_match(match):
        mid  = match["cricheroes_match_id"]
        murl = match.get("match_url", "")
        if not murl:
            return mid, 0, ["No match URL"]

        time.sleep(random.uniform(delay_min, delay_max))

        all_stats = None
        last_exception = None
        for attempt in range(1, 3):
            try:
                all_stats = fetch_module.fetch_match_scorecard_all_players(murl, mid)
                break
            except Exception as e:
                last_exception = e
                time.sleep(2.0 * attempt)

        if all_stats is None and last_exception is not None:
            return mid, 0, [f"Scorecard fetch failed: {last_exception}"]

        if not all_stats:
            log.info(f"[fetch-player-history] match {mid}: no scorecard data — skipping")
            return mid, 0, []

        new_count = 0
        errors    = []
        try:
            with db_lock:
                db_mid = db.upsert_match({
                    "cricheroes_match_id": mid,
                    "match_date":          match.get("match_date"),
                    "opponent":            match.get("opponent"),
                    "overs":               match.get("overs", 0),
                    "result":              match.get("result"),
                    "tournament":          match.get("tournament", ""),
                    "venue":               match.get("venue"),
                    # cross-team history: other teams' matches a squad member played in
                    "is_game_changers_match":  int(match.get("is_game_changers_match", 0)),
                })

                for stat in all_stats:
                    ch_pid = stat.get("cricheroes_player_id", "")
                    pname  = stat.get("player_name", f"Player_{ch_pid}")

                    if ch_pid in BLOCKED_PLAYER_IDS:
                        continue

                    if player_filter and pname.lower() not in player_filter:
                        continue

                    db_pid = db.upsert_player({
                        "cricheroes_id": ch_pid,
                        "name":          pname,
                        "role":          "unknown",
                    })

                    db.upsert_player_match_stats({
                        "player_id":          db_pid,
                        "match_id":           db_mid,
                        "runs":               stat.get("runs", 0),
                        "balls_faced":        stat.get("balls_faced", 0),
                        "fours":              stat.get("fours", 0),
                        "sixes":              stat.get("sixes", 0),
                        "batting_sr":         stat.get("batting_sr", 0),
                        "is_out":             stat.get("is_out", False),
                        "batting_position":   stat.get("batting_position"),
                        "yet_to_bat":         stat.get("yet_to_bat", False),
                        "overs_bowled":       stat.get("overs_bowled", 0),
                        "wickets":            stat.get("wickets", 0),
                        "runs_conceded":      stat.get("runs_conceded", 0),
                        "economy":            stat.get("economy", 0),
                        "maidens":            stat.get("maidens", 0),
                        "did_not_bowl":       stat.get("did_not_bowl", False),
                        "wides":              stat.get("wides", 0),
                        "no_balls":           stat.get("no_balls", 0),
                        "boundaries_conceded": stat.get("boundaries_conceded", 0),
                        "catches":            stat.get("catches", 0),
                        "run_outs":           stat.get("run_outs", 0),
                        "stumpings":          stat.get("stumpings", 0),
                        "dot_balls":          stat.get("dot_balls", 0),
                    })
                    new_count += 1

                total_new_ref[0] += new_count

                # Update progress file (best-effort)
                try:
                    prog = json.loads(PROGRESS_FILE.read_text(encoding="utf-8")) if PROGRESS_FILE.exists() else {}
                    ids  = prog.get("completed_ids", [])
                    if mid not in ids:
                        ids.append(mid)
                    prog["completed_ids"] = ids
                    PROGRESS_FILE.write_text(json.dumps(prog, indent=2), encoding="utf-8")
                except Exception:
                    pass

        except Exception as e:
            errors.append(f"DB write error: {e}")

        return mid, new_count, errors

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_match, m): m for m in team_matches}
        match_lookup = {m["cricheroes_match_id"]: m for m in team_matches}
        for future in as_completed(futures):
            mid, count, errors = future.result()
            info     = match_lookup.get(mid, {})
            opponent = info.get("opponent", mid)
            date_str = info.get("match_date", "")
            with console_lock:
                console.print(f"  [green]✓[/] {date_str} vs {opponent}: {count} player stats stored")
                for err in errors:
                    console.print(f"    [red]⚠ {err}[/]")

    console.print(f"\n[bold green]Fetch complete.[/] {total_new_ref[0]} player stat rows stored.")
    console.print("Run `python main.py snapshot && python main.py dashboard` to refresh the dashboard.")


# ── snapshot ──────────────────────────────────────────────────────────────────

@cli.command("snapshot")
def cmd_snapshot():
    """Save a weekly stats snapshot for all players (for W-o-W tracking)."""
    db.init_db()
    df      = db.get_player_stats_df()
    players = db.get_all_players()

    if df.empty:
        console.print("[yellow]No data in DB yet — run `fetch` first.[/]")
        return

    today   = date.today().isoformat()
    count   = 0

    for p in players:
        pid  = p["id"]
        role = p.get("role", "unknown")
        for fmt in [0] + FORMATS:
            stats = analyzer.compute_player_format_stats(df, pid, fmt)
            score = analyzer.compute_composite_score(stats, role)
            db.save_snapshot({
                "player_id":       pid,
                "snapshot_date":   today,
                "format_overs":    fmt,
                "matches_played":  stats["matches_played"],
                "batting_avg":     stats["batting_avg"],
                "batting_sr":      stats["batting_sr"],
                "runs_total":      stats["runs_total"],
                "highest_score":   stats["highest_score"],
                "bowling_avg":     stats["bowling_avg"],
                "bowling_economy": stats["bowling_economy"],
                "wickets_total":   stats["wickets_total"],
                "form_score":      score,
            })
            count += 1

    console.print(f"[green]Snapshot saved:[/] {count} records for {today}")


# ── select ────────────────────────────────────────────────────────────────────

@cli.command("select")
@click.argument("fmt", type=click.Choice(["6","10","15","20"]))
@click.option("--size", default=11, type=click.Choice(["11","15"]), help="Squad size.")
def cmd_select(fmt, size):
    """Print the auto-selected squad for a format."""
    db.init_db()
    df      = db.get_player_stats_df()
    players = db.get_all_players()

    if df.empty:
        console.print("[yellow]No data — run `fetch` first.[/]")
        return

    all_analysis = analyzer.analyse_all(df, players)
    result = sel_module.select_squad(all_analysis, int(fmt), size=int(size))

    title = f"[bold gold1]{fmt}-over | Playing {size}[/]"
    console.print(title)

    t = Table(show_header=True, header_style="bold dim")
    t.add_column("#", width=3)
    t.add_column("Player", min_width=18)
    t.add_column("Role", width=12)
    t.add_column("Score", width=8)
    t.add_column("Bat Avg", width=8)
    t.add_column("Wkts", width=6)
    t.add_column("Econ", width=6)
    t.add_column("Reason")

    role_colors = {
        "wicket_keeper": "magenta",
        "batsman":       "yellow",
        "bowler":        "cyan",
        "all_rounder":   "green",
    }

    for i, p in enumerate(result["squad"], 1):
        color = role_colors.get(p["role"], "white")
        t.add_row(
            str(i),
            f"[{color}]{p['name']}[/]",
            p["role"].replace("_", " ").title(),
            f"[bold]{p['score']:.1f}[/]",
            f"{p['batting_avg']:.1f}",
            str(p["wickets_total"]),
            f"{p['bowling_economy']:.1f}" if p["bowling_economy"] else "—",
            f"[dim]{p['reason']}[/dim]",
        )

    console.print(t)

    if result["warnings"]:
        for w in result["warnings"]:
            console.print(f"[red]⚠ {w}[/]")

    if result["bench"]:
        bench = ", ".join(f"{p['name']} ({p['score']:.0f})" for p in result["bench"])
        console.print(f"[dim]Bench: {bench}[/]")


# ── dashboard ─────────────────────────────────────────────────────────────────

@cli.command("dashboard")
@click.option("--offline", is_flag=True, default=False,
              help="Embed JS libs inline for truly offline use (downloads ~500KB once).")
@click.option("--force-refresh", is_flag=True, default=False,
              help="Re-fetch player profiles from CricHeroes even if cached.")
@click.option("--skip-profiles", is_flag=True, default=False,
              help="Skip CricHeroes profile fetch (use CSV/DB data only — fast, no internet needed).")
def cmd_dashboard(offline, force_refresh, skip_profiles):
    """Generate interactive dashboard (Option A HTML + Option B JSON)."""
    console.print("[bold gold1]Generating dashboard...[/]")
    dashboard_generator.generate(
        force_player_refresh=force_refresh,
        offline=offline,
        skip_profiles=skip_profiles,
    )
    from config import REPORTS_DIR
    console.print(f"[bold green]Dashboard ready:[/] {REPORTS_DIR}/dashboard.html")
    console.print(f"[dim]Data JSON:[/] {REPORTS_DIR}/data/data.json")


# ── run-all ───────────────────────────────────────────────────────────────────

@cli.command("run-all")
@click.option("--max-matches", default=200)
@click.option("--months", default=18, type=int, help="Months of history to fetch.")
@click.option("--team-id", default=None, help="CricHeroes team ID.")
@click.option("--team-slug", default=None, help="CricHeroes team URL slug.")
@click.option("--skip-commentary", is_flag=True, default=False, help="Skip commentary fetch.")
@click.option("--include-player-history", is_flag=True, default=False,
              help="Also run fetch-player-history (cross-team stats for all players). Adds 45-90 min on first run.")
def cmd_run_all(max_matches, months, team_id, team_slug, skip_commentary, include_player_history):
    """Full pipeline: fetch → [player-history] → snapshot → dashboard."""
    from click.testing import CliRunner
    runner = CliRunner()
    fetch_args = ["--max-matches", str(max_matches), "--months", str(months)]
    if team_id:
        fetch_args += ["--team-id", team_id]
    if team_slug:
        fetch_args += ["--team-slug", team_slug]
    if skip_commentary:
        fetch_args.append("--skip-commentary")

    steps = [
        (cmd_init_db,   []),
        (cmd_fetch,     fetch_args),
    ]
    if include_player_history:
        steps.append((cmd_fetch_player_history, ["--months", str(months)]))
    steps += [
        (cmd_snapshot,  []),
        (cmd_dashboard, []),
    ]

    for cmd, args in steps:
        console.rule(f"[bold]{cmd.name}[/]")
        result = runner.invoke(cmd, args, catch_exceptions=False)
        console.print(result.output)


if __name__ == "__main__":
    cli()
