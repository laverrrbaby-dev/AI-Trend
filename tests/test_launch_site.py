from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("launch_site", ROOT / "scripts" / "launch_site.py")
assert SPEC and SPEC.loader
launch_site = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launch_site
SPEC.loader.exec_module(launch_site)


class LaunchSiteTests(unittest.TestCase):
    def test_second_launch_opens_existing_site_without_starting_a_second_process(self) -> None:
        running_process = Mock()
        running_process.poll.return_value = 0
        running_process.returncode = 0

        with (
            patch.object(launch_site, "_is_site_running", create=True, return_value=True) as is_running,
            patch.object(launch_site, "_ensure_environment") as ensure_environment,
            patch.object(launch_site, "_start_streamlit", return_value=running_process) as start_streamlit,
            patch.object(launch_site, "_wait_until_ready", return_value=True),
            patch.object(launch_site.webbrowser, "open") as open_browser,
            patch.object(sys, "argv", ["launch_site.py"]),
        ):
            self.assertEqual(launch_site.main(), 0)

        is_running.assert_called_once_with("http://127.0.0.1:8501")
        ensure_environment.assert_not_called()
        start_streamlit.assert_not_called()
        open_browser.assert_called_once_with("http://localhost:8501")

    def test_unexpected_launcher_error_does_not_stop_the_site_process(self) -> None:
        process = Mock()

        with (
            patch.object(launch_site, "_is_site_running", return_value=False),
            patch.object(launch_site, "_ensure_environment"),
            patch.object(launch_site, "_start_streamlit", return_value=process),
            patch.object(launch_site, "_wait_until_ready", side_effect=RuntimeError("launcher failed")),
            patch.object(launch_site, "_stop_process") as stop_process,
            patch.object(sys, "argv", ["launch_site.py", "--no-browser"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "launcher failed"):
                launch_site.main()

        stop_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
