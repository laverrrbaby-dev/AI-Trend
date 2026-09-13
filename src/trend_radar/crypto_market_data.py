"""Fixed-universe crypto OHLCV acquisition with UTC+8 completeness rules."""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .config import PROJECT_ROOT
from .crypto_alert_config import CryptoAlertConfig

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
OKX_HISTORY_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
CANDLE_COLUMNS = [
    "timestamp", "日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
    "source", "pair", "is_complete",
]
_HEADERS = {"User-Agent": "trend-radar/crypto-alerts", "Accept": "application/json"}
JsonFetcher = Callable[[str, dict[str, str]], Any]


@dataclass(frozen=True)
class CryptoHistoryResult:
    symbol: str
    source: str
    pair: str
    frame: pd.DataFrame
    in_progress: dict[str, Any] | None = None
    used_fallback: bool = False


def latest_complete_crypto_date(now: datetime | None = None) -> str:
    # Binance/OKX UTC daily candles close at 08:00 in Asia/Shanghai.
    current_utc = (now or datetime.now(UTC)).astimezone(UTC)
    return (current_utc.date() - timedelta(days=1)).isoformat()


def fetch_crypto_history(
    symbol: str,
    config: CryptoAlertConfig,
    root: Path = PROJECT_ROOT,
    now: datetime | None = None,
    fetch_json: JsonFetcher | None = None,
) -> CryptoHistoryResult:
    spec = config.sources[symbol]
    fetcher = fetch_json or _fetch_json
    used_fallback = False
    try:
        rows = _fetch_history_rows(fetcher, spec.primary, spec.pair, config.history_bars)
        source = spec.primary
        pair = spec.pair
    except Exception:
        rows = _fetch_history_rows(fetcher, spec.backup, spec.backup_pair, config.history_bars)
        source = spec.backup
        pair = spec.backup_pair
        used_fallback = True

    current = (now or datetime.now(UTC)).astimezone(UTC)
    frame, in_progress = _normalize_rows(rows, source, pair, current)
    if frame.empty:
        raise ValueError(f"no complete {symbol} crypto candles returned")
    _validate_frame(frame)
    _write_cache(root, symbol, frame, in_progress)
    return CryptoHistoryResult(symbol=symbol, source=source, pair=pair, frame=frame, in_progress=in_progress, used_fallback=used_fallback)


def load_crypto_history(root: Path, symbol: str) -> pd.DataFrame:
    path = root / "data" / "crypto" / "alerts" / "history" / f"{symbol}.csv"
    if not path.exists():
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    return pd.read_csv(path)


def fetch_latest_backup_candle(
    symbol: str,
    config: CryptoAlertConfig,
    now: datetime | None = None,
    fetch_json: JsonFetcher | None = None,
) -> dict[str, Any] | None:
    spec = config.sources[symbol]
    fetcher = fetch_json or _fetch_json
    if spec.backup == "binance":
        rows = _fetch_binance(fetcher, spec.backup_pair, 3)
        source = "binance"
    elif spec.backup == "okx":
        rows = _fetch_okx(fetcher, spec.backup_pair, 3)
        source = "okx"
    else:
        return None
    frame, _ = _normalize_rows(rows, source, spec.backup_pair, (now or datetime.now(UTC)).astimezone(UTC))
    return frame.iloc[-1].to_dict() if not frame.empty else None


def _fetch_binance(fetcher: JsonFetcher, pair: str, limit: int) -> list[Any]:
    url = _url(BINANCE_KLINES_URL, {"symbol": pair, "interval": "1d", "limit": str(limit)})
    payload = fetcher(url, _HEADERS)
    return payload if isinstance(payload, list) else []


def _fetch_okx(fetcher: JsonFetcher, pair: str, limit: int) -> list[Any]:
    rows: list[Any] = []
    after: str | None = None
    remaining = limit
    while remaining > 0:
        page_limit = min(300, remaining)
        params = {"instId": pair, "bar": "1Dutc", "limit": str(page_limit)}
        if after:
            params["after"] = after
        payload = fetcher(_url(OKX_HISTORY_CANDLES_URL, params), _HEADERS)
        page = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(page, list) or not page:
            break
        rows.extend(page)
        if len(page) < page_limit:
            break
        after = str(page[-1][0])
        remaining -= len(page)
    return rows


def _fetch_history_rows(fetcher: JsonFetcher, source: str, pair: str, limit: int) -> list[Any]:
    if source == "binance":
        return _fetch_binance(fetcher, pair, limit)
    if source == "okx":
        return _fetch_okx(fetcher, pair, limit)
    raise ValueError(f"unsupported crypto source: {source}")


def _normalize_rows(rows: list[Any], source: str, pair: str, now_utc: datetime) -> tuple[pd.DataFrame, dict[str, Any] | None]:
    normalized: list[dict[str, Any]] = []
    in_progress: dict[str, Any] | None = None
    now_ms = int(now_utc.timestamp() * 1000)
    for row in rows:
        if source == "binance":
            if not isinstance(row, list) or len(row) < 8:
                continue
            timestamp = int(float(row[0]))
            values = row[1:6]
            close_time = int(float(row[6]))
            turnover = row[7]
            complete = close_time < now_ms
        else:
            if not isinstance(row, list) or len(row) < 9:
                continue
            timestamp = int(float(row[0]))
            values = row[1:6]
            turnover = row[7]
            complete = str(row[8]) == "1"
            close_time = timestamp + 86_400_000 - 1
        try:
            open_price, high, low, close, volume = [float(value) for value in values]
            quote_turnover = float(turnover)
        except (TypeError, ValueError):
            continue
        item = {
            "timestamp": timestamp,
            "日期": pd.Timestamp(timestamp, unit="ms", tz="UTC").tz_convert(ASIA_SHANGHAI).date().isoformat(),
            "开盘价": open_price,
            "最高价": high,
            "最低价": low,
            "收盘价": close,
            "成交量": volume,
            "成交额": quote_turnover,
            "source": source,
            "pair": pair,
            "is_complete": bool(complete and close_time < now_ms),
        }
        if item["is_complete"]:
            normalized.append(item)
        elif in_progress is None or timestamp > int(in_progress["timestamp"]):
            item["is_complete"] = False
            in_progress = item
    frame = pd.DataFrame(normalized, columns=CANDLE_COLUMNS).sort_values("timestamp").reset_index(drop=True)
    return frame, in_progress


def _validate_frame(frame: pd.DataFrame) -> None:
    numeric = ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]
    if frame["timestamp"].duplicated().any() or not frame["timestamp"].is_monotonic_increasing:
        raise ValueError("crypto candle timestamps must be unique and ordered")
    numeric_values = frame[numeric].stack()
    if frame[numeric].isna().any().any() or not numeric_values.map(pd.api.types.is_number).all():
        raise ValueError("crypto candle values must be numeric")
    if not numeric_values.map(lambda value: math.isfinite(float(value))).all():
        raise ValueError("crypto candle values must be finite")
    if (frame[["开盘价", "最高价", "最低价", "收盘价"]] <= 0).any().any():
        raise ValueError("crypto candle prices must be positive")
    if (frame["最低价"] > frame[["开盘价", "收盘价"]].min(axis=1)).any():
        raise ValueError("crypto candle low is above open or close")
    if (frame["最高价"] < frame[["开盘价", "收盘价"]].max(axis=1)).any():
        raise ValueError("crypto candle high is below open or close")
    if (frame[["成交量", "成交额"]] < 0).any().any():
        raise ValueError("crypto candle volume cannot be negative")


def _write_cache(root: Path, symbol: str, frame: pd.DataFrame, in_progress: dict[str, Any] | None = None) -> None:
    path = root / "data" / "crypto" / "alerts" / "history" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    cached = pd.concat([frame, pd.DataFrame([in_progress])] if in_progress else [frame], ignore_index=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        cached.to_csv(handle, index=False)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _url(base: str, params: dict[str, str]) -> str:
    from urllib.parse import urlencode
    return f"{base}?{urlencode(params)}"


def _fetch_json(url: str, headers: dict[str, str]) -> Any:
    proxy = _resolve_http_proxy()
    request_options: dict[str, Any] = {"headers": headers, "timeout": 15}
    if proxy:
        request_options["proxies"] = {"http": proxy, "https": proxy}
    response = requests.get(url, **request_options)
    response.raise_for_status()
    return response.json()


def _resolve_http_proxy() -> str | None:
    """Resolve an explicit proxy first, then the macOS system HTTP proxy."""
    for name in ("CRYPTO_HTTP_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return _macos_http_proxy()


@lru_cache(maxsize=1)
def _macos_http_proxy() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values = {}
    for line in result.stdout.splitlines():
        if " : " not in line:
            continue
        key, value = line.split(" : ", 1)
        values[key.strip()] = value.strip()
    if values.get("HTTPSEnable") == "1" and values.get("HTTPSProxy") and values.get("HTTPSPort"):
        return f"http://{values['HTTPSProxy']}:{values['HTTPSPort']}"
    if values.get("HTTPEnable") == "1" and values.get("HTTPProxy") and values.get("HTTPPort"):
        return f"http://{values['HTTPProxy']}:{values['HTTPPort']}"
    return None
