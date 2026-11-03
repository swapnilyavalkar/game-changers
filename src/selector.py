from __future__ import annotations

"""
Automatic squad selection.

Algorithm:
  1. Score all players for the target format using composite_score.
  2. Apply role-based constraints (min/max per role from config).
  3. Greedy fill: pick highest-scoring eligible player per role slot,
     then fill remaining slots with highest-scoring remaining players.
  4. Produce Playing 11 and 15-member squad with reasoning.
"""

import logging
from copy import deepcopy
from typing import Literal

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import FORMATS, SQUAD_11, SQUAD_15

log = logging.getLogger(__name__)

SquadSize = Literal[11, 15]

ROLE_ORDER = ["wicket_keeper", "batsman", "all_rounder", "bowler", "unknown"]


def _role_idx(role) -> int:
    """Safe sort key — handles None and any unexpected role string."""
    r = role or "unknown"
    return ROLE_ORDER.index(r) if r in ROLE_ORDER else len(ROLE_ORDER)


def select_squad(
    analysis: list[dict],
    fmt: int,
    size: SquadSize = 11,
) -> dict:
    """
    analysis : output of analyzer.analyse_all()
    fmt      : overs (6/10/15/20) — used to pick the right format score
    size     : 11 or 15

    Returns:
    {
      "format": fmt,
      "size":   size,
      "squad":  [ { player_id, name, role, score, reason } ],
      "bench":  [ ... ],   # next best players not selected
      "warnings": [...]
    }
    """
    constraints = SQUAD_11 if size == 11 else SQUAD_15
    total_slots = size

    # Build flat list: { player_id, name, role, score }
    pool = []
    for p in analysis:
        # Use format-specific stats only when the player has actually played that format.
        # Fall back to all-format (fmt=0) stats if no format-specific data exists, but
        # apply a 20% discount to avoid over-ranking players untested in this format.
        fmt_stats    = p["formats"].get(fmt, {})
        all_stats    = p["formats"].get(0, {})
        has_fmt_data = fmt_stats.get("matches_played", 0) > 0

        if has_fmt_data:
            chosen = fmt_stats
            score  = chosen["form_score"]
        else:
            chosen = all_stats
            score  = round(chosen.get("form_score", 0) * 0.80, 2)  # 20% discount

        pool.append({
            "player_id":       p["player_id"],
            "name":            p["name"],
            "role":            p["role"],
            "score":           score,
            "has_fmt_data":    has_fmt_data,
            "matches_played":  chosen.get("matches_played", 0),
            "batting_avg":     chosen.get("batting_avg", 0),
            "batting_sr":      chosen.get("batting_sr", 0),
            "bowling_economy": chosen.get("bowling_economy", 0),
            "wickets_total":   chosen.get("wickets_total", 0),
            "runs_total":      chosen.get("runs_total", 0),
            "catches_total":   chosen.get("catches_total", 0),
            "dismissals_taken": chosen.get("dismissals_taken", 0),
        })

    pool.sort(key=lambda x: x["score"], reverse=True)

    # Players with zero match data go straight to bench — never fill squad slots
    no_data   = [p for p in pool if p["matches_played"] == 0]
    pool      = [p for p in pool if p["matches_played"] > 0]

    selected  = []
    remaining = list(pool)   # will shrink as players are picked
    warnings  = []

    # Role flexibility: all_rounders can fill bowler OR batsman slots.
    # wicket_keeper is strict (must be a designated keeper).
    _FLEXIBLE = {
        "bowler":  ["bowler",  "all_rounder"],
        "batsman": ["batsman", "all_rounder"],
        "all_rounder": ["all_rounder"],
        "wicket_keeper": ["wicket_keeper"],
    }

    # ── Phase 1: Fill minimum slots per role ──────────────────────────────────
    role_counts = {r: 0 for r in ROLE_ORDER}

    for role, (min_n, max_n) in constraints.items():
        eligible_roles = _FLEXIBLE.get(role, [role])
        eligible = [p for p in remaining if p["role"] in eligible_roles]
        if len(eligible) < min_n:
            # Last resort: unknown-role players
            eligible += [p for p in remaining if p["role"] == "unknown"]
            warnings.append(
                f"Only {len(eligible)} players available for role '{role}' (need {min_n})"
            )
        picks = eligible[:min_n]
        for pick in picks:
            pick = dict(pick)
            pick["reason"] = f"Required {role} (min {min_n})"
            selected.append(pick)
            remaining = [p for p in remaining if p["player_id"] != pick["player_id"]]
            role_counts[role] += 1

    # ── Phase 2: Fill remaining slots respecting max constraints ─────────────
    slots_left = total_slots - len(selected)
    for player in remaining:
        if slots_left <= 0:
            break
        role = player["role"]
        _, max_n = constraints.get(role, (0, total_slots))
        if role_counts.get(role, 0) >= max_n:
            continue
        p = dict(player)
        p["reason"] = f"Best available {role}"
        selected.append(p)
        role_counts[role] = role_counts.get(role, 0) + 1
        remaining = [p2 for p2 in remaining if p2["player_id"] != p["player_id"]]
        slots_left -= 1

    # ── Phase 3: If still short, ignore role caps and take best remaining ─────
    slots_left = total_slots - len(selected)
    if slots_left > 0:
        warnings.append(f"Short {slots_left} player(s) — filling without role constraints")
        for player in remaining[:slots_left]:
            p = dict(player)
            p["reason"] = "Best available (role cap override)"
            selected.append(p)
            remaining = [p2 for p2 in remaining if p2["player_id"] != p["player_id"]]

    # Sort final squad: WK first, then bat, all-rounder, bowler
    selected.sort(key=lambda x: (_role_idx(x.get("role")), -x["score"]))

    # Bench: highest-scoring unselected players with data, then 0-data players
    for p in no_data:
        p = dict(p)
        p["reason"] = "No match data available"
    bench_with_data = sorted(remaining, key=lambda x: x["score"], reverse=True)
    bench = (bench_with_data + no_data)[:5]

    log.info(
        f"[selector] {size}-member squad for {fmt}-over format: "
        f"{[p['name'] for p in selected]}"
    )

    return {
        "format":    fmt,
        "size":      size,
        "squad":     selected,
        "bench":     bench,
        "warnings":  warnings,
        "role_summary": {r: role_counts.get(r, 0) for r in ROLE_ORDER if r != "unknown"},
    }


def select_all_formats(analysis: list[dict]) -> dict:
    """
    Returns selections for all formats × both squad sizes.
    {
      6:  { 11: {...}, 15: {...} },
      10: { 11: {...}, 15: {...} },
      15: { 11: {...}, 15: {...} },
      20: { 11: {...}, 15: {...} },
    }
    """
    results = {}
    for fmt in FORMATS:
        results[fmt] = {
            11: select_squad(analysis, fmt, size=11),
            15: select_squad(analysis, fmt, size=15),
        }
    return results
