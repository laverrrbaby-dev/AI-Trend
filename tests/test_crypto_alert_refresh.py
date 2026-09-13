from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from datetime import UTC, datetime

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_refresh import (  # noqa: E402
    _state_for_events,
    crypto_alert_refresh_is_active,
    read_crypto_alert_refresh_status,
    refresh_crypto_alerts,
)
from trend_radar.crypto_alert_store import load_crypto_dashboard, publish_crypto_result  # noqa: E402
from trend_radar.crypto_alert_config import ALGORITHM_VERSION, activate_crypto_alert_preset, save_crypto_alert_preset  # noqa: E402
from trend_radar.crypto_market_data import CryptoHistoryResult  # noqa: E402
from trend_radar.crypto_signals import CryptoCurrentState, CryptoEvent  # noqa: E402


class CryptoAlertRefreshTests(unittest.TestCase):
    def test_same_day_messages_put_important_alerts_before_attention_alerts(self) -> None:
        state = CryptoCurrentState("ETH", "有变化", "上升结构", "", None, "2026-09-12")
        events = [
            CryptoEvent("attention", "ETH", "ma120", "回踩", "2026-09-12", "2026-09-12", "关注：回踩 MA120", {}, ALGORITHM_VERSION, "default", alert_level="attention"),
            CryptoEvent("important", "ETH", "macd_divergence", "看跌背离", "2026-09-12", "2026-09-12", "重要：MACD 看跌背离", {}, ALGORITHM_VERSION, "default", alert_level="important"),
        ]

        current = _state_for_events(state, events, ALGORITHM_VERSION, "default")

        self.assertEqual(current.latest_change, "重要：MACD 看跌背离；关注：回踩 MA120")
        self.assertEqual(current.alert_level, "important")

    def test_daily_refresh_publishes_only_events_after_the_saved_baseline_and_advances_it(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({"algorithm_version": ALGORITHM_VERSION, "preset_id": "default", "date": "2026-09-09"}))
            dates = ["2026-09-09", "2026-09-10"]
            frame = pd.DataFrame({"日期": dates, "开盘价": [100.0, 100.0], "最高价": [101.0, 102.0], "最低价": [99.0, 99.0], "收盘价": [100.0, 101.0], "成交量": [1.0, 1.0], "成交额": [100.0, 101.0], "is_complete": [True, True]})
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                events = [
                    CryptoEvent(f"{symbol}-old", symbol, "td9", "上涨计数", dates[0], dates[0], "旧事件", {}, config.algorithm_version, config.preset_id),
                    CryptoEvent(f"{symbol}-new", symbol, "td13", "上涨计数", dates[1], dates[1], "新事件", {}, config.algorithm_version, config.preset_id),
                ]
                return CryptoCurrentState(symbol, "有变化", "震荡/未确认", "新事件", dates[1], dates[1]), events

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": dates[-1], "收盘价": 101.0}), \
                 patch("trend_radar.crypto_alert_refresh.calculate_indicators", side_effect=lambda values, config: values), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root, now=datetime(2026, 9, 11, 12, tzinfo=UTC))

            dashboard = load_crypto_dashboard(root)
            self.assertEqual({event["event_id"] for event in dashboard["events"]}, {f"{symbol}-new" for symbol in ("BTC", "ETH", "SOL", "XAUT", "BNB")})
            self.assertEqual(dashboard["unread_count"], 5)
            self.assertEqual(json.loads((alert_dir / "baseline.json").read_text())["date"], "2026-09-10")

    def test_initial_historical_replay_does_not_mark_symbol_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame({"日期": ["2026-09-10"], "开盘价": [100.0], "最高价": [101.0], "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0], "成交额": [100.0], "is_complete": [True]})
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                event = CryptoEvent(f"{symbol}-history", symbol, "td9", "上涨计数", "2026-09-10", "2026-09-10", "历史TD9", {}, config.algorithm_version, config.preset_id)
                return CryptoCurrentState(symbol, "有变化", "震荡/未确认", event.message, event.candle_date, event.candle_date), [event]

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-10", "收盘价": 100.0}), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root, now=datetime(2026, 9, 11, 12, tzinfo=UTC))

            self.assertTrue(all(state["status"] == "无新变化" for state in load_crypto_dashboard(root)["symbols"]))

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

    def test_first_successful_refresh_marks_detected_events_as_historical(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame({"日期": ["2026-09-09"], "开盘价": [100.0], "最高价": [101.0], "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0], "成交额": [100.0], "is_complete": [True]})
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)
            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-09", "收盘价": 100.0}):
                refresh_crypto_alerts(root)
            dashboard = load_crypto_dashboard(root)
            self.assertEqual(dashboard["unread_count"], 0)
            self.assertTrue((root / "data/crypto/alerts/baseline.json").exists())

    def test_completed_baseline_skips_network_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({"algorithm_version": ALGORITHM_VERSION, "preset_id": "default", "date": "2026-09-09"}))
            now = datetime(2026, 9, 10, 12, tzinfo=UTC)
            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", side_effect=AssertionError("network should be skipped")):
                result = refresh_crypto_alerts(root, now=now)
            self.assertEqual(result.status, "skipped")
            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", side_effect=RuntimeError("forced refresh reached network")):
                forced = refresh_crypto_alerts(root, force=True, now=now)
            self.assertEqual(forced.status, "partial")

    def test_stale_persisted_running_status_is_recovered_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "refresh_status.json").write_text('{"status":"running","completed":2,"total":5}')

            status = read_crypto_alert_refresh_status(root)

            self.assertEqual(status["status"], "partial")
            self.assertIn("中断", status["message"])

    def test_initial_refresh_replays_only_the_latest_configured_window(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            dates = pd.date_range("2026-01-01", periods=200, freq="D").strftime("%Y-%m-%d")
            frame = pd.DataFrame({
                "日期": dates, "开盘价": [100.0] * 200, "最高价": [101.0] * 200,
                "最低价": [99.0] * 200, "收盘价": [100.0] * 200, "成交量": [1.0] * 200,
                "成交额": [100.0] * 200, "is_complete": [True] * 200,
            })
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                date = str(values.iloc[0]["日期"])
                event = CryptoEvent(f"{symbol}-{date}", symbol, "td9", "上涨计数", date, date, "TD9：上涨计数", {}, config.algorithm_version, config.preset_id)
                return CryptoCurrentState(symbol, "有变化", "震荡/未确认", event.message, date, str(values.iloc[-1]["日期"])), [event]

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": str(dates[-1]), "收盘价": 100.0}), \
                 patch("trend_radar.crypto_alert_refresh.calculate_indicators", side_effect=lambda values, config: values), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root)

            stored_events = load_crypto_dashboard(root)["events"]
            self.assertEqual({event["candle_date"] for event in stored_events}, {str(dates[-180])})
            self.assertEqual({event["evidence"]["source"] for event in stored_events}, {"binance"})

    def test_changing_preset_rebaselines_events_instead_of_creating_unread_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({"algorithm_version": ALGORITHM_VERSION, "preset_id": "default", "date": "2026-09-09"}))
            save_crypto_alert_preset(root, "quiet", {"enabled_alerts": ["td"]})
            activate_crypto_alert_preset(root, "quiet")
            frame = pd.DataFrame({"日期": ["2026-09-09"], "开盘价": [100.0], "最高价": [101.0], "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0], "成交额": [100.0], "is_complete": [True]})
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                event = CryptoEvent(f"{symbol}-quiet", symbol, "td9", "上涨计数", "2026-09-09", "2026-09-09", "TD9：上涨计数", {}, config.algorithm_version, config.preset_id)
                return CryptoCurrentState(symbol, "有变化", "震荡/未确认", event.message, event.candle_date, event.candle_date), [event]

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-09", "收盘价": 100.0}), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root)

            self.assertEqual(load_crypto_dashboard(root)["unread_count"], 0)

    def test_fallback_source_marks_symbol_for_review_without_publishing_events(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame({"日期": ["2026-09-09"], "开盘价": [100.0], "最高价": [101.0], "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0], "成交额": [100.0], "is_complete": [True]})
            history = CryptoHistoryResult("BTC", "okx", "BTC-USDT", frame, used_fallback=True)
            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-09", "收盘价": 100.0}):
                refresh_crypto_alerts(root)

            dashboard = load_crypto_dashboard(root)
            self.assertEqual(dashboard["unread_count"], 0)
            self.assertTrue(all(state["quality"] == "待复核" for state in dashboard["symbols"]))


def _payload(url: str) -> object:
    if "history-candles" in url:
        return {"code": "0", "data": []}
    return []


if __name__ == "__main__":
    unittest.main()
