from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_trade_checklist import valid_draft
from test_trade_records import execution
from trend_radar.trade_records import TradeRecord
from trend_radar.trade_record_market import StopMarket, fetch_stop_market, suggest_stop


class StopSuggestionTests(unittest.TestCase):
    def test_long_tightens_initial_exit_then_keeps_tighter_manual_stop(self) -> None:
        record = TradeRecord("a" * 32, 1, "active", valid_draft(), execution())
        market = StopMarket("BTC-USDT-SWAP", Decimal("102"), Decimal("110"), Decimal("95"),
                            datetime.fromisoformat("2026-10-16T00:00:00+08:00"),
                            datetime.fromisoformat("2026-10-17T10:00:00+08:00"))
        suggestion = suggest_stop(record, market)
        self.assertEqual(suggestion.action, "tighten")
        self.assertEqual(suggestion.price, Decimal("95"))
        record = TradeRecord("a" * 32, 2, "active", valid_draft(), execution(), "97")
        self.assertEqual(suggest_stop(record, market).action, "keep")

    def test_short_mirror_equal_and_crossed_prices(self) -> None:
        draft = valid_draft(direction="做空", planned_exit_price="110", liquidation_price="112", take_profit_price="80")
        record = TradeRecord("a" * 32, 1, "active", draft, execution())
        market = StopMarket("BTC-USDT-SWAP", Decimal("102"), Decimal("105"), Decimal("95"),
                            datetime.fromisoformat("2026-10-16T00:00:00+08:00"),
                            datetime.fromisoformat("2026-10-17T10:00:00+08:00"))
        self.assertEqual(suggest_stop(record, market).action, "tighten")
        self.assertEqual(suggest_stop(record, market).price, Decimal("105"))
        self.assertEqual(suggest_stop(replace(record, stop_price="105"), market).action, "keep")
        for price in ("105", "106"):
            self.assertEqual(suggest_stop(record, replace(market, current_price=Decimal(price))).action, "crossed")
        long = replace(record, snapshot=valid_draft())
        for price in ("95", "94"):
            self.assertEqual(suggest_stop(long, replace(market, current_price=Decimal(price))).action, "crossed")
        with self.assertRaises(ValueError):
            suggest_stop(long, replace(market, instrument_id="BTC-USDT"))


class StopMarketTests(unittest.TestCase):
    NOW = datetime.fromisoformat("2026-10-17T00:00:30+08:00")

    def setUp(self) -> None:
        self.requests: list[str] = []
        self.ticker = {"instId": "BTC-USDT-SWAP", "last": "102", "ts": str(int(self.NOW.timestamp() * 1000))}
        self.rows = [self.candle("2026-10-17T00:00:00+08:00", "0"),
                     self.candle("2026-10-16T00:00:00+08:00", "1")]

    def candle(self, opened: str, confirmed: str) -> list[str]:
        return [str(int(datetime.fromisoformat(opened).timestamp() * 1000)), "100", "110", "95", "102", "1", "1", "1", confirmed]

    def transport(self, url: str, headers: dict[str, str]) -> dict:
        self.requests.append(url)
        return {"code": "0", "data": [self.ticker] if urlparse(url).path.endswith("/ticker") else self.rows}

    def fetch(self) -> StopMarket:
        return fetch_stop_market("BTC-USDT-SWAP", now=self.NOW, fetch_json=self.transport)

    def test_fetch_uses_actual_contract_and_completed_beijing_yesterday(self) -> None:
        market = self.fetch()
        self.assertEqual(market.candle_open.isoformat(), "2026-10-16T00:00:00+08:00")
        self.assertEqual(market.low, Decimal("95"))
        self.assertEqual(market.current_price, Decimal("102"))
        candle_request = next(url for url in self.requests if urlparse(url).path.endswith("/candles"))
        self.assertEqual(parse_qs(urlparse(candle_request).query)["bar"], ["1D"])
        for url in self.requests:
            self.assertEqual(parse_qs(urlparse(url).query)["instId"], ["BTC-USDT-SWAP"])

    def test_wrong_day_utc_boundaries_and_unfinished_candles_fail(self) -> None:
        for row in [self.candle("2026-10-16T08:00:00+08:00", "1"),
                    self.candle("2026-10-15T00:00:00+08:00", "1"),
                    self.candle("2026-10-16T00:00:00+08:00", "0")]:
            self.rows = [row]
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.fetch()

    def test_stale_future_wrong_instrument_and_invalid_prices_fail(self) -> None:
        original = self.ticker.copy()
        for changes in [{"ts": str(int(self.NOW.timestamp() * 1000) - 121000)},
                        {"ts": str(int(self.NOW.timestamp() * 1000) + 6000)},
                        {"instId": "BTC-USDT"}, {"last": "NaN"}, {"last": "0"}]:
            self.ticker = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.fetch()
        self.ticker = original
        self.rows[1][2] = "90"
        with self.assertRaises(ValueError):
            self.fetch()

    def test_api_error_or_malformed_payload_is_unavailable(self) -> None:
        for payload in [{"code": "51001", "msg": "invalid instrument"}, {}, {"code": "0", "data": []},
                        {"code": "0", "data": [None]}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                fetch_stop_market("BTC-USDT-SWAP", now=self.NOW, fetch_json=lambda *_: payload)


if __name__ == "__main__":
    unittest.main()
