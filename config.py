"""
config.py — Central configuration for horse-analyst CLI.
"""

from __future__ import annotations
import json
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
DB_PATH      = PROJECT_ROOT / "horse_analyst.db"
REPORTS_DIR  = PROJECT_ROOT / "reports"
SETTINGS_FILE = PROJECT_ROOT / "settings.json"

# External project paths (for sportsbet_enricher import)
SPORTSBET_SCRAPER_PATH = r"C:\Users\Sam\projects\sportsbet-scraper"
BETFAIR_PATH           = r"C:\Users\Sam\projects\betfair"

# ── Timezone ──────────────────────────────────────────────────────────────────
AEST = ZoneInfo("Australia/Brisbane")   # UTC+10, no DST

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_BASE     = "http://localhost:11434"
OLLAMA_MODEL    = "gemma4:e2b"
OLLAMA_FALLBACK = "gemma:2b"

# ── Sportsbet API ─────────────────────────────────────────────────────────────
SB_BASE_URL  = "https://www.sportsbet.com.au/apigw/sportsbook-racing/Sportsbook/Racing"
SB_CLASS_THOROUGHBRED = 1

# ── BOM Station Map ───────────────────────────────────────────────────────────
# Format: venue_key → (station_id, product_id, filename)
# URL: http://www.bom.gov.au/fwo/{product_id}/{filename}.json
BOM_STATIONS: dict[str, tuple[str, str, str]] = {
    # ── Sydney metro
    "randwick":         ("066062", "IDN60801", "IDN60801.94768"),
    "rosehill":         ("066062", "IDN60801", "IDN60801.94768"),
    "warwick farm":     ("066062", "IDN60801", "IDN60801.94768"),
    "canterbury":       ("066062", "IDN60801", "IDN60801.94768"),
    "kembla grange":    ("068228", "IDN60801", "IDN60801.95711"),
    "hawkesbury":       ("067105", "IDN60801", "IDN60801.94874"),
    "gosford":          ("061055", "IDN60801", "IDN60801.94802"),
    "wyong":            ("061055", "IDN60801", "IDN60801.94802"),
    # ── NSW country
    "newcastle":        ("061055", "IDN60801", "IDN60801.94802"),
    "grafton":          ("058089", "IDN60801", "IDN60801.55054"),
    "tamworth":         ("055325", "IDN60801", "IDN60801.94886"),
    "goulburn":         ("069018", "IDN60801", "IDN60801.94891"),
    "wagga wagga":      ("072150", "IDN60801", "IDN60801.94940"),
    "orange":           ("063254", "IDN60801", "IDN60801.94926"),
    "dubbo":            ("065070", "IDN60801", "IDN60801.94938"),
    "coffs harbour":    ("059040", "IDN60801", "IDN60801.94810"),
    "lismore":          ("058198", "IDN60801", "IDN60801.94819"),
    "casino":           ("058198", "IDN60801", "IDN60801.94819"),
    "armidale":         ("056238", "IDN60801", "IDN60801.94871"),
    "bathurst":         ("063254", "IDN60801", "IDN60801.94926"),
    # ── Melbourne metro
    "flemington":       ("086338", "IDV60801", "IDV60801.95936"),
    "caulfield":        ("086338", "IDV60801", "IDV60801.95936"),
    "moonee valley":    ("086338", "IDV60801", "IDV60801.95936"),
    "sandown":          ("086104", "IDV60801", "IDV60801.95867"),
    "mornington":       ("086220", "IDV60801", "IDV60801.94866"),
    "pakenham":         ("086104", "IDV60801", "IDV60801.95867"),
    "geelong":          ("087031", "IDV60801", "IDV60801.95897"),
    "ballarat":         ("089002", "IDV60801", "IDV60801.95928"),
    "bendigo":          ("081123", "IDV60801", "IDV60801.95921"),
    "cranbourne":       ("086104", "IDV60801", "IDV60801.95867"),
    "seymour":          ("088043", "IDV60801", "IDV60801.95930"),
    # ── Queensland
    "eagle farm":       ("040842", "IDQ60801", "IDQ60801.94576"),
    "doomben":          ("040842", "IDQ60801", "IDQ60801.94576"),
    "sunshine coast":   ("040902", "IDQ60801", "IDQ60801.94575"),
    "toowoomba":        ("041359", "IDQ60801", "IDQ60801.94552"),
    "ipswich":          ("040806", "IDQ60801", "IDQ60801.94571"),
    "gold coast":       ("040717", "IDQ60801", "IDQ60801.94582"),
    "rockhampton":      ("039083", "IDQ60801", "IDQ60801.94549"),
    "townsville":       ("032040", "IDQ60801", "IDQ60801.94294"),
    "cairns":           ("031011", "IDQ60801", "IDQ60801.94287"),
    # ── South Australia
    "morphettville":    ("023090", "IDS60801", "IDS60801.94675"),
    "murray bridge":    ("025509", "IDS60801", "IDS60801.94699"),
    "gawler":           ("023122", "IDS60801", "IDS60801.94672"),
    "mount gambier":    ("026021", "IDS60801", "IDS60801.94738"),
    # ── Western Australia
    "ascot":            ("009021", "IDW60801", "IDW60801.94610"),
    "belmont":          ("009021", "IDW60801", "IDW60801.94610"),
    "gloucester park":  ("009021", "IDW60801", "IDW60801.94610"),
    "pinjarra":         ("009203", "IDW60801", "IDW60801.94608"),
    "bunbury":          ("009741", "IDW60801", "IDW60801.94611"),
    # ── Tasmania
    "elwick":           ("094029", "IDT60801", "IDT60801.94920"),
    "launceston":       ("091311", "IDT60801", "IDT60801.94908"),
    # ── ACT
    "thoroughbred park": ("070351", "IDN60801", "IDN60801.94926"),
    "canberra":          ("070351", "IDN60801", "IDN60801.94926"),
}


def get_aest_now() -> datetime:
    return datetime.now(AEST)


def get_aest_today() -> date:
    return datetime.now(AEST).date()


def get_aest_date_str() -> str:
    return get_aest_today().isoformat()


# ── Settings ──────────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "model": OLLAMA_MODEL,
    "save_reports": True,
    "run_all_passes": True,
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            return {**_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save_settings(d: dict) -> None:
    merged = {**load_settings(), **d}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(merged, f, indent=2)


# ── Report path builder ───────────────────────────────────────────────────────
def report_path(venue: str, race_date: str, race_number: int,
                pass_name: str = "FINAL") -> Path:
    """
    Returns path: reports/{race_date}/{venue_slug}/Race_{N}_{pass_name}.txt
    Creates parent dirs automatically.
    """
    slug = venue.lower().replace(" ", "_")
    rdir = REPORTS_DIR / race_date / slug
    rdir.mkdir(parents=True, exist_ok=True)
    return rdir / f"Race_{race_number:02d}_{pass_name}.txt"
