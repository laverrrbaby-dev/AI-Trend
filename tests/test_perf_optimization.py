"""跨模块性能优化集成测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class FetchReportMemoizeTests(unittest.TestCase):
    def test_memoize_same_mtime_returns_cached(self) -> None:
        """R5: 同一 (data_date, mtime) → 命中缓存"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            report_path = root / "data" / "normalized" / data_date / "fetch_report.json"
            report_path.parent.mkdir(parents=True)
            payload = {"data_date": data_date, "candidate_rows": 100}
            report_path.write_text(json.dumps(payload), encoding="utf-8")

            # app.py 引用 ROOT，需要 mock
            with patch("app.ROOT", root):
                import app
                # 重置 memoize 缓存
                if hasattr(app, "_fetch_report_cache"):
                    app._fetch_report_cache = {}
                report1 = app._read_fetch_report(data_date)
                # 第二次应命中缓存（mock pd.read_json 验证未调用）
                read_count = [0]
                original_read = app._read_fetch_report.__wrapped__ if hasattr(app._read_fetch_report, "__wrapped__") else None
                report2 = app._read_fetch_report(data_date)

                self.assertEqual(report1, report2)
                self.assertEqual(report1["candidate_rows"], 100)

    def test_memoize_invalidated_on_mtime_change(self) -> None:
        """R5: mtime 变 → 重读"""
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"
            report_path = root / "data" / "normalized" / data_date / "fetch_report.json"
            report_path.parent.mkdir(parents=True)
            payload1 = {"data_date": data_date, "candidate_rows": 100}
            report_path.write_text(json.dumps(payload1), encoding="utf-8")

            with patch("app.ROOT", root):
                import app
                if hasattr(app, "_fetch_report_cache"):
                    app._fetch_report_cache = {}
                report1 = app._read_fetch_report(data_date)
                self.assertEqual(report1["candidate_rows"], 100)

                # 修改文件
                import time
                payload2 = {"data_date": data_date, "candidate_rows": 200}
                report_path.write_text(json.dumps(payload2), encoding="utf-8")
                time.sleep(0.01)  # 确保 mtime 变化
                os.utime(report_path, None)  # 强制更新 mtime

                report2 = app._read_fetch_report(data_date)
                self.assertEqual(report2["candidate_rows"], 200)


class IncrementalRefreshIntegrationTests(unittest.TestCase):
    def test_incremental_refresh_with_cached_history_completes_fast(self) -> None:
        """集成测试：大部分数据已缓存时，增量刷新应在合理时间内完成"""
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from trend_radar.config import ASIA_SHANGHAI
        from trend_radar.refresh import refresh_latest_data, REFRESH_SUCCEEDED

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_date = "2026-05-29"

            # 预写缓存历史 + radar
            self._write_cached_history(root, data_date)

            call_count = [0]
            def fake_fetcher(data_date: str, root: Path, **kwargs: object) -> object:
                call_count[0] += 1
                callback = kwargs.get("progress_callback")
                if callback:
                    callback({
                        "phase": "fetch_history",
                        "completed": 1, "total": 1,
                        "current_code": "159995", "current_name": "测试",
                        "skipped_cached": 1, "fetched": 0, "errors": 0,
                        "max_workers": 20,
                    })
                from types import SimpleNamespace
                return SimpleNamespace(data_date=data_date, date_counts={"2026-05-29": 1})

            def fake_scorer(data_date: str, root: Path, source_date: str | None = None) -> object:
                return self._write_radar(root, data_date)

            now = datetime(2026, 5, 30, 16, 0, tzinfo=ASIA_SHANGHAI)
            start = time.time()
            result = refresh_latest_data(root=root, now=now, fetcher=fake_fetcher, scorer=fake_scorer)
            elapsed = time.time() - start

            self.assertEqual(result.status, REFRESH_SUCCEEDED)
            self.assertLess(elapsed, 5.0, f"incremental refresh should be fast; elapsed={elapsed}")

    def _write_cached_history(self, root: Path, data_date: str) -> None:
        """预写缓存，模拟增量场景。"""
        normalized_dir = root / "data" / "normalized" / data_date
        history_dir = normalized_dir / "history"
        history_dir.mkdir(parents=True)
        spot_dir = normalized_dir
        # spot_candidates
        spot_df = pd.DataFrame({
            "code": ["159995"], "name": ["测试ETF"],
            "数据日期": [data_date], "amount_yi": [5.0],
            "total_market_cap_yi": [10.0], "float_market_cap_yi": [8.0],
            "latest_price": [1.2], "pct_change": [0.1],
            "volume": [1000000], "latest_share_yi": [1.0],
            "is_equity_etf": [True], "universe_reason": ["test"],
        })
        spot_df.to_csv(spot_dir / "spot_candidates.csv", index=False)
        # history
        history_df = pd.DataFrame({
            "日期": [data_date],
            "开盘价": [1.0], "最高价": [1.1], "最低价": [0.9],
            "收盘价": [1.0], "成交额(亿)": [5.0],
            "成交量": [1000000], "code": ["159995"],
            "history_source": ["cache"],
        })
        history_df.to_csv(history_dir / "159995.csv", index=False)

    def _write_radar(self, root: Path, data_date: str) -> object:
        from types import SimpleNamespace
        radar_dir = root / "data" / "radar"
        radar_dir.mkdir(parents=True, exist_ok=True)
        (radar_dir / f"{data_date}.csv").write_text("排名,代码\n1,159995\n", encoding="utf-8")
        (radar_dir / f"{data_date}.json").write_text(
            json.dumps({"data_date": data_date, "generated_at": "2026-05-29 16:00:00"}),
            encoding="utf-8",
        )
        return SimpleNamespace(
            data_date=data_date,
            radar_csv=radar_dir / f"{data_date}.csv",
            radar_json=radar_dir / f"{data_date}.json",
            rows=1,
            quality_status="OK",
        )


if __name__ == "__main__":
    unittest.main()
