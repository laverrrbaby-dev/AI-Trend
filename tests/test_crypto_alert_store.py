from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_store import (  # noqa: E402
    load_crypto_events,
    load_crypto_dashboard,
    publish_crypto_result,
    replay_crypto_history,
)
from trend_radar.crypto_alert_config import load_crypto_alert_config  # noqa: E402
from trend_radar.crypto_signals import CryptoCurrentState, CryptoEvent  # noqa: E402


class CryptoAlertStoreTests(unittest.TestCase):
    def test_dashboard_does_not_publish_legacy_unread_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            event = CryptoEvent("eth-red", "ETH", "breakout", "向上", "2026-09-12", "2026-09-12", "突破", {}, "v3", "default", alert_level="important")
            state = CryptoCurrentState("ETH", "有变化", "上升结构", "突破", "2026-09-12", "2026-09-12", alert_level="important", algorithm_version="v3", preset_id="default")

            dashboard = publish_crypto_result(root, state, [event])

            self.assertNotIn("unread_count", dashboard)
            self.assertNotIn("is_unread", dashboard["events"][0])
            self.assertFalse((root / "data/crypto/alerts/read_state.json").exists())

    def test_dashboard_counts_each_important_symbol_once_for_the_latest_data_date(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            eth_event = CryptoEvent("eth-a", "ETH", "breakout", "向上", "2026-09-12", "2026-09-12", "突破", {}, "v3", "default", alert_level="important")
            eth_state = CryptoCurrentState("ETH", "有变化", "上升结构", "突破；MACD背离", "2026-09-12", "2026-09-12", alert_level="important", algorithm_version="v3", preset_id="default")
            publish_crypto_result(root, eth_state, [eth_event])
            btc_event = CryptoEvent("btc-a", "BTC", "td9", "上涨计数", "2026-09-12", "2026-09-12", "TD9", {}, "v3", "default", alert_level="attention")
            btc_state = CryptoCurrentState("BTC", "有变化", "上升结构", "TD9", "2026-09-12", "2026-09-12", alert_level="attention", algorithm_version="v3", preset_id="default")

            dashboard = publish_crypto_result(root, btc_state, [btc_event])

            self.assertEqual(dashboard["important_symbol_count"], 1)
    def test_new_algorithm_state_hides_events_from_previous_algorithm(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            old = CryptoEvent("old", "BTC", "td9", "上涨计数", "2026-09-10", "2026-09-10", "旧算法事件", {}, "v1", "default")
            old_state = CryptoCurrentState("BTC", "有变化", "震荡/未确认", old.message, old.candle_date, old.candle_date, alert_level="attention", algorithm_version="v1", preset_id="default")
            publish_crypto_result(root, old_state, [old])
            new_state = CryptoCurrentState("BTC", "无新变化", "震荡/未确认", "暂无新的策略变化", None, "2026-09-11", algorithm_version="v2", preset_id="default")

            dashboard = publish_crypto_result(root, new_state, [])

            self.assertEqual(dashboard["events"], [])

    def test_events_are_append_only_and_duplicate_keys_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            event = CryptoEvent("id", "BTC", "td9", "上涨计数", "2026-09-09", "2026-09-09", "TD9：上涨计数", {}, "v1", "default")
            state = CryptoCurrentState("BTC", "有变化", "震荡/未确认", event.message, event.candle_date, event.candle_date)
            publish_crypto_result(root, state, [event])
            publish_crypto_result(root, state, [event])
            self.assertEqual(len(load_crypto_events(root)), 1)
            lines = (root / "data/crypto/alerts/events.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["event_id"], "id")

    def test_publish_writes_an_auditable_snapshot_for_the_state_date(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            event = CryptoEvent("snapshot", "BTC", "td9", "上涨计数", "2026-09-09", "2026-09-09", "TD9：上涨计数", {}, "v1", "default")
            state = CryptoCurrentState("BTC", "有变化", "震荡/未确认", event.message, event.candle_date, "2026-09-09")

            publish_crypto_result(root, state, [event])

            snapshot = json.loads((root / "data/crypto/alerts/snapshots/2026-09-09.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["symbols"][0]["symbol"], "BTC")
            self.assertEqual(snapshot["events"][0]["event_id"], "snapshot")

    def test_initial_replay_persists_baseline_and_historical_events(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame({"日期": ["2026-09-09"], "收盘价": [100.0]})
            with patch("trend_radar.crypto_alert_store.load_crypto_history", return_value=frame), \
                 patch("trend_radar.crypto_alert_store.evaluate_crypto_signals", return_value=(
                     CryptoCurrentState("BTC", "有变化", "震荡/未确认", "TD9：上涨计数", "2026-09-09", "2026-09-09"),
                     [CryptoEvent("replay", "BTC", "td9", "上涨计数", "2026-09-09", "2026-09-09", "TD9：上涨计数", {}, "v1", "default")],
                 )):
                replay_crypto_history(root, load_crypto_alert_config(PROJECT_ROOT))
            self.assertTrue((root / "data/crypto/alerts/baseline.json").exists())
            self.assertTrue(load_crypto_events(root)[0]["historical_backfill"])
            self.assertEqual(load_crypto_dashboard(root)["important_symbol_count"], 0)


if __name__ == "__main__":
    unittest.main()
