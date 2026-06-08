from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trend_radar.config import ASIA_SHANGHAI, DEFAULT_HISTORY_FETCH_MAX_WORKERS  # noqa: E402
from trend_radar.refresh import (  # noqa: E402
    REFRESH_FAILED,
    REFRESH_RUNNING,
    REFRESH_SKIPPED,
    REFRESH_SUCCEEDED,
    problem_snapshot_summary,
    read_refresh_status,
    refresh_latest_data,
    refresh_task_is_active,
    retry_problem_snapshots,
    retry_selected_date,
    scan_problem_snapshots,
    resolve_refresh_target_date,
)


class RefreshTests(unittest.TestCase):
    def test_saturday_resolves_to_previous_complete_weekday(self) -> None:
        now = datetime(2026, 5, 30, 10, 0, tzinfo=ASIA_SHANGHAI)

        self.assertEqual(resolve_refresh_target_date(now), "2026-05-29")

    def test_before_market_complete_resolves_to_previous_weekday(self) -> None:
        now = datetime(2026, 6, 1, 14, 59, tzinfo=ASIA_SHANGHAI)

        self.assertEqual(resolve_refresh_target_date(now), "2026-05-29")

    def test_existing_target_radar_skips_without_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-05-29")

            def fail_fetcher(**kwargs: object) -> None:
                raise AssertionError("fetcher should not be called")

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fail_fetcher)

            self.assertEqual(result.status, REFRESH_SKIPPED)
            self.assertEqual(result.target_date, "2026-05-29")
            status = read_refresh_status(root)
            self.assertEqual(status["status"], REFRESH_SKIPPED)
            self.assertEqual(status["generated_at"], "2026-05-29 16:00:00")

    def test_missing_weekday_gap_is_backfilled_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-06-04")
            calls: list[tuple[str, str, str | None]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date, source_date=source_date)

            now = datetime(2026, 6, 8, 16, 0, tzinfo=ASIA_SHANGHAI)
            result = refresh_latest_data(root=root, now=now, fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-06-08")
            self.assertEqual(
                calls,
                [
                    ("fetch", "2026-06-05", None),
                    ("score", "2026-06-05", None),
                    ("fetch", "2026-06-08", None),
                    ("score", "2026-06-08", None),
                ],
            )
            self.assertTrue((root / "data" / "radar" / "2026-06-05.csv").exists())
            self.assertIn("已补齐 1 个缺失日期：2026-06-05", result.message or "")

    def test_existing_target_still_backfills_missing_weekday_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-06-04")
            self._write_radar(root, "2026-06-08")
            calls: list[tuple[str, str, str | None]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date, source_date=source_date)

            now = datetime(2026, 6, 8, 16, 0, tzinfo=ASIA_SHANGHAI)
            result = refresh_latest_data(root=root, now=now, fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-06-08")
            self.assertEqual(calls, [("fetch", "2026-06-05", None), ("score", "2026-06-05", None)])
            self.assertEqual(result.radar_csv, root / "data" / "radar" / "2026-06-08.csv")
            self.assertIn("当前已是最新 2026-06-08 数据", result.message or "")

    def test_missing_target_fetches_then_scores_and_writes_success(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls: list[tuple[str, str, str | None]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date)

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-05-29")
            self.assertEqual(calls, [("fetch", "2026-05-29", None), ("score", "2026-05-29", None)])
            status = read_refresh_status(root)
            self.assertEqual(status["status"], REFRESH_SUCCEEDED)
            self.assertEqual(status["actual_date"], "2026-05-29")

    def test_stale_source_scores_actual_latest_history_date(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls: list[tuple[str, str, str | None]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                self._write_history(root, data_date, "2026-05-28")
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date)

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-05-28")
            self.assertEqual(calls, [("fetch", "2026-05-29", None), ("score", "2026-05-28", "2026-05-29")])
            self.assertEqual(read_refresh_status(root)["message"], "数据源最新为 2026-05-28，已生成对应数据")

    def test_partial_target_coverage_scores_recent_complete_history_date(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls: list[tuple[str, str, str | None]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                self._write_history_coverage(root, data_date, complete_date="2026-05-29", partial_date=data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date)

            now = datetime(2026, 6, 1, 16, 0, tzinfo=ASIA_SHANGHAI)
            self._write_radar(root, "2026-06-01")
            result = refresh_latest_data(root=root, force=True, now=now, fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(result.actual_date, "2026-05-29")
            self.assertEqual(calls, [("fetch", "2026-06-01", None), ("score", "2026-05-29", "2026-06-01")])
            self.assertFalse((root / "data" / "radar" / "2026-06-01.csv").exists())
            latest = json.loads((root / "data" / "state" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["data_date"], "2026-05-29")

    def test_fetch_error_writes_failed_status_and_preserves_old_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-05-28")

            def fake_fetcher(**kwargs: object) -> object:
                raise RuntimeError("network down")

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher)

            self.assertEqual(result.status, REFRESH_FAILED)
            self.assertTrue((root / "data" / "radar" / "2026-05-28.csv").exists())
            status = read_refresh_status(root)
            self.assertEqual(status["status"], REFRESH_FAILED)
            self.assertIn("network down", status["error"])

    def test_dead_pid_lock_is_recovered_and_refresh_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "refresh.lock").write_text(json.dumps({"pid": 999999, "created_at": "2026-05-30 10:01:34"}), encoding="utf-8")
            (state_dir / "refresh_status.json").write_text(
                json.dumps({"status": REFRESH_RUNNING, "target_date": "2026-05-29"}),
                encoding="utf-8",
            )
            calls: list[str] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(data_date)
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(calls, ["2026-05-29"])
            self.assertFalse((state_dir / "refresh.lock").exists())
            self.assertFalse(refresh_task_is_active(root))

    def test_live_pid_lock_still_blocks_duplicate_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "refresh.lock").write_text(
                json.dumps({"pid": os.getpid(), "created_at": "2026-05-30 10:01:34"}),
                encoding="utf-8",
            )
            (state_dir / "refresh_status.json").write_text(
                json.dumps({"status": REFRESH_RUNNING, "target_date": "2026-05-29"}),
                encoding="utf-8",
            )

            def fail_fetcher(**kwargs: object) -> None:
                raise AssertionError("fetcher should not be called")

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fail_fetcher)

            self.assertEqual(result.status, REFRESH_RUNNING)
            self.assertEqual(result.target_date, "2026-05-29")
            self.assertTrue(refresh_task_is_active(root))

    def test_running_status_without_lock_does_not_block_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "refresh_status.json").write_text(
                json.dumps({"status": REFRESH_RUNNING, "target_date": "2026-05-29"}),
                encoding="utf-8",
            )
            calls: list[str] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(data_date)
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(calls, ["2026-05-29"])

    def test_fetch_progress_is_written_to_status(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            progress_statuses: list[dict[str, object]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                callback = kwargs["progress_callback"]
                callback(
                    {
                        "phase": "fetch_history",
                        "completed": 12,
                        "total": 455,
                        "current_code": "560580",
                        "current_name": "测试ETF",
                        "skipped_cached": 7,
                        "fetched": 5,
                        "errors": 0,
                        "max_workers": DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                    }
                )
                progress_statuses.append(read_refresh_status(root))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            result = refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(progress_statuses[0]["phase"], "fetch_history")
            self.assertEqual(progress_statuses[0]["progress"]["completed"], 12)
            self.assertEqual(progress_statuses[0]["progress"]["total"], 455)
            self.assertEqual(progress_statuses[0]["progress"]["current_code"], "560580")
            self.assertEqual(progress_statuses[0]["progress"]["skipped_cached"], 7)
            self.assertEqual(progress_statuses[0]["progress"]["fetched"], 5)
            self.assertEqual(progress_statuses[0]["progress"]["errors"], 0)
            self.assertEqual(progress_statuses[0]["progress"]["max_workers"], DEFAULT_HISTORY_FETCH_MAX_WORKERS)

    def test_refresh_passes_resume_and_worker_options_to_fetcher(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls: list[dict[str, object]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(dict(kwargs))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            refresh_latest_data(root=root, now=self._saturday(), fetcher=fake_fetcher, scorer=fake_scorer)
            self.assertTrue(calls[0]["resume"])
            self.assertEqual(calls[0]["max_workers"], DEFAULT_HISTORY_FETCH_MAX_WORKERS)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            calls = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(dict(kwargs))
                self._write_history(root, data_date, data_date)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            refresh_latest_data(root=root, now=self._saturday(), force=True, fetcher=fake_fetcher, scorer=fake_scorer)
            self.assertFalse(calls[0]["resume"])
            self.assertEqual(calls[0]["max_workers"], DEFAULT_HISTORY_FETCH_MAX_WORKERS)

    def test_retry_selected_date_fetches_with_resume_even_when_radar_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_radar(root, "2026-05-29")
            calls: list[tuple[str, str, str | None]] = []
            fetch_kwargs: list[dict[str, object]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                fetch_kwargs.append(dict(kwargs))
                self._write_history(root, data_date, data_date)
                self._write_fetch_report(root, data_date, total=3, success=2, failed=1)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date)

            result = retry_selected_date("2026-05-29", root=root, fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(calls, [("fetch", "2026-05-29", None), ("score", "2026-05-29", None)])
            self.assertTrue(fetch_kwargs[0]["resume"])
            self.assertEqual(fetch_kwargs[0]["max_workers"], DEFAULT_HISTORY_FETCH_MAX_WORKERS)
            self.assertIn("历史行情成功 2/3，失败 1", result.message or "")
            status = read_refresh_status(root)
            self.assertEqual(status["status"], REFRESH_SUCCEEDED)
            self.assertIn("历史行情成功 2/3，失败 1", status["message"])

    def test_retry_selected_date_live_lock_blocks_duplicate_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "refresh.lock").write_text(
                json.dumps({"pid": os.getpid(), "created_at": "2026-05-30 10:01:34"}),
                encoding="utf-8",
            )
            (state_dir / "refresh_status.json").write_text(
                json.dumps({"status": REFRESH_RUNNING, "target_date": "2026-05-29"}),
                encoding="utf-8",
            )

            def fail_fetcher(**kwargs: object) -> None:
                raise AssertionError("fetcher should not be called")

            result = retry_selected_date("2026-05-29", root=root, fetcher=fail_fetcher)

            self.assertEqual(result.status, REFRESH_RUNNING)
            self.assertEqual(result.target_date, "2026-05-29")
            self.assertTrue(refresh_task_is_active(root))

    def test_scan_problem_snapshots_detects_failed_and_incomplete_history(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_normalized_snapshot(root, "2026-05-27", ["159001"], valid_codes=["159001"])
            self._write_fetch_report(root, "2026-05-27", total=1, success=1, failed=1)

            self._write_normalized_snapshot(root, "2026-05-28", ["159002"], valid_codes=["159002"])
            report_path = root / "data" / "normalized" / "2026-05-28" / "fetch_report.json"
            report_path.write_text(
                json.dumps({"errors": ["159002: history down", "SSE scale 20260528: bad columns"]}, ensure_ascii=False),
                encoding="utf-8",
            )

            self._write_normalized_snapshot(root, "2026-05-29", ["159003", "159004"], valid_codes=["159003"])
            self._write_normalized_snapshot(root, "2026-06-01", ["159005"], valid_codes=["159005"])
            scale_only = root / "data" / "normalized" / "2026-06-01" / "fetch_report.json"
            scale_only.write_text(json.dumps({"errors": ["SSE scale 20260601: bad columns"]}), encoding="utf-8")

            problems = scan_problem_snapshots(root)

            self.assertEqual([item.data_date for item in problems], ["2026-05-27", "2026-05-28", "2026-05-29"])
            self.assertEqual(problems[0].report_history_failed, 1)
            self.assertEqual(problems[1].legacy_history_errors, 1)
            self.assertEqual(problems[2].missing_or_invalid_history, 1)
            self.assertEqual(problem_snapshot_summary(root)["problem_count"], 3)

    def test_retry_problem_snapshots_fetches_all_problems_and_rescores_referencing_radars(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_normalized_snapshot(root, "2026-05-28", ["159001", "159002"], valid_codes=["159001"])
            self._write_normalized_snapshot(root, "2026-06-01", ["159003"], valid_codes=[])
            self._write_radar(root, "2026-04-28", source_date="2026-05-28")
            self._write_radar(root, "2026-05-28", source_date="2026-05-28")
            calls: list[tuple[str, str, str | None]] = []
            fetch_kwargs: list[dict[str, object]] = []

            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                calls.append(("fetch", data_date, None))
                fetch_kwargs.append(dict(kwargs))
                self._write_normalized_snapshot(root, data_date, [f"{data_date[-2:]}0001"], valid_codes=[f"{data_date[-2:]}0001"])
                self._write_fetch_report(root, data_date, total=1, success=1, failed=0)
                return SimpleNamespace(data_date=data_date)

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                calls.append(("score", data_date, source_date))
                return self._write_radar(root, data_date, source_date=source_date)

            result = retry_problem_snapshots(root=root, fetcher=fake_fetcher, scorer=fake_scorer)

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertEqual(
                calls,
                [
                    ("fetch", "2026-05-28", None),
                    ("fetch", "2026-06-01", None),
                    ("score", "2026-04-28", "2026-05-28"),
                    ("score", "2026-05-28", "2026-05-28"),
                    ("score", "2026-06-01", "2026-06-01"),
                ],
            )
            self.assertTrue(all(item["resume"] for item in fetch_kwargs))
            self.assertIn("已补拉 2 个存量快照并重算 3 个排行榜", result.message or "")

    def test_retry_problem_snapshots_live_lock_blocks_duplicate_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_dir = root / "data" / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "refresh.lock").write_text(
                json.dumps({"pid": os.getpid(), "created_at": "2026-05-30 10:01:34"}),
                encoding="utf-8",
            )
            (state_dir / "refresh_status.json").write_text(
                json.dumps({"status": REFRESH_RUNNING, "target_date": "存量问题"}),
                encoding="utf-8",
            )

            def fail_fetcher(**kwargs: object) -> None:
                raise AssertionError("fetcher should not be called")

            result = retry_problem_snapshots(root=root, fetcher=fail_fetcher)

            self.assertEqual(result.status, REFRESH_RUNNING)
            self.assertEqual(result.target_date, "存量问题")
            self.assertTrue(refresh_task_is_active(root))

    def _saturday(self) -> datetime:
        return datetime(2026, 5, 30, 10, 0, tzinfo=ASIA_SHANGHAI)

    def _write_history(self, root: Path, source_date: str, latest_date: str) -> None:
        history_dir = root / "data" / "normalized" / source_date / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        dates = pd.bdate_range(end=pd.Timestamp(latest_date), periods=3)
        pd.DataFrame({"日期": dates}).to_csv(history_dir / "159995.csv", index=False)

    def _write_history_coverage(self, root: Path, source_date: str, complete_date: str, partial_date: str) -> None:
        normalized_dir = root / "data" / "normalized" / source_date
        history_dir = normalized_dir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        codes = [f"159{i:03d}" for i in range(100)]
        pd.DataFrame({"code": codes, "name": [f"ETF{i}" for i in range(100)]}).to_csv(
            normalized_dir / "spot_candidates.csv",
            index=False,
        )
        for index, code in enumerate(codes):
            dates = [complete_date]
            if index < 5:
                dates.append(partial_date)
            pd.DataFrame({"日期": dates}).to_csv(history_dir / f"{code}.csv", index=False)

    def _write_normalized_snapshot(self, root: Path, data_date: str, codes: list[str], valid_codes: list[str]) -> None:
        normalized_dir = root / "data" / "normalized" / data_date
        history_dir = normalized_dir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"code": codes, "name": [f"ETF{code}" for code in codes]}).to_csv(
            normalized_dir / "spot_candidates.csv",
            index=False,
        )
        for code in valid_codes:
            pd.DataFrame({"日期": [data_date], "收盘价": [1.0], "成交额(亿)": [5.0]}).to_csv(
                history_dir / f"{code}.csv",
                index=False,
            )

    def _write_radar(self, root: Path, data_date: str, source_date: str | None = None) -> SimpleNamespace:
        radar_dir = root / "data" / "radar"
        radar_dir.mkdir(parents=True, exist_ok=True)
        radar_csv = radar_dir / f"{data_date}.csv"
        radar_json = radar_dir / f"{data_date}.json"
        radar_csv.write_text("排名,ETF名称,代码\n1,芯片ETF,159995\n", encoding="utf-8")
        radar_json.write_text(
            json.dumps(
                {
                    "data_date": data_date,
                    "source_date": source_date or data_date,
                    "generated_at": f"{data_date} 16:00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(data_date=data_date, radar_csv=radar_csv, radar_json=radar_json)

    def _write_fetch_report(self, root: Path, data_date: str, total: int, success: int, failed: int) -> None:
        report_path = root / "data" / "normalized" / data_date / "fetch_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "history_total": total,
                    "history_success": success,
                    "history_failed": failed,
                    "failed_history": [{"code": "512880", "name": "证券ETF", "error": "history down"}] if failed else [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
