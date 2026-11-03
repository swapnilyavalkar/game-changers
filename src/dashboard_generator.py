from __future__ import annotations

"""
Dashboard generator — Option A (offline HTML) and Option B (JSON + SPA).

Option A: reports/dashboard.html
  Self-contained single file. All player data embedded as window.__DATA__ JSON.
  Alpine.js + ApexCharts loaded from CDN (or inlined with --offline flag).
  Works opened directly from filesystem: file:///path/to/dashboard.html

Option B: reports/data/data.json
  JSON file consumed by GitHub Pages SPA.
  Auto-deployed by GitHub Actions on every Monday run.
"""

import json
import logging
import math
import urllib.request
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    FORMATS, REPORTS_DIR, DATA_DIR, TEAM_ID, TEMPLATES_DIR,
    BAT_AVG_BENCHMARK, BAT_SR_BENCHMARK, BOWL_ECON_GOOD, BOWL_ECON_BAD,
    WEIGHTS, RECENT_MATCHES_N, FORM_IN_THRESHOLD, FORM_AVG_THRESHOLD,
)
import db
import analyzer
import selector as sel_module
from player_fetcher import fetch_all_player_profiles

log = logging.getLogger(__name__)

DATA_OUT_DIR  = REPORTS_DIR / "data"
JS_CACHE_DIR  = DATA_DIR / "cache" / "js_libs"
DATA_OUT_DIR.mkdir(parents=True, exist_ok=True)
JS_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── JSON safety ───────────────────────────────────────────────────────────────

def _clean(v):
    """Replace NaN/inf with 0 for JSON safety."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return 0.0
    return v


def _serialise(obj):
    """Recursively clean a nested structure for JSON serialisation."""
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(i) for i in obj]
    return _clean(obj)


# ── Opportunity payload (Phase 2) ────────────────────────────────────────────

def _why_not_selected(player_id: int, selections: dict) -> dict[str, str]:
    """
    For each format, explain why a player was NOT selected (or confirm they were).
    Returns {format_str: reason_str}.
    """
    reasons: dict[str, str] = {}
    for fmt, sizes in selections.items():
        result_11 = sizes.get(11, {})
        squad_ids = {p["player_id"] for p in result_11.get("squad", [])}
        bench_ids = {p["player_id"] for p in result_11.get("bench", [])}
        if player_id in squad_ids:
            p = next(p for p in result_11["squad"] if p["player_id"] == player_id)
            reasons[str(fmt)] = f"Selected — {p.get('reason', '')}"
        elif player_id in bench_ids:
            p = next(p for p in result_11["bench"] if p["player_id"] == player_id)
            reasons[str(fmt)] = f"On bench (score {p.get('score', 0):.1f})"
        else:
            reasons[str(fmt)] = "Not selected — score below threshold"
    return reasons


def _opportunity_index(player_id: int, df, total_team_matches: int) -> float:
    """
    Opportunity Index = (NCT matches where player had any role) / total_nct_matches × 100
    Measures how often the player gets a chance to play for Game Changers specifically.
    """
    if df.empty or total_team_matches == 0:
        return 0.0
    player_nct = df[(df["player_id"] == player_id) & (df["is_game_changers_match"] == 1)]["match_id"].nunique()
    return round(player_nct / total_team_matches * 100, 1)


def _trajectory_arrow(analysis: dict, fmt: int = 0) -> str:
    """
    Compare last-3 vs previous-3 form score. Returns: 'up', 'down', 'stable'.
    """
    fs = analysis.get("formats", {}).get(fmt, {})
    recent = fs.get("recent_bat_avg", 0)
    career = fs.get("batting_avg", 0)
    if career == 0:
        return "stable"
    ratio = recent / career if career > 0 else 1.0
    if ratio >= 1.25:
        return "up"
    if ratio <= 0.60:
        return "down"
    return "stable"


def _build_bowling_phase_stats(players_db: list[dict]) -> dict:
    """
    Aggregate per-bowler per-format bowling phase breakdown from balls_*.json commentary files.
    Returns: {player_name: {fmt_str: {phase: {"overs": float, "runs": int, "wickets": int, "economy": float}}}}

    Phase boundaries (0-indexed over number in JSON):
      6-over:  powerplay 0-2,  death 3+
      10-over: powerplay 0-2,  middle 3-6,  death 7+
      12-over: powerplay 0-2,  middle 3-8,  death 9+
      15-over: powerplay 0-4,  middle 5-9,  death 10+
      18-over: powerplay 0-5,  middle 6-11, death 12+
      20-over: powerplay 0-5,  middle 6-14, death 15+
    """
    import re

    PHASES: dict[int, list[tuple[str, int, int]]] = {
        6:  [("powerplay", 0, 2), ("death",    3, 100)],
        10: [("powerplay", 0, 2), ("middle",   3,   6), ("death",  7, 100)],
        12: [("powerplay", 0, 2), ("middle",   3,   8), ("death",  9, 100)],
        15: [("powerplay", 0, 4), ("middle",   5,   9), ("death", 10, 100)],
        18: [("powerplay", 0, 5), ("middle",   6,  11), ("death", 12, 100)],
        20: [("powerplay", 0, 5), ("middle",   6,  14), ("death", 15, 100)],
    }

    def norm(name: str) -> str:
        return re.sub(r'\s*\([^)]*\)', '', name).strip().lower()

    # Build Game Changers name lookup: normalised → original
    nct_names: dict[str, str] = {}
    for p in players_db:
        if p.get("is_game_changers") == 1:
            nct_names[norm(p["name"])] = p["name"]

    # Match format lookup: cricheroes_match_id → overs
    all_matches = db.get_all_matches()
    match_overs_map = {str(m["cricheroes_match_id"]): int(m.get("overs") or 0) for m in all_matches}

    commentary_dir = DATA_DIR / "cache" / "commentary"
    if not commentary_dir.exists():
        return {}

    # Raw accumulator: {(orig_name, fmt_int): {phase: {"balls": int, "runs": int, "wickets": int}}}
    raw: dict = {}

    for balls_file in sorted(commentary_dir.glob("balls_*.json")):
        match_id = balls_file.stem[len("balls_"):]
        fmt = match_overs_map.get(match_id)
        if not fmt:
            continue
        if fmt not in PHASES:
            fmt = min(PHASES.keys(), key=lambda x: abs(x - fmt))

        phase_defs = PHASES[fmt]

        try:
            with open(balls_file, encoding="utf-8") as f:
                balls = json.load(f)
        except Exception:
            continue

        for ball in balls:
            bname_norm = norm(ball.get("bowler_name", ""))
            if bname_norm not in nct_names:
                continue
            orig_name = nct_names[bname_norm]
            over = int(ball.get("over", 0))

            phase = None
            for ph, lo, hi in phase_defs:
                if lo <= over <= hi:
                    phase = ph
                    break
            if phase is None:
                continue

            key = (orig_name, fmt)
            raw.setdefault(key, {}).setdefault(phase, {"balls": 0, "runs": 0, "wickets": 0})
            entry = raw[key][phase]
            entry["balls"]   += 1
            entry["runs"]    += int(ball.get("runs", 0)) + int(ball.get("extras", 0))
            entry["wickets"] += 1 if ball.get("is_wicket") else 0

    # Convert to overs + economy per format
    result: dict = {}
    for (name, fmt), phases_data in raw.items():
        fmt_str = str(fmt)
        result.setdefault(name, {})[fmt_str] = {}
        for phase, d in phases_data.items():
            balls   = d["balls"]
            overs   = round(balls / 6, 1)
            economy = round(d["runs"] / overs, 2) if overs > 0 else 0.0
            result[name][fmt_str][phase] = {
                "overs":   overs,
                "runs":    d["runs"],
                "wickets": d["wickets"],
                "economy": economy,
            }

    # Build "0" (All formats) entry by aggregating balls/runs/wickets across all formats
    for name, fmt_map in result.items():
        combined: dict[str, dict] = {}  # phase → {balls, runs, wickets}
        for fmt_str, phases_data in fmt_map.items():
            if fmt_str == "0":
                continue
            fmt_int = int(fmt_str)
            balls_per_over = 6
            for phase, s in phases_data.items():
                balls = round(s["overs"] * balls_per_over)
                combined.setdefault(phase, {"balls": 0, "runs": 0, "wickets": 0})
                combined[phase]["balls"]   += balls
                combined[phase]["runs"]    += s["runs"]
                combined[phase]["wickets"] += s["wickets"]
        if combined:
            result[name]["0"] = {}
            for phase, d in combined.items():
                overs   = round(d["balls"] / 6, 1)
                economy = round(d["runs"] / overs, 2) if overs > 0 else 0.0
                result[name]["0"][phase] = {
                    "overs":   overs,
                    "runs":    d["runs"],
                    "wickets": d["wickets"],
                    "economy": economy,
                }

    return result


def _min_to_qualify() -> dict:
    """
    Minimum thresholds a player should meet to be considered for selection.
    Based on local cricket benchmarks.
    """
    return {
        "batsman":       {"matches": 3, "batting_avg": BAT_AVG_BENCHMARK * 0.6, "batting_sr": BAT_SR_BENCHMARK * 0.7},
        "bowler":        {"matches": 3, "bowling_economy": BOWL_ECON_BAD * 0.9, "wickets_per_match": 0.5},
        "all_rounder":   {"matches": 3, "batting_avg": BAT_AVG_BENCHMARK * 0.4, "wickets_per_match": 0.3},
        "wicket_keeper": {"matches": 3, "batting_avg": BAT_AVG_BENCHMARK * 0.5, "dismissals_per_match": 0.3},
    }


def build_opportunity_payload(
    players_db: list[dict],
    all_analysis: list[dict],
    df,
    selections: dict,
) -> dict:
    """
    Build the Opportunity Board payload (Phase 2):
    - Bowling discipline stats per player per format
    - Batting by position breakdown
    - Chances given/not given (yet_to_bat, did_not_bowl)
    - Why not selected (per format)
    - Opportunity Index
    - Trajectory arrow
    - Peer comparison vs team average
    - Minimum-to-qualify thresholds
    """
    import pandas as pd

    analysis_by_pid = {a["player_id"]: a for a in all_analysis}

    # ── Bowling phase stats from commentary cache ─────────────────────────────
    bowling_phase_stats = _build_bowling_phase_stats(players_db)

    # Total unique Game Changers team matches (exclude cross-team player history)
    total_team_matches = int(df[df["is_game_changers_match"] == 1]["match_id"].nunique()) if not df.empty else 0

    # Team averages across all players (all-format)
    team_bat_avg  = float(df["runs"].mean())    if not df.empty and "runs"    in df.columns else 0
    team_bowl_econ = float(df[df["overs_bowled"] > 0]["economy"].mean()) if not df.empty and "economy" in df.columns else 0

    players_out = []
    for p in players_db:
        pid   = p["id"]
        role  = p.get("role", "unknown")
        an    = analysis_by_pid.get(pid, {})

        fmt_opportunity: dict[str, dict] = {}
        for fmt in [0] + FORMATS:
            fs = an.get("formats", {}).get(fmt, {})
            fmt_key = str(fmt)

            # ── Bowling discipline ─────────────────────────────────────────────
            bowling_discipline = {
                "wides_per_over":      fs.get("wides_per_over", 0),
                "no_balls_per_over":   fs.get("no_balls_per_over", 0),
                "bdry_per_over":       fs.get("bdry_per_over", 0),
                "dot_pct":             fs.get("dot_pct", 0),
                "discipline_score":    fs.get("discipline_score"),
                "wides_total":         fs.get("wides_total", 0),
                "no_balls_total":      fs.get("no_balls_total", 0),
                "boundaries_conceded": fs.get("boundaries_conceded", 0),
            }

            # ── Batting position breakdown ─────────────────────────────────────
            batting_position = fs.get("batting_by_position", {
                "top_order": {"innings": 0, "runs": 0, "avg": 0, "sr": 0},
                "mid_order": {"innings": 0, "runs": 0, "avg": 0, "sr": 0},
                "low_order": {"innings": 0, "runs": 0, "avg": 0, "sr": 0},
            })
            # Exact position frequency: {pos_number: count}
            position_freq = fs.get("position_freq", {})

            # ── Chances not given ──────────────────────────────────────────────
            yet_to_bat   = fs.get("yet_to_bat_count", 0)
            did_not_bowl = fs.get("did_not_bowl_count", 0)
            matches      = fs.get("matches_played", 0)
            chances = {
                "yet_to_bat":          yet_to_bat,
                "did_not_bowl":        did_not_bowl,
                "batting_chance_pct":  round((matches - yet_to_bat) / max(matches, 1) * 100, 1),
                "bowling_chance_pct":  round((matches - did_not_bowl) / max(matches, 1) * 100, 1) if role in ("bowler", "all_rounder") else None,
            }

            # ── Peer comparison vs team average ───────────────────────────────
            peer = {
                "bat_avg_vs_team":  round(fs.get("batting_avg", 0) - team_bat_avg, 2),
                "econ_vs_team":     round(fs.get("bowling_economy", 0) - team_bowl_econ, 2) if fs.get("bowling_economy", 0) > 0 else None,
                "team_bat_avg":     round(team_bat_avg, 2),
                "team_bowl_econ":   round(team_bowl_econ, 2),
            }

            # ── Bowling by over phase (from commentary balls data) ─────────────
            bpo = bowling_phase_stats.get(p["name"], {}).get(fmt_key) or None

            fmt_opportunity[fmt_key] = {
                "discipline":       bowling_discipline,
                "batting_position": batting_position,
                "position_freq":    position_freq,
                "chances":          chances,
                "peer_comparison":  peer,
                "bowling_by_over":  bpo,
            }

        # ── Per-player cross-format fields ────────────────────────────────────
        why_not = _why_not_selected(pid, selections)
        opp_idx = _opportunity_index(pid, df, total_team_matches)
        traj    = _trajectory_arrow(an)

        # ── Minimum to qualify check ───────────────────────────────────────────
        thresholds = _min_to_qualify().get(role, {})
        all_fmt_fs = an.get("formats", {}).get(0, {})
        meets_min: dict = {}
        if thresholds:
            meets_min["matches"]    = all_fmt_fs.get("matches_played", 0) >= thresholds.get("matches", 0)
            if "batting_avg" in thresholds:
                meets_min["batting_avg"] = all_fmt_fs.get("batting_avg", 0) >= thresholds["batting_avg"]
            if "batting_sr" in thresholds:
                meets_min["batting_sr"]  = all_fmt_fs.get("batting_sr", 0) >= thresholds["batting_sr"]
            if "bowling_economy" in thresholds:
                ec = all_fmt_fs.get("bowling_economy", 0)
                meets_min["bowling_economy"] = 0 < ec <= thresholds["bowling_economy"]
            if "wickets_per_match" in thresholds:
                m  = max(all_fmt_fs.get("matches_played", 1), 1)
                wpm = all_fmt_fs.get("wickets_total", 0) / m
                meets_min["wickets_per_match"] = wpm >= thresholds["wickets_per_match"]
            if "dismissals_per_match" in thresholds:
                m   = max(all_fmt_fs.get("matches_played", 1), 1)
                dpm = all_fmt_fs.get("dismissals_taken", 0) / m
                meets_min["dismissals_per_match"] = dpm >= thresholds["dismissals_per_match"]

        players_out.append({
            "id":               pid,
            "name":             p["name"],
            "role":             role,
            "formats":          fmt_opportunity,
            "why_not_selected": why_not,
            "opportunity_index": opp_idx,
            "trajectory":       traj,
            "meets_minimum":    meets_min,
            "thresholds":       thresholds,
        })

    return {
        "players":         players_out,
        "min_thresholds":  _min_to_qualify(),
        "benchmarks": {
            "bat_avg":   BAT_AVG_BENCHMARK,
            "bat_sr":    BAT_SR_BENCHMARK,
            "bowl_econ_good": BOWL_ECON_GOOD,
            "bowl_econ_bad":  BOWL_ECON_BAD,
        },
    }


# ── Data payload assembly ─────────────────────────────────────────────────────

def build_data_payload(
    players_db: list[dict],
    all_analysis: list[dict],
    df,
    selections: dict,
    profiles: dict,
    snapshots_by_player: dict,
) -> dict:
    """
    Assemble the complete dashboard JSON payload from all data sources.

    - formats stats: from DB match data (via analyzer)
    - career stats:  from player_fetcher (all-time CricHeroes profile stats)
    - wow delta:     from weekly_snapshots DB table
    - squad:         from selector
    """
    analysis_by_pid = {a["player_id"]: a for a in all_analysis}

    players_out = []
    for p in players_db:
        pid   = p["id"]
        ch_id = str(p.get("cricheroes_id", ""))
        profile  = profiles.get(ch_id, {})
        analysis = analysis_by_pid.get(pid, {})

        # ── Format-specific stats ─────────────────────────────────────────────
        fmt_stats = {}
        for fmt in [0] + FORMATS:
            fs = analysis.get("formats", {}).get(fmt, {})
            fmt_stats[str(fmt)] = {
                "matches_played":     fs.get("matches_played", 0),
                "innings_batted":     fs.get("innings_batted", 0) or 0,
                "batting_avg":        round(fs.get("batting_avg", 0) or 0, 2),
                "batting_sr":         round(fs.get("batting_sr", 0) or 0, 2),
                "runs_total":         fs.get("runs_total", 0) or 0,
                "highest_score":      fs.get("highest_score", 0) or 0,
                "bowling_avg":        round(fs.get("bowling_avg", 0) or 0, 2),
                "bowling_economy":    round(fs.get("bowling_economy", 0) or 0, 2),
                "wickets_total":      fs.get("wickets_total", 0) or 0,
                "overs_bowled_total": round(fs.get("overs_bowled_total", 0) or 0, 2),
                "form_score":         round(fs.get("form_score", 0) or 0, 2),
                "recent_bat_avg":     round(fs.get("recent_bat_avg", 0) or 0, 2),
            }

        # ── W-o-W delta ───────────────────────────────────────────────────────
        snaps       = snapshots_by_player.get(pid, [])
        current_s   = snaps[0] if snaps else {}
        prev_s      = snaps[1] if len(snaps) > 1 else None
        wow         = analyzer.compute_wow_delta(current_s, prev_s)

        # ── Form badge ────────────────────────────────────────────────────────
        all_fmt_stats = analysis.get("formats", {}).get(0, {})
        form_badge = analyzer.classify_form(all_fmt_stats)

        # ── Recent match history (from DB, last 50 — supports last-N per-player filter) ──
        recent_matches = []
        if not df.empty:
            import pandas as pd
            pm = (
                df[df["player_id"] == pid]
                .sort_values("match_date", ascending=False)
                .head(50)
            )
            for _, row in pm.iterrows():
                recent_matches.append({
                    "date":               str(row.get("match_date", "") or ""),
                    "opponent":           str(row.get("opponent", "") or ""),
                    "overs":              int(row.get("match_overs", 0) or 0),
                    "result":             str(row.get("result", "") or ""),
                    "is_game_changers_match": int(row.get("is_game_changers_match", 1) or 1),
                    "runs":               int(row.get("runs", 0) or 0),
                    "balls_faced":        int(row.get("balls_faced", 0) or 0),
                    "fours":              int(row.get("fours", 0) or 0),
                    "sixes":              int(row.get("sixes", 0) or 0),
                    "batting_sr":         round(float(row.get("batting_sr", 0) or 0), 2),
                    "is_out":             int(row.get("is_out", 0) or 0),
                    "yet_to_bat":         int(row.get("yet_to_bat", 0) or 0),
                    "overs_bowled":       round(float(row.get("overs_bowled", 0) or 0), 1),
                    "wickets":            int(row.get("wickets", 0) or 0),
                    "runs_conceded":      int(row.get("runs_conceded", 0) or 0),
                    "economy":            round(float(row.get("economy", 0) or 0), 2),
                })

        # ── Career all-time stats (from player_fetcher) ───────────────────────
        career_bat  = profile.get("career_batting", {}) or {}
        career_bowl = profile.get("career_bowling", {}) or {}
        career_fld  = profile.get("career_fielding", {}) or {}

        # ── WoW trend (weekend clusters) ──────────────────────────────────────
        wow_trend = analysis.get("wow_trend", {})

        players_out.append({
            "id":              pid,
            "cricheroes_id":   ch_id,
            "name":            p["name"],
            "role":            p.get("role", "unknown"),
            "photo_url":       (profile.get("photo_url") or p.get("photo_url") or ""),
            "batting_style":   (profile.get("batting_style") or p.get("batting_style") or ""),
            "bowling_style":   (profile.get("bowling_style") or p.get("bowling_style") or ""),
            "form_badge":      form_badge,
            "overall_score":   round(analysis.get("overall_score", 0) or 0, 2),
            "formats":         fmt_stats,
            "wow":             wow,
            "wow_trend":       wow_trend,
            "recent_matches":  recent_matches,
            "career_batting":  career_bat,
            "career_bowling":  career_bowl,
            "career_fielding": career_fld,
        })

    # ── Squad selections ──────────────────────────────────────────────────────
    sels_out = {}
    for fmt, sizes in selections.items():
        sels_out[str(fmt)] = {}
        for sz, result in sizes.items():
            sels_out[str(fmt)][str(sz)] = {
                "squad":        result.get("squad", []),
                "bench":        result.get("bench", []),
                "warnings":     result.get("warnings", []),
                "role_summary": result.get("role_summary", {}),
            }

    nct_matches = int(df[df["is_game_changers_match"] == 1]["match_id"].nunique()) if not df.empty else 0

    return {
        "team": {"name": "Game Changers", "team_id": TEAM_ID, "nct_matches": nct_matches},
        "players":      players_out,
        "selections":   sels_out,
        "formats":      [str(f) for f in FORMATS],
        "generated_at": date.today().isoformat(),
        # opportunity populated separately and merged in generate()
        "config": {
            "bat_avg_benchmark":  BAT_AVG_BENCHMARK,
            "bat_sr_benchmark":   BAT_SR_BENCHMARK,
            "bowl_econ_good":     BOWL_ECON_GOOD,
            "bowl_econ_bad":      BOWL_ECON_BAD,
            "recent_matches_n":   RECENT_MATCHES_N,
            "form_in_threshold":  FORM_IN_THRESHOLD,
            "form_avg_threshold": FORM_AVG_THRESHOLD,
            "weight_recent_form":     WEIGHTS["recent_form"],
            "weight_format_specific": WEIGHTS["format_specific"],
            "weight_career_avg":      WEIGHTS["career_avg"],
            "weight_consistency":     WEIGHTS["consistency"],
        },
    }


# ── Fallback analysis (when no match DB data yet) ─────────────────────────────

def _analysis_from_profiles(players: list[dict], profiles: dict) -> list[dict]:
    """
    Build minimal analysis from CricHeroes career stats when no match-level
    DB data exists yet. Used on fresh install before first `fetch` run.
    """
    results = []
    for p in players:
        ch_id  = str(p.get("cricheroes_id", ""))
        prof   = profiles.get(ch_id, {})
        bat    = prof.get("career_batting") or {}
        bowl   = prof.get("career_bowling") or {}

        base_stats = {
            "player_id":       p["id"],
            "format_overs":    0,
            "matches_played":  bat.get("matches") or bowl.get("matches") or 0,
            "batting_avg":     float(bat.get("average") or 0),
            "batting_sr":      float(bat.get("strike_rate") or 0),
            "runs_total":      int(bat.get("runs") or 0),
            "highest_score":   int(bat.get("highest") or 0),
            "batting_std":     0.0,
            "innings":         int(bat.get("innings") or 0),
            "bowling_avg":     float(bowl.get("average") or 0),
            "bowling_economy": float(bowl.get("economy") or 0),
            "wickets_total":   int(bowl.get("wickets") or 0),
            "bowling_std":     0.0,
            "recent_bat_avg":  float(bat.get("average") or 0),
            "recent_wkts":     0.0,
            "recent_econ":     float(bowl.get("economy") or 0),
        }
        base_stats["form_score"] = analyzer.compute_composite_score(
            base_stats, p.get("role", "unknown")
        )

        fmt_stats = {}
        for fmt in [0] + FORMATS:
            fs = dict(base_stats)
            fs["format_overs"] = fmt
            fmt_stats[fmt] = fs

        results.append({
            "player_id":      p["id"],
            "name":           p["name"],
            "role":           p.get("role", "unknown"),
            "formats":        fmt_stats,
            "overall_score":  base_stats["form_score"],
            "overall_matches": base_stats["matches_played"],
        })

    results.sort(key=lambda x: x["overall_score"], reverse=True)
    return results


# ── Strategy payload ──────────────────────────────────────────────────────────

def _build_strategy_payload(players_db: list[dict], strategy_raw: dict,
                             bvb_rows: list[dict] | None = None) -> dict:
    """
    Transform raw strategy DB rows into dashboard-ready payload.

    Structure:
    {
      per_opponent: [{opponent, matches, wins, losses, ...}],
      our_bowlers_dismissals: [
        { bowler_name, bowler_player_id, victims: [{batter, dismissal_type, opponent, date}, ...], total }
      ],
      opponent_bowlers_vs_us: [
        { bowler_name, our_batters_dismissed: [{batter, dismissal_type, date, opponent}, ...], total }
      ],
      opponent_bowler_stats: [
        { bowler_name, opponent, matches_vs_us, total_overs, total_runs, total_wickets, avg_economy }
      ],
    }
    """
    our_player_map = {p["id"]: p["name"] for p in players_db}

    # ── Per opponent summary ───────────────────────────────────────────────────
    per_opponent = [dict(r) for r in strategy_raw.get("per_opponent", [])]

    # Raw per-match rows for client-side format/lastN filtering
    per_opponent_raw = [
        {
            "match_date":    r.get("match_date", "")[:10],
            "opponent":      r.get("opponent", ""),
            "result":        r.get("result", ""),
            "match_overs":   int(r.get("match_overs") or 0),
            "match_runs":    int(r.get("match_runs") or 0),
            "match_wickets": int(r.get("match_wickets") or 0),
        }
        for r in strategy_raw.get("per_opponent_matches", [])
    ]

    # ── Our bowlers → opponent dismissals ─────────────────────────────────────
    bowler_dict: dict[str, dict] = {}
    for d in strategy_raw.get("dismissals_by_bowler", []):
        bpid = d.get("bowler_player_id")
        key  = str(bpid) if bpid else d.get("bowler_name", "")
        if key not in bowler_dict:
            bowler_dict[key] = {
                "bowler_name":      our_player_map.get(bpid, d.get("bowler_name", "")),
                "bowler_player_id": bpid,
                "victims":          [],
                "total":            0,
            }
        bowler_dict[key]["victims"].append({
            "batter":         d.get("batter_name"),
            "dismissal_type": d.get("dismissal_type"),
            "opponent":       d.get("opponent"),
            "date":           d.get("match_date", "")[:10],
            "overs":          int(d.get("match_overs") or 0),
        })
        bowler_dict[key]["total"] += 1

    our_bowlers_dismissals = sorted(
        bowler_dict.values(), key=lambda x: x["total"], reverse=True
    )

    # ── Opponent bowlers → our batter dismissals ───────────────────────────────
    opp_bowler_dict: dict[str, dict] = {}
    for d in strategy_raw.get("dismissals_of_batters", []):
        key = d.get("bowler_name", "Unknown")
        if key not in opp_bowler_dict:
            opp_bowler_dict[key] = {
                "bowler_name":          key,
                "our_batters_dismissed": [],
                "total":                0,
            }
        bpid  = d.get("batter_player_id")
        bname = our_player_map.get(bpid, d.get("batter_name", ""))
        opp_bowler_dict[key]["our_batters_dismissed"].append({
            "batter":           bname,
            "batter_player_id": bpid,
            "dismissal_type":   d.get("dismissal_type"),
            "opponent":         d.get("opponent"),
            "date":             d.get("match_date", "")[:10],
            "overs":            int(d.get("match_overs") or 0),
        })
        opp_bowler_dict[key]["total"] += 1

    opponent_bowlers_vs_us = sorted(
        opp_bowler_dict.values(), key=lambda x: x["total"], reverse=True
    )

    # ── Opponent bowler per-match figures vs us ────────────────────────────────
    # Key includes opponent so same name on different teams isn't merged
    opp_match_dict: dict[str, dict] = {}
    for obs in strategy_raw.get("opponent_bowler_stats", []):
        bname    = obs.get("bowler_name", "")
        opponent = obs.get("opponent", "")
        key      = f"{bname}|{opponent}"
        if key not in opp_match_dict:
            opp_match_dict[key] = {
                "bowler_name":   bname,
                "opponent":      opponent,
                "matches_vs_us": 0,
                "total_overs":   0.0,
                "total_runs":    0,
                "total_wickets": 0,
                "per_match":     [],
            }
        entry = opp_match_dict[key]
        entry["matches_vs_us"]  += 1
        entry["total_overs"]    += float(obs.get("overs") or 0)
        entry["total_runs"]     += int(obs.get("runs_conceded") or 0)
        entry["total_wickets"]  += int(obs.get("wickets") or 0)
        entry["per_match"].append({
            "date":        obs.get("match_date", "")[:10],
            "overs":       float(obs.get("overs") or 0),
            "runs":        int(obs.get("runs_conceded") or 0),
            "wickets":     int(obs.get("wickets") or 0),
            "economy":     float(obs.get("economy_rate") or 0),
            "match_overs": int(obs.get("match_overs") or 0),
        })

    for entry in opp_match_dict.values():
        ov = entry["total_overs"]
        entry["avg_economy"] = round(entry["total_runs"] / max(ov, 0.001), 2) if ov > 0 else 0

    opponent_bowler_stats = sorted(
        opp_match_dict.values(), key=lambda x: x["total_wickets"], reverse=True
    )

    # ── Batter vs bowler (from commentary ball-by-ball API) ───────────────────
    # Group by batter: { batter_name: [{ bowler_name, matches, runs, balls, sr, dismissals, ... }] }
    bvb_by_batter: dict[str, dict] = {}
    our_player_id_to_name = {p["id"]: p["name"] for p in players_db}
    for row in (bvb_rows or []):
        pid   = row.get("batter_player_id")
        bname = our_player_id_to_name.get(pid, row.get("batter_name", ""))
        if not bname:
            continue
        if bname not in bvb_by_batter:
            bvb_by_batter[bname] = {
                "batter_name":      bname,
                "batter_player_id": pid,
                "vs_bowlers":       [],
            }
        bvb_by_batter[bname]["vs_bowlers"].append({
            "bowler_name":  row.get("bowler_name", ""),
            "bowler_ch_id": row.get("bowler_ch_id"),
            "matches":      row.get("matches", 0),
            "runs":         row.get("runs", 0),
            "balls":        row.get("balls", 0),
            "fours":        row.get("fours", 0),
            "sixes":        row.get("sixes", 0),
            "dismissals":   row.get("dismissals", 0),
            "dot_balls":    row.get("dot_balls", 0),
            "strike_rate":  row.get("strike_rate", 0),
        })

    batter_vs_bowler = sorted(bvb_by_batter.values(), key=lambda x: x["batter_name"])

    return {
        "per_opponent":            per_opponent,
        "per_opponent_raw":        per_opponent_raw,
        "our_bowlers_dismissals":  our_bowlers_dismissals,
        "opponent_bowlers_vs_us":  opponent_bowlers_vs_us,
        "opponent_bowler_stats":   opponent_bowler_stats,
        "batter_vs_bowler":        batter_vs_bowler,
    }


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render_dashboard_html(payload: dict, offline: bool = False) -> str:
    """Render dashboard.html Jinja2 template with embedded data payload."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # data_json is embedded inside <script>, no HTML escaping
    )
    template = env.get_template("dashboard.html")

    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    kwargs: dict = {"data_json": data_json, "offline": offline}
    if offline:
        kwargs.update(_get_inline_libs())

    return template.render(**kwargs)


def _get_inline_libs() -> dict:
    """
    Download JS/CSS libs once and cache locally for offline embedding.
    Returns dict of {key: file_content}.
    """
    libs = {
        "alpine_js":     ("alpine.min.js",     "https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js"),
        "apexcharts_js": ("apexcharts.min.js",  "https://cdn.jsdelivr.net/npm/apexcharts@3.54.0/dist/apexcharts.min.js"),
    }
    result = {}
    for key, (filename, url) in libs.items():
        cache_file = JS_CACHE_DIR / filename
        if not cache_file.exists():
            log.info(f"[dashboard_gen] downloading {filename}...")
            urllib.request.urlretrieve(url, cache_file)
        result[key] = cache_file.read_text(encoding="utf-8")
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def generate(
    force_player_refresh: bool = False,
    offline: bool = False,
    skip_profiles: bool = False,
) -> None:
    """
    Full pipeline:
      1. Load match stats from SQLite
      2. Fetch all-time career stats from CricHeroes player profiles
      3. Run form analysis + squad selection
      4. Write reports/data/data.json       (Option B)
      5. Write reports/dashboard.html       (Option A)
    """
    db.init_db()
    df      = db.get_player_stats_df()
    players = db.get_game_changers_players()

    if not players:
        log.warning("[dashboard_gen] No players in DB — run `python main.py fetch` first.")
        return

    if df.empty:
        log.warning("[dashboard_gen] No match data — using career profile stats only.")

    # ── Player profiles (all-time career stats from CricHeroes) ───────────────
    if skip_profiles:
        log.info("[dashboard_gen] Skipping player profile fetch (--skip-profiles)")
        profiles = {}
    else:
        log.info("[dashboard_gen] Fetching player career profiles...")
        profiles = fetch_all_player_profiles(players, force_refresh=force_player_refresh)

    # ── Analysis ──────────────────────────────────────────────────────────────
    log.info("[dashboard_gen] Running form analysis...")
    if not df.empty:
        all_analysis = analyzer.analyse_all(df, players)
    else:
        all_analysis = _analysis_from_profiles(players, profiles)

    # ── Squad selection ───────────────────────────────────────────────────────
    selections = sel_module.select_all_formats(all_analysis) if all_analysis else {}

    # ── Snapshots for W-o-W ───────────────────────────────────────────────────
    snapshots_by_player = {
        p["id"]: db.get_snapshots_for_player(p["id"], 0)
        for p in players
    }

    # ── Assemble payload ──────────────────────────────────────────────────────
    payload = build_data_payload(
        players_db=players,
        all_analysis=all_analysis,
        df=df,
        selections=selections,
        profiles=profiles,
        snapshots_by_player=snapshots_by_player,
    )

    # ── Opportunity Board payload (Phase 2) ───────────────────────────────────
    if not df.empty:
        opportunity = build_opportunity_payload(
            players_db=players,
            all_analysis=all_analysis,
            df=df,
            selections=selections,
        )
        payload["opportunity"] = opportunity
        payload["accountability"] = opportunity
    else:
        payload["opportunity"] = {"players": [], "min_thresholds": {}, "benchmarks": {}}
        payload["accountability"] = {"players": [], "min_thresholds": {}, "benchmarks": {}}

    # ── Strategy data (Phase 2: head-to-head, dismissals, opponent bowling) ───
    try:
        strategy_raw = db.get_strategy_data()
        bvb_rows = db.get_batter_vs_bowler_stats()
        payload["strategy"] = _build_strategy_payload(players, strategy_raw, bvb_rows)
    except Exception as e:
        log.warning(f"[dashboard_gen] Strategy data unavailable: {e}")
        payload["strategy"] = {
            "per_opponent": [], "our_bowlers_dismissals": [],
            "opponent_bowlers_vs_us": [], "opponent_bowler_stats": [],
            "batter_vs_bowler": [],
        }

    payload = _serialise(payload)

    # ── Option B: write data.json ─────────────────────────────────────────────
    json_path = DATA_OUT_DIR / "data.json"
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(f"[dashboard_gen] Option B JSON  → {json_path}")

    # ── Option A: render offline-capable HTML ─────────────────────────────────
    html = _render_dashboard_html(payload, offline=offline)
    html_path = REPORTS_DIR / "dashboard.html"
    html_path.write_text(html, encoding="utf-8")
    (REPORTS_DIR / "index.html").write_text(html, encoding="utf-8")   # identical copy for GitHub Pages
    log.info(f"[dashboard_gen] Option A HTML  → {html_path}")
    log.info(f"[dashboard_gen] Done. {len(players)} players, {len(all_analysis)} analysed.")
