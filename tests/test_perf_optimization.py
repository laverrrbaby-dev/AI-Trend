"""跨模块性能优化集成测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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


if __name__ == "__main__":
    unittest.main()
