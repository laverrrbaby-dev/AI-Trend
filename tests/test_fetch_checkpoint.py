from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.fetcher import (
    _checkpoint_path,
    _clear_checkpoint,
    _read_checkpoint,
    _rebuild_outcomes_from_checkpoint,
    _write_checkpoint,
    fetch_data,
)
from trend_radar.config import now_shanghai


class _FakeAk:
    def __init__(
        self,
        histories: dict[str, pd.DataFrame],
        failures: set[str] | None = None,
        spot_date: str | None = None,
        spot_amounts: dict[str, float] | None = None,
    ) -> None:
        self.histories = histories
        self.failures = failures or set()
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
        if symbol in self.failures:
            raise RuntimeError("history down")
        return self.histories[symbol]

    def fund_etf_scale_sse(self, date: str) -> pd.DataFrame:
        return pd.DataFrame()

    def fund_scale_daily_szse(self, **kwargs: object) -> pd.DataFrame:
        return pd.DataFrame()


class CheckpointTests(unittest.TestCase):
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

    def _write_normalized_history(
        self,
        root: Path,
        data_date: str,
        code: str,
        latest_date: str | None = None,
    ) -> None:
        path = root / "data" / "normalized" / data_date / "history" / f"{code}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
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

    def _write_candidates(
        self,
        root: Path,
        data_date: str,
        codes: list[str],
    ) -> None:
        path = root / "data" / "normalized" / data_date / "spot_candidates.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "code": codes,
                "name": [f"测试ETF{code}" for code in codes],
                "latest_price": [1.2] * len(codes),
                "pct_change": [0.5] * len(codes),
                "amount_yi": [5.0] * len(codes),
                "volume": [100_000] * len(codes),
                "data_date": [data_date] * len(codes),
                "update_time": [f"{data_date} 16:00:00+08:00"] * len(codes),
                "latest_share_yi": [10.0] * len(codes),
                "total_market_cap_yi": [12.0] * len(codes),
                "float_market_cap_yi": [12.0] * len(codes),
                "is_equity_etf": [True] * len(codes),
                "universe_reason": ["a_share_equity_candidate"] * len(codes),
            }
        ).to_csv(path, index=False)

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

    # --- Test Cases ---

    def test_checkpoint_write_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            codes = ["159995", "510300"]
            outcomes = {
                "159995": {"code": "159995", "name": "test", "status": "cached", "rows": 100, "provider": "fixture", "latest_date": data_date, "stale": False},
                "510300": {"code": "510300", "name": "test2", "status": "fetched", "rows": 120, "provider": "eastmoney_urllib", "latest_date": data_date, "stale": False},
            }

            _write_checkpoint(data_date, codes, outcomes, root)
            # Override PID to dead value so _read_checkpoint accepts it
            import os as _os
            dead_pid = 999999
            while True:
                try:
                    _os.kill(dead_pid, 0)
                    dead_pid += 1
                except (ProcessLookupError, PermissionError):
                    break
                if dead_pid > 9999999:
                    break
            path = _checkpoint_path(root, data_date)
            original = json.loads(path.read_text(encoding="utf-8"))
            original["pid"] = dead_pid
            path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            self.assertTrue(path.exists())

            payload = _read_checkpoint(root, data_date)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["data_date"], data_date)
            self.assertEqual(payload["candidate_codes"], codes)
            self.assertIn("159995", payload["outcomes"])
            self.assertIn("510300", payload["outcomes"])
            self.assertEqual(payload["outcomes"]["159995"]["status"], "cached")
            self.assertEqual(payload["outcomes"]["510300"]["status"], "fetched")

    def test_checkpoint_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            codes = ["159995"]
            outcomes = {"159995": {"code": "159995", "name": "test", "status": "cached", "rows": 100, "provider": "fixture", "latest_date": data_date, "stale": False}}

            _write_checkpoint(data_date, codes, outcomes, root)
            self.assertTrue(_checkpoint_path(root, data_date).exists())

            _clear_checkpoint(root, data_date)
            self.assertFalse(_checkpoint_path(root, data_date).exists())

    def test_clear_checkpoint_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            _clear_checkpoint(root, data_date)  # Should not raise

    def test_checkpoint_ignores_different_date(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            date_a = "2026-06-01"
            date_b = "2026-06-02"
            outcomes = {"159995": {"code": "159995", "name": "test", "status": "cached", "rows": 100, "provider": "fixture", "latest_date": date_a, "stale": False}}

            _write_checkpoint(date_a, ["159995"], outcomes, root)
            result = _read_checkpoint(root, date_b)
            self.assertIsNone(result)

    def test_checkpoint_live_pid_is_ignored(self) -> None:
        """Checkpoint with the current PID should be ignored (prevents same-process reuse)."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            outcomes = {"159995": {"code": "159995", "name": "test", "status": "cached", "rows": 100, "provider": "fixture", "latest_date": data_date, "stale": False}}

            _write_checkpoint(data_date, ["159995"], outcomes, root)
            payload = _read_checkpoint(root, data_date)
            self.assertIsNone(payload, "Checkpoint with current PID should be ignored")

    def test_checkpoint_dead_pid_is_accepted(self) -> None:
        """Checkpoint with a dead PID should be accepted."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            outcomes = {"159995": {"code": "159995", "name": "test", "status": "cached", "rows": 100, "provider": "fixture", "latest_date": data_date, "stale": False}}

            _write_checkpoint(data_date, ["159995"], outcomes, root)

            # Overwrite PID to a dead one
            path = _checkpoint_path(root, data_date)
            original = json.loads(path.read_text(encoding="utf-8"))
            dead_pid = 99999
            while True:
                try:
                    os.kill(dead_pid, 0)
                    dead_pid += 1
                except (ProcessLookupError, PermissionError):
                    break
                if dead_pid > 999999:
                    self.skipTest("Cannot find a dead PID")
            original["pid"] = dead_pid
            path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            payload = _read_checkpoint(root, data_date)
            self.assertIsNotNone(payload, "Checkpoint with dead PID should be accepted")

    def test_resume_from_checkpoint_skips_completed_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            codes = ["159995", "510300", "588000"]

            # Pre-write candidates and completed history files for 2 of 3 codes
            self._write_candidates(root, data_date, codes)
            self._write_normalized_history(root, data_date, "159995", data_date)
            self._write_normalized_history(root, data_date, "510300", data_date)

            # Manually create a checkpoint (with dead PID to pass validation)
            outcomes = {
                "159995": {"code": "159995", "name": "test159995", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
                "510300": {"code": "510300", "name": "test510300", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
            }
            _write_checkpoint(data_date, codes, outcomes, root)
            # Set PID to dead
            path = _checkpoint_path(root, data_date)
            original = json.loads(path.read_text(encoding="utf-8"))
            original["pid"] = 999999
            path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            fake_ak = _FakeAk(
                {code: self._raw_history(data_date) for code in codes},
            )

            result = self._fetch(fake_ak, data_date, root=root, resume=True, max_workers=2)

            self.assertEqual(result.candidate_rows, 3)
            # Only 588000 should be fetched (the other 2 are in checkpoint)
            self.assertEqual(fake_ak.history_calls, ["588000"])

            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 3)
            self.assertEqual(report["history_success"], 3)
            self.assertEqual(report["skipped_cached"], 2)
            self.assertEqual(report["fetched_history_files"], 1)

            # Checkpoint should be cleared after success
            self.assertFalse(_checkpoint_path(root, data_date).exists())

    def test_checkpoint_verifies_file_existence_before_restoring(self) -> None:
        """If a checkpoint says 'cached' but the file was deleted, re-fetch that code."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            codes = ["159995", "510300"]

            self._write_candidates(root, data_date, codes)
            # Only write history for 510300; 159995 is in checkpoint but file missing
            self._write_normalized_history(root, data_date, "510300", data_date)

            outcomes = {
                "159995": {"code": "159995", "name": "test159995", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
                "510300": {"code": "510300", "name": "test510300", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
            }
            _write_checkpoint(data_date, codes, outcomes, root)
            path = _checkpoint_path(root, data_date)
            original = json.loads(path.read_text(encoding="utf-8"))
            original["pid"] = 999999
            path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            fake_ak = _FakeAk(
                {code: self._raw_history(data_date) for code in codes},
            )

            result = self._fetch(fake_ak, data_date, root=root, resume=True, max_workers=2)

            # 159995 was in checkpoint but file missing -> should be re-fetched
            # 510300 was in checkpoint with valid file -> should be restored
            self.assertIn("159995", fake_ak.history_calls)
            self.assertEqual(result.candidate_rows, 2)
            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 2)
            self.assertEqual(report["history_success"], 2)

    def test_non_resume_does_not_write_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            codes = ["159995"]

            self._write_candidates(root, data_date, codes)
            fake_ak = _FakeAk(
                {"159995": self._raw_history(data_date)},
            )

            self._fetch(fake_ak, data_date, root=root, resume=False, max_workers=1)

            self.assertFalse(_checkpoint_path(root, data_date).exists())

    def test_checkpoint_handles_failed_codes(self) -> None:
        """Failed codes recorded in checkpoint should not be re-fetched on resume."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            codes = ["159995", "510300"]

            self._write_candidates(root, data_date, codes)
            self._write_normalized_history(root, data_date, "510300", data_date)

            outcomes = {
                "159995": {"code": "159995", "name": "test159995", "status": "error", "error": "permanent failure"},
                "510300": {"code": "510300", "name": "test510300", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
            }
            _write_checkpoint(data_date, codes, outcomes, root)
            path = _checkpoint_path(root, data_date)
            original = json.loads(path.read_text(encoding="utf-8"))
            original["pid"] = 999999
            path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            fake_ak = _FakeAk(
                {code: self._raw_history(data_date) for code in codes},
            )

            result = self._fetch(fake_ak, data_date, root=root, resume=True, max_workers=2)

            # Only 159995 was error, 510300 was cached -> neither should trigger new fetch
            self.assertEqual(fake_ak.history_calls, [])

            report = self._read_report(root, data_date)
            self.assertEqual(report["history_total"], 2)
            self.assertEqual(report["history_success"], 1)
            self.assertEqual(report["history_failed"], 1)
            self.assertEqual(report["failed_history"][0]["code"], "159995")

    def test_checkpoint_atomic_write_does_not_create_partial_checkpoint(self) -> None:
        """Verify that the checkpoint write uses a temp file pattern (no partial files read)."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            outcomes = {"159995": {"code": "159995", "name": "test", "status": "cached", "rows": 100, "provider": "fixture", "latest_date": data_date, "stale": False}}

            _write_checkpoint(data_date, ["159995"], outcomes, root)

            # The actual file should be fetch_checkpoint_{date}.json (not .tmp)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            files = list(state_dir.glob("fetch_checkpoint_*"))
            # Only the real checkpoint file, no .tmp files after successful write
            json_files = [f for f in files if f.suffix == ".json"]
            self.assertEqual(len(json_files), 1)
            self.assertEqual(json_files[0].name, f"fetch_checkpoint_{data_date}.json")

    def test_resume_without_checkpoint_behaves_normally(self) -> None:
        """When no checkpoint exists, resume=True should behave identically to before."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            codes = ["159995", "510300"]

            self._write_candidates(root, data_date, codes)
            self._write_normalized_history(root, data_date, "159995", data_date)

            fake_ak = _FakeAk(
                {code: self._raw_history(data_date) for code in codes},
            )

            result = self._fetch(fake_ak, data_date, root=root, resume=True, max_workers=2)

            # 159995 cached, 510300 fetched
            self.assertEqual(result.candidate_rows, 2)
            self.assertEqual(fake_ak.history_calls, ["510300"])
            self.assertFalse(_checkpoint_path(root, data_date).exists())

    def test_rebuild_outcomes_from_checkpoint_with_valid_files(self) -> None:
        """Unit test for _rebuild_outcomes_from_checkpoint with existing files."""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-06-01"
            codes = ["159995", "510300"]
            normalized_history_dir = root / "data" / "normalized" / data_date / "history"
            normalized_history_dir.mkdir(parents=True)

            # Write valid history for code 159995 only
            self._write_normalized_history(root, data_date, "159995", data_date)

            checkpoint = {
                "outcomes": {
                    "159995": {"code": "159995", "name": "test", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
                    "510300": {"code": "510300", "name": "test2", "status": "cached", "rows": 1, "provider": "fixture", "latest_date": data_date, "stale": False},
                }
            }

            target = pd.Timestamp(data_date)
            outcomes, failed, skipped, fetched, errors, hist_rows = _rebuild_outcomes_from_checkpoint(
                checkpoint, normalized_history_dir, target
            )

            # 159995 file exists -> restored; 510300 file missing -> excluded
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].code, "159995")
            self.assertEqual(outcomes[0].status, "cached")
            self.assertEqual(skipped, 1)
            self.assertEqual(hist_rows, 1)


if __name__ == "__main__":
    unittest.main()
