"""
analyst.py — OMNI-FORENSIC orchestrator with Package-File Pipeline.

Pipeline overview
─────────────────────────────────────────────────────────────────────────────
STEP 1  build_race_package(race_id)
          Assembles race card + form guide + speed map + weather/track from DB
          into ONE structured text file:  Race_NN_PACKAGE.txt
          Stored in DB as analysis_pass='PACKAGE'.

STEP 2  run_full_analysis(race_id)
          Loads the PACKAGE file as the sole data context.
          Runs 6 passes internally (all fed from PACKAGE, not re-queried DB).
          Each intermediate pass is still stored in DB for audit / post-race.
          Final PASS_4 output is written as: Race_NN_ANALYSIS.txt
          Stored in DB as analysis_pass='PASS_4'.

POST-RACE
  build_post_race_input(race_id, results)
          Combines PACKAGE + ANALYSIS content + formatted results into one file:
          Race_NN_POST_RACE_INPUT.txt
          Stored in DB as analysis_pass='POST_RACE_PACKAGE'.

  run_post_race_analysis(race_id, results)
          Loads POST_RACE_INPUT as the sole prompt context.
          Saves output as: Race_NN_POST_RACE.txt
          Stored in DB as analysis_pass='POST_RACE'.
"""

from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

import db
import ollama_client as ollama
from config import AEST, OLLAMA_MODEL, load_settings, report_path, REPORTS_DIR
from db import normalise_condition
from prompt import (
    SYSTEM_PROMPT,
    PASS_0_TEMPLATE,
    PASS_05_TEMPLATE,
    PASS_1_TEMPLATE,
    PASS_2_TEMPLATE,
    PASS_15_TEMPLATE,
    PASS_4_TEMPLATE,
    POST_RACE_SYSTEM_PROMPT,
    POST_RACE_TEMPLATE,
    format_race_context,
)

logger = logging.getLogger(__name__)


# ── Numeric safety helper ─────────────────────────────────────────────────────

def _to_float(val) -> Optional[float]:
    """
    Safely coerce a DB value to float.
    SQLite can return numeric columns as int, float, or str depending on the
    declared column affinity and how the value was inserted.  All three are
    handled here; anything else (None, '', non-numeric string) returns None.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# ── Pass label registry ───────────────────────────────────────────────────────

_PASS_LABELS = {
    "PACKAGE":           "Race Package  — Race Card + Form Guide + Speed Map",
    "PASS_0":            "Pass 0        — Forensic Data Audit",
    "PASS_05":           "Pass 0.5      — Tier 1 Macro Sweep",
    "PASS_1":            "Pass 1        — Forward Draft Canvas",
    "PASS_2":            "Pass 2        — Silo Integrity Audit",
    "PASS_15":           "Pass 1.5      — Probabilistic Projection",
    "PASS_4":            "Pass 4        — Final Canvas Render  [ANALYSIS]",
    "POST_RACE_PACKAGE": "Post-Race Input — Package + Analysis + Results",
    "POST_RACE":         "Post-Race     — Forensic Trace & Feedback",
}


# ══════════════════════════════════════════════════════════════════════════════
#  DB context builders
# ══════════════════════════════════════════════════════════════════════════════

def _build_race_dict(race_id: int) -> dict:
    """Load race + meeting metadata as a plain dict."""
    race = db.get_race_by_id(race_id)
    if not race:
        return {}
    d = dict(race)
    meeting = db._conn().execute(
        "SELECT venue, race_date FROM meetings WHERE id=?",
        (d["meeting_id"],),
    ).fetchone()
    if meeting:
        d["venue"]     = meeting["venue"]
        d["race_date"] = meeting["race_date"]
    return d


def _build_runners_list(race_id: int) -> list[dict]:
    """
    Load all runners as plain dicts.
    Merges form_history from the current scrape with historical
    runner_form rows from previous race scrapes of the same horse.
    Deduplication key: (run_date, venue, finishing_position).
    """
    rows = db.get_runners(race_id)
    runners = []
    for row in rows:
        r = dict(row)
        current_form = [dict(fr) for fr in db.get_runner_form(r["id"])]
        hist_rows    = db.get_historical_form_for_horse(r["runner_name"], limit=25)

        seen: set[tuple] = {
            (f.get("run_date"), f.get("venue"), f.get("finishing_position"))
            for f in current_form
        }
        merged = list(current_form)
        for hr in hist_rows:
            key = (hr["run_date"], hr["venue"], hr["finishing_position"])
            if key not in seen:
                seen.add(key)
                merged.append(dict(hr))

        merged.sort(key=lambda x: x.get("run_date") or "", reverse=True)
        r["form_history"] = merged[:15]
        runners.append(r)
    return runners


def _get_prior_pass(race_id: int, pass_name: str) -> str:
    """Read a prior pass result from DB."""
    row = db.get_analysis(race_id, pass_name)
    if row:
        return row["raw_text"] or ""
    return f"[{pass_name} not yet available]"


def _weather_summary(race_id: int) -> str:
    """Fetch weather data as formatted text for PASS 1.5."""
    race    = _build_race_dict(race_id)
    weather = dict(db.get_weather(race.get("venue", ""), race.get("race_date", "")) or {})
    if not weather:
        return "Weather: UNAVAILABLE"
    parts = []
    if weather.get("temperature") is not None:
        parts.append(f"Temp: {weather['temperature']}°C")
    if weather.get("wind_speed_kmh") is not None:
        wd = weather.get("wind_direction") or ""
        parts.append(f"Wind: {wd} {weather['wind_speed_kmh']} km/h")
    if weather.get("humidity") is not None:
        parts.append(f"Humidity: {weather['humidity']}%")
    if weather.get("barometric_pressure") is not None:
        parts.append(f"Pressure: {weather['barometric_pressure']} hPa")
    if weather.get("dew_point") is not None:
        parts.append(f"Dew Point: {weather['dew_point']}°C")
    return " | ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — Race Package builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_race_package_text(race_id: int) -> str:
    """
    Assemble race card + form guide + speed map + weather/track from DB
    into a single structured text block.

    This is the authoritative data document fed to every analysis pass.
    No DB queries happen after this point during an analysis run.
    """
    race    = _build_race_dict(race_id)
    runners = _build_runners_list(race_id)
    venue   = race.get("venue", "?")
    rdate   = race.get("race_date", "?")
    rnum    = race.get("race_number", 0)
    dist    = race.get("distance_m") or "?"
    cond    = race.get("track_condition") or "Unknown"
    rail    = race.get("rail_position") or "N/A"
    rclass  = race.get("race_class") or "Unknown"
    prize   = race.get("prize_money")
    jtime   = race.get("jump_time") or "TBC"
    rname   = race.get("race_name") or f"Race {rnum}"

    weather   = dict(db.get_weather(venue, rdate) or {})
    track     = dict(db.get_track_condition(venue, rdate, rnum) or {})
    sect_rows = db.get_sectionals(venue, rdate, rnum)
    sects     = [dict(s) for s in sect_rows]

    SEP  = "═" * 80
    SEP2 = "─" * 80

    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines += [
        SEP,
        f"  RACE PACKAGE  ·  {venue}  R{rnum}  ·  {rdate}",
        f"  {rname}",
        (
            f"  Distance: {dist}m  ·  Class: {rclass}  ·  "
            f"Track: {cond}  ·  Rail: {rail}"
        ),
        f"  Prize: ${float(prize):,.0f}" if _to_float(prize) is not None else "  Prize: N/A",
        f"  Jump: {str(jtime)[:19]}",
        SEP,
        "",
    ]

    active = [r for r in runners if not r.get("scratched")]
    scr    = [r for r in runners if r.get("scratched")]

    # ══════════════════════════════════════════════════════════════════════════
    #  SECTION 1 — RACE CARD
    # ══════════════════════════════════════════════════════════════════════════
    lines += [f"── SECTION 1: RACE CARD {SEP2[23:]}", ""]

    hdr = (
        f"{'No.':<4} {'Name':<22} {'Bar':<4} {'Wgt':>6}  "
        f"{'Jockey':<18} {'Trainer':<20} {'Win':>7} {'Plc':>7}  Form"
    )
    lines += [hdr, SEP2]

    for r in sorted(active, key=lambda x: x.get("runner_number") or 99):
        _wo  = _to_float(r.get("win_odds"))
        _po  = _to_float(r.get("place_odds"))
        _wkg = _to_float(r.get("weight_kg"))
        wo   = f"${_wo:.2f}"   if _wo  is not None else "   —"
        po   = f"${_po:.2f}"   if _po  is not None else "   —"
        wkg  = f"{_wkg:.1f}kg" if _wkg is not None else "    —"
        name = (r.get("sb_name") or r.get("runner_name") or "?")[:21]
        jock = (r.get("jockey") or "?")[:17]
        trnr = (r.get("trainer") or "?")[:19]
        form = r.get("form_fig") or "—"
        gear = ""
        if r.get("gear_json"):
            try:
                g = json.loads(r["gear_json"])
                if g:
                    gear = "  [" + ", ".join(str(x) for x in g) + "]"
            except Exception:
                pass
        ovr = ""
        if r.get("overview"):
            ovr = f"\n        ↳ {str(r['overview'])[:100]}"
        lines.append(
            f"{str(r.get('runner_number','?')):<4} {name:<22} "
            f"{str(r.get('barrier','?')):<4} {wkg:>6}  "
            f"{jock:<18} {trnr:<20} {wo:>7} {po:>7}  {form}{gear}{ovr}"
        )

    if scr:
        lines += [
            "",
            "Scratched: " + ", ".join(
                f"{r.get('runner_number','?')}. "
                f"{r.get('sb_name') or r.get('runner_name','?')}"
                for r in scr
            ),
        ]
    lines.append("")

    # ══════════════════════════════════════════════════════════════════════════
    #  SECTION 2 — FORM GUIDE
    # ══════════════════════════════════════════════════════════════════════════
    lines += [f"── SECTION 2: FORM GUIDE {SEP2[24:]}", ""]

    for r in sorted(active, key=lambda x: x.get("runner_number") or 99):
        name   = (r.get("sb_name") or r.get("runner_name") or "?")
        rno    = r.get("runner_number") or "?"
        bar    = r.get("barrier") or "?"
        days   = _to_float(r.get("days_since_last_run"))
        days_s = f"  ·  Days since: {int(days)}" if days is not None else ""

        # Career from dedicated columns (fast path) or JSON blob (fallback)
        career_s = ""
        cs  = _to_float(r.get("career_starts"))
        cw  = _to_float(r.get("career_wins"))
        cp  = _to_float(r.get("career_places"))
        if cs is not None:
            career_s = f"  ·  Career: {int(cs)}s {int(cw or 0)}w {int(cp or 0)}p"
        elif r.get("career_json"):
            try:
                cj = json.loads(r["career_json"])
                st = cj.get("starts") or cj.get("career_starts") or cj.get("total_runs")
                if isinstance(st, str) and ":" in st:
                    st2, rest = st.split(":", 1)
                    parts = rest.split(",")
                    career_s = (
                        f"  ·  Career: {st2.strip()}s "
                        f"{parts[0].strip() if parts else 0}w "
                        f"{parts[1].strip() if len(parts) > 1 else 0}p"
                    )
                elif st is not None:
                    wi = cj.get("wins") or cj.get("career_wins") or 0
                    pl = cj.get("places") or cj.get("career_places") or 0
                    career_s = f"  ·  Career: {st}s {wi}w {pl}p"
            except Exception:
                pass

        # Track breakdown (from dedicated columns if available)
        track_s = ""
        tcs = _to_float(r.get("career_track_starts"))
        tcw = _to_float(r.get("career_track_wins"))
        if tcs:
            track_s = f"  ·  Track: {int(tcs)}s {int(tcw or 0)}w"

        # Condition breakdown
        cond_r = normalise_condition(cond)
        cond_base = cond_r.split("(")[0].strip().lower()
        cond_key_map = {
            "good": ("career_good_starts", "career_good_wins"),
            "soft": ("career_soft_starts", "career_soft_wins"),
            "heavy": ("career_heavy_starts", "career_heavy_wins"),
        }
        cond_s = ""
        if cond_base in cond_key_map:
            ck_s, ck_w = cond_key_map[cond_base]
            c_s = _to_float(r.get(ck_s))
            c_w = _to_float(r.get(ck_w))
            if c_s is not None:
                cond_s = f"  ·  {cond_r}: {int(c_s)}s {int(c_w or 0)}w"

        # Spell (first-up / second-up)
        spell_s = ""
        fus = _to_float(r.get("career_first_up_starts"))
        fuw = _to_float(r.get("career_first_up_wins"))
        if fus is not None and days is not None and days > 21:
            spell_s = f"  ·  1stUp: {int(fus)}s {int(fuw or 0)}w"

        # Scores
        vel  = _to_float(r.get("velocity_score"))
        fs   = _to_float(r.get("form_score"))
        rtr  = _to_float(r.get("official_rating"))
        spdR = _to_float(r.get("speed_rating"))
        mktm = r.get("market_mover")

        score_parts = []
        if vel  is not None: score_parts.append(f"Vel:{vel:.1f}")
        if fs   is not None: score_parts.append(f"Form:{fs:.1f}")
        if rtr  is not None: score_parts.append(f"Rating:{int(rtr)}")
        if spdR is not None: score_parts.append(f"Spd:{int(spdR)}")
        score_s = ("  ·  " + "  ".join(score_parts)) if score_parts else ""
        mktm_s  = "  ·  [MARKET MOVER ▲]" if mktm else ""

        # Win rates
        cwr = _to_float(r.get("career_win_rate"))
        twr = _to_float(r.get("track_win_rate"))
        wr_s = ""
        if cwr is not None or twr is not None:
            wr_parts = []
            if cwr is not None: wr_parts.append(f"Win%:{cwr:.1%}")
            if twr is not None: wr_parts.append(f"Trk%:{twr:.1%}")
            wr_s = "  ·  " + "  ".join(wr_parts)

        # Odds fluctuations
        flucs_s = ""
        if r.get("odds_fluctuations"):
            try:
                flucs = json.loads(r["odds_fluctuations"])
                if flucs:
                    flucs_s = "  ·  Flucs: " + " → ".join(f"${f:.1f}" for f in flucs[-5:])
            except Exception:
                pass

        lines.append(
            f"{rno}. {name}  (Barrier {bar})"
            f"{career_s}{track_s}{cond_s}{spell_s}{days_s}"
            f"{score_s}{wr_s}{mktm_s}{flucs_s}"
        )

        # Trainer/jockey stats from DB (if available)
        trainer = r.get("trainer") or ""
        jockey  = r.get("jockey") or ""
        tj_lines: list[str] = []

        if trainer:
            t_cond = db.get_trainer_stats(trainer, "track_condition")
            t_dist = db.get_trainer_stats(trainer, "distance")
            t_spell = db.get_trainer_stats(trainer, "spell")
            for stat in t_cond:
                sd = dict(stat)
                if cond_base in sd.get("dimension_value", "").lower() and sd.get("starts", 0) >= 3:
                    tj_lines.append(
                        f"   T/{trainer[:16]}  {sd['dimension_value']}:"
                        f" {sd['starts']}s {sd['wins']}w {sd.get('win_pct') or 0:.0f}%"
                        + (f"  ROI:{sd['roi_pct']:.0f}%" if sd.get("roi_pct") is not None else "")
                    )
            for stat in t_dist:
                sd = dict(stat)
                if sd.get("starts", 0) >= 3:
                    tj_lines.append(
                        f"   T/{trainer[:16]}  Dist {sd['dimension_value']}:"
                        f" {sd['starts']}s {sd['wins']}w {sd.get('win_pct') or 0:.0f}%"
                    )
                    break  # only best-matching distance row

        if jockey:
            j_cond = db.get_jockey_stats(jockey, "track_condition")
            j_dist = db.get_jockey_stats(jockey, "distance")
            for stat in j_cond:
                sd = dict(stat)
                if cond_base in sd.get("dimension_value", "").lower() and sd.get("starts", 0) >= 3:
                    tj_lines.append(
                        f"   J/{jockey[:16]}  {sd['dimension_value']}:"
                        f" {sd['starts']}s {sd['wins']}w {sd.get('win_pct') or 0:.0f}%"
                        + (f"  ROI:{sd['roi_pct']:.0f}%" if sd.get("roi_pct") is not None else "")
                    )
            for stat in j_dist:
                sd = dict(stat)
                if sd.get("starts", 0) >= 3:
                    tj_lines.append(
                        f"   J/{jockey[:16]}  Dist {sd['dimension_value']}:"
                        f" {sd['starts']}s {sd['wins']}w {sd.get('win_pct') or 0:.0f}%"
                    )
                    break

        lines.extend(tj_lines)

        form_hist = r.get("form_history") or []
        if form_hist:
            for f in form_hist[:8]:
                fd    = (f.get("run_date") or "?")[:10]
                fv    = (f.get("venue") or "?")[:14]
                fdist = f.get("distance_m") or "?"
                fc    = (f.get("track_condition") or "?")[:4]
                fp    = f.get("finishing_position") or "?"
                fsz   = f.get("field_size") or "?"
                fm    = _to_float(f.get("margin"))
                fsp   = _to_float(f.get("starting_price"))
                fb    = f.get("barrier")
                fj    = (f.get("jockey") or "?")[:14]
                fw    = _to_float(f.get("weight_kg"))
                fir   = f.get("in_running_pos") or ""
                fwn   = f.get("winner_name") or ""
                fwn2  = f.get("second_name") or ""
                frc   = f.get("race_class") or ""

                mg_s  = f" [{fm:.1f}L]"     if fm  is not None else ""
                sp_s  = f" SP${fsp:.2f}"    if fsp is not None else ""
                bar_s = f" B{fb}"           if fb               else ""
                wt_s  = f" {fw:.1f}kg"      if fw  is not None  else ""
                ir_s  = f" IR:{fir}"        if fir              else ""
                wn_s  = f" (W:{fwn[:14]})"  if fwn              else ""
                wn2_s = f" 2:{fwn2[:10]}"   if fwn2             else ""
                rc_s  = f" [{frc[:10]}]"    if frc              else ""

                lines.append(
                    f"   {fd}  {fv:<14}  {fdist}m  {fc}  "
                    f"{fp}/{fsz}{mg_s}{sp_s}{bar_s}{wt_s}"
                    f"  {fj}{ir_s}{wn_s}{wn2_s}{rc_s}"
                )
        else:
            lines.append("   No form history available")
        lines.append("")

    # ══════════════════════════════════════════════════════════════════════════
    #  SECTION 3 — SPEED MAP
    # ══════════════════════════════════════════════════════════════════════════
    lines += [f"── SECTION 3: SPEED MAP {SEP2[23:]}", ""]
    lines.append(f"Distance: {dist}m  ·  Track: {normalise_condition(cond)}  ·  Rail: {rail}")
    lines.append("")

    # Prefer actual speed map positions from DB over inferred pace_role
    speedmap_db = db.get_speedmap_positions(venue, rdate, rnum)
    use_actual  = len(speedmap_db) > 0

    if use_actual:
        lines.append("  Pace Positions  (source: Sportsbet speed map widget)")
        lines.append(f"  {'Runner':<24} {'Bar':>3}  {'Position':<12}  {'Pct':>5}  {'Win':>7}")
        lines.append("  " + "─" * 60)
        for sp in speedmap_db:
            sd    = dict(sp)
            sname = sd.get("runner_name") or "?"
            spos  = sd.get("settling_position") or "?"
            spct  = sd.get("speed_map_pct")
            # Match to runner for odds
            matched = next(
                (r for r in active
                 if (r.get("sb_name") or r.get("runner_name") or "").lower()
                    == sname.lower()),
                None,
            )
            bar_m = matched.get("barrier") or "?" if matched else "?"
            _wo_m = _to_float(matched.get("win_odds")) if matched else None
            wo_m  = f"${_wo_m:.2f}" if _wo_m is not None else "—"
            pct_s = f"{spct}%" if spct is not None else "—"
            lines.append(
                f"  {sname[:23]:<24} {str(bar_m):>3}  {spos:<12}  {pct_s:>5}  {wo_m:>7}"
            )
        lines.append("")
    else:
        lines.append("  Pace Role Groups  (inferred from form — no speed map data)")
        lines.append("")

    # Pace role grouping (always shown as secondary reference)
    pace_groups: dict[str, list[str]] = {}
    for r in sorted(active, key=lambda x: x.get("runner_number") or 99):
        name  = (r.get("sb_name") or r.get("runner_name") or "?")[:22]
        bar   = r.get("barrier") or "?"
        _wo   = _to_float(r.get("win_odds"))
        wo    = f"${_wo:.2f}" if _wo is not None else "—"
        # Prefer actual settling_position if available on runner row
        sett  = r.get("settling_position")
        role  = (sett or r.get("pace_role") or "UNKNOWN").upper()
        pace_groups.setdefault(role, []).append(
            f"{r.get('runner_number','?')}. {name} (B{bar}) {wo}"
        )

    label_prefix = "  Pace Role Groups (confirmed):" if use_actual else "  Pace Role Groups (inferred):"
    lines.append(label_prefix)
    _PACE_ORDER = [
        "LEADER", "PACE SETTER", "ON PACE", "OFF PACE",
        "MIDFIELD", "STALKER", "BACK", "BACK MARKER", "BACKMARKER", "UNKNOWN",
    ]
    printed: set[str] = set()
    for role in _PACE_ORDER:
        if role in pace_groups:
            lines.append(f"    {role}:")
            for entry in pace_groups[role]:
                lines.append(f"      → {entry}")
            printed.add(role)
    for role, entries in pace_groups.items():
        if role not in printed:
            lines.append(f"    {role}:")
            for entry in entries:
                lines.append(f"      → {entry}")
    lines.append("")

    # Barrier draw — ordered by barrier number
    lines.append("  Barrier Draw:")
    for r in sorted(active, key=lambda x: _to_float(x.get("barrier")) or 99):
        name  = (r.get("sb_name") or r.get("runner_name") or "?")[:22]
        bar   = r.get("barrier") or "?"
        sett  = r.get("settling_position")
        role  = sett or r.get("pace_role") or "?"
        _wo   = _to_float(r.get("win_odds"))
        wo    = f"${_wo:.2f}" if _wo is not None else "—"
        lines.append(
            f"    B{str(bar):<3}  {r.get('runner_number','?')}. {name:<22}  [{role}]  {wo}"
        )
    lines.append("")

    # Sectional times
    if sects:
        lines.append("  Sectional Times (racing.com):")
        lines.append(f"  {'Runner':<20}  {'L600':>7}  {'L400':>7}  {'L200':>7}")
        lines.append("  " + "─" * 47)
        for s in sects:
            sn = (s.get("runner_name") or "?")[:19]
            l6 = str(s.get("l600m") or "—")
            l4 = str(s.get("l400m") or "—")
            l2 = str(s.get("l200m") or "—")
            lines.append(f"  {sn:<20}  {l6:>7}  {l4:>7}  {l2:>7}")
        lines.append("")
    else:
        lines.append("  Sectional Times: Not available")
        lines.append("")

    # ══════════════════════════════════════════════════════════════════════════
    #  SECTION 4 — WEATHER & TRACK CONDITIONS
    # ══════════════════════════════════════════════════════════════════════════
    lines += [f"── SECTION 4: WEATHER & TRACK {SEP2[29:]}", ""]

    if weather:
        w_parts: list[str] = []
        if weather.get("temperature") is not None:
            w_parts.append(f"Temp: {weather['temperature']}°C")
        if weather.get("wind_speed_kmh") is not None:
            wd = weather.get("wind_direction") or ""
            w_parts.append(f"Wind: {wd} {weather['wind_speed_kmh']} km/h")
        if weather.get("humidity") is not None:
            w_parts.append(f"Humidity: {weather['humidity']}%")
        if weather.get("barometric_pressure") is not None:
            w_parts.append(f"Pressure: {weather['barometric_pressure']} hPa")
        if weather.get("dew_point") is not None:
            w_parts.append(f"Dew Point: {weather['dew_point']}°C")
        lines.append("  " + "  |  ".join(w_parts) if w_parts else "  Weather: N/A")
    else:
        lines.append("  Weather: UNAVAILABLE (BOM scrape may have failed)")

    if track:
        lines.append(
            f"  Track Rating: {track.get('track_rating') or 'N/A'}  "
            f"|  Rail: {track.get('rail_position') or 'N/A'}"
        )
    else:
        lines.append(f"  Track: {cond}  |  Rail: {rail}  (from race card)")

    lines += ["", SEP, ""]
    return "\n".join(lines)


def build_race_package(
    race_id: int,
    console: Optional[Console] = None,
    force_rebuild: bool = False,
) -> str:
    """
    Public function — build (or load) the PACKAGE file for a race.

    1. If PACKAGE already exists in DB and force_rebuild=False → return cached text.
    2. Otherwise build from DB, save to Race_NN_PACKAGE.txt, store in DB.
    Returns the package text string.
    """
    # ── Try cache first ───────────────────────────────────────────────────────
    if not force_rebuild:
        cached = db.get_analysis(race_id, "PACKAGE")
        if cached and cached["raw_text"]:
            if console:
                console.print("[dim]  Package: loaded from DB cache[/dim]")
            return cached["raw_text"]

        # Filesystem fallback
        race = _build_race_dict(race_id)
        venue = race.get("venue", "?")
        rdate = race.get("race_date", "?")
        rnum  = race.get("race_number", 0)
        pkg_path = _package_path(venue, rdate, rnum)
        if pkg_path.exists():
            text = pkg_path.read_text(encoding="utf-8", errors="replace")
            # Back-fill DB from file
            db.insert_analysis(race_id=race_id, analysis_pass="PACKAGE",
                               model="N/A", raw_text=text)
            if console:
                console.print(f"[dim]  Package: loaded from file {pkg_path.name}[/dim]")
            return text

    # ── Build from DB ─────────────────────────────────────────────────────────
    if console:
        console.print("[bold]  Building race package (race card + form + speed map)...[/bold]")

    text = _build_race_package_text(race_id)

    # Persist to DB
    db.insert_analysis(race_id=race_id, analysis_pass="PACKAGE",
                       model="N/A", raw_text=text)

    # Save to filesystem
    race  = _build_race_dict(race_id)
    venue = race.get("venue", "?")
    rdate = race.get("race_date", "?")
    rnum  = race.get("race_number", 0)
    pkg_path = _package_path(venue, rdate, rnum)
    pkg_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(pkg_path, "Race Package — Race Card + Form Guide + Speed Map",
                  venue, rnum, rdate, text, overwrite=True)

    if console:
        console.print(f"[green]  Package saved → {pkg_path.name}[/green]")

    return text


def get_race_package(race_id: int) -> Optional[str]:
    """Retrieve the PACKAGE for a race (DB first, file fallback). None if not found."""
    row = db.get_analysis(race_id, "PACKAGE")
    if row and row["raw_text"]:
        return row["raw_text"]

    race = _build_race_dict(race_id)
    pkg  = _package_path(
        race.get("venue", "?"),
        race.get("race_date", "?"),
        race.get("race_number", 0),
    )
    if pkg.exists():
        return pkg.read_text(encoding="utf-8", errors="replace")
    return None


def _package_path(venue: str, rdate: str, rnum: int) -> Path:
    """Return filesystem path for the PACKAGE file."""
    slug = venue.lower().replace(" ", "_")
    rdir = REPORTS_DIR / rdate / slug
    return rdir / f"Race_{rnum:02d}_PACKAGE.txt"


def _analysis_path(venue: str, rdate: str, rnum: int) -> Path:
    """Return filesystem path for the ANALYSIS (final prediction) file."""
    slug = venue.lower().replace(" ", "_")
    rdir = REPORTS_DIR / rdate / slug
    return rdir / f"Race_{rnum:02d}_ANALYSIS.txt"


def _post_race_input_path(venue: str, rdate: str, rnum: int) -> Path:
    """Return filesystem path for the POST_RACE_INPUT combined file."""
    slug = venue.lower().replace(" ", "_")
    rdir = REPORTS_DIR / rdate / slug
    return rdir / f"Race_{rnum:02d}_POST_RACE_INPUT.txt"


def _post_race_output_path(venue: str, rdate: str, rnum: int) -> Path:
    """Return filesystem path for the POST_RACE output file."""
    slug = venue.lower().replace(" ", "_")
    rdir = REPORTS_DIR / rdate / slug
    return rdir / f"Race_{rnum:02d}_POST_RACE.txt"


# ══════════════════════════════════════════════════════════════════════════════
#  File I/O helpers
# ══════════════════════════════════════════════════════════════════════════════

def _write_report(
    path: Path,
    label: str,
    venue: str,
    race_number: int,
    race_date: str,
    content: str,
    overwrite: bool = False,
) -> bool:
    """
    Write a plain-text file with a standard header block.
    Skips write if file already exists unless overwrite=True.
    Returns True if written, False if skipped.
    """
    if path.exists() and not overwrite:
        return False
    header = "\n".join([
        "╔" + "═" * 78 + "╗",
        f"║  OMNI-FORENSIC ANALYST V325.1 AU-KINETIC-OMNI{' ' * 30}║",
        f"║  {label:<76}║",
        (
            f"║  {venue} R{race_number}  |  {race_date}"
            + " " * max(0, 62 - len(venue) - len(str(race_number)) - len(race_date))
            + "║"
        ),
        "╚" + "═" * 78 + "╝",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + content, encoding="utf-8")
    return True


def _load_pass_text(
    race_id: int,
    pass_name: str,
    venue: str,
    rdate: str,
    rnum: int,
    char_limit: int,
) -> str:
    """
    Load a pass result: DB first, then the saved .txt report file.
    Returns empty-placeholder string if neither source has data.
    """
    row = db.get_analysis(race_id, pass_name)
    if row and row["raw_text"]:
        return row["raw_text"][:char_limit]

    # Old-style individual pass txt fallback
    slug     = venue.lower().replace(" ", "_")
    txt_path = REPORTS_DIR / rdate / slug / f"Race_{rnum:02d}_{pass_name}.txt"
    if txt_path.exists():
        raw   = txt_path.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines()
        body_start = next(
            (i for i, ln in enumerate(lines) if i >= 4 and ln.strip() == ""),
            6,
        ) + 1
        return "\n".join(lines[body_start:])[:char_limit]

    return f"[{pass_name} — not available]"


# ══════════════════════════════════════════════════════════════════════════════
#  Single-pass executor
# ══════════════════════════════════════════════════════════════════════════════

def _run_pass(
    race_id: int,
    pass_name: str,
    user_prompt: str,
    console: Console,
    model: str,
    save_report: bool = True,
    overwrite_file: bool = False,
) -> str:
    """
    Stream one pass to Ollama, store result in DB, save intermediate .txt file.
    DB insert is always additive.
    Returns full raw text.
    """
    label = _PASS_LABELS.get(pass_name, pass_name)
    race  = _build_race_dict(race_id)
    venue = race.get("venue", "?")
    rdate = race.get("race_date", "?")
    rnum  = race.get("race_number", 0)

    console.print()
    console.rule(f"[bold cyan]{label}[/bold cyan]")
    console.print(f"[dim]Model: {model}  |  Race: {venue} R{rnum}  {rdate}[/dim]")

    accumulated: list[str] = []
    start_time  = time.time()
    first_token = [None]

    def on_token(chunk: str) -> None:
        if first_token[0] is None:
            first_token[0] = time.time()
        accumulated.append(chunk)

    with console.status("[bold yellow]Running pass...[/bold yellow]", spinner="dots"):
        try:
            result = ollama.stream_chat(
                system_prompt=SYSTEM_PROMPT,
                user_message=user_prompt,
                model=model,
                on_token=on_token,
                options={"num_ctx": 32768, "temperature": 0.1, "top_p": 0.9},
            )
        except ollama.OllamaError as e:
            console.print(f"[red]Ollama error: {e}[/red]")
            return ""

    elapsed = time.time() - start_time
    load_t  = (first_token[0] - start_time) if first_token[0] else elapsed

    console.print(Panel(
        result,
        title=f"[bold green]{label}[/bold green]",
        border_style="green",
        expand=True,
    ))
    console.print(f"[dim]  {elapsed:.1f}s total  (first token: {load_t:.1f}s)[/dim]")

    # Store to DB (additive — never update existing)
    db.insert_analysis(race_id=race_id, analysis_pass=pass_name,
                       model=model, raw_text=result)

    # Save intermediate .txt (individual pass file — audit trail)
    if save_report:
        from config import report_path as _rp
        path    = _rp(venue, rdate, rnum, pass_name)
        written = _write_report(path, label, venue, rnum, rdate, result,
                                overwrite=overwrite_file)
        if written:
            console.print(f"[dim]  Intermediate pass saved → {path.name}[/dim]")
        else:
            console.print(f"[dim]  File exists, skipped → {path.name}[/dim]")

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — Full 6-pass analysis  (package-file driven)
# ══════════════════════════════════════════════════════════════════════════════

def run_full_analysis(
    race_id: int,
    console: Console,
    model: Optional[str] = None,
    passes: Optional[list[str]] = None,
    overwrite_files: bool = False,
) -> bool:
    """
    Run all 6 passes sequentially.

    Data flow (new pipeline):
      1. Build or load the PACKAGE file once from DB.
      2. Every pass is fed the PACKAGE as its primary context  — no further
         DB queries for race / runner / weather data during pass execution.
      3. Each intermediate pass result is stored in DB for audit and to feed
         subsequent passes.
      4. After PASS_4 (the final prediction), write Race_NN_ANALYSIS.txt —
         the single authoritative output file.  Also store in DB as 'PASS_4'.

    Returns True if all passes completed successfully.
    """
    settings = load_settings()
    model    = model or settings.get("model") or OLLAMA_MODEL
    save_rep = settings.get("save_reports", True)

    if not ollama.ensure_ollama_running():
        console.print("[red]ERROR: Cannot connect to Ollama. Is it installed?[/red]")
        return False

    model = ollama.resolve_model(model)
    console.print(f"[bold]Using model: [cyan]{model}[/cyan][/bold]")

    race  = _build_race_dict(race_id)
    venue = race.get("venue", "?")
    rdate = race.get("race_date", "?")
    rnum  = race.get("race_number", 0)

    # ── STEP 1: Ensure PACKAGE exists ────────────────────────────────────────
    console.print()
    console.rule("[bold yellow]BUILDING RACE PACKAGE[/bold yellow]")
    package_text = build_race_package(race_id, console=console,
                                      force_rebuild=overwrite_files)
    if not package_text:
        console.print("[red]Could not build race package — aborting.[/red]")
        return False

    # Use PACKAGE as the context for all passes (legacy format_race_context NOT used)
    context = package_text

    run_passes = passes or ["PASS_0", "PASS_05", "PASS_1", "PASS_2", "PASS_15", "PASS_4"]

    # Pre-compute silo once — used in PASS_4 template
    analyst_silo = _classify_silo(venue, rdate)

    # ── STEP 2: Execute passes ────────────────────────────────────────────────
    for pass_name in run_passes:

        # Per-pass duplicate guard
        if not overwrite_files and db.get_analysis(race_id, pass_name):
            console.print(
                f"[dim]  {_PASS_LABELS.get(pass_name, pass_name)} already in DB — skipping LLM[/dim]"
            )
            # Restore .txt file from DB if missing
            if save_rep:
                existing = db.get_analysis(race_id, pass_name)
                if existing:
                    from config import report_path as _rp
                    path = _rp(venue, rdate, rnum, pass_name)
                    if not path.exists():
                        label = _PASS_LABELS.get(pass_name, pass_name)
                        _write_report(path, label, venue, rnum, rdate,
                                      existing["raw_text"], overwrite=True)
                        console.print(f"[dim]  Restored from DB → {path.name}[/dim]")
            continue

        # ── PASS 0: Forensic Data Audit ──────────────────────────────────────
        if pass_name == "PASS_0":
            user_msg = PASS_0_TEMPLATE.format(context=context)
            _run_pass(race_id, "PASS_0", user_msg, console, model, save_rep,
                      overwrite_file=overwrite_files)

        # ── PASS 0.5: Tier 1 Macro Sweep ─────────────────────────────────────
        elif pass_name == "PASS_05":
            pass_0   = _get_prior_pass(race_id, "PASS_0")
            user_msg = PASS_05_TEMPLATE.format(
                pass_0=pass_0[:4000],
                context=context,
            )
            _run_pass(race_id, "PASS_05", user_msg, console, model, save_rep,
                      overwrite_file=overwrite_files)

        # ── PASS 1: Forward Draft Canvas ─────────────────────────────────────
        elif pass_name == "PASS_1":
            pass_0   = _get_prior_pass(race_id, "PASS_0")
            pass_05  = _get_prior_pass(race_id, "PASS_05")
            user_msg = PASS_1_TEMPLATE.format(
                pass_0=pass_0[:3000],
                pass_05=pass_05[:2000],
                context=context,
            )
            _run_pass(race_id, "PASS_1", user_msg, console, model, save_rep,
                      overwrite_file=overwrite_files)

        # ── PASS 2: Silo Integrity Audit ─────────────────────────────────────
        elif pass_name == "PASS_2":
            pass_1   = _get_prior_pass(race_id, "PASS_1")
            user_msg = PASS_2_TEMPLATE.format(pass_1=pass_1)
            _run_pass(race_id, "PASS_2", user_msg, console, model, save_rep,
                      overwrite_file=overwrite_files)

        # ── PASS 1.5: Probabilistic Projection ───────────────────────────────
        elif pass_name == "PASS_15":
            pass_1   = _get_prior_pass(race_id, "PASS_1")
            pass_2   = _get_prior_pass(race_id, "PASS_2")
            weather  = _weather_summary(race_id)
            user_msg = PASS_15_TEMPLATE.format(
                weather=weather,
                pass_1_summary=pass_1[:2000],
                pass_2=pass_2[:1500],
            )
            _run_pass(race_id, "PASS_15", user_msg, console, model, save_rep,
                      overwrite_file=overwrite_files)

        # ── PASS 4: Final Canvas Render → ANALYSIS file ───────────────────────
        elif pass_name == "PASS_4":
            pass_0  = _get_prior_pass(race_id, "PASS_0")
            pass_05 = _get_prior_pass(race_id, "PASS_05")
            pass_1  = _get_prior_pass(race_id, "PASS_1")
            pass_2  = _get_prior_pass(race_id, "PASS_2")
            pass_15 = _get_prior_pass(race_id, "PASS_15")

            user_msg = PASS_4_TEMPLATE.format(
                date=rdate,
                venue=venue,
                race_number=rnum,
                distance_m=race.get("distance_m", "?"),
                race_class=race.get("race_class") or "?",
                track_condition=normalise_condition(race.get("track_condition")),
                silo=analyst_silo,
                pass_0_summary=pass_0[:1500],
                pass_05_summary=pass_05[:1500],
                pass_1=pass_1[:4000],
                pass_2=pass_2[:2000],
                pass_15=pass_15[:2000],
                context=context,
            )
            final_text = _run_pass(
                race_id, "PASS_4", user_msg, console, model, save_rep,
                overwrite_file=overwrite_files,
            )

            # ── Write the single ANALYSIS file (final prediction) ─────────────
            if final_text and save_rep:
                ana_path = _analysis_path(venue, rdate, rnum)
                _write_report(
                    ana_path,
                    "ANALYSIS — Final Prediction  [PASS 4 — Final Canvas Render]",
                    venue, rnum, rdate, final_text, overwrite=True,
                )
                console.print()
                console.print(Panel(
                    f"[bold green]ANALYSIS FILE SAVED[/bold green]\n{ana_path}",
                    border_style="green", expand=False,
                ))

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    console.rule("[bold green] ANALYSIS COMPLETE[/bold green]")
    report_dir = REPORTS_DIR / rdate / venue.lower().replace(" ", "_")
    console.print(f"[bold]Key files in:[/bold] {report_dir}")
    console.print(f"  [cyan]Race_{rnum:02d}_PACKAGE.txt[/cyan]   ← race card + form + speed map")
    console.print(f"  [cyan]Race_{rnum:02d}_ANALYSIS.txt[/cyan]  ← final prediction  ✓")

    return True


def run_quick_analysis(race_id: int, console: Console,
                       model: Optional[str] = None) -> bool:
    """Quick mode: PASS_0 + PASS_1 + PASS_4 only (still uses PACKAGE)."""
    return run_full_analysis(
        race_id, console, model,
        passes=["PASS_0", "PASS_1", "PASS_4"],
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Post-race helpers
# ══════════════════════════════════════════════════════════════════════════════

def _format_results_for_prompt(results: dict) -> str:
    """Convert results dict to clean text for the LLM."""
    venue = results.get("venue") or "?"
    rnum  = results.get("race_number") or "?"
    rtime = results.get("race_time") or ""
    lines = [f"{venue}  R{rnum}  {rtime}", "─" * 55]

    for f in results.get("finishers") or []:
        pos    = f.get("position") or "?"
        rn     = f.get("runner_number") or "?"
        name   = f.get("name") or "?"
        bar    = f.get("barrier")
        jockey = f.get("jockey") or "?"
        wo     = f.get("win_odds")
        po     = f.get("place_odds")
        margin = f.get("margin")

        ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(pos, f"{pos}th")
        bar_s   = f" (B{bar})"       if bar    else ""
        wo_s    = f"  Win ${wo:.2f}" if wo     else ""
        po_s    = f"  Plc ${po:.2f}" if po     else ""
        mg_s    = f"  [{margin}]"    if margin else ""
        lines.append(
            f"  {ordinal}:  {rn}. {name}{bar_s}  J: {jockey}{wo_s}{po_s}{mg_s}"
        )

    scratched = results.get("scratched") or []
    if scratched:
        lines += [
            "",
            "  Scratched: " + ", ".join(
                f"{s.get('runner_number','?')}. {s.get('name','?')}"
                for s in scratched
            ),
        ]

    return "\n".join(lines)


def _build_post_race_input_text(
    race_id: int,
    results: dict,
) -> str:
    """
    Combine PACKAGE + ANALYSIS (PASS_4) + Results into one document.
    This is the complete input for the post-race analysis prompt.
    """
    SEP = "═" * 80

    # Load PACKAGE
    package_text = get_race_package(race_id)
    if not package_text:
        package_text = "[PACKAGE not found — rebuild with build_race_package()]"

    # Load ANALYSIS (PASS_4)
    race  = _build_race_dict(race_id)
    venue = race.get("venue", "?")
    rdate = race.get("race_date", "?")
    rnum  = race.get("race_number", 0)
    analysis_text = _load_pass_text(race_id, "PASS_4", venue, rdate, rnum, 8000)

    # Format results
    results_text = _format_results_for_prompt(results)

    return "\n".join([
        SEP,
        "  SECTION A — PRE-RACE PACKAGE (Race Card + Form Guide + Speed Map)",
        SEP, "",
        package_text,
        "", SEP,
        "  SECTION B — PRE-RACE ANALYSIS  (Final Prediction / PASS 4)",
        SEP, "",
        analysis_text,
        "", SEP,
        "  SECTION C — RACE RESULTS",
        SEP, "",
        results_text,
        "", SEP, "",
    ])


def build_post_race_input(
    race_id: int,
    results: dict,
    console: Optional[Console] = None,
    force_rebuild: bool = False,
) -> str:
    """
    Build (or load) the POST_RACE_INPUT file.
    Saved as Race_NN_POST_RACE_INPUT.txt and stored in DB as 'POST_RACE_PACKAGE'.
    Returns the combined text.
    """
    # ── Try cache ─────────────────────────────────────────────────────────────
    if not force_rebuild:
        cached = db.get_analysis(race_id, "POST_RACE_PACKAGE")
        if cached and cached["raw_text"]:
            if console:
                console.print("[dim]  Post-race input: loaded from DB cache[/dim]")
            return cached["raw_text"]

        race  = _build_race_dict(race_id)
        venue = race.get("venue", "?")
        rdate = race.get("race_date", "?")
        rnum  = race.get("race_number", 0)
        pri_path = _post_race_input_path(venue, rdate, rnum)
        if pri_path.exists():
            text = pri_path.read_text(encoding="utf-8", errors="replace")
            db.insert_analysis(race_id=race_id, analysis_pass="POST_RACE_PACKAGE",
                               model="N/A", raw_text=text)
            if console:
                console.print(f"[dim]  Post-race input: loaded from {pri_path.name}[/dim]")
            return text

    # ── Build from parts ──────────────────────────────────────────────────────
    if console:
        console.print("[bold]  Building post-race input (package + analysis + results)...[/bold]")

    text  = _build_post_race_input_text(race_id, results)

    # Persist to DB
    db.insert_analysis(race_id=race_id, analysis_pass="POST_RACE_PACKAGE",
                       model="N/A", raw_text=text)

    # Save to filesystem
    race  = _build_race_dict(race_id)
    venue = race.get("venue", "?")
    rdate = race.get("race_date", "?")
    rnum  = race.get("race_number", 0)
    pri_path = _post_race_input_path(venue, rdate, rnum)
    pri_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(
        pri_path,
        "Post-Race Input — Package + Analysis + Results",
        venue, rnum, rdate, text, overwrite=True,
    )

    if console:
        console.print(f"[green]  Post-race input saved → {pri_path.name}[/green]")

    return text


# ══════════════════════════════════════════════════════════════════════════════
#  Post-race analysis runner
# ══════════════════════════════════════════════════════════════════════════════

def run_post_race_analysis(
    race_id: int,
    results: dict,
    framework_text: str,
    console: Console,
    model: Optional[str] = None,
) -> bool:
    """
    Run the post-race forensic analysis.

    New pipeline:
      1. Build POST_RACE_INPUT  =  PACKAGE + ANALYSIS + Results  (one file).
      2. Stream POST_RACE_TEMPLATE prompt with POST_RACE_INPUT as context.
      3. Save output as Race_NN_POST_RACE.txt and to DB as 'POST_RACE'.

    Returns True on success.
    """
    settings = load_settings()
    model    = model or settings.get("model") or OLLAMA_MODEL
    save_rep = settings.get("save_reports", True)

    if not ollama.ensure_ollama_running():
        console.print("[red]ERROR: Cannot connect to Ollama.[/red]")
        return False

    model = ollama.resolve_model(model)

    race  = _build_race_dict(race_id)
    venue = race.get("venue", "?")
    rdate = race.get("race_date", "?")
    rnum  = race.get("race_number", 0)

    # ── Guard: require PASS_4 / ANALYSIS ─────────────────────────────────────
    pass_4_row = db.get_analysis(race_id, "PASS_4")
    ana_path   = _analysis_path(venue, rdate, rnum)
    if not pass_4_row and not ana_path.exists():
        console.print(
            f"[red]No ANALYSIS found for race_id={race_id}. "
            f"Run full analysis first.[/red]"
        )
        return False

    # ── STEP 1: Build post-race input file ───────────────────────────────────
    console.print()
    console.rule("[bold yellow]BUILDING POST-RACE INPUT FILE[/bold yellow]")
    post_race_input = build_post_race_input(
        race_id, results, console=console, force_rebuild=False,
    )

    # ── Report pass availability from input ───────────────────────────────────
    silo_label  = _classify_silo(venue, rdate)
    day_of_week = _day_name(rdate)

    # For the POST_RACE_TEMPLATE we still pass individual pass summaries
    # (they're embedded in the POST_RACE_INPUT, but the template expects named vars).
    # We load them from DB with tight char limits to stay within context.
    pass_texts = {
        "pass_0":  _load_pass_text(race_id, "PASS_0",  venue, rdate, rnum, 2000),
        "pass_05": _load_pass_text(race_id, "PASS_05", venue, rdate, rnum, 2000),
        "pass_1":  _load_pass_text(race_id, "PASS_1",  venue, rdate, rnum, 3000),
        "pass_2":  _load_pass_text(race_id, "PASS_2",  venue, rdate, rnum, 2000),
        "pass_15": _load_pass_text(race_id, "PASS_15", venue, rdate, rnum, 1500),
        "pass_4":  _load_pass_text(race_id, "PASS_4",  venue, rdate, rnum, 4000),
    }

    # Prepend the full POST_RACE_INPUT to the user prompt so the model has
    # the complete structured document, with named pass vars as fallback.
    actual_results = _format_results_for_prompt(results)

    user_prompt = (
        "## COMPLETE RACE PACKAGE + ANALYSIS + RESULTS\n\n"
        + post_race_input
        + "\n\n## POST-RACE ANALYSIS FRAMEWORK & INSTRUCTIONS\n\n"
        + POST_RACE_TEMPLATE.format(
            framework=framework_text,
            venue=venue,
            race_date=rdate,
            day_of_week=day_of_week,
            race_number=rnum,
            distance_m=race.get("distance_m") or "?",
            track_condition=race.get("track_condition") or "Unknown",
            silo_label=silo_label,
            actual_results=actual_results,
            **pass_texts,
        )
    )

    # ── STEP 2: Stream post-race analysis ─────────────────────────────────────
    console.print()
    console.rule("[bold magenta]POST-RACE FORENSIC ANALYSIS[/bold magenta]")
    console.print(
        f"[dim]Model: {model}  |  {venue} R{rnum}  {rdate}  |  Silo: {silo_label}[/dim]"
    )

    accumulated: list[str] = []
    start_time  = time.time()
    first_token = [None]

    def on_token(chunk: str) -> None:
        if first_token[0] is None:
            first_token[0] = time.time()
        accumulated.append(chunk)

    with console.status(
        "[bold yellow] Running post-race analysis...[/bold yellow]", spinner="dots"
    ):
        try:
            result = ollama.stream_chat(
                system_prompt=POST_RACE_SYSTEM_PROMPT,
                user_message=user_prompt,
                model=model,
                on_token=on_token,
                options={"num_ctx": 32768, "temperature": 0.2, "top_p": 0.9},
            )
        except ollama.OllamaError as e:
            console.print(f"[red]Ollama error: {e}[/red]")
            return False

    elapsed = time.time() - start_time
    load_t  = (first_token[0] - start_time) if first_token[0] else elapsed

    console.print(Panel(
        result,
        title="[bold magenta]POST-RACE FORENSIC ANALYSIS[/bold magenta]",
        border_style="magenta",
        expand=True,
    ))
    console.print(f"[dim]  {elapsed:.1f}s  (first token: {load_t:.1f}s)[/dim]")

    # ── STEP 3: Persist output ─────────────────────────────────────────────────
    db.insert_analysis(race_id=race_id, analysis_pass="POST_RACE",
                       model=model, raw_text=result)

    if save_rep:
        out_path = _post_race_output_path(venue, rdate, rnum)
        _write_report(
            out_path,
            "POST-RACE Forensic Analysis",
            venue, rnum, rdate, result, overwrite=True,
        )
        console.print()
        console.print(Panel(
            f"[bold green]POST-RACE FILE SAVED[/bold green]\n{out_path}",
            border_style="magenta", expand=False,
        ))
        console.print(
            f"  [cyan]Race_{rnum:02d}_POST_RACE_INPUT.txt[/cyan]  ← combined input\n"
            f"  [cyan]Race_{rnum:02d}_POST_RACE.txt[/cyan]       ← analysis output  ✓"
        )

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Misc helpers used by main.py
# ══════════════════════════════════════════════════════════════════════════════

def display_stored_analysis(race_id: int, pass_name: str, console: Console) -> None:
    """Display a previously stored analysis pass from DB."""
    row = db.get_analysis(race_id, pass_name)
    if not row:
        console.print(f"[yellow]No stored analysis for pass {pass_name}[/yellow]")
        return
    label = _PASS_LABELS.get(pass_name, pass_name)
    console.print(Panel(
        row["raw_text"],
        title=f"[bold green]{label}[/bold green]  (stored {row['created_at'][:16]})",
        border_style="blue",
        expand=True,
    ))


_METRO_VENUES: frozenset[str] = frozenset({
    "randwick", "rosehill", "warwick farm", "canterbury",
    "flemington", "caulfield", "moonee valley", "sandown",
    "eagle farm", "doomben", "morphettville", "ascot", "belmont",
})

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "Sunday"]


def _classify_silo(venue: str, race_date: str) -> str:
    """Return 'A (Saturday Metro)', 'B (Wednesday/Friday)', or 'C (Other)'."""
    try:
        from datetime import date as _date
        d   = _date.fromisoformat(race_date)
        dow = d.weekday()  # 0=Mon … 6=Sun
    except Exception:
        return "C (Other)"

    if dow == 5 and venue.lower() in _METRO_VENUES:
        return "A (Saturday Metropolitan — Tier 1)"
    if dow in (2, 4):
        return "B (Wednesday / Friday)"
    return "C (Other Day)"


def _day_name(race_date: str) -> str:
    try:
        from datetime import date as _date
        return _DAY_NAMES[_date.fromisoformat(race_date).weekday()]
    except Exception:
        return "Unknown"
