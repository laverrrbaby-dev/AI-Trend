"""Explainable, fact-only anomaly rules for US stock turnover snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


ANOMALY_COLUMNS = [
    "异动等级",
    "异动标签",
    "成交额放大倍数",
    "昨日排名",
    "排名变化",
    "异动阈值",
    "异动数据状态",
]


@dataclass(frozen=True)
class AnomalyResult:
    level: str = ""
    labels: tuple[str, ...] = ()
    turnover_multiple: float | None = None
    previous_rank: int | None = None
    rank_change: int | None = None
    thresholds: tuple[str, ...] = ()
    data_status: str = "历史数据不足，未评估成交额放大"

    @property
    def is_attention(self) -> bool:
        return bool(self.level)

    def as_dict(self) -> dict[str, Any]:
        return {
            "异动等级": self.level,
            "异动标签": "、".join(self.labels),
            "成交额放大倍数": self.turnover_multiple,
            "昨日排名": self.previous_rank,
            "排名变化": self.rank_change,
            "异动阈值": "；".join(self.thresholds),
            "异动数据状态": self.data_status,
        }


def evaluate_anomalies(
    snapshot: pd.DataFrame,
    history_by_symbol: dict[str, pd.DataFrame] | None = None,
    previous_snapshot: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return a copy of a snapshot enriched with deterministic anomaly facts."""

    history_by_symbol = history_by_symbol or {}
    previous_ranks = _rank_map(previous_snapshot)
    current = snapshot.copy()
    current = current.sort_values("成交额", ascending=False).reset_index(drop=True)
    results: list[dict[str, Any]] = []
    for index, row in current.iterrows():
        symbol = str(row.get("代码") or "").strip().upper()
        result = evaluate_row(
            row,
            current_rank=index + 1,
            previous_rank=previous_ranks.get(symbol),
            history=history_by_symbol.get(symbol),
        )
        results.append(result.as_dict())
    for column in ANOMALY_COLUMNS:
        current[column] = [result[column] for result in results]
    return current


def evaluate_row(
    row: pd.Series | dict[str, Any],
    *,
    current_rank: int,
    previous_rank: int | None,
    history: pd.DataFrame | None,
) -> AnomalyResult:
    labels: list[str] = []
    levels: list[str] = []
    turnover_multiple: float | None = None
    data_status = "历史数据不足，未评估成交额放大"
    thresholds = (
        "成交额≥2倍20日均额：关注；≥3倍：强异动",
        "新进Top30：关注；排名跃升≥10位：关注；≥20位：强异动",
        "|涨跌幅|≥5%：关注；≥10%：强异动",
    )

    if history is not None:
        history_turnover = pd.to_numeric(history.get("成交额"), errors="coerce").dropna()
        history_turnover = history_turnover[history_turnover > 0].tail(20)
        current_turnover = _number(row.get("成交额"))
        if len(history_turnover) >= 20 and current_turnover is not None:
            baseline = float(history_turnover.mean())
            if baseline > 0:
                turnover_multiple = current_turnover / baseline
                data_status = "历史数据完整"
                if turnover_multiple >= 3:
                    labels.append("成交额强放大")
                    levels.append("强异动")
                elif turnover_multiple >= 2:
                    labels.append("成交额放大")
                    levels.append("关注")

    previous_rank_value = previous_rank
    rank_change = previous_rank - current_rank if previous_rank is not None else None
    if previous_rank is None:
        labels.append("新进Top30")
        levels.append("关注")
    elif rank_change is not None:
        if rank_change >= 20:
            labels.append("排名大幅跃升")
            levels.append("强异动")
        elif rank_change >= 10:
            labels.append("排名跃升")
            levels.append("关注")

    price_change = _number(row.get("涨跌幅"))
    if price_change is not None:
        if abs(price_change) >= 10:
            labels.append("价格强异动")
            levels.append("强异动")
        elif abs(price_change) >= 5:
            labels.append("价格异动")
            levels.append("关注")

    level = "强异动" if "强异动" in levels else ("关注" if levels else "")
    return AnomalyResult(
        level=level,
        labels=tuple(labels),
        turnover_multiple=turnover_multiple,
        previous_rank=previous_rank_value,
        rank_change=rank_change,
        thresholds=thresholds,
        data_status=data_status,
    )


def serialize_anomaly_details(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-safe detail payload for the UI expander."""

    def value(name: str) -> Any:
        raw = row.get(name) if isinstance(row, dict) else row.get(name)
        if pd.isna(raw) if not isinstance(raw, str) else False:
            return None
        return raw

    return {
        "代码": str(value("代码") or ""),
        "异动等级": str(value("异动等级") or "无异动"),
        "异动标签": str(value("异动标签") or "无"),
        "成交额": value("成交额"),
        "成交额放大倍数": value("成交额放大倍数"),
        "昨日排名": value("昨日排名"),
        "排名变化": value("排名变化"),
        "异动阈值": str(value("异动阈值") or ""),
        "涨跌幅": value("涨跌幅"),
        "异动数据状态": str(value("异动数据状态") or ""),
    }


def _rank_map(snapshot: pd.DataFrame | None) -> dict[str, int]:
    if snapshot is None or snapshot.empty or "代码" not in snapshot:
        return {}
    ordered = snapshot.sort_values("成交额", ascending=False).reset_index(drop=True)
    return {str(code).strip().upper(): index + 1 for index, code in enumerate(ordered["代码"])}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None
