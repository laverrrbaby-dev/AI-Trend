from __future__ import annotations

import sys
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.crypto_alert_config import load_crypto_alert_config  # noqa: E402
from trend_radar.crypto_market_data import (  # noqa: E402
    BINANCE_KLINES_URL,
    OKX_HISTORY_CANDLES_URL,
    fetch_crypto_history,
    load_crypto_history,
)


NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


class CryptoMarketDataTests(unittest.TestCase):
    def test_binance_request_uses_china_daily_boundary_and_drops_open_candle(self) -> None:
        seen: list[str] = []

        def fetch(url: str, headers: dict[str, str]) -> object:
            del headers
            seen.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertEqual(query["interval"], ["1d"])
            self.assertEqual(query["timeZone"], ["8"])
            self.assertEqual(query["limit"], ["500"])
            return [
                _binance_row("2026-09-08", 100.0, complete=True),
                _binance_row("2026-09-09", 110.0, complete=True),
                _binance_row("2026-09-10", 120.0, complete=False),
            ]

        root = Path(tempfile.mkdtemp())
        result = fetch_crypto_history("BTC", load_crypto_alert_config(PROJECT_ROOT), root, now=NOW, fetch_json=fetch)
        self.assertEqual(result.frame["日期"].tolist(), ["2026-09-08", "2026-09-09"])
        self.assertTrue(result.frame["is_complete"].all())
        self.assertEqual(result.in_progress["日期"], "2026-09-10")
        self.assertEqual(result.frame["收盘价"].tolist(), [100.0, 110.0])
        cached = load_crypto_history(root, "BTC")
        self.assertEqual(cached["is_complete"].tolist(), [True, True, False])
        self.assertEqual(seen[0].split("?")[0], BINANCE_KLINES_URL)

    def test_xaut_uses_okx_utc8_daily_candles_and_confirm_flag(self) -> None:
        seen: list[str] = []

        def fetch(url: str, headers: dict[str, str]) -> object:
            del headers
            seen.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertEqual(query["bar"], ["1D"])
            self.assertNotIn("1Dutc", url)
            return {
                "code": "0",
                "data": [
                    ["1788825600000", "2490", "2520", "2480", "2500", "100", "249900", "249900", "1"],
                    ["1788912000000", "2500", "2530", "2490", "2510", "110", "275550", "275550", "1"],
                ],
            }

        result = fetch_crypto_history("XAUT", load_crypto_alert_config(PROJECT_ROOT), Path(tempfile.mkdtemp()), now=NOW, fetch_json=fetch)
        self.assertEqual(result.source, "okx")
        self.assertEqual(result.frame["收盘价"].tolist(), [2500.0, 2510.0])
        self.assertEqual(seen[0].split("?")[0], OKX_HISTORY_CANDLES_URL)

    def test_invalid_ohlc_is_rejected_without_writing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            def fetch(url: str, headers: dict[str, str]) -> object:
                del url, headers
                return [_binance_row("2026-09-09", 100.0, complete=True, low=120.0)]

            with self.assertRaises(ValueError):
                fetch_crypto_history("BTC", load_crypto_alert_config(PROJECT_ROOT), Path(tempdir), now=NOW, fetch_json=fetch)
            self.assertFalse((Path(tempdir) / "data" / "crypto" / "alerts" / "history" / "BTC.csv").exists())

    def test_non_finite_or_non_positive_price_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            def fetch(url: str, headers: dict[str, str]) -> object:
                del url, headers
                return [_binance_row("2026-09-09", 0.0, complete=True)]

            with self.assertRaises(ValueError):
                fetch_crypto_history("BTC", load_crypto_alert_config(PROJECT_ROOT), Path(tempdir), now=NOW, fetch_json=fetch)

    def test_primary_failure_uses_configured_backup_and_records_the_switch(self) -> None:
        def fetch(url: str, headers: dict[str, str]) -> object:
            del headers
            if url.startswith(BINANCE_KLINES_URL):
                raise RuntimeError("binance unavailable")
            self.assertTrue(url.startswith(OKX_HISTORY_CANDLES_URL))
            return {"code": "0", "data": [_okx_row("2026-09-09", 100.0)]}

        result = fetch_crypto_history("BTC", load_crypto_alert_config(PROJECT_ROOT), Path(tempfile.mkdtemp()), now=NOW, fetch_json=fetch)

        self.assertEqual(result.source, "okx")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.frame["日期"].tolist(), ["2026-09-09"])


def _binance_row(day: str, close: float, complete: bool, low: float | None = None) -> list[object]:
    local_day = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    start = int((local_day - timedelta(days=1)).replace(hour=16).timestamp() * 1000)
    close_time = start + 86_400_000 - 1
    if not complete:
        close_time = int(NOW.timestamp() * 1000) + 10_000
    return [start, str(close - 1), str(close + 2), str(low if low is not None else close - 2), str(close), "100", close_time, "10000", 10, "50", "5000", "0"]


def _okx_row(day: str, close: float) -> list[str]:
    start = int(datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp() * 1000)
    return [str(start), str(close - 1), str(close + 2), str(close - 2), str(close), "100", "10000", "10000", "1"]


if __name__ == "__main__":
    unittest.main()
