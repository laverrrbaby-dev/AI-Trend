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

from trend_radar import crypto_alert_refresh as refresh_module  # noqa: E402
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
    def test_pending_date_appears_when_an_open_app_crosses_0800_china_time(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({
                "algorithm_version": ALGORITHM_VERSION,
                "preset_id": "default",
                "date": "2026-09-12",
            }))
            pending_date = getattr(refresh_module, "pending_crypto_alert_date", lambda root, now=None: None)

            before = pending_date(root, datetime.fromisoformat("2026-09-14T07:59:59+08:00"))
            after = pending_date(root, datetime.fromisoformat("2026-09-14T08:00:00+08:00"))

            self.assertIsNone(before)
            self.assertEqual(after, "2026-09-13")

    def test_catch_up_refresh_does_not_show_an_older_event_as_today(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({"algorithm_version": ALGORITHM_VERSION, "preset_id": "default", "date": "2026-09-10"}))
            frame = pd.DataFrame({
                "日期": ["2026-09-11", "2026-09-12"], "开盘价": [100.0, 101.0],
                "最高价": [102.0, 102.0], "最低价": [99.0, 100.0], "收盘价": [101.0, 101.0],
                "成交量": [1.0, 1.0], "成交额": [101.0, 101.0], "is_complete": [True, True],
            })
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                event = CryptoEvent(f"{symbol}-old-red", symbol, "breakout", "向上", "2026-09-11", "2026-09-11", "昨天突破", {}, config.algorithm_version, config.preset_id, alert_level="important")
                state = CryptoCurrentState(symbol, "有变化", "上升结构", event.message, event.candle_date, "2026-09-12", alert_level="important")
                return state, [event]

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-12", "收盘价": 101.0}), \
                 patch("trend_radar.crypto_alert_refresh.calculate_indicators", side_effect=lambda values, config: values), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root, now=datetime(2026, 9, 13, 12, tzinfo=UTC))

            dashboard = load_crypto_dashboard(root)
            self.assertTrue(all(item["status"] == "无新变化" for item in dashboard["symbols"]))
            self.assertEqual(dashboard["important_symbol_count"], 0)
            self.assertEqual(len(dashboard["events"]), 5)

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
            self.assertNotIn("unread_count", dashboard)
            self.assertEqual(json.loads((alert_dir / "baseline.json").read_text())["date"], "2026-09-10")

    def test_forced_recheck_preserves_existing_latest_change_when_no_new_event_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({
                "algorithm_version": ALGORITHM_VERSION,
                "preset_id": "default",
                "date": "2026-09-09",
            }))
            prior = CryptoCurrentState(
                "BTC", "有变化", "上升结构", "刚刚确认突破", "2026-09-10", "2026-09-10",
                alert_level="important", algorithm_version=ALGORITHM_VERSION, preset_id="default",
                latest_alert_time="2026-09-11 08:00",
            )
            publish_crypto_result(root, prior, [])
            frame = pd.DataFrame({
                "日期": ["2026-09-09", "2026-09-10"],
                "开盘价": [100.0, 101.0], "最高价": [102.0, 103.0],
                "最低价": [99.0, 100.0], "收盘价": [101.0, 102.0],
                "成交量": [1.0, 1.0], "成交额": [101.0, 102.0],
                "is_complete": [True, True],
            })
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                old_event = CryptoEvent(
                    f"{symbol}-old", symbol, "breakout", "向上", "2026-09-09", "2026-09-09",
                    "旧事件", {}, config.algorithm_version, config.preset_id,
                )
                return CryptoCurrentState(
                    symbol, "有变化", "上升结构", old_event.message, old_event.candle_date,
                    "2026-09-10", alert_level="attention",
                ), [old_event]

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-10", "收盘价": 102.0}), \
                 patch("trend_radar.crypto_alert_refresh.calculate_indicators", side_effect=lambda values, config: values), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root, force=True, now=datetime(2026, 9, 11, 12, tzinfo=UTC))

            btc = next(item for item in load_crypto_dashboard(root)["symbols"] if item["symbol"] == "BTC")
            self.assertEqual(btc["status"], "有变化")
            self.assertEqual(btc["latest_change"], "刚刚确认突破")
            self.assertEqual(btc["latest_change_date"], "2026-09-10")

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

    def test_algorithm_upgrade_shows_only_new_rules_on_the_latest_complete_candle(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({
                "algorithm_version": "crypto-alerts-v3", "preset_id": "default", "date": "2026-09-10",
            }))
            frame = pd.DataFrame({
                "日期": ["2026-09-09", "2026-09-10"], "开盘价": [100.0, 100.0],
                "最高价": [101.0, 102.0], "最低价": [99.0, 99.0], "收盘价": [100.0, 101.0],
                "成交量": [1.0, 1.0], "成交额": [100.0, 101.0], "is_complete": [True, True],
            })
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                events = [
                    CryptoEvent(f"{symbol}-old", symbol, "td9", "上涨计数", "2026-09-09", "2026-09-09", "旧信号", {}, config.algorithm_version, config.preset_id, alert_level="important"),
                    CryptoEvent(f"{symbol}-today", symbol, "breakout_retest", "向上", "2026-09-10", "2026-09-10", "新规则当天信号", {}, config.algorithm_version, config.preset_id, alert_level="attention"),
                ]
                return CryptoCurrentState(symbol, "有变化", "上升结构", "新规则当天信号", "2026-09-10", "2026-09-10"), events

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-10", "收盘价": 101.0}), \
                 patch("trend_radar.crypto_alert_refresh.calculate_indicators", side_effect=lambda values, config: values), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root, now=datetime(2026, 9, 11, 12, tzinfo=UTC))

            dashboard = load_crypto_dashboard(root)
            self.assertEqual({event["event_id"] for event in dashboard["events"] if not event["historical_backfill"]}, {f"{symbol}-today" for symbol in ("BTC", "ETH", "SOL", "XAUT", "BNB")})
            self.assertTrue(all(event["historical_backfill"] for event in dashboard["events"] if event["event_id"].endswith("-old")))
            self.assertTrue(all(state["latest_change"] == "新规则当天信号" for state in dashboard["symbols"]))

    def test_algorithm_upgrade_does_not_keep_an_old_same_day_red_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            alert_dir = root / "data/crypto/alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "baseline.json").write_text(json.dumps({
                "algorithm_version": "crypto-alerts-v3", "preset_id": "default", "date": "2026-09-10",
            }))
            old = CryptoCurrentState("BTC", "有变化", "上升结构", "旧版红色提醒", "2026-09-10", "2026-09-10",
                                     alert_level="important", algorithm_version="crypto-alerts-v3", preset_id="default")
            publish_crypto_result(root, old, [])
            frame = pd.DataFrame({
                "日期": ["2026-09-10"], "开盘价": [100.0], "最高价": [101.0],
                "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0],
                "成交额": [100.0], "is_complete": [True],
            })
            history = CryptoHistoryResult("BTC", "binance", "BTCUSDT", frame)

            def evaluate(symbol, values, config):
                return CryptoCurrentState(symbol, "无新变化", "上升结构", "暂无新的策略变化", None, "2026-09-10"), []

            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-10", "收盘价": 100.0}), \
                 patch("trend_radar.crypto_alert_refresh.evaluate_crypto_signals", side_effect=evaluate):
                refresh_crypto_alerts(root, now=datetime(2026, 9, 11, 12, tzinfo=UTC))

            dashboard = load_crypto_dashboard(root)
            self.assertEqual(dashboard["important_symbol_count"], 0)
            self.assertEqual(next(item for item in dashboard["symbols"] if item["symbol"] == "BTC")["status"], "无新变化")

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
            self.assertEqual(dashboard["important_symbol_count"], 0)
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

            self.assertNotIn("unread_count", load_crypto_dashboard(root))

    def test_fallback_source_marks_symbol_for_review_without_publishing_events(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame({"日期": ["2026-09-09"], "开盘价": [100.0], "最高价": [101.0], "最低价": [99.0], "收盘价": [100.0], "成交量": [1.0], "成交额": [100.0], "is_complete": [True]})
            history = CryptoHistoryResult("BTC", "okx", "BTC-USDT", frame, used_fallback=True)
            with patch("trend_radar.crypto_alert_refresh.fetch_crypto_history", return_value=history), \
                 patch("trend_radar.crypto_alert_refresh.fetch_latest_backup_candle", return_value={"日期": "2026-09-09", "收盘价": 100.0}):
                refresh_crypto_alerts(root)

            dashboard = load_crypto_dashboard(root)
            self.assertEqual(dashboard["important_symbol_count"], 0)
            self.assertTrue(all(state["quality"] == "待复核" for state in dashboard["symbols"]))


def _payload(url: str) -> object:
    if "history-candles" in url:
        return {"code": "0", "data": []}
    return []


if __name__ == "__main__":
    unittest.main()
