# horse-analyst

**OMNI-FORENSIC Race Shape Analyst — V325.1 AU-KINETIC-OMNI**

A local-first CLI tool for thoroughbred race analysis, forensic prediction, and post-race review. Scrapes race data from Sportsbet, assembles structured race packages, runs a six-pass LLM analysis pipeline, and produces post-race forensic feedback — all from a single terminal window.

All race data is stored in a local SQLite database. All AI inference runs locally via Ollama or through any OpenAI-compatible API.

Current Prompt version 325.3 included. To update the prompt have your LLM update the horse analyst files to align with the new prompt.
See post race analysis to improve prompts.

NOTE: This will most likely require updating but we are going to leave this as the baseline build.
---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
- [Menu System — Full Walkthrough](#menu-system--full-walkthrough)
- [Understanding the Reports](#understanding-the-reports)
- [Post-Race Analysis — Improving Future Predictions](#post-race-analysis--improving-future-predictions)
- [Using Alternative LLMs](#using-alternative-llms)
- [File and Folder Layout](#file-and-folder-layout)
- [Database Schema](#database-schema)
- [Navigation Rules](#navigation-rules)
- [Roadmap](#roadmap)
- [License](#license)

---

## Features

- **One-command race scrape** — fetches race card, form guide, odds, weather, and track conditions from Sportsbet and the Bureau of Meteorology
- **Package-file pipeline** — assembles all data into a single structured document before analysis, so the LLM always has complete context with no mid-analysis re-querying
- **Six-pass forensic analysis** — progressive refinement from raw data audit through to a final prediction with full designation tables
- **Post-race forensic loop** — combines the pre-race analysis with actual results to generate structured feedback you can use to improve the prompt
- **Natural-language DB queries** — ask plain-English questions about your race history database
- **Local-first** — everything runs on your machine; no cloud required
- **Multi-LLM** — works with Ollama (local), OpenAI, Anthropic Claude, Google Gemini, or any OpenAI-compatible endpoint

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| [Ollama](https://ollama.ai) | Latest | For local inference. Optional if using API providers. |
| Playwright | Latest | Required for Sportsbet scraping |
| Git | Any | For cloning |

**Python packages** — all installed via pip:

```
requests
rich
playwright
```

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/horse-analyst.git
cd horse-analyst

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. Install Python dependencies
pip install requests rich playwright

# 4. Install Playwright browser binaries (required for Sportsbet scraping)
playwright install chromium

# 5. Install Ollama (for local inference)
# macOS / Linux:
curl -fsSL https://ollama.ai/install.sh | sh
# Windows: download the installer from https://ollama.ai/download

# 6. Pull a model
ollama pull gemma3:4b          # Fast, good for batches
ollama pull gemma3:12b         # Balanced — recommended starting point
ollama pull llama3.1:8b        # Strong reasoning
ollama pull qwen2.5:14b        # Excellent structured output

# 7. Run the app
python main.py
```

> **Windows note:** The app writes UTF-8 report files. If you see encoding errors in the terminal run `chcp 65001` first.

---

## Configuration

All settings live in `settings.json` in the root `horse-analyst/` folder. The file is created automatically on first run with these defaults:

```json
{
  "model": "gemma3:12b",
  "save_reports": true,
  "run_all_passes": true
}
```

| Key | Default | Description |
|---|---|---|
| `model` | `gemma3:12b` | Ollama model name, or the model string for your API provider |
| `save_reports` | `true` | Write `.txt` report files to `reports/` |
| `run_all_passes` | `true` | Run all 6 passes. `false` = quick mode (Pass 0 + 1 + 4 only) |

You can change the model at any time from inside the app via **Settings [4]** without editing this file manually.

### Environment variables for cloud API providers

If you are using a cloud LLM instead of Ollama, set the relevant environment variable before running:

```bash
# OpenAI
export OPENAI_API_KEY=sk-...

# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Google Gemini
export GOOGLE_API_KEY=AIza...

# Any OpenAI-compatible endpoint (LM Studio, Groq, Together AI, vLLM)
export OPENAI_BASE_URL=https://api.groq.com/openai/v1
export OPENAI_API_KEY=gsk_...
```

See [Using Alternative LLMs](#using-alternative-llms) for the code changes required.

---

## Quick Start

```bash
# Start the interactive menu
python main.py

# Analyse a specific race directly — bypasses all menus, useful for scripting
python main.py --location Randwick --race 5
python main.py --location "Eagle Farm" --race 3 --date 2026-04-28

# Override the model for one session only (does not change settings.json)
python main.py --model llama3.1:8b

# Quick analysis mode — Pass 0 + 1 + 4 only, skips integrity and probability passes
python main.py --location Flemington --race 1 --quick
```

### Typical first-session workflow

```
1.  python main.py
2.  Select [5] Scan / Scrape Races
3.  Select today → your region → [A] to scrape all races
4.  Press [M] to return to main menu
5.  Select [1] Analyse a Race
6.  Select [A] to analyse all scraped races
    → Analysis files saved to horse-analyst/reports/YYYY-MM-DD/venue/
7.  After the races run, select [7] Fetch Race Results
8.  Select [A] to fetch all missing results
9.  Select [6] Post-Race Analysis
10. Select [A] to run post-race forensic review for all completed races
```

---

## Menu System — Full Walkthrough

When you start `python main.py` you see the main menu:

```
╔══════════════════════════════════════════════════════════╗
║  OMNI-FORENSIC RACE SHAPE ANALYST                        ║
║  V325.1 AU-KINETIC-OMNI  |  Powered by Ollama            ║
║  Tue 28 Apr 2026  08:30 AEST                             ║
╚══════════════════════════════════════════════════════════╝
 [1] Analyse a Race
 [2] Browse Stored Races
 [3] View Past Analyses
 [4] Settings
 [5] Scan / Scrape Races
 [6] Post-Race Analysis
 [7] Fetch Race Results
 [8] Historic Archive
 [9] Ask the Database
 [Q] Quit
```

Type the number or letter for your choice and press Enter. At any prompt you can type:

| Key | Action |
|---|---|
| `B` | Go back one level |
| `M` | Jump straight to main menu (available 2+ levels deep) |
| `Q` | Quit the application (Ollama model unloaded cleanly) |

---

### [5] Scan / Scrape Races

Your starting point each day. Fetches race cards, form guides, odds, weather, and track conditions.

**Step 1 — Choose a date:**

```
 [1] Today (2026-04-28)
 [2] Tomorrow (2026-04-29)
 [3] Custom date
```

**Step 2 — Choose a region:**

```
 No.  Region        Venues  Races
  1   NSW / ACT     4       34
  2   VIC           3       24
  3   QLD           3       22
  4   SA / WA       2       14
```

**Step 3 — Location list (Level 1):**

```
══════════ NSW / ACT — 2026-04-28 ══════════

 [A]  Scrape ALL NSW / ACT races not in DB  (34 missing)

 No.  Venue          Races  First Jump  Not in DB
  1   Randwick        8      11:55       8
  2   Gosford         7      11:30       7
  3   Wagga Wagga     7      12:15       7
  4   Canberra        8      11:45       4

 [B]  Back to regions
```

- **`[A]`** — scrapes every race in the region not already in the DB, then returns to the **main menu**
- **`[1]`** — opens the race list for Randwick

**Step 4 — Venue race list (Level 2):**

```
══════════ Randwick — 2026-04-28 ════════════

 [A]  Scrape all Randwick races not in DB  (8 missing)

 No.  Race                           Dist   Jump     In DB?
  1   R1  Tab Everest Prelude        1200m  11:55
  2   R2  Benchmark 78 Handicap      1400m  12:30
  3   R3  Open Handicap              1600m  13:05
  4   R4  Group 3 Handicap           2000m  13:40    ✓

 [B]  Back to location list  [M] Main menu
```

- **`[A]`** — scrapes all Randwick races not yet in the DB, then returns to the **location list** (counts refresh automatically)
- **`[2]`** — scrapes only Race 2, then stays at this venue race list (the ✓ appears)

> **No duplicates:** The scraper checks `sportsbet_event_id` before each scrape. Races already in the database are skipped silently.

---

### [1] Analyse a Race

After scraping, run the six-pass LLM analysis here.

**Location list (Level 1):**

```
 [A]  Analyse all races   (19 pending)

 [1]  Randwick             (8 races)
 [2]  Gosford              (7 races)
 [3]  Wagga Wagga          (4 races)

 [N]  Enter a new race manually
 [B]  Back
```

- **`[A]`** — runs the full pipeline for every pending race, then returns to the **main menu**
- **`[1]`** — opens the Randwick venue list
- **`[N]`** — enter a venue name, race number, and optional date to scrape and analyse a race on the fly

**Venue race list (Level 2):**

```
══════════ RANDWICK — PENDING RACES ════════════

 [A]  Analyse all Randwick races  (8 pending)

 No.  Date        Race                              Passes on Disk
  1   2026-04-28  R1  Tab Everest Prelude 1200m     —
  2   2026-04-28  R2  Benchmark 78 1400m            PASS_0
  3   2026-04-28  R3  Open Handicap 1600m           —

 [B]  Back to location list  [M] Main menu
```

- **`[A]`** — analyses all 8 Randwick races, then returns to the **location list** (counts drop to 0)
- **`[2]`** — runs the full pipeline for Race 2 only, then **stays here** (Race 2 disappears from the list)

The "Passes on Disk" column shows which intermediate passes already exist from a previous partial run. The engine skips any pass already in the database.

**What you see while a race is being analysed:**

```
──────────── BUILDING RACE PACKAGE ─────────────
  Building race package (race card + form + speed map)...
  Package saved → Race_02_PACKAGE.txt

──────────── PASS 0 — Forensic Data Audit ───────
  Model: gemma3:12b  |  Randwick R2  2026-04-28
  Running pass...  [████████████████] 24.3s

──────────── PASS 0.5 — Tier 1 Macro Sweep ──────
  Running pass...  [████████████████] 18.7s

──────────── PASS 1 — Forward Draft Canvas ───────
  Running pass...  [████████████████] 41.2s

──────────── PASS 2 — Silo Integrity Audit ───────
──────────── PASS 1.5 — Probabilistic Projection ─
──────────── PASS 4 — Final Canvas Render ─────────
  Intermediate pass saved → Race_02_PASS_4.txt

╔════════════════════════════════════════╗
║  ANALYSIS FILE SAVED                  ║
║  reports/2026-04-28/randwick/         ║
║  Race_02_ANALYSIS.txt                 ║
╚════════════════════════════════════════╝

─────── ANALYSIS COMPLETE ──────────────
Key files in: reports/2026-04-28/randwick/
  Race_02_PACKAGE.txt    ← race card + form + speed map
  Race_02_ANALYSIS.txt   ← final prediction  ✓
```

---

### [7] Fetch Race Results

After the races have been run, fetch official results from Sportsbet.

```
──────────── FETCH RACE RESULTS ─────────────────

RECENT  (today & yesterday)
 No.  Date        Venue        Race                  Results?
  1   2026-04-28  Randwick     R1  Everest Prelude
  2   2026-04-28  Randwick     R2  BM78 Handicap      ✓
  3   2026-04-28  Gosford      R3  Maiden Plate

OLDER DATES  (select group [Dx] to expand)
 Grp  Date        Venues  Races  Missing Results
  D1  2026-04-21  2       14     3

 [A]   Scrape all missing recent results (2 races)
 [1-3] Scrape a specific recent race
 [D1]  Expand older date group
 [B]  Back
```

- **`[A]`** — scrapes all recent races that have an analysis but no results yet
- **`[2]`** — scrapes just that one race; the ✓ appears in the table immediately
- **`[D1]`** — expands the older date group with its own `[A]` and numbered list

> Results are only available after the race finishes. If you try too early you will see "Not available yet" and the race is skipped gracefully — run `[A]` again later.

---

### [6] Post-Race Analysis

Runs the forensic post-race review for races that have both an ANALYSIS and results.

**Location list (Level 1):**

```
──────────── POST-RACE ANALYSIS ─────────────────

 [A]  Run ALL pending post-race analyses  (8 races across 2 venues)

 No.  Venue        Pending  Dates
  1   Randwick     5        2026-04-28
  2   Gosford      3        2026-04-28

 [B]  Back
```

**Venue race list (Level 2):**

```
══════════ RANDWICK — PENDING POST-RACE ══════════

 [A]  Run all Randwick post-race analyses  (5 pending)

 No.  Date        Race                         Silo
  1   2026-04-28  R1  Everest Prelude 1200m    A (Sat)
  2   2026-04-28  R2  BM78 1400m               A (Sat)
  3   2026-04-28  R3  Open Handicap 1600m      A (Sat)

 [B]  Back to location list  [M] Main menu
```

When you trigger an analysis, the system:
1. Loads the PACKAGE (pre-race data) from the DB or file
2. Loads the ANALYSIS (your prediction) from the DB or file
3. Combines them with the formatted results into `Race_NN_POST_RACE_INPUT.txt`
4. Streams the post-race prompt and saves `Race_NN_POST_RACE.txt`

---

### [2] Browse Stored Races

Shows every race in your database with pipeline completion indicators:

```
 No.  Date        Venue        Race                 Dist  Analysed  Results  Post-Race
  1   2026-04-28  Randwick     R1  Everest Prelude  1200m    ✓         ✓        ✓
  2   2026-04-28  Randwick     R2  BM78 Handicap    1400m    ✓         ✓
  3   2026-04-28  Gosford      R3  Maiden Plate     1200m    ✓

  ✓ = in DB   f = file only (not in DB)   – = missing
```

Select a number to view any stored pass for that race or re-run the analysis.

---

### [3] View Past Analyses

Lists every stored analysis pass across all races — useful for reading intermediate passes or reviewing older predictions.

```
 No.  Date        Venue        Race     Pass          Source  Created
  1   2026-04-28  Randwick     R1       PASS_4        DB      2026-04-28 08:45
  2   2026-04-28  Randwick     R1       POST_RACE     DB      2026-04-28 18:22
  3   2026-04-28  Gosford      R3       PASS_1        DB      2026-04-28 09:10
```

---

### [4] Settings

```
Current Model: gemma3:12b
Save Reports : True
Database     : C:\Users\Sam\projects\horse-analyst\horse_analyst.db
Reports Dir  : C:\Users\Sam\projects\horse-analyst\reports

 [1] Change Ollama Model
 [2] Toggle Save Reports
 [3] List Available Models
 [B] Back
```

Selecting **`[1]`** lists every model Ollama has installed so you can pick by number rather than typing.

---

### [8] Historic Archive

Browse fully-completed races (PASS_4 + results + POST_RACE all done), grouped by date.

```
 No.  Date        Venues  Races
  1   2026-04-28  2       13
  2   2026-04-21  3       18
```

Select a date → select a race → select which pass to display in full.

---

### [9] Ask the Database

Type any question in plain English. The LLM converts it to SQL and runs it against your local database.

```
> What was the strike rate of barrier 1 at Randwick in the last 30 days?
> Show me every race where the favourite did not finish top 3
> Which trainers had the most winners last Saturday?
> List all races where the track was Heavy 8 or worse
> What is my 1A win rate by silo for April?
> Show races where Torque Delta was confirmed but 1A still didn't win
```

Results are shown as a formatted table. Only `SELECT` queries are permitted — the database cannot be modified from this interface.

---

## Understanding the Reports

Every analysed race produces a set of structured text files in `horse-analyst/reports/YYYY-MM-DD/venue_name/`.

### Race_NN_PACKAGE.txt — The Data Document

The complete data document assembled before any LLM call. Open this to see exactly what the model had to work with.

**Section 1 — Race Card**

```
No.  Name                   Bar  Wgt     Jockey             Trainer              Win     Plc    Form
1    Arcane Gesture          3    57.0kg  J Bowman           C Waller            $3.80   $1.70  1x231
2    Mossman Gorge           7    57.5kg  K McEvoy           T Busuttin         $6.50   $2.40  2214x
```

Fields: runner number, name, barrier, weight (post-claim effective weight), jockey, trainer, win odds, place odds, form figure (last 6 runs), gear changes in brackets.

**Section 2 — Form Guide**

For each runner, the last 8 runs with full context including trainer/jockey stats when available:

```
1. Arcane Gesture  (Barrier 3)
   · Career: 12s 4w 3p  · Track: 6s 2w  · Good: 8s 3w  · Vel:8.2  · Form:7.4  · Rating:88
   T/C Waller    Good: 18s 7w 39%  ROI:+8%   Dist 1200-1400: 22s 9w 41%
   J/J Bowman    Good: 31s 12w 39%  ROI:+5%

   2026-04-14  RANDWICK       1400m  Good4  1/9   [0.1L]  SP$3.80  B3  J Bowman  IR:3rd  (W:Arcane Gesture)
   2026-03-28  ROSEHILL       1200m  Good4  2/10  [1.8L]  SP$4.20  B5  J Bowman  IR:4th  (W:Stellar Dream)  2:Blue Orbit
   2026-03-07  RANDWICK       1400m  Soft5  3/8   [2.3L]  SP$3.60  B2  J Bowman  IR:2nd
```

**Section 3 — Speed Map**

```
  Pace Positions  (source: Sportsbet speed map widget)
  Runner                    Bar  Position      Pct    Win
  Stellar Dream              2   Leader        12%   $5.50
  Gold Factor                4   On Pace       28%   $8.00
  Arcane Gesture             3   Midfield      52%   $3.80
  Blue Orbit                 7   Back          78%   $12.00
```

When actual speed map positions are not available the pace roles are inferred from form.

**Section 4 — Weather & Track**

```
  Temp: 18°C  |  Wind: NE 22 km/h  |  Humidity: 72%  |  Pressure: Falling
  Track Rating: Good(4)  |  Rail: True
```

---

### Race_NN_ANALYSIS.txt — The Prediction

The final prediction produced by Pass 4. Read it in this order:

**1. Deterministic Lock** — who was eliminated and why:

```
ELIMINATED (TERMINAL LAYS): Blue Orbit (V6 — Zombie Start: 4yo, 22 starts, 4% win rate)
VETO FLAGS (CEILING LAYS): Heavy Metal King (V12 — Top Weight Class Rise, Field 12)
SURVIVOR CLUSTER: Arcane Gesture, Stellar Dream, Gold Factor, Mossman Gorge
LOCKED ENGINE: Thermodynamic Collapse — pace-genuine sprint, wide handicap, rail advantage
```

**2. 1A Sovereign** — your primary selection:

```
1A SOVEREIGN: 1. Arcane Gesture (Bar 3)
PHYSICS DRIVER: Thermodynamic Collapse + T3 Prominent Stalker Control
PROJECTED MOMENTUM TACTIC: TACTIC 3 — PROMINENT STALKER
  Settling: One-off rail, Speed Rank 3, Effective Barrier 3
DIRECTIVE 0.0 CLEARANCE: Pass
CONFIDENCE: 8/10
ESCAPE SCORE: 9/10
WHY INEVITABLE: C&D proven (4 wins from 6 at 1400m Randwick, last win 14 days ago).
  T3 profile immune to traffic on Good track. Bowman partnership 5-from-9 at Randwick.
  M2.36 Track Condition Congruence Score: 0.94 (8 Good track runs, 3 wins).
  Trainer Waller: Dist 1200-1400m 22s 9w 41% ROI+8%. Pattern: HIGH CONFIDENCE.
```

**3. 1B Shield** — divergent insurance:

```
1B SHIELD: 7. Mossman Gorge (Bar 7)
PACE VECTOR AND TACTIC: TACTIC 1 — WIDE SWEEP
  Divergent from 1A T3 inside path. Pace Delta: 6 positions. Wide sweep bypasses
  traffic in a pace-genuine race. Top-2 L200m Torque Delta confirmed (31.8s last run).
```

**4. The Trinity Tables** — three mandatory output tables every analysis must contain:

- **Table 1: Full Field Designation Mapping (Kinetic-Table)** — every horse listed with their Momentum Tactic (T1/T2/T3/Lead/Trap), base kinetic score, and kinetic justification. No horse may be omitted.
- **Table 2: Tier 2 Survivor Audit Matrix (SAM)** — Torque Delta confirmation, gear history, trainer intent, and Tactic 2 Torque prerequisite status for each survivor.
- **Table 3: SSM Omni-Spatial Scoring Matrix** — spatial resonance, performance variance, and exotic placement candidates.

**5. Investment directive:**

```
INVESTMENT: Win + Exacta
DIRECTIVE: Win 2U on 1A. Exacta 1A/1B + reversed 1B/1A. Entropy Level: Low.
  Top P Metric: N_p = 3 horses to 90%.
SILO C NOTE: Two-Leg SRM (1A+1B Top-4/5) is the default betting vehicle.
```

---

### Race_NN_POST_RACE.txt — The Forensic Review

The forensic review produced after results are known. Key sections:

**Prediction vs Actual — Full Field:**

```
PREDICTION vs ACTUAL:
  1A: Arcane Gesture (predicted 1A)   → FINISHED 1ST  ✓
  1B: Mossman Gorge  (predicted 1B)   → FINISHED 3RD  ✓
  Mortal Lay: Blue Orbit              → FINISHED 8TH  ✓
  System Shield: Gold Factor          → FINISHED 2ND  ✓
  KINETIC-TABLE matched: 4/6 top designations in correct positions
```

**Physics validation:**

```
LOCKED ENGINE VALIDATION: Thermodynamic Collapse confirmed.
  Leader (Stellar Dream, B2) burned through 600m in 35.1s — hot pace.
  T3 Stalkers (1A, 1B) preserved energy — both top 3. T1 Wide Sweep correct.
  M10.26 Soft Track Hierarchy: Not triggered (Good track — T3 dominant as locked).
```

**Pattern reinforcement and corrections:**

```
CELEBRATE: T3 at Randwick 1400m Good(4) True rail with pace-genuine field.
  Pattern confirmed: C&D record + Good congruence + T3 + Waller = strong 1A signal.
  Waller Dist 1400m Good track ROI+8% — EMBED THIS as a M2.34 Trial Venue boost.

CORRECTION: M1.35 Gate-Release Mandate was not applied for runner 4 (Gold Factor)
  who had barrier incidents in last 3 runs. Should be noted as risk flag in PASS_2.
  ACTION: Add barrier incident scan to PASS_2 checklist for all horses EB 1-5.
```

---

## Post-Race Analysis — Improving Future Predictions

The post-race forensic loop is how you continuously improve accuracy over time. Follow this structured workflow.

### Step 1 — Run the post-race analysis

Use **[6] Post-Race Analysis** after results are in. Each `Race_NN_POST_RACE.txt` contains structured feedback from the engine.

### Step 2 — Read the key sections

Look for these in every POST_RACE file:

| Section | What to do |
|---|---|
| **CELEBRATE** blocks | Reinforce these patterns — they are working correctly |
| **CORRECTION** blocks | These need a change in `prompt.py` |
| **MANDATE COMPLIANCE LOG** | Check which mandates fired and whether they were correct |
| **TORQUE DELTA CHECK** | Were sectionals used correctly? Was the tactic right? |
| **DYNAMIC REVISION LOG** | Did the engine rewind any steps? Were the reasons valid? |

### Step 3 — Keep a prompt improvement log

Create a file called `prompt_improvements.txt` in the root folder and maintain three sections:

```
## REINFORCED PATTERNS (do not touch these)
- T3 Prominent Stalker at metropolitan 1400m on Good(4) True rail is dominant
- C&D record (M8.26) within 18 months is a reliable 1A filter at Randwick and Flemington
- Pace Delta > 5 positions between 1A (T3) and 1B (T1) correctly predicted finish order 7/9 races

## CORRECTIONS TO IMPLEMENT
- PASS_2 checklist misses M1.35 Gate-Release for horses with barrier incidents in last 3 runs
- M3.21.C Extreme Distance Drop fires too aggressively in Silo C — apply only when drop >= 400m
- SAM Table: Tactic 2 torque prerequisite was not reclassified to Pocket Trap for 2 runners

## ABSTAIN PATTERNS (race types to avoid)
- Silo C maiden races with field >= 12 produce High Entropy N_p >= 5 in ~80% of cases
  → Default to boxed exotic Top-4 only, no win bet
- Saturday Group races with 3+ confirmed leaders — Kamikaze Pace Collapse makes pace
  map unreliable until Live Bias Audit confirms (use Contingent 1A only)
```

### Step 4 — Update the prompt

The prompt is in `horse-analyst/prompt.py`. Each template maps to a pass:

| Template | Pass | When to edit |
|---|---|---|
| `SYSTEM_PROMPT` | All | Global rules that apply everywhere |
| `PASS_0_TEMPLATE` | Pass 0 | Change what data is parsed or flagged first |
| `PASS_05_TEMPLATE` | Pass 0.5 | Change the macro sweep or environmental checks |
| `PASS_1_TEMPLATE` | Pass 1 | Add/edit module instructions under M-codes |
| `PASS_2_TEMPLATE` | Pass 2 | Add new integrity check items to the checklist |
| `PASS_4_TEMPLATE` | Pass 4 | Change the output format or report sections |
| `POST_RACE_TEMPLATE` | Post-Race | Add new forensic check questions |

**Example — adding an M1.35 Gate-Release check to PASS_2:**

Open `prompt.py`, find `PASS_2_TEMPLATE`, and add a line to the checklist:

```python
"- [ ] M1.35 Gate-Release Mandate: Executed for all horses with 3+ barrier "
"incidents in last 5 runs? Mandatory -10 NLS penalty applied in PASS_1?\n"
```

**Example — tightening M3.21.C threshold:**

Find the M3.21.C line in `PASS_1_TEMPLATE`:
```python
# Before
"- M3.21.C Extreme Distance Drop: Evaluate.\n"

# After
"- M3.21.C Extreme Distance Drop: Apply ONLY when distance reduction "
"is >= 400m (not 200m). Silo C exemption applies. Confirm drop is genuine "
"class-driven, not trainer targeting.\n"
```

### Step 5 — Version your changes

Before editing `prompt.py`, copy it to a dated backup:

```bash
cp prompt.py backups/prompt_V325_1_original.py
cp prompt.py backups/prompt_V325_2_gaterelease_fix.py
```

Commit after each meaningful change:

```bash
git add prompt.py
git commit -m "PASS_2: add M1.35 Gate-Release barrier incident check"
```

### Step 6 — Test the updated prompt

Pick 3–5 races from the Historic Archive **[8]** where you have known results. Re-run the analysis with the updated prompt and compare the new ANALYSIS to the old one:

```bash
# Re-run a specific race with new prompt (overwrites existing files)
python main.py --location Randwick --race 2 --date 2026-04-28
```

Or from inside the app: **[2] Browse Stored Races** → select the race → re-run PASS_2 or PASS_4.

### Step 7 — Track performance metrics with [9] Ask the Database

```
> Show my 1A win rate by silo for races after 2026-04-01
> What is the average confidence score for 1A selections that won vs lost?
> Show all races where Torque Delta K1 was confirmed and 1A finished top 2
> Compare win rate for T3 selections vs T1 selections on Good tracks
> Show races where the POST_RACE identified a CORRECTION pattern
> What percentage of my Mortal Lay selections finished outside top 4?
```

The `backtest_results` table (populated after each post-race run) stores your 1A selection outcome, silo, model used, and finishing position — giving you a rolling accuracy dashboard as you refine the prompt.

---

## Using Alternative LLMs

The LLM interface is in `horse-analyst/ollama_client.py`. Swap out the `stream_chat` and `chat` functions to use any provider.

### Option 1 — Ollama (local, default)

Best for: privacy, offline use, no API costs, running large batches.

```bash
ollama pull gemma3:12b   # recommended starting point
```

**Model recommendations:**

| Use case | Model | Notes |
|---|---|---|
| Speed / daily batch | `gemma3:4b` | ~3–5s per pass on modern GPU |
| Balanced | `gemma3:12b` | Recommended default |
| Strong reasoning | `llama3.1:8b` | Good at structured output |
| Maximum local quality | `qwen2.5:14b` | Best for PASS_4 final render |
| Large RAM/GPU | `gemma3:27b` | Excellent but slow on CPU |

---

### Option 2 — OpenAI

Best for: highest quality when local compute is limited.

```bash
pip install openai
export OPENAI_API_KEY=sk-...
```

Replace `stream_chat` in `ollama_client.py`:

```python
import openai, os

def stream_chat(system_prompt, user_message, model="gpt-4o",
                on_token=None, options=None):
    client = openai.OpenAI()
    accumulated = []
    with client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        stream=True,
        max_tokens=4096,
        temperature=0.1,
    ) as stream:
        for chunk in stream:
            content = chunk.choices[0].delta.content or ""
            if content:
                accumulated.append(content)
                if on_token:
                    on_token(content)
    return "".join(accumulated)

def chat(system_prompt, user_message, model="gpt-4o", options=None):
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        max_tokens=1024,
        temperature=0.1,
    )
    return response.choices[0].message.content or ""
```

Set in `settings.json`: `"model": "gpt-4o"`

**Estimated cost per race (6 passes, ~25k tokens total):**

| Model | Estimated cost |
|---|---|
| `gpt-4o-mini` | ~$0.01–0.03 |
| `gpt-4o` | ~$0.10–0.30 |
| `gpt-4.1` | ~$0.20–0.50 |

---

### Option 3 — Anthropic Claude

Best for: long-context races with large fields, nuanced reasoning chains.

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

Replace `stream_chat` in `ollama_client.py`:

```python
import anthropic, os

def stream_chat(system_prompt, user_message,
                model="claude-sonnet-4-5", on_token=None, options=None):
    client = anthropic.Anthropic()
    accumulated = []
    with client.messages.stream(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            accumulated.append(text)
            if on_token:
                on_token(text)
    return "".join(accumulated)

def chat(system_prompt, user_message,
         model="claude-haiku-4-5", options=None):
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Recommended Claude models:**

| Model | Use case |
|---|---|
| `claude-haiku-4-5` | Fast cheap passes (PASS_0, PASS_0.5) |
| `claude-sonnet-4-5` | Balanced — recommended for all passes |
| `claude-opus-4-5` | Maximum quality — PASS_4 final render only |

---

### Option 4 — Google Gemini

Best for: very large fields or extremely data-rich packages (Gemini 1.5 Pro handles 1M tokens).

```bash
pip install google-generativeai
export GOOGLE_API_KEY=AIza...
```

Replace `stream_chat` in `ollama_client.py`:

```python
import google.generativeai as genai, os

genai.configure(api_key=os.environ["GOOGLE_API_KEY"])

def stream_chat(system_prompt, user_message,
                model="gemini-1.5-flash", on_token=None, options=None):
    gmodel = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_prompt,
    )
    accumulated = []
    for chunk in gmodel.generate_content(
        user_message,
        generation_config={"temperature": 0.1, "max_output_tokens": 4096},
        stream=True,
    ):
        text = chunk.text or ""
        accumulated.append(text)
        if on_token:
            on_token(text)
    return "".join(accumulated)

def chat(system_prompt, user_message, model="gemini-1.5-flash", options=None):
    gmodel = genai.GenerativeModel(model_name=model,
                                   system_instruction=system_prompt)
    response = gmodel.generate_content(user_message)
    return response.text or ""
```

---

### Option 5 — OpenAI-compatible APIs (Groq, Together AI, LM Studio, vLLM)

Any provider with an OpenAI-compatible `/chat/completions` endpoint works by setting two environment variables — no code change needed if you use the OpenAI client above.

**Groq** (fastest inference — Llama 3.1 70B in ~3 seconds):
```bash
export OPENAI_API_KEY=gsk_...
export OPENAI_BASE_URL=https://api.groq.com/openai/v1
# settings.json: "model": "llama-3.1-70b-versatile"
```

**Together AI:**
```bash
export OPENAI_API_KEY=your_together_key
export OPENAI_BASE_URL=https://api.together.xyz/v1
# settings.json: "model": "meta-llama/Llama-3-70b-chat-hf"
```

**LM Studio** (local, OpenAI-compatible UI):
```bash
export OPENAI_BASE_URL=http://localhost:1234/v1
export OPENAI_API_KEY=lm-studio
# settings.json: "model": "whichever-model-you-loaded"
```

Then use the OpenAI `stream_chat` implementation from Option 2 — it reads `OPENAI_BASE_URL` automatically.

---

### Choosing the right model

| Priority | Best option |
|---|---|
| Privacy + zero cost | Ollama local (`gemma3:12b` or `qwen2.5:14b`) |
| Best analysis quality | Claude Sonnet or GPT-4o |
| Fastest results | Groq (`llama-3.1-70b-versatile`) |
| Very large fields / rich packages | Gemini 1.5 Pro (1M context window) |
| Free tier / experimentation | Groq free tier, Google AI Studio free tier |

---

## File and Folder Layout

```
horse-analyst/                               ← project root (clone here)
│
├── main.py                                  ← CLI entry point and all menu flows
├── analyst.py                               ← Package-file pipeline and pass orchestration
├── prompt.py                                ← All prompt templates (V325.1)
├── db.py                                    ← SQLite schema and all query helpers
├── config.py                                ← Paths, settings, BOM station map
├── ollama_client.py                         ← Streaming LLM client (swap here for other providers)
├── run_post_race_pipeline.py                ← Standalone batch post-race runner
│
├── post-race-analysis.txt                   ← Post-race framework text (loaded at runtime)
├── settings.json                            ← User settings (auto-created on first run)
├── horse_analyst.db                         ← SQLite database (auto-created on first run)
│
├── scrapers/                                ← Data collection modules
│   ├── sportsbet.py                         ← Race card, form, odds (Playwright)
│   ├── results.py                           ← Race results after the event
│   ├── bom.py                               ← Bureau of Meteorology weather
│   ├── racing_com.py                        ← Sectional times
│   └── speedmap.py                          ← Speed map widget positions (optional)
│
├── backups/                                 ← Versioned prompt backups (create manually)
│   ├── prompt_V325_1_original.py
│   └── prompt_V325_2_mychanges.py
│
├── prompt_improvements.txt                  ← Your running log of patterns and corrections
│
└── reports/                                 ← All generated files (auto-created)
    └── 2026-04-28/                          ← One folder per race date
        └── randwick/                        ← One folder per venue (snake_case)
            ├── Race_01_PACKAGE.txt          ← Assembled data document (Stage 1)
            ├── Race_01_PASS_0.txt           ← Forensic Data Audit (audit trail)
            ├── Race_01_PASS_05.txt          ← Tier 1 Macro Sweep
            ├── Race_01_PASS_1.txt           ← Forward Draft Canvas
            ├── Race_01_PASS_2.txt           ← Silo Integrity Audit
            ├── Race_01_PASS_15.txt          ← Probabilistic Projection
            ├── Race_01_ANALYSIS.txt         ← Final prediction (Stage 2) ✓
            ├── Race_01_results.json         ← Raw results JSON
            ├── Race_01_POST_RACE_INPUT.txt  ← PACKAGE + ANALYSIS + Results combined
            └── Race_01_POST_RACE.txt        ← Forensic review (Stage 3) ✓
```

> The `reports/` folder and `horse_analyst.db` are in `.gitignore` by default since they contain your personal race history. Remove them from `.gitignore` if you want to track your analysis history in git.

---

## Database Schema

`horse_analyst.db` is created automatically on first run. Key tables:

| Table | Contents |
|---|---|
| `meetings` | Venue, race date, Sportsbet meeting ID |
| `races` | Race number, name, distance, class, track condition, rail, prize, jump time, age/sex restrictions |
| `runners` | Name, barrier, effective weight, jockey, trainer, odds, official rating, form score, velocity score, career breakdown columns, pace role, settling position |
| `runner_form` | Per-runner historical runs: date, venue, distance, position, margin, SP, jockey, in-running position, winner name |
| `analysis_results` | Every pass output stored as text |
| `race_results` | Raw results JSON |
| `weather` | BOM observations per venue and date |
| `track_conditions` | Track rating, rail position |
| `sectionals` | L200m / L400m / L600m times per runner |
| `speedmap_positions` | Actual speed map widget positions |
| `trainer_stats` | Breakdown by track condition, distance, barrier, spell, month |
| `jockey_stats` | Same breakdown as trainer_stats |
| `backtest_results` | 1A selection outcome per race for performance tracking |

**`analysis_results.analysis_pass` values:**

| Value | Stage | Description |
|---|---|---|
| `PACKAGE` | 1 | Assembled data document (race card + form + speed map) |
| `PASS_0` | 2 | Forensic Data Audit |
| `PASS_05` | 2 | Tier 1 Macro Sweep |
| `PASS_1` | 2 | Forward Draft Canvas |
| `PASS_2` | 2 | Silo Integrity Audit |
| `PASS_15` | 2 | Probabilistic Projection |
| `PASS_4` | 2 | Final Canvas Render — the prediction |
| `POST_RACE_PACKAGE` | 3 | PACKAGE + ANALYSIS + Results combined input |
| `POST_RACE` | 3 | Forensic review and feedback |

---

## Navigation Rules

The same three-level structure applies identically to **Analyse [1]**, **Scrape [5]**, and **Post-Race [6]**:

```
Main Menu
  └─► Flow entry
        │
        ├─ [A]  All venues ────────────────────────────────────────► Main Menu
        │
        └─► Location list  (venues grouped by region or date)
              │
              ├─ [A]  All venues in location list ─────────────────► Main Menu
              │
              └─► Venue  (e.g. Randwick)
                    │
                    ├─ [A]  All races at this venue ───────────────► Location list (refreshed)
                    ├─ [1-N]  Single race ─────────────────────────► Venue race list (refreshed)
                    ├─ [B]  Back ─────────────────────────────────► Location list
                    └─ [M]  Main menu  (2+ levels deep only) ──────► Main Menu
```

| Input | Context | Returns to |
|---|---|---|
| `A` | Top-level all-venues | **Main menu** |
| `A` | Venue race list | **Location list** (counts refresh) |
| `1–N` | Venue race list | **Venue race list** (completed row disappears) |
| `B` | Any level | One level up |
| `M` | Any level ≥ 2 deep | **Main menu** directly |
| `Q` | Any prompt | Exits cleanly |

---

## Roadmap

Contributions welcome. Open an issue or pull request.

- [ ] Progress bar during batch analysis (Rich `Progress` widget)
- [ ] Breadcrumb header line on every screen (`Main > Analyse > Randwick`)
- [ ] Smart re-scrape detection — offer to refresh races scraped >60 min before jump
- [ ] Auto-scrape on launch option in `settings.json`
- [ ] Country filter persistence (remember last region between sessions)
- [ ] Trainer/jockey breakdown stats from sportsbetform.com.au (Phase 2)
- [ ] Actual speed map SVG positions from Sportsbet widget (Phase 4)
- [ ] Performance dashboard — win/place/ROI by silo via `backtest_results`
- [ ] Historical bulk form backfill (last 90 days)
- [ ] Export ANALYSIS and POST_RACE to PDF
- [ ] Telegram/Discord notification on completion
- [ ] Web UI mode (Flask/FastAPI)
- [ ] Cloud DB sync to S3 or Google Drive

---

## License

Copyright 2026 horse-analyst contributors.

Licensed under the **Apache License, Version 2.0**. You may not use this project except in compliance with the License.

```
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

A copy of the full license text is in the [`LICENSE`](LICENSE) file in this repository.

---

> **Disclaimer:** This tool is for research, educational, and analytical purposes only. It does not constitute financial or betting advice. Horse racing involves financial risk. Past analytical accuracy does not guarantee future results. Please gamble responsibly.
