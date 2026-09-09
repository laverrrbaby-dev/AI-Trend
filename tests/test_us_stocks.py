from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.us_stocks import (  # noqa: E402
    NEW_YORK,
    NASDAQ_SCREENER_URL,
    TRADINGVIEW_SCANNER_URL,
    US_REFRESH_FAILED,
    US_REFRESH_SKIPPED,
    US_REFRESH_SUCCEEDED,
    US_CN_NAME_SEED,
    US_TOP_TURNOVER_LIMIT,
    YAHOO_CHART_URL,
    fetch_us_top_turnover,
    latest_us_stock_cache_date,
    load_us_name_map,
    load_us_stock_metadata,
    load_us_stock_snapshot,
    read_us_stock_refresh_status,
    refresh_us_stock_turnover,
    resolve_us_stock_target_date,
)
from trend_radar.us_stock_anomalies import evaluate_anomalies, serialize_anomaly_details
from trend_radar.us_stock_news import load_cached_news, load_or_fetch_news


class UsStocksTests(unittest.TestCase):
    def test_refresh_persists_yahoo_history_and_anomaly_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            history_calls: list[str] = []

            def scanner_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                return {"data": _tradingview_rows()}

            def history_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                history_calls.append(url)
                timestamps = [int(datetime(2026, 5, 1 + index, tzinfo=NEW_YORK).timestamp()) for index in range(20)]
                return {
                    "chart": {
                        "result": [
                            {
                                "timestamp": timestamps,
                                "indicators": {"quote": [{"close": [10.0] * 20, "volume": [100.0] * 20}]},
                            }
                        ]
                    }
                }

            result = refresh_us_stock_turnover(
                root=root,
                now=datetime(2026, 6, 3, 17, 0, tzinfo=NEW_YORK),
                fetch_json=scanner_fetch,
                history_fetch_json=history_fetch,
            )

            snapshot = load_us_stock_snapshot(result.target_date, root)

        self.assertEqual(len(history_calls), US_TOP_TURNOVER_LIMIT)
        self.assertIn("异动等级", snapshot.columns)
        self.assertIn("数据生成时间", snapshot.columns)
        self.assertTrue((snapshot["异动数据状态"] == "历史数据完整").all())

    def test_news_without_key_is_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch.dict("os.environ", {}, clear=True):
            result = load_or_fetch_news("AAA", "示例公司", "2026-06-03", root=Path(tempdir))

        self.assertEqual(result["status"], "missing_key")
        self.assertEqual(result["articles"], [])

    def test_news_normalizes_two_articles_and_caches_result(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            calls: list[str] = []

            def fake_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                calls.append(url)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '[{"title_zh":"公司公告","summary_zh":"公司发布公开公告。","source":"示例来源","url":"https://example.com/a","published_at":"2026-06-03"},{"title_zh":"行业消息","summary_zh":"行业出现公开变化。","source":"另一来源","url":"https://example.com/b","published_at":"2026-06-03"},{"title_zh":"超出限制"}]'
                            }
                        }
                    ]
                }

            first = load_or_fetch_news("AAA", "示例公司", "2026-06-03", root=Path(tempdir), fetch_json=fake_fetch)
            second = load_or_fetch_news("AAA", "示例公司", "2026-06-03", root=Path(tempdir), fetch_json=fake_fetch)
            cached = load_cached_news("AAA", "2026-06-03", root=Path(tempdir))

        self.assertEqual(first["status"], "ok")
        self.assertEqual(len(first["articles"]), 2)
        self.assertEqual(second["articles"], first["articles"])
        self.assertEqual(cached, first)
        self.assertEqual(len(calls), 1)

    def test_failed_news_request_is_retryable_and_invalid_links_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            calls = 0

            def flaky_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("temporary failure")
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '[{"title_zh":"无效","summary_zh":"无效","source":"","url":"javascript:alert(1)"},{"title_zh":"有效","summary_zh":"公开报道。","source":"来源","url":"https://example.com/news"}]'
                            }
                        }
                    ]
                }

            first = load_or_fetch_news("AAA", "示例公司", "2026-06-03", root=Path(tempdir), fetch_json=flaky_fetch)
            second = load_or_fetch_news("AAA", "示例公司", "2026-06-03", root=Path(tempdir), fetch_json=flaky_fetch)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(len(second["articles"]), 1)
        self.assertEqual(calls, 2)

    def test_anomaly_rules_mark_turnover_rank_and_price_facts(self) -> None:
        snapshot = pd.DataFrame(
            [
                {"代码": "AAA", "成交额": 300.0, "涨跌幅": 12.0},
                {"代码": "BBB", "成交额": 250.0, "涨跌幅": 6.0},
            ]
        )
        previous = pd.DataFrame(
            [{"代码": "BBB", "成交额": 2000.0}]
            + [{"代码": f"X{index:02d}", "成交额": 1000 - index} for index in range(30)]
            + [{"代码": "AAA", "成交额": 1.0}]
        )
        history = {"AAA": pd.DataFrame({"成交额": [100.0] * 20}), "BBB": pd.DataFrame({"成交额": [100.0] * 20})}

        result = evaluate_anomalies(snapshot, history, previous)

        self.assertEqual(result.loc[result["代码"] == "AAA", "异动等级"].item(), "强异动")
        self.assertIn("成交额强放大", result.loc[result["代码"] == "AAA", "异动标签"].item())
        self.assertIn("价格强异动", result.loc[result["代码"] == "AAA", "异动标签"].item())
        self.assertEqual(result.loc[result["代码"] == "BBB", "异动等级"].item(), "关注")

    def test_anomaly_details_report_insufficient_history_without_guessing(self) -> None:
        snapshot = pd.DataFrame([{"代码": "AAA", "成交额": 300.0, "涨跌幅": 0.5}])

        result = evaluate_anomalies(snapshot, {"AAA": pd.DataFrame({"成交额": [100.0]})})
        details = serialize_anomaly_details(result.iloc[0])

        self.assertEqual(details["成交额放大倍数"], None)
        self.assertIn("历史数据不足", details["异动数据状态"])
    def test_target_date_uses_previous_weekday_before_market_complete(self) -> None:
        now = datetime(2026, 6, 3, 12, 0, tzinfo=NEW_YORK)

        self.assertEqual(resolve_us_stock_target_date(now), "2026-06-02")

    def test_tradingview_rows_use_direct_value_traded_and_chinese_names(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _write_name_map(root, {"XYZ": "映射股票"})
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                calls.append((url, payload))
                return {"data": _tradingview_rows()}

            frame, metadata = fetch_us_top_turnover(root=root, fetch_json=fake_fetch)

            self.assertEqual(calls[0][0], TRADINGVIEW_SCANNER_URL)
            self.assertEqual(calls[0][1]["sort"], {"sortBy": "Value.Traded", "sortOrder": "desc"})
            self.assertEqual(metadata["source"], "tradingview")
            self.assertEqual(len(frame), US_TOP_TURNOVER_LIMIT)
            self.assertNotIn("PREF", frame["代码"].tolist())
            self.assertTrue(frame["成交额"].is_monotonic_decreasing)
            mu = frame[frame["代码"] == "MU"].iloc[0]
            self.assertEqual(mu["名称"], "美光科技")
            self.assertEqual(mu["英文名称"], "Micron Technology, Inc.")
            self.assertEqual(float(mu["成交额"]), 9_000_000_000.0)
            self.assertNotEqual(float(mu["成交额"]), float(mu["最新价"]) * 100_000_000)
            xyz = frame[frame["代码"] == "XYZ"].iloc[0]
            self.assertEqual(xyz["名称"], "映射股票")
            self.assertIn("MISS", metadata["missing_cn_name_codes"])

    def test_name_map_cache_overrides_seed_and_seed_fills_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _write_name_map(root, {"MU": "缓存美光", "ABC": "缓存ABC"})

            mapping = load_us_name_map(root=root)

            self.assertEqual(mapping["MU"], "缓存美光")
            self.assertEqual(mapping["ABC"], "缓存ABC")
            self.assertEqual(mapping["NVDA"], "英伟达")

    def test_seed_localizes_recent_top_turnover_names(self) -> None:
        expected_names = {
            "AAOI": "应用光电",
            "AMAT": "应用材料",
            "ARM": "安谋控股",
            "ASML": "阿斯麦",
            "BRK.B": "伯克希尔哈撒韦-B",
            "CRWD": "众击",
            "INHD": "英诺控股",
            "KLAC": "科磊",
            "LITE": "鲁门特姆",
            "LLY": "礼来",
            "LRCX": "泛林集团",
            "META": "脸书母公司",
            "MSTR": "策略公司（原微策略）",
            "NBIS": "涅比乌斯集团",
            "PLTR": "帕兰蒂尔",
            "UNH": "联合健康",
            "WDC": "西部数据",
        }

        for code, expected_name in expected_names.items():
            with self.subTest(code=code):
                self.assertEqual(US_CN_NAME_SEED[code], expected_name)

    def test_seed_names_are_not_pure_english(self) -> None:
        untranslated_codes = [
            code
            for code, name in US_CN_NAME_SEED.items()
            if not any("\u3400" <= character <= "\u9fff" for character in name)
        ]

        self.assertEqual(untranslated_codes, [])

    def test_load_snapshot_applies_chinese_names_to_legacy_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _write_cached_snapshot(root, "2026-06-02")
            _write_name_map(root, {"C00": "中文缓存0"})

            snapshot = load_us_stock_snapshot("2026-06-02", root=root)

            self.assertEqual(snapshot.iloc[0]["代码"], "C00")
            self.assertEqual(snapshot.iloc[0]["名称"], "中文缓存0")
            self.assertEqual(snapshot.iloc[0]["英文名称"], "Cached 0")

    def test_refresh_writes_tradingview_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls: list[str] = []

            def fake_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                calls.append(url)
                return {"data": _tradingview_rows()}

            result = refresh_us_stock_turnover(
                root=root,
                now=datetime(2026, 6, 3, 17, 0, tzinfo=NEW_YORK),
                fetch_json=fake_fetch,
            )

            self.assertEqual(result.status, US_REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-06-03")
            self.assertEqual(result.source, "tradingview")
            self.assertEqual(calls, [TRADINGVIEW_SCANNER_URL])
            snapshot = load_us_stock_snapshot("2026-06-03", root=root)
            self.assertEqual(snapshot.columns[:8].tolist(), ["代码", "名称", "英文名称", "最新价", "涨跌幅", "涨跌额", "成交额", "市值"])
            self.assertEqual(len(snapshot), US_TOP_TURNOVER_LIMIT)
            metadata = load_us_stock_metadata("2026-06-03", root=root)
            self.assertEqual(metadata["source"], "tradingview")
            self.assertIn("MISS", metadata["missing_cn_name_codes"])
            self.assertEqual(read_us_stock_refresh_status(root)["status"], US_REFRESH_SUCCEEDED)

    def test_tradingview_failure_falls_back_to_nasdaq_download(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls: list[str] = []

            def fake_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                calls.append(url)
                if url == TRADINGVIEW_SCANNER_URL:
                    raise RuntimeError("tradingview down")
                if url == NASDAQ_SCREENER_URL:
                    return {"data": {"rows": _nasdaq_rows()}}
                raise AssertionError(f"unexpected URL {url}")

            frame, metadata = fetch_us_top_turnover(root=root, fetch_json=fake_fetch)

            self.assertEqual(calls, [TRADINGVIEW_SCANNER_URL, NASDAQ_SCREENER_URL])
            self.assertEqual(metadata["source"], "nasdaq")
            self.assertEqual(metadata["provider"], "nasdaq_screener_download")
            self.assertEqual(metadata["turnover_method"], "lastsale_x_volume")
            self.assertIn("tradingview: tradingview down", metadata["fallback_errors"])
            self.assertEqual(len(frame), US_TOP_TURNOVER_LIMIT)
            self.assertNotIn("WARR", frame["代码"].tolist())
            mu = frame[frame["代码"] == "MU"].iloc[0]
            self.assertEqual(mu["名称"], "美光科技")
            self.assertEqual(float(mu["成交额"]), 10.0 * 900_000_000.0)

    def test_existing_target_cache_skips_fetch_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _write_cached_snapshot(root, "2026-06-03")

            def fail_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                raise AssertionError("fetch should not be called")

            result = refresh_us_stock_turnover(
                root=root,
                now=datetime(2026, 6, 3, 17, 0, tzinfo=NEW_YORK),
                fetch_json=fail_fetch,
            )

            self.assertEqual(result.status, US_REFRESH_SKIPPED)
            self.assertEqual(result.actual_date, "2026-06-03")

    def test_tradingview_failure_preserves_existing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _write_cached_snapshot(root, "2026-06-02")
            calls: list[str] = []

            def failing_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
                calls.append(url)
                raise RuntimeError("tradingview down")

            result = refresh_us_stock_turnover(
                root=root,
                now=datetime(2026, 6, 3, 17, 0, tzinfo=NEW_YORK),
                fetch_json=failing_fetch,
            )

            self.assertEqual(result.status, US_REFRESH_FAILED)
            self.assertEqual(result.actual_date, "2026-06-02")
            self.assertEqual(latest_us_stock_cache_date(root), "2026-06-02")
            self.assertEqual(calls, [TRADINGVIEW_SCANNER_URL, NASDAQ_SCREENER_URL])
            self.assertIn("继续展示 2026-06-02 缓存", result.message or "")

    def test_missing_direct_turnover_does_not_generate_wrong_ranking(self) -> None:
        def fake_fetch(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
            rows = [_tv_row(f"BAD{index}", f"Bad {index}", ["common"], 100.0, 1.0, 1.0, None, 1000.0) for index in range(25)]
            return {"data": rows}

        with self.assertRaises(RuntimeError) as context:
            fetch_us_top_turnover(fetch_json=fake_fetch)

        self.assertIn("tradingview: No direct US stock turnover rows", str(context.exception))
        self.assertIn("nasdaq: No Nasdaq computed US stock turnover rows", str(context.exception))

    def test_default_tradingview_fetch_retries_transient_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls = 0

            def fail_then_succeed(*args: object, **kwargs: object) -> MagicMock:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ConnectionResetError("connection reset by peer")
                return _mock_urlopen_json({"data": _tradingview_rows()})

            with (
                patch("trend_radar.us_stocks.urllib.request.urlopen", side_effect=fail_then_succeed),
                patch("trend_radar.us_stocks.sleep") as sleep_mock,
            ):
                frame, metadata = fetch_us_top_turnover(root=root)

            self.assertEqual(calls, 2)
            sleep_mock.assert_called_once()
            self.assertEqual(len(frame), US_TOP_TURNOVER_LIMIT)
            self.assertEqual(metadata["source"], "tradingview")


def _tradingview_rows() -> list[dict[str, object]]:
    rows = [
        _tv_row("PREF", "Preferred Example", ["preferred"], 900.0, 1.0, 1.0, 99_000_000_000.0, 0.0),
        _tv_row("MU", "Micron Technology, Inc.", ["common"], 10.0, 2.76, 0.27, 9_000_000_000.0, 1_200_000_000_000.0),
        _tv_row("XYZ", "Mapped Stock Inc.", ["common"], 50.0, 1.0, 0.5, 8_500_000_000.0, 100_000_000_000.0),
        _tv_row("MISS", "Missing Chinese Name Inc.", ["common"], 40.0, -1.0, -0.4, 8_000_000_000.0, 90_000_000_000.0),
    ]
    for index in range(35):
        rows.append(
            _tv_row(
                f"STK{index:02d}",
                f"Stock {index:02d}",
                ["common"],
                100.0 + index,
                index / 10,
                index / 20,
                float((index + 1) * 100_000_000),
                float((index + 1) * 1_000_000_000),
            )
        )
    return rows


def _nasdaq_rows() -> list[dict[str, object]]:
    rows = [
        _nasdaq_row("WARR", "Warrant Example Warrants", "$50.00", "1.0", "1.0%", "999999999", "1000000000"),
        _nasdaq_row("MU", "Micron Technology Inc. Common Stock", "$10.00", "0.27", "2.76%", "900000000", "1200000000000"),
        _nasdaq_row("XYZ", "Mapped Stock Inc. Common Stock", "$50.00", "0.50", "1.0%", "170000000", "100000000000"),
    ]
    for index in range(35):
        rows.append(
            _nasdaq_row(
                f"STK{index:02d}",
                f"Stock {index:02d} Common Stock",
                f"${100 + index}.00",
                f"{index / 20:.2f}",
                f"{index / 10:.2f}%",
                str((index + 1) * 1_000_000),
                str((index + 1) * 1_000_000_000),
            )
        )
    return rows


def _nasdaq_row(
    symbol: str,
    name: str,
    last_sale: str,
    net_change: str,
    pct_change: str,
    volume: str,
    market_cap: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": name,
        "lastsale": last_sale,
        "netchange": net_change,
        "pctchange": pct_change,
        "volume": volume,
        "marketCap": market_cap,
    }


def _tv_row(
    symbol: str,
    name: str,
    specs: list[str],
    close: float,
    change: float,
    change_abs: float,
    value_traded: float | None,
    market_cap: float,
) -> dict[str, object]:
    return {
        "s": f"NASDAQ:{symbol}",
        "d": [
            symbol,
            name,
            "stock",
            specs,
            "NASDAQ",
            close,
            change,
            change_abs,
            value_traded,
            market_cap,
            "USD",
            "United States",
        ],
    }


def _mock_urlopen_json(payload: dict[str, object]) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload).encode("utf-8")
    return response


def _write_name_map(root: Path, values: dict[str, str]) -> None:
    path = root / "data" / "us_stocks" / "name_map.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"代码": code, "中文名称": name, "英文名称": f"{code} English", "updated_at": "2026-06-03", "source": "test"}
            for code, name in values.items()
        ]
    ).to_csv(path, index=False)


def _write_cached_snapshot(root: Path, data_date: str) -> None:
    cache_dir = root / "data" / "us_stocks" / "top_turnover"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "代码": f"C{i:02d}",
                "名称": f"缓存{i}",
                "英文名称": f"Cached {i}",
                "最新价": 100.0 + i,
                "涨跌幅": 1.0,
                "涨跌额": 1.0,
                "成交额": (US_TOP_TURNOVER_LIMIT - i) * 100_000_000.0,
                "市值": 1000000000.0 + i,
            }
            for i in range(US_TOP_TURNOVER_LIMIT)
        ]
    ).to_csv(cache_dir / f"{data_date}.csv", index=False)
    (cache_dir / f"{data_date}.json").write_text(
        json.dumps({"source": "tradingview", "generated_at": "2026-06-03 08:00:00"}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
