from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .crypto_alert_config import CryptoAlertConfig
from .crypto_indicators import calculate_indicators


@dataclass(frozen=True)
class CryptoEvent:
    event_id: str
    symbol: str
    event_type: str
    direction: str
    candle_date: str
    confirmed_date: str
    message: str
    evidence: dict[str, Any]
    algorithm_version: str
    preset_id: str
    historical_backfill: bool = False


@dataclass(frozen=True)
class CryptoCurrentState:
    symbol: str
    status: str
    trend: str
    latest_change: str
    latest_change_date: str | None
    data_date: str | None
    quality: str = "正常"
    stale: bool = False


def evaluate_crypto_signals(symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> tuple[CryptoCurrentState, list[CryptoEvent]]:
    values = calculate_indicators(frame, config) if "td_up" not in frame.columns else frame.copy()
    events: list[CryptoEvent] = []
    if "td" in config.enabled_alerts:
        _add_td_events(events, symbol, values, config)
    if "ma120" in config.enabled_alerts:
        _add_ma_events(events, symbol, values, config)
    if "vegas" in config.enabled_alerts:
        _add_vegas_events(events, symbol, values, config)
    trend = _classify_trend(values)
    if "dow" in config.enabled_alerts and trend != "震荡/未确认":
        _append_event(events, symbol, "dow_change", trend, values, len(values) - 1, config, f"道氏结构变化：{trend}")
    latest = events[-1] if events else None
    return CryptoCurrentState(symbol, "有变化" if latest else "无新变化", trend,
                              latest.message if latest else "暂无新的策略变化",
                              latest.confirmed_date if latest else None,
                              str(values.iloc[-1]["日期"]) if len(values) else None), events


def _add_td_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    for column, direction in (("td_up", "上涨计数"), ("td_down", "下跌计数")):
        series = frame[column].fillna(0).astype(int)
        for index, value in series.items():
            if value in (9, 13) and (index == 0 or int(series.iloc[index - 1]) != value):
                _append_event(events, symbol, f"td{value}", direction, frame, index, config, f"TD{value}：{direction}")


def _add_ma_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    ma = frame["sma120"]
    close = frame["收盘价"]
    for index in range(1, len(frame)):
        if pd.notna(ma.iloc[index]) and pd.notna(ma.iloc[index - 1]):
            before = close.iloc[index - 1] - ma.iloc[index - 1]
            after = close.iloc[index] - ma.iloc[index]
            if before * after < 0:
                direction = "上穿MA120" if after > 0 else "下穿MA120"
                _append_event(events, symbol, "ma120_cross", direction, frame, index, config, f"价格发生{direction}")


def _add_vegas_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    upper = frame[["ema144", "ema169"]].max(axis=1)
    lower = frame[["ema144", "ema169"]].min(axis=1)
    close = frame["收盘价"]
    for index in range(1, len(frame)):
        if close.iloc[index - 1] <= upper.iloc[index - 1] and close.iloc[index] > upper.iloc[index]:
            _append_event(events, symbol, "vegas_break", "向上", frame, index, config, "Vegas通道向上突破")
        elif close.iloc[index - 1] >= lower.iloc[index - 1] and close.iloc[index] < lower.iloc[index]:
            _append_event(events, symbol, "vegas_break", "向下", frame, index, config, "Vegas通道向下突破")


def _classify_trend(frame: pd.DataFrame) -> str:
    highs = frame.index[frame.get("swing_high_5", pd.Series(False, index=frame.index))].tolist()
    lows = frame.index[frame.get("swing_low_5", pd.Series(False, index=frame.index))].tolist()
    if len(highs) < 2 or len(lows) < 2:
        return "震荡/未确认"
    higher = frame.loc[highs[-1], "最高价"] > frame.loc[highs[-2], "最高价"] and frame.loc[lows[-1], "最低价"] > frame.loc[lows[-2], "最低价"]
    lower = frame.loc[highs[-1], "最高价"] < frame.loc[highs[-2], "最高价"] and frame.loc[lows[-1], "最低价"] < frame.loc[lows[-2], "最低价"]
    return "上升结构" if higher else "下降结构" if lower else "震荡/未确认"


def _append_event(events: list[CryptoEvent], symbol: str, event_type: str, direction: str, frame: pd.DataFrame, index: int, config: CryptoAlertConfig, message: str) -> None:
    date = str(frame.iloc[index]["日期"])
    event_id = f"{symbol}:{event_type}:{direction}:{date}:{config.algorithm_version}:{config.preset_id}"
    events.append(CryptoEvent(event_id, symbol, event_type, direction, date, date, message,
                              {"close": float(frame.iloc[index]["收盘价"])}, config.algorithm_version, config.preset_id))
