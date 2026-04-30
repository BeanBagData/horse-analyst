"""
run_post_race_pipeline.py

Standalone batch runner for the post-race pipeline.

Flow:
  1. Scan reports/ for races that have a PACKAGE (or PASS_4) but no POST_RACE yet.
  2. Scrape results from Sportsbet via Playwright for any race missing them.
  3. Build (or load) Race_NN_POST_RACE_INPUT.txt  = PACKAGE + ANALYSIS + Results.
  4. Run post-race forensic analysis and save Race_NN_POST_RACE.txt.

Usage:
    python run_post_race_pipeline.py
"""

from __future__ import annotations
import io
import json
import sys
from pathlib import Path

# UTF-8 stdout (Windows cp1252 fix)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )

import db
from config import REPORTS_DIR, load_settings, OLLAMA_MODEL, get_aest_now
from scrapers.results import scrape_results, save_results, build_results_url
from analyst import (
    run_post_race_analysis,
    build_race_package,
    build_post_race_input,
    _build_race_dict,
)

from rich.console import Console
from rich.panel import Panel
from rich import box

console = Console(highlight=False)

FRAMEWORK_PATH = Path(__file__).parent / "post-race-analysis.txt"


def load_framework() -> str:
    if FRAMEWORK_PATH.exists():
        return FRAMEWORK_PATH.read_text(encoding="utf-8").strip()
    return "[Framework file missing — proceeding with system-prompt guidance only]"


# ── Race discovery ────────────────────────────────────────────────────────────

def find_report_races() -> list[dict]:
    """
    Walk reports/ and match each Race_NN folder to a DB record.
    Returns list of dicts with race info + paths + eligibility flags.

    Eligibility is determined by the new pipeline stages:
      - has_package:   PACKAGE in DB or Race_NN_PACKAGE.txt exists
      - has_analysis:  PASS_4  in DB or Race_NN_ANALYSIS.txt exists
      - has_results:   race_results in DB or Race_NN_results.json exists
      - has_post_race: POST_RACE in DB or Race_NN_POST_RACE.txt exists
    """
    db.init_db()
    found = []
    if not REPORTS_DIR.exists():
        return found

    for date_dir in sorted(REPORTS_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        race_date = date_dir.name

        for venue_dir in sorted(date_dir.iterdir()):
            if not venue_dir.is_dir():
                continue
            venue_slug = venue_dir.name

            # Find all race numbers from any Race_NN_*.txt file
            race_nums: set[int] = set()
            for f in venue_dir.glob("Race_*.txt"):
                try:
                    rnum = int(f.name.split("_")[1])
                    race_nums.add(rnum)
                except (IndexError, ValueError):
                    pass

            for rnum in sorted(race_nums):
                # ── Match to DB ────────────────────────────────────────────────
                db_race = db._conn().execute(
                    """SELECT r.id, r.sportsbet_event_id, r.race_name, r.distance_m,
                              r.track_condition, r.scraped_at
                       FROM races r
                       JOIN meetings m ON m.id = r.meeting_id
                       WHERE lower(replace(m.venue,' ','_')) = ?
                         AND m.race_date = ?
                         AND r.race_number = ?
                       ORDER BY r.scraped_at DESC LIMIT 1""",
                    (venue_slug, race_date, rnum),
                ).fetchone()

                race_id  = dict(db_race)["id"]              if db_race else None
                event_id = dict(db_race)["sportsbet_event_id"] if db_race else None

                # ── Stage flags ────────────────────────────────────────────────
                # PACKAGE
                has_package_db   = bool(race_id and db.get_analysis(race_id, "PACKAGE"))
                has_package_file = (venue_dir / f"Race_{rnum:02d}_PACKAGE.txt").exists()
                has_package      = has_package_db or has_package_file

                # ANALYSIS (PASS_4)
                has_analysis_db   = bool(race_id and db.get_analysis(race_id, "PASS_4"))
                has_analysis_file = (venue_dir / f"Race_{rnum:02d}_ANALYSIS.txt").exists()
                has_analysis      = has_analysis_db or has_analysis_file

                # Results
                results_path    = venue_dir / f"Race_{rnum:02d}_results.json"
                has_results_db  = bool(race_id and db.get_race_results(race_id))
                has_results_file = results_path.exists()
                has_results     = has_results_db or has_results_file

                # POST_RACE
                post_race_path      = venue_dir / f"Race_{rnum:02d}_POST_RACE.txt"
                has_post_race_db    = bool(race_id and db.get_analysis(race_id, "POST_RACE"))
                has_post_race_file  = post_race_path.exists()
                has_post_race       = has_post_race_db or has_post_race_file

                # POST_RACE_INPUT (combined file)
                pri_path = venue_dir / f"Race_{rnum:02d}_POST_RACE_INPUT.txt"

                # Disk passes (audit trail — old individual pass files)
                passes_on_disk = {
                    p for p in ["PASS_0", "PASS_05", "PASS_1", "PASS_2", "PASS_15", "PASS_4"]
                    if (venue_dir / f"Race_{rnum:02d}_{p}.txt").exists()
                }

                found.append({
                    "race_id":        race_id,
                    "event_id":       event_id,
                    "venue":          venue_slug.replace("_", " ").title(),
                    "venue_slug":     venue_slug,
                    "race_date":      race_date,
                    "race_number":    rnum,
                    "passes_disk":    passes_on_disk,
                    "has_package":    has_package,
                    "has_analysis":   has_analysis,
                    "results_path":   results_path,
                    "has_results":    has_results,
                    "has_post_race":  has_post_race,
                    "pri_path":       pri_path,
                })
    return found


# ── Results scraping ──────────────────────────────────────────────────────────

def scrape_missing_results(races: list[dict]) -> None:
    """Scrape results.json for any race that has an ANALYSIS but no results yet."""
    need = [r for r in races if r["has_analysis"] and not r["has_results"]]
    if not need:
        console.print("[green]✓ All eligible races already have results.[/green]")
        return

    console.print(f"\n[bold]Scraping results for {len(need)} race(s)...[/bold]")
    for r in need:
        venue     = r["venue"]
        race_date = r["race_date"]
        rnum      = r["race_number"]
        event_id  = r["event_id"]
        race_id   = r["race_id"]

        if not event_id:
            console.print(
                f"  [yellow]⚠ {venue} R{rnum} {race_date} — no event_id in DB, skipping[/yellow]"
            )
            continue

        url = build_results_url(r["venue_slug"], rnum, event_id)
        console.print(f"  Scraping [cyan]{venue} R{rnum}[/cyan]  {url}")

        with console.status("  Fetching...", spinner="dots"):
            try:
                result = scrape_results(url)
            except RuntimeError as e:
                console.print(f"  [red]✗ Dependency: {e}[/red]")
                continue
            except ValueError as e:
                console.print(f"  [yellow]⚠ Not available yet: {e}[/yellow]")
                continue
            except Exception as e:
                console.print(f"  [red]✗ Error: {e}[/red]")
                continue

        saved = save_results(result, REPORTS_DIR, venue, race_date, rnum)
        r["has_results"]  = True
        r["results_path"] = saved

        if race_id:
            db.insert_race_results(race_id, result)

        finishers = result.get("finishers") or []
        winner    = next((f for f in finishers if f.get("position") == 1), None)
        if winner:
            wo = winner.get("win_odds")
            console.print(
                f"  [green]✓ Saved[/green] → {saved.name}"
                + (f"  | Winner: {winner['name']}" + (f" @ ${wo:.2f}" if wo else ""))
            )
        else:
            console.print(f"  [green]✓ Saved[/green] → {saved.name}")


# ── Post-race batch ───────────────────────────────────────────────────────────

def run_post_race_batch(races: list[dict], framework: str) -> None:
    """
    For every race with ANALYSIS + results but no POST_RACE yet:
      1. Ensure PACKAGE exists (build if needed).
      2. Build Race_NN_POST_RACE_INPUT.txt (PACKAGE + ANALYSIS + Results).
      3. Run post-race analysis → Race_NN_POST_RACE.txt.
    """
    eligible = [
        r for r in races
        if r["has_analysis"] and r["has_results"] and not r["has_post_race"]
    ]
    already_done = [
        r for r in races
        if r["has_analysis"] and r["has_results"] and r["has_post_race"]
    ]

    if already_done:
        console.print(
            f"\n[dim]{len(already_done)} race(s) already have POST_RACE — skipping.[/dim]"
        )

    if not eligible:
        console.print("[green]✓ No pending post-race analyses.[/green]")
        return

    settings = load_settings()
    model    = settings.get("model") or OLLAMA_MODEL

    console.print(f"\n[bold]Running post-race analysis for {len(eligible)} race(s)...[/bold]")
    console.print(f"[dim]Model: {model}[/dim]")

    for r in eligible:
        venue   = r["venue"]
        rnum    = r["race_number"]
        rdate   = r["race_date"]
        race_id = r["race_id"]

        console.print()
        console.print(Panel(
            f"[bold]{venue}  R{rnum}  {rdate}[/bold]\n"
            f"race_id={race_id}  |  passes on disk: {', '.join(sorted(r['passes_disk']))}",
            title="[magenta]POST-RACE PIPELINE[/magenta]",
            border_style="magenta",
        ))

        if race_id is None:
            console.print("[red]No DB race_id — cannot proceed. Skipping.[/red]")
            continue

        # ── Ensure PACKAGE ────────────────────────────────────────────────────
        console.print()
        console.rule("[yellow]Stage 1 — Race Package[/yellow]")
        try:
            build_race_package(race_id, console=console, force_rebuild=False)
        except Exception as e:
            console.print(f"[yellow]  Package build warning: {e} — continuing[/yellow]")

        # ── Load results ──────────────────────────────────────────────────────
        results = db.get_race_results(race_id)
        if not results:
            try:
                results = json.loads(
                    r["results_path"].read_text(encoding="utf-8")
                )
            except Exception as e:
                console.print(f"[red]Cannot read results: {e}[/red]")
                continue

        # ── Build POST_RACE_INPUT ─────────────────────────────────────────────
        console.print()
        console.rule("[yellow]Stage 2 — Post-Race Input File[/yellow]")
        try:
            build_post_race_input(race_id, results, console=console, force_rebuild=False)
        except Exception as e:
            console.print(f"[red]Failed to build post-race input: {e}[/red]")
            continue

        # ── Run post-race analysis ────────────────────────────────────────────
        console.print()
        console.rule("[yellow]Stage 3 — Post-Race Analysis[/yellow]")
        ok = run_post_race_analysis(
            race_id=race_id,
            results=results,
            framework_text=framework,
            console=console,
            model=model,
        )
        if ok:
            r["has_post_race"] = True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    console.print()
    console.rule("[bold cyan]POST-RACE PIPELINE  (Package-File Mode)[/bold cyan]")
    console.print(f"[dim]{get_aest_now().strftime('%A %d %B %Y  %H:%M AEST')}[/dim]")

    # Discover races
    console.print("\n[bold]Scanning report folders...[/bold]")
    races = find_report_races()

    if not races:
        console.print("[yellow]No races found in reports/ folder.[/yellow]")
        return

    # Summary table
    from rich.table import Table
    tbl = Table(
        "Date", "Venue", "Race", "Package", "Analysis", "Results", "Post-Race",
        box=box.SIMPLE, header_style="bold cyan",
    )
    for r in races:
        tbl.add_row(
            r["race_date"],
            r["venue"],
            f"R{r['race_number']}",
            "[green]✓[/green]" if r["has_package"]   else "",
            "[green]✓[/green]" if r["has_analysis"]  else "",
            "[green]✓[/green]" if r["has_results"]   else "",
            "[green]✓[/green]" if r["has_post_race"] else "",
        )
    console.print(tbl)

    framework = load_framework()

    # Step 1: scrape missing results
    console.print()
    console.rule("[cyan]Step 1 — Scrape Missing Results[/cyan]")
    scrape_missing_results(races)

    # Step 2: run post-race analysis
    console.print()
    console.rule("[cyan]Step 2 — Post-Race Analysis[/cyan]")
    run_post_race_batch(races, framework)

    console.print()
    console.rule("[bold green]✓ PIPELINE COMPLETE[/bold green]")


if __name__ == "__main__":
    main()
