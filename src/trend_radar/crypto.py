"""Multi-exchange crypto spot turnover snapshots."""

from __future__ import annotations

import json
import zipfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from time import sleep
from typing import Any

import pandas as pd
import requests

from .config import PROJECT_ROOT, ensure_data_dirs, now_shanghai


JsonFetcher = Callable[[str, dict[str, str]], Any]
BinaryFetcher = Callable[[str, dict[str, str]], bytes]

CRYPTO_TOP_TURNOVER_LIMIT = 10
CRYPTO_WINDOW_DAYS = 30
CRYPTO_STATUS_FILE = "crypto_turnover_status.json"
CRYPTO_PER_EXCHANGE_CANDIDATE_LIMIT = 60
CRYPTO_BINANCE_VISION_CANDIDATE_LIMIT = 25
CRYPTO_HTTP_TIMEOUT_SECONDS = 4
CRYPTO_TARGET_BACKOFF_DAYS = 7

CRYPTO_REFRESH_IDLE = "idle"
CRYPTO_REFRESH_RUNNING = "running"
CRYPTO_REFRESH_SUCCEEDED = "succeeded"
CRYPTO_REFRESH_FAILED = "failed"
CRYPTO_REFRESH_SKIPPED = "skipped"

BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_TICKER_24H_URL = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_VISION_DAILY_KLINE_URL = "https://data.binance.vision/data/spot/daily/klines/{symbol}/1d/{symbol}-1d-{date}.zip"
COINBASE_PRODUCTS_URL = "https://api.exchange.coinbase.com/products"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"
KRAKEN_ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
OKX_HISTORY_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_KLINES_URL = "https://api.bybit.com/v5/market/kline"

USD_QUOTE_ASSETS = frozenset(
    {
        "USD",
        "USDT",
        "USDC",
        "FDUSD",
        "TUSD",
        "BUSD",
        "DAI",
        "USDP",
        "PYUSD",
        "USDD",
        "USD1",
        "USDE",
        "USDS",
        "GUSD",
    }
)
STABLECOIN_BASE_DENYLIST = frozenset(
    {
        "USD",
        "USDT",
        "USDC",
        "FDUSD",
        "TUSD",
        "BUSD",
        "DAI",
        "USDP",
        "USDD",
        "PYUSD",
        "USD1",
        "USDE",
        "USDS",
        "FRAX",
        "LUSD",
        "GUSD",
        "UST",
        "USTC",
    }
)
KRAKEN_ASSET_ALIASES = {
    "XBT": "BTC",
    "XXBT": "BTC",
    "XETH": "ETH",
    "ZUSD": "USD",
    "ZUSDT": "USDT",
    "ZUSDC": "USDC",
}
EXCHANGE_LABELS = {
    "binance_vision": "Binance Vision",
    "binance": "Binance",
    "coinbase": "Coinbase",
    "kraken": "Kraken",
    "okx": "OKX",
    "bybit": "Bybit",
}
PREFERRED_BASE_ASSETS = [
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "BNB",
    "DOGE",
    "ADA",
    "TRX",
    "LINK",
    "AVAX",
    "SUI",
    "BCH",
    "LTC",
    "DOT",
    "UNI",
    "TON",
    "HBAR",
    "APT",
    "NEAR",
    "ICP",
    "ETC",
    "POL",
    "MATIC",
    "FIL",
    "ARB",
    "OP",
    "ATOM",
    "AAVE",
    "INJ",
    "RENDER",
    "RNDR",
    "TIA",
    "WIF",
    "PEPE",
    "SHIB",
    "FET",
    "TAO",
    "SEI",
    "ALGO",
    "VET",
    "XLM",
    "MKR",
    "JUP",
    "LDO",
    "GRT",
    "RUNE",
    "ENA",
    "BONK",
    "FLOKI",
    "PYTH",
    "WLD",
    "CRV",
    "COMP",
    "SNX",
    "SAND",
    "MANA",
    "AXS",
    "GALA",
    "APE",
]
_PREFERRED_BASE_RANK = {asset: index for index, asset in enumerate(PREFERRED_BASE_ASSETS)}
CRYPTO_COLUMNS = [
    "币种",
    "30日成交额",
    "日均成交额",
    "交易所数",
    "交易对数",
    "最大来源",
    "最大来源占比",
    "来源明细",
    "窗口开始",
    "窗口结束",
]
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class CryptoPair:
    symbol: str
    request_symbol: str
    base_asset: str
    quote_asset: str
    rank_value: float = 0.0


@dataclass(frozen=True)
class CryptoRefreshResult:
    status: str
    target_date: str
    actual_date: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None
    generated_at: str | None = None
    cache_csv: Path | None = None
    metadata_json: Path | None = None
    source: str | None = None
    row_count: int = 0
    error: str | None = None
    message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target_date": self.target_date,
            "actual_date": self.actual_date,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "generated_at": self.generated_at,
            "cache_csv": str(self.cache_csv) if self.cache_csv else None,
            "metadata_json": str(self.metadata_json) if self.metadata_json else None,
            "source": self.source,
            "row_count": self.row_count,
            "error": self.error,
            "message": self.message,
        }


def resolve_crypto_target_date(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    return (current.date() - timedelta(days=1)).isoformat()


def get_available_crypto_dates(root: Path = PROJECT_ROOT) -> list[str]:
    cache_dir = _cache_dir(root)
    if not cache_dir.exists():
        return []
    return sorted(path.stem for path in cache_dir.glob("*.csv"))


def latest_crypto_cache_date(root: Path = PROJECT_ROOT) -> str | None:
    dates = get_available_crypto_dates(root)
    return dates[-1] if dates else None


def load_crypto_snapshot(data_date: str, root: Path = PROJECT_ROOT) -> pd.DataFrame:
    path = _cache_csv_path(root, data_date)
    if not path.exists():
        return pd.DataFrame(columns=CRYPTO_COLUMNS)
    frame = pd.read_csv(path, dtype={"币种": str})
    for column in ["30日成交额", "日均成交额", "最大来源占比"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ["交易所数", "交易对数"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
    return frame.sort_values("30日成交额", ascending=False).reset_index(drop=True)


def load_crypto_metadata(data_date: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = _metadata_path(root, data_date)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_crypto_refresh_status(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = _status_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": CRYPTO_REFRESH_IDLE}
    return payload if isinstance(payload, dict) else {"status": CRYPTO_REFRESH_IDLE}


def refresh_crypto_turnover(
    root: Path = PROJECT_ROOT,
    force: bool = False,
    now: datetime | None = None,
    fetch_json: JsonFetcher | None = None,
) -> CryptoRefreshResult:
    target_date = resolve_crypto_target_date(now)
    actual_target_date = (
        _resolve_binance_vision_target_date(target_date)
        if fetch_json is None
        else target_date
    )
    ensure_data_dirs(root)
    _ensure_crypto_dirs(root)
    started_at = _timestamp()
    cache_csv = _cache_csv_path(root, actual_target_date)
    metadata_json = _metadata_path(root, actual_target_date)

    if cache_csv.exists() and not force:
        metadata = load_crypto_metadata(actual_target_date, root)
        result = CryptoRefreshResult(
            status=CRYPTO_REFRESH_SKIPPED,
            target_date=target_date,
            actual_date=actual_target_date,
            started_at=started_at,
            finished_at=_timestamp(),
            updated_at=_timestamp(),
            generated_at=str(metadata.get("generated_at") or ""),
            cache_csv=cache_csv,
            metadata_json=metadata_json,
            source=str(metadata.get("source") or "multi_exchange"),
            row_count=len(load_crypto_snapshot(actual_target_date, root)),
            message=f"当前已是最新币圈现货 {actual_target_date} 30日成交额 Top{CRYPTO_TOP_TURNOVER_LIMIT}",
        )
        _write_status(root, result)
        return result

    _write_status(
        root,
        CryptoRefreshResult(
            status=CRYPTO_REFRESH_RUNNING,
            target_date=target_date,
            actual_date=actual_target_date,
            started_at=started_at,
            updated_at=started_at,
            message=f"正在更新币圈现货 {actual_target_date} 30日成交额数据...",
        ),
    )

    try:
        frame, metadata = fetch_multi_exchange_crypto_turnover(
            root=root,
            fetch_json=fetch_json,
            target_date=actual_target_date,
            limit=CRYPTO_TOP_TURNOVER_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001 - public market source failures must be surfaced in UI
        cached_date = latest_crypto_cache_date(root)
        if cached_date:
            cached_metadata = load_crypto_metadata(cached_date, root)
            result = CryptoRefreshResult(
                status=CRYPTO_REFRESH_FAILED,
                target_date=target_date,
                actual_date=cached_date,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                generated_at=str(cached_metadata.get("generated_at") or ""),
                cache_csv=_cache_csv_path(root, cached_date),
                metadata_json=_metadata_path(root, cached_date),
                source=str(cached_metadata.get("source") or "cached"),
                row_count=len(load_crypto_snapshot(cached_date, root)),
                error=str(exc),
                message=f"币圈现货更新失败，继续展示 {cached_date} 缓存",
            )
            _write_status(root, result)
            return result
        result = CryptoRefreshResult(
            status=CRYPTO_REFRESH_FAILED,
            target_date=target_date,
            started_at=started_at,
            finished_at=_timestamp(),
            updated_at=_timestamp(),
            error=str(exc),
            message=f"{target_date} 币圈现货30日成交额更新失败",
        )
        _write_status(root, result)
        return result

    generated_at = _timestamp()
    frame = frame.copy()
    frame["数据日期"] = actual_target_date
    frame["source"] = str(metadata["source"])
    frame.to_csv(cache_csv, index=False)
    metadata = {
        **metadata,
        "target_date": target_date,
        "data_date": actual_target_date,
        "generated_at": generated_at,
        "row_count": int(len(frame)),
        "cache_csv": str(cache_csv),
    }
    metadata_json.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    result = CryptoRefreshResult(
        status=CRYPTO_REFRESH_SUCCEEDED,
        target_date=target_date,
        actual_date=actual_target_date,
        started_at=started_at,
        finished_at=_timestamp(),
        updated_at=_timestamp(),
        generated_at=generated_at,
        cache_csv=cache_csv,
        metadata_json=metadata_json,
        source=str(metadata["source"]),
        row_count=len(frame),
        message=f"已生成 {actual_target_date} 币圈现货30日成交额 Top{CRYPTO_TOP_TURNOVER_LIMIT}",
    )
    _write_status(root, result)
    return result


def fetch_multi_exchange_crypto_turnover(
    root: Path = PROJECT_ROOT,
    fetch_json: JsonFetcher | None = None,
    target_date: str | None = None,
    window_days: int = CRYPTO_WINDOW_DAYS,
    limit: int = CRYPTO_TOP_TURNOVER_LIMIT,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    del root
    loader = fetch_json or _fetch_json
    target = target_date or resolve_crypto_target_date()
    window = _window(target, window_days)
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    successful_exchanges: list[str] = []
    exchange_metadata: dict[str, Any] = {}

    fetchers = [
        ("binance", _fetch_binance_records),
        ("coinbase", _fetch_coinbase_records),
        ("kraken", _fetch_kraken_records),
        ("okx", _fetch_okx_records),
        ("bybit", _fetch_bybit_records),
    ]
    if fetch_json is None:
        fetchers.insert(0, ("binance_vision", _fetch_binance_vision_records))

    for exchange, fetcher in fetchers:
        try:
            exchange_records, metadata = fetcher(loader, window)
        except Exception as exc:  # noqa: BLE001 - each exchange is an optional public source
            failures.append({"exchange": exchange, "error": str(exc)})
            continue
        _tag_exchange_records(exchange_records, exchange)
        successful_exchanges.append(exchange)
        exchange_metadata[exchange] = metadata
        records.extend(exchange_records)
        if exchange == "binance_vision":
            frame = _aggregate_records(records, window, limit)
            if len(frame) >= limit:
                return frame, _metadata_payload(
                    window=window,
                    records=records,
                    frame=frame,
                    successful_exchanges=successful_exchanges,
                    failures=failures,
                    exchange_metadata=exchange_metadata,
                    source="binance_vision_fallback",
                    skipped_exchanges=["binance", "coinbase", "kraken", "okx", "bybit"],
                    partial=True,
                )

    frame = _aggregate_records(records, window, limit)
    if len(frame) < limit:
        failure_text = "; ".join(f"{item['exchange']}: {item['error']}" for item in failures)
        raise RuntimeError(
            f"Only {len(frame)} aggregated crypto turnover rows were available; "
            f"successful exchanges: {', '.join(successful_exchanges) or 'none'}; "
            f"failed exchanges: {failure_text or 'none'}"
        )

    metadata = _metadata_payload(
        window=window,
        records=records,
        frame=frame,
        successful_exchanges=successful_exchanges,
        failures=failures,
        exchange_metadata=exchange_metadata,
    )
    return frame, metadata


def _metadata_payload(
    *,
    window: "_Window",
    records: list[dict[str, Any]],
    frame: pd.DataFrame,
    successful_exchanges: list[str],
    failures: list[dict[str, str]],
    exchange_metadata: dict[str, Any],
    source: str = "multi_exchange",
    skipped_exchanges: list[str] | None = None,
    partial: bool | None = None,
) -> dict[str, Any]:
    source_mode = _source_mode(source, successful_exchanges)
    return {
        "source": source,
        "source_mode": source_mode,
        "window_start": window.start_date.isoformat(),
        "window_end": window.end_date.isoformat(),
        "window_days": window.days,
        "quote_assets": sorted(USD_QUOTE_ASSETS),
        "stablecoin_base_denylist": sorted(STABLECOIN_BASE_DENYLIST),
        "successful_exchanges": successful_exchanges,
        "failed_exchanges": failures,
        "skipped_exchanges": skipped_exchanges or [],
        "partial": bool(failures) if partial is None else partial,
        "input_rows": len(records),
        "aggregated_assets": int(len(frame)),
        "exchange_metadata": exchange_metadata,
    }


def _source_mode(source: str, successful_exchanges: list[str]) -> str:
    if source == "binance_plugin":
        return "binance_plugin"
    if source == "binance_vision_fallback" or set(successful_exchanges).issubset({"binance", "binance_vision"}):
        return "binance_public_fallback"
    return "multi_exchange_public"


@dataclass(frozen=True)
class _Window:
    start_date: date
    end_date: date
    start_ms: int
    end_ms: int
    start_seconds: int
    end_exclusive_seconds: int
    start_iso: str
    end_exclusive_iso: str
    days: int


def _window(target_date: str, days: int) -> _Window:
    end_date = date.fromisoformat(target_date)
    start_date = end_date - timedelta(days=days - 1)
    start_dt = datetime.combine(start_date, time.min, UTC)
    end_exclusive_dt = datetime.combine(end_date + timedelta(days=1), time.min, UTC)
    return _Window(
        start_date=start_date,
        end_date=end_date,
        start_ms=int(start_dt.timestamp() * 1000),
        end_ms=int(end_exclusive_dt.timestamp() * 1000) - 1,
        start_seconds=int(start_dt.timestamp()),
        end_exclusive_seconds=int(end_exclusive_dt.timestamp()),
        start_iso=start_dt.isoformat().replace("+00:00", "Z"),
        end_exclusive_iso=end_exclusive_dt.isoformat().replace("+00:00", "Z"),
        days=days,
    )


def _fetch_binance_records(loader: JsonFetcher, window: _Window) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = loader(BINANCE_EXCHANGE_INFO_URL, _HEADERS)
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    rank_values = _binance_rank_values(loader)
    candidates: list[CryptoPair] = []
    for item in symbols:
        if not isinstance(item, dict):
            continue
        base = _asset(item.get("baseAsset"))
        quote = _asset(item.get("quoteAsset"))
        symbol = str(item.get("symbol") or "").strip().upper()
        if item.get("status") != "TRADING" or not _binance_spot_allowed(item) or not _valid_pair(base, quote):
            continue
        candidates.append(CryptoPair(symbol=symbol, request_symbol=symbol, base_asset=base, quote_asset=quote, rank_value=rank_values.get(symbol, 0.0)))
    candidates = _limit_candidates(candidates)
    records, symbol_errors = _records_from_candidates(candidates, "binance", loader, window, _fetch_binance_pair_records)
    if not records:
        raise RuntimeError("Binance produced no turnover rows")
    return records, _exchange_metadata(symbols, candidates, records, symbol_errors)


def _fetch_binance_vision_records(loader: JsonFetcher, window: _Window) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    del loader
    candidates = [
        CryptoPair(symbol=f"{asset}USDT", request_symbol=f"{asset}USDT", base_asset=asset, quote_asset="USDT")
        for asset in PREFERRED_BASE_ASSETS[:CRYPTO_BINANCE_VISION_CANDIDATE_LIMIT]
        if asset not in {"MATIC", "RNDR"}
    ]
    records: list[dict[str, Any]] = []
    symbol_errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_binance_vision_pair_records, _fetch_bytes, candidate, window): candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                pair_records = future.result()
            except Exception as exc:  # noqa: BLE001 - one Binance Vision symbol can be delisted or missing
                symbol_errors.append(f"{candidate.symbol}: {exc}")
                continue
            if len(pair_records) >= max(20, window.days - 3):
                records.extend(pair_records)
            else:
                symbol_errors.append(f"{candidate.symbol}: only {len(pair_records)} daily rows")
    if not records:
        raise RuntimeError("Binance Vision produced no turnover rows")
    return records, _exchange_metadata(candidates, candidates, records, symbol_errors)


def _fetch_coinbase_records(loader: JsonFetcher, window: _Window) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = loader(COINBASE_PRODUCTS_URL, _HEADERS)
    rows = payload if isinstance(payload, list) else []
    candidates: list[CryptoPair] = []
    for item in rows:
        if not isinstance(item, dict) or _coinbase_disabled(item):
            continue
        base = _asset(item.get("base_currency"))
        quote = _asset(item.get("quote_currency"))
        symbol = str(item.get("id") or "").strip().upper()
        if not symbol or not _valid_pair(base, quote):
            continue
        price = _parse_float(item.get("price")) or 0.0
        volume = _parse_float(item.get("volume_24h")) or 0.0
        candidates.append(CryptoPair(symbol=symbol, request_symbol=symbol, base_asset=base, quote_asset=quote, rank_value=price * volume))
    candidates = _limit_candidates(candidates)
    records, symbol_errors = _records_from_candidates(candidates, "coinbase", loader, window, _fetch_coinbase_pair_records)
    if not records:
        raise RuntimeError("Coinbase produced no turnover rows")
    return records, _exchange_metadata(rows, candidates, records, symbol_errors)


def _fetch_kraken_records(loader: JsonFetcher, window: _Window) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = loader(KRAKEN_ASSET_PAIRS_URL, _HEADERS)
    _raise_kraken_errors(payload)
    pairs = payload.get("result", {}) if isinstance(payload, dict) else {}
    rows = list(pairs.values()) if isinstance(pairs, dict) else []
    candidates: list[CryptoPair] = []
    for pair_id, item in pairs.items() if isinstance(pairs, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "online").lower() != "online":
            continue
        parsed = _parse_kraken_pair(item)
        if parsed is None:
            continue
        base, quote = parsed
        if not _valid_pair(base, quote):
            continue
        symbol = str(item.get("wsname") or item.get("altname") or pair_id).strip().upper()
        candidates.append(CryptoPair(symbol=symbol, request_symbol=str(pair_id), base_asset=base, quote_asset=quote))
    records, symbol_errors = _records_from_candidates(candidates, "kraken", loader, window, _fetch_kraken_pair_records)
    if not records:
        raise RuntimeError("Kraken produced no turnover rows")
    return records, _exchange_metadata(rows, candidates, records, symbol_errors)


def _fetch_okx_records(loader: JsonFetcher, window: _Window) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = loader(_url_with_query(OKX_INSTRUMENTS_URL, {"instType": "SPOT"}), _HEADERS)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    rank_values = _okx_rank_values(loader)
    candidates: list[CryptoPair] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        base = _asset(item.get("baseCcy"))
        quote = _asset(item.get("quoteCcy"))
        symbol = str(item.get("instId") or "").strip().upper()
        if str(item.get("state") or "").lower() != "live" or not _valid_pair(base, quote):
            continue
        candidates.append(CryptoPair(symbol=symbol, request_symbol=symbol, base_asset=base, quote_asset=quote, rank_value=rank_values.get(symbol, 0.0)))
    candidates = _limit_candidates(candidates)
    records, symbol_errors = _records_from_candidates(candidates, "okx", loader, window, _fetch_okx_pair_records)
    if not records:
        raise RuntimeError("OKX produced no turnover rows")
    return records, _exchange_metadata(rows, candidates, records, symbol_errors)


def _fetch_bybit_records(loader: JsonFetcher, window: _Window) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _fetch_bybit_instruments(loader)
    rank_values = _bybit_rank_values(loader)
    candidates: list[CryptoPair] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        base = _asset(item.get("baseCoin"))
        quote = _asset(item.get("quoteCoin"))
        symbol = str(item.get("symbol") or "").strip().upper()
        if str(item.get("status") or "").lower() != "trading" or not _valid_pair(base, quote):
            continue
        candidates.append(CryptoPair(symbol=symbol, request_symbol=symbol, base_asset=base, quote_asset=quote, rank_value=rank_values.get(symbol, 0.0)))
    candidates = _limit_candidates(candidates)
    records, symbol_errors = _records_from_candidates(candidates, "bybit", loader, window, _fetch_bybit_pair_records)
    if not records:
        raise RuntimeError("Bybit produced no turnover rows")
    return records, _exchange_metadata(rows, candidates, records, symbol_errors)


def _records_from_candidates(
    candidates: list[CryptoPair],
    exchange: str,
    loader: JsonFetcher,
    window: _Window,
    pair_fetcher: Callable[[JsonFetcher, CryptoPair, _Window], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in candidates:
        try:
            records.extend(pair_fetcher(loader, candidate, window))
        except Exception as exc:  # noqa: BLE001 - one bad symbol should not remove the exchange
            errors.append(f"{candidate.symbol}: {exc}")
    for record in records:
        record["exchange"] = exchange
    return records, errors


def _fetch_binance_pair_records(loader: JsonFetcher, pair: CryptoPair, window: _Window) -> list[dict[str, Any]]:
    payload = loader(
        _url_with_query(
            BINANCE_KLINES_URL,
            {
                "symbol": pair.request_symbol,
                "interval": "1d",
                "startTime": window.start_ms,
                "endTime": window.end_ms,
                "limit": window.days,
            },
        ),
        _HEADERS,
    )
    rows = payload if isinstance(payload, list) else []
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= 7:
            continue
        amount = _parse_float(row[7])
        item_date = _date_from_ms(row[0])
        if amount is None or not _date_in_window(item_date, window):
            continue
        records.append(_record(pair, item_date, amount, "quote_asset_volume"))
    return records


def _fetch_coinbase_pair_records(loader: JsonFetcher, pair: CryptoPair, window: _Window) -> list[dict[str, Any]]:
    url = _url_with_query(
        COINBASE_CANDLES_URL.format(product_id=urllib.parse.quote(pair.request_symbol, safe="")),
        {"granularity": 86400, "start": window.start_iso, "end": window.end_exclusive_iso},
    )
    payload = loader(url, _HEADERS)
    rows = payload if isinstance(payload, list) else []
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        item_date = _date_from_seconds(row[0])
        low = _parse_float(row[1])
        high = _parse_float(row[2])
        open_price = _parse_float(row[3])
        close = _parse_float(row[4])
        volume = _parse_float(row[5])
        if None in {low, high, open_price, close, volume} or not _date_in_window(item_date, window):
            continue
        amount = float(volume) * ((float(open_price) + float(high) + float(low) + float(close)) / 4)
        records.append(_record(pair, item_date, amount, "volume_x_ohlc4"))
    return records


def _fetch_kraken_pair_records(loader: JsonFetcher, pair: CryptoPair, window: _Window) -> list[dict[str, Any]]:
    payload = loader(
        _url_with_query(KRAKEN_OHLC_URL, {"pair": pair.request_symbol, "interval": 1440, "since": window.start_seconds}),
        _HEADERS,
    )
    _raise_kraken_errors(payload)
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    rows = _first_kraken_ohlc_rows(result)
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        item_date = _date_from_seconds(row[0])
        vwap = _parse_float(row[5])
        volume = _parse_float(row[6])
        if vwap is None or volume is None or not _date_in_window(item_date, window):
            continue
        records.append(_record(pair, item_date, vwap * volume, "volume_x_vwap"))
    return records


def _fetch_okx_pair_records(loader: JsonFetcher, pair: CryptoPair, window: _Window) -> list[dict[str, Any]]:
    payload = loader(
        _url_with_query(OKX_HISTORY_CANDLES_URL, {"instId": pair.request_symbol, "bar": "1Dutc", "limit": 100}),
        _HEADERS,
    )
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= 7:
            continue
        if len(row) > 8 and str(row[8]) not in {"1", "true", "True"}:
            continue
        item_date = _date_from_ms(row[0])
        amount = _parse_float(row[7])
        if amount is None or not _date_in_window(item_date, window):
            continue
        records.append(_record(pair, item_date, amount, "volCcyQuote"))
    return records


def _fetch_bybit_pair_records(loader: JsonFetcher, pair: CryptoPair, window: _Window) -> list[dict[str, Any]]:
    payload = loader(
        _url_with_query(
            BYBIT_KLINES_URL,
            {
                "category": "spot",
                "symbol": pair.request_symbol,
                "interval": "D",
                "start": window.start_ms,
                "end": window.end_ms,
                "limit": window.days,
            },
        ),
        _HEADERS,
    )
    rows = payload.get("result", {}).get("list", []) if isinstance(payload, dict) else []
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= 6:
            continue
        item_date = _date_from_ms(row[0])
        amount = _parse_float(row[6])
        if amount is None or not _date_in_window(item_date, window):
            continue
        records.append(_record(pair, item_date, amount, "turnover"))
    return records


def _fetch_binance_vision_pair_records(fetcher: BinaryFetcher, pair: CryptoPair, window: _Window) -> list[dict[str, Any]]:
    dates = [window.start_date + timedelta(days=offset) for offset in range(window.days)]
    records: list[dict[str, Any]] = []
    for item_date in dates:
        url = _binance_vision_daily_url(pair.request_symbol, item_date)
        try:
            payload = fetcher(url, _HEADERS)
        except Exception:
            continue
        records.extend(_parse_binance_vision_zip(pair, payload, window))
    return records


def _parse_binance_vision_zip(pair: CryptoPair, payload: bytes, window: _Window) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        for name in archive.namelist():
            if not name.endswith(".csv"):
                continue
            text = archive.read(name).decode("utf-8").strip()
            for line in text.splitlines():
                columns = line.split(",")
                if len(columns) <= 7 or columns[0] == "open_time":
                    continue
                item_date = _date_from_epoch_value(columns[0])
                amount = _parse_float(columns[7])
                if amount is None or not _date_in_window(item_date, window):
                    continue
                rows.append(_record(pair, item_date, amount, "binance_vision_quote_asset_volume"))
    return rows


def _record(pair: CryptoPair, item_date: date, amount: float, method: str) -> dict[str, Any]:
    return {
        "symbol": pair.symbol,
        "base_asset": pair.base_asset,
        "quote_asset": pair.quote_asset,
        "date": item_date.isoformat(),
        "quote_turnover_usd": float(amount),
        "method": method,
    }


def _tag_exchange_records(records: list[dict[str, Any]], exchange: str) -> None:
    for record in records:
        record.setdefault("exchange", exchange)


def _aggregate_records(records: list[dict[str, Any]], window: _Window, limit: int) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=CRYPTO_COLUMNS)
    frame = pd.DataFrame(records)
    required = {"exchange", "symbol", "base_asset", "quote_turnover_usd"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=CRYPTO_COLUMNS)
    frame["quote_turnover_usd"] = pd.to_numeric(frame["quote_turnover_usd"], errors="coerce")
    frame = frame.dropna(subset=["quote_turnover_usd"])
    frame = frame[frame["quote_turnover_usd"] > 0].copy()
    if frame.empty:
        return pd.DataFrame(columns=CRYPTO_COLUMNS)
    frame["exchange_symbol"] = frame["exchange"].astype(str) + ":" + frame["symbol"].astype(str)
    rows = []
    for asset, group in frame.groupby("base_asset"):
        total = float(group["quote_turnover_usd"].sum())
        by_exchange = group.groupby("exchange")["quote_turnover_usd"].sum().sort_values(ascending=False)
        max_exchange = str(by_exchange.index[0])
        rows.append(
            {
                "币种": asset,
                "30日成交额": round(total, 2),
                "日均成交额": round(total / window.days, 2),
                "交易所数": int(group["exchange"].nunique()),
                "交易对数": int(group["exchange_symbol"].nunique()),
                "最大来源": EXCHANGE_LABELS.get(max_exchange, max_exchange),
                "最大来源占比": round(float(by_exchange.iloc[0]) / total * 100, 2) if total else 0.0,
                "来源明细": _source_detail(by_exchange, total),
                "窗口开始": window.start_date.isoformat(),
                "窗口结束": window.end_date.isoformat(),
            }
        )
    return pd.DataFrame(rows, columns=CRYPTO_COLUMNS).sort_values("30日成交额", ascending=False).head(limit).reset_index(drop=True)


def _source_detail(by_exchange: pd.Series, total: float) -> str:
    if total <= 0:
        return "-"
    parts = []
    for exchange, amount in by_exchange.items():
        label = EXCHANGE_LABELS.get(str(exchange), str(exchange))
        parts.append(f"{label} {float(amount) / total * 100:.1f}%")
    return " / ".join(parts)


def _binance_rank_values(loader: JsonFetcher) -> dict[str, float]:
    try:
        payload = loader(BINANCE_TICKER_24H_URL, _HEADERS)
    except Exception:  # noqa: BLE001 - ranking is only a performance hint
        return {}
    rows = payload if isinstance(payload, list) else []
    return {str(row.get("symbol") or "").upper(): _parse_float(row.get("quoteVolume")) or 0.0 for row in rows if isinstance(row, dict)}


def _okx_rank_values(loader: JsonFetcher) -> dict[str, float]:
    try:
        payload = loader(_url_with_query(OKX_TICKERS_URL, {"instType": "SPOT"}), _HEADERS)
    except Exception:  # noqa: BLE001 - ranking is only a performance hint
        return {}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return {
        str(row.get("instId") or "").upper(): (_parse_float(row.get("volCcy24h")) or _parse_float(row.get("vol24h")) or 0.0)
        for row in rows
        if isinstance(row, dict)
    }


def _bybit_rank_values(loader: JsonFetcher) -> dict[str, float]:
    try:
        payload = loader(_url_with_query(BYBIT_TICKERS_URL, {"category": "spot"}), _HEADERS)
    except Exception:  # noqa: BLE001 - ranking is only a performance hint
        return {}
    rows = payload.get("result", {}).get("list", []) if isinstance(payload, dict) else []
    return {str(row.get("symbol") or "").upper(): _parse_float(row.get("turnover24h")) or 0.0 for row in rows if isinstance(row, dict)}


def _fetch_bybit_instruments(loader: JsonFetcher) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {"category": "spot", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = loader(_url_with_query(BYBIT_INSTRUMENTS_URL, params), _HEADERS)
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        batch = result.get("list", [])
        if isinstance(batch, list):
            rows.extend(row for row in batch if isinstance(row, dict))
        next_cursor = str(result.get("nextPageCursor") or "")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return rows


def _limit_candidates(candidates: list[CryptoPair]) -> list[CryptoPair]:
    if not candidates:
        return []
    if not any(candidate.rank_value > 0 for candidate in candidates):
        preferred = [
            candidate
            for candidate in candidates
            if candidate.base_asset in _PREFERRED_BASE_RANK
        ]
        ordered = sorted(
            preferred or candidates,
            key=lambda item: (
                _PREFERRED_BASE_RANK.get(item.base_asset, len(_PREFERRED_BASE_RANK)),
                item.quote_asset != "USD",
                item.quote_asset != "USDT",
                item.symbol,
            ),
        )
        return ordered[:CRYPTO_PER_EXCHANGE_CANDIDATE_LIMIT]
    return sorted(candidates, key=lambda item: item.rank_value, reverse=True)[:CRYPTO_PER_EXCHANGE_CANDIDATE_LIMIT]


def _exchange_metadata(
    raw_rows: list[Any],
    candidates: list[CryptoPair],
    records: list[dict[str, Any]],
    symbol_errors: list[str],
) -> dict[str, Any]:
    return {
        "input_rows": len(raw_rows),
        "candidate_rows": len(candidates),
        "record_rows": len(records),
        "symbol_error_count": len(symbol_errors),
        "symbol_errors": symbol_errors[:20],
    }


def _binance_spot_allowed(item: dict[str, Any]) -> bool:
    if item.get("isSpotTradingAllowed") is True:
        return True
    permissions = item.get("permissions")
    return isinstance(permissions, list) and "SPOT" in permissions


def _coinbase_disabled(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "online").lower()
    disabled = str(item.get("trading_disabled") or "").lower()
    return status not in {"online", ""} or disabled in {"true", "1"}


def _parse_kraken_pair(item: dict[str, Any]) -> tuple[str, str] | None:
    wsname = str(item.get("wsname") or "").strip().upper()
    if "/" in wsname:
        base, quote = wsname.split("/", 1)
        return _kraken_asset(base), _kraken_asset(quote)
    base = item.get("base")
    quote = item.get("quote")
    if base and quote:
        return _kraken_asset(base), _kraken_asset(quote)
    return None


def _kraken_asset(value: object) -> str:
    asset = _asset(value)
    if asset in KRAKEN_ASSET_ALIASES:
        return KRAKEN_ASSET_ALIASES[asset]
    if len(asset) > 3 and asset[0] in {"X", "Z"}:
        stripped = asset[1:]
        return KRAKEN_ASSET_ALIASES.get(stripped, stripped)
    return asset


def _valid_pair(base: str, quote: str) -> bool:
    return bool(base and quote and quote in USD_QUOTE_ASSETS and base not in STABLECOIN_BASE_DENYLIST and base != quote)


def _asset(value: object) -> str:
    return str(value or "").strip().upper()


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _date_from_ms(value: object) -> date:
    return datetime.fromtimestamp(float(value) / 1000, UTC).date()


def _date_from_seconds(value: object) -> date:
    return datetime.fromtimestamp(float(value), UTC).date()


def _date_from_epoch_value(value: object) -> date:
    numeric = float(value)
    if numeric >= 1_000_000_000_000_000:
        numeric = numeric / 1_000_000
    elif numeric >= 1_000_000_000_000:
        numeric = numeric / 1_000
    return datetime.fromtimestamp(numeric, UTC).date()


def _date_in_window(item_date: date, window: _Window) -> bool:
    return window.start_date <= item_date <= window.end_date


def _raise_kraken_errors(payload: Any) -> None:
    errors = payload.get("error", []) if isinstance(payload, dict) else []
    if errors:
        raise RuntimeError("; ".join(str(error) for error in errors))


def _first_kraken_ohlc_rows(result: Any) -> list[Any]:
    if not isinstance(result, dict):
        return []
    for key, value in result.items():
        if key != "last" and isinstance(value, list):
            return value
    return []


def _url_with_query(url: str, params: dict[str, Any]) -> str:
    return f"{url}?{urllib.parse.urlencode(params)}"


def _binance_vision_daily_url(symbol: str, item_date: date) -> str:
    return BINANCE_VISION_DAILY_KLINE_URL.format(symbol=symbol, date=item_date.isoformat())


def _resolve_binance_vision_target_date(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    for offset in range(CRYPTO_TARGET_BACKOFF_DAYS + 1):
        candidate = target - timedelta(days=offset)
        if _binance_vision_date_available("BTCUSDT", candidate):
            return candidate.isoformat()
    return target_date


def _binance_vision_date_available(symbol: str, item_date: date) -> bool:
    url = _binance_vision_daily_url(symbol, item_date)
    try:
        _fetch_head(url, _HEADERS)
    except Exception:
        return False
    return True


def _fetch_json(
    url: str,
    headers: dict[str, str],
    timeout: int = CRYPTO_HTTP_TIMEOUT_SECONDS,
    retries: int = 1,
    retry_delay: float = 0.8,
) -> Any:
    attempts = max(1, retries)
    errors: list[str] = []
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = _request("GET", url, headers, timeout)
            return response.json()
        except Exception as exc:  # noqa: BLE001 - remote source retry must preserve all failures
            last_exc = exc
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < attempts:
                sleep(retry_delay)
    raise RuntimeError(f"crypto market request failed after {attempts} attempts: {'; '.join(errors)}") from last_exc


def _fetch_bytes(
    url: str,
    headers: dict[str, str],
    timeout: int = CRYPTO_HTTP_TIMEOUT_SECONDS,
    retries: int = 2,
    retry_delay: float = 0.4,
) -> bytes:
    attempts = max(1, retries)
    errors: list[str] = []
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = _request("GET", url, headers, timeout)
            return response.content
        except Exception as exc:  # noqa: BLE001 - CDN fetch failures are retried and then summarized
            last_exc = exc
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < attempts:
                sleep(retry_delay)
    raise RuntimeError(f"binary market request failed after {attempts} attempts: {'; '.join(errors)}") from last_exc


def _fetch_head(url: str, headers: dict[str, str], timeout: int = CRYPTO_HTTP_TIMEOUT_SECONDS) -> None:
    _request("HEAD", url, headers, timeout)


def _request(method: str, url: str, headers: dict[str, str], timeout: int) -> requests.Response:
    session = requests.Session()
    session.trust_env = False
    response = session.request(method, url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response


def _ensure_crypto_dirs(root: Path) -> None:
    _cache_dir(root).mkdir(parents=True, exist_ok=True)
    (root / "data" / "state").mkdir(parents=True, exist_ok=True)


def _cache_dir(root: Path) -> Path:
    return root / "data" / "crypto" / "top_turnover_30d"


def _cache_csv_path(root: Path, data_date: str) -> Path:
    return _cache_dir(root) / f"{data_date}.csv"


def _metadata_path(root: Path, data_date: str) -> Path:
    return _cache_dir(root) / f"{data_date}.json"


def _status_path(root: Path) -> Path:
    return root / "data" / "state" / CRYPTO_STATUS_FILE


def _write_status(root: Path, result: CryptoRefreshResult) -> None:
    _status_path(root).write_text(json.dumps(result.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8")


def _timestamp() -> str:
    return now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
