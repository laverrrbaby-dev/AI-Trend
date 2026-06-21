from __future__ import annotations

import sys
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pandas as pd  # noqa: E402

from trend_radar.crypto import (  # noqa: E402
    BINANCE_EXCHANGE_INFO_URL,
    BINANCE_KLINES_URL,
    BINANCE_TICKER_24H_URL,
    BYBIT_INSTRUMENTS_URL,
    BYBIT_KLINES_URL,
    BYBIT_TICKERS_URL,
    COINBASE_PRODUCTS_URL,
    CRYPTO_REFRESH_FAILED,
    CRYPTO_REFRESH_SKIPPED,
    CRYPTO_REFRESH_SUCCEEDED,
    KRAKEN_ASSET_PAIRS_URL,
    KRAKEN_OHLC_URL,
    OKX_HISTORY_CANDLES_URL,
    OKX_INSTRUMENTS_URL,
    OKX_TICKERS_URL,
    fetch_multi_exchange_crypto_turnover,
    latest_crypto_cache_date,
    load_crypto_metadata,
    load_crypto_snapshot,
    read_crypto_refresh_status,
    refresh_crypto_turnover,
    resolve_crypto_target_date,
    _fetch_coinbase_records,
    _aggregate_records,
    _tag_exchange_records,
    _window,
)


TARGET_DATE = "2026-06-20"
DAY_MS = int(datetime(2026, 6, 20, tzinfo=UTC).timestamp() * 1000)
DAY_SECONDS = int(datetime(2026, 6, 20, tzinfo=UTC).timestamp())
BINANCE_AMOUNTS = {
    "BTCUSDT": 1000.0,
    "ETHUSDT": 900.0,
    "BNBUSDT": 800.0,
    "SOLUSDT": 700.0,
    "XRPUSDT": 600.0,
    "ADAUSDT": 500.0,
    "DOGEUSDT": 400.0,
    "LINKUSDT": 300.0,
    "AVAXUSDT": 200.0,
    "MATICUSDT": 100.0,
    "USDCUSDT": 99_999.0,
}


class CryptoTurnoverTests(unittest.TestCase):
    def test_target_date_uses_latest_complete_utc_day(self) -> None:
        now = datetime(2026, 6, 21, 8, 0, tzinfo=UTC)

        self.assertEqual(resolve_crypto_target_date(now), TARGET_DATE)

    def test_multi_exchange_aggregation_parses_sources_and_filters_stables(self) -> None:
        frame, metadata = fetch_multi_exchange_crypto_turnover(
            fetch_json=fake_crypto_fetch,
            target_date=TARGET_DATE,
        )

        self.assertEqual(metadata["successful_exchanges"], ["binance", "coinbase", "kraken", "okx", "bybit"])
        self.assertFalse(metadata["partial"])
        self.assertEqual(len(frame), 10)
        self.assertNotIn("USDC", frame["币种"].tolist())

        btc = frame[frame["币种"] == "BTC"].iloc[0]
        self.assertEqual(float(btc["30日成交额"]), 2800.0)
        self.assertEqual(int(btc["交易所数"]), 5)
        self.assertEqual(int(btc["交易对数"]), 5)
        self.assertEqual(btc["最大来源"], "Binance")
        self.assertIn("Kraken", btc["来源明细"])

        eth = frame[frame["币种"] == "ETH"].iloc[0]
        self.assertEqual(float(eth["30日成交额"]), 1300.0)

    def test_partial_exchange_failure_still_generates_top10_metadata(self) -> None:
        def fail_coinbase(url: str, headers: dict[str, str]) -> object:
            if url == COINBASE_PRODUCTS_URL:
                raise RuntimeError("coinbase unavailable")
            return fake_crypto_fetch(url, headers)

        frame, metadata = fetch_multi_exchange_crypto_turnover(
            fetch_json=fail_coinbase,
            target_date=TARGET_DATE,
        )

        self.assertEqual(len(frame), 10)
        self.assertTrue(metadata["partial"])
        self.assertIn({"exchange": "coinbase", "error": "coinbase unavailable"}, metadata["failed_exchanges"])
        self.assertNotIn("coinbase", metadata["successful_exchanges"])

    def test_unranked_exchange_limits_to_preferred_assets(self) -> None:
        requested_products: list[str] = []

        def fake_many_coinbase(url: str, headers: dict[str, str]) -> object:
            del headers
            parsed = urllib.parse.urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if url == COINBASE_PRODUCTS_URL:
                rows = [
                    _coinbase_product("BTC-USD", "BTC", "USD", price=0, volume_24h=0),
                    _coinbase_product("ETH-USD", "ETH", "USD", price=0, volume_24h=0),
                    _coinbase_product("ZZZ-USD", "ZZZ", "USD", price=0, volume_24h=0),
                ]
                rows.extend(
                    _coinbase_product(f"ALT{index}-USD", f"ALT{index}", "USD", price=0, volume_24h=0)
                    for index in range(80)
                )
                return rows
            if base_url.startswith(COINBASE_PRODUCTS_URL) and base_url.endswith("/candles"):
                requested_products.append(base_url.split("/")[-2])
                return [[DAY_SECONDS, 90, 110, 100, 100, 1]]
            raise AssertionError(f"unexpected URL {url}")

        records, metadata = _fetch_coinbase_records(fake_many_coinbase, _window(TARGET_DATE, 30))

        self.assertEqual({record["base_asset"] for record in records}, {"BTC", "ETH"})
        self.assertEqual(requested_products, ["BTC-USD", "ETH-USD"])
        self.assertEqual(metadata["candidate_rows"], 2)

    def test_exchange_tagged_vision_records_can_be_aggregated(self) -> None:
        window = _window(TARGET_DATE, 30)
        records = [
            {
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "date": TARGET_DATE,
                "quote_turnover_usd": 100.0,
                "method": "binance_vision_quote_asset_volume",
            }
        ]

        _tag_exchange_records(records, "binance_vision")
        frame = _aggregate_records(records, window, 10)

        self.assertEqual(frame.iloc[0]["币种"], "BTC")
        self.assertEqual(float(frame.iloc[0]["30日成交额"]), 100.0)
        self.assertEqual(frame.iloc[0]["最大来源"], "Binance Vision")

    def test_refresh_writes_cache_metadata_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            result = refresh_crypto_turnover(
                root=root,
                now=datetime(2026, 6, 21, 8, 0, tzinfo=UTC),
                fetch_json=fake_crypto_fetch,
            )

            self.assertEqual(result.status, CRYPTO_REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, TARGET_DATE)
            self.assertEqual(latest_crypto_cache_date(root), TARGET_DATE)
            snapshot = load_crypto_snapshot(TARGET_DATE, root=root)
            self.assertEqual(snapshot.iloc[0]["币种"], "BTC")
            metadata = load_crypto_metadata(TARGET_DATE, root=root)
            self.assertEqual(metadata["source"], "multi_exchange")
            self.assertEqual(metadata["window_start"], "2026-05-22")
            self.assertEqual(read_crypto_refresh_status(root)["status"], CRYPTO_REFRESH_SUCCEEDED)

    def test_refresh_cache_uses_actual_date_after_source_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            frame = pd.DataFrame(
                [
                    {
                        "币种": "BTC",
                        "30日成交额": 100.0,
                        "日均成交额": 3.33,
                        "交易所数": 1,
                        "交易对数": 1,
                        "最大来源": "Binance Vision",
                        "最大来源占比": 100.0,
                        "来源明细": "Binance Vision 100.0%",
                        "窗口开始": "2026-05-21",
                        "窗口结束": "2026-06-19",
                    }
                ]
            )
            metadata = {
                "source": "binance_vision_fallback",
                "window_start": "2026-05-21",
                "window_end": "2026-06-19",
            }

            with (
                patch("trend_radar.crypto._resolve_binance_vision_target_date", return_value="2026-06-19"),
                patch("trend_radar.crypto.fetch_multi_exchange_crypto_turnover", return_value=(frame, metadata)) as fetch_mock,
            ):
                result = refresh_crypto_turnover(root=root, now=datetime(2026, 6, 21, 8, 0, tzinfo=UTC))

            self.assertEqual(result.target_date, TARGET_DATE)
            self.assertEqual(result.actual_date, "2026-06-19")
            self.assertEqual(fetch_mock.call_args.kwargs["target_date"], "2026-06-19")
            snapshot = load_crypto_snapshot("2026-06-19", root=root)
            self.assertEqual(set(snapshot["数据日期"]), {"2026-06-19"})

    def test_existing_target_cache_skips_fetch_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            refresh_crypto_turnover(root=root, now=datetime(2026, 6, 21, 8, 0, tzinfo=UTC), fetch_json=fake_crypto_fetch)

            def fail_fetch(url: str, headers: dict[str, str]) -> object:
                raise AssertionError("fetch should not be called")

            result = refresh_crypto_turnover(root=root, now=datetime(2026, 6, 21, 9, 0, tzinfo=UTC), fetch_json=fail_fetch)

            self.assertEqual(result.status, CRYPTO_REFRESH_SKIPPED)
            self.assertEqual(result.actual_date, TARGET_DATE)

    def test_refresh_failure_preserves_existing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            refresh_crypto_turnover(root=root, now=datetime(2026, 6, 21, 8, 0, tzinfo=UTC), fetch_json=fake_crypto_fetch)

            def fail_fetch(url: str, headers: dict[str, str]) -> object:
                raise RuntimeError("all exchanges unavailable")

            result = refresh_crypto_turnover(
                root=root,
                force=True,
                now=datetime(2026, 6, 21, 9, 0, tzinfo=UTC),
                fetch_json=fail_fetch,
            )

            self.assertEqual(result.status, CRYPTO_REFRESH_FAILED)
            self.assertEqual(result.actual_date, TARGET_DATE)
            self.assertIn("继续展示", result.message or "")


def fake_crypto_fetch(url: str, headers: dict[str, str]) -> object:
    del headers
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if url == BINANCE_EXCHANGE_INFO_URL:
        return {"symbols": [_binance_symbol(symbol) for symbol in BINANCE_AMOUNTS]}
    if url == BINANCE_TICKER_24H_URL:
        return [{"symbol": symbol, "quoteVolume": str(amount)} for symbol, amount in BINANCE_AMOUNTS.items()]
    if base_url == BINANCE_KLINES_URL:
        symbol = query["symbol"][0]
        return [_binance_kline(BINANCE_AMOUNTS[symbol])]
    if url == COINBASE_PRODUCTS_URL:
        return [
            _coinbase_product("BTC-USD", "BTC", "USD", price=100, volume_24h=3),
            _coinbase_product("ETH-USD", "ETH", "USD", price=100, volume_24h=3),
            _coinbase_product("USDT-USD", "USDT", "USD", price=1, volume_24h=999999),
        ]
    if base_url.startswith(COINBASE_PRODUCTS_URL) and base_url.endswith("/candles"):
        product = base_url.split("/")[-2]
        if product == "BTC-USD":
            return [[DAY_SECONDS, 90, 110, 100, 100, 3]]
        if product == "ETH-USD":
            return [[DAY_SECONDS, 90, 110, 100, 100, 3]]
        return []
    if url == KRAKEN_ASSET_PAIRS_URL:
        return {
            "error": [],
            "result": {
                "XXBTZUSD": {"wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD", "status": "online"},
                "XETHZUSD": {"wsname": "ETH/USD", "base": "XETH", "quote": "ZUSD", "status": "online"},
                "USDTZUSD": {"wsname": "USDT/USD", "base": "USDT", "quote": "ZUSD", "status": "online"},
            },
        }
    if base_url == KRAKEN_OHLC_URL:
        pair = query["pair"][0]
        if pair == "XXBTZUSD":
            return {"error": [], "result": {"XXBTZUSD": [[DAY_SECONDS, "1", "1", "1", "1", "10", "40", 1]], "last": DAY_SECONDS}}
        if pair == "XETHZUSD":
            return {"error": [], "result": {"XETHZUSD": [[DAY_SECONDS, "1", "1", "1", "1", "10", "10", 1]], "last": DAY_SECONDS}}
        return {"error": [], "result": {"USDTZUSD": []}}
    if base_url == OKX_INSTRUMENTS_URL:
        return {
            "data": [
                {"instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT", "state": "live"},
                {"instId": "SOL-USDT", "baseCcy": "SOL", "quoteCcy": "USDT", "state": "live"},
                {"instId": "DAI-USDT", "baseCcy": "DAI", "quoteCcy": "USDT", "state": "live"},
            ]
        }
    if base_url == OKX_TICKERS_URL:
        return {"data": [{"instId": "BTC-USDT", "volCcy24h": "500"}, {"instId": "SOL-USDT", "volCcy24h": "50"}]}
    if base_url == OKX_HISTORY_CANDLES_URL:
        inst_id = query["instId"][0]
        if inst_id == "BTC-USDT":
            return {"data": [[str(DAY_MS), "1", "1", "1", "1", "1", "1", "500", "1"]]}
        if inst_id == "SOL-USDT":
            return {"data": [[str(DAY_MS), "1", "1", "1", "1", "1", "1", "50", "1"]]}
        return {"data": []}
    if base_url == BYBIT_INSTRUMENTS_URL:
        return {
            "result": {
                "list": [
                    {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading"},
                    {"symbol": "XRPUSDC", "baseCoin": "XRP", "quoteCoin": "USDC", "status": "Trading"},
                    {"symbol": "PYUSDUSDT", "baseCoin": "PYUSD", "quoteCoin": "USDT", "status": "Trading"},
                ]
            }
        }
    if base_url == BYBIT_TICKERS_URL:
        return {"result": {"list": [{"symbol": "BTCUSDT", "turnover24h": "600"}, {"symbol": "XRPUSDC", "turnover24h": "60"}]}}
    if base_url == BYBIT_KLINES_URL:
        symbol = query["symbol"][0]
        if symbol == "BTCUSDT":
            return {"result": {"list": [[str(DAY_MS), "1", "1", "1", "1", "1", "600"]]}}
        if symbol == "XRPUSDC":
            return {"result": {"list": [[str(DAY_MS), "1", "1", "1", "1", "1", "60"]]}}
        return {"result": {"list": []}}
    raise AssertionError(f"unexpected URL {url}")


def _binance_symbol(symbol: str) -> dict[str, object]:
    base = symbol.removesuffix("USDT")
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": "USDT",
        "status": "TRADING",
        "isSpotTradingAllowed": True,
    }


def _binance_kline(amount: float) -> list[object]:
    return [DAY_MS, "1", "1", "1", "1", "1", DAY_MS + 1, str(amount), 1, "1", "1", "0"]


def _coinbase_product(symbol: str, base: str, quote: str, price: float, volume_24h: float) -> dict[str, object]:
    return {
        "id": symbol,
        "base_currency": base,
        "quote_currency": quote,
        "status": "online",
        "trading_disabled": False,
        "price": str(price),
        "volume_24h": str(volume_24h),
    }


if __name__ == "__main__":
    unittest.main()
