"""
config.py — BMSS Order App
All path and threshold configuration in one place.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Data root — adjust this to where your CSVs live
# ---------------------------------------------------------------------------

# Default: parent of the backend/ directory (i.e. the "BMSS Order App" folder)
DATA_ROOT = str(Path(__file__).parent.parent)

# Override with environment variable if set
if os.environ.get("BMSS_DATA_ROOT"):
    DATA_ROOT = os.environ["BMSS_DATA_ROOT"]

# ---------------------------------------------------------------------------
# Output paths (JSON files the frontend reads)
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path(DATA_ROOT) / "data"

OUTPUT_FILES = {
    "orders": OUTPUT_DIR / "orders.json",
    "momentum": OUTPUT_DIR / "momentum.json",
    "historical": OUTPUT_DIR / "historical.json",
    "margins": OUTPUT_DIR / "margins.json",
    "pricing": OUTPUT_DIR / "pricing.json",
    "revival": OUTPUT_DIR / "revival.json",
    "summary": OUTPUT_DIR / "summary.json",
}

# ---------------------------------------------------------------------------
# Engine thresholds
# ---------------------------------------------------------------------------

REORDER_THRESHOLD_WEEKS = 1.0
TARGET_WEEKS_COVER = 2.0
MIN_REVENUE_FOR_ANALYSIS = 50.0
