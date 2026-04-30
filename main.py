"""
main.py — OMNI-FORENSIC Horse Race Analyst CLI

Usage:
  python main.py
  python main.py --location "Randwick" --race 5
  python main.py --location "Randwick" --race 5 --date 2026-04-20
"""

from __future__ import annotations
import argparse
import atexit
import json
import logging
import re
import signal
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from rich import box

# ── Project imports ───────────────────────────────────────────────────────────
import db
import analyst
import ollama_client as ollama
from config import (
    AEST, OLLAMA_MODEL, load_settings, save_settings,
    get_aest_today, get_aest_now, REPORTS_DIR, DB_PATH,
)
from scrapers.sportsbet import (
    search_across_days, scrape_race, scrape_by_event_id,
    extract_event_id_from_url, find_race_on_sportsbet,
    fetch_all_racing_structured,
)
from scrapers.bom import fetch_weather_for_venue, format_weather_summary
from scrapers.racing_com import fetch_sectionals
from scrapers.results import scrape_results, save_results, build_results_url
from analyst import run_post_race_analysis

# ── Date/recency helpers ──────────────────────────────────────────────────────

_RECENT_DAYS = 2  # Today + yesterday are "recent"; older dates get rolled up


def _aest_header() -> str:
    """One-line AEST timestamp + today date for display at top of menus."""
    now = get_aest_now()
    return (
        f"[dim]AEST: {now.strftime('%a %d %b %Y  %H:%M')}  "
        f"│  Today: {now.strftime('%Y-%m-%d')}[/dim]"
    )


def _recency_cutoff() -> str:
    """ISO date string for the oldest day considered 'recent' (yesterday)."""
    return (get_aest_today() - timedelta(days=_RECENT_DAYS - 1)).isoformat()


def _split_races_by_recency(
    races: list[dict],
) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    Split a flat race list into:
      recent  — races on today or yesterday
      older   — {date_str: [races]}  keyed by date, oldest-last ordering within
    """
    cutoff = _recency_cutoff()
    recent: list[dict] = []
    older: dict[str, list[dict]] = {}
    for r in races:
        d = r.get("race_date", "")
        if d >= cutoff:
            recent.append(r)
        else:
            older.setdefault(d, []).append(r)
    return recent, older


def _race_completions(races: list[dict]) -> dict[int, dict]:
    """Batch-fetch completion flags for a list of race dicts."""
    ids = [r["id"] for r in races if r.get("id")]
    return db.get_race_completion_batch(ids)

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s: %(message)s",
)

import io as _io
import sys as _sys
if hasattr(_sys.stdout, "buffer"):
    _utf8_stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    _utf8_stdout = _sys.stdout
console = Console(file=_utf8_stdout, highlight=False)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_shutdown_done = False


def _shutdown() -> None:
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    settings = load_settings()
    model = settings.get("model", OLLAMA_MODEL)
    console.print("\n[yellow]  Shutting down Ollama model...[/yellow]")
    try:
        ollama.stop_ollama(model)
    except Exception:
        pass
    console.print("[dim]Goodbye.[/dim]")


def _signal_handler(sig, frame):
    sys.exit(0)


atexit.register(_shutdown)
signal.signal(signal.SIGINT, _signal_handler)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _signal_handler)

# ── Main-menu escape ─────────────────────────────────────────────────────────

class _GoMainMenu(Exception):
    """Raised anywhere in a nested menu flow to jump straight back to main menu."""


# ── Helper: back/quit/main-menu prompt ───────────────────────────────────────

def _nav_prompt(
    prompt_text: str,
    choices: list[str],
    extra: str = "",
    depth: int = 0,
) -> str:
    """
    Prompt that always accepts B (back) and Q (quit).
    At depth >= 2 (two or more levels below main menu) also shows [M] main menu.

    depth=0  → at main menu level  (no [M])
    depth=1  → one level in        (no [M])
    depth>=2 → two or more levels  ([M] shown, raises _GoMainMenu)
    """
    m_hint = "  [M]ain menu" if depth >= 2 else ""
    full_hint = f"  [dim][B]ack / [Q]uit{m_hint}[/dim]"
    upper_choices = [c.upper() for c in choices]

    while True:
        console.print()
        val = Prompt.ask(
            prompt_text + extra + full_hint, console=console
        ).strip().upper()

        if val == "Q":
            console.print("[dim]Exiting...[/dim]")
            sys.exit(0)
        if val == "B":
            return "BACK"
        if val == "M" and depth >= 2:
            raise _GoMainMenu()
        if val in upper_choices:
            return val
        console.print(
            f"[red]Invalid choice. Enter one of: "
            f"{', '.join(choices + (['M'] if depth >= 2 else []) + ['B', 'Q'])}[/red]"
        )


def _any_prompt(prompt_text: str, depth: int = 0) -> Optional[str]:
    """Free-text prompt. Returns None if B/back, raises _GoMainMenu on M (depth>=2)."""
    m_hint = " / [M]ain" if depth >= 2 else ""
    val = Prompt.ask(
        prompt_text + f"  [dim]([B]ack / [Q]uit{m_hint})[/dim]",
        console=console,
    ).strip()
    if val.upper() == "Q":
        sys.exit(0)
    if val.upper() == "B":
        return None
    if val.upper() == "M" and depth >= 2:
        raise _GoMainMenu()
    return val


# ── Race storage helper ───────────────────────────────────────────────────────

def _store_race_to_db(race_data: dict) -> tuple[int, int]:
    """
    Store scraped race data to DB.
    Returns (meeting_id, race_id).
    """
    venue      = race_data.get("venue") or "Unknown"
    race_date  = race_data.get("race_date") or get_aest_today().isoformat()
    meeting_id = db.insert_meeting(
        venue=venue,
        race_date=race_date,
        sportsbet_meeting_id=race_data.get("sportsbet_meeting_id"),
    )
    race_id = db.insert_race(
        meeting_id=meeting_id,
        race_number=race_data.get("race_number") or 0,
        race_name=race_data.get("race_name"),
        distance_m=race_data.get("distance_m"),
        race_class=race_data.get("race_class"),
        track_condition=race_data.get("track_condition"),
        rail_position=race_data.get("rail_position"),
        prize_money=race_data.get("prize_money"),
        jump_time=race_data.get("jump_time"),
        sportsbet_event_id=race_data.get("sportsbet_event_id"),
    )
    # Store track condition
    if race_data.get("track_condition"):
        db.insert_track_condition(
            venue=venue,
            race_date=race_date,
            race_number=race_data.get("race_number"),
            track_rating=race_data.get("track_condition"),
            rail_position=race_data.get("rail_position"),
        )

    for runner in race_data.get("runners") or []:
        runner_id = db.insert_runner(race_id, runner)
        for run in runner.get("recent_form_raw") or []:
            try:
                db.insert_runner_form(runner_id, run)
            except Exception:
                pass

    return meeting_id, race_id


def _scrape_extras(race_data: dict, race_id: int) -> None:
    """Fetch BOM weather and racing.com sectionals, store to DB."""
    venue      = race_data.get("venue") or ""
    race_date  = race_data.get("race_date") or ""
    race_number = race_data.get("race_number") or 0

    # BOM weather
    with console.status("[bold]Fetching BOM weather...[/bold]", spinner="dots"):
        obs = fetch_weather_for_venue(venue, race_date)
    if obs:
        db.insert_weather(venue, race_date, obs)
        console.print(f"[green] Weather:[/green] {format_weather_summary(obs)}")
    else:
        console.print("[yellow]  Weather unavailable (BOM)[/yellow]")

    # racing.com sectionals
    with console.status("[bold]Fetching racing.com sectionals...[/bold]", spinner="dots"):
        sects = fetch_sectionals(venue, race_date, race_number)
    if sects:
        for s in sects:
            db.insert_sectionals(
                runner_name=s.get("runner_name") or "",
                venue=venue,
                race_date=race_date,
                race_number=race_number,
                l200m=s.get("l200m"),
                l400m=s.get("l400m"),
                l600m=s.get("l600m"),
                source="racing.com",
            )
        console.print(f"[green] Sectionals:[/green] {len(sects)} runners")
    else:
        console.print("[yellow]  Sectionals unavailable (racing.com)[/yellow]")


# ── Race display ──────────────────────────────────────────────────────────────

def _display_race_summary(race_data: dict) -> None:
    """Show a Rich table summary of a scraped race."""
    venue   = race_data.get("venue") or "?"
    rnum    = race_data.get("race_number") or "?"
    rname   = race_data.get("race_name") or ""
    dist    = race_data.get("distance_m") or "?"
    cond    = race_data.get("track_condition") or "?"
    rdate   = race_data.get("race_date") or "?"
    jtime   = race_data.get("jump_time") or "TBC"
    runners = race_data.get("runners") or []
    active  = [r for r in runners if not r.get("scratched")]
    scr     = [r for r in runners if r.get("scratched")]

    console.print()
    console.print(Panel(
        f"[bold white]{venue}  R{rnum}[/bold white]\n"
        f"[cyan]{rname}[/cyan]\n"
        f"Date: {rdate}  |  Distance: {dist}m  |  Condition: {cond}\n"
        f"Jump: {jtime[:19] if jtime else 'TBC'}  |  "
        f"Runners: {len(active)} active, {len(scr)} scratched",
        title="[bold green]Race Found[/bold green]",
        border_style="green",
    ))

    if not active:
        console.print("[red]No active runners found.[/red]")
        return

    tbl = Table(
        "No.", "Name", "Barrier", "Weight", "Jockey", "Win", "Form",
        box=box.SIMPLE_HEAVY, show_header=True, header_style="bold magenta",
    )
    for r in sorted(active, key=lambda x: x.get("runner_number") or 99):
        wo = r.get("win_odds")
        tbl.add_row(
            str(r.get("runner_number") or "-"),
            r.get("sb_name") or r.get("runner_name") or "-",
            str(r.get("barrier") or "-"),
            f"{r.get('weight_kg'):.1f}kg" if r.get("weight_kg") else "-",
            r.get("jockey") or "-",
            f"${wo:.2f}" if wo else "-",
            r.get("form_fig") or "-",
        )
    console.print(tbl)

    if scr:
        console.print(f"[dim]Scratched: {', '.join(r.get('sb_name') or r.get('runner_name') or '?' for r in scr)}[/dim]")


# ── Analysis sub-menu ─────────────────────────────────────────────────────────

def _analysis_menu(race_id: int, race_data: Optional[dict] = None) -> None:
    """Sub-menu for running/viewing analysis for a race."""
    settings = load_settings()
    model    = settings.get("model", OLLAMA_MODEL)

    race = analyst._build_race_dict(race_id)
    venue = race.get("venue", "?")
    rnum  = race.get("race_number", "?")

    while True:
        console.print()
        console.print(Panel(
            f"[bold]{venue} R{rnum}[/bold]\n"
            f"Analysis Options",
            title="[cyan]ANALYSIS MENU[/cyan]",
            border_style="cyan",
        ))
        console.print(" [1] Run Full Analysis  (6 passes)")
        console.print(" [2] Run Quick Analysis (Pass 0 + 1 + Final)")
        console.print(" [3] View Stored Passes")
        console.print(" [4] Re-run Single Pass")
        console.print(" [B] Back")
        console.print(" [Q] Quit")

        choice = _nav_prompt("Select", ["1", "2", "3", "4"])
        if choice == "BACK":
            return

        if choice == "1":
            analyst.run_full_analysis(race_id, console, model)

        elif choice == "2":
            analyst.run_quick_analysis(race_id, console, model)

        elif choice == "3":
            _view_stored_passes(race_id)

        elif choice == "4":
            _rerun_pass_menu(race_id, model)


def _view_stored_passes(race_id: int) -> None:
    """List and display stored analysis passes."""
    rows = db.get_all_analyses(race_id)
    if not rows:
        console.print("[yellow]No stored analyses for this race.[/yellow]")
        return

    passes = [dict(r) for r in rows]
    console.print()
    for i, p in enumerate(passes):
        label = analyst._PASS_LABELS.get(p["analysis_pass"], p["analysis_pass"])
        console.print(f" [{i+1}] {label}  ({p['created_at'][:16]})")

    choice = _any_prompt("Enter number to view")
    if choice is None:
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(passes):
            p = passes[idx]
            console.print(Panel(
                p["raw_text"],
                title=f"[bold green]{analyst._PASS_LABELS.get(p['analysis_pass'], p['analysis_pass'])}[/bold green]",
                border_style="blue",
                expand=True,
            ))
        else:
            console.print("[red]Invalid selection.[/red]")
    except ValueError:
        console.print("[red]Please enter a number.[/red]")


def _rerun_pass_menu(race_id: int, model: str) -> None:
    """Choose and re-run a single pass."""
    passes = [
        ("1", "PASS_0",  "Pass 0 — Forensic Data Audit"),
        ("2", "PASS_05", "Pass 0.5 — Tier 1 Macro Sweep"),
        ("3", "PASS_1",  "Pass 1 — Forward Draft Canvas"),
        ("4", "PASS_2",  "Pass 2 — Silo Integrity Audit"),
        ("5", "PASS_15", "Pass 1.5 — Probabilistic Projection"),
        ("6", "PASS_4",  "Pass 4 — Final Canvas Render"),
    ]
    console.print()
    for num, pname, label in passes:
        console.print(f" [{num}] {label}")
    choice = _nav_prompt("Select pass", [p[0] for p in passes])
    if choice == "BACK":
        return
    selected = next((p for p in passes if p[0] == choice), None)
    if selected:
        analyst.run_full_analysis(race_id, console, model, passes=[selected[1]])


# ── Main analysis flow ────────────────────────────────────────────────────────

def _analyse_flow(location: str, race_number: int,
                  date_str: Optional[str] = None) -> None:
    """Full race selection + scrape + analysis flow."""
    console.print()
    console.print(f"[bold]Searching for:[/bold] [cyan]{location}[/cyan]  R{race_number}")

    race_data: Optional[dict] = None
    found_date: Optional[str] = None

    if date_str:
        # Specific date provided
        with console.status(f"[bold]Checking {date_str}...[/bold]", spinner="dots"):
            race_data = scrape_race(location, race_number, date_str)
        found_date = date_str if race_data else None
    else:
        # Search today → +1 → +2
        today = get_aest_today()
        for delta in range(3):
            d = (today + timedelta(days=delta)).isoformat()
            label = ["Today", "Tomorrow", "Day After"][delta]
            with console.status(f"[bold]Checking {label} ({d})...[/bold]", spinner="dots"):
                race_data = scrape_race(location, race_number, d)
            if race_data:
                found_date = d
                console.print(f"[green] Found on {label} ({d})[/green]")
                break
            else:
                console.print(f"[dim] Not found on {d}[/dim]")

    if not race_data:
        console.print(f"\n[yellow]Race not found on Sportsbet within 3 days.[/yellow]")
        console.print("[dim]You can paste the Sportsbet race URL to proceed manually.[/dim]")

        url = _any_prompt("Paste Sportsbet URL (or [B]ack)")
        if url is None:
            return

        event_id = extract_event_id_from_url(url)
        if not event_id:
            console.print("[red]Could not parse event_id from URL.[/red]")
            console.print("[dim]URL should contain a 7-9 digit number, e.g. .../12345678[/dim]")
            return

        rdate = date_str or get_aest_today().isoformat()
        with console.status("[bold]Fetching race by event ID...[/bold]", spinner="dots"):
            race_data = scrape_by_event_id(event_id, location, rdate, race_number)

        if not race_data:
            console.print(f"[red]Could not fetch race for event_id={event_id}[/red]")
            return
        found_date = rdate

    # Display summary
    _display_race_summary(race_data)

    # ── DB-first check ────────────────────────────────────────────────────────
    existing_race = None
    event_id_check = race_data.get("sportsbet_event_id")
    if event_id_check:
        existing_row = db.get_race_by_event_id(event_id_check)
        if existing_row:
            existing_race = dict(existing_row)
            scraped_str = (existing_race.get("scraped_at") or "")[:16]
            console.print(
                f"\n[cyan]DB hit:[/cyan] This race is already stored "
                f"(race_id={existing_race['id']}, scraped {scraped_str})"
            )
            runner_count = db.count_runners(existing_race["id"])
            console.print(
                f"  {runner_count} runners in DB — historical form will be merged "
                f"automatically during analysis."
            )
            use_cached = Confirm.ask(
                "Use cached DB data (skip re-scrape)?",
                default=True,
                console=console,
            )
            if use_cached:
                _analysis_menu(existing_race["id"], race_data)
                return

    # Confirm scrape (new store)
    if not Confirm.ask("\n[bold]Scrape and store this race?[/bold]", default=True, console=console):
        return

    # Store to DB
    console.print("[bold]Storing race data...[/bold]")
    meeting_id, race_id = _store_race_to_db(race_data)
    runner_count = db.count_runners(race_id)
    console.print(f"[green] Stored:[/green] {runner_count} runners  (race_id={race_id})")

    # Fetch extras (BOM + sectionals)
    _scrape_extras(race_data, race_id)

    # Analysis menu
    _analysis_menu(race_id, race_data)


# ── Browse stored races ───────────────────────────────────────────────────────

def _browse_races_menu() -> None:
    """List stored races with analysis/results/post-race status and select one."""
    rows = db.list_stored_races_deduped(100)
    if not rows:
        console.print("[yellow]No stored races yet.[/yellow]")
        return

    console.print()
    tbl = Table("No.", "Date", "Venue", "Race", "Dist", "Analysed", "Results", "Post-Race",
                box=box.SIMPLE, show_header=True, header_style="bold cyan")
    races = [dict(r) for r in rows]
    for i, r in enumerate(races):
        race_id    = r.get("id")
        venue_slug = (r.get("venue") or "").lower().replace(" ", "_")
        race_date  = r.get("race_date") or ""
        race_num   = r.get("race_number") or 0

        analysed = "[green] [/green]" if db.get_analysis(race_id, "PASS_4") else "[dim]–[/dim]"

        if db.get_race_results(race_id):
            results_col = "[green] [/green]"
        else:
            rpath = REPORTS_DIR / race_date / venue_slug / f"Race_{race_num:02d}_results.json"
            results_col = "[yellow]f[/yellow]" if rpath.exists() else "[dim]–[/dim]"

        post_col = "[green] [/green]" if db.get_analysis(race_id, "POST_RACE") else "[dim]–[/dim]"

        tbl.add_row(
            str(i + 1),
            race_date,
            r.get("venue") or "-",
            f"R{race_num}  {(r.get('race_name') or '')[:22]}",
            f"{r.get('distance_m')}m" if r.get("distance_m") else "-",
            analysed,
            results_col,
            post_col,
        )
    console.print(tbl)
    console.print("[dim] =DB  f=file only  –=missing[/dim]")

    choice = _any_prompt("Enter number to open")
    if choice is None:
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(races):
            r = races[idx]
            _analysis_menu(r["id"])
        else:
            console.print("[red]Invalid selection.[/red]")
    except ValueError:
        console.print("[red]Please enter a number.[/red]")


# ── Past analyses ─────────────────────────────────────────────────────────────

def _past_analyses_menu() -> None:
    """List all stored analysis passes (DB + filesystem-only) and view on demand."""
    import re as _re

    db_rows = db.list_past_analyses(50)
    entries: list[dict] = []
    db_keys: set[tuple] = set()

    for row in db_rows:
        a = dict(row)
        db_keys.add((a["race_id"], a["analysis_pass"]))
        entries.append({
            "source":        "db",
            "race_id":       a["race_id"],
            "analysis_pass": a["analysis_pass"],
            "model":         a.get("model") or "-",
            "created_at":    a.get("created_at") or "",
            "venue":         a.get("venue") or "-",
            "race_date":     a.get("race_date") or "-",
            "race_number":   a.get("race_number"),
            "race_name":     a.get("race_name") or "",
            "file_path":     None,
        })

    _pass_pat = _re.compile(r"Race_(\d+)_(PASS_\w+|POST_RACE)\.txt$")
    if REPORTS_DIR.exists():
        for date_dir in sorted(REPORTS_DIR.iterdir()):
            if not date_dir.is_dir():
                continue
            race_date = date_dir.name
            for venue_dir in sorted(date_dir.iterdir()):
                if not venue_dir.is_dir():
                    continue
                venue_slug = venue_dir.name
                for txt in sorted(venue_dir.glob("Race_*_PASS_*.txt")) + \
                           sorted(venue_dir.glob("Race_*_POST_RACE.txt")):
                    m = _pass_pat.search(txt.name)
                    if not m:
                        continue
                    rnum      = int(m.group(1))
                    pass_name = m.group(2)

                    db_race = db._conn().execute(
                        """SELECT r.id, r.race_name FROM races r
                           JOIN meetings m ON m.id = r.meeting_id
                           WHERE lower(replace(m.venue,' ','_'))=?
                             AND m.race_date=? AND r.race_number=?
                           ORDER BY r.scraped_at DESC LIMIT 1""",
                        (venue_slug, race_date, rnum),
                    ).fetchone()
                    if not db_race:
                        continue
                    race_id = dict(db_race)["id"]

                    if (race_id, pass_name) in db_keys:
                        continue

                    entries.append({
                        "source":        "file",
                        "race_id":       race_id,
                        "analysis_pass": pass_name,
                        "model":         "-",
                        "created_at":    "",
                        "venue":         venue_slug.replace("_", " ").title(),
                        "race_date":     race_date,
                        "race_number":   rnum,
                        "race_name":     dict(db_race).get("race_name") or "",
                        "file_path":     txt,
                    })

    if not entries:
        console.print("[yellow]No analyses found in DB or reports folder.[/yellow]")
        return

    console.print()
    tbl = Table("No.", "Date", "Venue", "Race", "Pass", "Source", "Created",
                box=box.SIMPLE, show_header=True, header_style="bold cyan")
    for i, a in enumerate(entries):
        src_tag = "[green]DB[/green]" if a["source"] == "db" else "[yellow]file[/yellow]"
        tbl.add_row(
            str(i + 1),
            a["race_date"],
            a["venue"],
            f"R{a['race_number']}  {(a['race_name'] or '')[:18]}",
            a["analysis_pass"],
            src_tag,
            (a["created_at"] or "")[:16],
        )
    console.print(tbl)

    choice = _any_prompt("Enter number to view")
    if choice is None:
        return
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(entries)):
            console.print("[red]Invalid selection.[/red]")
            return
        a = entries[idx]
        if a["source"] == "db":
            analyst.display_stored_analysis(a["race_id"], a["analysis_pass"], console)
        else:
            try:
                text = a["file_path"].read_text(encoding="utf-8", errors="replace")
                console.print(Panel(
                    text,
                    title=f"[bold green]{a['venue']} R{a['race_number']} — {a['analysis_pass']} (file)[/bold green]",
                    border_style="blue",
                    expand=True,
                ))
            except Exception as e:
                console.print(f"[red]Cannot read file: {e}[/red]")
    except ValueError:
        console.print("[red]Please enter a number.[/red]")


# ── Settings menu ─────────────────────────────────────────────────────────────

def _settings_menu() -> None:
    settings = load_settings()
    while True:
        console.print()
        console.print(Panel(
            f"Current Model: [cyan]{settings.get('model', OLLAMA_MODEL)}[/cyan]\n"
            f"Save Reports : [cyan]{settings.get('save_reports', True)}[/cyan]\n"
            f"Database     : [dim]{DB_PATH}[/dim]\n"
            f"Reports Dir  : [dim]{REPORTS_DIR}[/dim]",
            title="[bold]Settings[/bold]",
            border_style="yellow",
        ))
        console.print(" [1] Change Ollama Model")
        console.print(" [2] Toggle Save Reports")
        console.print(" [3] List Available Models")
        console.print(" [B] Back")

        choice = _nav_prompt("Select", ["1", "2", "3"])
        if choice == "BACK":
            return

        if choice == "1":
            models = ollama.list_models()
            if models:
                console.print("[bold]Available models:[/bold]")
                for i, m in enumerate(models):
                    console.print(f"  [{i+1}] {m}")
                val = _any_prompt("Enter number or model name")
                if val is None:
                    continue
                try:
                    idx = int(val) - 1
                    if 0 <= idx < len(models):
                        val = models[idx]
                except ValueError:
                    pass
                settings["model"] = val
                save_settings(settings)
                console.print(f"[green]Model set to: {val}[/green]")
            else:
                val = _any_prompt("Enter model name (ollama not running?)")
                if val:
                    settings["model"] = val
                    save_settings(settings)

        elif choice == "2":
            settings["save_reports"] = not settings.get("save_reports", True)
            save_settings(settings)
            console.print(f"[green]Save Reports: {settings['save_reports']}[/green]")

        elif choice == "3":
            models = ollama.list_models()
            if models:
                for m in models:
                    console.print(f"  • {m}")
            else:
                console.print("[yellow]Ollama not running or no models installed.[/yellow]")


# ── Race scanner ──────────────────────────────────────────────────────────────

def _scan_races_flow() -> None:
    """
    Scrape thoroughbred races for a chosen date.

    3-level navigation (mirrors Analyse and Post-Race flows exactly):

      Level 1 — Location list  (venues in the chosen region)
        [A]   Scrape ALL region races not in DB  → Main Menu when done
        [1-N] Select a venue                     → Level 2

      Level 2 — Venue race list
        [A]   Scrape all venue races not in DB   → Location list (refreshed)
        [1-N] Scrape a single race               → stays at venue race list (refreshed)
        [B]   Back                               → Location list

      [B] at Location list → Region list → [B] → Main Menu
    """
    # ── Date selection ────────────────────────────────────────────────────────
    today = get_aest_today()
    console.print()
    console.print(Panel(
        f"[bold]Select date to scan[/bold]\n"
        f"[1] Today ({today.isoformat()})\n"
        f"[2] Tomorrow ({(today + timedelta(days=1)).isoformat()})\n"
        f"[3] Custom date",
        title="[cyan]RACE SCANNER[/cyan]",
        border_style="cyan",
    ))
    date_choice = _nav_prompt("Select", ["1", "2", "3"])
    if date_choice == "BACK":
        return

    if date_choice == "1":
        scan_date = today.isoformat()
    elif date_choice == "2":
        scan_date = (today + timedelta(days=1)).isoformat()
    else:
        val = _any_prompt("Enter date YYYY-MM-DD")
        if val is None:
            return
        try:
            datetime.strptime(val, "%Y-%m-%d")
            scan_date = val
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD.[/red]")
            return

    # ── Fetch all racing ──────────────────────────────────────────────────────
    console.print()
    with console.status(
        f"[bold]Fetching all races for {scan_date}...[/bold]", spinner="dots"
    ):
        try:
            structured = fetch_all_racing_structured(scan_date)
        except Exception as e:
            console.print(f"[red]Failed to fetch races: {e}[/red]")
            return

    if not structured:
        console.print(f"[yellow]No thoroughbred races found for {scan_date}.[/yellow]")
        return

    # ── Region selection ──────────────────────────────────────────────────────
    while True:
        regions = list(structured.keys())
        console.print()
        tbl = Table("No.", "Region", "Venues", "Races",
                    box=box.SIMPLE, show_header=True, header_style="bold cyan")
        for i, region in enumerate(regions):
            total_races = sum(len(evs) for evs in structured[region].values())
            tbl.add_row(str(i + 1), region, str(len(structured[region])), str(total_races))
        console.print(tbl)

        region_choice = _nav_prompt("Select region", [str(i + 1) for i in range(len(regions))])
        if region_choice == "BACK":
            return

        selected_region = regions[int(region_choice) - 1]
        venues = structured[selected_region]

        # ── LEVEL 1 — Location list ───────────────────────────────────────────
        while True:
            venue_names = list(venues.keys())

            # Recount missing each time the location list redraws (stays current)
            venue_missing: dict[str, int] = {}
            total_missing = 0
            for vname, evs in venues.items():
                m = sum(
                    1 for ev in evs
                    if not (ev.get("event_id") and db.get_race_by_event_id(ev["event_id"]))
                )
                venue_missing[vname] = m
                total_missing += m

            console.print()
            console.rule(
                f"[bold cyan]{selected_region}  —  {scan_date}[/bold cyan]"
            )
            console.print(_aest_header())
            console.print()
            console.print(
                f" [A]  Scrape ALL {selected_region} races not in DB  "
                f"([yellow]{total_missing}[/yellow] missing)"
            )
            console.print()

            vtbl = Table(
                "No.", "Venue", "Races", "First Jump", "Not in DB",
                box=box.SIMPLE, show_header=True, header_style="bold magenta",
            )
            for j, vname in enumerate(venue_names):
                evs   = venues[vname]
                first = evs[0].get("jump_time") or ""
                miss  = venue_missing[vname]
                vtbl.add_row(
                    str(j + 1),
                    vname,
                    str(len(evs)),
                    first[11:16] if len(first) > 16 else first,
                    f"[yellow]{miss}[/yellow]" if miss else "[green]0 ✓[/green]",
                )
            console.print(vtbl)
            console.print(" [B]  Back to regions")
            console.print()

            try:
                loc_choice = _nav_prompt(
                    "Select", ["A"] + [str(j + 1) for j in range(len(venue_names))],
                    depth=1,
                )
            except _GoMainMenu:
                return  # straight to main menu
            if loc_choice == "BACK":
                break  # → Region list

            # ── [A] Scrape entire region → Main Menu ──────────────────────────
            if loc_choice == "A":
                if total_missing == 0:
                    console.print(
                        f"[green]✓ All {selected_region} races are already in the DB.[/green]"
                    )
                else:
                    _scrape_all_region_races(venues, scan_date, selected_region)
                return  # ← Main Menu

            selected_venue = venue_names[int(loc_choice) - 1]
            events = venues[selected_venue]

            # ── LEVEL 2 — Venue race list ─────────────────────────────────────
            while True:
                console.print()
                console.rule(
                    f"[bold yellow]{selected_venue}  —  {scan_date}[/bold yellow]"
                )
                console.print(_aest_header())

                not_in_db_venue = [
                    ev for ev in events
                    if not (ev.get("event_id") and db.get_race_by_event_id(ev["event_id"]))
                ]
                console.print()
                console.print(
                    f" [A]  Scrape all {selected_venue} races not in DB  "
                    f"([yellow]{len(not_in_db_venue)}[/yellow] missing)"
                )
                console.print()

                rtbl = Table(
                    "No.", "Race", "Dist", "Jump (AEST)", "In DB?",
                    box=box.SIMPLE, show_header=True, header_style="bold yellow",
                )
                for k, ev in enumerate(events):
                    in_db = ""
                    if ev.get("event_id") and db.get_race_by_event_id(ev["event_id"]):
                        in_db = "[green]✓[/green]"
                    dist     = f"{ev['distance_m']}m" if ev.get("distance_m") else "-"
                    jump     = ev.get("jump_time") or ""
                    jump_str = jump[11:16] if len(jump) > 16 else jump
                    rtbl.add_row(
                        str(k + 1),
                        ev.get("race_name") or f"R{ev.get('race_number',k+1)}",
                        dist,
                        jump_str,
                        in_db,
                    )
                console.print(rtbl)
                console.print(" [B]  Back to location list  [M] Main menu")
                console.print()

                race_choices = ["A"] + [str(k + 1) for k in range(len(events))]
                try:
                    race_choice = _nav_prompt("Select", race_choices, depth=2)
                except _GoMainMenu:
                    return  # straight to main menu

                if race_choice == "BACK":
                    break  # → Location list (redraws with refreshed counts)

                if race_choice == "A":
                    # Scrape all venue races → back to Location list
                    if not not_in_db_venue:
                        console.print(
                            f"[green]✓ All {selected_venue} races are already in the DB.[/green]"
                        )
                    else:
                        _scan_scrape_races(not_in_db_venue, selected_venue, scan_date)
                    break  # ← Location list (loop redraws with refreshed missing counts)

                # Single race → scrape → stay at venue race list (loop continues)
                ev = events[int(race_choice) - 1]
                _scan_scrape_races([ev], selected_venue, scan_date)
                # `continue` implicit — race list redraws with updated ✓ marker

            # Back from venue race list → location list redraws (while True above)
            continue

        # Back from location list → region list redraws (while True above)
        continue


def _scrape_all_region_races(
    venues: dict,
    scan_date: str,
    region_name: str = "",
) -> None:
    """
    Scrape every race not already in the DB across all venues in a region.
    Prints a per-venue summary as it goes, then a final totals panel.
    Called when the user selects [A] at the venue-selection level.
    After this returns, the caller returns to the main menu.
    """
    console.print()
    console.rule(
        f"[bold cyan]SCRAPING ALL {region_name.upper()} RACES — {scan_date}[/bold cyan]"
    )

    grand_stored  = 0
    grand_skipped = 0

    for venue_name, events in venues.items():
        not_in_db = [
            ev for ev in events
            if not (ev.get("event_id") and db.get_race_by_event_id(ev["event_id"]))
        ]
        already_in_db = len(events) - len(not_in_db)

        if not not_in_db:
            console.print(
                f"[dim]  {venue_name}: all {len(events)} race(s) already in DB — skipped[/dim]"
            )
            grand_skipped += already_in_db
            continue

        console.print(
            f"\n[bold magenta]{venue_name}[/bold magenta]  "
            f"— scraping {len(not_in_db)} of {len(events)} race(s)  "
            f"[dim]({already_in_db} already in DB)[/dim]"
        )
        _scan_scrape_races(not_in_db, venue_name, scan_date)
        grand_stored  += len(not_in_db)
        grand_skipped += already_in_db

    console.print()
    console.print(Panel(
        f"[bold green]{grand_stored}[/bold green] race(s) scraped and stored\n"
        f"[dim]{grand_skipped} race(s) already in DB — skipped[/dim]\n\n"
        f"Returning to main menu…",
        title=f"[bold]{region_name}  {scan_date}  — REGION SCRAPE COMPLETE[/bold]",
        border_style="green",
    ))


def _scan_scrape_races(events: list[dict], venue: str, scan_date: str) -> None:
    """
    Scrape and store a list of event stubs for a given venue+date.
    Navigation: stays at the calling venue's race list after completion.
    """
    console.print()
    stored_ids: list[int] = []
    skipped: list[str] = []

    for ev in events:
        event_id   = ev.get("event_id")
        race_num   = ev.get("race_number") or 0
        race_label = ev.get("race_name") or f"R{race_num}"

        # ── DB-first check ────────────────────────────────────────────────────
        if event_id:
            existing = db.get_race_by_event_id(event_id)
            if existing:
                scraped_str = (dict(existing).get("scraped_at") or "")[:16]
                console.print(
                    f"[dim]  {race_label}:[/dim] already in DB "
                    f"(id={existing['id']}, scraped {scraped_str}) — [dim]skipped[/dim]"
                )
                skipped.append(race_label)
                continue

        # ── Scrape from Sportsbet ─────────────────────────────────────────────
        with console.status(
            f"[bold]  Scraping {race_label} ({venue})...[/bold]", spinner="dots"
        ):
            if event_id:
                race_data = scrape_by_event_id(event_id, venue, scan_date, race_num)
            else:
                race_data = scrape_race(venue, race_num, scan_date)

        if not race_data:
            console.print(f"[red]  ✗ {race_label}: scrape failed[/red]")
            continue

        # ── Store to DB ───────────────────────────────────────────────────────
        meeting_id, race_id = _store_race_to_db(race_data)
        runner_count = db.count_runners(race_id)
        stored_ids.append(race_id)
        console.print(
            f"[green]    {race_label}:[/green] "
            f"{runner_count} runners stored  (race_id={race_id})"
        )

    # ── Extras: weather + sectionals ─────────────────────────────────────────
    if stored_ids:
        console.print()
        last_race_data = None
        if events:
            ev = events[-1]
            last_race_data = {"venue": venue, "race_date": scan_date,
                              "race_number": ev.get("race_number") or 0}
        if last_race_data:
            with console.status("[bold]Fetching BOM weather...[/bold]", spinner="dots"):
                from scrapers.bom import fetch_weather_for_venue
                obs = fetch_weather_for_venue(venue, scan_date)
            if obs:
                db.insert_weather(venue, scan_date, obs)
                from scrapers.bom import format_weather_summary
                console.print(f"[green] Weather:[/green] {format_weather_summary(obs)}")
            else:
                console.print("[dim]  Weather unavailable (BOM)[/dim]")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        f"Stored [bold green]{len(stored_ids)}[/bold green] race(s)  |  "
        f"Skipped (already in DB): [dim]{len(skipped)}[/dim]\n"
        + (f"Skipped: {', '.join(skipped)}" if skipped else ""),
        title=f"[bold]{venue}  {scan_date}[/bold]",
        border_style="blue",
    ))

    # ── Option to jump straight to analysis ───────────────────────────────────
    if stored_ids and Confirm.ask(
        "Open analysis menu for last stored race?", default=False, console=console
    ):
        _analysis_menu(stored_ids[-1])


# ── Fetch race results ────────────────────────────────────────────────────────

def _do_scrape_results(r: dict) -> bool:
    """
    Scrape and store results for a single race dict.
    Returns True on success.
    """
    venue      = r.get("venue") or ""
    venue_slug = venue.lower().replace(" ", "_")
    race_date  = r.get("race_date") or ""
    race_num   = r.get("race_number") or 0
    event_id   = r.get("sportsbet_event_id")
    race_id    = r.get("id")

    if not event_id:
        console.print(f"  [yellow]  {venue} R{race_num} — no event_id, skipping[/yellow]")
        return False

    url = build_results_url(venue_slug, race_num, event_id)
    console.print(f"  [dim]URL:[/dim] [cyan]{url}[/cyan]")

    results_path = REPORTS_DIR / race_date / venue_slug / f"Race_{race_num:02d}_results.json"
    if results_path.exists():
        console.print(f"  [dim]Overwriting existing results.json[/dim]")

    with console.status("  Fetching...", spinner="dots"):
        try:
            result = scrape_results(url)
        except RuntimeError as e:
            console.print(f"  [red]✗ Dependency: {e}[/red]")
            return False
        except ValueError as e:
            console.print(f"  [yellow]  Not available yet: {e}[/yellow]")
            return False
        except Exception as e:
            console.print(f"  [red]✗ Error: {e}[/red]")
            return False

    save_results(result, REPORTS_DIR, venue, race_date, race_num)
    if race_id:
        db.insert_race_results(race_id, result)

    finishers = result.get("finishers") or []
    winner    = next((f for f in finishers if f.get("position") == 1), None)
    if winner:
        wo = winner.get("win_odds")
        console.print(
            f"  [green] [/green] {venue} R{race_num} → "
            f"[bold]{winner['name']}[/bold]"
            + (f" @ ${wo:.2f}" if wo else "")
        )
    else:
        console.print(f"  [green] [/green] {venue} R{race_num} — saved")
    return True


def _fetch_results_flow() -> None:
    """
    List races and scrape results from Sportsbet.

    Layout:
    • RECENT (today + yesterday) — individual numbered rows, always shown.
    • OLDER (>2 days) with incomplete results — rolled up as date groups [D1…DN].
    • OLDER fully-complete dates — hidden here; use Historic Archive [8].
    • [A] Scrape all recent races missing results.
    Loops so tick marks refresh after each scrape.
    """
    while True:
        all_races = [dict(r) for r in db.list_stored_races_deduped(300)]
        if not all_races:
            console.print("[yellow]No stored races yet.[/yellow]")
            return

        completions = _race_completions(all_races)
        recent, older_by_date = _split_races_by_recency(all_races)

        older_groups: list[tuple[str, list[dict]]] = []
        for d in sorted(older_by_date.keys(), reverse=True):
            graces = older_by_date[d]
            all_have_results = all(
                completions.get(r["id"], {}).get("has_results", False)
                for r in graces
            )
            if not all_have_results:
                older_groups.append((d, graces))

        console.print()
        console.rule("[bold cyan]FETCH RACE RESULTS[/bold cyan]")
        console.print(_aest_header())

        recent_missing = 0
        if recent:
            console.print(
                f"\n[bold]RECENT[/bold]  "
                f"[dim]{_recency_cutoff()} → {get_aest_today().isoformat()}  "
                f"(today & yesterday)[/dim]"
            )
            tbl = Table(
                "No.", "Date", "Venue", "Race", "Event ID", "Results?",
                box=box.SIMPLE, header_style="bold cyan",
            )
            for i, r in enumerate(recent):
                race_id  = r.get("id")
                race_num = r.get("race_number") or 0
                event_id = r.get("sportsbet_event_id")
                has_db   = completions.get(race_id, {}).get("has_results", False)
                if has_db:
                    res_col = "[green] [/green]"
                else:
                    vslug = (r.get("venue") or "").lower().replace(" ", "_")
                    fp = REPORTS_DIR / r.get("race_date","") / vslug / f"Race_{race_num:02d}_results.json"
                    res_col = "[yellow]f[/yellow]" if fp.exists() else ""
                    recent_missing += 1
                tbl.add_row(
                    str(i + 1),
                    r.get("race_date") or "-",
                    r.get("venue") or "-",
                    f"R{race_num}  {(r.get('race_name') or '')[:22]}",
                    str(event_id) if event_id else "[dim]–[/dim]",
                    res_col,
                )
            console.print(tbl)
        else:
            console.print("[dim]\nNo races in the last 2 days.[/dim]")

        if older_groups:
            console.print(
                f"\n[bold]OLDER DATES[/bold]  "
                f"[dim](select group [Dx] to expand)[/dim]"
            )
            g_tbl = Table(
                "Grp", "Date", "Venues", "Races", "Missing Results",
                box=box.SIMPLE, header_style="bold yellow",
            )
            for gi, (d, graces) in enumerate(older_groups):
                venues  = len({r.get("venue") for r in graces})
                missing = sum(
                    1 for r in graces
                    if not completions.get(r["id"], {}).get("has_results", False)
                )
                g_tbl.add_row(
                    f"D{gi + 1}", d, str(venues), str(len(graces)),
                    f"[yellow]{missing}[/yellow]" if missing else "[green]0 [/green]",
                )
            console.print(g_tbl)
            console.print(
                "[dim]  Dates with all results complete are in Historic Archive [8][/dim]"
            )

        console.print("\n[dim]  =DB  f=file only  blank=missing[/dim]")
        console.print()
        console.print(f" [A]  Scrape all missing recent results ({recent_missing} races)")
        if recent:
            console.print(f" [1-{len(recent)}]  Scrape specific recent race")
        if older_groups:
            console.print(f" [D1-D{len(older_groups)}]  Expand older date group")
        console.print(" [B]  Back")
        console.print()

        raw = Prompt.ask(
            "Select  [dim]([B]ack / [Q]uit)[/dim]",
            console=console,
        ).strip().upper()

        if raw == "Q":
            sys.exit(0)
        if raw == "B":
            return

        if raw == "A":
            targets = [
                r for r in recent
                if not completions.get(r.get("id"), {}).get("has_results", False)
            ]
            if not targets:
                console.print("[green] All recent races already have results.[/green]")
                continue
            console.print(f"\n[bold]Scraping {len(targets)} race(s)...[/bold]")
            for r in targets:
                _do_scrape_results(r)
            console.print()
            console.rule("[bold green] BATCH SCRAPE COMPLETE[/bold green]")
            continue

        if raw.startswith("D") and raw[1:].isdigit():
            gi = int(raw[1:]) - 1
            if not (0 <= gi < len(older_groups)):
                console.print("[red]Invalid group.[/red]")
                continue
            d, graces = older_groups[gi]
            g_completions = _race_completions(graces)
            console.print(f"\n[bold yellow]{d}[/bold yellow] — {len(graces)} race(s)")
            exp_tbl = Table(
                "No.", "Venue", "Race", "Event ID", "Results?",
                box=box.SIMPLE, header_style="bold yellow",
            )
            for ei, r in enumerate(graces):
                race_num = r.get("race_number") or 0
                event_id = r.get("sportsbet_event_id")
                has_res  = g_completions.get(r["id"], {}).get("has_results", False)
                exp_tbl.add_row(
                    f"E{ei + 1}",
                    r.get("venue") or "-",
                    f"R{race_num}  {(r.get('race_name') or '')[:22]}",
                    str(event_id) if event_id else "[dim]–[/dim]",
                    "[green] [/green]" if has_res else "",
                )
            console.print(exp_tbl)
            console.print()
            console.print(f" [A]  Scrape all missing in {d}")
            console.print(f" [E1-E{len(graces)}]  Scrape specific race")
            console.print(" [B]  Back to list")
            console.print()
            sub = Prompt.ask("Select  [dim]([B]ack)[/dim]", console=console).strip().upper()
            if sub == "Q":
                sys.exit(0)
            if sub == "B":
                continue
            if sub == "A":
                targets = [
                    r for r in graces
                    if not g_completions.get(r.get("id"), {}).get("has_results", False)
                ]
                if targets:
                    for r in targets:
                        _do_scrape_results(r)
                else:
                    console.print("[green] All races in this date already have results.[/green]")
            elif sub.startswith("E") and sub[1:].isdigit():
                ei = int(sub[1:]) - 1
                if 0 <= ei < len(graces):
                    _do_scrape_results(graces[ei])
                else:
                    console.print("[red]Invalid selection.[/red]")
            else:
                console.print("[red]Invalid choice.[/red]")
            continue

        try:
            idx = int(raw) - 1
        except ValueError:
            console.print("[red]Invalid choice.[/red]")
            continue
        if not (0 <= idx < len(recent)):
            console.print("[red]Invalid selection.[/red]")
            continue
        r = recent[idx]
        console.print(f"\n[bold]{r.get('venue')} R{r.get('race_number')}[/bold]")
        _do_scrape_results(r)
        # Loop — refreshed table shows the new tick


# ── Post-race analysis ───────────────────────────────────────────────────────

_POST_RACE_FRAMEWORK_PATH = Path(__file__).parent / "post-race-analysis.txt"


def _load_framework() -> str:
    """Load post-race analysis framework text from file."""
    if _POST_RACE_FRAMEWORK_PATH.exists():
        return _POST_RACE_FRAMEWORK_PATH.read_text(encoding="utf-8").strip()
    return "[Post-race framework file not found — proceeding with system-prompt guidance only]"


def _get_eligible_post_race_races(include_completed: bool = False) -> list[dict]:
    """
    Return races that have PASS_4 + results available.
    By default only returns races without a POST_RACE yet.
    """
    db.init_db()

    if include_completed:
        rows = db._conn().execute(
            """SELECT r.id, r.race_number, r.race_name, r.distance_m,
                      r.track_condition, r.sportsbet_event_id,
                      m.venue, m.race_date
               FROM races r
               JOIN meetings m ON m.id = r.meeting_id
               WHERE r.id IN (
                   SELECT race_id FROM analysis_results WHERE analysis_pass = 'PASS_4'
               )
               AND r.id IN (
                   SELECT MAX(r2.id)
                   FROM races r2
                   JOIN meetings m2 ON m2.id = r2.meeting_id
                   GROUP BY
                     CASE WHEN r2.sportsbet_event_id IS NOT NULL
                          THEN CAST(r2.sportsbet_event_id AS TEXT)
                          ELSE m2.venue || '|' || m2.race_date || '|' || CAST(r2.race_number AS TEXT)
                     END
               )
               ORDER BY m.race_date DESC, m.venue, r.race_number""",
        ).fetchall()
    else:
        rows = db.list_races_pending_post_race()

    eligible: list[dict] = []
    for row in rows:
        r = dict(row)

        results_data = db.get_race_results(r["id"])
        if results_data:
            r["results_data"]   = results_data
            r["results_path"]   = None
            r["results_source"] = "db"
        else:
            venue_slug   = (r.get("venue") or "").lower().replace(" ", "_")
            race_date    = r.get("race_date") or ""
            race_num     = r.get("race_number") or 0
            results_path = REPORTS_DIR / race_date / venue_slug / f"Race_{race_num:02d}_results.json"
            if not results_path.exists():
                continue
            r["results_data"]   = None
            r["results_path"]   = results_path
            r["results_source"] = "file"

        post_race_row     = db.get_analysis(r["id"], "POST_RACE")
        r["has_post_race"] = post_race_row is not None
        eligible.append(r)
    return eligible


def _run_single_post_race(race: dict, framework: str) -> None:
    """Load results (DB-first, filesystem fallback) and run post-race analysis."""
    import json as _json

    if race.get("results_data"):
        results = race["results_data"]
    else:
        results_path = race["results_path"]
        try:
            results = _json.loads(results_path.read_text(encoding="utf-8"))
        except Exception as e:
            console.print(f"[red]Cannot read {results_path}: {e}[/red]")
            return

    settings = load_settings()
    model    = settings.get("model", OLLAMA_MODEL)

    run_post_race_analysis(
        race_id=race["id"],
        results=results,
        framework_text=framework,
        console=console,
        model=model,
    )


def _post_race_venue_loop(venue: str, framework: str) -> None:
    """
    Inner post-race loop for one venue.
    Re-fetches pending races each iteration so counts stay accurate.

    Navigation:
      [A]   Run all venue races  → runs all → returns to Location list (caller).
      [1-N] Run single race      → runs it  → stays at venue race list (refreshed).
      [B]   Back                 → Location list immediately.
    """
    while True:
        all_pending = _get_eligible_post_race_races(include_completed=False)
        venue_races = [r for r in all_pending if r.get("venue") == venue]

        console.print()
        console.rule(f"[bold magenta]{venue.upper()} — PENDING POST-RACE[/bold magenta]")
        console.print(_aest_header())

        if not venue_races:
            console.print(f"\n[green]✓ All {venue} post-race analyses are complete.[/green]")
            console.print("[dim]Returning to location list…[/dim]")
            return

        console.print()
        console.print(
            f" [A]  Run all {venue} post-race analyses  "
            f"([yellow]{len(venue_races)}[/yellow] pending)"
        )
        console.print()

        tbl = Table(
            "No.", "Date", "Race", "Silo",
            box=box.SIMPLE, header_style="bold magenta",
        )
        for i, r in enumerate(venue_races):
            rdate = r.get("race_date") or ""
            rnum  = r.get("race_number") or 0
            silo  = analyst._classify_silo(venue, rdate).split("(")[0].strip()
            tbl.add_row(
                str(i + 1),
                rdate,
                f"R{rnum}  {(r.get('race_name') or '')[:32]}",
                silo,
            )
        console.print(tbl)
        console.print(" [B]  Back to location list  [M] Main menu")
        console.print()

        valid = ["A"] + [str(i + 1) for i in range(len(venue_races))]
        try:
            choice = _nav_prompt("Select", valid, depth=2)
        except _GoMainMenu:
            raise  # propagate up through _post_race_flow to main menu

        if choice == "BACK":
            return

        # ── [A] Run all venue post-race → back to Location list ───────────────
        if choice == "A":
            console.print(
                f"\n[bold]Running {len(venue_races)} post-race analysis(es) for {venue}...[/bold]\n"
            )
            for r in venue_races:
                rnum  = r.get("race_number") or 0
                rdate = r.get("race_date") or ""
                console.print(Panel(
                    f"[bold]{venue}  R{rnum}  {rdate}[/bold]\nrace_id={r['id']}",
                    title="[magenta]POST-RACE[/magenta]",
                    border_style="magenta",
                ))
                _run_single_post_race(r, framework)
            console.rule(f"[bold green]✓ {venue.upper()} POST-RACE BATCH COMPLETE[/bold green]")
            return  # ← Location list

        # ── [1-N] Single race → stays at venue race list ──────────────────────
        try:
            idx = int(choice) - 1
        except ValueError:
            console.print("[red]Invalid selection.[/red]")
            continue
        if not (0 <= idx < len(venue_races)):
            console.print("[red]Invalid selection.[/red]")
            continue

        r     = venue_races[idx]
        rnum  = r.get("race_number") or 0
        rdate = r.get("race_date") or ""
        console.print(Panel(
            f"[bold]{venue}  R{rnum}  {rdate}[/bold]\nrace_id={r['id']}",
            title="[magenta]POST-RACE[/magenta]",
            border_style="magenta",
        ))
        _run_single_post_race(r, framework)
        # Loop continues — venue race list redraws, completed race disappears


def _post_race_flow() -> None:
    """
    Post-race analysis — 3-level navigation matching Analyse and Scrape flows.

      Level 1 — Location list
        [A]   Run ALL pending post-race analyses  → Main Menu when done
        [1-N] Select a venue                      → Level 2

      Level 2 — Venue race list  (handled by _post_race_venue_loop)
        [A]   Run all venue post-race             → Location list (refreshed)
        [1-N] Run single race                     → stays at venue race list (refreshed)
        [B]   Back                                → Location list
    """
    framework = _load_framework()

    while True:
        with console.status("[bold]Scanning pending post-race races...[/bold]", spinner="dots"):
            pending = _get_eligible_post_race_races(include_completed=False)

        # Group by venue, preserving date-first order
        by_venue: dict[str, list[dict]] = {}
        for r in pending:
            by_venue.setdefault(r.get("venue") or "Unknown", []).append(r)
        venue_list = list(by_venue.keys())

        console.print()
        console.rule("[bold magenta]POST-RACE ANALYSIS[/bold magenta]")
        console.print(_aest_header())
        console.print()

        # ── Empty state ───────────────────────────────────────────────────────
        if not pending:
            console.print(Panel(
                "[bold green]✓ All eligible races have been post-analysed.[/bold green]\n\n"
                "Use [bold]Fetch Race Results [7][/bold] to bring in new results,\n"
                "then return here to run the post-race analysis.",
                title="[magenta]POST-RACE ANALYSIS[/magenta]",
                border_style="magenta",
            ))
            console.print()
            console.print(" [B] Back")
            console.print()
            raw = Prompt.ask(
                "Select  [dim]([B]ack / [Q]uit)[/dim]", console=console
            ).strip().upper()
            if raw == "Q":
                sys.exit(0)
            return

        # ── Location list ─────────────────────────────────────────────────────
        console.print(
            f" [A]  Run ALL pending post-race analyses  "
            f"([yellow]{len(pending)}[/yellow] races across {len(venue_list)} venue(s))"
        )
        console.print()

        vtbl = Table(
            "No.", "Venue", "Pending", "Dates",
            box=box.SIMPLE, header_style="bold magenta",
        )
        for i, venue in enumerate(venue_list):
            races  = by_venue[venue]
            dates  = ", ".join(sorted({r.get("race_date","") for r in races}))
            vtbl.add_row(
                str(i + 1),
                venue,
                str(len(races)),
                dates,
            )
        console.print(vtbl)
        console.print(" [B]  Back")
        console.print()

        valid = ["A"] + [str(i + 1) for i in range(len(venue_list))]
        try:
            choice = _nav_prompt("Select", valid, depth=1)
        except _GoMainMenu:
            return

        if choice == "BACK":
            return

        # ── [A] Run everything → Main Menu ────────────────────────────────────
        if choice == "A":
            console.print(
                f"\n[bold]Running post-race analysis for all "
                f"{len(pending)} pending race(s)...[/bold]\n"
            )
            for r in pending:
                venue = r.get("venue") or "?"
                rnum  = r.get("race_number") or 0
                rdate = r.get("race_date") or ""
                console.print(Panel(
                    f"[bold]{venue}  R{rnum}  {rdate}[/bold]\nrace_id={r['id']}",
                    title="[magenta]POST-RACE[/magenta]",
                    border_style="magenta",
                ))
                _run_single_post_race(r, framework)
            console.print()
            console.rule("[bold green]✓ POST-RACE BATCH COMPLETE[/bold green]")
            return  # ← Main Menu

        # ── [1-N] Venue selected → venue race list ────────────────────────────
        try:
            idx = int(choice) - 1
        except ValueError:
            console.print("[red]Invalid choice.[/red]")
            continue
        if not (0 <= idx < len(venue_list)):
            console.print("[red]Invalid selection.[/red]")
            continue

        try:
            _post_race_venue_loop(venue_list[idx], framework)
        except _GoMainMenu:
            return  # propagate all the way back to main menu
        # Returns here when user hits [B] → location list redraws


# ── Pending analysis discovery ───────────────────────────────────────────────

def _find_pending_analysis_races() -> list[dict]:
    """
    Return all DB races that have no PASS_4 in analysis_results.
    Deduplicated by sportsbet_event_id (or venue+date+race_number).
    Today's races sort first.
    """
    db.init_db()
    from config import get_aest_today
    today = get_aest_today().isoformat()
    rows = db._conn().execute(
        """SELECT r.id, r.race_number, r.race_name, r.distance_m,
                  m.venue, m.race_date
           FROM races r
           JOIN meetings m ON m.id = r.meeting_id
           WHERE r.id NOT IN (
               SELECT race_id FROM analysis_results WHERE analysis_pass = 'PASS_4'
           )
           AND r.id IN (
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
             m.race_date DESC, m.venue, r.race_number""",
        (today,),
    ).fetchall()

    pending = []
    for row in rows:
        r = dict(row)
        venue_slug = r["venue"].lower().replace(" ", "_")
        rnum = r["race_number"]
        race_date = r["race_date"]

        venue_dir = REPORTS_DIR / race_date / venue_slug
        passes_on_disk = {
            p for p in ["PASS_0", "PASS_05", "PASS_1", "PASS_2", "PASS_15", "PASS_4"]
            if (venue_dir / f"Race_{rnum:02d}_{p}.txt").exists()
        }

        pending.append({
            "race_id":     r["id"],
            "venue":       r["venue"],
            "venue_slug":  venue_slug,
            "race_date":   race_date,
            "race_number": rnum,
            "race_name":   r.get("race_name") or f"R{rnum}",
            "passes_disk": passes_on_disk,
        })

    return pending


def _analyse_venue_loop(venue: str, model: str) -> None:
    """
    depth=2 (venue list is 2 levels from main menu).
    [A] all → Location list.  [1-N] single → stays here.  [B] → Location list.
    """
    while True:
        all_pending = _find_pending_analysis_races()
        venue_races = [r for r in all_pending if r["venue"] == venue]

        console.print()
        console.rule(f"[bold cyan]{venue.upper()} — PENDING RACES[/bold cyan]")
        console.print(_aest_header())

        if not venue_races:
            console.print(f"\n[green]✓ All {venue} races have been analysed.[/green]")
            console.print("[dim]Returning to location list…[/dim]")
            return

        console.print()
        console.print(
            f" [A]  Analyse all {venue} races  "
            f"([yellow]{len(venue_races)}[/yellow] pending)"
        )
        console.print()

        tbl = Table(
            "No.", "Date", "Race", "Passes on Disk",
            box=box.SIMPLE, header_style="bold cyan",
        )
        for i, r in enumerate(venue_races):
            passes_str = (
                ", ".join(sorted(r["passes_disk"])) if r["passes_disk"] else "—"
            )
            tbl.add_row(
                str(i + 1),
                r["race_date"],
                f"R{r['race_number']}  {r['race_name'][:30]}",
                passes_str,
            )
        console.print(tbl)
        console.print(" [B]  Back to location list  [M] Main menu")
        console.print()

        valid = ["A"] + [str(i + 1) for i in range(len(venue_races))]
        choice = _nav_prompt("Select", valid, depth=2)

        if choice == "BACK":
            return

        settings = load_settings()
        m = settings.get("model", model)

        if choice == "A":
            console.print(
                f"\n[bold]Analysing all {len(venue_races)} {venue} race(s)...[/bold]\n"
                f"[dim]Model: {m}[/dim]\n"
            )
            for r in venue_races:
                console.print(Panel(
                    f"[bold]{r['venue']}  R{r['race_number']}  {r['race_date']}[/bold]\n"
                    f"race_id={r['race_id']}",
                    title="[cyan]ANALYSING[/cyan]", border_style="cyan",
                ))
                analyst.run_full_analysis(r["race_id"], console, m)
            console.rule(f"[bold green]✓ {venue.upper()} BATCH COMPLETE[/bold green]")
            return

        try:
            idx = int(choice) - 1
        except ValueError:
            console.print("[red]Invalid selection.[/red]")
            continue
        if not (0 <= idx < len(venue_races)):
            console.print("[red]Invalid selection.[/red]")
            continue

        r = venue_races[idx]
        console.print(Panel(
            f"[bold]{r['venue']}  R{r['race_number']}  {r['race_date']}[/bold]\n"
            f"race_id={r['race_id']}",
            title="[cyan]ANALYSING[/cyan]", border_style="cyan",
        ))
        analyst.run_full_analysis(r["race_id"], console, m)
        # Loop continues → table redraws, completed race disappears


def _analyse_pending_flow() -> None:
    """
    Analyse pending races (scraped to DB but not yet PASS_4).

    Top-level layout
    ─────────────────────────────────────
    [A]  Analyse all races   (N pending)

    [1]  Randwick             (3 races)
    [2]  Canberra             (2 races)
    [3]  Eagle Farm           (1 race)
    ─────────────────────────────────────
    [N]  Enter a new race manually
    [B]  Back
    ─────────────────────────────────────

    Selecting [A] → runs everything → returns to MAIN MENU.
    Selecting a venue → opens _analyse_venue_loop() → returns here.
    """
    settings = load_settings()
    model    = settings.get("model", OLLAMA_MODEL)

    while True:
        with console.status(
            "[bold]Scanning pending races...[/bold]", spinner="dots"
        ):
            pending = _find_pending_analysis_races()

        # Group by venue, preserving the order races come back in (today-first)
        by_venue: dict[str, list[dict]] = {}
        for r in pending:
            by_venue.setdefault(r["venue"], []).append(r)
        venue_list = list(by_venue.keys())

        console.print()
        console.rule("[bold cyan]ANALYSE A RACE[/bold cyan]")
        console.print(_aest_header())
        console.print()

        if pending:
            console.print(
                f" [A]  Analyse all races  "
                f"([yellow]{len(pending)}[/yellow] pending)"
            )
            console.print()
            for i, venue in enumerate(venue_list):
                count = len(by_venue[venue])
                console.print(
                    f" [{i + 1}]  {venue}  "
                    f"([dim]{count} race{'s' if count != 1 else ''}[/dim])"
                )
        else:
            console.print(
                "[green]✓ All scraped races have been fully analysed.[/green]\n"
            )

        console.print()
        console.print(" [N]  Enter a new race manually")
        console.print(" [B]  Back")
        console.print()

        valid_choices = (
            ["A"] + [str(i + 1) for i in range(len(venue_list))]
            if pending else []
        ) + ["N"]

        try:
            choice = _nav_prompt("Select", valid_choices, depth=1)
        except _GoMainMenu:
            return

        if choice == "BACK":
            return

        # ── [A] Analyse every pending race ───────────────────────────────────
        if choice == "A" and pending:
            settings = load_settings()
            m = settings.get("model", model)
            console.print(
                f"\n[bold]Running analysis for {len(pending)} race(s)...[/bold]\n"
                f"[dim]Model: {m}[/dim]\n"
            )
            for r in pending:
                console.print(Panel(
                    f"[bold]{r['venue']}  R{r['race_number']}  {r['race_date']}[/bold]\n"
                    f"race_id={r['race_id']}",
                    title="[cyan]ANALYSING[/cyan]",
                    border_style="cyan",
                ))
                analyst.run_full_analysis(r["race_id"], console, m)
            console.rule("[bold green]✓ BATCH ANALYSIS COMPLETE[/bold green]")
            return  # Back to main menu after full batch

        # ── [N] Manual race entry ─────────────────────────────────────────────
        elif choice == "N":
            location = _any_prompt(
                "Enter venue name (e.g. Randwick, Flemington, Grafton)"
            )
            if location is None:
                continue
            race_num_str = _any_prompt("Enter race number")
            if race_num_str is None:
                continue
            try:
                race_num = int(race_num_str)
            except ValueError:
                console.print("[red]Race number must be an integer.[/red]")
                continue
            date_str = _any_prompt(
                "Enter date YYYY-MM-DD (or press Enter for auto-detect)"
            )
            if date_str == "":
                date_str = None
            if date_str and date_str.upper() not in ("B", "Q", ""):
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    console.print("[red]Invalid date format. Use YYYY-MM-DD.[/red]")
                    continue
            _analyse_flow(location, race_num, date_str or None)

        # ── Venue selection ───────────────────────────────────────────────────
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(venue_list):
                _analyse_venue_loop(venue_list[idx], model)
                # Returns here after user hits [B] inside venue loop
            else:
                console.print("[red]Invalid selection.[/red]")

        else:
            console.print("[red]Invalid choice.[/red]")


# ── Historic archive ──────────────────────────────────────────────────────────

def _historic_archive_menu() -> None:
    """
    Browse fully-completed races: PASS_4 + results + POST_RACE all done.
    Grouped by date for readability.  Select a date to expand, then a race to view.
    """
    while True:
        rows = db.list_completed_races(300)
        if not rows:
            console.print(Panel(
                "No fully-completed races in the archive yet.\n\n"
                "A race is archived once it has:\n"
                "  • PASS_4 analysis\n"
                "  • Results scraped\n"
                "  • POST_RACE analysis\n",
                title="[bold]HISTORIC ARCHIVE[/bold]",
                border_style="blue",
            ))
            return

        races = [dict(r) for r in rows]

        by_date: dict[str, list[dict]] = {}
        for r in races:
            by_date.setdefault(r.get("race_date", "?"), []).append(r)

        console.print()
        console.rule("[bold blue]HISTORIC ARCHIVE[/bold blue]")
        console.print(_aest_header())
        console.print(
            f"\n[dim]{len(races)} fully-completed races across {len(by_date)} date(s)[/dim]\n"
        )

        date_list = sorted(by_date.keys(), reverse=True)
        tbl = Table(
            "No.", "Date", "Venues", "Races",
            box=box.SIMPLE, header_style="bold blue",
        )
        for i, d in enumerate(date_list):
            graces  = by_date[d]
            venues  = len({r.get("venue") for r in graces})
            tbl.add_row(str(i + 1), d, str(venues), str(len(graces)))
        console.print(tbl)
        console.print()

        raw = Prompt.ask(
            "Select date [1-N] to expand  [dim]([B]ack / [Q]uit)[/dim]",
            console=console,
        ).strip().upper()
        if raw == "Q":
            sys.exit(0)
        if raw == "B":
            return

        try:
            di = int(raw) - 1
        except ValueError:
            console.print("[red]Invalid choice.[/red]")
            continue
        if not (0 <= di < len(date_list)):
            console.print("[red]Invalid selection.[/red]")
            continue

        d = date_list[di]
        graces = by_date[d]
        console.print(f"\n[bold blue]{d}[/bold blue] — {len(graces)} race(s)\n")
        race_tbl = Table(
            "No.", "Venue", "Race", "Dist", "Track",
            box=box.SIMPLE, header_style="bold blue",
        )
        for ri, r in enumerate(graces):
            race_tbl.add_row(
                str(ri + 1),
                r.get("venue") or "-",
                f"R{r.get('race_number')}  {(r.get('race_name') or '')[:24]}",
                f"{r.get('distance_m')}m" if r.get("distance_m") else "-",
                r.get("track_condition") or "-",
            )
        console.print(race_tbl)
        console.print()

        raw2 = Prompt.ask(
            "Select race [1-N] to view analyses  [dim]([B]ack)[/dim]",
            console=console,
        ).strip().upper()
        if raw2 == "Q":
            sys.exit(0)
        if raw2 == "B":
            continue
        try:
            ri = int(raw2) - 1
        except ValueError:
            console.print("[red]Invalid choice.[/red]")
            continue
        if not (0 <= ri < len(graces)):
            console.print("[red]Invalid selection.[/red]")
            continue

        race = graces[ri]
        race_id = race["id"]

        passes = db.get_all_analyses(race_id)
        if not passes:
            console.print("[yellow]No analyses stored for this race.[/yellow]")
            continue
        for pi, p in enumerate(passes):
            pd = dict(p)
            label = analyst._PASS_LABELS.get(pd["analysis_pass"], pd["analysis_pass"])
            console.print(f"  [{pi + 1}] {label}  [dim]({pd['created_at'][:16]})[/dim]")
        console.print()

        raw3 = Prompt.ask(
            "Select pass to view  [dim]([B]ack)[/dim]",
            console=console,
        ).strip().upper()
        if raw3 == "Q":
            sys.exit(0)
        if raw3 == "B":
            continue
        try:
            pi = int(raw3) - 1
        except ValueError:
            console.print("[red]Invalid choice.[/red]")
            continue
        if 0 <= pi < len(passes):
            p = dict(passes[pi])
            label = analyst._PASS_LABELS.get(p["analysis_pass"], p["analysis_pass"])
            console.print(Panel(
                p["raw_text"],
                title=f"[bold green]{race.get('venue')} R{race.get('race_number')} — {label}[/bold green]",
                border_style="blue",
                expand=True,
            ))
            Prompt.ask("[dim]Press Enter to continue[/dim]", console=console)
        else:
            console.print("[red]Invalid selection.[/red]")


# ── Natural-language DB query ─────────────────────────────────────────────────

_NL_SCHEMA = """
SQLite database tables:
  meetings(id, venue TEXT, race_date TEXT 'YYYY-MM-DD', scraped_at)
  races(id, meeting_id→meetings.id, race_number INT, race_name TEXT,
        distance_m INT, race_class TEXT, track_condition TEXT,
        rail_position TEXT, prize_money REAL, jump_time TEXT,
        sportsbet_event_id INT, scraped_at)
  runners(id, race_id→races.id, runner_number INT, runner_name TEXT,
          barrier INT, weight_kg REAL, jockey TEXT, trainer TEXT,
          scratched INT(0/1), win_odds REAL, place_odds REAL,
          form_fig TEXT, pace_role TEXT,
          career_win_rate REAL, track_win_rate REAL, scraped_at)
  runner_form(id, runner_id→runners.id, run_date TEXT, venue TEXT,
              distance_m INT, track_condition TEXT,
              finishing_position INT, field_size INT, margin REAL,
              starting_price REAL, barrier INT, jockey TEXT)
  analysis_results(id, race_id→races.id, analysis_pass TEXT,
                   model TEXT, raw_text TEXT, created_at TEXT)
    -- analysis_pass values: PASS_0 PASS_05 PASS_1 PASS_2 PASS_15 PASS_4 POST_RACE
  race_results(id, race_id→races.id, raw_json TEXT, scraped_at)
  weather(id, venue TEXT, race_date TEXT, temperature REAL,
          wind_speed_kmh REAL, wind_direction TEXT, humidity REAL)
  track_conditions(id, venue TEXT, race_date TEXT, track_rating TEXT, rail_position TEXT)

JOIN HINT: always JOIN meetings m ON m.id = r.meeting_id to get venue/race_date.
Always add LIMIT 50 unless more is explicitly requested.
Only SELECT is allowed.
"""

_NL_SYSTEM = (
    "You are a SQLite query assistant for a horse racing analysis system.\n\n"
    + _NL_SCHEMA
    + "\nIf the user's question requires database data, respond with ONLY a single "
    "SQLite SELECT statement wrapped in ```sql ... ```.  No explanation before the SQL.\n"
    "If you can answer from general knowledge (e.g. 'what is a barrier?'), respond in prose.\n"
    "Never use DROP, DELETE, UPDATE, INSERT, ALTER, CREATE, ATTACH, PRAGMA."
)


def _nl_query_menu() -> None:
    """
    REPL-style natural-language database query interface.
    Sends the user's question to Ollama which returns SQL; we execute it and display results.
    """
    settings = load_settings()
    model    = settings.get("model", OLLAMA_MODEL)

    console.print()
    console.rule("[bold green]ASK THE DATABASE[/bold green]")
    console.print(_aest_header())
    console.print(
        "\n[dim]Type a question in plain English — Ollama translates it to SQL and executes it.\n"
        "Type [B] to go back or [Q] to quit.[/dim]\n"
    )

    if not ollama.ensure_ollama_running():
        console.print("[red]Ollama is not available.  Start Ollama and try again.[/red]")
        return

    while True:
        question = Prompt.ask("[bold green]>[/bold green]", console=console).strip()
        if not question:
            continue
        if question.upper() == "Q":
            sys.exit(0)
        if question.upper() == "B":
            return

        with console.status("[bold]Thinking...[/bold]", spinner="dots"):
            try:
                response = ollama.chat(
                    system_prompt=_NL_SYSTEM,
                    user_message=question,
                    model=model,
                    options={"temperature": 0.1, "num_ctx": 4096},
                )
            except Exception as e:
                console.print(f"[red]Ollama error: {e}[/red]")
                continue

        import re as _re
        sql_match = _re.search(r"```sql\s*(.*?)\s*```", response, _re.DOTALL | _re.IGNORECASE)

        if not sql_match:
            console.print(Panel(response.strip(), border_style="green", expand=False))
            continue

        sql = sql_match.group(1).strip()
        console.print(f"[dim]SQL: {sql[:120]}{'…' if len(sql) > 120 else ''}[/dim]")

        try:
            cols, rows = db.execute_safe_sql(sql)
        except ValueError as e:
            console.print(f"[red]Query error: {e}[/red]")
            continue

        if not rows:
            console.print("[yellow]No results returned.[/yellow]")
            continue

        res_tbl = Table(*cols, box=box.SIMPLE, header_style="bold green")
        for row in rows:
            res_tbl.add_row(*[str(row.get(c, "")) for c in cols])
        console.print(res_tbl)
        console.print(f"[dim]{len(rows)} row(s)[/dim]")


# ── Main menu ─────────────────────────────────────────────────────────────────

def _main_menu() -> None:
    while True:
        now_str = get_aest_now().strftime("%a %d %b %Y  %H:%M AEST")
        console.print()
        console.print(Panel(
            "[bold white]OMNI-FORENSIC RACE SHAPE ANALYST[/bold white]\n"
            "[dim]V324.8 AU-KINETIC-OMNI  |  Powered by Ollama[/dim]\n"
            f"[dim]{now_str}[/dim]",
            border_style="bold blue",
            expand=False,
        ))
        console.print(" [1] Analyse a Race")
        console.print(" [2] Browse Stored Races")
        console.print(" [3] View Past Analyses")
        console.print(" [4] Settings")
        console.print(" [5] Scan / Scrape Races")
        console.print(" [6] Post-Race Analysis")
        console.print(" [7] Fetch Race Results")
        console.print(" [8] Historic Archive")
        console.print(" [9] Ask the Database")
        console.print(" [Q] Quit")
        console.print()

        choice = _nav_prompt("Select", ["1", "2", "3", "4", "5", "6", "7", "8", "9"])
        if choice == "BACK":
            sys.exit(0)

        if choice == "1":
            _analyse_pending_flow()
        elif choice == "2":
            _browse_races_menu()
        elif choice == "3":
            _past_analyses_menu()
        elif choice == "4":
            _settings_menu()
        elif choice == "5":
            _scan_races_flow()
        elif choice == "6":
            _post_race_flow()
        elif choice == "7":
            _fetch_results_flow()
        elif choice == "8":
            _historic_archive_menu()
        elif choice == "9":
            _nl_query_menu()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OMNI-FORENSIC Horse Race Analyst CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py\n"
            "  python main.py --location Randwick --race 5\n"
            "  python main.py --location Flemington --race 3 --date 2026-04-19\n"
        ),
    )
    parser.add_argument("--location", "-l", type=str, default=None,
                        help="Venue name (e.g. Randwick, Flemington)")
    parser.add_argument("--race", "-r", type=int, default=None,
                        help="Race number (e.g. 5)")
    parser.add_argument("--date", "-d", type=str, default=None,
                        help="Race date YYYY-MM-DD (default: AEST today+2 search)")
    parser.add_argument("--model", "-m", type=str, default=None,
                        help="Ollama model override")
    parser.add_argument("--quick", "-q", action="store_true",
                        help="Quick analysis (Pass 0+1+Final only)")
    args = parser.parse_args()

    db.init_db()

    if args.model:
        settings = load_settings()
        settings["model"] = args.model
        save_settings(settings)

    console.print()
    console.rule("[bold cyan]OMNI-FORENSIC HORSE RACE ANALYST  V324.8[/bold cyan]")
    now_aest = get_aest_now()
    console.print(f"[dim]AEST: {now_aest.strftime('%A %d %B %Y  %H:%M')}  |  DB: {DB_PATH}[/dim]")
    console.print()

    if args.location and args.race:
        _analyse_flow(args.location, args.race, args.date)
        return

    _main_menu()


if __name__ == "__main__":
    main()
