from __future__ import annotations

"""
Statistical analysis engine.

For each player × format combination, computes:
  - batting_avg, batting_sr, highest_score
  - bowling_avg, bowling_economy
  - recent_form_score  (last N matches, weighted)
  - composite_score    (0–100, used for squad selection)
  - week-on-week delta vs previous snapshot
"""

import logging
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    FORMATS, WEIGHTS, RECENT_MATCHES_N,
    BAT_AVG_BENCHMARK, BAT_SR_BENCHMARK, BOWL_ECON_GOOD, BOWL_ECON_BAD,
)

log = logging.getLogger(__name__)

SMALL = 1e-9  # avoid division by zero


# ── Core per-player per-format stats ─────────────────────────────────────────

def compute_player_format_stats(df: pd.DataFrame, player_id: int, fmt: int) -> dict:
    """
    df: full joined dataframe from db.get_player_stats_df()
    fmt: 0 = all formats, else specific overs
    """
    pdata = df[df["player_id"] == player_id].copy()
    if fmt != 0:
        pdata = pdata[pdata["match_overs"] == fmt]

    if pdata.empty:
        return _empty_stats(player_id, fmt)

    pdata = pdata.sort_values("match_date", ascending=False)

    # ── Batting ───────────────────────────────────────────────────────────────
    bat = pdata[pdata["runs"].notna() & (pdata["balls_faced"] > 0)]
    innings      = len(bat)
    dismissals   = int(bat["is_out"].sum())
    runs_total   = int(bat["runs"].sum())
    balls_total  = int(bat["balls_faced"].sum())
    highest      = int(bat["runs"].max()) if innings else 0
    batting_avg  = round(runs_total / max(dismissals, 1), 2)
    batting_sr   = round((runs_total / max(balls_total, 1)) * 100, 2)
    batting_std  = float(bat["runs"].std()) if innings > 1 else 0.0

    # ── Bowling ───────────────────────────────────────────────────────────────
    bowl = pdata[pdata["overs_bowled"] > 0]
    wickets_total = int(bowl["wickets"].sum())
    runs_conceded = int(bowl["runs_conceded"].sum())
    overs_bowled  = float(bowl["overs_bowled"].sum())
    bowling_avg   = round(runs_conceded / max(wickets_total, 1), 2)
    bowling_econ  = round(runs_conceded / max(overs_bowled, SMALL), 2)
    bowling_std   = float(bowl["economy"].std()) if len(bowl) > 1 else 0.0

    # ── Bowling extras + discipline ───────────────────────────────────────────
    dot_balls_total     = int(bowl["dot_balls"].sum())           if "dot_balls"           in bowl.columns else 0
    wides_total         = int(bowl["wides"].sum())               if "wides"               in bowl.columns else 0
    no_balls_total      = int(bowl["no_balls"].sum())            if "no_balls"            in bowl.columns else 0
    boundaries_conceded = int(bowl["boundaries_conceded"].sum()) if "boundaries_conceded" in bowl.columns else 0
    balls_bowled        = int(round(overs_bowled * 6)) if overs_bowled > 0 else 0
    bowling_sr          = round(balls_bowled / max(wickets_total, 1), 2) if wickets_total > 0 else 0.0

    # Rates (per over)
    wides_per_over      = round(wides_total      / max(overs_bowled, SMALL), 2)
    no_balls_per_over   = round(no_balls_total   / max(overs_bowled, SMALL), 2)
    bdry_per_over       = round(boundaries_conceded / max(overs_bowled, SMALL), 2)
    dot_pct             = round(dot_balls_total  / max(balls_bowled, 1) * 100, 1)
    # Discipline score: 100 = perfect, penalties for wides/NB/boundaries given
    # Each wide/NB is a free delivery that costs runs; boundaries are big hits
    discipline_score    = max(0.0, round(
        100.0
        - wides_per_over   * 8.0   # each wide/over costs 8 pts
        - no_balls_per_over * 10.0  # each NB/over costs 10 pts
        - bdry_per_over    * 4.0,   # each boundary/over costs 4 pts
        1
    )) if overs_bowled > 0 else None

    # ── Chances tracking ──────────────────────────────────────────────────────
    yet_to_bat_count    = int(pdata["yet_to_bat"].sum())    if "yet_to_bat"    in pdata.columns else 0
    did_not_bowl_count  = int(pdata["did_not_bowl"].sum())  if "did_not_bowl"  in pdata.columns else 0

    # ── Fielding ──────────────────────────────────────────────────────────────
    catches_total   = int(pdata["catches"].sum())   if "catches"   in pdata.columns else 0
    run_outs_total  = int(pdata["run_outs"].sum())  if "run_outs"  in pdata.columns else 0
    stumpings_total = int(pdata["stumpings"].sum()) if "stumpings" in pdata.columns else 0
    dismissals_taken = catches_total + run_outs_total + stumpings_total

    # ── Batting position breakdown ────────────────────────────────────────────
    top_order_inn  = bat[bat["batting_position"].between(1, 3)]   if innings else bat.iloc[0:0]
    mid_order_inn  = bat[bat["batting_position"].between(4, 6)]   if innings else bat.iloc[0:0]
    low_order_inn  = bat[bat["batting_position"] >= 7]            if innings else bat.iloc[0:0]

    def _pos_stats(subset):
        if subset.empty:
            return {"innings": 0, "runs": 0, "avg": 0, "sr": 0}
        d = int(subset["is_out"].sum())
        r = int(subset["runs"].sum())
        b = int(subset["balls_faced"].sum())
        return {
            "innings": len(subset),
            "runs":    r,
            "avg":     round(r / max(d, 1), 2),
            "sr":      round(r / max(b, 1) * 100, 2),
        }

    batting_by_position = {
        "top_order":  _pos_stats(top_order_inn),
        "mid_order":  _pos_stats(mid_order_inn),
        "low_order":  _pos_stats(low_order_inn),
    }

    # ── Exact batting position frequency: {pos_number: count} ────────────────
    # e.g. {1: 2, 3: 5, 4: 1} means batted at #1 twice, #3 five times, etc.
    position_freq: dict[int, int] = {}
    if innings and "batting_position" in bat.columns:
        for pos_val, grp in bat.groupby("batting_position"):
            if pos_val is not None and not pd.isna(pos_val):
                position_freq[int(pos_val)] = len(grp)

    # ── Recent form (last N matches) ──────────────────────────────────────────
    recent        = pdata.head(RECENT_MATCHES_N)
    recent_bat    = recent[recent["balls_faced"] > 0]
    recent_runs   = float(recent_bat["runs"].mean())    if not recent_bat.empty  else 0
    recent_bat_sr = float(recent_bat["batting_sr"].mean()) if not recent_bat.empty else 0
    recent_bowl   = recent[recent["overs_bowled"] > 0]
    recent_wkts   = float(recent_bowl["wickets"].mean())  if not recent_bowl.empty else 0
    recent_econ   = float(recent_bowl["economy"].mean())  if not recent_bowl.empty else 0

    return {
        "player_id":       player_id,
        "format_overs":    fmt,
        "matches_played":  len(pdata),
        # batting
        "batting_avg":         batting_avg,
        "batting_sr":          batting_sr,
        "runs_total":          runs_total,
        "highest_score":       highest,
        "batting_std":         batting_std,
        "innings":             innings,
        "innings_batted":      innings,          # alias used by dashboard / planner
        "batting_by_position": batting_by_position,
        "position_freq":       position_freq,
        # bowling
        "overs_bowled_total":  round(overs_bowled, 2),  # decimal overs (for planner)
        "bowling_avg":         bowling_avg,
        "bowling_economy":     bowling_econ,
        "bowling_sr":          bowling_sr,
        "wickets_total":       wickets_total,
        "bowling_std":         bowling_std,
        "dot_balls_total":     dot_balls_total,
        # bowling discipline (Phase 1)
        "wides_total":         wides_total,
        "no_balls_total":      no_balls_total,
        "boundaries_conceded": boundaries_conceded,
        "wides_per_over":      wides_per_over,
        "no_balls_per_over":   no_balls_per_over,
        "bdry_per_over":       bdry_per_over,
        "dot_pct":             dot_pct,
        "discipline_score":    discipline_score,
        # chances
        "yet_to_bat_count":    yet_to_bat_count,
        "did_not_bowl_count":  did_not_bowl_count,
        # fielding
        "catches_total":       catches_total,
        "run_outs_total":      run_outs_total,
        "stumpings_total":     stumpings_total,
        "dismissals_taken":    dismissals_taken,
        # recent
        "recent_bat_avg":      round(recent_runs, 2),
        "recent_bat_sr":       round(recent_bat_sr, 2),
        "recent_wkts":         round(recent_wkts, 2),
        "recent_econ":         round(recent_econ, 2),
    }


def _empty_stats(player_id: int, fmt: int) -> dict:
    _empty_pos = {"top_order": {"innings": 0, "runs": 0, "avg": 0, "sr": 0},
                  "mid_order": {"innings": 0, "runs": 0, "avg": 0, "sr": 0},
                  "low_order": {"innings": 0, "runs": 0, "avg": 0, "sr": 0}}
    return {
        "player_id": player_id, "format_overs": fmt, "matches_played": 0,
        "batting_avg": 0, "batting_sr": 0, "runs_total": 0, "highest_score": 0,
        "batting_std": 0, "innings": 0, "innings_batted": 0, "batting_by_position": _empty_pos, "position_freq": {},
        "bowling_avg": 0, "bowling_economy": 0, "bowling_sr": 0, "overs_bowled_total": 0.0,
        "wickets_total": 0, "bowling_std": 0, "dot_balls_total": 0,
        "wides_total": 0, "no_balls_total": 0, "boundaries_conceded": 0,
        "wides_per_over": 0, "no_balls_per_over": 0, "bdry_per_over": 0,
        "dot_pct": 0, "discipline_score": None,
        "yet_to_bat_count": 0, "did_not_bowl_count": 0,
        "catches_total": 0, "run_outs_total": 0, "stumpings_total": 0,
        "dismissals_taken": 0,
        "recent_bat_avg": 0, "recent_bat_sr": 0, "recent_wkts": 0, "recent_econ": 0,
    }


# ── Composite scoring ─────────────────────────────────────────────────────────

def compute_composite_score(stats: dict, role: str) -> float:
    """
    Returns a 0–100 composite score for squad selection.
    Weights defined in config.WEIGHTS.
    """
    if stats["matches_played"] == 0:
        return 0.0

    # ── Batting component (0–100) — local cricket benchmarks ─────────────────
    # avg 20=100, SR 120=100, recent avg weighted, consistency bonus
    bat_avg_norm   = min(stats["batting_avg"]     / BAT_AVG_BENCHMARK, 1.0) * 100
    bat_sr_norm    = min(stats["batting_sr"]      / BAT_SR_BENCHMARK,  1.0) * 100
    bat_recent_avg = min(stats["recent_bat_avg"]  / BAT_AVG_BENCHMARK, 1.0) * 100
    bat_recent_sr  = min(stats.get("recent_bat_sr", 0) / BAT_SR_BENCHMARK, 1.0) * 100
    # Recent form = blend of recent avg and recent SR (ICC: both matter for batting)
    bat_recent     = bat_recent_avg * 0.6 + bat_recent_sr * 0.4
    bat_consist    = max(0, 100 - min(stats["batting_std"] / 15.0, 1.0) * 100)

    batting_score = (
        WEIGHTS["recent_form"]     * bat_recent   +
        WEIGHTS["format_specific"] * bat_sr_norm  +
        WEIGHTS["career_avg"]      * bat_avg_norm +
        WEIGHTS["consistency"]     * bat_consist
    )

    # ── Bowling component (0–100) — local cricket benchmarks ─────────────────
    # Economy: linear between BOWL_ECON_BAD (score=0) and BOWL_ECON_GOOD (score=100)
    _econ_range  = BOWL_ECON_BAD - BOWL_ECON_GOOD
    econ_inv     = max(0, (BOWL_ECON_BAD - stats["bowling_economy"])  / _econ_range) * 100 \
                   if stats["bowling_economy"] > 0 else 0
    recent_econ_inv = max(0, (BOWL_ECON_BAD - stats["recent_econ"])   / _econ_range) * 100 \
                   if stats["recent_econ"] > 0 else 0
    wkts_norm    = min(stats["wickets_total"] / 15.0, 1.0) * 100   # 15 wkts = top bowler
    bowl_consist = max(0, 100 - min(stats["bowling_std"] / 3.0, 1.0) * 100)

    bowling_score = (
        WEIGHTS["recent_form"]     * recent_econ_inv +
        WEIGHTS["format_specific"] * econ_inv        +
        WEIGHTS["career_avg"]      * wkts_norm       +
        WEIGHTS["consistency"]     * bowl_consist
    )

    # Discipline bonus/penalty: wides/no-balls adjust bowling score by up to ±5
    disc = stats.get("discipline_score")
    if disc is not None:
        # discipline_score 100 → +5pts, 50 → 0, 0 → -5pts
        discipline_adj = (disc - 50) / 10.0
        bowling_score  = max(0, bowling_score + discipline_adj)

    # ── Fielding bonus (up to +5 points) ─────────────────────────────────────
    # Catches, run-outs, stumpings add a small bonus to reward good fielders/keepers
    dismissals = stats.get("dismissals_taken", 0)
    matches    = max(stats["matches_played"], 1)
    field_bonus = min(dismissals / matches * 10, 5.0)   # max 5 points

    # ── Role weighting ────────────────────────────────────────────────────────
    if role == "batsman":
        score = batting_score + field_bonus * 0.5
    elif role == "bowler":
        score = bowling_score + field_bonus * 0.5
    elif role == "wicket_keeper":
        # Keeper: batting + keeping bonus (stumpings/catches valued highly)
        keep_bonus = min(stats.get("stumpings_total", 0) * 2 +
                         stats.get("catches_total", 0), 10)
        score = batting_score * 0.75 + keep_bonus + field_bonus
    elif role == "all_rounder":
        score = batting_score * 0.55 + bowling_score * 0.45 + field_bonus
    else:  # unknown — average both
        score = batting_score * 0.5 + bowling_score * 0.5 + field_bonus * 0.5

    return round(min(score, 100.0), 2)


# ── Week-on-week delta ────────────────────────────────────────────────────────

def compute_wow_delta(
    current: dict, previous: Optional[dict]
) -> dict:
    """
    Returns delta dict: positive = improvement, negative = decline.
    Keys match weekly_snapshots columns.
    """
    if previous is None:
        return {k: None for k in
                ["batting_avg", "batting_sr", "bowling_avg", "bowling_economy",
                 "wickets_total", "runs_total", "form_score"]}

    def delta(key):
        c = current.get(key) or 0
        p = previous.get(key) or 0
        return round(c - p, 2)

    return {
        "batting_avg":     delta("batting_avg"),
        "batting_sr":      delta("batting_sr"),
        "bowling_avg":     -delta("bowling_avg"),    # lower = better
        "bowling_economy": -delta("bowling_economy"),# lower = better
        "wickets_total":   delta("wickets_total"),
        "runs_total":      delta("runs_total"),
        "form_score":      delta("form_score"),
    }


def trend_label(delta_val: Optional[float]) -> str:
    if delta_val is None:
        return "new"
    if delta_val > 2:
        return "up"
    if delta_val < -2:
        return "down"
    return "stable"


# ── Full analysis pipeline ────────────────────────────────────────────────────

def infer_role_from_stats(stats: dict) -> str:
    """
    Infer role from career stats when role is 'unknown'.
    Used to auto-classify players who weren't tagged on CricHeroes.

    Logic:
      - Has both batting innings AND bowling wickets/overs → all_rounder
      - Only bowling data (overs > 0, no batting) → bowler
      - Only batting data (innings > 0, no bowling) → batsman
      - Neither → keep unknown
    """
    has_bat  = (stats.get("innings", 0) or 0) > 0
    has_bowl = (stats.get("wickets_total", 0) or 0) > 0 or (stats.get("bowling_avg", 0) or 0) > 0
    if has_bat and has_bowl:
        return "all_rounder"
    if has_bowl:
        return "bowler"
    if has_bat:
        return "batsman"
    return "unknown"


def compute_weekend_trend(df: pd.DataFrame, player_id: int, fmt: int = 0) -> dict:
    """
    Compute week-by-week (weekend cluster) performance trend for a player.

    Groups matches by weekend (date clusters where consecutive matches are
    within 2 days of each other = same playing session). Computes a form
    score for each weekend. Applies linear regression slope to classify
    overall trajectory as Growing / Declining / Stable.

    Returns:
    {
      "weekends":  [{"dates": [...], "score": float, "runs": int, "wickets": int}, ...],
      "trend":     "growing" | "declining" | "stable" | "insufficient_data",
      "slope":     float,   # per-weekend score change (positive = growing)
      "scores":    [float, ...],  # one per weekend, chronological order
    }
    """
    pdata = df[df["player_id"] == player_id].copy()
    if fmt != 0:
        pdata = pdata[pdata["match_overs"] == fmt]

    if pdata.empty or len(pdata) < 2:
        return {"weekends": [], "trend": "insufficient_data", "slope": 0.0, "scores": []}

    pdata = pdata.sort_values("match_date", ascending=True)
    pdata["match_date"] = pd.to_datetime(pdata["match_date"], errors="coerce")
    pdata = pdata.dropna(subset=["match_date"])

    if len(pdata) < 2:
        return {"weekends": [], "trend": "insufficient_data", "slope": 0.0, "scores": []}

    # ── Cluster matches into weekends ─────────────────────────────────────────
    # A "weekend" is a cluster of consecutive match dates within ≤2 days of each other.
    clusters: list[list] = []
    current: list        = [pdata.iloc[0]]
    for _, row in pdata.iloc[1:].iterrows():
        delta = (row["match_date"] - current[-1]["match_date"]).days
        if delta <= 2:
            current.append(row)
        else:
            clusters.append(current)
            current = [row]
    clusters.append(current)

    if len(clusters) < 2:
        return {"weekends": [], "trend": "insufficient_data", "slope": 0.0, "scores": []}

    # ── Compute a simple form score per weekend ───────────────────────────────
    # Score = weighted contribution of runs and wickets normalised to 0-100
    from config import BAT_AVG_BENCHMARK, BAT_SR_BENCHMARK, BOWL_ECON_GOOD, BOWL_ECON_BAD
    weekend_data = []
    for cluster in clusters:
        rows    = pd.DataFrame(cluster)
        dates   = sorted(str(r["match_date"])[:10] for _, r in rows.iterrows())
        bat_r   = rows[rows["balls_faced"] > 0]
        bowl_r  = rows[rows["overs_bowled"] > 0]

        runs_    = int(bat_r["runs"].sum())  if not bat_r.empty  else 0
        dismi_   = int(bat_r["is_out"].sum()) if not bat_r.empty else 0
        wkts_    = int(bowl_r["wickets"].sum()) if not bowl_r.empty else 0
        runs_c_  = int(bowl_r["runs_conceded"].sum()) if not bowl_r.empty else 0
        ov_      = float(bowl_r["overs_bowled"].sum()) if not bowl_r.empty else 0

        bat_avg  = runs_ / max(dismi_, 1)
        bat_sc   = min(bat_avg / BAT_AVG_BENCHMARK, 1.0) * 100

        econ_    = runs_c_ / max(ov_, 0.001)
        econ_range = BOWL_ECON_BAD - BOWL_ECON_GOOD
        bowl_sc  = max(0, (BOWL_ECON_BAD - econ_) / econ_range) * 100 if ov_ > 0 else 0

        # Blend: if player has both disciplines, weight equally; otherwise single
        if runs_ > 0 and wkts_ > 0:
            score = round(bat_sc * 0.5 + bowl_sc * 0.5, 1)
        elif wkts_ > 0:
            score = round(bowl_sc, 1)
        else:
            score = round(bat_sc, 1)

        weekend_data.append({
            "dates":   dates,
            "score":   score,
            "runs":    runs_,
            "wickets": wkts_,
            "matches": len(cluster),
        })

    scores = [w["score"] for w in weekend_data]

    # ── Linear regression slope ───────────────────────────────────────────────
    x = np.arange(len(scores), dtype=float)
    y = np.array(scores, dtype=float)
    if len(x) >= 2 and y.std() > 0:
        slope = float(np.polyfit(x, y, 1)[0])   # points per weekend
    else:
        slope = 0.0

    # Classify trend
    if slope > 3.0:
        trend = "growing"
    elif slope < -3.0:
        trend = "declining"
    else:
        trend = "stable"

    return {
        "weekends": weekend_data,
        "trend":    trend,
        "slope":    round(slope, 2),
        "scores":   scores,
    }


def analyse_all(df: pd.DataFrame, players: list[dict]) -> list[dict]:
    """
    For every player × every format (+ all-format summary), compute stats
    and composite score.

    Returns list of player analysis dicts, each with:
      { player_id, name, role, formats: { 6: {...}, 10: {...}, 15: {...}, 20: {...}, 0: {...} } }
    """
    results = []
    for p in players:
        pid  = p["id"]
        role = p.get("role", "unknown")

        fmt_stats = {}
        for fmt in [0] + FORMATS:   # 0 = all-format combined
            stats = compute_player_format_stats(df, pid, fmt)
            # Auto-infer role from stats when role is 'unknown'
            if role == "unknown":
                inferred = infer_role_from_stats(stats)
                if inferred != "unknown":
                    role = inferred   # use inferred role for scoring (but don't write to DB here)
            stats["form_score"] = compute_composite_score(stats, role)
            fmt_stats[fmt] = stats

        # Compute weekend trend (all formats)
        wow_trend = compute_weekend_trend(df, pid, fmt=0)

        results.append({
            "player_id": pid,
            "name":      p["name"],
            "role":      role,
            "formats":   fmt_stats,
            # Top-line for sorting
            "overall_score": fmt_stats[0]["form_score"],
            "overall_matches": fmt_stats[0]["matches_played"],
            "wow_trend": wow_trend,
        })

    results.sort(key=lambda x: x["overall_score"], reverse=True)

    # Persist inferred roles back to DB for players who were 'unknown'
    try:
        import db as _db
        for p, r in zip(players, [res for res in results]):
            if p.get("role", "unknown") == "unknown" and r["role"] != "unknown":
                with _db.get_conn() as conn:
                    conn.execute(
                        "UPDATE players SET role=?, updated_at=datetime('now') WHERE id=?",
                        (r["role"], p["id"])
                    )
                log.info(f"[analyzer] inferred role '{r['role']}' for {p['name']}")
    except Exception as e:
        log.debug(f"[analyzer] role persistence skipped: {e}")

    log.info(f"[analyzer] analysed {len(results)} players across {len(FORMATS)} formats")
    return results


# ── Form classification ───────────────────────────────────────────────────────

def classify_form(stats_all_fmt: dict) -> str:
    """
    Classifies recent form vs career baseline using both batting avg and SR
    (ICC standard: both output and strike rate determine batting form).
      'In Form' / 'Average' / 'Out of Form' / 'Insufficient Data'
    """
    recent_avg = stats_all_fmt.get("recent_bat_avg", 0)
    career_avg = stats_all_fmt.get("batting_avg", 0)
    recent_sr  = stats_all_fmt.get("recent_bat_sr", 0)
    career_sr  = stats_all_fmt.get("batting_sr", 0)

    if career_avg == 0:
        return "Insufficient Data"

    avg_ratio = recent_avg / career_avg
    # SR ratio: only use if we have career SR baseline
    sr_ratio  = (recent_sr / career_sr) if career_sr > 0 else 1.0

    # Combined signal: 60% avg, 40% SR (avg is more reliable over small samples)
    combined = avg_ratio * 0.6 + sr_ratio * 0.4

    if combined >= 1.2:
        return "In Form"
    if combined <= 0.6:
        return "Out of Form"
    return "Average"
