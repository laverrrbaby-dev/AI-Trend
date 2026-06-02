"""Shared paths and constants for the ETF radar."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
NORMALIZED_DIR = DATA_DIR / "normalized"
RADAR_DIR = DATA_DIR / "radar"
STATE_DIR = DATA_DIR / "state"

ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_COMPLETE_TIME = time(15, 30)

MIN_LISTING_DAYS = 120
MIN_LATEST_AMOUNT_YI = 0.30
MIN_AVG_20D_AMOUNT_YI = 0.20
MIN_MARKET_CAP_YI = 2.0
DEFAULT_HISTORY_FETCH_MAX_WORKERS = 12


def now_shanghai() -> datetime:
    return datetime.now(ASIA_SHANGHAI)


def ensure_data_dirs(root: Path = PROJECT_ROOT) -> None:
    for path in [
        root / "data" / "raw",
        root / "data" / "normalized",
        root / "data" / "radar",
        root / "data" / "state",
    ]:
        path.mkdir(parents=True, exist_ok=True)
