"""Unit tests for _fetch_history multi-source logic."""
import io
import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd
import numpy as np
from trend_radar.fetcher import (
    _fetch_history,
    _fetch_history_eastmoney,
    _fetch_history_sina,
    _HISTORY_API_COLUMNS,
)

EASTMONEY_KLINES = [
    "2026-05-22,3.450,3.460,3.480,3.440,12345678,42678900,1.16,0.58,0.02,2.34",
    "2026-05-23,3.460,3.470,3.490,3.450,11234567,38890000,1.16,0.29,0.01,2.30",
    "2026-05-25,3.470,3.490,3.520,3.460,13456789,46900000,1.73,0.58,0.02,2.45",
    "2026-05-26,3.490,3.480,3.510,3.470,10876543,37850000,1.15,-0.29,-0.01,2.28",
    "2026-05-27,3.480,3.500,3.530,3.470,11234567,39320000,1.73,0.57,0.02,2.35",
    "2026-05-28,3.500,3.520,3.540,3.490,12345678,43450000,1.44,0.57,0.02,2.40",
    "2026-05-29,3.520,3.540,3.560,3.510,11890000,41780000,1.42,0.57,0.02,2.38",
]


def _make_mock_urlopen(klines):
    resp = MagicMock()
    resp_data = {"data": {"klines": klines}}
    resp.read.return_value = json.dumps(resp_data).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestEastmoneyDirect(unittest.TestCase):
    def test_parses_klines_correctly(self):
        mock = _make_mock_urlopen(EASTMONEY_KLINES)
        with patch("urllib.request.urlopen", return_value=mock):
            df = _fetch_history_eastmoney("510050", "20260522", "20260529")
        self.assertEqual(len(df), 7)
        self.assertListEqual(list(df.columns), _HISTORY_API_COLUMNS)
        self.assertEqual(df.iloc[-1]["收盘"], "3.540")

    def test_empty_data(self):
        mock = _make_mock_urlopen([])
        with patch("urllib.request.urlopen", return_value=mock):
            df = _fetch_history_eastmoney("510050", "20260522", "20260529")
        self.assertEqual(len(df), 0)

    def test_null_data_field(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"data": None}).encode("utf-8")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            df = _fetch_history_eastmoney("510050", "20260522", "20260529")
        self.assertEqual(len(df), 0)

    def test_sh_secid(self):
        mock = _make_mock_urlopen(EASTMONEY_KLINES)
        with patch("urllib.request.urlopen", return_value=mock):
            with patch("urllib.request.Request") as mock_req:
                _fetch_history_eastmoney("588000", "20260522", "20260529")
                url = mock_req.call_args[0][0]
                self.assertIn("secid=1.588000", url)

    def test_sz_secid(self):
        mock = _make_mock_urlopen(EASTMONEY_KLINES)
        with patch("urllib.request.urlopen", return_value=mock):
            with patch("urllib.request.Request") as mock_req:
                _fetch_history_eastmoney("159915", "20260522", "20260529")
                url = mock_req.call_args[0][0]
                self.assertIn("secid=0.159915", url)


class TestSinaFallback(unittest.TestCase):
    def setUp(self):
        self.mock_ak = MagicMock()

    def _make_sina_df(self, dates, prices):
        n = len(dates)
        return pd.DataFrame({
            "date": [date.fromisoformat(d) for d in dates],
            "open": [p - 0.02 for p in prices],
            "high": [p + 0.03 for p in prices],
            "low": [p - 0.01 for p in prices],
            "close": prices,
            "volume": [10000000] * n,
        })

    def test_converts_sina_to_eastmoney_format(self):
        sina_df = self._make_sina_df(
            ["2026-05-22", "2026-05-23", "2026-05-25"],
            [3.45, 3.47, 3.49],
        )
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df

        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")
        self.assertEqual(len(df), 3)
        self.assertListEqual(list(df.columns), _HISTORY_API_COLUMNS)
        self.assertEqual(float(df.iloc[-1]["收盘"]), 3.49)
        self.assertEqual(float(df.iloc[-1]["开盘"]), 3.47)

    def test_filters_by_date_range(self):
        sina_df = self._make_sina_df(
            ["2026-05-20", "2026-05-22", "2026-05-25", "2026-05-30"],
            [3.40, 3.45, 3.47, 3.50],
        )
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df

        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")
        self.assertEqual(len(df), 2)
        self.assertEqual(float(df.iloc[0]["收盘"]), 3.45)
        self.assertEqual(float(df.iloc[1]["收盘"]), 3.47)

    def test_empty_sina_data(self):
        self.mock_ak.fund_etf_hist_sina.return_value = pd.DataFrame()
        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")
        self.assertEqual(len(df), 0)

    def test_no_data_in_range(self):
        sina_df = self._make_sina_df(
            ["2026-05-20", "2026-05-21"],
            [3.40, 3.42],
        )
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df
        df = _fetch_history_sina(self.mock_ak, "510050", "20260525", "20260529")
        self.assertEqual(len(df), 0)

    def test_sh_prefix(self):
        sina_df = self._make_sina_df(["2026-05-22"], [3.45])
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df
        _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")
        self.mock_ak.fund_etf_hist_sina.assert_called_with(symbol="sh510050")

    def test_sz_prefix(self):
        sina_df = self._make_sina_df(["2026-05-22"], [3.45])
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df
        _fetch_history_sina(self.mock_ak, "159915", "20260522", "20260529")
        self.mock_ak.fund_etf_hist_sina.assert_called_with(symbol="sz159915")

    def test_calculates_pct_change(self):
        sina_df = self._make_sina_df(
            ["2026-05-22", "2026-05-23"],
            [3.45, 3.50],
        )
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df

        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")
        pct = float(df.iloc[1]["涨跌幅"])
        expected = (3.50 - 3.45) / 3.45 * 100
        self.assertAlmostEqual(pct, expected, places=2)

    def test_calculates_amount(self):
        sina_df = self._make_sina_df(
            ["2026-05-22"],
            [3.45],
        )
        sina_df["volume"] = [20000000]
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df

        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")
        amount = float(df.iloc[0]["成交额"])
        expected = 3.45 * 20000000
        self.assertAlmostEqual(amount, expected, places=2)
        self.assertEqual(float(df.iloc[0]["成交量"]), 200000)

    def test_uses_sina_raw_amount_when_available(self):
        sina_df = self._make_sina_df(
            ["2026-05-22"],
            [3.45],
        )
        sina_df["volume"] = [20000000]
        sina_df["amount"] = [70000000]
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df

        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")

        self.assertEqual(float(df.iloc[0]["成交额"]), 70000000)

    def test_keeps_volume_when_sina_volume_already_looks_like_hands(self):
        sina_df = self._make_sina_df(
            ["2026-05-22"],
            [3.45],
        )
        sina_df["volume"] = [200000]
        sina_df["amount"] = [69000000]
        self.mock_ak.fund_etf_hist_sina.return_value = sina_df

        df = _fetch_history_sina(self.mock_ak, "510050", "20260522", "20260529")

        self.assertEqual(float(df.iloc[0]["成交量"]), 200000)


class TestMultiSourceFallback(unittest.TestCase):
    def setUp(self):
        self.mock_ak = MagicMock()

    def test_uses_eastmoney_when_available(self):
        mock = _make_mock_urlopen(EASTMONEY_KLINES)
        with patch("urllib.request.urlopen", return_value=mock):
            df = _fetch_history(self.mock_ak, "510050", "20260522", "20260529")
        self.assertEqual(len(df), 7)
        self.mock_ak.fund_etf_hist_sina.assert_not_called()

    def test_falls_back_to_sina_when_eastmoney_fails(self):
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionError("blocked")),
            patch("trend_radar.fetcher._fetch_history_eastmoney_curl", side_effect=RuntimeError("curl down")),
        ):
            sina_df = pd.DataFrame({
                "date": [date.fromisoformat("2026-05-22")],
                "open": [3.43],
                "high": [3.48],
                "low": [3.44],
                "close": [3.45],
                "volume": [10000000],
            })
            self.mock_ak.fund_etf_hist_sina.return_value = sina_df

            df = _fetch_history(self.mock_ak, "510050", "20260522", "20260529")
            self.assertEqual(len(df), 1)
            self.mock_ak.fund_etf_hist_sina.assert_called_once()

    def test_raises_when_all_sources_fail(self):
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionError("blocked")),
            patch("trend_radar.fetcher._fetch_history_eastmoney_curl", side_effect=RuntimeError("curl down")),
        ):
            self.mock_ak.fund_etf_hist_sina.side_effect = RuntimeError("sina down")
            with self.assertRaises(RuntimeError) as ctx:
                _fetch_history(self.mock_ak, "510050", "20260522", "20260529")
            self.assertIn("All sources exhausted", str(ctx.exception))
            self.assertIn("sina", str(ctx.exception))

    def test_eastmoney_single_attempt_falls_to_sina(self):
        """R2: eastmoney 单次尝试失败 → sina 兜底（不再重试 2 次）"""
        call_count = [0]

        def urlopen_fail(*args, **kwargs):
            call_count[0] += 1
            raise ConnectionError("blocked")

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_fail),
            patch("trend_radar.fetcher._fetch_history_eastmoney_curl", side_effect=RuntimeError("curl down")),
        ):
            sina_df = pd.DataFrame({
                "date": [date.fromisoformat("2026-05-22")],
                "open": [3.43],
                "high": [3.48],
                "low": [3.44],
                "close": [3.45],
                "volume": [10000000],
            })
            self.mock_ak.fund_etf_hist_sina.return_value = sina_df

            df = _fetch_history(self.mock_ak, "510050", "20260522", "20260529")
            self.assertEqual(len(df), 1)
            # eastmoney urllib 只调用 1 次（不再重试）
            self.assertEqual(call_count[0], 1)
            self.mock_ak.fund_etf_hist_sina.assert_called_once()


class TestSinaTimeout(unittest.TestCase):
    def test_sina_call_timeout_records_error(self) -> None:
        """R2: sina 调用超时 → 记为 error，不阻塞"""
        from trend_radar.fetcher import _fetch_history_with_attempt_details
        import time

        # akshare sina 接口模拟永久阻塞
        blocking_ak = MagicMock()
        def blocking_sina(*args, **kwargs):
            time.sleep(30)  # 模拟阻塞
        blocking_ak.fund_etf_hist_sina = blocking_sina

        # urllib 和 curl 都失败，sina 阻塞
        with patch("trend_radar.fetcher._fetch_history_eastmoney_with_provider", side_effect=RuntimeError("eastmoney down")):
            start = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                _fetch_history_with_attempt_details(blocking_ak, "159995", "20260522", "20260529")
            elapsed = time.monotonic() - start
            # 应在 ~10s 内超时（不是 30s）
            self.assertLess(elapsed, 15, f"sina should timeout, not block; elapsed={elapsed}")
            self.assertIn("sina", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
