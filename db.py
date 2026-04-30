"""
db.py — SQLite database layer for horse-analyst.

Schema is additive: new scrapes append rows, never update.
All "current" reads use ORDER BY scraped_at DESC LIMIT 1.

Schema version history:
  v1 — initial
  v2 — added win_odds_mdp, official_rating, speed_rating, form_score cols to runners
  v3 — added trainer_stats, jockey_stats, trainer_form_ids, jockey_form_ids
  v4 — added backtest_results, speedmap_positions
  v5 — added career breakdown columns, market_mover, odds_fluctuations,
         sb_result, settling_position, speed_map_pct, race_class to runner_form
"""

from __future__ import annotations
import json
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional

from config import AEST, DB_PATH

# ── Thread-local connection ───────────────────────────────────────────────────
_local = threading.local()

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meetings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    venue                TEXT    NOT NULL,
    race_date            TEXT    NOT NULL,
    sportsbet_meeting_id TEXT,
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_venue_date ON meetings(venue, race_date);

CREATE TABLE IF NOT EXISTS races (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id              INTEGER NOT NULL REFERENCES meetings(id),
    race_number             INTEGER NOT NULL,
    race_name               TEXT,
    distance_m              INTEGER,
    race_class              TEXT,
    track_condition         TEXT,
    rail_position           TEXT,
    prize_money             REAL,
    prize_1st               REAL,
    prize_2nd               REAL,
    prize_3rd               REAL,
    prize_4th               REAL,
    jump_time               TEXT,
    age_restriction         TEXT,
    sex_restriction         TEXT,
    official_rating_band    TEXT,
    straight_m              INTEGER,
    circumference_m         INTEGER,
    track_record            TEXT,
    sbform_url              TEXT,
    sportsbet_event_id      INTEGER,
    scraped_at              TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_races_meeting ON races(meeting_id, race_number);
CREATE INDEX IF NOT EXISTS idx_races_event_id ON races(sportsbet_event_id);

CREATE TABLE IF NOT EXISTS runners (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id              INTEGER NOT NULL REFERENCES races(id),
    runner_number        INTEGER,
    runner_name          TEXT    NOT NULL,
    barrier              INTEGER,
    weight_kg            REAL,
    jockey               TEXT,
    trainer              TEXT,
    scratched            INTEGER DEFAULT 0,
    -- Market data
    win_odds             REAL,
    place_odds           REAL,
    win_odds_mdp         REAL,
    market_mover         INTEGER DEFAULT 0,
    odds_fluctuations    TEXT,       -- JSON array of floats
    sb_result            TEXT,       -- W / L / P / '' (filled post-race)
    -- Ratings & scores
    official_rating      REAL,
    speed_rating         REAL,
    form_score           REAL,
    place_score_form     REAL,
    recent_form_pts      REAL,
    distance_fit         REAL,
    condition_fit        REAL,
    venue_fit            REAL,
    velocity_score       REAL,
    class_fit_score      REAL,
    -- Career aggregates (fast SQL-queryable, not JSON)
    career_starts        INTEGER,
    career_wins          INTEGER,
    career_places        INTEGER,
    career_win_rate      REAL,
    track_win_rate       REAL,
    career_good_starts   INTEGER,
    career_good_wins     INTEGER,
    career_soft_starts   INTEGER,
    career_soft_wins     INTEGER,
    career_heavy_starts  INTEGER,
    career_heavy_wins    INTEGER,
    career_first_up_starts  INTEGER,
    career_first_up_wins    INTEGER,
    career_second_up_starts INTEGER,
    career_second_up_wins   INTEGER,
    career_dist_starts   INTEGER,
    career_dist_wins     INTEGER,
    career_track_starts  INTEGER,
    career_track_wins    INTEGER,
    prize_money_total    REAL,
    -- Pace / speed map
    pace_role            TEXT,
    settling_position    TEXT,       -- Leader / Off Pace / Midfield / Back (from speed map)
    speed_map_pct        INTEGER,    -- 0-100 horizontal position
    -- Descriptive
    form_fig             TEXT,
    overview             TEXT,
    days_since_last_run  INTEGER,
    career_json          TEXT,       -- full raw JSON blob (kept for backward compat)
    gear_json            TEXT,
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runners_race ON runners(race_id);
CREATE INDEX IF NOT EXISTS idx_runners_name ON runners(runner_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS runner_form (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    runner_id            INTEGER NOT NULL REFERENCES runners(id),
    run_date             TEXT,
    venue                TEXT,
    distance_m           INTEGER,
    track_condition      TEXT,
    race_class           TEXT,
    finishing_position   INTEGER,
    field_size           INTEGER,
    margin               REAL,
    starting_price       REAL,
    barrier              INTEGER,
    weight_kg            REAL,
    jockey               TEXT,
    in_running_pos       TEXT,
    winner_name          TEXT,
    second_name          TEXT,
    third_name           TEXT,
    prize_money          REAL,
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runner_form_runner ON runner_form(runner_id);
CREATE INDEX IF NOT EXISTS idx_runner_form_date ON runner_form(run_date);

CREATE TABLE IF NOT EXISTS weather (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    venue                TEXT    NOT NULL,
    race_date            TEXT    NOT NULL,
    observation_time     TEXT,
    temperature          REAL,
    wind_speed_kmh       REAL,
    wind_direction       TEXT,
    humidity             REAL,
    barometric_pressure  REAL,
    dew_point            REAL,
    source               TEXT    DEFAULT 'bom',
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weather_venue_date ON weather(venue, race_date);

CREATE TABLE IF NOT EXISTS track_conditions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    venue                TEXT    NOT NULL,
    race_date            TEXT    NOT NULL,
    race_number          INTEGER,
    track_rating         TEXT,
    rail_position        TEXT,
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_venue_date ON track_conditions(venue, race_date);

CREATE TABLE IF NOT EXISTS sectionals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    runner_name          TEXT    NOT NULL,
    venue                TEXT    NOT NULL,
    race_date            TEXT    NOT NULL,
    race_number          INTEGER NOT NULL,
    l200m                REAL,
    l400m                REAL,
    l600m                REAL,
    source               TEXT    DEFAULT 'racing.com',
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sectionals_race ON sectionals(venue, race_date, race_number);

CREATE TABLE IF NOT EXISTS speedmap_positions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    venue                TEXT    NOT NULL,
    race_date            TEXT    NOT NULL,
    race_number          INTEGER NOT NULL,
    runner_name          TEXT    NOT NULL,
    settling_position    TEXT,       -- Leader / Off Pace / Midfield / Back
    speed_map_pct        INTEGER,    -- 0-100
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_speedmap ON speedmap_positions(venue, race_date, race_number);

CREATE TABLE IF NOT EXISTS analysis_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id              INTEGER NOT NULL REFERENCES races(id),
    analysis_pass        TEXT    NOT NULL,
    model                TEXT    NOT NULL,
    result_json          TEXT,
    raw_text             TEXT    NOT NULL,
    created_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_race_pass ON analysis_results(race_id, analysis_pass);

CREATE TABLE IF NOT EXISTS race_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL REFERENCES races(id),
    raw_json   TEXT    NOT NULL,
    scraped_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_race_results_race ON race_results(race_id);

-- ── Trainer / jockey stats from sportsbetform.com.au ──────────────────────────

CREATE TABLE IF NOT EXISTS trainer_form_ids (
    trainer              TEXT PRIMARY KEY,
    sportsbetform_id     INTEGER,
    updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS jockey_form_ids (
    jockey               TEXT PRIMARY KEY,
    sportsbetform_id     INTEGER,
    updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS trainer_stats (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    trainer              TEXT    NOT NULL,
    dimension_type       TEXT    NOT NULL,  -- track_condition / distance / barrier / spell / month / field_size / prize_bracket
    dimension_value      TEXT    NOT NULL,  -- e.g. 'Good' / '1200-1400' / '4-6' / 'First Up'
    stat_period          TEXT    DEFAULT 'career',
    starts               INTEGER DEFAULT 0,
    wins                 INTEGER DEFAULT 0,
    places               INTEGER DEFAULT 0,
    win_pct              REAL,
    place_pct            REAL,
    roi_pct              REAL,
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trainer_stats ON trainer_stats(trainer, dimension_type);

CREATE TABLE IF NOT EXISTS jockey_stats (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    jockey               TEXT    NOT NULL,
    dimension_type       TEXT    NOT NULL,
    dimension_value      TEXT    NOT NULL,
    stat_period          TEXT    DEFAULT 'career',
    starts               INTEGER DEFAULT 0,
    wins                 INTEGER DEFAULT 0,
    places               INTEGER DEFAULT 0,
    win_pct              REAL,
    place_pct            REAL,
    roi_pct              REAL,
    scraped_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jockey_stats ON jockey_stats(jockey, dimension_type);

-- ── Backtest / performance tracking ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS backtest_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id              INTEGER REFERENCES races(id),
    race_date            TEXT,
    venue                TEXT,
    race_number          INTEGER,
    silo                 TEXT,          -- A / B / C
    distance_m           INTEGER,
    track_condition      TEXT,
    selection_1a         TEXT,
    selection_1a_barrier INTEGER,
    selection_1a_odds    REAL,
    selection_1a_finished INTEGER,      -- finishing position
    winner_name          TEXT,
    winner_barrier       INTEGER,
    winner_sp            REAL,
    top4_correct         INTEGER DEFAULT 0,   -- 1 if 1A finished top 4
    pass4_model          TEXT,
    post_race_at         TEXT,
    notes                TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_date ON backtest_results(race_date, venue);
"""

# ── Migration: add columns that appeared in later schema versions ─────────────
# Safe to run on existing DB — ALTER TABLE ADD COLUMN is idempotent (skips if exists).

_MIGRATIONS = [
    # v2
    ("runners", "win_odds_mdp",          "REAL"),
    ("runners", "official_rating",       "REAL"),
    ("runners", "speed_rating",          "REAL"),
    ("runners", "form_score",            "REAL"),
    ("runners", "place_score_form",      "REAL"),
    ("runners", "recent_form_pts",       "REAL"),
    ("runners", "distance_fit",          "REAL"),
    ("runners", "condition_fit",         "REAL"),
    ("runners", "venue_fit",             "REAL"),
    ("runners", "velocity_score",        "REAL"),
    ("runners", "career_win_rate",       "REAL"),
    ("runners", "track_win_rate",        "REAL"),
    # v5
    ("runners", "market_mover",          "INTEGER DEFAULT 0"),
    ("runners", "odds_fluctuations",     "TEXT"),
    ("runners", "sb_result",             "TEXT"),
    ("runners", "class_fit_score",       "REAL"),
    ("runners", "career_starts",         "INTEGER"),
    ("runners", "career_wins",           "INTEGER"),
    ("runners", "career_places",         "INTEGER"),
    ("runners", "career_good_starts",    "INTEGER"),
    ("runners", "career_good_wins",      "INTEGER"),
    ("runners", "career_soft_starts",    "INTEGER"),
    ("runners", "career_soft_wins",      "INTEGER"),
    ("runners", "career_heavy_starts",   "INTEGER"),
    ("runners", "career_heavy_wins",     "INTEGER"),
    ("runners", "career_first_up_starts","INTEGER"),
    ("runners", "career_first_up_wins",  "INTEGER"),
    ("runners", "career_second_up_starts","INTEGER"),
    ("runners", "career_second_up_wins", "INTEGER"),
    ("runners", "career_dist_starts",    "INTEGER"),
    ("runners", "career_dist_wins",      "INTEGER"),
    ("runners", "career_track_starts",   "INTEGER"),
    ("runners", "career_track_wins",     "INTEGER"),
    ("runners", "prize_money_total",     "REAL"),
    ("runners", "settling_position",     "TEXT"),
    ("runners", "speed_map_pct",         "INTEGER"),
    # races v5
    ("races",   "prize_1st",             "REAL"),
    ("races",   "prize_2nd",             "REAL"),
    ("races",   "prize_3rd",             "REAL"),
    ("races",   "prize_4th",             "REAL"),
    ("races",   "age_restriction",       "TEXT"),
    ("races",   "sex_restriction",       "TEXT"),
    ("races",   "official_rating_band",  "TEXT"),
    ("races",   "straight_m",            "INTEGER"),
    ("races",   "circumference_m",       "INTEGER"),
    ("races",   "track_record",          "TEXT"),
    ("races",   "sbform_url",            "TEXT"),
    # runner_form v5
    ("runner_form", "race_class",        "TEXT"),
    ("runner_form", "prize_money",       "REAL"),
]


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(
            "PRAGMA journal_mode=WAL; "
            "PRAGMA synchronous=NORMAL; "
            "PRAGMA foreign_keys=ON;"
        )
        _local.conn = c
    return _local.conn


def _now() -> str:
    return datetime.now(AEST).isoformat()


def _run_migrations() -> None:
    """Add any missing columns to existing tables without wiping data."""
    conn = _conn()
    for table, col, typ in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass   # column already exists
    conn.commit()


def init_db() -> None:
    """Create all tables (if missing) then run additive migrations."""
    _conn().executescript(_SCHEMA)
    _conn().commit()
    _run_migrations()


# ── Condition normaliser ──────────────────────────────────────────────────────

_COND_MAP = {
    "good":   "Good",
    "firm":   "Firm",
    "soft":   "Soft",
    "heavy":  "Heavy",
    "synth":  "Synthetic",
    "all weather": "Synthetic",
    "yielding":    "Soft",
    "dead":        "Good",
}
_RATING_RE = re.compile(r"\((\d+)\)")


def normalise_condition(raw: Optional[str]) -> str:
    """
    Normalise track condition strings from the Sportsbet API.
    'Good(4)' → 'Good(4)'  (keep rating for model, but base word is title-cased)
    'GOOD4'   → 'Good'
    'soft7'   → 'Soft(7)'
    """
    if not raw:
        return "Unknown"
    s = str(raw).strip()
    # Extract numeric rating if present
    m = _RATING_RE.search(s)
    rating = f"({m.group(1)})" if m else ""
    # Strip rating and normalise base
    base = _RATING_RE.sub("", s).strip().rstrip("0123456789").strip().lower()
    mapped = _COND_MAP.get(base, base.title())
    return f"{mapped}{rating}" if rating else mapped


# ── Numeric helpers ───────────────────────────────────────────────────────────

def _parse_margin(m: Any) -> Optional[float]:
    if m is None:
        return None
    if isinstance(m, (int, float)):
        return float(m)
    s = str(m).replace("L", "").replace("l", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v).replace("m", "").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _parse_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_career_record(s: Optional[str]) -> tuple[int, int, int]:
    """
    Parse '14:3,2,1' or '3:1,0,1' → (starts, wins, places).
    Returns (0, 0, 0) on any parse failure.
    """
    if not s:
        return 0, 0, 0
    try:
        colon = s.index(":")
        starts = int(s[:colon])
        parts = s[colon + 1:].split(",")
        wins   = int(parts[0]) if len(parts) > 0 else 0
        places = int(parts[1]) if len(parts) > 1 else 0
        return starts, wins, places
    except Exception:
        return 0, 0, 0


# ── Insert helpers ────────────────────────────────────────────────────────────

def insert_meeting(venue: str, race_date: str,
                   sportsbet_meeting_id: Optional[str] = None) -> int:
    cur = _conn().execute(
        "INSERT INTO meetings (venue, race_date, sportsbet_meeting_id, scraped_at)"
        " VALUES (?,?,?,?)",
        (venue, race_date, sportsbet_meeting_id, _now()),
    )
    _conn().commit()
    return cur.lastrowid


def insert_race(
    meeting_id: int,
    race_number: int,
    race_name: Optional[str],
    distance_m: Optional[int],
    race_class: Optional[str],
    track_condition: Optional[str],
    rail_position: Optional[str],
    prize_money: Optional[float],
    jump_time: Optional[str],
    sportsbet_event_id: Optional[int],
    age_restriction: Optional[str] = None,
    sex_restriction: Optional[str] = None,
    official_rating_band: Optional[str] = None,
    sbform_url: Optional[str] = None,
    prize_1st: Optional[float] = None,
    prize_2nd: Optional[float] = None,
    prize_3rd: Optional[float] = None,
    prize_4th: Optional[float] = None,
) -> int:
    cur = _conn().execute(
        """INSERT INTO races
           (meeting_id, race_number, race_name, distance_m, race_class,
            track_condition, rail_position, prize_money, jump_time,
            sportsbet_event_id, age_restriction, sex_restriction,
            official_rating_band, sbform_url,
            prize_1st, prize_2nd, prize_3rd, prize_4th, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            meeting_id, race_number, race_name, distance_m, race_class,
            normalise_condition(track_condition), rail_position, prize_money, jump_time,
            sportsbet_event_id, age_restriction, sex_restriction,
            official_rating_band, sbform_url,
            prize_1st, prize_2nd, prize_3rd, prize_4th, _now(),
        ),
    )
    _conn().commit()
    return cur.lastrowid


def insert_runner(race_id: int, fd: dict) -> int:
    """
    Insert a runner from a scraper form_data dict.
    Parses career_stats sub-dict into individual columns for fast SQL queries.
    Also stores the full career blob in career_json for backward compat.
    """
    career_raw = fd.get("career") or fd.get("career_stats") or {}

    # Parse each career dimension record string
    def _cr(key: str) -> tuple[int, int, int]:
        return _parse_career_record(career_raw.get(key))

    cs_career  = _cr("total_runs")
    cs_good    = _cr("good_track")
    cs_soft    = _cr("soft_track")
    cs_heavy   = _cr("heavy_track")
    cs_first   = _cr("first_up")
    cs_second  = _cr("second_up")
    cs_dist    = _cr("distance_record")
    cs_track   = _cr("track_record")

    def _wi(t: tuple) -> Optional[float]:
        """Win rate or None if no starts."""
        return (t[1] / t[0]) if t[0] else None

    gear  = fd.get("gear_flags") or fd.get("gear") or []
    flucs = fd.get("odds_fluctuations") or []

    cur = _conn().execute(
        """INSERT INTO runners
           (race_id, runner_number, runner_name, barrier, weight_kg,
            jockey, trainer, scratched,
            win_odds, place_odds, win_odds_mdp, market_mover, odds_fluctuations, sb_result,
            official_rating, speed_rating,
            form_score, place_score_form, recent_form_pts,
            distance_fit, condition_fit, venue_fit, velocity_score, class_fit_score,
            career_starts, career_wins, career_places, career_win_rate, track_win_rate,
            career_good_starts, career_good_wins,
            career_soft_starts, career_soft_wins,
            career_heavy_starts, career_heavy_wins,
            career_first_up_starts, career_first_up_wins,
            career_second_up_starts, career_second_up_wins,
            career_dist_starts, career_dist_wins,
            career_track_starts, career_track_wins,
            prize_money_total,
            pace_role, settling_position, speed_map_pct,
            form_fig, overview, days_since_last_run,
            career_json, gear_json, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            race_id,
            fd.get("runner_number"),
            fd.get("sb_name") or fd.get("runner_name") or "",
            fd.get("barrier"),
            _parse_float(fd.get("weight_kg")),
            fd.get("jockey"),
            fd.get("trainer"),
            1 if fd.get("scratched") else 0,
            # market
            _parse_float(fd.get("win_odds")),
            _parse_float(fd.get("place_odds")),
            _parse_float(fd.get("win_odds_mdp")),
            1 if fd.get("market_mover") else 0,
            json.dumps(flucs) if flucs else None,
            fd.get("sb_result") or "",
            # ratings
            _parse_float(fd.get("official_rating")),
            _parse_float(fd.get("speed_rating")),
            # scores
            _parse_float(fd.get("form_score")),
            _parse_float(fd.get("place_score_form")),
            _parse_float(fd.get("recent_form_pts")),
            _parse_float(fd.get("distance_fit")),
            _parse_float(fd.get("condition_fit")),
            _parse_float(fd.get("venue_fit")),
            _parse_float(fd.get("velocity_score")),
            _parse_float(fd.get("class_fit_score")),
            # career breakdown
            cs_career[0], cs_career[1], cs_career[2],
            _wi(cs_career), _wi(cs_track),
            cs_good[0],   cs_good[1],
            cs_soft[0],   cs_soft[1],
            cs_heavy[0],  cs_heavy[1],
            cs_first[0],  cs_first[1],
            cs_second[0], cs_second[1],
            cs_dist[0],   cs_dist[1],
            cs_track[0],  cs_track[1],
            _parse_float(career_raw.get("prize_money")),
            # pace
            fd.get("pace_role"),
            fd.get("settling_position"),
            fd.get("speed_map_pct"),
            # descriptive
            fd.get("form_fig") or fd.get("lastSix"),
            fd.get("overview"),
            _parse_int(fd.get("days_since_last_run")),
            json.dumps(career_raw) if career_raw else None,
            json.dumps(gear) if gear else None,
            _now(),
        ),
    )
    _conn().commit()
    return cur.lastrowid


def insert_runner_form(runner_id: int, run: dict) -> None:
    _conn().execute(
        """INSERT INTO runner_form
           (runner_id, run_date, venue, distance_m, track_condition, race_class,
            finishing_position, field_size, margin, starting_price,
            barrier, weight_kg, jockey, in_running_pos,
            winner_name, second_name, third_name, prize_money, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            runner_id,
            run.get("date") or run.get("run_date"),
            run.get("trackName") or run.get("venue"),
            _parse_int(run.get("distance") or run.get("distance_m")),
            normalise_condition(run.get("trackStatus") or run.get("track_condition")),
            run.get("race_class"),
            _parse_int(run.get("place") or run.get("finishing_position")),
            _parse_int(run.get("totalRunners") or run.get("field_size")),
            _parse_margin(run.get("margin")),
            _parse_float(run.get("startingPrice") or run.get("starting_price")),
            run.get("barrier"),
            _parse_float(run.get("weight_kg")),
            run.get("jockey"),
            run.get("in_running_pos"),
            run.get("winner_name"),
            run.get("second_name"),
            run.get("third_name"),
            _parse_float(run.get("prize_money")),
            _now(),
        ),
    )
    _conn().commit()


def insert_weather(venue: str, race_date: str, obs: dict) -> None:
    _conn().execute(
        """INSERT INTO weather
           (venue, race_date, observation_time, temperature, wind_speed_kmh,
            wind_direction, humidity, barometric_pressure, dew_point, source, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            venue, race_date,
            obs.get("observation_time"),
            obs.get("temperature"),
            obs.get("wind_speed_kmh"),
            obs.get("wind_direction"),
            obs.get("humidity"),
            obs.get("barometric_pressure"),
            obs.get("dew_point"),
            obs.get("source", "bom"),
            _now(),
        ),
    )
    _conn().commit()


def insert_track_condition(venue: str, race_date: str, race_number: Optional[int],
                            track_rating: Optional[str],
                            rail_position: Optional[str]) -> None:
    _conn().execute(
        """INSERT INTO track_conditions
           (venue, race_date, race_number, track_rating, rail_position, scraped_at)
           VALUES (?,?,?,?,?,?)""",
        (venue, race_date, race_number, track_rating, rail_position, _now()),
    )
    _conn().commit()


def insert_sectionals(runner_name: str, venue: str, race_date: str,
                      race_number: int, l200m: Optional[float],
                      l400m: Optional[float], l600m: Optional[float],
                      source: str = "racing.com") -> None:
    _conn().execute(
        """INSERT INTO sectionals
           (runner_name, venue, race_date, race_number, l200m, l400m, l600m, source, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (runner_name, venue, race_date, race_number, l200m, l400m, l600m, source, _now()),
    )
    _conn().commit()


def insert_speedmap_positions(venue: str, race_date: str, race_number: int,
                               positions: list[dict]) -> None:
    """
    Bulk-insert speed map positions for all runners in a race.
    Each dict should have: runner_name, settling_position, speed_map_pct.
    Also back-fills runners.settling_position and runners.speed_map_pct.
    """
    conn = _conn()
    for p in positions:
        name = p.get("runner_name") or ""
        sett = p.get("settling_position")
        pct  = p.get("speed_map_pct")
        conn.execute(
            """INSERT INTO speedmap_positions
               (venue, race_date, race_number, runner_name, settling_position, speed_map_pct, scraped_at)
               VALUES (?,?,?,?,?,?,?)""",
            (venue, race_date, race_number, name, sett, pct, _now()),
        )
        # Back-fill runners table (best-effort — matches by race + name)
        conn.execute(
            """UPDATE runners SET settling_position=?, speed_map_pct=?
               WHERE race_id IN (
                   SELECT r.id FROM races r
                   JOIN meetings m ON m.id=r.meeting_id
                   WHERE m.venue=? AND m.race_date=? AND r.race_number=?
               )
               AND LOWER(runner_name) = LOWER(?)""",
            (sett, pct, venue, race_date, race_number, name),
        )
    conn.commit()


def insert_analysis(race_id: int, analysis_pass: str, model: str,
                    raw_text: str, result_json: Optional[str] = None) -> int:
    cur = _conn().execute(
        """INSERT INTO analysis_results
           (race_id, analysis_pass, model, result_json, raw_text, created_at)
           VALUES (?,?,?,?,?,?)""",
        (race_id, analysis_pass, model, result_json, raw_text, _now()),
    )
    _conn().commit()
    return cur.lastrowid


def insert_race_results(race_id: int, result: dict) -> None:
    _conn().execute(
        "INSERT INTO race_results (race_id, raw_json, scraped_at) VALUES (?,?,?)",
        (race_id, json.dumps(result, ensure_ascii=False), _now()),
    )
    _conn().commit()


# ── Trainer / Jockey stat helpers ─────────────────────────────────────────────

def upsert_trainer_form_id(trainer: str, sbform_id: int) -> None:
    _conn().execute(
        """INSERT INTO trainer_form_ids (trainer, sportsbetform_id, updated_at)
           VALUES (?,?,?)
           ON CONFLICT(trainer) DO UPDATE SET
               sportsbetform_id=excluded.sportsbetform_id,
               updated_at=excluded.updated_at""",
        (trainer, sbform_id, _now()),
    )
    _conn().commit()


def upsert_jockey_form_id(jockey: str, sbform_id: int) -> None:
    _conn().execute(
        """INSERT INTO jockey_form_ids (jockey, sportsbetform_id, updated_at)
           VALUES (?,?,?)
           ON CONFLICT(jockey) DO UPDATE SET
               sportsbetform_id=excluded.sportsbetform_id,
               updated_at=excluded.updated_at""",
        (jockey, sbform_id, _now()),
    )
    _conn().commit()


def insert_trainer_stats(trainer: str, stats: list[dict]) -> None:
    """
    Bulk-insert trainer breakdown stats.
    Each dict: dimension_type, dimension_value, stat_period, starts, wins, places,
               win_pct, place_pct, roi_pct
    Replaces existing rows for same trainer+dimension.
    """
    conn = _conn()
    # Delete existing rows for this trainer first (full refresh)
    conn.execute("DELETE FROM trainer_stats WHERE trainer=?", (trainer,))
    for s in stats:
        conn.execute(
            """INSERT INTO trainer_stats
               (trainer, dimension_type, dimension_value, stat_period,
                starts, wins, places, win_pct, place_pct, roi_pct, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trainer,
                s.get("dimension_type", ""),
                s.get("dimension_value", ""),
                s.get("stat_period", "career"),
                s.get("starts", 0),
                s.get("wins", 0),
                s.get("places", 0),
                _parse_float(s.get("win_pct")),
                _parse_float(s.get("place_pct")),
                _parse_float(s.get("roi_pct")),
                _now(),
            ),
        )
    conn.commit()


def insert_jockey_stats(jockey: str, stats: list[dict]) -> None:
    conn = _conn()
    conn.execute("DELETE FROM jockey_stats WHERE jockey=?", (jockey,))
    for s in stats:
        conn.execute(
            """INSERT INTO jockey_stats
               (jockey, dimension_type, dimension_value, stat_period,
                starts, wins, places, win_pct, place_pct, roi_pct, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                jockey,
                s.get("dimension_type", ""),
                s.get("dimension_value", ""),
                s.get("stat_period", "career"),
                s.get("starts", 0),
                s.get("wins", 0),
                s.get("places", 0),
                _parse_float(s.get("win_pct")),
                _parse_float(s.get("place_pct")),
                _parse_float(s.get("roi_pct")),
                _now(),
            ),
        )
    conn.commit()


def insert_backtest_result(data: dict) -> None:
    """Record a post-race backtest result for performance tracking."""
    _conn().execute(
        """INSERT INTO backtest_results
           (race_id, race_date, venue, race_number, silo, distance_m, track_condition,
            selection_1a, selection_1a_barrier, selection_1a_odds, selection_1a_finished,
            winner_name, winner_barrier, winner_sp, top4_correct, pass4_model,
            post_race_at, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get("race_id"),
            data.get("race_date"),
            data.get("venue"),
            data.get("race_number"),
            data.get("silo"),
            data.get("distance_m"),
            data.get("track_condition"),
            data.get("selection_1a"),
            data.get("selection_1a_barrier"),
            _parse_float(data.get("selection_1a_odds")),
            data.get("selection_1a_finished"),
            data.get("winner_name"),
            data.get("winner_barrier"),
            _parse_float(data.get("winner_sp")),
            1 if data.get("top4_correct") else 0,
            data.get("pass4_model"),
            data.get("post_race_at") or _now(),
            data.get("notes"),
        ),
    )
    _conn().commit()


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_meeting(venue: str, race_date: str) -> Optional[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM meetings WHERE venue=? AND race_date=? ORDER BY scraped_at DESC LIMIT 1",
        (venue, race_date),
    ).fetchone()


def get_race(meeting_id: int, race_number: int) -> Optional[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM races WHERE meeting_id=? AND race_number=?"
        " ORDER BY scraped_at DESC LIMIT 1",
        (meeting_id, race_number),
    ).fetchone()


def get_race_by_id(race_id: int) -> Optional[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM races WHERE id=?", (race_id,)
    ).fetchone()


def get_race_by_event_id(event_id: int) -> Optional[sqlite3.Row]:
    return _conn().execute(
        """SELECT r.*, m.venue, m.race_date
           FROM races r JOIN meetings m ON m.id = r.meeting_id
           WHERE r.sportsbet_event_id=?
           ORDER BY r.scraped_at DESC LIMIT 1""",
        (event_id,),
    ).fetchone()


def get_runners(race_id: int) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT r.* FROM runners r
           WHERE r.race_id=?
             AND r.id IN (
               SELECT MAX(id) FROM runners WHERE race_id=? GROUP BY runner_name
             )
           ORDER BY runner_number ASC NULLS LAST, barrier ASC NULLS LAST""",
        (race_id, race_id),
    ).fetchall()


def get_runner_form(runner_id: int) -> list[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM runner_form WHERE runner_id=? ORDER BY run_date DESC",
        (runner_id,),
    ).fetchall()


def get_historical_form_for_horse(runner_name: str,
                                  limit: int = 20) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT rf.*,
                  m.venue  AS meet_venue,
                  m.race_date AS meet_date,
                  ra.race_number,
                  ra.distance_m  AS race_dist,
                  ra.track_condition AS race_cond
           FROM runner_form rf
           JOIN runners r  ON r.id  = rf.runner_id
           JOIN races   ra ON ra.id = r.race_id
           JOIN meetings m ON m.id  = ra.meeting_id
           WHERE r.runner_name = ? COLLATE NOCASE
           ORDER BY rf.run_date DESC, rf.scraped_at DESC
           LIMIT ?""",
        (runner_name, limit),
    ).fetchall()


def get_weather(venue: str, race_date: str) -> Optional[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM weather WHERE venue=? AND race_date=? ORDER BY scraped_at DESC LIMIT 1",
        (venue, race_date),
    ).fetchone()


def get_track_condition(venue: str, race_date: str,
                        race_number: Optional[int] = None) -> Optional[sqlite3.Row]:
    if race_number is not None:
        row = _conn().execute(
            """SELECT * FROM track_conditions
               WHERE venue=? AND race_date=? AND race_number=?
               ORDER BY scraped_at DESC LIMIT 1""",
            (venue, race_date, race_number),
        ).fetchone()
        if row:
            return row
    return _conn().execute(
        """SELECT * FROM track_conditions
           WHERE venue=? AND race_date=?
           ORDER BY scraped_at DESC LIMIT 1""",
        (venue, race_date),
    ).fetchone()


def get_sectionals(venue: str, race_date: str, race_number: int) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT * FROM sectionals
           WHERE venue=? AND race_date=? AND race_number=?
           ORDER BY scraped_at DESC""",
        (venue, race_date, race_number),
    ).fetchall()


def get_speedmap_positions(venue: str, race_date: str,
                           race_number: int) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT * FROM speedmap_positions
           WHERE venue=? AND race_date=? AND race_number=?
           ORDER BY speed_map_pct ASC""",
        (venue, race_date, race_number),
    ).fetchall()


def get_analysis(race_id: int, analysis_pass: str) -> Optional[sqlite3.Row]:
    return _conn().execute(
        """SELECT * FROM analysis_results
           WHERE race_id=? AND analysis_pass=?
           ORDER BY created_at DESC LIMIT 1""",
        (race_id, analysis_pass),
    ).fetchone()


def get_all_analyses(race_id: int) -> list[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM analysis_results WHERE race_id=? ORDER BY created_at ASC",
        (race_id,),
    ).fetchall()


def get_race_results(race_id: int) -> Optional[dict]:
    row = _conn().execute(
        "SELECT raw_json FROM race_results WHERE race_id=? ORDER BY scraped_at DESC LIMIT 1",
        (race_id,),
    ).fetchone()
    if row:
        try:
            return json.loads(row["raw_json"])
        except Exception:
            return None
    return None


def get_trainer_stats(trainer: str,
                      dimension_type: Optional[str] = None) -> list[sqlite3.Row]:
    if dimension_type:
        return _conn().execute(
            "SELECT * FROM trainer_stats WHERE trainer=? AND dimension_type=?"
            " ORDER BY starts DESC",
            (trainer, dimension_type),
        ).fetchall()
    return _conn().execute(
        "SELECT * FROM trainer_stats WHERE trainer=? ORDER BY dimension_type, starts DESC",
        (trainer,),
    ).fetchall()


def get_jockey_stats(jockey: str,
                     dimension_type: Optional[str] = None) -> list[sqlite3.Row]:
    if dimension_type:
        return _conn().execute(
            "SELECT * FROM jockey_stats WHERE jockey=? AND dimension_type=?"
            " ORDER BY starts DESC",
            (jockey, dimension_type),
        ).fetchall()
    return _conn().execute(
        "SELECT * FROM jockey_stats WHERE jockey=? ORDER BY dimension_type, starts DESC",
        (jockey,),
    ).fetchall()


def get_backtest_summary() -> dict:
    """Return aggregate performance metrics across all backtest_results rows."""
    rows = _conn().execute(
        """SELECT silo,
                  COUNT(*) AS races,
                  SUM(CASE WHEN selection_1a_finished=1 THEN 1 ELSE 0 END) AS wins,
                  SUM(CASE WHEN selection_1a_finished<=3 THEN 1 ELSE 0 END) AS places,
                  SUM(top4_correct) AS top4,
                  AVG(CASE WHEN selection_1a_odds IS NOT NULL THEN selection_1a_odds END) AS avg_odds
           FROM backtest_results
           GROUP BY silo
           ORDER BY silo""",
    ).fetchall()
    return {r["silo"]: dict(r) for r in rows}


# ── List / batch helpers ──────────────────────────────────────────────────────

def list_stored_races(limit: int = 50) -> list[sqlite3.Row]:
    return list_stored_races_deduped(limit)


def list_stored_races_deduped(limit: int = 100) -> list[sqlite3.Row]:
    from config import get_aest_today
    today = get_aest_today().isoformat()
    return _conn().execute(
        """SELECT r.id, r.race_number, r.race_name, r.distance_m, r.track_condition,
                  r.jump_time, r.sportsbet_event_id, r.scraped_at,
                  m.venue, m.race_date
           FROM races r
           JOIN meetings m ON m.id = r.meeting_id
           WHERE r.id IN (
               SELECT MAX(r2.id)
               FROM races r2
               JOIN meetings m2 ON m2.id = r2.meeting_id
               GROUP BY
                 CASE WHEN r2.sportsbet_event_id IS NOT NULL
                      THEN CAST(r2.sportsbet_event_id AS TEXT)
                      ELSE m2.venue || '|' || m2.race_date || '|' || CAST(r2.race_number AS TEXT)
                 END
           )
           ORDER BY
             CASE WHEN m.race_date = ? THEN 0 ELSE 1 END ASC,
             m.race_date DESC, m.venue ASC, r.race_number ASC
           LIMIT ?""",
        (today, limit),
    ).fetchall()


def list_races_analysed_no_results() -> list[sqlite3.Row]:
    from config import get_aest_today
    today = get_aest_today().isoformat()
    return _conn().execute(
        """SELECT r.id, r.race_number, r.race_name, r.sportsbet_event_id, r.scraped_at,
                  m.venue, m.race_date
           FROM races r
           JOIN meetings m ON m.id = r.meeting_id
           WHERE r.id IN (SELECT race_id FROM analysis_results WHERE analysis_pass='PASS_4')
             AND r.id NOT IN (SELECT race_id FROM race_results)
             AND r.id IN (
               SELECT MAX(r2.id) FROM races r2
               JOIN meetings m2 ON m2.id = r2.meeting_id
               GROUP BY CASE WHEN r2.sportsbet_event_id IS NOT NULL
                             THEN CAST(r2.sportsbet_event_id AS TEXT)
                             ELSE m2.venue||'|'||m2.race_date||'|'||CAST(r2.race_number AS TEXT)
                        END
             )
           ORDER BY
             CASE WHEN m.race_date = ? THEN 0 ELSE 1 END ASC,
             m.race_date DESC, m.venue ASC, r.race_number ASC""",
        (today,),
    ).fetchall()


def list_races_pending_post_race() -> list[sqlite3.Row]:
    from config import get_aest_today
    today = get_aest_today().isoformat()
    return _conn().execute(
        """SELECT r.id, r.race_number, r.race_name, r.sportsbet_event_id,
                  r.distance_m, r.track_condition, r.scraped_at,
                  m.venue, m.race_date
           FROM races r
           JOIN meetings m ON m.id = r.meeting_id
           WHERE r.id IN (SELECT race_id FROM analysis_results WHERE analysis_pass='PASS_4')
             AND r.id IN (SELECT race_id FROM race_results)
             AND r.id NOT IN (SELECT race_id FROM analysis_results WHERE analysis_pass='POST_RACE')
             AND r.id IN (
               SELECT MAX(r2.id) FROM races r2
               JOIN meetings m2 ON m2.id = r2.meeting_id
               GROUP BY CASE WHEN r2.sportsbet_event_id IS NOT NULL
                             THEN CAST(r2.sportsbet_event_id AS TEXT)
                             ELSE m2.venue||'|'||m2.race_date||'|'||CAST(r2.race_number AS TEXT)
                        END
             )
           ORDER BY
             CASE WHEN m.race_date = ? THEN 0 ELSE 1 END ASC,
             m.race_date DESC, m.venue ASC, r.race_number ASC""",
        (today,),
    ).fetchall()


def list_completed_races(limit: int = 300) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT r.id, r.race_number, r.race_name, r.distance_m,
                  r.track_condition, r.sportsbet_event_id, r.scraped_at,
                  m.venue, m.race_date
           FROM races r
           JOIN meetings m ON m.id = r.meeting_id
           WHERE r.id IN (SELECT race_id FROM analysis_results WHERE analysis_pass='PASS_4')
             AND r.id IN (SELECT race_id FROM race_results)
             AND r.id IN (SELECT race_id FROM analysis_results WHERE analysis_pass='POST_RACE')
             AND r.id IN (
               SELECT MAX(r2.id) FROM races r2
               JOIN meetings m2 ON m2.id = r2.meeting_id
               GROUP BY CASE WHEN r2.sportsbet_event_id IS NOT NULL
                             THEN CAST(r2.sportsbet_event_id AS TEXT)
                             ELSE m2.venue||'|'||m2.race_date||'|'||CAST(r2.race_number AS TEXT)
                        END
             )
           ORDER BY m.race_date DESC, m.venue, r.race_number
           LIMIT ?""",
        (limit,),
    ).fetchall()


def list_past_analyses(limit: int = 50) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT ar.id, ar.race_id, ar.analysis_pass, ar.model, ar.created_at,
                  r.race_number, r.race_name, m.venue, m.race_date
           FROM analysis_results ar
           JOIN races r ON r.id = ar.race_id
           JOIN meetings m ON m.id = r.meeting_id
           ORDER BY m.race_date DESC, m.venue, r.race_number, ar.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def count_runners(race_id: int) -> int:
    row = _conn().execute(
        "SELECT COUNT(DISTINCT runner_name) FROM runners WHERE race_id=?",
        (race_id,),
    ).fetchone()
    return row[0] if row else 0


def get_race_completion_batch(race_ids: list[int]) -> dict[int, dict]:
    if not race_ids:
        return {}
    ph = ",".join("?" * len(race_ids))
    pass_rows = _conn().execute(
        f"""SELECT race_id, analysis_pass FROM analysis_results
            WHERE race_id IN ({ph})
              AND analysis_pass IN ('PASS_4','POST_RACE','PACKAGE')""",
        race_ids,
    ).fetchall()
    result_rows = _conn().execute(
        f"SELECT DISTINCT race_id FROM race_results WHERE race_id IN ({ph})",
        race_ids,
    ).fetchall()
    flags = {
        rid: {"has_pass4": False, "has_results": False,
              "has_post_race": False, "has_package": False}
        for rid in race_ids
    }
    for row in pass_rows:
        rid = row["race_id"]
        p   = row["analysis_pass"]
        if p == "PASS_4":
            flags[rid]["has_pass4"] = True
        elif p == "POST_RACE":
            flags[rid]["has_post_race"] = True
        elif p == "PACKAGE":
            flags[rid]["has_package"] = True
    for row in result_rows:
        flags[row["race_id"]]["has_results"] = True
    return flags


def get_past_runner_appearances(runner_name: str,
                                limit: int = 10) -> list[sqlite3.Row]:
    return _conn().execute(
        """SELECT r.runner_number, r.barrier, r.weight_kg, r.jockey, r.trainer,
                  r.win_odds, r.place_odds, r.form_fig, r.career_win_rate,
                  r.track_win_rate, r.official_rating, r.form_score,
                  r.velocity_score, r.scraped_at,
                  m.venue, m.race_date,
                  ra.race_number, ra.race_name, ra.distance_m, ra.track_condition
           FROM runners r
           JOIN races   ra ON ra.id = r.race_id
           JOIN meetings m ON m.id  = ra.meeting_id
           WHERE r.runner_name = ? COLLATE NOCASE
           ORDER BY m.race_date DESC, r.scraped_at DESC
           LIMIT ?""",
        (runner_name, limit),
    ).fetchall()


def execute_safe_sql(sql: str) -> tuple[list[str], list[dict]]:
    stripped = sql.strip().lstrip(";").strip()
    if not stripped.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are permitted.")
    for kw in ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER",
                "CREATE", "ATTACH", "PRAGMA"):
        if kw in stripped.upper():
            raise ValueError(f"Disallowed keyword in query: {kw}")
    try:
        cursor = _conn().execute(stripped)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        rows = [dict(zip(cols, row)) for row in cursor.fetchmany(200)]
        return cols, rows
    except Exception as exc:
        raise ValueError(f"SQL error: {exc}") from exc
