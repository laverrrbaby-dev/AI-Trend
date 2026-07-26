"""Fetch ETF data from public AkShare adapters and cache normalized files."""

from __future__ import annotations

import atexit
import importlib
import json
import os
import random
import shutil
import signal
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
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
    CHECKPOINT_WRITE_INTERVAL,
    DEFAULT_HISTORY_FETCH_MAX_WORKERS,
    HISTORY_FETCH_PER_CODE_TIMEOUT,
    MIN_LATEST_AMOUNT_YI,
    MIN_MARKET_CAP_YI,
    PROJECT_ROOT,
    SSE_FETCH_MAX_WORKERS,
    SSE_FETCH_PER_DAY_TIMEOUT,
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


class _FetchAborted(Exception):
    """Raised when the fetch pipeline is interrupted by a signal."""


class _AbortEvent:
    """Thread-safe abort flag for graceful shutdown."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise _FetchAborted("fetch aborted by signal")


_ACTIVE_ABORT_EVENT: _AbortEvent | None = None
_ACTIVE_ABORT_LOCK = threading.Lock()


def _install_signal_handlers(abort: _AbortEvent) -> None:
    """Install SIGINT/SIGTERM handlers that set the abort event."""
    def _handler(signum: int, frame: Any) -> None:
        abort.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not in main thread; signal handlers can't be installed


def _restore_signal_handlers() -> None:
    """Restore default signal handlers."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError):
            pass


def _cleanup_stale_tmp_files(root: Path) -> None:
    """Remove orphaned .tmp files left by interrupted writes."""
    data_dir = root / "data"
    if not data_dir.exists():
        return
    for tmp_path in data_dir.rglob("*.tmp"):
        try:
            tmp_path.unlink()
        except OSError:
            pass


_atexit_cleanup_registered = False


def _register_atexit_cleanup(root: Path) -> None:
    """Register atexit handler to clean up on abnormal exit.

    Uses a sentinel to avoid duplicate registrations within the same process.
    """
    global _atexit_cleanup_registered
    if _atexit_cleanup_registered:
        return
    _atexit_cleanup_registered = True

    def _cleanup() -> None:
        _cleanup_stale_tmp_files(root)

    atexit.register(_cleanup)


def _sse_checkpoint_path(root: Path, data_date: str) -> Path:
    return root / "data" / "state" / f"fetch_checkpoint_sse_{data_date}.json"


def _read_sse_checkpoint(root: Path, data_date: str) -> set[str] | None:
    """Read SSE checkpoint and return completed days if PID is dead. Returns None if should resume."""
    path = _sse_checkpoint_path(root, data_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return None
        except OSError:
            pass
        else:
            return None  # PID alive, don't reuse
    completed = payload.get("completed_days", [])
    if isinstance(completed, list):
        return {str(d) for d in completed}
    return None


def _write_sse_checkpoint(root: Path, data_date: str, completed_days: set[str]) -> None:
    """Write SSE checkpoint atomically."""
    path = _sse_checkpoint_path(root, data_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "data_date": data_date,
        "completed_days": sorted(completed_days),
        "updated_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
    }
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _clear_sse_checkpoint(root: Path, data_date: str) -> None:
    path = _sse_checkpoint_path(root, data_date)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _checkpoint_path(root: Path, data_date: str) -> Path:
    return root / "data" / "state" / f"fetch_checkpoint_{data_date}.json"


def _read_checkpoint(root: Path, data_date: str) -> dict[str, Any] | None:
    path = _checkpoint_path(root, data_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("candidate_codes"):
        return None
    pid = payload.get("pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return None
        except OSError:
            pass
        else:
            return None
    return payload


def _write_checkpoint(
    data_date: str,
    candidate_codes: list[str],
    outcomes_dict: dict[str, dict[str, Any]],
    root: Path,
) -> None:
    path = _checkpoint_path(root, data_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "data_date": data_date,
        "candidate_codes": candidate_codes,
        "outcomes": {str(code): dict(item) for code, item in outcomes_dict.items()},
        "updated_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
    }
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _clear_checkpoint(root: Path, data_date: str) -> None:
    path = _checkpoint_path(root, data_date)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _rebuild_outcomes_from_checkpoint(
    checkpoint: dict[str, Any],
    normalized_history_dir: Path,
    target: pd.Timestamp,
) -> tuple[list[HistoryFetchOutcome], list[dict[str, str]], int, int, int, int]:
    outcomes: list[HistoryFetchOutcome] = []
    failed_history: list[dict[str, str]] = []
    skipped_cached = 0
    fetched = 0
    history_errors = 0
    history_rows = 0

    for code, entry in checkpoint.get("outcomes", {}).items():
        code = str(code).zfill(6)
        status = str(entry.get("status", ""))
        name = str(entry.get("name", ""))
        normalized_path = normalized_history_dir / f"{code}.csv"

        if status in ("cached", "fetched"):
            meta = _valid_normalized_history_metadata(normalized_path, target)
            if meta is not None:
                rows, latest_date = meta
                provider = str(entry.get("provider") or "")
                stale = bool(entry.get("stale", False))
                outcome = HistoryFetchOutcome(
                    code=code,
                    name=name,
                    status=status,
                    rows=rows,
                    provider=provider if provider else None,
                    latest_date=latest_date,
                    stale=stale,
                    primary_provider=str(entry.get("primary_provider") or "") or None,
                    primary_error=str(entry.get("primary_error") or "") or None,
                    fallback_provider=str(entry.get("fallback_provider") or "") or None,
                    fallback_error=str(entry.get("fallback_error") or "") or None,
                )
                outcomes.append(outcome)
                history_rows += rows
                if status == "cached":
                    skipped_cached += 1
                else:
                    fetched += 1
                continue
        elif status == "error":
            error_msg = str(entry.get("error") or "unknown history error")
            outcome = HistoryFetchOutcome(
                code=code,
                name=name,
                status="error",
                error=error_msg,
            )
            outcomes.append(outcome)
            failed_history.append({"code": code, "name": name, "error": error_msg})
            history_errors += 1
            continue

    return outcomes, failed_history, skipped_cached, fetched, history_errors, history_rows


def _filter_pending_records(
    candidate_records: list[dict[str, Any]],
    checkpoint_outcomes: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        record
        for record in candidate_records
        if str(record["code"]).zfill(6) not in checkpoint_outcomes
    ]


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
    _cleanup_stale_tmp_files(root)

    # Install signal handlers for graceful shutdown
    global _ACTIVE_ABORT_EVENT
    with _ACTIVE_ABORT_LOCK:
        abort = _AbortEvent()
        _ACTIVE_ABORT_EVENT = abort
    _install_signal_handlers(abort)
    _register_atexit_cleanup(root)

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
    spot = _load_or_fetch_spot(
        ak,
        raw_dir / "spot_em.csv",
        normalized_dir / "spot_all.csv",
        resume,
        target=target,
        warnings=warnings,
    )
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

    checkpoint_data: dict[str, Any] | None = None
    checkpoint_outcomes: dict[str, dict[str, Any]] = {}
    checkpoint_lock = threading.Lock()
    completed_before_resume = 0

    try:
        if resume:
            checkpoint_data = _read_checkpoint(root, data_date)
            if checkpoint_data is not None:
                rebuilt_outcomes, rebuilt_failed, rebuilt_cached, rebuilt_fetched, rebuilt_errors, rebuilt_rows = (
                    _rebuild_outcomes_from_checkpoint(checkpoint_data, normalized_history_dir, target)
                )
                history_outcomes.extend(rebuilt_outcomes)
                failed_history.extend(rebuilt_failed)
                skipped_cached += rebuilt_cached
                fetched += rebuilt_fetched
                history_errors += rebuilt_errors
                history_rows += rebuilt_rows
                completed_before_resume = len(rebuilt_outcomes)
                completed = completed_before_resume
                checkpoint_outcomes = {str(item.code): asdict(item) for item in rebuilt_outcomes}
                pending_records = _filter_pending_records(candidate_records, checkpoint_outcomes)
            else:
                pending_records = candidate_records
        else:
            pending_records = candidate_records

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
        if pending_records:
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
                    for row in pending_records
                ]
                for future in as_completed(futures):
                    # Check abort signal before processing each result
                    abort.check()

                    try:
                        outcome = future.result(timeout=HISTORY_FETCH_PER_CODE_TIMEOUT)
                    except FutureTimeoutError:
                        # ETF fetch timed out - record as error and continue
                        outcome = HistoryFetchOutcome(
                            code="unknown",
                            name="unknown",
                            status="error",
                            error=f"fetch timed out after {HISTORY_FETCH_PER_CODE_TIMEOUT}s",
                        )
                    except _FetchAborted:
                        raise
                    except Exception as exc:
                        # Unexpected exception from worker - record as error, save checkpoint, continue
                        outcome = HistoryFetchOutcome(
                            code="unknown",
                            name="unknown",
                            status="error",
                            error=f"unexpected fetch error: {exc}",
                        )

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

                    if resume:
                        with checkpoint_lock:
                            checkpoint_outcomes[str(outcome.code)] = asdict(outcome)
                        checkpoint_count = completed - completed_before_resume
                        if (
                            checkpoint_count % CHECKPOINT_WRITE_INTERVAL == 0
                            or completed >= total_candidates
                        ):
                            _write_checkpoint(
                                data_date,
                                [str(r["code"]).zfill(6) for r in candidate_records],
                                checkpoint_outcomes,
                                root,
                            )

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

        if resume:
            _clear_checkpoint(root, data_date)

        abort.check()

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
            sse_completed_days: set[str] = set()
            if resume:
                sse_checkpoint = _read_sse_checkpoint(root, data_date)
                if sse_checkpoint is not None:
                    sse_completed_days = sse_checkpoint
            share_frames.extend(_fetch_sse_scales(
                ak, share_start, target_ymd, raw_scale_dir, errors,
                abort=abort, completed_days=sse_completed_days,
                checkpoint_root=root, data_date=data_date,
            ))
            abort.check()
            share_frames.extend(_fetch_szse_scales(ak, share_start, target_ymd, raw_scale_dir, errors))
            shares = _merge_share_sources(share_frames, candidates, data_date)
            _write_csv(shares, shares_path)
            _clear_sse_checkpoint(root, data_date)

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
    except _FetchAborted:
        # Save checkpoint on abort so progress is not lost
        if resume and checkpoint_outcomes:
            try:
                _write_checkpoint(
                    data_date,
                    [str(r["code"]).zfill(6) for r in candidate_records],
                    checkpoint_outcomes,
                    root,
                )
            except Exception:
                pass
        raise
    finally:
        with _ACTIVE_ABORT_LOCK:
            _ACTIVE_ABORT_EVENT = None
        _restore_signal_handlers()


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


def _load_or_fetch_spot(
    ak: Any,
    raw_path: Path,
    normalized_path: Path,
    resume: bool,
    target: pd.Timestamp | None = None,
    warnings: list[str] | None = None,
) -> pd.DataFrame:
    """加载或拉取 spot 快照，resume 时校验 data_date。

    回退链：
    1. resume + cached_spot 有效 + 日期匹配 target → 返回 cached_spot
    2. cached_spot stale 或不存在 → akshare 重拉
    3. akshare 失败 → 用 raw_dir/spot_em.csv（如果存在）
    4. raw 也没有 → 用 stale cached_spot + warning（保底）
    """
    cached_spot = _read_valid_spot(normalized_path) if resume else None

    # 路径 1：cached 有效且日期匹配
    if cached_spot is not None and target is not None and _frame_dates_match_target(cached_spot, target):
        return cached_spot

    # 路径 2：尝试 akshare 重拉
    try:
        spot_raw = _call_akshare(ak, "fund_etf_spot_em")
        _write_csv(spot_raw, raw_path)
        spot = normalize_spot(spot_raw)
        _write_csv(spot, normalized_path)
        return spot
    except Exception as ak_exc:  # noqa: BLE001 - 回退到 raw/stale
        # 路径 3：akshare 失败，尝试 raw
        if raw_path.exists():
            if warnings is not None:
                warnings.append(f"akshare fund_etf_spot_em failed: {ak_exc}; using raw spot snapshot")
            raw_frame = _read_csv(raw_path)
            if raw_frame is not None:
                spot = normalize_spot(raw_frame)
                _write_csv(spot, normalized_path)
                return spot

        # 路径 4：raw 也没有，回退 stale cached
        if cached_spot is not None:
            if warnings is not None:
                warnings.append(
                    f"akshare fund_etf_spot_em failed and no raw spot available: {ak_exc}; "
                    "using stale cached spot snapshot"
                )
            return cached_spot

        raise


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
            if attempt < 1:
                delay = 0.2 * (2 ** attempt) + random.uniform(0, 0.1)
                time.sleep(delay)

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


_HISTORY_SSL_CONTEXT: ssl.SSLContext | None = None
_HISTORY_SSL_LOCK = threading.Lock()


def _get_history_ssl_context() -> ssl.SSLContext:
    global _HISTORY_SSL_CONTEXT
    if _HISTORY_SSL_CONTEXT is None:
        with _HISTORY_SSL_LOCK:
            if _HISTORY_SSL_CONTEXT is None:
                ctx = ssl.create_default_context(cafile=certifi.where())
                ctx.set_alpn_protocols(["http/1.1"])
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                _HISTORY_SSL_CONTEXT = ctx
    return _HISTORY_SSL_CONTEXT


_EASTMONEY_OPENER_INSTALLED = False
_EASTMONEY_OPENER_LOCK = threading.Lock()


def _ensure_eastmoney_opener() -> None:
    """Install a shared opener so urllib.request.urlopen reuses TCP connections."""
    global _EASTMONEY_OPENER_INSTALLED
    if not _EASTMONEY_OPENER_INSTALLED:
        with _EASTMONEY_OPENER_LOCK:
            if not _EASTMONEY_OPENER_INSTALLED:
                https_handler = urllib.request.HTTPSHandler(context=_get_history_ssl_context())
                opener = urllib.request.build_opener(https_handler)
                opener.addheaders = [("User-Agent", _EASTMONEY_UA)]
                urllib.request.install_opener(opener)
                _EASTMONEY_OPENER_INSTALLED = True


def _fetch_history_eastmoney_urllib(url: str) -> pd.DataFrame:
    _ensure_eastmoney_opener()
    req = urllib.request.Request(url, headers={"User-Agent": _EASTMONEY_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
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


def _fetch_sse_scales(
    ak: Any,
    start_ymd: str,
    end_ymd: str,
    raw_scale_dir: Path,
    errors: list[str],
    abort: _AbortEvent | None = None,
    completed_days: set[str] | None = None,
    checkpoint_root: Path | None = None,
    data_date: str = "",
) -> list[pd.DataFrame]:
    if not hasattr(ak, "fund_etf_scale_sse"):
        errors.append("SSE scale: akshare.fund_etf_scale_sse is unavailable")
        return []

    start = pd.to_datetime(start_ymd, format="%Y%m%d")
    end = pd.to_datetime(end_ymd, format="%Y%m%d")
    all_days = list(pd.bdate_range(start, end))

    # Filter out already completed days (checkpoint resume)
    skip_days = completed_days or set()
    pending_days = [d for d in all_days if d.strftime("%Y%m%d") not in skip_days]

    if not pending_days:
        return []

    frames: list[pd.DataFrame] = []
    errors_lock = threading.Lock()
    frames_lock = threading.Lock()
    completed_lock = threading.Lock()
    current_completed = set(skip_days)

    def _fetch_one_day(current: pd.Timestamp) -> pd.DataFrame | None:
        ymd = current.strftime("%Y%m%d")
        if abort and abort.is_set():
            return None
        try:
            raw = _call_akshare(ak, "fund_etf_scale_sse", date=ymd)
            if raw.empty:
                with completed_lock:
                    current_completed.add(ymd)
                _write_sse_checkpoint_partial(checkpoint_root, data_date, current_completed)
                return None
            _write_csv(raw, raw_scale_dir / f"sse_{ymd}.csv")
            frame = normalize_scale(raw, "sse", current.strftime("%Y-%m-%d"))
            with completed_lock:
                current_completed.add(ymd)
            _write_sse_checkpoint_partial(checkpoint_root, data_date, current_completed)
            return frame
        except Exception as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(f"SSE scale {ymd}: {exc}")
            with completed_lock:
                current_completed.add(ymd)
            _write_sse_checkpoint_partial(checkpoint_root, data_date, current_completed)
            return None

    workers = min(SSE_FETCH_MAX_WORKERS, len(pending_days))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_day = {
            executor.submit(_fetch_one_day, day): day
            for day in pending_days
        }
        for future in as_completed(future_to_day):
            if abort and abort.is_set():
                for f in future_to_day:
                    f.cancel()
                break
            try:
                result = future.result(timeout=SSE_FETCH_PER_DAY_TIMEOUT)
            except FutureTimeoutError:
                day = future_to_day[future]
                with errors_lock:
                    errors.append(f"SSE scale {day.strftime('%Y%m%d')}: timed out after {SSE_FETCH_PER_DAY_TIMEOUT}s")
                continue
            except Exception:
                continue
            if result is not None:
                with frames_lock:
                    frames.append(result)

    # Merge in frames from completed days that were skipped
    if skip_days:
        for day_str in sorted(skip_days):
            if day_str not in {d.strftime("%Y%m%d") for d in pending_days}:
                scale_path = raw_scale_dir / f"sse_{day_str}.csv"
                raw = _read_csv(scale_path)
                if raw is not None and not raw.empty:
                    try:
                        day_ts = pd.Timestamp(day_str)
                        frames.append(normalize_scale(raw, "sse", day_ts.strftime("%Y-%m-%d")))
                    except Exception:
                        pass

    return frames


def _write_sse_checkpoint_partial(root: Path | None, data_date: str, completed_days: set[str]) -> None:
    """Write SSE checkpoint if root is provided."""
    if root is None or not data_date:
        return
    _write_sse_checkpoint(root, data_date, completed_days)


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
