from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app as app_module  # noqa: E402
from app import (  # noqa: E402
    _crypto_candlestick_chart,
    _crypto_dashboard_table,
    _crypto_market_label,
    _style_crypto_dashboard_table,
    crypto_history_source_caption,
    go,
)


class CryptoDashboardTableTests(unittest.TestCase):
    def test_crypto_turnover_page_does_not_render_legacy_turnover_block(self) -> None:
        with patch.object(app_module, "_render_crypto_alert_dashboard") as alert_dashboard, patch.object(
            app_module, "_render_legacy_crypto_turnover"
        ) as legacy_turnover:
            app_module._render_crypto_turnover_page()

        alert_dashboard.assert_called_once_with()
        legacy_turnover.assert_not_called()

    def test_crypto_auto_refresh_claims_each_complete_candle_date_once_per_session(self) -> None:
        claim = getattr(app_module, "_claim_crypto_auto_refresh_date", lambda state, target: False)
        state: dict[str, str] = {}

        self.assertTrue(claim(state, "2026-09-13"))
        self.assertFalse(claim(state, "2026-09-13"))
        self.assertTrue(claim(state, "2026-09-14"))

    def test_background_crypto_completion_requests_a_full_page_refresh(self) -> None:
        completed = getattr(app_module, "_crypto_alert_refresh_completed", lambda previous, current: False)
        self.assertTrue(completed("running", "succeeded"))
        self.assertTrue(completed("running", "partial"))
        self.assertFalse(completed("succeeded", "succeeded"))

    def test_fast_crypto_refresh_completion_is_detected_by_revision_even_without_running_state(self) -> None:
        needs_rerun = getattr(app_module, "_crypto_alert_status_needs_rerun", lambda previous, current: False)
        previous = {"status": "succeeded", "finished_at": "2026-09-14T00:00:00Z"}
        current = {"status": "succeeded", "finished_at": "2026-09-14T00:00:02Z"}

        self.assertTrue(needs_rerun(previous, current))
        self.assertFalse(needs_rerun(current, current))

    def test_history_source_caption_uses_cached_actual_source_and_pair(self) -> None:
        history = pd.DataFrame({"source": ["okx"], "pair": ["BTC-USDT"]})

        self.assertEqual(crypto_history_source_caption(history), "实际行情来源：OKX · BTC-USDT")

    def test_market_label_counts_today_important_symbols_not_events(self) -> None:
        self.assertEqual(_crypto_market_label({"status": "succeeded", "important_symbol_count": 2}), "币圈现货 🔴2")
        self.assertEqual(_crypto_market_label({"status": "succeeded", "important_symbol_count": 0}), "币圈现货")

    def test_market_label_shows_review_instead_of_stale_count_after_failure(self) -> None:
        self.assertEqual(_crypto_market_label({"status": "partial", "important_symbol_count": 2}), "币圈现货 ⚠待复核")

    def test_crypto_failure_message_lists_each_failed_symbol_reason(self) -> None:
        formatter = getattr(app_module, "_crypto_alert_error_message", lambda status: "")

        message = formatter({"status": "partial", "warnings": ["BTC: connection timed out", "XAUT: backup mismatch"]})

        self.assertIn("BTC: connection timed out", message)
        self.assertIn("XAUT: backup mismatch", message)

    def test_table_always_contains_fixed_five_in_order(self) -> None:
        table = _crypto_dashboard_table({"symbols": [{
            "symbol": "ETH",
            "status": "无新变化",
            "latest_change": "暂无新的策略变化",
            "latest_change_date": None,
            "latest_alert_time": None,
        }]})
        self.assertEqual(table["币种"].tolist(), ["BTC", "ETH", "SOL", "XAUT", "BNB"])
        self.assertEqual(list(table.columns), ["币种", "当前状态", "提醒级别", "最新变化", "信号K线", "提醒时间", "数据截至", "数据质量"])
        self.assertEqual(table.loc[1, "信号K线"], "-")
        self.assertEqual(table.loc[1, "提醒时间"], "-")
        self.assertNotIn("买入", " ".join(table["最新变化"].tolist()))

    def test_table_can_show_only_symbols_with_changes(self) -> None:
        dashboard = {"symbols": [
            {"symbol": "BTC", "status": "有变化", "latest_change": "TD9：上涨计数"},
            {"symbol": "ETH", "status": "无新变化", "latest_change": "暂无新的策略变化"},
        ]}

        table = _crypto_dashboard_table(dashboard, changed_only=True)

        self.assertEqual(table["币种"].tolist(), ["BTC"])

    def test_crypto_rows_use_red_for_important_and_orange_for_attention(self) -> None:
        table = _crypto_dashboard_table({"symbols": [
            {"symbol": "BTC", "status": "有变化", "alert_level": "important", "latest_change": "MACD普通看跌背离"},
            {"symbol": "ETH", "status": "有变化", "alert_level": "attention", "latest_change": "TD9：上涨计数"},
            {"symbol": "SOL", "status": "无新变化", "alert_level": "none", "latest_change": "暂无新的策略变化"},
        ]})

        styles = _style_crypto_dashboard_table(table, show_attention=True)._compute().ctx

        self.assertTrue(all(("background-color", "#ffe0e0") in styles[(0, column)] for column in range(len(table.columns))))
        self.assertTrue(all(("background-color", "#fff0d9") in styles[(1, column)] for column in range(len(table.columns))))
        self.assertFalse(any(styles.get((2, column), []) for column in range(len(table.columns)) if column != 3))

    def test_alert_labels_remain_visible_when_alert_fills_are_disabled(self) -> None:
        table = _crypto_dashboard_table({"symbols": [
            {"symbol": "BTC", "status": "有变化", "alert_level": "important"},
            {"symbol": "ETH", "status": "有变化", "alert_level": "attention"},
        ]})

        styles = _style_crypto_dashboard_table(table, show_important=False, show_attention=False)._compute().ctx

        self.assertEqual(table.loc[0, "提醒级别"], "重要")
        self.assertEqual(table.loc[1, "提醒级别"], "关注")
        self.assertFalse(any(styles.get((0, column), []) for column in range(len(table.columns)) if column != 3))
        self.assertFalse(any(styles.get((1, column), []) for column in range(len(table.columns)) if column != 3))

    def test_latest_change_cells_allow_wrapping(self) -> None:
        table = _crypto_dashboard_table({"symbols": [{
            "symbol": "ETH",
            "status": "有变化",
            "latest_change": "日内越过关键高点后收盘回落；触及已确认阻力位后回落；价格行为变化：上影线针形；RSI14出现隐藏看涨背离",
        }]})

        styles = _style_crypto_dashboard_table(table)._compute().ctx

        latest_change_styles = styles[(1, 3)]
        self.assertIn(("white-space", "normal"), latest_change_styles)
        self.assertIn(("overflow-wrap", "anywhere"), latest_change_styles)

    def test_table_distinguishes_signal_candle_from_next_morning_alert_time(self) -> None:
        table = _crypto_dashboard_table({"symbols": [{
            "symbol": "ETH",
            "status": "有变化",
            "latest_change_date": "2026-09-12",
            "latest_alert_time": "2026-09-13 08:00",
        }]})

        eth = table.loc[table["币种"] == "ETH"].iloc[0]
        self.assertEqual(eth["信号K线"], "2026-09-12")
        self.assertEqual(eth["提醒时间"], "2026-09-13 08:00")

    def test_detail_chart_includes_candles_and_default_trend_overlays(self) -> None:
        if not hasattr(go.Figure(), "add_trace"):
            self.skipTest("Plotly import stub is active in this test process")
        chart = _crypto_candlestick_chart(pd.DataFrame({
            "日期": ["2026-09-09"], "开盘价": [100], "最高价": [110], "最低价": [90], "收盘价": [105],
            "sma120": [99], "ema144": [98], "ema169": [97],
        }))
        self.assertEqual([trace.name for trace in chart.data], ["日线", "SMA120", "EMA144", "EMA169"])

    def test_detail_chart_labels_the_unfinished_utc_candle(self) -> None:
        if not hasattr(go.Figure(), "add_trace"):
            self.skipTest("Plotly import stub is active in this test process")
        chart = _crypto_candlestick_chart(pd.DataFrame({
            "日期": ["2026-09-09", "2026-09-10"], "开盘价": [100, 105], "最高价": [110, 111],
            "最低价": [90, 100], "收盘价": [105, 106], "is_complete": [True, False],
        }))
        self.assertIn("形成中日线", [trace.name for trace in chart.data])


if __name__ == "__main__":
    unittest.main()
