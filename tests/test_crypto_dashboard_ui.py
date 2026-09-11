from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import (  # noqa: E402
    _crypto_candlestick_chart,
    _crypto_dashboard_table,
    crypto_unread_event_ids,
    go,
)


class CryptoDashboardTableTests(unittest.TestCase):
    def test_crypto_unread_event_ids_excludes_history_and_other_symbols(self) -> None:
        dashboard = {"events": [
            {"event_id": "btc-new", "symbol": "BTC", "historical_backfill": False},
            {"event_id": "btc-history", "symbol": "BTC", "historical_backfill": True},
            {"event_id": "eth-new", "symbol": "ETH", "historical_backfill": False},
        ]}

        self.assertEqual(crypto_unread_event_ids(dashboard, "BTC"), ["btc-new"])

    def test_table_always_contains_fixed_five_in_order(self) -> None:
        table = _crypto_dashboard_table({"symbols": [{"symbol": "ETH", "status": "有变化", "latest_change": "TD9：上涨计数"}]})
        self.assertEqual(table["币种"].tolist(), ["BTC", "ETH", "SOL", "XAUT", "BNB"])
        self.assertEqual(list(table.columns), ["币种", "当前状态", "最新变化", "变化日期", "数据截至", "数据质量"])
        self.assertNotIn("买入", " ".join(table["最新变化"].tolist()))

    def test_table_can_show_only_symbols_with_changes(self) -> None:
        dashboard = {"symbols": [
            {"symbol": "BTC", "status": "有变化", "latest_change": "TD9：上涨计数"},
            {"symbol": "ETH", "status": "无新变化", "latest_change": "暂无新的策略变化"},
        ]}

        table = _crypto_dashboard_table(dashboard, changed_only=True)

        self.assertEqual(table["币种"].tolist(), ["BTC"])

    def test_detail_chart_includes_candles_and_default_trend_overlays(self) -> None:
        if not hasattr(go.Figure(), "add_trace"):
            self.skipTest("Plotly import stub is active in this test process")
        chart = _crypto_candlestick_chart(pd.DataFrame({
            "日期": ["2026-09-09"], "开盘价": [100], "最高价": [110], "最低价": [90], "收盘价": [105],
            "sma120": [99], "ema144": [98], "ema169": [97],
        }))
        self.assertEqual([trace.name for trace in chart.data], ["日线", "SMA120", "EMA144", "EMA169"])


if __name__ == "__main__":
    unittest.main()
