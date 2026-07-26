from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from helpers.data_factory import DEFAULT_AMOUNTS, raw_history_frame, write_source_fixture  # noqa: E402
from trend_radar.fetcher import _merge_share_sources, fetch_data  # noqa: E402
from trend_radar.scoring import generate_scores  # noqa: E402


class DataPipelineContractsTests(unittest.TestCase):
    def test_cached_candidates_are_revalidated_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            normalized = root / "data" / "normalized" / data_date
            history_dir = normalized / "history"
            history_dir.mkdir(parents=True)

            spot = pd.DataFrame(
                {
                    "code": ["159995", "511990", "560580"],
                    "name": ["芯片ETF", "华宝添益ETF", "低流动ETF"],
                    "latest_price": [1.5, 1.0, 1.0],
                    "amount_yi": [5.0, 100.0, 0.01],
                    "latest_share_yi": [40.0, 1000.0, 5.0],
                    "total_market_cap_yi": [60.0, 1000.0, 10.0],
                    "float_market_cap_yi": [60.0, 1000.0, 10.0],
                }
            )
            spot.to_csv(normalized / "spot_all.csv", index=False)
            polluted = spot.copy()
            polluted["is_equity_etf"] = True
            polluted["universe_reason"] = "legacy_cache"
            polluted.to_csv(normalized / "spot_candidates.csv", index=False)

            pd.DataFrame(
                {
                    "code": ["159995"],
                    "日期": [data_date],
                    "收盘价": [1.0],
                    "成交额(亿)": [5.0],
                }
            ).to_csv(history_dir / "159995.csv", index=False)
            pd.DataFrame(
                {
                    "code": ["159995"],
                    "name": ["159995"],
                    "日期": [data_date],
                    "ETF份额(亿份)": [40.0],
                    "share_source": ["fixture"],
                }
            ).to_csv(normalized / "shares.csv", index=False)

            with patch("trend_radar.fetcher._load_akshare", return_value=object()):
                result = fetch_data(data_date=data_date, root=root, resume=True)

            self.assertEqual(result.candidate_rows, 1)
            cleaned = pd.read_csv(normalized / "spot_candidates.csv", dtype={"code": str})
            self.assertEqual(cleaned["code"].astype(str).str.zfill(6).tolist(), ["159995"])
            self.assertEqual(cleaned["universe_reason"].tolist(), ["a_share_equity_candidate"])

    def test_share_merge_prefers_official_same_day_source_over_spot_fallback(self) -> None:
        data_date = "2026-05-29"
        official = pd.DataFrame(
            {
                "code": ["159995"],
                "name": ["芯片ETF"],
                "日期": [pd.Timestamp(data_date)],
                "ETF份额(亿份)": [42.0],
                "share_source": ["sse"],
            }
        )
        candidates = pd.DataFrame(
            {
                "code": ["159995", "510300"],
                "name": ["芯片ETF", "沪深300ETF"],
                "latest_share_yi": [99.0, 30.0],
            }
        )

        merged = _merge_share_sources([official], candidates, data_date)
        by_code = dict(zip(merged["code"], merged["ETF份额(亿份)"], strict=True))
        source_by_code = dict(zip(merged["code"], merged["share_source"], strict=True))

        self.assertEqual(by_code["159995"], 42.0)
        self.assertEqual(source_by_code["159995"], "sse")
        self.assertEqual(by_code["510300"], 30.0)
        self.assertEqual(source_by_code["510300"], "spot_fallback")

    def test_scoring_ignores_future_history_and_share_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            write_source_fixture(root, data_date=data_date, future_rows=5)

            result = generate_scores(data_date, root=root, cross_validate_top_pool=False)

            self.assertEqual(result.rows, 5)
            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})
            row = radar[radar["代码"].astype(str).str.zfill(6) == "159995"].iloc[0]
            self.assertLess(float(row["成交额(亿)"]), 100.0)

            detail_path = root / "data" / "normalized" / data_date / "radar_history" / "159995.csv"
            detail = pd.read_csv(detail_path)
            latest_detail_date = pd.to_datetime(detail["日期"]).max().date().isoformat()
            self.assertEqual(latest_detail_date, data_date)
            self.assertLess(float(detail["ETF份额(亿份)"].dropna().iloc[-1]), 1000.0)

            payload = json.loads(result.radar_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["data_quality"]["status"], "OK")

    def test_spot_fallback_history_is_low_confidence_and_explained(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            write_source_fixture(root, data_date=data_date, history_provider="spot_fallback")

            result = generate_scores(data_date, root=root, cross_validate_top_pool=False)
            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})

            self.assertEqual(set(radar["数据置信度"]), {"低"})
            self.assertNotIn("完整", set(radar["数据质量"]))
            self.assertTrue(radar["数据问题"].str.contains("历史行情补点来自实时行情快照").all())
            payload = json.loads(result.radar_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["data_quality"]["low_confidence_rows"], len(radar))
            self.assertGreater(payload["data_quality"]["top10_low_confidence_rows"], 0)

    def test_trusted_history_source_stays_high_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            write_source_fixture(root, data_date=data_date, history_provider="fixture")

            result = generate_scores(data_date, root=root)
            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})

            self.assertEqual(set(radar["数据置信度"]), {"高"})
            self.assertEqual(set(radar["数据质量"]), {"完整"})
            self.assertEqual(set(radar["数据问题"]), {"无"})

    def test_sina_history_source_is_medium_confidence_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            write_source_fixture(root, data_date=data_date, history_provider="sina")

            result = generate_scores(data_date, root=root, cross_validate_top_pool=False)
            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})

            self.assertEqual(set(radar["数据置信度"]), {"中"})
            self.assertEqual(set(radar["行情来源"]), {"新浪兜底行情"})
            self.assertNotIn("完整", set(radar["数据质量"]))
            self.assertTrue(radar["数据问题"].str.contains("东方财富历史行情暂不可用，使用新浪兜底").all())

    def test_top_pool_cross_validation_replaces_sina_with_eastmoney_when_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            fixture = write_source_fixture(root, data_date=data_date, history_provider="sina")

            def fake_eastmoney(code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
                return raw_history_frame(data_date, code, DEFAULT_AMOUNTS.get(code, 5.0)), "eastmoney_urllib"

            with patch("trend_radar.fetcher._fetch_history_eastmoney_with_provider", side_effect=fake_eastmoney):
                result = generate_scores(data_date, root=root)

            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})
            top_codes = radar.head(10)["代码"].astype(str).str.zfill(6).tolist()
            checked = radar[radar["代码"].astype(str).str.zfill(6).isin(top_codes)]
            self.assertTrue((checked["行情来源"] == "东方财富历史行情").all())
            self.assertTrue((checked["数据置信度"] == "高").all())
            history = pd.read_csv(fixture.history_dir / f"{top_codes[0]}.csv")
            self.assertEqual(set(history["history_source"]), {"eastmoney_urllib"})

    def test_top_pool_cross_validation_marks_primary_mismatch_low_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            write_source_fixture(root, data_date=data_date, history_provider="sina")

            def fake_eastmoney(code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
                frame = raw_history_frame(data_date, code, DEFAULT_AMOUNTS.get(code, 5.0)).copy()
                frame["收盘"] = pd.to_numeric(frame["收盘"], errors="coerce") * 1.1
                return frame, "eastmoney_urllib"

            with patch("trend_radar.fetcher._fetch_history_eastmoney_with_provider", side_effect=fake_eastmoney):
                result = generate_scores(data_date, root=root)

            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})
            top = radar.head(10)
            self.assertTrue((top["数据置信度"] == "低").all())
            self.assertTrue(top["数据问题"].str.contains("主源与兜底源关键字段不一致").all())

    def test_spot_fallback_share_source_is_not_marked_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            write_source_fixture(root, data_date=data_date, share_source="spot_fallback")

            result = generate_scores(data_date, root=root)
            radar = pd.read_csv(result.radar_csv, dtype={"代码": str})

            self.assertEqual(set(radar["数据置信度"]), {"中"})
            self.assertEqual(set(radar["份额来源"]), {"实时行情份额"})
            self.assertNotIn("完整", set(radar["数据质量"]))
            self.assertTrue(radar["数据问题"].str.contains("官方份额接口缺失").all())


if __name__ == "__main__":
    unittest.main()
