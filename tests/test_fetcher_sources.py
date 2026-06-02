from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.fetcher import (  # noqa: E402
    _HISTORY_API_COLUMNS,
    _fetch_history_eastmoney_with_provider,
)


EASTMONEY_KLINES = [
    "2026-05-28,3.500,3.520,3.540,3.490,12345678,43450000,1.44,0.57,0.02,2.40",
    "2026-05-29,3.520,3.540,3.560,3.510,11890000,41780000,1.42,0.57,0.02,2.38",
]


def _mock_urlopen(klines: list[str]) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps({"data": {"klines": klines}}).encode("utf-8")
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


class FetcherSourcesTests(unittest.TestCase):
    def test_eastmoney_provider_reports_urllib_success(self) -> None:
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(EASTMONEY_KLINES)):
            frame, provider = _fetch_history_eastmoney_with_provider("510050", "20260528", "20260529")

        self.assertEqual(provider, "eastmoney_urllib")
        self.assertEqual(frame.columns.tolist(), _HISTORY_API_COLUMNS)
        self.assertEqual(len(frame), 2)

    def test_eastmoney_provider_uses_curl_after_urllib_failure(self) -> None:
        curl_frame = pd.DataFrame([EASTMONEY_KLINES[0].split(",")], columns=_HISTORY_API_COLUMNS)
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionError("blocked")),
            patch("trend_radar.fetcher._fetch_history_eastmoney_curl", return_value=curl_frame),
        ):
            frame, provider = _fetch_history_eastmoney_with_provider("510050", "20260528", "20260529")

        self.assertEqual(provider, "eastmoney_curl")
        self.assertEqual(len(frame), 1)

    def test_eastmoney_provider_uses_proxy_curl_after_direct_curl_failure(self) -> None:
        curl_frame = pd.DataFrame([EASTMONEY_KLINES[0].split(",")], columns=_HISTORY_API_COLUMNS)
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionError("blocked")),
            patch("trend_radar.fetcher._fetch_history_eastmoney_curl", side_effect=[RuntimeError("direct down"), curl_frame]) as curl_mock,
            patch("trend_radar.fetcher._history_proxy", return_value="http://127.0.0.1:7890"),
        ):
            frame, provider = _fetch_history_eastmoney_with_provider("510050", "20260528", "20260529")

        self.assertEqual(provider, "eastmoney_curl_proxy")
        self.assertEqual(len(frame), 1)
        self.assertEqual(curl_mock.call_args_list[0].kwargs, {})
        self.assertEqual(curl_mock.call_args_list[1].kwargs, {"proxy": "http://127.0.0.1:7890"})

    def test_eastmoney_provider_raises_when_urllib_and_curl_fail(self) -> None:
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionError("blocked")),
            patch("trend_radar.fetcher._fetch_history_eastmoney_curl", side_effect=RuntimeError("curl down")),
            patch("trend_radar.fetcher._history_proxy", return_value=None),
        ):
            with self.assertRaises(RuntimeError) as context:
                _fetch_history_eastmoney_with_provider("510050", "20260528", "20260529")

        self.assertIn("urllib failed", str(context.exception))
        self.assertIn("curl failed", str(context.exception))


if __name__ == "__main__":
    unittest.main()
