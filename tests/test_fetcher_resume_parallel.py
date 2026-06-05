from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.fetcher import fetch_data  # noqa: E402


class _FakeAk:
    def __init__(
        self,
        histories: dict[str, pd.DataFrame],
        failures: set[str] | None = None,
        transient_failures: dict[str, int] | None = None,
        spot_date: str | None = None,
        spot_amounts: dict[str, float] | None = None,
    ) -> None:
        self.histories = histories
        self.failures = failures or set()
        self.transient_failures = transient_failures or {}
        self.spot_date = spot_date
        self.spot_amounts = spot_amounts or {}
        self.history_calls: list[str] = []

    def fund_etf_spot_em(self) -> pd.DataFrame:
        codes = sorted(self.histories)
        frame = pd.DataFrame(
            {
                "代码": codes,
                "名称": [f"测试ETF{code}" for code in codes],
                "最新价": [1.2] * len(codes),
                "成交额": [self.spot_amounts.get(code, 500_000_000) for code in codes],
                "最新份额": [100_000_000] * len(codes),
                "总市值": [500_000_000] * len(codes),
            }
        )
        if self.spot_date is not None:
            frame["数据日期"] = self.spot_date
        return frame

    def fund_etf_hist_em(self, symbol: str, **kwargs: object) -> pd.DataFrame:
        self.history_calls.append(symbol)
        if self.transient_failures.get(symbol, 0) > 0:
            self.transient_failures[symbol] -= 1
            raise RuntimeError("temporary history down")
        if symbol in self.failures:
            raise RuntimeError("history down")
        return self.histories[symbol]

    def fund_etf_scale_sse(self, date: str) -> pd.DataFrame:
        return pd.DataFrame()

    def fund_scale_daily_szse(self, **kwargs: object) -> pd.DataFrame:
        return pd.DataFrame()


class FetcherResumeParallelTests(unittest.TestCase):
    def test_resume_true_reuses_valid_normalized_history_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_normalized_history(root, data_date, "159995")
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)}, failures={"159995"})

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 1)
            self.assertEqual(fake_ak.history_calls, [])
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 1)
            self.assertEqual(report["history_success"], 1)
            self.assertEqual(report["history_failed"], 0)
            self.assertEqual(report["skipped_cached"], 1)
            self.assertEqual(report["fetched_history_files"], 0)

    def test_resume_true_refetches_stale_normalized_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            self._write_normalized_history(root, data_date, "159995", latest_date="2026-05-29")
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)})

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=1)

            self.assertEqual(result.candidate_rows, 1)
            self.assertEqual(fake_ak.history_calls, ["159995"])
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 1)
            self.assertEqual(report["history_success"], 1)
            self.assertEqual(report["history_failed"], 0)
            self.assertEqual(report["skipped_cached"], 0)
            self.assertEqual(report["fetched_history_files"], 1)
            self.assertEqual(report["history_latest_date_counts"], {"2026-06-01": 1})

    def test_resume_true_patches_stale_history_from_current_spot_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            self._write_normalized_spot(root, data_date, "159995")
            self._write_raw_spot(root, data_date, "159995")
            self._write_normalized_history(root, data_date, "159995", latest_date="2026-05-29")
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)}, failures={"159995"})

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=1)

            self.assertEqual(result.errors, [])
            self.assertEqual(fake_ak.history_calls, [])
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_provider_counts"], {"spot_fallback": 1})
            self.assertEqual(report["history_latest_date_counts"], {"2026-06-01": 1})
            history = pd.read_csv(root / "data" / "normalized" / data_date / "history" / "159995.csv")
            self.assertEqual(history["日期"].tolist(), ["2026-05-29", "2026-06-01"])

    def test_resume_true_normalizes_cached_raw_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            raw_dir = root / "data" / "raw" / data_date / "history"
            raw_dir.mkdir(parents=True)
            self._raw_history(data_date).to_csv(raw_dir / "159995.csv", index=False)
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)}, failures={"159995"})

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 1)
            self.assertEqual(fake_ak.history_calls, [])
            self.assertTrue((root / "data" / "normalized" / data_date / "history" / "159995.csv").exists())

    def test_resume_false_clears_old_files_and_fetches_again(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            old_file = root / "data" / "normalized" / data_date / "history" / "old.csv"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("old", encoding="utf-8")
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)})

            result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=False, max_workers=2)

            self.assertEqual(result.candidate_rows, 1)
            self.assertFalse(old_file.exists())
            self.assertEqual(fake_ak.history_calls, ["159995"])

    def test_parallel_fetch_records_successes_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            histories = {
                "159995": self._raw_history(data_date),
                "510300": self._raw_history(data_date),
                "512880": self._raw_history(data_date),
            }
            fake_ak = _FakeAk(histories, failures={"512880"})
            progress: list[dict[str, object]] = []

            result = self._fetch(
                fake_ak,
                data_date,
                root=root,
                codes=["159995", "510300", "512880"],
                resume=True,
                max_workers=3,
                progress_callback=progress.append,
            )

            self.assertEqual(result.candidate_rows, 3)
            self.assertEqual(set(fake_ak.history_calls), {"159995", "510300", "512880"})
            self.assertTrue((root / "data" / "normalized" / data_date / "history" / "159995.csv").exists())
            self.assertTrue((root / "data" / "normalized" / data_date / "history" / "510300.csv").exists())
            self.assertFalse((root / "data" / "normalized" / data_date / "history" / "512880.csv").exists())
            self.assertTrue(any("512880" in item for item in result.errors))
            final_history_progress = [item for item in progress if item["phase"] == "fetch_history"][-1]
            self.assertEqual(final_history_progress["completed"], 3)
            self.assertEqual(final_history_progress["fetched"], 2)
            self.assertEqual(final_history_progress["errors"], 1)
            self.assertEqual(final_history_progress["max_workers"], 3)
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 3)
            self.assertEqual(report["history_success"], 2)
            self.assertEqual(report["history_failed"], 1)
            self.assertEqual(report["failed_history"][0]["code"], "512880")
            self.assertEqual(report["failed_history"][0]["name"], "测试ETF512880")
            self.assertIn("history down", report["failed_history"][0]["error"])

    def test_invalid_cached_history_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            path = root / "data" / "normalized" / data_date / "history" / "159995.csv"
            path.parent.mkdir(parents=True)
            path.write_text("日期,收盘价\n2026-05-29,1.0\n", encoding="utf-8")
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)})

            self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=1)

            self.assertEqual(fake_ak.history_calls, ["159995"])

    def test_history_fetch_retries_after_transient_error(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            fake_ak = _FakeAk({"159995": self._raw_history(data_date)}, transient_failures={"159995": 1})

            with patch("trend_radar.fetcher.time.sleep"):
                result = self._fetch(fake_ak, data_date, root=root, codes=["159995"], resume=True, max_workers=1)

            self.assertEqual(fake_ak.history_calls, ["159995", "159995"])
            self.assertEqual(result.errors, [])
            self.assertTrue((root / "data" / "normalized" / data_date / "history" / "159995.csv").exists())
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 1)
            self.assertEqual(report["history_success"], 1)
            self.assertEqual(report["history_failed"], 0)
            self.assertEqual(report["fetched_history_files"], 1)

    def test_resume_retry_only_fetches_missing_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            self._write_normalized_history(root, data_date, "159995")
            fake_ak = _FakeAk({"159995": self._raw_history(data_date), "510300": self._raw_history(data_date)})

            result = self._fetch(
                fake_ak,
                data_date,
                root=root,
                codes=["159995", "510300"],
                resume=True,
                max_workers=2,
            )

            self.assertEqual(result.candidate_rows, 2)
            self.assertEqual(fake_ak.history_calls, ["510300"])
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 2)
            self.assertEqual(report["history_success"], 2)
            self.assertEqual(report["history_failed"], 0)
            self.assertEqual(report["skipped_cached"], 1)
            self.assertEqual(report["fetched_history_files"], 1)

    def test_mismatched_spot_date_uses_previous_candidate_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            previous_date = "2026-06-03"
            data_date = "2026-06-04"
            codes = ["159995", "510300", "588000"]
            self._write_candidates(root, previous_date, codes)
            fake_ak = _FakeAk(
                {code: self._raw_history(data_date) for code in codes},
                spot_date="2026-06-05",
                spot_amounts={code: 1_000_000 for code in codes},
            )

            result = self._fetch(fake_ak, data_date, root=root, resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 3)
            self.assertEqual(set(fake_ak.history_calls), set(codes))
            report = self._read_report(root, data_date)
            self.assertEqual(report["spot_data_dates"], ["2026-06-05"])
            self.assertFalse(report["spot_data_matches_target"])
            self.assertFalse(report["spot_derived_metrics_enabled"])
            self.assertEqual(report["candidate_source"], "previous_candidates")
            self.assertEqual(report["candidate_source_date"], previous_date)
            candidates = pd.read_csv(root / "data" / "normalized" / data_date / "spot_candidates.csv")
            self.assertTrue(candidates["amount_yi"].isna().all())
            self.assertTrue(candidates["latest_share_yi"].isna().all())

    def test_mismatched_cached_candidates_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            previous_date = "2026-06-03"
            data_date = "2026-06-04"
            self._write_candidates(root, previous_date, ["159995", "510300"])
            self._write_candidates(root, data_date, ["588000"], candidate_data_date="2026-06-05")
            fake_ak = _FakeAk(
                {
                    "159995": self._raw_history(data_date),
                    "510300": self._raw_history(data_date),
                    "588000": self._raw_history(data_date),
                },
                spot_date="2026-06-05",
                spot_amounts={"159995": 1_000_000, "510300": 1_000_000, "588000": 1_000_000},
            )

            result = self._fetch(fake_ak, data_date, root=root, resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 2)
            self.assertEqual(set(fake_ak.history_calls), {"159995", "510300"})
            report = self._read_report(root, data_date)
            self.assertEqual(report["candidate_source"], "previous_candidates")
            self.assertEqual(report["candidate_source_date"], previous_date)

    def _write_normalized_history(self, root: Path, data_date: str, code: str, latest_date: str | None = None) -> None:
        path = root / "data" / "normalized" / data_date / "history" / f"{code}.csv"
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "code": [code],
                "日期": [latest_date or data_date],
                "开盘价": [1.0],
                "最高价": [1.1],
                "最低价": [0.9],
                "收盘价": [1.0],
                "成交额(亿)": [5.0],
                "涨跌幅": [0.1],
            }
        ).to_csv(path, index=False)

    def _write_normalized_spot(self, root: Path, data_date: str, code: str) -> None:
        path = root / "data" / "normalized" / data_date / "spot_all.csv"
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "code": [code],
                "name": [f"测试ETF{code}"],
                "latest_price": [1.2],
                "amount_yi": [5.0],
                "latest_share_yi": [10.0],
            }
        ).to_csv(path, index=False)

    def _write_raw_spot(self, root: Path, data_date: str, code: str) -> None:
        path = root / "data" / "raw" / data_date / "spot_em.csv"
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "代码": [code],
                "名称": [f"测试ETF{code}"],
                "最新价": [1.2],
                "开盘价": [1.1],
                "最高价": [1.3],
                "最低价": [1.0],
                "成交额": [600_000_000],
                "成交量": [100_000],
                "涨跌幅": [2.0],
                "数据日期": [data_date],
            }
        ).to_csv(path, index=False)

    def _write_candidates(
        self,
        root: Path,
        data_date: str,
        codes: list[str],
        candidate_data_date: str | None = None,
    ) -> None:
        path = root / "data" / "normalized" / data_date / "spot_candidates.csv"
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "code": codes,
                "name": [f"测试ETF{code}" for code in codes],
                "latest_price": [1.2] * len(codes),
                "pct_change": [0.5] * len(codes),
                "amount_yi": [5.0] * len(codes),
                "volume": [100_000] * len(codes),
                "data_date": [candidate_data_date or data_date] * len(codes),
                "update_time": [f"{candidate_data_date or data_date} 16:00:00+08:00"] * len(codes),
                "latest_share_yi": [10.0] * len(codes),
                "total_market_cap_yi": [12.0] * len(codes),
                "float_market_cap_yi": [12.0] * len(codes),
                "is_equity_etf": [True] * len(codes),
                "universe_reason": ["a_share_equity_candidate"] * len(codes),
            }
        ).to_csv(path, index=False)

    def _raw_history(self, data_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "日期": [data_date],
                "开盘": [1.0],
                "最高": [1.1],
                "最低": [0.9],
                "收盘": [1.0],
                "成交额": [500_000_000],
                "涨跌幅": [0.1],
            }
        )

    def _read_report(self, root: Path, data_date: str) -> dict[str, object]:
        path = root / "data" / "normalized" / data_date / "fetch_report.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _fetch(self, fake_ak: _FakeAk, *args: object, **kwargs: object) -> object:
        def fake_eastmoney(code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
            return fake_ak.fund_etf_hist_em(code, start_date=start_date, end_date=end_date), "eastmoney_mock"

        with (
            patch("trend_radar.fetcher._load_akshare", return_value=fake_ak),
            patch("trend_radar.fetcher._fetch_history_eastmoney_with_provider", side_effect=fake_eastmoney),
        ):
            return fetch_data(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
