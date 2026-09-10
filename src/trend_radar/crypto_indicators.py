from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .crypto_alert_config import CryptoAlertConfig


@dataclass(frozen=True)
class IndicatorFrame:
    frame: pd.DataFrame


def td_continuous_counts(close: pd.Series) -> pd.DataFrame:
    up = [0] * len(close)
    down = [0] * len(close)
    for index in range(4, len(close)):
        if close.iloc[index] > close.iloc[index - 4]:
            up[index] = min(13, up[index - 1] + 1 if up[index - 1] else 1)
        elif close.iloc[index] < close.iloc[index - 4]:
            down[index] = min(13, down[index - 1] + 1 if down[index - 1] else 1)
    return pd.DataFrame({"td_up": up, "td_down": down}, index=close.index)


def confirmed_pivots(frame: pd.DataFrame, left: int, right: int) -> pd.DataFrame:
    result = frame.copy()
    result["swing_high"] = False
    result["swing_low"] = False
    result["confirmed_at_index"] = pd.NA
    result["is_confirmed"] = False
    highs = frame["最高价"].astype(float).tolist()
    lows = frame["最低价"].astype(float).tolist()
    for index in range(left, len(frame) - right):
        high_window = highs[index - left : index + right + 1]
        low_window = lows[index - left : index + right + 1]
        if highs[index] == max(high_window) and high_window.count(highs[index]) == 1:
            result.loc[result.index[index], "swing_high"] = True
            result.loc[result.index[index], "confirmed_at_index"] = index + right
            result.loc[result.index[index], "is_confirmed"] = True
        if lows[index] == min(low_window) and low_window.count(lows[index]) == 1:
            result.loc[result.index[index], "swing_low"] = True
            result.loc[result.index[index], "confirmed_at_index"] = index + right
            result.loc[result.index[index], "is_confirmed"] = True
    return result


def _wilder(series: pd.Series, period: int) -> pd.Series:
    values = series.astype(float).tolist()
    result = [float("nan")] * len(values)
    if len(values) < period:
        return pd.Series(result, index=series.index)
    result[period - 1] = sum(values[:period]) / period
    for index in range(period, len(values)):
        result[index] = (result[index - 1] * (period - 1) + values[index]) / period
    return pd.Series(result, index=series.index)


def calculate_indicators(frame: pd.DataFrame, config: CryptoAlertConfig) -> pd.DataFrame:
    result = frame.copy()
    params = config.parameters
    close = result["收盘价"].astype(float)
    high = result["最高价"].astype(float)
    low = result["最低价"].astype(float)
    volume = result["成交量"].astype(float)
    result["sma120"] = close.rolling(int(params["sma_period"]), min_periods=int(params["sma_period"])).mean()
    result["ema144"] = close.ewm(span=int(params["vegas_fast"]), adjust=False, min_periods=1).mean()
    result["ema169"] = close.ewm(span=int(params["vegas_slow"]), adjust=False, min_periods=1).mean()
    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    result["macd"] = ema12 - ema26
    result["macd_signal"] = result["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
    result["macd_hist"] = result["macd"] - result["macd_signal"]
    delta = close.diff().fillna(0)
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = _wilder(gains, int(params["rsi_period"]))
    avg_loss = _wilder(losses, int(params["rsi_period"]))
    result["rsi14"] = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, pd.NA)))
    result["rsi14"] = result["rsi14"].fillna(100.0).where(avg_gain.notna())
    direction = close.diff().fillna(0).map(lambda value: 1 if value > 0 else -1 if value < 0 else 0)
    result["obv"] = (direction * volume).cumsum()
    previous_close = close.shift(1)
    true_range = pd.concat([high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
    result["atr14"] = _wilder(true_range, int(params["atr_period"]))
    for period in (int(params["short_pivot"]), int(params["main_pivot"])):
        pivots = confirmed_pivots(result, period, period)
        result[f"swing_high_{period}"] = pivots["swing_high"]
        result[f"swing_low_{period}"] = pivots["swing_low"]
    td = td_continuous_counts(close)
    result[["td_up", "td_down"]] = td
    return result
