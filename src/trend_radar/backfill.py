"""Backfill daily radar outputs from an existing normalized source snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError

from .config import PROJECT_ROOT
from .scoring import ScoreResult, generate_scores


TRADING_DAYS_PER_MONTH = 20


@dataclass(frozen=True)
class BackfillResult:
    source_date: str
    dates: list[str]
    results: list[ScoreResult]


def backfill_scores(months: int = 1, root: Path = PROJECT_ROOT, source_date: str | None = None) -> BackfillResult:
    if months < 1:
        raise ValueError("months must be >= 1")

    resolved_source_date = source_date or latest_source_date(root)
    source_dir = root / "data" / "normalized" / resolved_source_date
    dates = recent_trading_dates(source_dir, resolved_source_date, TRADING_DAYS_PER_MONTH * months)
    if not dates:
        raise RuntimeError(f"No trading dates found in {source_dir / 'history'}")

    results = [generate_scores(data_date=item, root=root, source_date=resolved_source_date) for item in dates]
    return BackfillResult(source_date=resolved_source_date, dates=dates, results=results)


def latest_source_date(root: Path = PROJECT_ROOT) -> str:
    normalized_root = root / "data" / "normalized"
    candidates = []
    if normalized_root.exists():
        for path in normalized_root.iterdir():
            if not path.is_dir():
                continue
            if (path / "spot_candidates.csv").exists() and (path / "shares.csv").exists() and (path / "history").exists():
                candidates.append(path.name)
    if not candidates:
        raise FileNotFoundError(f"No normalized source snapshot found under {normalized_root}")
    return sorted(candidates)[-1]


def recent_trading_dates(source_dir: Path, end_date: str, count: int) -> list[str]:
    dates: set[str] = set()
    for path in sorted((source_dir / "history").glob("*.csv")):
        try:
            frame = pd.read_csv(path, usecols=["日期"])
        except (EmptyDataError, ValueError):
            continue
        if frame.empty:
            continue
        parsed = pd.to_datetime(frame["日期"], errors="coerce").dropna()
        dates.update(item.strftime("%Y-%m-%d") for item in parsed if item.strftime("%Y-%m-%d") <= end_date)

    return sorted(dates)[-count:]


__all__ = [
    "BackfillResult",
    "backfill_scores",
    "latest_source_date",
    "recent_trading_dates",
]
