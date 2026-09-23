from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app as app_module  # noqa: E402


class RuntimeUpdateTests(unittest.TestCase):
    def test_open_runtime_update_tells_user_to_refresh_only_after_update_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            update_script = source_root / "scripts" / "update_local_runtime.sh"
            update_script.parent.mkdir(parents=True)
            update_script.touch()
            runtime_root = root / "runtime"
            source_record = runtime_root / "config" / "local" / "update-source-root.txt"
            source_record.parent.mkdir(parents=True)
            source_record.write_text(f"{source_root}\n", encoding="utf-8")

            with patch.object(app_module.subprocess, "Popen") as popen:
                message = app_module._open_runtime_update_terminal(runtime_root)

        self.assertEqual(message, "已在终端打开更新任务；请等终端显示更新完成后，再手动刷新页面。")
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn(str(update_script), command[2])

    def test_open_runtime_update_reports_missing_source_configuration_without_running_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app_module.subprocess, "Popen") as popen:
                message = app_module._open_runtime_update_terminal(Path(directory))

        self.assertEqual(message, "未找到更新来源配置；请先在项目目录运行一次安装器。")
        popen.assert_not_called()

    def test_open_runtime_update_rejects_an_empty_source_configuration_without_running_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            source_record = runtime_root / "config" / "local" / "update-source-root.txt"
            source_record.parent.mkdir(parents=True)
            source_record.write_text("\n", encoding="utf-8")

            with patch.object(app_module.subprocess, "Popen") as popen:
                message = app_module._open_runtime_update_terminal(runtime_root)

        self.assertEqual(message, "更新来源无效；请先在项目目录运行一次安装器。")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
