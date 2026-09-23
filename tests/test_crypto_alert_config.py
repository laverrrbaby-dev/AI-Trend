from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_config import (  # noqa: E402
    ALGORITHM_VERSION,
    activate_crypto_alert_preset,
    load_crypto_alert_config,
    list_crypto_alert_presets,
    save_crypto_alert_preset,
)


class CryptoAlertConfigTests(unittest.TestCase):
    def test_default_config_has_fixed_order_and_sources(self) -> None:
        config = load_crypto_alert_config(root=PROJECT_ROOT)
        self.assertEqual(list(config.symbols), ["BTC", "ETH", "SOL", "XAUT", "BNB"])
        self.assertEqual(config.sources["BTC"].primary, "binance")
        self.assertEqual(config.sources["XAUT"].primary, "okx")
        self.assertEqual(config.history_bars, 500)
        self.assertEqual(config.timezone, "Asia/Shanghai")

    def test_named_preset_round_trips_without_mutating_default(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            saved = save_crypto_alert_preset(root, "quiet", {"enabled_alerts": ["td"]})
            self.assertEqual(saved.preset_id, "quiet")
            self.assertEqual(load_crypto_alert_config(root, "quiet").enabled_alerts, ("td",))
            self.assertIn("default", list_crypto_alert_presets(root))

    def test_invalid_preset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaises(ValueError):
                save_crypto_alert_preset(Path(tempdir), "bad/name", {"enabled_alerts": ["td"]})

    def test_activated_preset_is_used_when_no_preset_is_explicitly_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            save_crypto_alert_preset(root, "quiet", {"enabled_alerts": ["td"]})

            activate_crypto_alert_preset(root, "quiet")

            active = load_crypto_alert_config(root)
            self.assertEqual(active.preset_id, "quiet")
            self.assertEqual(active.enabled_alerts, ("td",))

    def test_non_positive_indicator_period_in_preset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaises(ValueError):
                save_crypto_alert_preset(Path(tempdir), "bad-period", {"parameters": {"sma_period": 0}})

    def test_saved_preset_uses_current_signal_algorithm_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            save_crypto_alert_preset(root, "old", {"algorithm_version": "crypto-alerts-v3"})

            self.assertEqual(load_crypto_alert_config(root, "old").algorithm_version, ALGORITHM_VERSION)


if __name__ == "__main__":
    unittest.main()
