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
    def test_signal_records_the_next_0800_china_alert_time(self) -> None:
        close = [10, 10, 10, 10] + list(range(11, 20))

        _, events = evaluate_crypto_signals("BTC", _frame(close), load_crypto_alert_config(PROJECT_ROOT))

        td9 = next(event for event in events if event.event_type == "td9")
        self.assertEqual(td9.candle_date, "2026-01-13")
        self.assertEqual(td9.alert_time, "2026-01-14 08:00")

    def test_bullish_pin_bar_at_ma120_in_an_uptrend_is_important(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action", "ma120"))
        frame = _frame([105.0] * 18)
        frame["td_up"] = 0
        frame["swing_high_5"] = False
        frame["swing_low_5"] = False
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame.loc[[5, 11], "swing_high_5"] = True
        frame.loc[[6, 12], "swing_low_5"] = True
        frame.loc[5, "最高价"], frame.loc[11, "最高价"] = 110.0, 120.0
        frame.loc[6, "最低价"], frame.loc[12, "最低价"] = 90.0, 100.0
        frame["sma120"] = 104.0
        frame["ema144"] = 130.0
        frame["ema169"] = 131.0
        frame["atr14"] = 4.0
        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价"]] = [108.0, 110.5, 103.0, 110.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        pin = next(event for event in events if event.event_type == "pin_bar" and event.candle_date == "2026-01-18")
        self.assertEqual(pin.alert_level, "important")

    def test_early_macd_divergence_is_not_repeated_and_emits_invalidation(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("divergence",))
        frame = _frame([100.0] * 15)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame.loc[5, ["swing_high_3", "最高价"]] = [True, 110.0]
        frame["macd_hist"] = 0.0
        frame.loc[5, "macd_hist"] = 5.0
        frame.loc[11, "macd_hist"] = 3.0
        frame.loc[12, ["最高价", "macd_hist"]] = [120.0, 2.0]
        frame.loc[13, ["最高价", "macd_hist"]] = [121.0, 4.0]
        frame.loc[14, ["最高价", "macd_hist"]] = [122.0, 6.0]

        _, events = evaluate_crypto_signals("XAUT", frame, config)

        macd_events = [event for event in events if event.evidence.get("indicator") == "macd_hist"]
        self.assertEqual([(event.event_type, event.candle_date) for event in macd_events], [
            ("regular_divergence", "2026-01-13"),
            ("divergence_invalidated", "2026-01-15"),
        ])
        self.assertEqual(macd_events[-1].alert_level, "none")

    def test_early_macd_divergence_invalidates_when_histogram_recovers_without_a_new_price_extreme(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("divergence",))
        frame = _frame([100.0] * 14)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame.loc[5, ["swing_high_3", "最高价"]] = [True, 110.0]
        frame["macd_hist"] = 0.0
        frame.loc[5, "macd_hist"] = 5.0
        frame.loc[11, "macd_hist"] = 3.0
        frame.loc[12, ["最高价", "macd_hist"]] = [120.0, 2.0]
        frame.loc[13, ["最高价", "macd_hist"]] = [119.0, 6.0]

        _, events = evaluate_crypto_signals("XAUT", frame, config)

        macd_events = [event for event in events if event.evidence.get("indicator") == "macd_hist"]
        self.assertEqual([(event.event_type, event.candle_date) for event in macd_events], [
            ("regular_divergence", "2026-01-13"),
            ("divergence_invalidated", "2026-01-14"),
        ])
    def test_macd_regular_divergence_alerts_on_the_new_extreme_close_without_waiting_three_bars(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("divergence",))
        frame = _frame([100.0] * 13)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame.loc[5, ["swing_high_3", "最高价"]] = [True, 110.0]
        frame.loc[12, "最高价"] = 120.0
        frame["macd_hist"] = 0.0
        frame.loc[5, "macd_hist"] = 5.0
        frame.loc[11, "macd_hist"] = 3.0
        frame.loc[12, "macd_hist"] = 2.0

        _, events = evaluate_crypto_signals("XAUT", frame, config)

        event = next(event for event in events if event.evidence.get("indicator") == "macd_hist")
        self.assertEqual((event.event_type, event.direction), ("regular_divergence", "普通看跌"))
        self.assertEqual(event.candle_date, "2026-01-13")
        self.assertEqual(event.alert_level, "important")

    def test_macd_hidden_divergence_is_attention_level(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("divergence",))
        frame = _frame([100.0] * 13)
        frame["td_up"] = 0
        frame["swing_low_3"] = False
        frame["swing_high_3"] = False
        frame.loc[5, ["swing_low_3", "最低价"]] = [True, 90.0]
        frame.loc[12, "最低价"] = 95.0
        frame.loc[6:11, "最低价"] = 98.0
        frame["macd_hist"] = 0.0
        frame.loc[5, "macd_hist"] = -2.0
        frame.loc[11, "macd_hist"] = -5.0
        frame.loc[12, "macd_hist"] = -4.0

        _, events = evaluate_crypto_signals("BTC", frame, config)

        event = next(event for event in events if event.evidence.get("indicator") == "macd_hist")
        self.assertEqual((event.event_type, event.direction, event.alert_level), ("hidden_divergence", "隐藏看涨", "attention"))

    def test_hidden_macd_divergence_upgrades_when_same_day_breakout_confirms_direction(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("divergence", "price_action"))
        frame = _frame([100.0] * 13)
        frame["td_up"] = 0
        frame["swing_low_3"] = False
        frame["swing_high_3"] = False
        frame.loc[4, ["swing_high_3", "最高价"]] = [True, 105.0]
        frame.loc[5, ["swing_low_3", "最低价"]] = [True, 90.0]
        frame.loc[6:11, "最低价"] = 98.0
        frame.loc[12, ["最低价", "最高价", "收盘价"]] = [95.0, 112.0, 110.0]
        frame["macd_hist"] = 0.0
        frame.loc[5, "macd_hist"] = -2.0
        frame.loc[11, "macd_hist"] = -5.0
        frame.loc[12, "macd_hist"] = -4.0

        _, events = evaluate_crypto_signals("BTC", frame, config)

        hidden = next(event for event in events if event.event_type == "hidden_divergence" and event.evidence.get("indicator") == "macd_hist")
        self.assertEqual(hidden.alert_level, "important")

    def test_quality_breakout_of_confirmed_main_level_is_important_without_volume_expansion(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 10)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame["swing_high_5"] = False
        frame["swing_low_5"] = False
        frame.loc[2, ["swing_high_5", "最高价"]] = [True, 110.0]
        frame.loc[8, ["开盘价", "最高价", "最低价", "收盘价", "atr14", "成交量"]] = [100.0, 113.0, 99.0, 112.0, 10.0, 100.0]

        _, events = evaluate_crypto_signals("BNB", frame, config)

        event = next(event for event in events if event.event_type == "breakout")
        self.assertEqual(event.alert_level, "important")
        self.assertEqual(event.evidence["level_scope"], "main")
        self.assertIn("主要前高", event.message)

    def test_td_and_plain_level_reaction_are_attention_level(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("td", "price_action"))
        frame = _frame([10, 10, 10, 10] + list(range(11, 23)))
        frame = calculate_indicators(frame, config)
        frame["swing_low_3"] = False
        frame.loc[2, ["swing_low_3", "最低价"]] = [True, 8.0]
        frame.loc[10, ["最低价", "收盘价"]] = [7.5, 9.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        attention_types = {event.event_type for event in events if event.alert_level == "attention"}
        self.assertIn("td9", attention_types)
        self.assertIn("level_reaction", attention_types)

    def test_main_level_rejection_without_a_strong_close_stays_attention(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([80.0] * 18)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame["swing_high_5"] = False
        frame["swing_low_5"] = False
        frame.loc[[1, 7], "swing_high_5"] = True
        frame.loc[[2, 8], "swing_low_5"] = True
        frame.loc[1, "最高价"], frame.loc[7, "最高价"] = 90.0, 100.0
        frame.loc[2, "最低价"], frame.loc[8, "最低价"] = 60.0, 70.0
        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价", "atr14"]] = [75.0, 90.0, 69.0, 78.0, 10.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        event = next(event for event in events if event.event_type == "same_day_rejection" and event.candle_date == "2026-01-18")
        self.assertEqual(event.alert_level, "attention")

    def test_bearish_pin_bar_above_main_high_is_important_even_in_an_uptrend(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 18)
        frame["td_up"] = 0
        for column in ("swing_high_3", "swing_low_3", "swing_high_5", "swing_low_5"):
            frame[column] = False
        frame.loc[[1, 7], "swing_high_5"] = True
        frame.loc[[2, 8], "swing_low_5"] = True
        frame.loc[1, "最高价"], frame.loc[7, "最高价"] = 90.0, 100.0
        frame.loc[2, "最低价"], frame.loc[8, "最低价"] = 60.0, 70.0
        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价"]] = [100.0, 103.0, 98.5, 99.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        rejection = next(event for event in events if event.event_type == "same_day_rejection" and event.candle_date == "2026-01-18")
        self.assertEqual((rejection.alert_level, rejection.message), ("important", "冲高突破失败·偏空"))

        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价"]] = [100.0, 101.0, 99.5, 99.8]
        _, mild_events = evaluate_crypto_signals("BTC", frame, config)
        mild = next(event for event in mild_events if event.event_type == "same_day_rejection" and event.candle_date == "2026-01-18")
        self.assertEqual(mild.alert_level, "attention")

    def test_main_breakout_retest_waits_five_full_candles_before_pin_bar_confirmation(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _breakout_retest_frame()
        frame.loc[31, ["开盘价", "最高价", "最低价", "收盘价"]] = [102.0, 106.0, 100.0, 105.0]
        frame.loc[32, ["开盘价", "最高价", "最低价", "收盘价"]] = [105.0, 106.0, 102.0, 103.0]
        frame.loc[33:35, ["开盘价", "最高价", "最低价", "收盘价"]] = [103.0, 104.0, 102.0, 103.0]
        frame.loc[36, ["开盘价", "最高价", "最低价", "收盘价"]] = [103.0, 104.2, 100.0, 104.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        retests = [event for event in events if event.event_type == "breakout_retest"]
        self.assertEqual([(event.candle_date, event.alert_level) for event in retests], [
            ("2026-02-01", "attention"),
            ("2026-02-06", "important"),
        ])
        self.assertEqual(retests[-1].evidence["level"], 100.0)

    def test_third_rising_low_near_an_automatic_trendline_is_attention(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([105.0] * 18)
        frame["td_up"] = 0
        for column in ("swing_high_3", "swing_low_3", "swing_high_5", "swing_low_5"):
            frame[column] = False
        frame.loc[[3, 10], "swing_low_3"] = True
        frame.loc[3, "最低价"], frame.loc[10, "最低价"] = 90.0, 95.0
        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价"]] = [105.0, 106.0, 100.4, 104.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        trendline = next(event for event in events if event.event_type == "trendline_retest")
        self.assertEqual((trendline.candle_date, trendline.alert_level), ("2026-01-18", "attention"))
        self.assertAlmostEqual(trendline.evidence["line_price"], 100.0)
        self.assertEqual(trendline.evidence["anchor_dates"], ["2026-01-04", "2026-01-11"])

    def test_third_trendline_contact_can_alert_when_second_anchor_becomes_confirmed(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([105.0] * 14)
        frame["td_up"] = 0
        for column in ("swing_high_3", "swing_low_3", "swing_high_5", "swing_low_5"):
            frame[column] = False
        frame.loc[[3, 10], "swing_low_3"] = True
        frame.loc[3, "最低价"], frame.loc[10, "最低价"] = 90.0, 95.0
        frame.loc[13, ["开盘价", "最高价", "最低价", "收盘价"]] = [105.0, 106.0, 97.2, 104.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        self.assertTrue(any(event.event_type == "trendline_retest" and event.candle_date == "2026-01-14" for event in events))

    def test_third_falling_high_near_a_descending_trendline_is_attention(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([95.0] * 18)
        frame["td_up"] = 0
        for column in ("swing_high_3", "swing_low_3", "swing_high_5", "swing_low_5"):
            frame[column] = False
        frame.loc[[3, 10], "swing_high_3"] = True
        frame.loc[3, "最高价"], frame.loc[10, "最高价"] = 110.0, 105.0
        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价"]] = [95.0, 100.4, 94.0, 96.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        trendline = next(event for event in events if event.event_type == "trendline_retest")
        self.assertEqual((trendline.candle_date, trendline.direction, trendline.alert_level), ("2026-01-18", "向下", "attention"))

    def test_trendline_stops_alerting_after_two_closes_below_it(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([105.0] * 18)
        frame["td_up"] = 0
        for column in ("swing_high_3", "swing_low_3", "swing_high_5", "swing_low_5"):
            frame[column] = False
        frame.loc[[3, 10], "swing_low_3"] = True
        frame.loc[3, "最低价"], frame.loc[10, "最低价"] = 90.0, 95.0
        frame.loc[15, ["开盘价", "最高价", "最低价", "收盘价"]] = [105.0, 106.0, 97.0, 98.0]
        frame.loc[16, ["开盘价", "最高价", "最低价", "收盘价"]] = [98.0, 104.0, 97.0, 98.0]
        frame.loc[17, ["开盘价", "最高价", "最低价", "收盘价"]] = [98.0, 104.0, 100.4, 103.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        self.assertFalse(any(event.event_type == "trendline_retest" and event.candle_date == "2026-01-18" for event in events))

    def test_continuous_daily_candles_can_form_bullish_engulfing_at_a_mature_retest(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _breakout_retest_frame()
        frame.loc[32, ["开盘价", "最高价", "最低价", "收盘价"]] = [103.0, 105.0, 102.0, 104.0]
        frame.loc[33:34, ["开盘价", "最高价", "最低价", "收盘价"]] = [104.0, 105.0, 103.0, 104.0]
        frame.loc[35, ["开盘价", "最高价", "最低价", "收盘价"]] = [104.0, 105.0, 101.0, 102.0]
        frame.loc[36, ["开盘价", "最高价", "最低价", "收盘价"]] = [102.0, 106.0, 100.0, 105.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        current = [event for event in events if event.candle_date == "2026-02-06"]
        self.assertIn("engulfing", [event.event_type for event in current])
        self.assertEqual(next(event.alert_level for event in current if event.event_type == "breakout_retest"), "important")

    def test_early_retest_needs_both_high_volume_and_a_long_bullish_body_for_red(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _breakout_retest_frame(days=32)
        frame.loc[31, ["开盘价", "最高价", "最低价", "收盘价", "成交量"]] = [102.0, 111.0, 100.0, 110.0, 200.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        retest = next(event for event in events if event.event_type == "breakout_retest")
        self.assertEqual((retest.alert_level, retest.evidence["confirmation"]), ("important", "strong_volume_body"))

        frame.loc[31, "成交量"] = 100.0
        _, ordinary_events = evaluate_crypto_signals("BTC", frame, config)
        ordinary = next(event for event in ordinary_events if event.event_type == "breakout_retest")
        self.assertEqual(ordinary.alert_level, "attention")

    def test_retest_keeps_all_active_main_breakout_levels_with_distinct_event_ids(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _breakout_retest_frame(days=52)
        frame.loc[40, ["swing_high_5", "最高价"]] = [True, 110.0]
        frame.loc[50, ["开盘价", "最高价", "最低价", "收盘价"]] = [103.0, 113.0, 102.0, 112.0]
        frame.loc[51, ["开盘价", "最高价", "最低价", "收盘价"]] = [112.0, 114.0, 100.0, 113.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        same_day = [event for event in events if event.event_type == "breakout_retest" and event.candle_date == "2026-02-21"]
        self.assertEqual({event.evidence["level"] for event in same_day}, {100.0, 110.0})
        self.assertEqual(len({event.event_id for event in same_day}), 2)

    def test_breakout_level_expires_after_ninety_calendar_days(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _breakout_retest_frame(days=122)
        frame.loc[121, ["开盘价", "最高价", "最低价", "收盘价"]] = [103.0, 105.0, 100.0, 104.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        self.assertFalse(any(event.event_type == "breakout_retest" and event.candle_date == "2026-05-02" for event in events))

    def test_price_action_severity_does_not_use_future_dow_pivots(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([80.0] * 20)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame["swing_low_3"] = False
        frame["swing_high_5"] = False
        frame["swing_low_5"] = False
        frame.loc[[5, 12], "swing_high_5"] = True
        frame.loc[[6, 13], "swing_low_5"] = True
        frame.loc[5, "最高价"], frame.loc[12, "最高价"] = 90.0, 100.0
        frame.loc[6, "最低价"], frame.loc[13, "最低价"] = 60.0, 70.0
        frame.loc[11, ["开盘价", "最高价", "最低价", "收盘价", "atr14"]] = [65.0, 82.0, 59.0, 80.0, 10.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        event = next(event for event in events if event.event_type == "same_day_rejection" and event.candle_date == "2026-01-12")
        self.assertEqual(event.alert_level, "attention")

    def test_td_emits_only_first_9_and_13_in_same_continuous_run(self) -> None:
        close = [10, 10, 10, 10] + list(range(11, 26)) + [20, 19]
        frame = _frame(close)
        _, events = evaluate_crypto_signals("BTC", frame, load_crypto_alert_config(PROJECT_ROOT))
        self.assertEqual([(event.event_type, event.direction) for event in events if event.event_type.startswith("td")], [
            ("td9", "上涨计数"), ("td13", "上涨计数")
        ])

    def test_signal_evaluation_handles_a_replay_window_with_original_row_indices(self) -> None:
        close = [10, 10, 10, 10] + list(range(11, 26)) + [20, 19]
        frame = _frame(close)
        frame.index = range(180, 180 + len(frame))

        _, events = evaluate_crypto_signals("BTC", frame, load_crypto_alert_config(PROJECT_ROOT))

        self.assertEqual([event.event_type for event in events if event.event_type.startswith("td")], ["td9", "td13"])

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
        frame.loc[11, "macd_hist"] = -3.0
        frame.loc[12, ["最低价", "macd_hist", "rsi14", "obv"]] = [80.0, -2.0, 35.0, 200.0]
        _, events = evaluate_crypto_signals("BTC", frame, config)
        self.assertEqual({event.direction for event in events}, {"普通看涨"})
        dates_by_indicator = {event.evidence["indicator"]: event.confirmed_date for event in events}
        self.assertEqual(dates_by_indicator, {"macd_hist": "2026-01-13", "rsi14": "2026-01-16", "obv": "2026-01-16"})

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

    def test_ma120_and_vegas_touch_events_use_atr_distance(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("ma120", "vegas"))
        frame = _frame([100.0, 101.0, 102.0])
        frame["td_up"] = 0
        frame["sma120"] = [100.0, 100.0, 100.0]
        frame["ema144"] = [100.0, 100.0, 100.0]
        frame["ema169"] = [100.0, 100.0, 100.0]
        frame["atr14"] = [4.0, 4.0, 4.0]
        frame.loc[2, "收盘价"] = 101.0
        _, events = evaluate_crypto_signals("BTC", frame, config)
        self.assertIn("ma120_touch", [event.event_type for event in events])
        self.assertIn("vegas_touch", [event.event_type for event in events])

    def test_ma120_and_vegas_retest_exit_require_a_prior_touch_then_move_away(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("ma120", "vegas"))
        frame = _frame([104.0, 101.0, 106.0])
        frame["td_up"] = 0
        frame["sma120"] = [100.0, 100.0, 100.0]
        frame["ema144"] = [100.0, 100.0, 100.0]
        frame["ema169"] = [100.0, 100.0, 100.0]
        frame["atr14"] = [4.0, 4.0, 4.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        self.assertEqual(
            {(event.event_type, event.candle_date) for event in events if event.event_type.endswith("retest_exit")},
            {("ma120_retest_exit", "2026-01-03"), ("vegas_retest_exit", "2026-01-03")},
        )

    def test_price_breakout_requires_a_confirmed_short_pivot_level(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 10)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame.loc[2, ["swing_high_3", "最高价"]] = [True, 110.0]
        frame.loc[6, "收盘价"] = 112.0
        _, events = evaluate_crypto_signals("BTC", frame, config)
        event = next(event for event in events if event.event_type == "breakout")
        self.assertIn("短期前高", event.message)

    def test_downside_breakout_names_the_confirmed_short_low(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 10)
        frame["td_up"] = 0
        frame["swing_low_3"] = False
        frame.loc[2, ["swing_low_3", "最低价"]] = [True, 90.0]
        frame.loc[6, "收盘价"] = 88.0

        _, events = evaluate_crypto_signals("BTC", frame, config)

        event = next(event for event in events if event.event_type == "breakout" and event.direction == "向下")
        self.assertIn("短期前低", event.message)

    def test_return_inside_level_within_three_bars_is_delayed_false_break(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 10)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame.loc[2, ["swing_high_3", "最高价"]] = [True, 110.0]
        frame.loc[6, "收盘价"] = 112.0
        frame.loc[7, "收盘价"] = 108.0
        _, events = evaluate_crypto_signals("BTC", frame, config)
        self.assertIn("delayed_false_break", [event.event_type for event in events])

    def test_wick_past_confirmed_level_with_close_inside_is_same_day_rejection(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 10)
        frame["td_up"] = 0
        frame["swing_high_3"] = False
        frame.loc[2, ["swing_high_3", "最高价"]] = [True, 110.0]
        frame.loc[6, ["最高价", "收盘价"]] = [115.0, 108.0]
        _, events = evaluate_crypto_signals("BTC", frame, config)
        self.assertIn("same_day_rejection", [event.event_type for event in events])

    def test_touching_a_confirmed_support_then_closing_back_above_is_level_reaction(self) -> None:
        config = replace(load_crypto_alert_config(PROJECT_ROOT), enabled_alerts=("price_action",))
        frame = _frame([100.0] * 10)
        frame["td_up"] = 0
        frame["swing_low_3"] = False
        frame.loc[2, ["swing_low_3", "最低价"]] = [True, 90.0]
        frame.loc[6, ["最低价", "收盘价"]] = [89.0, 95.0]

        _, events = evaluate_crypto_signals("BTC", frame, config)

        self.assertIn("level_reaction", [event.event_type for event in events])


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


def _breakout_retest_frame(days: int = 37) -> pd.DataFrame:
    close = [95.0] * 30 + [102.0] + [103.0] * (days - 31)
    frame = pd.DataFrame({
        "日期": pd.date_range("2026-01-01", periods=days, freq="D").strftime("%Y-%m-%d"),
        "开盘价": [close[0], *close[:-1]],
        "最高价": [max(open_price, finish) + 1 for open_price, finish in zip([close[0], *close[:-1]], close)],
        "最低价": [min(open_price, finish) - 1 for open_price, finish in zip([close[0], *close[:-1]], close)],
        "收盘价": close,
        "成交量": [100.0] * days,
    })
    frame["td_up"] = 0
    for column in ("swing_high_3", "swing_low_3", "swing_high_5", "swing_low_5"):
        frame[column] = False
    frame.loc[10, "swing_high_5"] = True
    frame.loc[10, "最高价"] = 100.0
    return frame


if __name__ == "__main__":
    unittest.main()
