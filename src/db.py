from __future__ import annotations

"""
SQLite database layer — schema creation and CRUD helpers.
"""
import sqlite3
import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from config import DB_PATH, BLOCKED_PLAYER_IDS


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(drop_all: bool = False):
    """
    Initialise (or migrate) the database.

    drop_all=True: DROP all tables first — only called via `--force-db-delete`
                   CLI flag. NEVER called automatically; requires explicit user opt-in.
    """
    if drop_all:
        with get_conn() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS batter_vs_bowler;
                DROP TABLE IF EXISTS dismissals;
                DROP TABLE IF EXISTS opponent_bowling_stats;
                DROP TABLE IF EXISTS weekly_snapshots;
                DROP TABLE IF EXISTS player_match_stats;
                DROP TABLE IF EXISTS matches;
                DROP TABLE IF EXISTS players;
            """)

    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id              INTEGER PRIMARY KEY,
            cricheroes_id   TEXT    UNIQUE NOT NULL,
            name            TEXT    NOT NULL,
            role            TEXT    DEFAULT 'unknown',   -- batsman/bowler/all_rounder/wicket_keeper
            batting_style   TEXT,
            bowling_style   TEXT,
            profile_url     TEXT,
            photo_url       TEXT,
            is_game_changers    INTEGER DEFAULT 0,           -- 1 = registered Game Changers member, 0 = opponent
            created_at      TEXT    DEFAULT (datetime('now')),
            updated_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS matches (
            id              INTEGER PRIMARY KEY,
            cricheroes_match_id TEXT UNIQUE NOT NULL,
            match_date      TEXT,
            opponent        TEXT,
            overs           INTEGER,   -- 6 / 10 / 15 / 20
            result          TEXT,      -- win / loss / draw / no_result
            venue           TEXT,
            tournament      TEXT,
            scorecard_raw   TEXT,      -- JSON blob of raw scorecard
            is_game_changers_match INTEGER DEFAULT 1,  -- 1=Game Changers team match, 0=cross-team player history
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS player_match_stats (
            id              INTEGER PRIMARY KEY,
            player_id       INTEGER NOT NULL REFERENCES players(id),
            match_id        INTEGER NOT NULL REFERENCES matches(id),
            -- Batting
            runs            INTEGER DEFAULT 0,
            balls_faced     INTEGER DEFAULT 0,
            fours           INTEGER DEFAULT 0,
            sixes           INTEGER DEFAULT 0,
            batting_sr      REAL    DEFAULT 0,
            is_out          INTEGER DEFAULT 0,  -- 0/1
            batting_position INTEGER,
            yet_to_bat      INTEGER DEFAULT 0,  -- 1 if player was in team but did not bat (DNB)
            -- Bowling
            overs_bowled    REAL    DEFAULT 0,
            wickets         INTEGER DEFAULT 0,
            runs_conceded   INTEGER DEFAULT 0,
            economy         REAL    DEFAULT 0,
            maidens         INTEGER DEFAULT 0,
            did_not_bowl    INTEGER DEFAULT 0,  -- 1 if player was in team but did not bowl
            -- Bowling discipline (Phase 1)
            wides               INTEGER DEFAULT 0,
            no_balls            INTEGER DEFAULT 0,
            boundaries_conceded INTEGER DEFAULT 0,  -- 4s+6s conceded while bowling
            -- Fielding
            catches         INTEGER DEFAULT 0,
            run_outs        INTEGER DEFAULT 0,
            stumpings       INTEGER DEFAULT 0,
            -- Bowling extras
            dot_balls       INTEGER DEFAULT 0,
            -- Phase 4: over assignment (JSON list of over numbers bowled, e.g. "[1,3,5]")
            over_assignments TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            UNIQUE(player_id, match_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_snapshots (
            id              INTEGER PRIMARY KEY,
            player_id       INTEGER NOT NULL REFERENCES players(id),
            snapshot_date   TEXT    NOT NULL,
            format_overs    INTEGER NOT NULL,   -- 6/10/15/20 or 0 = all formats
            matches_played  INTEGER DEFAULT 0,
            -- Batting
            batting_avg     REAL,
            batting_sr      REAL,
            runs_total      INTEGER DEFAULT 0,
            highest_score   INTEGER DEFAULT 0,
            -- Bowling
            bowling_avg     REAL,
            bowling_economy REAL,
            wickets_total   INTEGER DEFAULT 0,
            -- Form
            form_score      REAL,   -- composite 0-100
            created_at      TEXT    DEFAULT (datetime('now')),
            UNIQUE(player_id, snapshot_date, format_overs)
        );

        -- Dismissal head-to-head (Phase 2 Strategy)
        -- Records every dismissal event extracted from scorecard how_to_out text.
        CREATE TABLE IF NOT EXISTS dismissals (
            id              INTEGER PRIMARY KEY,
            match_id        INTEGER NOT NULL REFERENCES matches(id),
            inning          INTEGER NOT NULL,  -- 1 or 2
            -- Dismissed batter
            batter_name     TEXT    NOT NULL,
            batter_ch_id    TEXT,              -- cricheroes player_id (may be NULL for opponents)
            batter_player_id INTEGER,          -- FK to players table (NULL for opponent batters)
            -- Bowler
            bowler_name     TEXT    NOT NULL,
            bowler_ch_id    TEXT,
            bowler_player_id INTEGER,          -- FK to players table (NULL for opponent bowlers)
            -- Fielder (for caught/stumped)
            fielder_name    TEXT,
            fielder_ch_id   TEXT,
            fielder_player_id INTEGER,
            -- Dismissal type
            dismissal_type  TEXT,              -- bowled/caught/lbw/run_out/stumped/caught_behind/other
            how_to_out_raw  TEXT,              -- raw string e.g. "c Fielder b Bowler"
            -- Which team was batting (context)
            batting_team_id TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            UNIQUE(match_id, inning, batter_name, bowler_name)
        );

        -- Ball-by-ball batter vs bowler aggregates (from commentary API)
        -- One row per (match × our batter × opponent bowler) pair.
        CREATE TABLE IF NOT EXISTS batter_vs_bowler (
            id                INTEGER PRIMARY KEY,
            match_id          INTEGER NOT NULL REFERENCES matches(id),
            batter_player_id  INTEGER REFERENCES players(id),
            batter_name       TEXT    NOT NULL,
            batter_ch_id      TEXT,
            bowler_player_id  INTEGER REFERENCES players(id),  -- NULL (opponent)
            bowler_name       TEXT    NOT NULL,
            bowler_ch_id      TEXT,
            runs_scored       INTEGER DEFAULT 0,
            balls_faced       INTEGER DEFAULT 0,
            fours             INTEGER DEFAULT 0,
            sixes             INTEGER DEFAULT 0,
            dismissals        INTEGER DEFAULT 0,
            dot_balls         INTEGER DEFAULT 0,
            created_at        TEXT    DEFAULT (datetime('now')),
            UNIQUE(match_id, batter_name, bowler_name)
        );

        -- Opponent bowling stats per match against Game Changers
        -- One row per opponent bowler per match (from our batting inning).
        CREATE TABLE IF NOT EXISTS opponent_bowling_stats (
            id              INTEGER PRIMARY KEY,
            match_id        INTEGER NOT NULL REFERENCES matches(id),
            bowler_name     TEXT    NOT NULL,
            bowler_ch_id    TEXT,
            overs           REAL    DEFAULT 0,
            runs_conceded   INTEGER DEFAULT 0,
            wickets         INTEGER DEFAULT 0,
            wides           INTEGER DEFAULT 0,
            no_balls        INTEGER DEFAULT 0,
            dot_balls       INTEGER DEFAULT 0,
            boundaries      INTEGER DEFAULT 0,
            economy_rate    REAL    DEFAULT 0,
            created_at      TEXT    DEFAULT (datetime('now')),
            UNIQUE(match_id, bowler_name)
        );
        """)
    # ── Migrations: add columns that may not exist in older DBs ──────────────
    _pms_new_cols = {
        "dot_balls":            "INTEGER DEFAULT 0",
        "yet_to_bat":           "INTEGER DEFAULT 0",
        "did_not_bowl":         "INTEGER DEFAULT 0",
        "wides":                "INTEGER DEFAULT 0",
        "no_balls":             "INTEGER DEFAULT 0",
        "boundaries_conceded":  "INTEGER DEFAULT 0",
        "over_assignments":     "TEXT",
    }
    with get_conn() as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(player_match_stats)").fetchall()}
        for col, typedef in _pms_new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE player_match_stats ADD COLUMN {col} {typedef}")

    # ── Migrate matches table ─────────────────────────────────────────────────
    with get_conn() as conn:
        existing_m = {r[1] for r in conn.execute("PRAGMA table_info(matches)").fetchall()}
        if "is_game_changers_match" not in existing_m:
            conn.execute("ALTER TABLE matches ADD COLUMN is_game_changers_match INTEGER DEFAULT 1")
            conn.execute("UPDATE matches SET is_game_changers_match = 1 WHERE is_game_changers_match IS NULL")

    # ── Migrate players table ─────────────────────────────────────────────────
    with get_conn() as conn:
        existing_p = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
        if "is_game_changers" not in existing_p:
            conn.execute("ALTER TABLE players ADD COLUMN is_game_changers INTEGER DEFAULT 0")

    print(f"[db] Initialised at {DB_PATH}")


# ── Player helpers ─────────────────────────────────────────────────────────────

def upsert_player(data: dict) -> int:
    if str(data.get("cricheroes_id", "")) in BLOCKED_PLAYER_IDS:
        return 0   # silently skip blocked players
    sql = """
    INSERT INTO players (cricheroes_id, name, role, batting_style, bowling_style, profile_url, photo_url, is_game_changers, updated_at)
    VALUES (:cricheroes_id, :name, :role, :batting_style, :bowling_style, :profile_url, :photo_url, :is_game_changers, datetime('now'))
    ON CONFLICT(cricheroes_id) DO UPDATE SET
        name          = excluded.name,
        role          = CASE
                          WHEN players.role IS NULL OR players.role = '' OR players.role = 'unknown'
                          THEN excluded.role
                          ELSE players.role
                        END,
        batting_style = COALESCE(NULLIF(excluded.batting_style, ''), players.batting_style),
        bowling_style = COALESCE(NULLIF(excluded.bowling_style, ''), players.bowling_style),
        profile_url   = COALESCE(NULLIF(excluded.profile_url, ''),   players.profile_url),
        photo_url     = COALESCE(NULLIF(excluded.photo_url, ''),     players.photo_url),
        is_game_changers  = MAX(players.is_game_changers, excluded.is_game_changers),
        updated_at    = datetime('now')
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "cricheroes_id": str(data["cricheroes_id"]),
            "name":          data.get("name", "Unknown"),
            "role":          data.get("role", "unknown"),
            "batting_style": data.get("batting_style"),
            "bowling_style": data.get("bowling_style"),
            "profile_url":   data.get("profile_url"),
            "photo_url":     data.get("photo_url"),
            "is_game_changers":  int(data.get("is_game_changers", 0)),
        })
        row = conn.execute(
            "SELECT id FROM players WHERE cricheroes_id = ?",
            (str(data["cricheroes_id"]),)
        ).fetchone()
    return row["id"]


def get_all_players() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM players ORDER BY name").fetchall()
    return [p for p in (dict(r) for r in rows) if str(p.get("cricheroes_id", "")) not in BLOCKED_PLAYER_IDS]


def get_game_changers_players() -> list[dict]:
    """Return only registered Game Changers team members (is_game_changers = 1)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM players WHERE is_game_changers = 1 ORDER BY name"
        ).fetchall()
        if not rows:
            rows = conn.execute("SELECT * FROM players ORDER BY name").fetchall()
    return [p for p in (dict(r) for r in rows) if str(p.get("cricheroes_id", "")) not in BLOCKED_PLAYER_IDS]


# ── Match helpers ──────────────────────────────────────────────────────────────

def upsert_match(data: dict) -> int:
    sql = """
    INSERT INTO matches (cricheroes_match_id, match_date, opponent, overs, result, venue, tournament,
                         scorecard_raw, is_game_changers_match)
    VALUES (:cricheroes_match_id, :match_date, :opponent, :overs, :result, :venue, :tournament,
            :scorecard_raw, :is_game_changers_match)
    ON CONFLICT(cricheroes_match_id) DO UPDATE SET
        match_date         = COALESCE(NULLIF(excluded.match_date, ''), matches.match_date),
        opponent           = COALESCE(NULLIF(excluded.opponent, ''), matches.opponent),
        overs              = COALESCE(NULLIF(excluded.overs, 0), matches.overs),
        result             = COALESCE(NULLIF(excluded.result, ''), matches.result),
        venue              = excluded.venue,
        tournament         = excluded.tournament,
        scorecard_raw      = excluded.scorecard_raw,
        is_game_changers_match = MAX(matches.is_game_changers_match, excluded.is_game_changers_match)
    """
    raw = data.get("scorecard_raw")
    with get_conn() as conn:
        conn.execute(sql, {
            "cricheroes_match_id": str(data["cricheroes_match_id"]),
            "match_date":        data.get("match_date"),
            "opponent":          data.get("opponent"),
            "overs":             data.get("overs"),
            "result":            data.get("result"),
            "venue":             data.get("venue"),
            "tournament":        data.get("tournament"),
            "scorecard_raw":     json.dumps(raw) if raw and not isinstance(raw, str) else raw,
            "is_game_changers_match": int(data.get("is_game_changers_match", 1)),
        })
        row = conn.execute(
            "SELECT id FROM matches WHERE cricheroes_match_id = ?",
            (str(data["cricheroes_match_id"]),)
        ).fetchone()
    return row["id"]


def get_all_matches() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM matches ORDER BY match_date DESC").fetchall()
    return [dict(r) for r in rows]


# ── Player match stats helpers ─────────────────────────────────────────────────

def upsert_player_match_stats(data: dict):
    sql = """
    INSERT INTO player_match_stats
        (player_id, match_id, runs, balls_faced, fours, sixes, batting_sr, is_out,
         batting_position, yet_to_bat,
         overs_bowled, wickets, runs_conceded, economy, maidens, did_not_bowl,
         wides, no_balls, boundaries_conceded,
         catches, run_outs, stumpings, dot_balls, over_assignments)
    VALUES
        (:player_id, :match_id, :runs, :balls_faced, :fours, :sixes, :batting_sr, :is_out,
         :batting_position, :yet_to_bat,
         :overs_bowled, :wickets, :runs_conceded, :economy, :maidens, :did_not_bowl,
         :wides, :no_balls, :boundaries_conceded,
         :catches, :run_outs, :stumpings, :dot_balls, :over_assignments)
    ON CONFLICT(player_id, match_id) DO UPDATE SET
        runs                 = excluded.runs,
        balls_faced          = excluded.balls_faced,
        fours                = excluded.fours,
        sixes                = excluded.sixes,
        batting_sr           = excluded.batting_sr,
        is_out               = excluded.is_out,
        batting_position     = excluded.batting_position,
        yet_to_bat           = excluded.yet_to_bat,
        overs_bowled         = excluded.overs_bowled,
        wickets              = excluded.wickets,
        runs_conceded        = excluded.runs_conceded,
        economy              = excluded.economy,
        maidens              = excluded.maidens,
        did_not_bowl         = excluded.did_not_bowl,
        wides                = excluded.wides,
        no_balls             = excluded.no_balls,
        boundaries_conceded  = excluded.boundaries_conceded,
        catches              = excluded.catches,
        run_outs             = excluded.run_outs,
        stumpings            = excluded.stumpings,
        dot_balls            = excluded.dot_balls,
        over_assignments     = excluded.over_assignments
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "player_id":            data["player_id"],
            "match_id":             data["match_id"],
            "runs":                 data.get("runs", 0),
            "balls_faced":          data.get("balls_faced", 0),
            "fours":                data.get("fours", 0),
            "sixes":                data.get("sixes", 0),
            "batting_sr":           data.get("batting_sr", 0),
            "is_out":               int(data.get("is_out", False)),
            "batting_position":     data.get("batting_position"),
            "yet_to_bat":           int(data.get("yet_to_bat", False)),
            "overs_bowled":         data.get("overs_bowled", 0),
            "wickets":              data.get("wickets", 0),
            "runs_conceded":        data.get("runs_conceded", 0),
            "economy":              data.get("economy", 0),
            "maidens":              data.get("maidens", 0),
            "did_not_bowl":         int(data.get("did_not_bowl", False)),
            "wides":                data.get("wides", 0),
            "no_balls":             data.get("no_balls", 0),
            "boundaries_conceded":  data.get("boundaries_conceded", 0),
            "catches":              data.get("catches", 0),
            "run_outs":             data.get("run_outs", 0),
            "stumpings":            data.get("stumpings", 0),
            "dot_balls":            data.get("dot_balls", 0),
            "over_assignments":     data.get("over_assignments"),
        })


def get_player_stats_match_ids(player_id: int) -> set:
    """Return set of cricheroes_match_ids that this player already has stats for."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.cricheroes_match_id FROM player_match_stats pms "
            "JOIN matches m ON m.id = pms.match_id WHERE pms.player_id = ?",
            (player_id,)
        ).fetchall()
    return {r[0] for r in rows}


def get_player_stats_df(team_only: bool = True):
    """Return joined player+match+stats as a pandas DataFrame.

    team_only=True (default) keeps only our two teams' matches. Cross-team rows (games a squad
    member played for other teams) stay in the database but are left out of every dashboard
    total. Pass team_only=False to include them.
    """
    import pandas as pd
    sql = """
    SELECT
        p.id            AS player_id,
        p.cricheroes_id,
        p.name,
        p.role,
        m.id            AS match_id,
        m.cricheroes_match_id,
        m.match_date,
        m.overs         AS match_overs,
        m.result,
        m.opponent,
        m.is_game_changers_match,
        s.runs, s.balls_faced, s.fours, s.sixes,
        s.batting_sr, s.is_out, s.batting_position,
        s.overs_bowled, s.wickets, s.runs_conceded,
        s.economy, s.maidens,
        s.yet_to_bat, s.did_not_bowl,
        s.wides, s.no_balls, s.boundaries_conceded,
        s.catches, s.run_outs, s.stumpings, s.dot_balls, s.over_assignments
    FROM player_match_stats s
    JOIN players p ON p.id = s.player_id
    JOIN matches m ON m.id = s.match_id
    {where}
    ORDER BY m.match_date DESC, CAST(m.cricheroes_match_id AS INTEGER) DESC
    """.format(where="WHERE m.is_game_changers_match = 1" if team_only else "")
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


# ── Snapshot helpers ───────────────────────────────────────────────────────────

def save_snapshot(data: dict):
    sql = """
    INSERT INTO weekly_snapshots
        (player_id, snapshot_date, format_overs, matches_played,
         batting_avg, batting_sr, runs_total, highest_score,
         bowling_avg, bowling_economy, wickets_total, form_score)
    VALUES
        (:player_id, :snapshot_date, :format_overs, :matches_played,
         :batting_avg, :batting_sr, :runs_total, :highest_score,
         :bowling_avg, :bowling_economy, :wickets_total, :form_score)
    ON CONFLICT(player_id, snapshot_date, format_overs) DO UPDATE SET
        matches_played  = excluded.matches_played,
        batting_avg     = excluded.batting_avg,
        batting_sr      = excluded.batting_sr,
        runs_total      = excluded.runs_total,
        highest_score   = excluded.highest_score,
        bowling_avg     = excluded.bowling_avg,
        bowling_economy = excluded.bowling_economy,
        wickets_total   = excluded.wickets_total,
        form_score      = excluded.form_score
    """
    with get_conn() as conn:
        conn.execute(sql, data)


def get_snapshots_for_player(player_id: int, format_overs: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM weekly_snapshots WHERE player_id=? AND format_overs=? ORDER BY snapshot_date DESC",
            (player_id, format_overs)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Dismissal helpers ──────────────────────────────────────────────────────────

def upsert_dismissal(data: dict):
    sql = """
    INSERT INTO dismissals
        (match_id, inning, batter_name, batter_ch_id, batter_player_id,
         bowler_name, bowler_ch_id, bowler_player_id,
         fielder_name, fielder_ch_id, fielder_player_id,
         dismissal_type, how_to_out_raw, batting_team_id)
    VALUES
        (:match_id, :inning, :batter_name, :batter_ch_id, :batter_player_id,
         :bowler_name, :bowler_ch_id, :bowler_player_id,
         :fielder_name, :fielder_ch_id, :fielder_player_id,
         :dismissal_type, :how_to_out_raw, :batting_team_id)
    ON CONFLICT(match_id, inning, batter_name, bowler_name) DO UPDATE SET
        dismissal_type   = excluded.dismissal_type,
        how_to_out_raw   = excluded.how_to_out_raw,
        fielder_name     = excluded.fielder_name,
        batter_ch_id     = excluded.batter_ch_id,
        bowler_ch_id     = excluded.bowler_ch_id
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "match_id":         data["match_id"],
            "inning":           data.get("inning", 1),
            "batter_name":      data.get("batter_name", ""),
            "batter_ch_id":     data.get("batter_ch_id"),
            "batter_player_id": data.get("batter_player_id"),
            "bowler_name":      data.get("bowler_name", ""),
            "bowler_ch_id":     data.get("bowler_ch_id"),
            "bowler_player_id": data.get("bowler_player_id"),
            "fielder_name":     data.get("fielder_name"),
            "fielder_ch_id":    data.get("fielder_ch_id"),
            "fielder_player_id": data.get("fielder_player_id"),
            "dismissal_type":   data.get("dismissal_type", "other"),
            "how_to_out_raw":   data.get("how_to_out_raw", ""),
            "batting_team_id":  data.get("batting_team_id"),
        })


def get_dismissals_for_strategy(our_player_ids: set) -> list[dict]:
    """Return all dismissal records involving our players (as bowler or batter)."""
    if not our_player_ids:
        return []
    placeholders = ",".join("?" * len(our_player_ids))
    sql = f"""
    SELECT d.*, m.match_date, m.opponent, m.overs AS match_overs, m.result, m.tournament
    FROM dismissals d
    JOIN matches m ON m.id = d.match_id
    WHERE d.bowler_player_id IN ({placeholders})
       OR d.batter_player_id IN ({placeholders})
    ORDER BY m.match_date DESC
    """
    ids = list(our_player_ids)
    with get_conn() as conn:
        rows = conn.execute(sql, ids + ids).fetchall()
    return [dict(r) for r in rows]


# ── Opponent bowling stats helpers ─────────────────────────────────────────────

def upsert_opponent_bowling_stat(data: dict):
    sql = """
    INSERT INTO opponent_bowling_stats
        (match_id, bowler_name, bowler_ch_id,
         overs, runs_conceded, wickets, wides, no_balls, dot_balls, boundaries, economy_rate)
    VALUES
        (:match_id, :bowler_name, :bowler_ch_id,
         :overs, :runs_conceded, :wickets, :wides, :no_balls, :dot_balls, :boundaries, :economy_rate)
    ON CONFLICT(match_id, bowler_name) DO UPDATE SET
        overs         = excluded.overs,
        runs_conceded = excluded.runs_conceded,
        wickets       = excluded.wickets,
        wides         = excluded.wides,
        no_balls      = excluded.no_balls,
        dot_balls     = excluded.dot_balls,
        boundaries    = excluded.boundaries,
        economy_rate  = excluded.economy_rate
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "match_id":     data["match_id"],
            "bowler_name":  data.get("bowler_name", ""),
            "bowler_ch_id": data.get("bowler_ch_id"),
            "overs":        data.get("overs", 0),
            "runs_conceded": data.get("runs_conceded", 0),
            "wickets":      data.get("wickets", 0),
            "wides":        data.get("wides", 0),
            "no_balls":     data.get("no_balls", 0),
            "dot_balls":    data.get("dot_balls", 0),
            "boundaries":   data.get("boundaries", 0),
            "economy_rate": data.get("economy_rate", 0),
        })


def get_strategy_data() -> dict:
    """
    Return all data needed for the Strategy tab:
    - per_opponent: aggregate stats for each opponent team
    - dismissals_by_our_bowler: our bowlers' head-to-head dismissal records
    - dismissals_of_our_batters: opponent bowlers who dismissed our batters
    - opponent_bowler_stats: per-match figures of opponent bowlers vs us
    """
    with get_conn() as conn:
        # Per-opponent win/loss/runs/wickets summary — Game Changers matches only.
        # CTE aggregates at match level first to avoid N-fold inflation from player rows.
        per_opponent = conn.execute("""
            WITH match_summary AS (
                SELECT
                    m.id,
                    m.opponent,
                    m.result,
                    SUM(CASE WHEN s.batting_position IS NOT NULL THEN s.runs ELSE 0 END) AS match_runs,
                    SUM(COALESCE(s.wickets, 0))                                          AS match_wickets,
                    AVG(CASE WHEN s.overs_bowled > 0 THEN s.economy ELSE NULL END)       AS match_economy
                FROM matches m
                JOIN player_match_stats s ON s.match_id = m.id
                JOIN players sp ON sp.id = s.player_id AND sp.is_game_changers = 1
                WHERE m.is_game_changers_match = 1
                GROUP BY m.id, m.opponent, m.result
            )
            SELECT
                opponent,
                COUNT(*)                                           AS matches,
                SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END)  AS wins,
                SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END)  AS losses,
                SUM(match_runs)                                    AS total_runs_scored,
                SUM(match_wickets)                                 AS total_wickets_taken,
                AVG(match_economy)                                 AS avg_economy
            FROM match_summary
            GROUP BY opponent
            ORDER BY matches DESC
        """).fetchall()

        # Per-match raw rows for client-side format/lastN filtering
        per_opponent_matches = conn.execute("""
            SELECT
                m.id AS match_id,
                m.cricheroes_match_id,
                m.match_date,
                m.opponent,
                m.result,
                m.overs AS match_overs,
                SUM(CASE WHEN s.batting_position IS NOT NULL THEN s.runs ELSE 0 END) AS match_runs,
                SUM(COALESCE(s.wickets, 0)) AS match_wickets
            FROM matches m
            JOIN player_match_stats s ON s.match_id = m.id
            JOIN players sp ON sp.id = s.player_id AND sp.is_game_changers = 1
            WHERE m.is_game_changers_match = 1
            GROUP BY m.id, m.cricheroes_match_id, m.match_date, m.opponent, m.result, m.overs
            ORDER BY m.match_date DESC
        """).fetchall()

        # Dismissals by our bowlers (opponent batters dismissed) — Game Changers matches only
        dismissals_by_bowler = conn.execute("""
            SELECT
                d.bowler_name, d.bowler_player_id,
                d.batter_name, d.dismissal_type,
                m.opponent, m.match_date, m.overs AS match_overs, m.id AS match_id
            FROM dismissals d
            JOIN matches m ON m.id = d.match_id
            WHERE d.bowler_player_id IS NOT NULL
              AND m.is_game_changers_match = 1
            ORDER BY m.match_date DESC
        """).fetchall()

        # Dismissals of our batters (opponent bowlers who got our players out) — Game Changers only
        dismissals_of_batters = conn.execute("""
            SELECT
                d.bowler_name, d.bowler_ch_id,
                d.batter_name, d.batter_player_id, d.dismissal_type,
                m.opponent, m.match_date, m.overs AS match_overs, m.id AS match_id
            FROM dismissals d
            JOIN matches m ON m.id = d.match_id
            WHERE d.batter_player_id IS NOT NULL
              AND m.is_game_changers_match = 1
            ORDER BY m.match_date DESC
        """).fetchall()

        # Opponent bowling stats vs us — Game Changers matches only
        opp_bowling = conn.execute("""
            SELECT obs.*, m.cricheroes_match_id, m.opponent, m.match_date, m.overs AS match_overs, m.result
            FROM opponent_bowling_stats obs
            JOIN matches m ON m.id = obs.match_id
            WHERE m.is_game_changers_match = 1
            ORDER BY m.match_date DESC
        """).fetchall()

    return {
        "per_opponent":            [dict(r) for r in per_opponent],
        "per_opponent_matches":    [dict(r) for r in per_opponent_matches],
        "dismissals_by_bowler":    [dict(r) for r in dismissals_by_bowler],
        "dismissals_of_batters":   [dict(r) for r in dismissals_of_batters],
        "opponent_bowler_stats":   [dict(r) for r in opp_bowling],
    }


# ── Batter vs bowler helpers ───────────────────────────────────────────────────

def upsert_batter_vs_bowler(data: dict):
    """
    Upsert one (match, batter, bowler) aggregate row.
    Increments existing stats if a row already exists (from a previous partial fetch).
    """
    sql = """
    INSERT INTO batter_vs_bowler
        (match_id, batter_player_id, batter_name, batter_ch_id,
         bowler_player_id, bowler_name, bowler_ch_id,
         runs_scored, balls_faced, fours, sixes, dismissals, dot_balls)
    VALUES
        (:match_id, :batter_player_id, :batter_name, :batter_ch_id,
         :bowler_player_id, :bowler_name, :bowler_ch_id,
         :runs_scored, :balls_faced, :fours, :sixes, :dismissals, :dot_balls)
    ON CONFLICT(match_id, batter_name, bowler_name) DO UPDATE SET
        runs_scored      = excluded.runs_scored,
        balls_faced      = excluded.balls_faced,
        fours            = excluded.fours,
        sixes            = excluded.sixes,
        dismissals       = excluded.dismissals,
        dot_balls        = excluded.dot_balls,
        batter_player_id = COALESCE(excluded.batter_player_id, batter_vs_bowler.batter_player_id),
        batter_ch_id     = COALESCE(excluded.batter_ch_id, batter_vs_bowler.batter_ch_id),
        bowler_ch_id     = COALESCE(excluded.bowler_ch_id, batter_vs_bowler.bowler_ch_id)
    """
    with get_conn() as conn:
        conn.execute(sql, {
            "match_id":          data["match_id"],
            "batter_player_id":  data.get("batter_player_id"),
            "batter_name":       data.get("batter_name", ""),
            "batter_ch_id":      data.get("batter_ch_id"),
            "bowler_player_id":  data.get("bowler_player_id"),
            "bowler_name":       data.get("bowler_name", ""),
            "bowler_ch_id":      data.get("bowler_ch_id"),
            "runs_scored":       data.get("runs_scored", 0),
            "balls_faced":       data.get("balls_faced", 0),
            "fours":             data.get("fours", 0),
            "sixes":             data.get("sixes", 0),
            "dismissals":        data.get("dismissals", 0),
            "dot_balls":         data.get("dot_balls", 0),
        })


def get_batter_vs_bowler_stats() -> list[dict]:
    """
    Return aggregated batter-vs-bowler stats (only our batters who have a player_id).
    Grouped by (batter, bowler) across Game Changers matches only.
    """
    sql = """
    SELECT
        bvb.batter_name,
        bvb.batter_player_id,
        bvb.bowler_name,
        bvb.bowler_ch_id,
        COUNT(DISTINCT bvb.match_id)             AS matches,
        SUM(bvb.runs_scored)                     AS runs,
        SUM(bvb.balls_faced)                     AS balls,
        SUM(bvb.fours)                           AS fours,
        SUM(bvb.sixes)                           AS sixes,
        SUM(bvb.dismissals)                      AS dismissals,
        SUM(bvb.dot_balls)                       AS dot_balls,
        CASE WHEN SUM(bvb.balls_faced) > 0
             THEN ROUND(SUM(bvb.runs_scored) * 100.0 / SUM(bvb.balls_faced), 1)
             ELSE 0 END                          AS strike_rate
    FROM batter_vs_bowler bvb
    JOIN matches m ON m.id = bvb.match_id
    WHERE bvb.batter_player_id IS NOT NULL
      AND m.is_game_changers_match = 1
    GROUP BY bvb.batter_player_id, bvb.bowler_name
    ORDER BY bvb.batter_name, runs DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_batter_vs_bowler_rows() -> list[dict]:
    """One row per (match, batter, bowler) for our batters, with the match date.

    The aggregated view above cannot be filtered by date; the dashboard rebuilds it from these rows.
    """
    sql = """
    SELECT
        m.cricheroes_match_id AS match_id,
        m.match_date,
        bvb.batter_player_id,
        bvb.bowler_name,
        bvb.bowler_ch_id,
        bvb.runs_scored AS runs,
        bvb.balls_faced AS balls,
        bvb.fours, bvb.sixes, bvb.dismissals, bvb.dot_balls
    FROM batter_vs_bowler bvb
    JOIN matches m ON m.id = bvb.match_id
    WHERE bvb.batter_player_id IS NOT NULL
      AND m.is_game_changers_match = 1
    ORDER BY m.match_date DESC, m.cricheroes_match_id DESC, bvb.bowler_name
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]
