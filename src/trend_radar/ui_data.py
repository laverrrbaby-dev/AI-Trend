"""UI-facing data access with real-cache first and mock fallback."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import ASIA_SHANGHAI, MARKET_COMPLETE_TIME, PROJECT_ROOT
from .mock_data import (
    build_changes as build_mock_changes,
    build_price_history as build_mock_price_history,
    build_score_breakdown as build_mock_score_breakdown,
    get_available_dates as get_mock_available_dates,
    load_snapshot as load_mock_snapshot,
    resolve_default_date as resolve_mock_default_date,
)
from .scoring import EMA_WINDOWS, build_changes_from_frames


def get_available_dates(root: Path = PROJECT_ROOT) -> list[str]:
    dates = _real_dates(root)
    return dates if dates else get_mock_available_dates()


def resolve_default_date(now: datetime | None = None, root: Path = PROJECT_ROOT) -> str:
    dates = _real_dates(root)
    if not dates:
        return resolve_mock_default_date(now)

    current = now.astimezone(ASIA_SHANGHAI) if now else datetime.now(ASIA_SHANGHAI)
    available = [date.fromisoformat(item) for item in dates]
    today = current.date()
    if today in available and current.time() >= MARKET_COMPLETE_TIME:
        return today.isoformat()

    complete_dates = [item for item in available if item < today or (item == today and current.time() >= MARKET_COMPLETE_TIME)]
    return max(complete_dates).isoformat() if complete_dates else max(available).isoformat()


def load_snapshot(data_date: str, root: Path = PROJECT_ROOT) -> pd.DataFrame:
    path = root / "data" / "radar" / f"{data_date}.csv"
    if not path.exists():
        return load_mock_snapshot(data_date)
    frame = pd.read_csv(path, dtype={"代码": str})
    frame["代码"] = frame["代码"].astype(str).str.zfill(6)
    frame = frame.sort_values("排名").reset_index(drop=True)
    return frame


def build_changes(data_date: str, root: Path = PROJECT_ROOT) -> dict[str, list[str]]:
    meta = load_metadata(data_date, root)
    changes = meta.get("changes")
    if isinstance(changes, dict):
        return {
            "新进Top10": list(changes.get("新进Top10", [])),
            "连续Top10": list(changes.get("连续Top10", [])),
            "跌出Top10": list(changes.get("跌出Top10", [])),
        }

    path = root / "data" / "radar" / f"{data_date}.csv"
    if not path.exists():
        return build_mock_changes(data_date)
    current = load_snapshot(data_date, root)
    previous = _previous_ranking(root, data_date)
    return build_changes_from_frames(current, previous)


def build_price_history(row: pd.Series, data_date: str, root: Path = PROJECT_ROOT) -> pd.DataFrame:
    code = str(row["代码"]).zfill(6)
    path = root / "data" / "normalized" / data_date / "radar_history" / f"{code}.csv"
    if not path.exists():
        return build_mock_price_history(row, data_date)
    history = pd.read_csv(path)
    history["日期"] = pd.to_datetime(history["日期"], errors="coerce")
    for column in [
        "开盘价",
        "最高价",
        "最低价",
        "收盘价",
        "成交额(亿)",
        "成交量",
        "ETF份额(亿份)",
        "EMA13",
        "EMA26",
        "EMA144",
        "EMA169",
    ]:
        if column in history:
            history[column] = pd.to_numeric(history[column], errors="coerce")
    for column in ["开盘价", "最高价", "最低价"]:
        if column not in history:
            history[column] = history["收盘价"]
    history["开盘价"] = history["开盘价"].fillna(history["收盘价"])
    history["最高价"] = history["最高价"].fillna(history[["开盘价", "收盘价"]].max(axis=1))
    history["最低价"] = history["最低价"].fillna(history[["开盘价", "收盘价"]].min(axis=1))
    history["最高价"] = history[["最高价", "开盘价", "收盘价"]].max(axis=1)
    history["最低价"] = history[["最低价", "开盘价", "收盘价"]].min(axis=1)
    history["成交量"] = _volume_with_amount_fallback(history)
    for window in EMA_WINDOWS:
        column = f"EMA{window}"
        if column not in history:
            history[column] = history["收盘价"].ewm(span=window, adjust=False, min_periods=1).mean()
    return history.dropna(subset=["日期", "收盘价"]).reset_index(drop=True)


def build_score_breakdown(row: pd.Series) -> pd.DataFrame:
    required = {"热度分", "资金分", "趋势分", "结构分"}
    if required.issubset(set(row.index)):
        return pd.DataFrame(
            [
                {"维度": "成交额热度", "得分": float(row["热度分"]), "权重": "45%"},
                {"维度": "份额流入", "得分": float(row["资金分"]), "权重": "20%"},
                {"维度": "趋势强度", "得分": float(row["趋势分"]), "权重": "25%"},
                {"维度": "趋势结构", "得分": float(row["结构分"]), "权重": "10%"},
            ]
        )
    return build_mock_score_breakdown(row)


def load_metadata(data_date: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = root / "data" / "radar" / f"{data_date}.json"
    if not path.exists():
        return {"source_mode": "mock", "data_quality": {"status": "MOCK"}}
    return json.loads(path.read_text(encoding="utf-8"))


def is_real_date(data_date: str, root: Path = PROJECT_ROOT) -> bool:
    return (root / "data" / "radar" / f"{data_date}.csv").exists()


def _real_dates(root: Path) -> list[str]:
    radar_dir = root / "data" / "radar"
    if not radar_dir.exists():
        return []
    return sorted(path.stem for path in radar_dir.glob("*.csv"))


def _previous_ranking(root: Path, data_date: str) -> pd.DataFrame | None:
    paths = sorted(path for path in (root / "data" / "radar").glob("*.csv") if path.stem < data_date)
    if not paths:
        return None
    frame = pd.read_csv(paths[-1], dtype={"代码": str})
    frame["代码"] = frame["代码"].astype(str).str.zfill(6)
    return frame.sort_values("排名")


def _volume_with_amount_fallback(frame: pd.DataFrame) -> pd.Series:
    volume = pd.to_numeric(frame.get("成交量", pd.Series(pd.NA, index=frame.index)), errors="coerce")
    if not {"成交额(亿)", "收盘价"}.issubset(frame.columns):
        return volume

    amount = pd.to_numeric(frame["成交额(亿)"], errors="coerce")
    close = pd.to_numeric(frame["收盘价"], errors="coerce")
    derived = (amount * 100_000_000 / close / 100).where(close > 0)
    return volume.fillna(derived)
