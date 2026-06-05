"""Fetch ETF data from public AkShare adapters and cache normalized files."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import certifi
import pandas as pd

_SYSTEM_PROXIES = urllib.request.getproxies()
SPOT_DERIVED_COLUMNS = (
    "latest_price",
    "pct_change",
    "amount_yi",
    "volume",
    "latest_share_yi",
    "total_market_cap_yi",
    "float_market_cap_yi",
)

for _proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_key, None)
os.environ["NO_PROXY"] = "*"

import requests as _requests
_original_session_init = _requests.Session.__init__


def _init_no_proxy(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    self.trust_env = False


_requests.Session.__init__ = _init_no_proxy  # type: ignore[method-assign]

from .config import (
    DEFAULT_HISTORY_FETCH_MAX_WORKERS,
    MIN_LATEST_AMOUNT_YI,
    MIN_MARKET_CAP_YI,
    PROJECT_ROOT,
    ensure_data_dirs,
    now_shanghai,
)
from .filters import filter_equity_etfs
from .normalization import CODE_COLUMNS, normalize_code, normalize_history, normalize_scale, normalize_spot, pick_column


@dataclass(frozen=True)
class FetchResult:
    data_date: str
    spot_rows: int
    candidate_rows: int
    history_rows: int
    share_rows: int
    errors: list[str]
    normalized_dir: Path


@dataclass(frozen=True)
class HistoryFetchOutcome:
    code: str
    name: str
    status: str
    rows: int = 0
    error: str | None = None
    provider: str | None = None
    latest_date: str | None = None
    stale: bool = False
    primary_provider: str | None = None
    primary_error: str | None = None
    fallback_provider: str | None = None
    fallback_error: str | None = None


def fetch_data(
    data_date: str,
    root: Path = PROJECT_ROOT,
    history_calendar_days: int = 340,
    share_calendar_days: int = 45,
    limit: int | None = None,
    codes: list[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    resume: bool = True,
    max_workers: int = DEFAULT_HISTORY_FETCH_MAX_WORKERS,
) -> FetchResult:
    ak = _load_akshare()
    ensure_data_dirs(root)

    target = pd.Timestamp(data_date)
    history_start = (target - timedelta(days=history_calendar_days)).strftime("%Y%m%d")
    share_start = (target - timedelta(days=share_calendar_days)).strftime("%Y%m%d")
    target_ymd = target.strftime("%Y%m%d")

    raw_dir = root / "data" / "raw" / data_date
    raw_history_dir = raw_dir / "history"
    raw_scale_dir = raw_dir / "scale"
    normalized_dir = root / "data" / "normalized" / data_date
    normalized_history_dir = normalized_dir / "history"
    if not resume:
        for path in [raw_dir, normalized_dir]:
            if path.exists():
                shutil.rmtree(path)
    for path in [raw_dir, raw_history_dir, raw_scale_dir, normalized_dir, normalized_history_dir]:
        path.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    warnings: list[str] = []
    _emit_progress(progress_callback, "fetch_spot", 0, 0)
    spot = _load_or_fetch_spot(ak, raw_dir / "spot_em.csv", normalized_dir / "spot_all.csv", resume)
    spot_data_dates = _frame_data_dates(spot)
    spot_matches_target = _frame_dates_match_target(spot, target)
    candidate_source = "target_spot"
    candidate_source_date: str | None = data_date
    if not spot_matches_target:
        candidate_source = "unknown"
        candidate_source_date = None
        warnings.append(
            f"spot snapshot date mismatch: target {data_date}, spot dates {spot_data_dates or ['unknown']}; "
            "disabled spot-derived liquidity/share/history fallbacks"
        )
    spot_history_fallbacks = _spot_history_fallbacks(_read_csv(raw_dir / "spot_em.csv"), target) if spot_matches_target else {}
    requested_codes = {str(code).zfill(6) for code in codes or []}
    cached_candidates = _read_valid_candidates(normalized_dir / "spot_candidates.csv") if resume else None
    if cached_candidates is not None and not _frame_dates_match_target(cached_candidates, target):
        cached_candidates = None
    if cached_candidates is not None and not requested_codes and limit is None:
        candidates = _revalidate_cached_candidates(cached_candidates)
        candidate_source = "cached_target_candidates"
        candidate_source_date = data_date
        if len(candidates) != len(cached_candidates) or not candidates.equals(cached_candidates.reset_index(drop=True)):
            _write_csv(candidates, normalized_dir / "spot_candidates.csv")
    elif requested_codes:
        candidates = spot[spot["code"].isin(requested_codes)].copy()
        candidates["is_equity_etf"] = True
        candidates["universe_reason"] = "requested_code"
        candidate_source = "requested_codes"
        candidate_source_date = data_date if spot_matches_target else None
        if not spot_matches_target:
            candidates = _without_spot_derived_metrics(candidates)
    else:
        if spot_matches_target:
            candidates = filter_equity_etfs(spot)
            candidates = _prefilter_liquid_candidates(candidates)
            candidate_source = "target_spot_liquidity"
            candidate_source_date = data_date
        else:
            previous_snapshot = _latest_previous_candidates(root, data_date)
            if previous_snapshot is not None:
                previous_date, previous_candidates = previous_snapshot
                candidates = _without_spot_derived_metrics(filter_equity_etfs(previous_candidates))
                candidate_source = "previous_candidates"
                candidate_source_date = previous_date
                warnings.append(f"using {previous_date} candidate universe because target-day spot snapshot is unavailable")
            else:
                candidates = _without_spot_derived_metrics(filter_equity_etfs(spot))
                candidate_source = "spot_universe_without_current_metrics"
                candidate_source_date = None
                warnings.append("no previous candidate universe found; using spot code/name universe without current-day metrics")
    if cached_candidates is None or requested_codes or limit is not None:
        if limit:
            candidates = candidates.head(limit).copy()
        _write_csv(candidates, normalized_dir / "spot_candidates.csv")

    history_rows = 0
    candidate_records = candidates.to_dict("records")
    total_candidates = len(candidate_records)
    workers = max(1, min(int(max_workers or 1), total_candidates or 1))
    completed = 0
    skipped_cached = 0
    fetched = 0
    history_errors = 0
    failed_history: list[dict[str, str]] = []
    history_outcomes: list[HistoryFetchOutcome] = []
    _emit_progress(
        progress_callback,
        "fetch_history",
        completed,
        total_candidates,
        skipped_cached=skipped_cached,
        fetched=fetched,
        errors=history_errors,
        max_workers=workers,
    )
    if total_candidates:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _load_or_fetch_history,
                    ak,
                    str(row["code"]).zfill(6),
                    str(row.get("name", "")),
                    history_start,
                    target_ymd,
                    target,
                    raw_history_dir,
                    normalized_history_dir,
                    resume,
                    spot_history_fallbacks.get(str(row["code"]).zfill(6)),
                )
                for row in candidate_records
            ]
            for future in as_completed(futures):
                outcome = future.result()
                history_outcomes.append(outcome)
                completed += 1
                history_rows += outcome.rows
                if outcome.status == "cached":
                    skipped_cached += 1
                elif outcome.status == "fetched":
                    fetched += 1
                else:
                    history_errors += 1
                    error = outcome.error or "unknown history error"
                    failed_history.append({"code": outcome.code, "name": outcome.name, "error": error})
                    errors.append(f"{outcome.code}: {error}")
                _emit_progress(
                    progress_callback,
                    "fetch_history",
                    completed,
                    total_candidates,
                    outcome.code,
                    outcome.name,
                    skipped_cached=skipped_cached,
                    fetched=fetched,
                    errors=history_errors,
                    max_workers=workers,
                )

    _emit_progress(
        progress_callback,
        "fetch_scale",
        total_candidates,
        total_candidates,
        skipped_cached=skipped_cached,
        fetched=fetched,
        errors=history_errors,
        max_workers=workers,
    )
    shares_path = normalized_dir / "shares.csv"
    shares = _read_valid_shares(shares_path) if resume else None
    if shares is None:
        share_frames = []
        share_frames.extend(_fetch_sse_scales(ak, share_start, target_ymd, raw_scale_dir, errors))
        share_frames.extend(_fetch_szse_scales(ak, share_start, target_ymd, raw_scale_dir, errors))
        shares = _merge_share_sources(share_frames, candidates, data_date)
        _write_csv(shares, shares_path)

    report = {
        "data_date": data_date,
        "fetched_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "spot_rows": int(len(spot)),
        "candidate_rows": int(len(candidates)),
        "history_rows": int(history_rows),
        "share_rows": int(len(shares)),
        "history_total": int(total_candidates),
        "history_success": int(skipped_cached + fetched),
        "history_failed": int(history_errors),
        "failed_history": failed_history,
        "stale_history_files": int(sum(1 for item in history_outcomes if item.stale)),
        "history_provider_counts": _counts(item.provider or "unknown" for item in history_outcomes),
        "history_latest_date_counts": _counts(item.latest_date or "missing" for item in history_outcomes),
        "history_outcomes": [asdict(item) for item in sorted(history_outcomes, key=lambda item: item.code)],
        "spot_data_dates": spot_data_dates,
        "spot_data_matches_target": bool(spot_matches_target),
        "spot_derived_metrics_enabled": bool(spot_matches_target),
        "candidate_source": candidate_source,
        "candidate_source_date": candidate_source_date,
        "skipped_cached": int(skipped_cached),
        "fetched_history_files": int(fetched),
        "history_errors": int(history_errors),
        "resume": bool(resume),
        "max_workers": int(workers),
        "errors": errors,
        "warnings": warnings,
        "source": "akshare",
    }
    _write_json(report, normalized_dir / "fetch_report.json")

    return FetchResult(
        data_date=data_date,
        spot_rows=len(spot),
        candidate_rows=len(candidates),
        history_rows=history_rows,
        share_rows=len(shares),
        errors=errors,
        normalized_dir=normalized_dir,
    )


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    phase: str,
    completed: int,
    total: int,
    current_code: str | None = None,
    current_name: str | None = None,
    skipped_cached: int = 0,
    fetched: int = 0,
    errors: int = 0,
    max_workers: int = 1,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        {
            "phase": phase,
            "completed": int(completed),
            "total": int(total),
            "current_code": current_code,
            "current_name": current_name,
            "skipped_cached": int(skipped_cached),
            "fetched": int(fetched),
            "errors": int(errors),
            "max_workers": int(max_workers),
        }
    )


def _load_or_fetch_spot(ak: Any, raw_path: Path, normalized_path: Path, resume: bool) -> pd.DataFrame:
    if resume:
        cached_spot = _read_valid_spot(normalized_path)
        if cached_spot is not None:
            return cached_spot
        cached_raw = _read_csv(raw_path)
        if cached_raw is not None:
            spot = normalize_spot(cached_raw)
            _write_csv(spot, normalized_path)
            return spot

    spot_raw = _call_akshare(ak, "fund_etf_spot_em")
    _write_csv(spot_raw, raw_path)
    spot = normalize_spot(spot_raw)
    _write_csv(spot, normalized_path)
    return spot


def _load_or_fetch_history(
    ak: Any,
    code: str,
    name: str,
    history_start: str,
    target_ymd: str,
    target: pd.Timestamp,
    raw_history_dir: Path,
    normalized_history_dir: Path,
    resume: bool,
    spot_history: pd.DataFrame | None = None,
) -> HistoryFetchOutcome:
    raw_path = raw_history_dir / f"{code}.csv"
    normalized_path = normalized_history_dir / f"{code}.csv"

    if resume:
        cached_meta = _valid_normalized_history_metadata(normalized_path, target)
        if cached_meta is not None:
            rows, latest_date = cached_meta
            if latest_date == target.date().isoformat():
                cached_source = _history_source_from_path(normalized_path) or "cache_normalized"
                return HistoryFetchOutcome(
                    code=code,
                    name=name,
                    status="cached",
                    rows=rows,
                    provider=cached_source,
                    latest_date=latest_date,
                    stale=False,
                )
            spot_meta = _write_history_with_spot_fallback(_read_csv(normalized_path), spot_history, normalized_path, target)
            if spot_meta is not None:
                rows, latest_date = spot_meta
                return HistoryFetchOutcome(
                    code=code,
                    name=name,
                    status="fetched",
                    rows=rows,
                    provider="spot_fallback",
                    latest_date=latest_date,
                    stale=False,
                    primary_provider="eastmoney",
                    primary_error="cached normalized history is stale; patched from current spot snapshot before primary refetch",
                    fallback_provider="spot_fallback",
                )

        raw_history = _read_csv(raw_path)
        if raw_history is not None:
            try:
                raw_source = _history_source_from_frame(raw_history) or "cache_raw"
                history = _with_history_source(normalize_history(raw_history, code), raw_source)
                cached_meta = _valid_history_metadata(history, target)
                if cached_meta is not None:
                    rows, latest_date = cached_meta
                    if latest_date == target.date().isoformat():
                        _write_csv(history, normalized_path)
                        return HistoryFetchOutcome(
                            code=code,
                            name=name,
                            status="cached",
                            rows=rows,
                            provider=raw_source,
                            latest_date=latest_date,
                            stale=False,
                        )
                    spot_meta = _write_history_with_spot_fallback(history, spot_history, normalized_path, target)
                    if spot_meta is not None:
                        rows, latest_date = spot_meta
                        return HistoryFetchOutcome(
                            code=code,
                            name=name,
                            status="fetched",
                            rows=rows,
                            provider="spot_fallback",
                            latest_date=latest_date,
                            stale=False,
                            primary_provider="eastmoney",
                            primary_error="cached raw history is stale; patched from current spot snapshot before primary refetch",
                            fallback_provider="spot_fallback",
                        )
            except Exception:
                pass

    try:
        raw_history, provider, primary_error, fallback_error = _fetch_history_with_attempt_details(
            ak, code, history_start, target_ymd
        )
        raw_history = raw_history.copy()
        raw_history["history_source"] = provider
        _write_csv(raw_history, raw_path)
        history = _with_history_source(normalize_history(raw_history, code), provider)
        meta = _valid_history_metadata(history, target)
        if meta is None:
            spot_meta = _write_history_with_spot_fallback(None, spot_history, normalized_path, target)
            if spot_meta is not None:
                rows, latest_date = spot_meta
                return HistoryFetchOutcome(
                    code=code,
                    name=name,
                    status="fetched",
                    rows=rows,
                    provider="spot_fallback",
                    latest_date=latest_date,
                    stale=False,
                    primary_provider="eastmoney",
                    primary_error=primary_error,
                    fallback_provider="spot_fallback",
                )
            return HistoryFetchOutcome(code=code, name=name, status="error", error="empty history")
        rows, latest_date = meta
        if latest_date != target.date().isoformat():
            spot_meta = _write_history_with_spot_fallback(history, spot_history, normalized_path, target)
            if spot_meta is not None:
                rows, latest_date = spot_meta
                return HistoryFetchOutcome(
                    code=code,
                    name=name,
                    status="fetched",
                    rows=rows,
                    provider="spot_fallback",
                    latest_date=latest_date,
                    stale=False,
                )
        _write_csv(history, normalized_path)
        return HistoryFetchOutcome(
            code=code,
            name=name,
            status="fetched",
            rows=rows,
            provider=provider,
            latest_date=latest_date,
            stale=latest_date != target.date().isoformat(),
            primary_provider=provider if _is_eastmoney_provider(provider) else "eastmoney",
            primary_error=primary_error,
            fallback_provider=provider if provider == "sina" else None,
            fallback_error=fallback_error,
        )
    except Exception as exc:  # noqa: BLE001 - caller records per-code errors
        spot_meta = _write_history_with_spot_fallback(None, spot_history, normalized_path, target)
        if spot_meta is not None:
            rows, latest_date = spot_meta
            return HistoryFetchOutcome(
                code=code,
                name=name,
                status="fetched",
                rows=rows,
                provider="spot_fallback",
                latest_date=latest_date,
                stale=False,
                primary_provider="eastmoney",
                primary_error=str(exc),
                fallback_provider="spot_fallback",
            )
        return HistoryFetchOutcome(code=code, name=name, status="error", error=str(exc))


def _spot_history_fallbacks(raw_spot: pd.DataFrame | None, target: pd.Timestamp) -> dict[str, pd.DataFrame]:
    if raw_spot is None or raw_spot.empty:
        return {}
    code_col = pick_column(raw_spot.columns, CODE_COLUMNS)
    date_col = pick_column(raw_spot.columns, ("数据日期", "日期"))
    if not code_col or not date_col:
        return {}

    frame = raw_spot.copy()
    frame["_spot_date"] = pd.to_datetime(frame[date_col], errors="coerce")
    frame = frame[frame["_spot_date"].dt.date == target.date()]
    if frame.empty:
        return {}

    fallbacks: dict[str, pd.DataFrame] = {}
    for _, row in frame.iterrows():
        code = normalize_code(row[code_col])
        if not code:
            continue
        try:
            history = normalize_history(pd.DataFrame([row.drop(labels=["_spot_date"], errors="ignore")]), code)
        except Exception:
            continue
        history["history_source"] = "spot_fallback"
        meta = _valid_history_metadata(history, target)
        if meta is not None and meta[1] == target.date().isoformat():
            fallbacks[code] = history
    return fallbacks


def _write_history_with_spot_fallback(
    history: pd.DataFrame | None,
    spot_history: pd.DataFrame | None,
    normalized_path: Path,
    target: pd.Timestamp,
) -> tuple[int, str] | None:
    if spot_history is None or spot_history.empty:
        return None
    prepared_frames = []
    for frame, source in [(history, "unknown"), (spot_history, "spot_fallback")]:
        if frame is None or frame.empty:
            continue
        prepared = frame.copy()
        if "history_source" not in prepared:
            prepared["history_source"] = source
        else:
            prepared["history_source"] = prepared["history_source"].fillna("").astype(str).replace("", source)
        prepared_frames.append(prepared)
    frames = prepared_frames
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    if "日期" not in combined:
        return None
    combined["日期"] = pd.to_datetime(combined["日期"], errors="coerce")
    combined = combined.dropna(subset=["日期"]).sort_values("日期")
    combined = combined.drop_duplicates(["code", "日期"], keep="last").reset_index(drop=True)
    meta = _valid_history_metadata(combined, target)
    if meta is None or meta[1] != target.date().isoformat():
        return None
    _write_csv(combined, normalized_path)
    return meta


def _with_history_source(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    result = frame.copy()
    result["history_source"] = str(source or "unknown")
    return result


def _history_source_from_path(path: Path) -> str | None:
    frame = _read_csv(path)
    if frame is None:
        return None
    return _history_source_from_frame(frame)


def _history_source_from_frame(frame: pd.DataFrame) -> str | None:
    if "history_source" not in frame or frame.empty:
        return None
    values = frame["history_source"].dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return None
    if values.eq("spot_fallback").any():
        return "spot_fallback"
    return str(values.iloc[-1])


def _frame_data_dates(frame: pd.DataFrame | None) -> list[str]:
    if frame is None or frame.empty or "data_date" not in frame:
        return []
    dates = pd.to_datetime(frame["data_date"], errors="coerce").dropna()
    if dates.empty:
        return []
    return sorted({item.date().isoformat() for item in dates})


def _frame_dates_match_target(frame: pd.DataFrame | None, target: pd.Timestamp) -> bool:
    data_dates = _frame_data_dates(frame)
    return not data_dates or data_dates == [target.date().isoformat()]


def _without_spot_derived_metrics(candidates: pd.DataFrame) -> pd.DataFrame:
    result = candidates.copy()
    for column in SPOT_DERIVED_COLUMNS:
        result[column] = pd.NA
    return result


def _latest_previous_candidates(root: Path, data_date: str) -> tuple[str, pd.DataFrame] | None:
    normalized_root = root / "data" / "normalized"
    if not normalized_root.exists():
        return None

    for path in sorted((item for item in normalized_root.iterdir() if item.is_dir()), reverse=True):
        if path.name >= data_date:
            continue
        try:
            target = pd.Timestamp(path.name)
        except ValueError:
            continue
        candidates = _read_valid_candidates(path / "spot_candidates.csv")
        if candidates is None or candidates.empty:
            continue
        if not _frame_dates_match_target(candidates, target):
            continue
        return path.name, candidates
    return None


def _read_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return pd.read_csv(path, dtype={"code": str, "代码": str})
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return None


def _read_valid_spot(path: Path) -> pd.DataFrame | None:
    frame = _read_csv(path)
    if frame is None or frame.empty or not {"code", "name"}.issubset(frame.columns):
        return None
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    return frame[frame["code"].str.len() == 6].reset_index(drop=True)


def _read_valid_candidates(path: Path) -> pd.DataFrame | None:
    frame = _read_valid_spot(path)
    if frame is None:
        return None
    return frame


def _read_valid_shares(path: Path) -> pd.DataFrame | None:
    frame = _read_csv(path)
    required = {"code", "日期", "ETF份额(亿份)"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return None
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
    frame["ETF份额(亿份)"] = pd.to_numeric(frame["ETF份额(亿份)"], errors="coerce")
    frame = frame.dropna(subset=["日期", "ETF份额(亿份)"])
    return frame.reset_index(drop=True) if not frame.empty else None


def _valid_normalized_history_metadata(path: Path, target: pd.Timestamp) -> tuple[int, str] | None:
    frame = _read_csv(path)
    if frame is None:
        return None
    return _valid_history_metadata(frame, target)


def _valid_normalized_history_rows(path: Path, target: pd.Timestamp) -> int | None:
    metadata = _valid_normalized_history_metadata(path, target)
    return metadata[0] if metadata is not None else None


def _valid_history_rows(frame: pd.DataFrame, target: pd.Timestamp) -> int | None:
    metadata = _valid_history_metadata(frame, target)
    return metadata[0] if metadata is not None else None


def _valid_history_metadata(frame: pd.DataFrame, target: pd.Timestamp) -> tuple[int, str] | None:
    required = {"日期", "收盘价", "成交额(亿)"}
    if frame.empty or not required.issubset(frame.columns):
        return None
    dates = pd.to_datetime(frame["日期"], errors="coerce")
    close = pd.to_numeric(frame["收盘价"], errors="coerce")
    amount = pd.to_numeric(frame["成交额(亿)"], errors="coerce")
    valid = frame[dates.notna() & close.notna() & amount.notna()].copy()
    if valid.empty:
        return None
    latest = pd.to_datetime(valid["日期"], errors="coerce").max()
    if pd.isna(latest) or latest > target:
        return None
    return int(len(valid)), latest.date().isoformat()


def _load_akshare() -> Any:
    try:
        return importlib.import_module("akshare")
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: install akshare with `python -m pip install -r requirements.txt`.") from exc


def _call_akshare(ak: Any, function_name: str, **kwargs: Any) -> pd.DataFrame:
    function = getattr(ak, function_name)
    result = function(**kwargs)
    if not isinstance(result, pd.DataFrame):
        raise TypeError(f"akshare.{function_name} returned {type(result)!r}, expected pandas.DataFrame")
    return result


_HISTORY_API_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]
_EASTMONEY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _fetch_history(ak: Any, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    frame, _provider = _fetch_history_with_provider(ak, code, start_date, end_date)
    return frame


def _fetch_history_with_provider(ak: Any, code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    frame, provider, _primary_error, _fallback_error = _fetch_history_with_attempt_details(ak, code, start_date, end_date)
    return frame, provider


def _fetch_history_with_attempt_details(
    ak: Any, code: str, start_date: str, end_date: str
) -> tuple[pd.DataFrame, str, str | None, str | None]:
    errors: list[str] = []

    for attempt in range(2):
        try:
            frame, provider = _fetch_history_eastmoney_with_provider(code, start_date, end_date)
            return frame, provider, None, None
        except Exception as exc:
            errors.append(f"eastmoney[{attempt}]: {exc}")
            time.sleep(0.5)

    primary_error = "; ".join(errors)
    try:
        return _fetch_history_sina(ak, code, start_date, end_date), "sina", primary_error, None
    except Exception as exc:
        fallback_error = f"sina: {exc}"
        errors.append(fallback_error)

    raise RuntimeError(f"All sources exhausted for {code}: {'; '.join(errors[-3:])}")


def _is_eastmoney_provider(provider: str | None) -> bool:
    return str(provider or "").startswith("eastmoney_")


def _fetch_history_eastmoney(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    frame, _provider = _fetch_history_eastmoney_with_provider(code, start_date, end_date)
    return frame


def _fetch_history_eastmoney_with_provider(code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    url = _eastmoney_history_url(code, start_date, end_date)
    try:
        return _fetch_history_eastmoney_urllib(url), "eastmoney_urllib"
    except Exception as urllib_exc:
        errors = [f"urllib failed: {urllib_exc}"]
        try:
            return _fetch_history_eastmoney_curl(url), "eastmoney_curl"
        except Exception as curl_exc:
            errors.append(f"curl failed: {curl_exc}")
            proxy = _history_proxy()
            if proxy:
                try:
                    return _fetch_history_eastmoney_curl(url, proxy=proxy), "eastmoney_curl_proxy"
                except Exception as proxy_exc:
                    errors.append(f"proxy curl failed: {proxy_exc}")
            raise RuntimeError("; ".join(errors)) from curl_exc


def _eastmoney_history_url(code: str, start_date: str, end_date: str) -> str:
    market_id = 1 if code.startswith("5") else 0
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101",
        "fqt": "0",
        "beg": start_date,
        "end": end_date,
        "secid": f"{market_id}.{code}",
    }
    return f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urllib.parse.urlencode(params)}"


def _fetch_history_eastmoney_urllib(url: str) -> pd.DataFrame:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl_context.set_alpn_protocols(["http/1.1"])
    ssl_context.check_hostname = True
    ssl_context.verify_mode = ssl.CERT_REQUIRED

    req = urllib.request.Request(url, headers={"User-Agent": _EASTMONEY_UA})
    with urllib.request.urlopen(req, timeout=15, context=ssl_context) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _eastmoney_payload_to_frame(data)


def _fetch_history_eastmoney_curl(url: str, proxy: str | None = None) -> pd.DataFrame:
    command = ["curl", "-sS", "-L", "-m", "15", "-A", _EASTMONEY_UA]
    if proxy:
        command.extend(["-x", proxy])
    command.append(url)
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=_curl_env(proxy),
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl exited {result.returncode}")
    data = json.loads(result.stdout)
    return _eastmoney_payload_to_frame(data)


def _history_proxy() -> str | None:
    proxy = _SYSTEM_PROXIES.get("https") or _SYSTEM_PROXIES.get("http") or _SYSTEM_PROXIES.get("socks")
    return str(proxy) if proxy else None


def _curl_env(proxy: str | None = None) -> dict[str, str] | None:
    if not proxy:
        return None
    env = os.environ.copy()
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    return env


def _eastmoney_payload_to_frame(data: dict[str, Any]) -> pd.DataFrame:
    klines = data.get("data") and data["data"].get("klines")
    if not klines:
        return pd.DataFrame(columns=_HISTORY_API_COLUMNS)
    rows = [item.split(",") for item in klines]
    return pd.DataFrame(rows, columns=_HISTORY_API_COLUMNS)


def _fetch_history_sina(ak: Any, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    prefix = "sh" if code.startswith("5") else "sz"
    raw = ak.fund_etf_hist_sina(symbol=f"{prefix}{code}")
    if raw.empty:
        return pd.DataFrame(columns=_HISTORY_API_COLUMNS)

    raw = raw.copy()
    raw["date_str"] = pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y%m%d")
    raw = raw.dropna(subset=["date_str"])
    mask = (raw["date_str"] >= start_date) & (raw["date_str"] <= end_date)
    raw = raw[mask].reset_index(drop=True)
    if raw.empty:
        return pd.DataFrame(columns=_HISTORY_API_COLUMNS)

    prev_close = raw["close"].shift(1).bfill()
    volume = pd.to_numeric(raw["volume"], errors="coerce")
    amount = _sina_amount(raw, volume)
    volume_hands = _sina_volume_to_hands(raw, volume, amount)

    result = pd.DataFrame()
    result["日期"] = raw["date_str"]
    result["开盘"] = raw["open"].round(4)
    result["收盘"] = raw["close"].round(4)
    result["最高"] = raw["high"].round(4)
    result["最低"] = raw["low"].round(4)
    result["成交量"] = volume_hands.round(0)
    result["成交额"] = amount.round(2)
    result["振幅"] = ((raw["high"] - raw["low"]) / prev_close * 100).round(4)
    result["涨跌幅"] = ((raw["close"] - prev_close) / prev_close * 100).round(4)
    result["涨跌额"] = (raw["close"] - prev_close).round(4)
    result["换手率"] = ""
    return result[_HISTORY_API_COLUMNS]


def _sina_amount(raw: pd.DataFrame, volume: pd.Series) -> pd.Series:
    close = pd.to_numeric(raw["close"], errors="coerce")
    derived = close * volume
    if "amount" in raw.columns:
        amount = pd.to_numeric(raw["amount"], errors="coerce")
        if amount.notna().any():
            return amount.fillna(derived)
    return derived


def _sina_volume_to_hands(raw: pd.DataFrame, volume: pd.Series, amount: pd.Series) -> pd.Series:
    close = pd.to_numeric(raw["close"], errors="coerce")
    amount_abs = amount.abs()
    shares_error = (amount - close * volume).abs() / amount_abs
    hands_error = (amount - close * volume * 100).abs() / amount_abs
    volume_looks_like_hands = hands_error < shares_error
    return volume.where(volume_looks_like_hands, volume / 100)


def _fetch_sse_scales(ak: Any, start_ymd: str, end_ymd: str, raw_scale_dir: Path, errors: list[str]) -> list[pd.DataFrame]:
    if not hasattr(ak, "fund_etf_scale_sse"):
        errors.append("SSE scale: akshare.fund_etf_scale_sse is unavailable")
        return []

    frames: list[pd.DataFrame] = []
    start = pd.to_datetime(start_ymd, format="%Y%m%d")
    end = pd.to_datetime(end_ymd, format="%Y%m%d")
    for current in pd.bdate_range(start, end):
        ymd = current.strftime("%Y%m%d")
        try:
            raw = _call_akshare(ak, "fund_etf_scale_sse", date=ymd)
            if raw.empty:
                continue
            _write_csv(raw, raw_scale_dir / f"sse_{ymd}.csv")
            frames.append(normalize_scale(raw, "sse", current.strftime("%Y-%m-%d")))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"SSE scale {ymd}: {exc}")
    return frames


def _fetch_szse_scales(ak: Any, start_ymd: str, end_ymd: str, raw_scale_dir: Path, errors: list[str]) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    if hasattr(ak, "fund_scale_daily_szse"):
        function = getattr(ak, "fund_scale_daily_szse")
        attempts = (
            {"start_date": start_ymd, "end_date": end_ymd, "symbol": "ETF"},
            {"start_date": start_ymd, "end_date": end_ymd},
            {"date": end_ymd},
        )
        raw = _call_with_attempts(function, attempts, errors, "SZSE daily scale")
        if raw is not None and not raw.empty:
            _write_csv(raw, raw_scale_dir / "szse_daily.csv")
            frames.append(normalize_scale(raw, "szse"))
    elif hasattr(ak, "fund_etf_scale_szse"):
        try:
            raw = _call_akshare(ak, "fund_etf_scale_szse")
            _write_csv(raw, raw_scale_dir / "szse_latest.csv")
            end_date = pd.to_datetime(end_ymd, format="%Y%m%d").strftime("%Y-%m-%d")
            frames.append(normalize_scale(raw, "szse", end_date))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"SZSE latest scale: {exc}")
    else:
        errors.append("SZSE scale: no known AkShare function is available")
    return frames


def _call_with_attempts(
    function: Any,
    attempts: tuple[dict[str, Any], ...],
    errors: list[str],
    label: str,
) -> pd.DataFrame | None:
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            result = function(**kwargs)
            if isinstance(result, pd.DataFrame):
                return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error:
        errors.append(f"{label}: {last_error}")
    return None


def _prefilter_liquid_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    frame = candidates.copy()
    amount = pd.to_numeric(frame["amount_yi"], errors="coerce").fillna(0)
    total_cap = pd.to_numeric(frame["total_market_cap_yi"], errors="coerce")
    float_cap = pd.to_numeric(frame["float_market_cap_yi"], errors="coerce")
    cap_ok = (total_cap >= MIN_MARKET_CAP_YI) | (float_cap >= MIN_MARKET_CAP_YI) | (total_cap.isna() & float_cap.isna())
    return frame[(amount >= MIN_LATEST_AMOUNT_YI) & cap_ok].reset_index(drop=True)


def _revalidate_cached_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    filtered = filter_equity_etfs(candidates)
    return _prefilter_liquid_candidates(filtered)


def _merge_share_sources(frames: list[pd.DataFrame], candidates: pd.DataFrame, data_date: str) -> pd.DataFrame:
    usable = [frame for frame in frames if frame is not None and not frame.empty]
    official = pd.concat(usable, ignore_index=True) if usable else pd.DataFrame()

    fallback = candidates[["code", "name", "latest_share_yi"]].copy()
    fallback = fallback.dropna(subset=["latest_share_yi"])
    fallback = fallback.rename(columns={"latest_share_yi": "ETF份额(亿份)"})
    fallback["日期"] = pd.Timestamp(data_date)
    fallback["share_source"] = "spot_fallback"

    combined = pd.concat([official, fallback[["code", "name", "日期", "ETF份额(亿份)", "share_source"]]], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=["code", "name", "日期", "ETF份额(亿份)", "share_source"])

    combined["code"] = combined["code"].astype(str).str.zfill(6)
    combined["日期"] = pd.to_datetime(combined["日期"], errors="coerce")
    combined["ETF份额(亿份)"] = pd.to_numeric(combined["ETF份额(亿份)"], errors="coerce")
    combined["source_rank"] = combined["share_source"].map({"sse": 0, "szse": 0, "spot_fallback": 1}).fillna(2)
    combined = combined.dropna(subset=["日期", "ETF份额(亿份)"]).sort_values(["code", "日期", "source_rank"])
    combined = combined.drop_duplicates(["code", "日期"], keep="first").drop(columns=["source_rank"])
    return combined.reset_index(drop=True)


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    frame.to_csv(temp_path, index=False)
    temp_path.replace(path)


def _write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))
