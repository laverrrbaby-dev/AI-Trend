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
    if "price_action" in config.enabled_alerts:
        _add_price_action_events(events, symbol, values, config)
    if "divergence" in config.enabled_alerts:
        _add_divergence_events(events, symbol, values, config)
    if "ma120" in config.enabled_alerts:
        _add_ma_events(events, symbol, values, config)
    if "vegas" in config.enabled_alerts:
        _add_vegas_events(events, symbol, values, config)
    trend = _classify_trend(values)
    if "dow" in config.enabled_alerts and trend != "震荡/未确认":
        confirmation_index = _dow_confirmation_index(values)
        _append_event(events, symbol, "dow_change", trend, values, confirmation_index, config, f"道氏结构变化：{trend}")
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
        if pd.notna(ma.iloc[index]) and pd.notna(frame.iloc[index].get("atr14")):
            if abs(close.iloc[index] - ma.iloc[index]) <= float(config.parameters["touch_atr"]) * float(frame.iloc[index]["atr14"]):
                _append_event(events, symbol, "ma120_touch", "触及MA120", frame, index, config, "价格接近MA120")


def _add_vegas_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    upper = frame[["ema144", "ema169"]].max(axis=1)
    lower = frame[["ema144", "ema169"]].min(axis=1)
    close = frame["收盘价"]
    for index in range(1, len(frame)):
        if close.iloc[index - 1] <= upper.iloc[index - 1] and close.iloc[index] > upper.iloc[index]:
            _append_event(events, symbol, "vegas_break", "向上", frame, index, config, "Vegas通道向上突破")
        elif close.iloc[index - 1] >= lower.iloc[index - 1] and close.iloc[index] < lower.iloc[index]:
            _append_event(events, symbol, "vegas_break", "向下", frame, index, config, "Vegas通道向下突破")
        if pd.notna(frame.iloc[index].get("atr14")):
            distance = min(abs(close.iloc[index] - upper.iloc[index]), abs(close.iloc[index] - lower.iloc[index]))
            if distance <= float(config.parameters["touch_atr"]) * float(frame.iloc[index]["atr14"]):
                _append_event(events, symbol, "vegas_touch", "触及通道", frame, index, config, "价格接近Vegas通道")


def _add_price_action_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    for index in range(1, len(frame)):
        open_price = float(frame.iloc[index]["开盘价"])
        close = float(frame.iloc[index]["收盘价"])
        high = float(frame.iloc[index]["最高价"])
        low = float(frame.iloc[index]["最低价"])
        previous_open = float(frame.iloc[index - 1]["开盘价"])
        previous_close = float(frame.iloc[index - 1]["收盘价"])
        confirmed_highs = [position for position, value in enumerate(frame.get("swing_high_3", pd.Series(False, index=frame.index))) if bool(value) and position + 3 <= index]
        confirmed_lows = [position for position, value in enumerate(frame.get("swing_low_3", pd.Series(False, index=frame.index))) if bool(value) and position + 3 <= index]
        if confirmed_highs:
            level = float(frame.iloc[confirmed_highs[-1]]["最高价"])
            if previous_close <= level < close:
                _append_event(events, symbol, "breakout", "向上", frame, index, config, "收盘突破已确认关键高点")
            lookback = int(config.parameters["delayed_false_break_bars"])
            if close <= level and any(float(frame.iloc[position]["收盘价"]) > level for position in range(max(0, index - lookback), index)):
                _append_event(events, symbol, "delayed_false_break", "向上回落", frame, index, config, "突破后回到关键高点内")
        if confirmed_lows:
            level = float(frame.iloc[confirmed_lows[-1]]["最低价"])
            if previous_close >= level > close:
                _append_event(events, symbol, "breakout", "向下", frame, index, config, "收盘跌破已确认关键低点")
            lookback = int(config.parameters["delayed_false_break_bars"])
            if close >= level and any(float(frame.iloc[position]["收盘价"]) < level for position in range(max(0, index - lookback), index)):
                _append_event(events, symbol, "delayed_false_break", "向下回升", frame, index, config, "跌破后回到关键低点内")
        body = abs(close - open_price)
        previous_body = abs(previous_close - previous_open)
        if body >= float(config.parameters["engulfing_body_ratio"]) * max(previous_body, 1e-12) and ((close > open_price > previous_close > previous_open) or (close < open_price < previous_close < previous_open)):
            direction = "阳线吞没" if close > open_price else "阴线吞没"
            _append_event(events, symbol, "engulfing", direction, frame, index, config, f"价格行为变化：{direction}")
        upper_wick = high - max(open_price, close)
        lower_wick = min(open_price, close) - low
        other = min(upper_wick, lower_wick)
        if body > 0 and max(upper_wick, lower_wick) >= float(config.parameters["pin_wick_body_ratio"]) * body and other <= float(config.parameters["pin_other_wick_body_max"]) * body:
            direction = "下影线" if lower_wick > upper_wick else "上影线"
            _append_event(events, symbol, "pin_bar", direction, frame, index, config, f"价格行为变化：{direction}针形")


def _add_divergence_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    minimum = int(config.parameters["divergence_min_gap"])
    maximum = int(config.parameters["divergence_max_gap"])
    radius = int(config.parameters["divergence_match_radius"])
    for pivot_column, price_column, is_low in (("swing_low_3", "最低价", True), ("swing_high_3", "最高价", False)):
        pivot_indexes = [position for position, value in enumerate(frame.get(pivot_column, pd.Series(False, index=frame.index))) if bool(value)]
        for previous, current in zip(pivot_indexes, pivot_indexes[1:]):
            if not minimum <= current - previous <= maximum:
                continue
            for indicator in ("macd_hist", "rsi14", "obv"):
                if indicator not in frame:
                    continue
                before = _indicator_at_pivot(frame[indicator], previous, radius, is_low)
                after = _indicator_at_pivot(frame[indicator], current, radius, is_low)
                if pd.isna(before) or pd.isna(after):
                    continue
                price_before = float(frame.iloc[previous][price_column])
                price_after = float(frame.iloc[current][price_column])
                if is_low and price_after < price_before and after > before:
                    _append_event(events, symbol, "regular_divergence", "普通看涨", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现普通看涨背离")
                elif not is_low and price_after > price_before and after < before:
                    _append_event(events, symbol, "regular_divergence", "普通看跌", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现普通看跌背离")
                elif is_low and price_after > price_before and after < before:
                    _append_event(events, symbol, "hidden_divergence", "隐藏看涨", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现隐藏看涨背离")
                elif not is_low and price_after < price_before and after > before:
                    _append_event(events, symbol, "hidden_divergence", "隐藏看跌", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现隐藏看跌背离")


def _indicator_at_pivot(series: pd.Series, index: int, radius: int, is_low: bool) -> float:
    window = series.iloc[max(0, index - radius) : index + radius + 1].dropna()
    if window.empty:
        return float("nan")
    return float(window.min() if is_low else window.max())


def _classify_trend(frame: pd.DataFrame) -> str:
    highs = frame.index[frame.get("swing_high_5", pd.Series(False, index=frame.index))].tolist()
    lows = frame.index[frame.get("swing_low_5", pd.Series(False, index=frame.index))].tolist()
    if len(highs) < 2 or len(lows) < 2:
        return "震荡/未确认"
    higher = frame.loc[highs[-1], "最高价"] > frame.loc[highs[-2], "最高价"] and frame.loc[lows[-1], "最低价"] > frame.loc[lows[-2], "最低价"]
    lower = frame.loc[highs[-1], "最高价"] < frame.loc[highs[-2], "最高价"] and frame.loc[lows[-1], "最低价"] < frame.loc[lows[-2], "最低价"]
    return "上升结构" if higher else "下降结构" if lower else "震荡/未确认"


def _dow_confirmation_index(frame: pd.DataFrame) -> int:
    highs = frame.index[frame.get("swing_high_5", pd.Series(False, index=frame.index))].tolist()
    lows = frame.index[frame.get("swing_low_5", pd.Series(False, index=frame.index))].tolist()
    return min(max(int(highs[-1]), int(lows[-1])) + 5, len(frame) - 1)


def _append_event(events: list[CryptoEvent], symbol: str, event_type: str, direction: str, frame: pd.DataFrame, index: int, config: CryptoAlertConfig, message: str) -> None:
    date = str(frame.iloc[index]["日期"])
    event_id = f"{symbol}:{event_type}:{direction}:{date}:{config.algorithm_version}:{config.preset_id}"
    events.append(CryptoEvent(event_id, symbol, event_type, direction, date, date, message,
                              {"close": float(frame.iloc[index]["收盘价"])}, config.algorithm_version, config.preset_id))
