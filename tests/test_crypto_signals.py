from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_config import load_crypto_alert_config  # noqa: E402
from trend_radar.crypto_indicators import calculate_indicators  # noqa: E402
from trend_radar.crypto_signals import evaluate_crypto_signals  # noqa: E402


class CryptoTrendSignalTests(unittest.TestCase):
    def test_td_emits_only_first_9_and_13_in_same_continuous_run(self) -> None:
        close = [10, 10, 10, 10] + list(range(11, 26)) + [20, 19]
        frame = _frame(close)
        _, events = evaluate_crypto_signals("BTC", frame, load_crypto_alert_config(PROJECT_ROOT))
        self.assertEqual([(event.event_type, event.direction) for event in events if event.event_type.startswith("td")], [
            ("td9", "上涨计数"), ("td13", "上涨计数")
        ])

    def test_ma120_and_vegas_events_are_neutral_attention_changes(self) -> None:
        close = [100.0] * 130 + [105.0, 90.0, 80.0]
        _, events = evaluate_crypto_signals("BTC", _frame(close), load_crypto_alert_config(PROJECT_ROOT))
        self.assertTrue(any(event.event_type == "ma120_cross" for event in events))
        self.assertTrue(all("买" not in event.message and "卖" not in event.message for event in events))

    def test_divergence_uses_confirmed_pivots_and_detects_regular_and_hidden_forms(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("divergence",))
        frame = _frame([100.0] * 20)
        frame["td_up"] = 0
        frame["td_down"] = 0
        frame["swing_low_3"] = False
        frame.loc[[5, 12], "swing_low_3"] = True
        frame.loc[5, ["最低价", "macd_hist", "rsi14", "obv"]] = [90.0, -4.0, 25.0, 100.0]
        frame.loc[12, ["最低价", "macd_hist", "rsi14", "obv"]] = [80.0, -2.0, 35.0, 200.0]
        _, events = evaluate_crypto_signals("BTC", frame, config)
        self.assertEqual({event.direction for event in events}, {"普通看涨"})
        self.assertEqual({event.confirmed_date for event in events}, {"2026-01-16"})

    def test_dow_event_uses_latest_pivot_confirmation_date(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("dow",))
        frame = _frame([100.0] * 20)
        frame["td_up"] = 0
        frame["swing_high_5"] = False
        frame["swing_low_5"] = False
        frame.loc[[4, 10], "swing_high_5"] = True
        frame.loc[[4, 10], "swing_low_5"] = True
        frame.loc[4, ["最高价", "最低价"]] = [110.0, 90.0]
        frame.loc[10, ["最高价", "最低价"]] = [120.0, 100.0]
        _, events = evaluate_crypto_signals("BTC", frame, config)
        self.assertEqual([(event.event_type, event.confirmed_date) for event in events], [("dow_change", "2026-01-16")])


def _frame(close: list[float]) -> pd.DataFrame:
    values = pd.Series(close, dtype=float)
    return pd.DataFrame({
        "日期": [f"2026-01-{(i % 28) + 1:02d}" for i in range(len(close))],
        "开盘价": values - 1,
        "最高价": values + 1,
        "最低价": values - 2,
        "收盘价": values,
        "成交量": [100.0] * len(values),
        "成交额": values * 100,
        "is_complete": [True] * len(values),
    })


if __name__ == "__main__":
    unittest.main()
