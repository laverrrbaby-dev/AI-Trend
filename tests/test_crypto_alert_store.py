from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_store import (  # noqa: E402
    load_crypto_events,
    publish_crypto_result,
)
from trend_radar.crypto_signals import CryptoCurrentState, CryptoEvent  # noqa: E402


class CryptoAlertStoreTests(unittest.TestCase):
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

    def test_historical_events_do_not_count_as_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            event = CryptoEvent("history", "ETH", "td13", "下跌计数", "2026-09-09", "2026-09-09", "TD13：下跌计数", {}, "v1", "default", True)
            state = CryptoCurrentState("ETH", "有变化", "震荡/未确认", event.message, event.candle_date, event.candle_date)
            dashboard = publish_crypto_result(root, state, [event])
            self.assertEqual(dashboard["unread_count"], 0)


if __name__ == "__main__":
    unittest.main()
