from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_refresh import (  # noqa: E402
    crypto_alert_refresh_is_active,
    read_crypto_alert_refresh_status,
    refresh_crypto_alerts,
)
from trend_radar.crypto_alert_store import load_crypto_dashboard, publish_crypto_result  # noqa: E402
from trend_radar.crypto_market_data import CryptoHistoryResult  # noqa: E402
from trend_radar.crypto_signals import CryptoCurrentState  # noqa: E402


class CryptoAlertRefreshTests(unittest.TestCase):
    def test_refresh_writes_status_and_keeps_all_fixed_symbols_on_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            result = refresh_crypto_alerts(root, fetch_json=lambda url, headers: _payload(url))
            self.assertEqual(result.status, "partial")
            status = read_crypto_alert_refresh_status(root)
            self.assertEqual(status["completed"], 5)
            self.assertFalse(crypto_alert_refresh_is_active())

    def test_failed_symbol_keeps_prior_state_and_marks_it_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            prior = CryptoCurrentState(
                "BTC", "无新变化", "上升结构", "暂无新的策略变化", None, "2026-09-08"
            )
            publish_crypto_result(root, prior, [])

            refresh_crypto_alerts(root, fetch_json=lambda url, headers: _payload(url))

            btc = next(item for item in load_crypto_dashboard(root)["symbols"] if item["symbol"] == "BTC")
            self.assertEqual(btc["data_date"], "2026-09-08")
            self.assertEqual(btc["quality"], "待复核")
            self.assertTrue(btc["stale"])

    def test_backup_close_deviation_marks_symbol_for_review_without_event(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame({"日期": ["2026-09-09"], "开盘价": [100.0], "最高价": [101.0], "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0], "成交额": [100.0], "is_complete": [True]})
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)
            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-09", "收盘价": 102.0}):
                refresh_crypto_alerts(root)
            btc = next(item for item in load_crypto_dashboard(root)["symbols"] if item["symbol"] == "BTC")
            self.assertEqual(btc["quality"], "待复核")


def _payload(url: str) -> object:
    if "history-candles" in url:
        return {"code": "0", "data": []}
    return []


if __name__ == "__main__":
    unittest.main()
