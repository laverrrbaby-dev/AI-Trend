from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.audit import audit_outputs  # noqa: E402
from trend_radar.scoring import generate_scores  # noqa: E402


class AuditTests(unittest.TestCase):
    def test_audit_detects_missing_history_wrong_top30_and_broken_json(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_source_fixture(root, data_date)
            generate_scores(data_date, root=root)

            (root / "data" / "normalized" / data_date / "history" / "512880.csv").unlink()
            radar_path = root / "data" / "radar" / f"{data_date}.csv"
            radar = pd.read_csv(radar_path, dtype={"代码": str})
            radar.loc[0, "成交额Top30次数"] = 0
            radar.to_csv(radar_path, index=False)
            json_path = root / "data" / "radar" / f"{data_date}.json"
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            payload["top10"] = []
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = audit_outputs(data_date, data_date, root=root)

            self.assertFalse(result.ok)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertTrue(any("missing/invalid history" in item for item in result.warnings))
            self.assertTrue(any("JSON top10" in item for item in result.errors))
            self.assertTrue(report["top30_diffs"])

    def test_audit_repair_backs_up_and_regenerates_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_source_fixture(root, data_date)
            generate_scores(data_date, root=root)
            (root / "data" / "normalized" / data_date / "history" / "512880.csv").unlink()

            def fake_fetcher(data_date: str, root: Path, resume: bool, **kwargs: object) -> object:
                self.assertFalse(resume)
                normalized = root / "data" / "normalized" / data_date
                raw = root / "data" / "raw" / data_date
                if normalized.exists():
                    shutil.rmtree(normalized)
                if raw.exists():
                    shutil.rmtree(raw)
                self._write_source_fixture(root, data_date)
                return SimpleNamespace(errors=[])

            result = audit_outputs(data_date, data_date, root=root, repair=True, fetcher=fake_fetcher)

            self.assertTrue(result.ok, result.errors)
            self.assertTrue(result.repaired)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            backup_dir = Path(report["repairs"]["backup_dir"])
            self.assertTrue(backup_dir.exists())
            self.assertEqual(report["repairs"]["regenerated_dates"], [data_date])
            self.assertTrue((root / "data" / "normalized" / data_date / "history" / "512880.csv").exists())

    def test_external_audit_accepts_matching_mock_provider_and_reports_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_source_fixture(root, data_date)
            generate_scores(data_date, root=root)

            def matching_fetcher(code: str, start_ymd: str, end_ymd: str) -> tuple[pd.DataFrame, str]:
                return self._raw_history(data_date, code, self._amount_for_code(code)), "mock"

            result = audit_outputs(data_date, data_date, root=root, external=True, external_fetcher=matching_fetcher)
            self.assertTrue(result.ok, result.errors)

            def changed_fetcher(code: str, start_ymd: str, end_ymd: str) -> tuple[pd.DataFrame, str]:
                amount = self._amount_for_code(code) * (2 if code == "159995" else 1)
                return self._raw_history(data_date, code, amount), "mock"

            changed = audit_outputs(data_date, data_date, root=root, external=True, external_fetcher=changed_fetcher)
            self.assertFalse(changed.ok)
            report = json.loads(changed.report_path.read_text(encoding="utf-8"))
            self.assertTrue(any(item["code"] == "159995" for item in report["external_diffs"]))

    def test_external_audit_reports_unavailable_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_source_fixture(root, data_date)
            generate_scores(data_date, root=root)

            def failing_fetcher(code: str, start_ymd: str, end_ymd: str) -> tuple[pd.DataFrame, str]:
                raise RuntimeError("offline")

            result = audit_outputs(data_date, data_date, root=root, external=True, external_fetcher=failing_fetcher)

            self.assertFalse(result.ok)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["external_diffs"])
            self.assertTrue(all(item["problem"] == "offline" for item in report["external_diffs"]))

    def test_audit_detects_spot_history_amount_unit_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_source_fixture(root, data_date)
            generate_scores(data_date, root=root)
            spot_path = root / "data" / "normalized" / data_date / "spot_candidates.csv"
            spot = pd.read_csv(spot_path, dtype={"code": str})
            spot.loc[spot["code"].astype(str).str.zfill(6) == "159995", "amount_yi"] = 500.0
            spot.to_csv(spot_path, index=False)

            result = audit_outputs(data_date, data_date, root=root)

            self.assertFalse(result.ok)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            diff = next(item for item in report["spot_history_diffs"] if item["code"] == "159995")
            self.assertEqual(diff["problem"], "suspected_100x_amount_unit_mismatch")
            self.assertTrue(diff["unit_mismatch_suspected"])

    def test_external_audit_marks_100x_unit_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_source_fixture(root, data_date)
            generate_scores(data_date, root=root)

            def changed_fetcher(code: str, start_ymd: str, end_ymd: str) -> tuple[pd.DataFrame, str]:
                amount = self._amount_for_code(code) * (100 if code == "159995" else 1)
                return self._raw_history(data_date, code, amount), "mock_sina"

            result = audit_outputs(data_date, data_date, root=root, external=True, external_fetcher=changed_fetcher)

            self.assertFalse(result.ok)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            diff = next(item for item in report["external_diffs"] if item["code"] == "159995")
            self.assertEqual(diff["problem"], "suspected_100x_amount_unit_mismatch")
            self.assertTrue(diff["unit_mismatch_suspected"])

    def _write_source_fixture(self, root: Path, data_date: str) -> None:
        normalized = root / "data" / "normalized" / data_date
        history_dir = normalized / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        codes = ["159995", "510300", "512880"]
        names = ["芯片ETF", "沪深300ETF", "证券ETF"]
        amounts = [5.0, 4.0, 3.0]
        pd.DataFrame(
            {
                "code": codes,
                "name": names,
                "latest_price": [1.5, 4.0, 1.1],
                "pct_change": [1.2, 0.5, -0.2],
                "amount_yi": amounts,
                "volume": [1, 1, 1],
                "data_date": [data_date] * 3,
                "update_time": ["15:00:00"] * 3,
                "latest_share_yi": [40.0, 30.0, 20.0],
                "total_market_cap_yi": [60.0, 120.0, 22.0],
                "float_market_cap_yi": [60.0, 120.0, 22.0],
                "is_equity_etf": [True, True, True],
                "universe_reason": ["fixture"] * 3,
            }
        ).to_csv(normalized / "spot_candidates.csv", index=False)

        dates = pd.bdate_range(end=pd.Timestamp(data_date), periods=130)
        for index, (code, amount) in enumerate(zip(codes, amounts, strict=True), start=1):
            close = pd.Series(range(130), dtype=float) * (0.002 * index) + (1.0 + index * 0.2)
            pd.DataFrame(
                {
                    "code": code,
                    "日期": dates,
                    "开盘价": close - 0.001,
                    "最高价": close + 0.004,
                    "最低价": close - 0.004,
                    "收盘价": close,
                    "成交额(亿)": [amount] * 130,
                    "成交量": [10000 * index] * 130,
                    "涨跌幅": [0.1] * 130,
                }
            ).to_csv(history_dir / f"{code}.csv", index=False)

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

    def _raw_history(self, data_date: str, code: str, amount_yi: float) -> pd.DataFrame:
        dates = pd.bdate_range(end=pd.Timestamp(data_date), periods=130)
        index = ["159995", "510300", "512880"].index(code) + 1
        close = pd.Series(range(130), dtype=float) * (0.002 * index) + (1.0 + index * 0.2)
        return pd.DataFrame(
            {
                "日期": dates.strftime("%Y-%m-%d"),
                "开盘": close - 0.001,
                "最高": close + 0.004,
                "最低": close - 0.004,
                "收盘": close,
                "成交额": [amount_yi * 100_000_000] * 130,
                "涨跌幅": [0.1] * 130,
            }
        )

    def _amount_for_code(self, code: str) -> float:
        return {"159995": 5.0, "510300": 4.0, "512880": 3.0}[code]


if __name__ == "__main__":
    unittest.main()
