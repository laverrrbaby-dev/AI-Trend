from __future__ import annotations

from dataclasses import dataclass, replace
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
    alert_level: str = "attention"


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
    alert_level: str = "none"
    algorithm_version: str = ""
    preset_id: str = ""


def evaluate_crypto_signals(symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> tuple[CryptoCurrentState, list[CryptoEvent]]:
    values = calculate_indicators(frame, config) if "td_up" not in frame.columns else frame.copy()
    # Replay windows often retain the source frame's index; all signal rules use
    # positional indexing, so normalize it once at the public evaluation seam.
    values = values.reset_index(drop=True)
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
    main_period = int(config.parameters["main_pivot"])
    trend = _classify_trend(values, main_period)
    if "dow" in config.enabled_alerts and trend != "震荡/未确认":
        confirmation_index = _dow_confirmation_index(values, main_period)
        _append_event(events, symbol, "dow_change", trend, values, confirmation_index, config, f"道氏结构变化：{trend}")
    events = _upgrade_confluence(events)
    latest_date = max((event.confirmed_date for event in events), default=None)
    latest_events = [event for event in events if event.confirmed_date == latest_date]
    latest = max(latest_events, key=lambda event: _level_rank(event.alert_level), default=None)
    return CryptoCurrentState(
        symbol,
        "有变化" if latest else "无新变化",
        trend,
        latest.message if latest else "暂无新的策略变化",
        latest.confirmed_date if latest else None,
        str(values.iloc[-1]["日期"]) if len(values) else None,
        alert_level=latest.alert_level if latest else "none",
        algorithm_version=config.algorithm_version,
        preset_id=config.preset_id,
    ), events


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
            direction = _retest_exit_direction(close.iloc[index - 1], ma.iloc[index - 1], close.iloc[index], ma.iloc[index], frame.iloc[index]["atr14"], config)
            if direction:
                _append_event(events, symbol, "ma120_retest_exit", direction, frame, index, config, f"回踩MA120后{direction}")


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
            previous_level = _nearest_level(close.iloc[index - 1], lower.iloc[index - 1], upper.iloc[index - 1])
            current_level = _nearest_level(close.iloc[index], lower.iloc[index], upper.iloc[index])
            direction = _retest_exit_direction(close.iloc[index - 1], previous_level, close.iloc[index], current_level, frame.iloc[index]["atr14"], config)
            if direction:
                _append_event(events, symbol, "vegas_retest_exit", direction, frame, index, config, f"回踩Vegas通道后{direction}")


def _add_price_action_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    for index in range(1, len(frame)):
        trend = _classify_trend_at(frame, index, int(config.parameters["main_pivot"]))
        open_price = float(frame.iloc[index]["开盘价"])
        close = float(frame.iloc[index]["收盘价"])
        high = float(frame.iloc[index]["最高价"])
        low = float(frame.iloc[index]["最低价"])
        previous_open = float(frame.iloc[index - 1]["开盘价"])
        previous_close = float(frame.iloc[index - 1]["收盘价"])
        high_levels = _confirmed_levels(frame, index, "high", config)
        low_levels = _confirmed_levels(frame, index, "low", config)
        for level, scope in high_levels:
            evidence = {"level": level, "level_scope": scope}
            if previous_close <= level < close:
                quality = scope == "main" and _is_quality_breakout(frame, index, True, config)
                _append_event(events, symbol, "breakout", "向上", frame, index, config, "收盘突破已确认关键高点", "important" if quality else "attention", evidence)
            if high > level and close <= level:
                important = scope == "main" and trend == "下降结构" and _is_strong_rejection_close(frame, index, False, config)
                _append_event(events, symbol, "same_day_rejection", "上方拒绝", frame, index, config, "日内越过关键高点后收盘回落", "important" if important else "attention", evidence)
            if previous_close <= level and high >= level and close < level:
                _append_event(events, symbol, "level_reaction", "阻力回落", frame, index, config, "触及已确认阻力位后回落", evidence=evidence)
            lookback = int(config.parameters["delayed_false_break_bars"])
            if close <= level and any(float(frame.iloc[position]["收盘价"]) > level for position in range(max(0, index - lookback), index)):
                important = scope == "main" and trend == "下降结构" and _is_strong_rejection_close(frame, index, False, config)
                _append_event(events, symbol, "delayed_false_break", "向上回落", frame, index, config, "突破后回到关键高点内", "important" if important else "attention", evidence)
        for level, scope in low_levels:
            evidence = {"level": level, "level_scope": scope}
            if previous_close >= level > close:
                quality = scope == "main" and _is_quality_breakout(frame, index, False, config)
                _append_event(events, symbol, "breakout", "向下", frame, index, config, "收盘跌破已确认关键低点", "important" if quality else "attention", evidence)
            if low < level and close >= level:
                important = scope == "main" and trend == "上升结构" and _is_strong_rejection_close(frame, index, True, config)
                _append_event(events, symbol, "same_day_rejection", "下方拒绝", frame, index, config, "日内跌破关键低点后收盘回升", "important" if important else "attention", evidence)
            if previous_close >= level and low <= level and close > level:
                _append_event(events, symbol, "level_reaction", "支撑回升", frame, index, config, "触及已确认支撑位后回升", evidence=evidence)
            lookback = int(config.parameters["delayed_false_break_bars"])
            if close >= level and any(float(frame.iloc[position]["收盘价"]) < level for position in range(max(0, index - lookback), index)):
                important = scope == "main" and trend == "上升结构" and _is_strong_rejection_close(frame, index, True, config)
                _append_event(events, symbol, "delayed_false_break", "向下回升", frame, index, config, "跌破后回到关键低点内", "important" if important else "attention", evidence)
        body = abs(close - open_price)
        previous_body = abs(previous_close - previous_open)
        if body >= float(config.parameters["engulfing_body_ratio"]) * max(previous_body, 1e-12) and ((close > open_price > previous_close > previous_open) or (close < open_price < previous_close < previous_open)):
            direction = "阳线吞没" if close > open_price else "阴线吞没"
            important = _pattern_at_aligned_main_level(frame, index, close > open_price, trend, config)
            _append_event(events, symbol, "engulfing", direction, frame, index, config, f"价格行为变化：{direction}", "important" if important else "attention")
        upper_wick = high - max(open_price, close)
        lower_wick = min(open_price, close) - low
        other = min(upper_wick, lower_wick)
        if body > 0 and max(upper_wick, lower_wick) >= float(config.parameters["pin_wick_body_ratio"]) * body and other <= float(config.parameters["pin_other_wick_body_max"]) * body:
            direction = "下影线" if lower_wick > upper_wick else "上影线"
            bullish = lower_wick > upper_wick
            important = _pattern_at_aligned_main_level(frame, index, bullish, trend, config)
            _append_event(events, symbol, "pin_bar", direction, frame, index, config, f"价格行为变化：{direction}针形", "important" if important else "attention")


def _add_divergence_events(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig) -> None:
    minimum = int(config.parameters["divergence_min_gap"])
    maximum = int(config.parameters["divergence_max_gap"])
    radius = int(config.parameters["divergence_match_radius"])
    _add_early_macd_divergences(events, symbol, frame, config, minimum, maximum, radius)
    for pivot_column, price_column, is_low in (("swing_low_3", "最低价", True), ("swing_high_3", "最高价", False)):
        pivot_indexes = [position for position, value in enumerate(frame.get(pivot_column, pd.Series(False, index=frame.index))) if bool(value)]
        for previous, current in zip(pivot_indexes, pivot_indexes[1:]):
            if not minimum <= current - previous <= maximum:
                continue
            for indicator in ("rsi14", "obv"):
                if indicator not in frame:
                    continue
                before = _indicator_at_pivot(frame[indicator], previous, radius, is_low)
                after = _indicator_at_pivot(frame[indicator], current, radius, is_low)
                if pd.isna(before) or pd.isna(after):
                    continue
                price_before = float(frame.iloc[previous][price_column])
                price_after = float(frame.iloc[current][price_column])
                evidence = {"indicator": indicator, "previous_pivot_date": str(frame.iloc[previous]["日期"]), "current_pivot_date": str(frame.iloc[current]["日期"]), "indicator_before": before, "indicator_after": after}
                if is_low and price_after < price_before and after > before:
                    _append_event(events, symbol, "regular_divergence", "普通看涨", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现普通看涨背离", evidence=evidence)
                elif not is_low and price_after > price_before and after < before:
                    _append_event(events, symbol, "regular_divergence", "普通看跌", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现普通看跌背离", evidence=evidence)
                elif is_low and price_after > price_before and after < before:
                    _append_event(events, symbol, "hidden_divergence", "隐藏看涨", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现隐藏看涨背离", evidence=evidence)
                elif not is_low and price_after < price_before and after > before:
                    _append_event(events, symbol, "hidden_divergence", "隐藏看跌", frame, min(current + 3, len(frame) - 1), config, f"{indicator}出现隐藏看跌背离", evidence=evidence)


def _add_early_macd_divergences(events: list[CryptoEvent], symbol: str, frame: pd.DataFrame, config: CryptoAlertConfig, minimum: int, maximum: int, radius: int) -> None:
    if "macd_hist" not in frame:
        return
    for pivot_column, price_column, is_low in (("swing_low_3", "最低价", True), ("swing_high_3", "最高价", False)):
        pivots = [position for position, value in enumerate(frame.get(pivot_column, pd.Series(False, index=frame.index))) if bool(value)]
        for current in range(1, len(frame)):
            confirmed = [position for position in pivots if position + 3 <= current and minimum <= current - position <= maximum]
            if not confirmed:
                continue
            previous = confirmed[-1]
            price_before = float(frame.iloc[previous][price_column])
            price_after = float(frame.iloc[current][price_column])
            interim = frame[price_column].iloc[previous + 1 : current + 1].astype(float)
            is_new_extreme = price_after <= interim.min() if is_low else price_after >= interim.max()
            if not is_new_extreme:
                continue
            before = _indicator_at_pivot(frame["macd_hist"], previous, radius, is_low)
            after = float(frame.iloc[current]["macd_hist"])
            if pd.isna(before) or pd.isna(after):
                continue
            previous_histogram = float(frame.iloc[current - 1]["macd_hist"])
            turned_toward_bullish = after > previous_histogram
            turned_toward_bearish = after < previous_histogram
            evidence = {"indicator": "macd_hist", "previous_pivot_date": str(frame.iloc[previous]["日期"]), "current_extreme_date": str(frame.iloc[current]["日期"]), "indicator_before": before, "indicator_after": after, "early_close_confirmation": True}
            if is_low and price_after < price_before and after > before and turned_toward_bullish:
                _append_event(events, symbol, "regular_divergence", "普通看涨", frame, current, config, "MACD柱体出现普通看涨背离", "important", evidence)
            elif not is_low and price_after > price_before and after < before and turned_toward_bearish:
                _append_event(events, symbol, "regular_divergence", "普通看跌", frame, current, config, "MACD柱体出现普通看跌背离", "important", evidence)
            elif is_low and price_after > price_before and after < before and turned_toward_bullish:
                _append_event(events, symbol, "hidden_divergence", "隐藏看涨", frame, current, config, "MACD柱体出现隐藏看涨背离", "attention", evidence)
            elif not is_low and price_after < price_before and after > before and turned_toward_bearish:
                _append_event(events, symbol, "hidden_divergence", "隐藏看跌", frame, current, config, "MACD柱体出现隐藏看跌背离", "attention", evidence)


def _indicator_at_pivot(series: pd.Series, index: int, radius: int, is_low: bool) -> float:
    window = series.iloc[max(0, index - radius) : index + radius + 1].dropna()
    if window.empty:
        return float("nan")
    return float(window.min() if is_low else window.max())


def _confirmed_levels(frame: pd.DataFrame, index: int, side: str, config: CryptoAlertConfig) -> list[tuple[float, str]]:
    result: list[tuple[float, str]] = []
    price_column = "最高价" if side == "high" else "最低价"
    for period, scope in ((int(config.parameters["main_pivot"]), "main"), (int(config.parameters["short_pivot"]), "short")):
        column = f"swing_{side}_{period}"
        positions = [position for position, value in enumerate(frame.get(column, pd.Series(False, index=frame.index))) if bool(value) and position + period <= index]
        if not positions:
            continue
        level = float(frame.iloc[positions[-1]][price_column])
        if not any(existing == level for existing, _ in result):
            result.append((level, scope))
    return result


def _is_quality_breakout(frame: pd.DataFrame, index: int, bullish: bool, config: CryptoAlertConfig) -> bool:
    row = frame.iloc[index]
    open_price = float(row["开盘价"])
    close = float(row["收盘价"])
    high = float(row["最高价"])
    low = float(row["最低价"])
    atr = float(row.get("atr14", float("nan")))
    body_gate = False
    if pd.notna(atr) and atr > 0 and high > low:
        outer_fraction = float(config.parameters["breakout_close_outer_fraction"])
        close_gate = close >= high - (high - low) * outer_fraction if bullish else close <= low + (high - low) * outer_fraction
        body_gate = abs(close - open_price) >= float(config.parameters["breakout_body_atr"]) * atr and close_gate
    prior_volume = frame["成交量"].iloc[max(0, index - 20) : index].astype(float)
    volume_gate = bool(len(prior_volume) and prior_volume.mean() > 0 and float(row["成交量"]) >= float(config.parameters["breakout_volume_ratio"]) * float(prior_volume.mean()))
    return body_gate or volume_gate


def _is_strong_rejection_close(frame: pd.DataFrame, index: int, bullish: bool, config: CryptoAlertConfig) -> bool:
    row = frame.iloc[index]
    high = float(row["最高价"])
    low = float(row["最低价"])
    close = float(row["收盘价"])
    if high <= low:
        return False
    outer_fraction = float(config.parameters["breakout_close_outer_fraction"])
    return close >= high - (high - low) * outer_fraction if bullish else close <= low + (high - low) * outer_fraction


def _pattern_at_aligned_main_level(frame: pd.DataFrame, index: int, bullish: bool, trend: str, config: CryptoAlertConfig) -> bool:
    if (bullish and trend != "上升结构") or (not bullish and trend != "下降结构"):
        return False
    side = "low" if bullish else "high"
    levels = [level for level, scope in _confirmed_levels(frame, index, side, config) if scope == "main"]
    if not levels:
        return False
    row = frame.iloc[index]
    price = float(row["最低价"] if bullish else row["最高价"])
    atr = float(row.get("atr14", float("nan")))
    return bool(pd.notna(atr) and any(abs(price - level) <= float(config.parameters["touch_atr"]) * atr for level in levels))


def _upgrade_confluence(events: list[CryptoEvent]) -> list[CryptoEvent]:
    groups: dict[tuple[str, str], list[int]] = {}
    for index, event in enumerate(events):
        if event.event_type not in {"regular_divergence", "hidden_divergence"}:
            continue
        direction = "bullish" if "看涨" in event.direction else "bearish"
        groups.setdefault((event.candle_date, direction), []).append(index)
    upgraded = list(events)
    for indexes in groups.values():
        indicators = {str(events[index].evidence.get("indicator")) for index in indexes}
        if "macd_hist" in indicators and indicators.intersection({"rsi14", "obv"}):
            for index in indexes:
                upgraded[index] = replace(upgraded[index], alert_level="important")
    directional_confirmations: set[tuple[str, str]] = set()
    for event in events:
        if event.event_type not in {"breakout", "ma120_retest_exit", "vegas_retest_exit"}:
            continue
        direction = "bullish" if "向上" in event.direction else "bearish" if "向下" in event.direction else ""
        if direction:
            directional_confirmations.add((event.candle_date, direction))
    for index, event in enumerate(events):
        if event.event_type != "hidden_divergence" or event.evidence.get("indicator") != "macd_hist":
            continue
        direction = "bullish" if "看涨" in event.direction else "bearish"
        if (event.candle_date, direction) in directional_confirmations:
            upgraded[index] = replace(upgraded[index], alert_level="important")
    return upgraded


def _level_rank(level: str) -> int:
    return {"none": 0, "attention": 1, "important": 2}.get(level, 0)


def _nearest_level(price: float, lower: float, upper: float) -> float:
    return float(lower if abs(price - lower) <= abs(price - upper) else upper)


def _retest_exit_direction(previous_close: float, previous_level: float, close: float, level: float, atr: float, config: CryptoAlertConfig) -> str | None:
    threshold = float(config.parameters["touch_atr"]) * float(atr)
    previous_delta = float(previous_close) - float(previous_level)
    current_delta = float(close) - float(level)
    if abs(previous_delta) > threshold or abs(current_delta) <= threshold or previous_delta * current_delta <= 0:
        return None
    return "向上离开" if current_delta > 0 else "向下离开"


def _classify_trend(frame: pd.DataFrame, period: int = 5) -> str:
    return _classify_trend_at(frame, len(frame) - 1, period)


def _classify_trend_at(frame: pd.DataFrame, index: int, period: int) -> str:
    high_flags = frame.get(f"swing_high_{period}", pd.Series(False, index=frame.index))
    low_flags = frame.get(f"swing_low_{period}", pd.Series(False, index=frame.index))
    highs = [position for position, value in enumerate(high_flags) if bool(value) and position + period <= index]
    lows = [position for position, value in enumerate(low_flags) if bool(value) and position + period <= index]
    if len(highs) < 2 or len(lows) < 2:
        return "震荡/未确认"
    higher = frame.iloc[highs[-1]]["最高价"] > frame.iloc[highs[-2]]["最高价"] and frame.iloc[lows[-1]]["最低价"] > frame.iloc[lows[-2]]["最低价"]
    lower = frame.iloc[highs[-1]]["最高价"] < frame.iloc[highs[-2]]["最高价"] and frame.iloc[lows[-1]]["最低价"] < frame.iloc[lows[-2]]["最低价"]
    return "上升结构" if higher else "下降结构" if lower else "震荡/未确认"


def _dow_confirmation_index(frame: pd.DataFrame, period: int = 5) -> int:
    highs = frame.index[frame.get(f"swing_high_{period}", pd.Series(False, index=frame.index))].tolist()
    lows = frame.index[frame.get(f"swing_low_{period}", pd.Series(False, index=frame.index))].tolist()
    return min(max(int(highs[-1]), int(lows[-1])) + period, len(frame) - 1)


def _append_event(
    events: list[CryptoEvent],
    symbol: str,
    event_type: str,
    direction: str,
    frame: pd.DataFrame,
    index: int,
    config: CryptoAlertConfig,
    message: str,
    alert_level: str = "attention",
    evidence: dict[str, Any] | None = None,
) -> None:
    date = str(frame.iloc[index]["日期"])
    event_id = f"{symbol}:{event_type}:{direction}:{date}:{config.algorithm_version}:{config.preset_id}"
    payload = {"close": float(frame.iloc[index]["收盘价"]), **(evidence or {})}
    events.append(CryptoEvent(event_id, symbol, event_type, direction, date, date, message,
                              payload, config.algorithm_version, config.preset_id, alert_level=alert_level))
