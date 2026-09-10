"""US stock turnover snapshots from public US market scanner data."""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from time import sleep
from typing import Any
from zoneinfo import ZoneInfo

import certifi
import pandas as pd

from .config import PROJECT_ROOT, ensure_data_dirs, now_shanghai
from .us_stock_anomalies import evaluate_anomalies


NEW_YORK = ZoneInfo("America/New_York")
US_MARKET_COMPLETE_TIME = time(16, 15)
US_STOCK_STATUS_FILE = "us_stock_turnover_status.json"

US_REFRESH_IDLE = "idle"
US_REFRESH_RUNNING = "running"
US_REFRESH_SUCCEEDED = "succeeded"
US_REFRESH_FAILED = "failed"
US_REFRESH_SKIPPED = "skipped"

TRADINGVIEW_SCANNER_URL = "https://scanner.tradingview.com/america/scan"
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=8000&offset=0&download=true"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d&events=div%2Csplits"
US_TOP_TURNOVER_LIMIT = 30
US_NAME_MAP_FILE = "name_map.csv"
US_STOCK_COLUMNS = ["代码", "名称", "英文名称", "最新价", "涨跌幅", "涨跌额", "成交额", "市值"]

TRADINGVIEW_COLUMNS = [
    "name",
    "description",
    "type",
    "typespecs",
    "exchange",
    "close",
    "change",
    "change_abs",
    "Value.Traded",
    "market_cap_basic",
    "currency",
    "country",
]
TRADINGVIEW_PAYLOAD = {
    "columns": TRADINGVIEW_COLUMNS,
    "filter": [
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
    ],
    "options": {"lang": "en"},
    "range": [0, 80],
    "sort": {"sortBy": "Value.Traded", "sortOrder": "desc"},
    "markets": ["america"],
}
_TRADINGVIEW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
_NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
}
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

US_CN_NAME_SEED = {
    "AAOI": "应用光电",
    "BAC": "美国银行",
    "BE": "布鲁姆能源",
    "MU": "美光科技",
    "NVDA": "英伟达",
    "MRVL": "迈威尔科技",
    "AVGO": "博通",
    "GOOGL": "谷歌-A",
    "GOOG": "谷歌-C",
    "MSFT": "微软",
    "TSLA": "特斯拉",
    "AAPL": "苹果",
    "AMD": "美国超微公司",
    "SNDK": "闪迪",
    "INTC": "英特尔",
    "AMZN": "亚马逊",
    "META": "脸书母公司",
    "HPE": "慧与科技",
    "LITE": "鲁门特姆",
    "ORCL": "甲骨文",
    "DELL": "戴尔科技",
    "PLTR": "帕兰蒂尔",
    "ARM": "安谋控股",
    "PANW": "帕洛阿尔托网络",
    "IBM": "国际商业机器",
    "NOW": "赛维诺",
    "TSM": "台积电",
    "QCOM": "高通",
    "MSTR": "策略公司（原微策略）",
    "CRWV": "科尔维夫",
    "CRM": "赛富时",
    "NFLX": "奈飞",
    "CSCO": "思科",
    "WMT": "沃尔玛",
    "COHR": "高意",
    "SNOW": "雪花公司",
    "AMAT": "应用材料",
    "ASML": "阿斯麦",
    "BRK.B": "伯克希尔哈撒韦-B",
    "CRWD": "众击",
    "INHD": "英诺控股",
    "KLAC": "科磊",
    "LLY": "礼来",
    "LRCX": "泛林集团",
    "NBIS": "涅比乌斯集团",
    "UNH": "联合健康",
    "SKHY": "SK海力士",
    "SPCX": "太空探索技术公司",
    "STX": "希捷科技",
    "VRT": "维谛技术",
    "WDC": "西部数据",
}


@dataclass(frozen=True)
class UsStockRefreshResult:
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


def resolve_us_stock_target_date(now: datetime | None = None) -> str:
    current = now.astimezone(NEW_YORK) if now else datetime.now(NEW_YORK)
    target = current.date()
    if target.weekday() >= 5 or current.time() < US_MARKET_COMPLETE_TIME:
        target = target - timedelta(days=1)
    while target.weekday() >= 5:
        target = target - timedelta(days=1)
    return target.isoformat()


def get_available_us_stock_dates(root: Path = PROJECT_ROOT) -> list[str]:
    cache_dir = _cache_dir(root)
    if not cache_dir.exists():
        return []
    return sorted(path.stem for path in cache_dir.glob("*.csv"))


def load_us_name_map(root: Path = PROJECT_ROOT) -> dict[str, str]:
    mapping: dict[str, str] = {}
    path = _name_map_path(root)
    if path.exists():
        frame = pd.read_csv(path, dtype={"代码": str})
        for _, row in frame.iterrows():
            code = str(row.get("代码") or "").strip().upper()
            name = str(row.get("中文名称") or "").strip()
            if code and name:
                mapping[code] = name
    for code, name in US_CN_NAME_SEED.items():
        mapping.setdefault(code, name)
    return mapping


def load_us_stock_snapshot(data_date: str, root: Path = PROJECT_ROOT) -> pd.DataFrame:
    path = _cache_csv_path(root, data_date)
    if not path.exists():
        return pd.DataFrame(columns=US_STOCK_COLUMNS)
    frame = pd.read_csv(path, dtype={"代码": str})
    if "英文名称" not in frame:
        frame["英文名称"] = frame.get("名称", "")
    name_map = load_us_name_map(root)
    if "名称" in frame:
        frame["名称"] = frame.apply(
            lambda row: name_map.get(str(row.get("代码") or "").strip().upper(), str(row.get("名称") or "")),
            axis=1,
        )
    for column in ["最新价", "涨跌幅", "涨跌额", "成交额", "市值"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values("成交额", ascending=False).reset_index(drop=True)


def load_us_stock_metadata(data_date: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = _metadata_path(root, data_date)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_us_stock_refresh_status(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = _status_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": US_REFRESH_IDLE}
    return payload if isinstance(payload, dict) else {"status": US_REFRESH_IDLE}


def refresh_us_stock_turnover(
    root: Path = PROJECT_ROOT,
    force: bool = False,
    now: datetime | None = None,
    fetch_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]] | None = None,
    history_fetch_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]] | None = None,
) -> UsStockRefreshResult:
    target_date = resolve_us_stock_target_date(now)
    ensure_data_dirs(root)
    _ensure_us_stock_dirs(root)
    started_at = _timestamp()
    cache_csv = _cache_csv_path(root, target_date)
    metadata_json = _metadata_path(root, target_date)

    if cache_csv.exists() and not force:
        cached_frame = load_us_stock_snapshot(target_date, root)
        if "异动等级" not in cached_frame.columns:
            cached_frame = _enrich_us_stock_anomalies(
                cached_frame, root, target_date, fetch_json or _fetch_json, history_fetch_json
            )
            cached_frame.to_csv(cache_csv, index=False)
        metadata = load_us_stock_metadata(target_date, root)
        result = UsStockRefreshResult(
            status=US_REFRESH_SKIPPED,
            target_date=target_date,
            actual_date=target_date,
            started_at=started_at,
            finished_at=_timestamp(),
            updated_at=_timestamp(),
            generated_at=str(metadata.get("generated_at") or ""),
            cache_csv=cache_csv,
            metadata_json=metadata_json,
            source=str(metadata.get("source") or "tradingview"),
            row_count=len(load_us_stock_snapshot(target_date, root)),
            message=f"当前已是最新美股成交额 {target_date} 数据",
        )
        _write_status(root, result)
        return result

    _write_status(
        root,
        UsStockRefreshResult(
            status=US_REFRESH_RUNNING,
            target_date=target_date,
            started_at=started_at,
            updated_at=started_at,
            message=f"正在更新美股成交额 {target_date} 数据",
        ),
    )

    try:
        frame, metadata = fetch_us_top_turnover(root=root, fetch_json=fetch_json)
    except Exception as exc:  # noqa: BLE001 - external source status must preserve failures for UI
        cached_date = latest_us_stock_cache_date(root)
        if cached_date:
            cached_metadata = load_us_stock_metadata(cached_date, root)
            result = UsStockRefreshResult(
                status=US_REFRESH_FAILED,
                target_date=target_date,
                actual_date=cached_date,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                generated_at=str(cached_metadata.get("generated_at") or ""),
                cache_csv=_cache_csv_path(root, cached_date),
                metadata_json=_metadata_path(root, cached_date),
                source=str(cached_metadata.get("source") or "cached"),
                row_count=len(load_us_stock_snapshot(cached_date, root)),
                error=str(exc),
                message=f"美股成交额更新失败，继续展示 {cached_date} 缓存",
            )
            _write_status(root, result)
            return result
        result = UsStockRefreshResult(
            status=US_REFRESH_FAILED,
            target_date=target_date,
            started_at=started_at,
            finished_at=_timestamp(),
            updated_at=_timestamp(),
            error=str(exc),
            message=f"{target_date} 美股成交额更新失败",
        )
        _write_status(root, result)
        return result

    generated_at = _timestamp()
    frame = frame.copy()
    frame = _enrich_us_stock_anomalies(
        frame, root, target_date, fetch_json or _fetch_json, history_fetch_json
    )
    frame["数据日期"] = target_date
    frame["source"] = str(metadata["source"])
    frame["数据生成时间"] = generated_at
    frame.to_csv(cache_csv, index=False)
    metadata = {
        **metadata,
        "data_date": target_date,
        "generated_at": generated_at,
        "row_count": int(len(frame)),
        "cache_csv": str(cache_csv),
    }
    metadata_json.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    result = UsStockRefreshResult(
        status=US_REFRESH_SUCCEEDED,
        target_date=target_date,
        actual_date=target_date,
        started_at=started_at,
        finished_at=_timestamp(),
        updated_at=_timestamp(),
        generated_at=generated_at,
        cache_csv=cache_csv,
        metadata_json=metadata_json,
        source=str(metadata["source"]),
        row_count=len(frame),
        message=f"已生成 {target_date} 美股成交额 Top{US_TOP_TURNOVER_LIMIT}",
    )
    _write_status(root, result)
    return result


def latest_us_stock_cache_date(root: Path = PROJECT_ROOT) -> str | None:
    dates = get_available_us_stock_dates(root)
    return dates[-1] if dates else None


def _enrich_us_stock_anomalies(
    frame: pd.DataFrame,
    root: Path,
    target_date: str,
    fetch_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]],
    history_fetch_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]] | None = None,
) -> pd.DataFrame:
    history_loader = history_fetch_json or (fetch_json if fetch_json is _fetch_json else None)
    histories: dict[str, pd.DataFrame] = {}
    for symbol in frame["代码"].astype(str).str.upper():
        history = _load_cached_us_stock_history(root, symbol)
        if len(history) < 20 and history_loader is not None:
            try:
                fetched = _fetch_yahoo_stock_history(symbol, history_loader)
                if len(fetched) >= len(history):
                    history = fetched
                    _write_cached_us_stock_history(root, symbol, history)
            except Exception:  # noqa: BLE001 - missing history is a visible data state
                pass
        histories[symbol] = history[history["日期"] < target_date].tail(20) if not history.empty else history
    previous = _previous_cached_snapshot(root, target_date)
    return evaluate_anomalies(frame, histories, previous)


def _previous_cached_snapshot(root: Path, target_date: str) -> pd.DataFrame | None:
    dates = [date for date in get_available_us_stock_dates(root) if date < target_date]
    return load_us_stock_snapshot(dates[-1], root) if dates else None


def _load_cached_us_stock_history(root: Path, symbol: str) -> pd.DataFrame:
    path = _history_path(root, symbol)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return pd.DataFrame(columns=["日期", "收盘价", "成交量", "成交额"])
    frame = pd.DataFrame(payload if isinstance(payload, list) else [])
    for column in ["收盘价", "成交量", "成交额"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame if {"日期", "成交额"}.issubset(frame.columns) else pd.DataFrame(columns=["日期", "成交额"])


def _write_cached_us_stock_history(root: Path, symbol: str, frame: pd.DataFrame) -> None:
    _history_path(root, symbol).write_text(
        json.dumps(frame.to_dict(orient="records"), ensure_ascii=False), encoding="utf-8"
    )


def _fetch_yahoo_stock_history(
    symbol: str,
    fetch_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]],
) -> pd.DataFrame:
    payload = fetch_json(YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol)), _YAHOO_HEADERS, {})
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return pd.DataFrame(columns=["日期", "收盘价", "成交量", "成交额"])
    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    records = []
    for timestamp, close, volume in zip(timestamps, closes, volumes):
        if close is None or volume is None:
            continue
        date = datetime.fromtimestamp(int(timestamp), NEW_YORK).date().isoformat()
        records.append({"日期": date, "收盘价": float(close), "成交量": float(volume), "成交额": float(close) * float(volume)})
    return pd.DataFrame(records, columns=["日期", "收盘价", "成交量", "成交额"])


def fetch_us_top_turnover(
    root: Path = PROJECT_ROOT,
    fetch_json: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]] | None = None,
    limit: int = US_TOP_TURNOVER_LIMIT,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    loader = fetch_json or _fetch_json
    fallback_errors: list[str] = []
    for source, source_fetcher in [
        ("tradingview", _fetch_tradingview_top_turnover),
        ("nasdaq", _fetch_nasdaq_top_turnover),
    ]:
        try:
            frame, metadata = source_fetcher(root, loader, limit)
        except Exception as exc:  # noqa: BLE001 - each public data source is an optional fallback
            fallback_errors.append(f"{source}: {exc}")
            continue
        if fallback_errors:
            metadata["fallback_errors"] = fallback_errors
        metadata["attempted_sources"] = [*[_source_name(error) for error in fallback_errors], source]
        return frame, metadata
    raise RuntimeError(f"All US stock turnover sources failed: {'; '.join(fallback_errors)}")


def _fetch_tradingview_top_turnover(
    root: Path,
    loader: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]],
    limit: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    payload = loader(TRADINGVIEW_SCANNER_URL, _TRADINGVIEW_HEADERS, TRADINGVIEW_PAYLOAD)
    rows = payload.get("data", [])
    name_map = load_us_name_map(root)
    frame, missing_codes = _normalize_tradingview_rows(rows, name_map, limit)
    metadata = {
        "source": "tradingview",
        "provider": "tradingview_scanner_value_traded",
        "input_rows": len(rows) if isinstance(rows, list) else 0,
        "filtered_rows": int(len(frame)),
        "missing_cn_name_codes": missing_codes,
    }
    return frame, metadata


def _fetch_nasdaq_top_turnover(
    root: Path,
    loader: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]],
    limit: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    payload = loader(NASDAQ_SCREENER_URL, _NASDAQ_HEADERS, {})
    rows = _extract_nasdaq_rows(payload)
    name_map = load_us_name_map(root)
    frame, missing_codes = _normalize_nasdaq_rows(rows, name_map, limit)
    metadata = {
        "source": "nasdaq",
        "provider": "nasdaq_screener_download",
        "input_rows": len(rows),
        "filtered_rows": int(len(frame)),
        "missing_cn_name_codes": missing_codes,
        "turnover_method": "lastsale_x_volume",
    }
    return frame, metadata


def _normalize_tradingview_rows(
    rows: Any, name_map: dict[str, str], limit: int
) -> tuple[pd.DataFrame, list[str]]:
    records: list[dict[str, Any]] = []
    missing_codes: list[str] = []
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("d"), list):
            continue
        item = _row_dict(row["d"])
        symbol = str(item.get("name") or "").strip().upper()
        english_name = str(item.get("description") or symbol).strip()
        specs = item.get("typespecs") if isinstance(item.get("typespecs"), list) else []
        amount = _parse_number(item.get("Value.Traded"))
        price = _parse_number(item.get("close"))
        if not symbol or "common" not in specs or amount is None or amount <= 0 or price is None:
            continue
        display_name = name_map.get(symbol)
        if not display_name:
            display_name = english_name
            missing_codes.append(symbol)
        records.append(
            {
                "代码": symbol,
                "名称": display_name,
                "英文名称": english_name,
                "最新价": price,
                "涨跌幅": _parse_number(item.get("change")),
                "涨跌额": _parse_number(item.get("change_abs")),
                "成交额": amount,
                "市值": _parse_number(item.get("market_cap_basic")),
            }
        )
    frame = _top_turnover_frame(records, limit, "direct US stock")
    present_codes = set(frame["代码"].tolist())
    return frame, [code for code in missing_codes if code in present_codes]


def _extract_nasdaq_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return [row for row in data["rows"] if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _normalize_nasdaq_rows(rows: list[dict[str, Any]], name_map: dict[str, str], limit: int) -> tuple[pd.DataFrame, list[str]]:
    records: list[dict[str, Any]] = []
    missing_codes: list[str] = []
    for row in rows:
        symbol = _normalize_symbol(row.get("symbol"))
        english_name = str(row.get("name") or symbol).strip()
        if not symbol or not _is_supported_us_stock_name(english_name):
            continue
        price = _parse_number(row.get("lastsale"))
        volume = _parse_number(row.get("volume"))
        if price is None or price <= 0 or volume is None or volume <= 0:
            continue
        display_name = name_map.get(symbol)
        if not display_name:
            display_name = english_name
            missing_codes.append(symbol)
        records.append(
            {
                "代码": symbol,
                "名称": display_name,
                "英文名称": english_name,
                "最新价": price,
                "涨跌幅": _parse_number(row.get("pctchange")),
                "涨跌额": _parse_number(row.get("netchange")),
                "成交额": price * volume,
                "市值": _parse_number(row.get("marketCap")),
            }
        )
    frame = _top_turnover_frame(records, limit, "Nasdaq computed US stock")
    present_codes = set(frame["代码"].tolist())
    return frame, [code for code in missing_codes if code in present_codes]


def _row_dict(values: list[Any]) -> dict[str, Any]:
    return {column: values[index] if index < len(values) else None for index, column in enumerate(TRADINGVIEW_COLUMNS)}


def _top_turnover_frame(records: list[dict[str, Any]], limit: int, source_label: str) -> pd.DataFrame:
    frame = pd.DataFrame(records)
    if frame.empty:
        raise RuntimeError(f"No {source_label} turnover rows were available")
    frame = frame[US_STOCK_COLUMNS].sort_values("成交额", ascending=False).head(limit).reset_index(drop=True)
    if len(frame) < limit:
        raise RuntimeError(f"Only {len(frame)} {source_label} turnover rows were available")
    return frame


def _normalize_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _is_supported_us_stock_name(name: str) -> bool:
    normalized = name.lower()
    excluded_terms = [
        " warrant",
        " warrants",
        " right",
        " rights",
        " unit",
        " units",
        " preferred",
        " preference",
        " notes",
        " note due",
        " bond",
        " debenture",
        " etf",
        " etn",
        " fund",
    ]
    return not any(term in f" {normalized}" for term in excluded_terms)


def _source_name(error_text: str) -> str:
    return error_text.split(":", 1)[0]


def _parse_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL", "-"}:
        return None
    text = text.replace("$", "").replace("%", "").replace(",", "").strip()
    if text.startswith("+"):
        text = text[1:]
    try:
        return float(text)
    except ValueError:
        return None


def _fetch_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int = 20,
    retries: int = 3,
    retry_delay: float = 0.8,
) -> dict[str, Any]:
    attempts = max(1, retries)
    errors: list[str] = []
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_json_once(url, headers, payload, timeout)
        except Exception as exc:  # noqa: BLE001 - remote scanner failures should be retried and surfaced together
            last_exc = exc
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < attempts:
                sleep(retry_delay)
    error_text = "; ".join(errors)
    raise RuntimeError(f"scanner request failed after {attempts} attempts: {error_text}") from last_exc


def _fetch_json_once(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload else None
    request = urllib.request.Request(url, data=data, headers=headers)
    context = ssl.create_default_context(cafile=certifi.where())
    context.set_alpn_protocols(["http/1.1"])
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError(f"{url} returned non-object JSON")
    return result


def _ensure_us_stock_dirs(root: Path) -> None:
    _cache_dir(root).mkdir(parents=True, exist_ok=True)
    (root / "data" / "us_stocks" / "history").mkdir(parents=True, exist_ok=True)
    _name_map_path(root).parent.mkdir(parents=True, exist_ok=True)
    (root / "data" / "state").mkdir(parents=True, exist_ok=True)


def _cache_dir(root: Path) -> Path:
    return root / "data" / "us_stocks" / "top_turnover"


def _history_path(root: Path, symbol: str) -> Path:
    return root / "data" / "us_stocks" / "history" / f"{symbol.upper()}.json"


def _cache_csv_path(root: Path, data_date: str) -> Path:
    return _cache_dir(root) / f"{data_date}.csv"


def _metadata_path(root: Path, data_date: str) -> Path:
    return _cache_dir(root) / f"{data_date}.json"


def _name_map_path(root: Path) -> Path:
    return root / "data" / "us_stocks" / US_NAME_MAP_FILE


def _status_path(root: Path) -> Path:
    return root / "data" / "state" / US_STOCK_STATUS_FILE


def _write_status(root: Path, result: UsStockRefreshResult) -> None:
    _status_path(root).write_text(json.dumps(result.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8")


def _timestamp() -> str:
    return now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
