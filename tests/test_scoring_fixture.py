from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.backfill import backfill_scores
from trend_radar.scoring import _structure_score, generate_scores
from trend_radar.validation import validate_outputs
from trend_radar.ui_data import build_changes, build_price_history, get_available_dates, load_snapshot


class ScoringFixtureTests(unittest.TestCase):
    def test_generate_scores_produces_ui_compatible_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-28"
            normalized = root / "data" / "normalized" / data_date
            history_dir = normalized / "history"
            history_dir.mkdir(parents=True)

            spot = pd.DataFrame(
                {
                    "code": ["159995", "510300", "512880"],
                    "name": ["芯片ETF", "沪深300ETF", "证券ETF"],
                    "latest_price": [1.5, 4.0, 1.1],
                    "pct_change": [1.2, 0.5, -0.2],
                    "amount_yi": [5.0, 4.0, 3.0],
                    "volume": [1, 1, 1],
                    "data_date": [data_date] * 3,
                    "update_time": ["15:00:00"] * 3,
                    "latest_share_yi": [40.0, 30.0, 20.0],
                    "total_market_cap_yi": [60.0, 120.0, 22.0],
                    "float_market_cap_yi": [60.0, 120.0, 22.0],
                    "is_equity_etf": [True, True, True],
                    "universe_reason": ["fixture"] * 3,
                }
            )
            spot.to_csv(normalized / "spot_candidates.csv", index=False)

            dates = pd.bdate_range(end=pd.Timestamp(data_date), periods=140)
            for index, code in enumerate(["159995", "510300", "512880"], start=1):
                close = pd.Series(range(140), dtype=float) * (0.002 * index) + (1.0 + index * 0.2)
                history = pd.DataFrame(
                    {
                        "code": code,
                        "日期": dates,
                        "开盘价": close - 0.001,
                        "最高价": close + 0.004,
                        "最低价": close - 0.004,
                        "收盘价": close,
                        "成交额(亿)": [5.0 - index * 0.5] * 140,
                        "成交量": [10000 * index] * 140,
                        "涨跌幅": [0.1] * 140,
                    }
                )
                history.to_csv(history_dir / f"{code}.csv", index=False)

            share_rows = []
            for code, base in [("159995", 40), ("510300", 30), ("512880", 20)]:
                for offset, current in enumerate(dates[-30:]):
                    share_rows.append(
                        {
                            "code": code,
                            "name": code,
                            "日期": current,
                            "ETF份额(亿份)": base + offset * 0.05,
                            "share_source": "fixture",
                        }
                    )
            pd.DataFrame(share_rows).to_csv(normalized / "shares.csv", index=False)

            result = generate_scores(data_date, root=root)
            self.assertEqual(result.rows, 3)
            self.assertTrue(result.radar_csv.exists())
            self.assertTrue(result.radar_json.exists())

            snapshot = load_snapshot(data_date, root=root)
            self.assertIn("主线得分", snapshot.columns)
            self.assertIn("成交额Top30次数", snapshot.columns)
            self.assertEqual(get_available_dates(root=root), [data_date])
            self.assertEqual(set(build_changes(data_date, root=root).keys()), {"新进Top10", "连续Top10", "跌出Top10"})

            history = build_price_history(snapshot.iloc[0], data_date, root=root)
            self.assertIn("EMA169", history.columns)
            self.assertIn("开盘价", history.columns)
            self.assertIn("ETF份额(亿份)", history.columns)
            self.assertIn("成交量", history.columns)

            payload = json.loads(result.radar_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_mode"], "real")
            self.assertEqual(payload["data_quality"]["included_rows"], 3)

    def test_backfill_generates_multiple_dates_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source_date = "2026-05-28"
            self._write_source_fixture(root, source_date)

            result = backfill_scores(months=1, root=root, source_date=source_date)
            self.assertEqual(len(result.dates), 20)
            self.assertEqual(result.dates[-1], source_date)

            dates = get_available_dates(root=root)
            self.assertEqual(dates, result.dates)
            latest = load_snapshot(source_date, root=root)
            self.assertEqual(latest["代码"].nunique(), len(latest))
            self.assertNotIn("511990", latest["代码"].tolist())
            self.assertEqual(int(latest[latest["代码"] == "159995"]["成交额排名"].iloc[0]), 1)

            history = build_price_history(latest.iloc[0], source_date, root=root)
            self.assertFalse(history.empty)
            self.assertIn("EMA169", history.columns)
            self.assertIn("最高价", history.columns)

            validation = validate_outputs(start=result.dates[0], end=result.dates[-1], root=root)
            self.assertTrue(validation.ok, validation.errors)
            self.assertTrue(validation.report_path.exists())

    def test_validation_reports_broken_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source_date = "2026-05-28"
            self._write_source_fixture(root, source_date)
            generate_scores(source_date, root=root)

            detail_path = root / "data" / "normalized" / source_date / "radar_history" / "159995.csv"
            detail_path.unlink()
            validation = validate_outputs(start=source_date, end=source_date, root=root)
            self.assertFalse(validation.ok)
            self.assertTrue(any("missing Top10 detail history" in item for item in validation.errors))

    def _write_source_fixture(self, root: Path, source_date: str) -> None:
        normalized = root / "data" / "normalized" / source_date
        history_dir = normalized / "history"
        history_dir.mkdir(parents=True)

        spot = pd.DataFrame(
            {
                "code": ["159995", "510300", "512880", "511990"],
                "name": ["芯片ETF", "沪深300ETF", "证券ETF", "华宝添益ETF"],
                "latest_price": [1.5, 4.0, 1.1, 1.0],
                "pct_change": [1.2, 0.5, -0.2, 0.0],
                "amount_yi": [5.0, 4.0, 3.0, 100.0],
                "volume": [1, 1, 1, 1],
                "data_date": [source_date] * 4,
                "update_time": ["15:00:00"] * 4,
                "latest_share_yi": [40.0, 30.0, 20.0, 1000.0],
                "total_market_cap_yi": [60.0, 120.0, 22.0, 1000.0],
                "float_market_cap_yi": [60.0, 120.0, 22.0, 1000.0],
                "is_equity_etf": [True, True, True, False],
                "universe_reason": ["fixture"] * 4,
            }
        )
        spot.to_csv(normalized / "spot_candidates.csv", index=False)

        dates = pd.bdate_range(end=pd.Timestamp(source_date), periods=150)
        amounts = {"159995": 5.0, "510300": 4.0, "512880": 3.0, "511990": 100.0}
        for index, code in enumerate(["159995", "510300", "512880", "511990"], start=1):
            close = pd.Series(range(150), dtype=float) * (0.002 * index) + (1.0 + index * 0.2)
            history = pd.DataFrame(
                {
                    "code": code,
                    "日期": dates,
                    "开盘价": close - 0.001,
                    "最高价": close + 0.004,
                    "最低价": close - 0.004,
                    "收盘价": close,
                    "成交额(亿)": [amounts[code]] * 150,
                    "成交量": [10000 * index] * 150,
                    "涨跌幅": [0.1] * 150,
                }
            )
            history.to_csv(history_dir / f"{code}.csv", index=False)

        share_rows = []
        for code, base in [("159995", 40), ("510300", 30), ("512880", 20), ("511990", 1000)]:
            for offset, current in enumerate(dates[-40:]):
                share_rows.append(
                    {
                        "code": code,
                        "name": code,
                        "日期": current,
                        "ETF份额(亿份)": base + offset * 0.05,
                        "share_source": "fixture",
                    }
                )
        pd.DataFrame(share_rows).to_csv(normalized / "shares.csv", index=False)

    def test_structure_score_uses_ema_rules(self) -> None:
        cases = [
            (pd.Series({"收盘价": 11, "EMA13": 10, "EMA26": 9, "EMA144": 8, "EMA169": 7}), 100.0),
            (pd.Series({"收盘价": 11, "EMA13": 10, "EMA26": 9, "EMA144": 9.5, "EMA169": 9.2}), 75.0),
            (pd.Series({"收盘价": 11, "EMA13": 10, "EMA26": 10.5, "EMA144": 9.5, "EMA169": 9.2}), 60.0),
            (pd.Series({"收盘价": 9, "EMA13": 9.7, "EMA26": 9.5, "EMA144": 9.6, "EMA169": 9.4}), 35.0),
            (pd.Series({"收盘价": 10, "EMA13": 10.2, "EMA26": 9.8, "EMA144": 10.1, "EMA169": 9.9}), 50.0),
        ]

        for latest, expected_score in cases:
            with self.subTest(expected_score=expected_score):
                actual_score, _ = _structure_score(latest)
                self.assertEqual(actual_score, expected_score)


if __name__ == "__main__":
    unittest.main()
