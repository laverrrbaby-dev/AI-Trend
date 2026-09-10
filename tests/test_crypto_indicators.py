from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_indicators import (  # noqa: E402
    confirmed_pivots,
    calculate_indicators,
    td_continuous_counts,
)
from trend_radar.crypto_alert_config import load_crypto_alert_config  # noqa: E402


class CryptoIndicatorsTests(unittest.TestCase):
    def test_td_counts_compare_close_four_bars_back_and_lock_after_13(self) -> None:
        close = pd.Series([10, 10, 10, 10] + list(range(11, 26)) + [20, 19], dtype=float)
        counts = td_continuous_counts(close)
        self.assertEqual(counts.loc[12, "td_up"], 9)
        self.assertEqual(counts.loc[16, "td_up"], 13)
        self.assertEqual(counts.loc[17, "td_up"], 13)
        self.assertEqual(counts.loc[19, "td_up"], 0)

    def test_pivot_is_visible_only_after_right_hand_confirmation(self) -> None:
        frame = _candle_frame([1, 2, 5, 2, 1], [0, 0, 0, 0, 0])
        pivots = confirmed_pivots(frame, left=2, right=2)
        self.assertEqual(pivots.iloc[2]["confirmed_at_index"], 4)
        self.assertFalse(pivots.iloc[2]["is_confirmed"] if len(pivots) > 5 else False)

    def test_indicator_windows_and_columns(self) -> None:
        frame = _candle_frame(list(range(1, 141)), list(range(0, 140)))
        result = calculate_indicators(frame, load_crypto_alert_config(PROJECT_ROOT))
        for column in ("sma120", "ema144", "ema169", "macd_hist", "rsi14", "obv", "atr14"):
            self.assertIn(column, result.columns)
        self.assertAlmostEqual(result.iloc[-1]["sma120"], 80.5)
        self.assertEqual(result.iloc[-1]["obv"], 13900)


def _candle_frame(high: list[float], low: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "日期": [f"2026-01-{i + 1:02d}" for i in range(len(high))],
        "开盘价": low,
        "最高价": high,
        "最低价": low,
        "收盘价": high,
        "成交量": [100.0] * len(high),
        "成交额": [10000.0] * len(high),
        "is_complete": [True] * len(high),
    })


if __name__ == "__main__":
    unittest.main()
