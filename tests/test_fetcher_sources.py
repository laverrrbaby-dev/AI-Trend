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
    _call_akshare,
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


class _BrokenStderr:
    """Simulates Streamlit's invalidated stderr (closed pipe → EIO on flush)."""

    def write(self, _s: str) -> int:
        raise OSError(5, "Input/output error")

    def flush(self) -> None:
        raise OSError(5, "Input/output error")

    def fileno(self) -> int:
        return 2

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False


class CallAkshareStderrProtectionTests(unittest.TestCase):
    """Regression: akshare uses tqdm which writes to sys.stderr. In a Streamlit
    background thread sys.stderr is a context-bound stream that becomes invalid
    on script rerun, causing tqdm's flush() to raise OSError [Errno 5].
    _call_akshare must redirect stderr so the call survives."""

    def test_call_akshare_survives_broken_stderr(self) -> None:
        original_stderr = sys.stderr

        def _tqdm_like(**_kwargs: object) -> pd.DataFrame:
            # Mimic akshare's internal tqdm writing to sys.stderr
            sys.stderr.write("progress")
            sys.stderr.flush()
            return pd.DataFrame({"code": ["510050"], "name": ["t"]})

        ak = MagicMock()
        ak.fund_etf_spot_em = _tqdm_like

        try:
            sys.stderr = _BrokenStderr()
            frame = _call_akshare(ak, "fund_etf_spot_em")
        finally:
            sys.stderr = original_stderr

        self.assertEqual(len(frame), 1)
        # stderr must be restored to the caller's (broken) value after the call
        self.assertIs(sys.stderr, original_stderr)

    def test_call_akshare_restores_stderr_even_on_failure(self) -> None:
        original_stderr = sys.stderr

        def _boom(**_kwargs: object) -> pd.DataFrame:
            sys.stderr.write("progress")
            sys.stderr.flush()
            raise RuntimeError("akshare failed")

        ak = MagicMock()
        ak.fund_etf_spot_em = _boom

        try:
            sys.stderr = _BrokenStderr()
            with self.assertRaises(RuntimeError):
                _call_akshare(ak, "fund_etf_spot_em")
        finally:
            sys.stderr = original_stderr

        self.assertIs(sys.stderr, original_stderr)


if __name__ == "__main__":
    unittest.main()
