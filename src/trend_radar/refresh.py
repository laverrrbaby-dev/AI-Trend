"""Refresh orchestration for the local ETF radar cache."""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import pandas as pd

from .config import (
    ASIA_SHANGHAI,
    DEFAULT_HISTORY_FETCH_MAX_WORKERS,
    MARKET_COMPLETE_TIME,
    PROJECT_ROOT,
    ensure_data_dirs,
    now_shanghai,
)
from .fetcher import _valid_normalized_history_metadata, fetch_data
from .scoring import generate_scores


REFRESH_STATUS_FILE = "refresh_status.json"
REFRESH_LOCK_FILE = "refresh.lock"

REFRESH_IDLE = "idle"
REFRESH_RUNNING = "running"
REFRESH_SUCCEEDED = "succeeded"
REFRESH_FAILED = "failed"
REFRESH_SKIPPED = "skipped"
HISTORY_COMPLETE_COVERAGE_RATIO = 0.95


@dataclass(frozen=True)
class RefreshResult:
    status: str
    target_date: str
    actual_date: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    generated_at: str | None = None
    radar_csv: Path | None = None
    radar_json: Path | None = None
    error: str | None = None
    message: str | None = None
    updated_at: str | None = None
    phase: str | None = None
    progress: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target_date": self.target_date,
            "actual_date": self.actual_date,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "generated_at": self.generated_at,
            "radar_csv": str(self.radar_csv) if self.radar_csv else None,
            "radar_json": str(self.radar_json) if self.radar_json else None,
            "error": self.error,
            "message": self.message,
            "updated_at": self.updated_at,
            "phase": self.phase,
            "progress": self.progress,
        }


@dataclass(frozen=True)
class ProblemSnapshot:
    data_date: str
    candidate_total: int
    valid_history_files: int
    missing_or_invalid_history: int
    report_history_failed: int
    legacy_history_errors: int
    failed_codes: tuple[str, ...] = ()

    @property
    def problem_count(self) -> int:
        return max(self.missing_or_invalid_history, self.report_history_failed, self.legacy_history_errors)


def resolve_refresh_target_date(now: datetime | None = None) -> str:
    """Resolve the newest complete A-share weekday date in Shanghai time."""

    current = now.astimezone(ASIA_SHANGHAI) if now else now_shanghai()
    target = current.date()
    if target.weekday() < 5 and current.time() < MARKET_COMPLETE_TIME:
        target = target - timedelta(days=1)
    return _previous_weekday(target).isoformat()


def refresh_latest_data(
    root: Path = PROJECT_ROOT,
    force: bool = False,
    now: datetime | None = None,
    fetcher: Callable[..., Any] = fetch_data,
    scorer: Callable[..., Any] = generate_scores,
) -> RefreshResult:
    """Fetch and score the newest complete trading-day data if needed."""

    target_date = resolve_refresh_target_date(now)
    ensure_data_dirs(root)

    lock_path = _lock_path(root)
    with _RefreshLock.try_acquire(lock_path) as lock:
        if lock is None:
            status = read_refresh_status(root)
            if status.get("status") == REFRESH_RUNNING:
                return _result_from_payload(status, target_date)
            return RefreshResult(
                status=REFRESH_RUNNING,
                target_date=str(status.get("target_date") or target_date),
                actual_date=_optional_str(status.get("actual_date")),
                started_at=_optional_str(status.get("started_at")),
                message="已有更新任务正在运行",
            )

        started_at = _timestamp()
        _write_status(
            root,
            RefreshResult(
                status=REFRESH_RUNNING,
                target_date=target_date,
                started_at=started_at,
                updated_at=started_at,
                phase="fetch_spot",
                progress=_progress_payload(0, 0),
                message=f"正在更新 {target_date} 数据",
            ),
        )

        try:
            existing_radar = root / "data" / "radar" / f"{target_date}.csv"
            dates_to_refresh = (
                [target_date] if force or not existing_radar.exists() else []
            )
            if not dates_to_refresh:
                result = RefreshResult(
                    status=REFRESH_SKIPPED,
                    target_date=target_date,
                    actual_date=target_date,
                    started_at=started_at,
                    finished_at=_timestamp(),
                    generated_at=_read_radar_generated_at(root, target_date),
                    radar_csv=existing_radar,
                    radar_json=root / "data" / "radar" / f"{target_date}.json",
                    updated_at=_timestamp(),
                    phase="done",
                    message=f"当前已是最新 {target_date} 数据",
                )
                _write_status(root, result)
                return result

            score_result = None
            actual_date = target_date if existing_radar.exists() else None
            for refresh_date in dates_to_refresh:

                def progress_callback(progress: dict[str, Any], refresh_date: str = refresh_date) -> None:
                    phase = str(progress.get("phase") or "fetch_history")
                    _write_status(
                        root,
                        RefreshResult(
                            status=REFRESH_RUNNING,
                            target_date=target_date,
                            started_at=started_at,
                            updated_at=_timestamp(),
                            phase=phase,
                            progress=_progress_from_callback(progress),
                            message=f"正在更新 {refresh_date} 数据",
                        ),
                    )

                fetcher(
                    data_date=refresh_date,
                    root=root,
                    progress_callback=progress_callback,
                    resume=not force,
                    max_workers=DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                )
                actual_date = _detect_complete_history_date(root, refresh_date) or refresh_date
                source_date = refresh_date if actual_date != refresh_date else None
                _write_status(
                    root,
                    RefreshResult(
                        status=REFRESH_RUNNING,
                        target_date=target_date,
                        actual_date=actual_date,
                        started_at=started_at,
                        updated_at=_timestamp(),
                        phase="score",
                        progress=_progress_payload(0, 0),
                        message=f"正在评分 {actual_date} 数据",
                    ),
                )
                score_result = scorer(data_date=actual_date, root=root, source_date=source_date)
                _remove_incomplete_target_outputs(root, refresh_date, actual_date)
                if refresh_date == target_date:
                    _promote_latest_from_radar(root, actual_date)

            target_refreshed = target_date in dates_to_refresh
            final_actual_date = (actual_date or target_date) if target_refreshed else target_date
            final_radar_csv = (
                Path(score_result.radar_csv)
                if target_refreshed and score_result
                else root / "data" / "radar" / f"{final_actual_date}.csv"
            )
            final_radar_json = (
                Path(score_result.radar_json)
                if target_refreshed and score_result
                else root / "data" / "radar" / f"{final_actual_date}.json"
            )
            result = RefreshResult(
                status=REFRESH_SUCCEEDED,
                target_date=target_date,
                actual_date=final_actual_date,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                generated_at=_read_radar_generated_at(root, final_actual_date),
                radar_csv=final_radar_csv,
                radar_json=final_radar_json,
                phase="done",
                message=_refresh_success_message(target_date, final_actual_date, dates_to_refresh),
            )
            _write_status(root, result)
            return result
        except Exception as exc:  # noqa: BLE001 - status must preserve failures for the UI
            current_status = read_refresh_status(root)
            result = RefreshResult(
                status=REFRESH_FAILED,
                target_date=target_date,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                error=str(exc),
                phase=_optional_str(current_status.get("phase")),
                progress=_optional_progress(current_status.get("progress")),
                message=f"{target_date} 更新失败",
            )
            _write_status(root, result, traceback_text=traceback.format_exc())
            return result


def retry_selected_date(
    data_date: str,
    root: Path = PROJECT_ROOT,
    fetcher: Callable[..., Any] = fetch_data,
    scorer: Callable[..., Any] = generate_scores,
) -> RefreshResult:
    """Re-fetch missing/invalid cached inputs for one selected date and rescore it."""

    target_date = pd.Timestamp(data_date).date().isoformat()
    ensure_data_dirs(root)

    lock_path = _lock_path(root)
    with _RefreshLock.try_acquire(lock_path) as lock:
        if lock is None:
            status = read_refresh_status(root)
            if status.get("status") == REFRESH_RUNNING:
                return _result_from_payload(status, target_date)
            return RefreshResult(
                status=REFRESH_RUNNING,
                target_date=str(status.get("target_date") or target_date),
                actual_date=_optional_str(status.get("actual_date")),
                started_at=_optional_str(status.get("started_at")),
                message="已有更新任务正在运行",
            )

        started_at = _timestamp()
        _write_status(
            root,
            RefreshResult(
                status=REFRESH_RUNNING,
                target_date=target_date,
                actual_date=target_date,
                started_at=started_at,
                updated_at=started_at,
                phase="fetch_spot",
                progress=_progress_payload(0, 0),
                message=f"正在补拉 {target_date} 数据",
            ),
        )

        try:
            def progress_callback(progress: dict[str, Any]) -> None:
                phase = str(progress.get("phase") or "fetch_history")
                _write_status(
                    root,
                    RefreshResult(
                        status=REFRESH_RUNNING,
                        target_date=target_date,
                        actual_date=target_date,
                        started_at=started_at,
                        updated_at=_timestamp(),
                        phase=phase,
                        progress=_progress_from_callback(progress),
                        message=f"正在补拉 {target_date} 数据",
                    ),
                )

            fetcher(
                data_date=target_date,
                root=root,
                progress_callback=progress_callback,
                resume=True,
                max_workers=DEFAULT_HISTORY_FETCH_MAX_WORKERS,
            )
            _write_status(
                root,
                RefreshResult(
                    status=REFRESH_RUNNING,
                    target_date=target_date,
                    actual_date=target_date,
                    started_at=started_at,
                    updated_at=_timestamp(),
                    phase="score",
                    progress=_progress_payload(0, 0),
                    message=f"正在评分 {target_date} 数据",
                ),
            )
            score_result = scorer(data_date=target_date, root=root)
            result = RefreshResult(
                status=REFRESH_SUCCEEDED,
                target_date=target_date,
                actual_date=target_date,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                generated_at=_read_radar_generated_at(root, target_date),
                radar_csv=Path(score_result.radar_csv),
                radar_json=Path(score_result.radar_json),
                phase="done",
                message=_retry_success_message(root, target_date),
            )
            _write_status(root, result)
            return result
        except Exception as exc:  # noqa: BLE001 - status must preserve failures for the UI
            current_status = read_refresh_status(root)
            result = RefreshResult(
                status=REFRESH_FAILED,
                target_date=target_date,
                actual_date=target_date,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                error=str(exc),
                phase=_optional_str(current_status.get("phase")),
                progress=_optional_progress(current_status.get("progress")),
                message=f"{target_date} 补拉失败",
            )
            _write_status(root, result, traceback_text=traceback.format_exc())
            return result


def retry_problem_snapshots(
    root: Path = PROJECT_ROOT,
    fetcher: Callable[..., Any] = fetch_data,
    scorer: Callable[..., Any] = generate_scores,
) -> RefreshResult:
    """Resume-fetch every normalized source snapshot with missing or failed history."""

    ensure_data_dirs(root)
    target_label = "存量问题"

    lock_path = _lock_path(root)
    with _RefreshLock.try_acquire(lock_path) as lock:
        if lock is None:
            status = read_refresh_status(root)
            if status.get("status") == REFRESH_RUNNING:
                return _result_from_payload(status, target_label)
            return RefreshResult(
                status=REFRESH_RUNNING,
                target_date=str(status.get("target_date") or target_label),
                actual_date=_optional_str(status.get("actual_date")),
                started_at=_optional_str(status.get("started_at")),
                message="已有更新任务正在运行",
            )

        started_at = _timestamp()
        _write_status(
            root,
            RefreshResult(
                status=REFRESH_RUNNING,
                target_date=target_label,
                started_at=started_at,
                updated_at=started_at,
                phase="retry_scan",
                progress=_progress_payload(0, 0),
                message="正在扫描存量问题",
            ),
        )

        try:
            problems = scan_problem_snapshots(root)
            if not problems:
                result = RefreshResult(
                    status=REFRESH_SKIPPED,
                    target_date=target_label,
                    started_at=started_at,
                    finished_at=_timestamp(),
                    updated_at=_timestamp(),
                    phase="done",
                    message="没有需要补拉的存量问题",
                )
                _write_status(root, result)
                return result

            total_snapshots = len(problems)
            for index, problem in enumerate(problems, start=1):

                def progress_callback(progress: dict[str, Any], problem: ProblemSnapshot = problem, index: int = index) -> None:
                    phase = str(progress.get("phase") or "fetch_history")
                    _write_status(
                        root,
                        RefreshResult(
                            status=REFRESH_RUNNING,
                            target_date=target_label,
                            started_at=started_at,
                            updated_at=_timestamp(),
                            phase=f"retry_{phase}",
                            progress=_retry_fetch_progress(progress, index - 1, total_snapshots, problem.data_date),
                            message=f"正在补拉存量问题 {problem.data_date}",
                        ),
                    )

                fetcher(
                    data_date=problem.data_date,
                    root=root,
                    progress_callback=progress_callback,
                    resume=True,
                    max_workers=DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                )

            score_targets = _score_targets_for_sources(root, [problem.data_date for problem in problems])
            score_total = len(score_targets)
            scored_results = []
            for index, (score_date, source_date) in enumerate(score_targets, start=1):
                _write_status(
                    root,
                    RefreshResult(
                        status=REFRESH_RUNNING,
                        target_date=target_label,
                        started_at=started_at,
                        updated_at=_timestamp(),
                        phase="retry_score",
                        progress=_retry_score_progress(index - 1, score_total, score_date, source_date),
                        message=f"正在重算 {score_date} 数据",
                    ),
                )
                scored_results.append(scorer(data_date=score_date, root=root, source_date=source_date))

            remaining = problem_snapshot_summary(root)
            result = RefreshResult(
                status=REFRESH_SUCCEEDED,
                target_date=target_label,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                phase="done",
                message=_problem_retry_success_message(total_snapshots, score_total, remaining),
                radar_csv=Path(scored_results[-1].radar_csv) if scored_results else None,
                radar_json=Path(scored_results[-1].radar_json) if scored_results else None,
            )
            _write_status(root, result)
            return result
        except Exception as exc:  # noqa: BLE001 - status must preserve failures for the UI
            current_status = read_refresh_status(root)
            result = RefreshResult(
                status=REFRESH_FAILED,
                target_date=target_label,
                started_at=started_at,
                finished_at=_timestamp(),
                updated_at=_timestamp(),
                error=str(exc),
                phase=_optional_str(current_status.get("phase")),
                progress=_optional_progress(current_status.get("progress")),
                message="存量问题补拉失败",
            )
            _write_status(root, result, traceback_text=traceback.format_exc())
            return result


def scan_problem_snapshots(root: Path = PROJECT_ROOT) -> list[ProblemSnapshot]:
    normalized_root = root / "data" / "normalized"
    if not normalized_root.exists():
        return []

    problems: list[ProblemSnapshot] = []
    for source_dir in sorted(path for path in normalized_root.iterdir() if path.is_dir()):
        source_date = source_dir.name
        try:
            target = pd.Timestamp(source_date)
        except ValueError:
            continue

        candidate_codes = sorted(_candidate_codes(root, source_date))
        candidate_total = len(candidate_codes)
        valid_history_files = 0
        missing_codes: list[str] = []
        history_dir = source_dir / "history"
        for code in candidate_codes:
            path = history_dir / f"{code}.csv"
            if _valid_normalized_history_metadata(path, target) is None:
                missing_codes.append(code)
            else:
                valid_history_files += 1

        report = _read_json(source_dir / "fetch_report.json")
        report_history_failed = _optional_int(report, "history_failed") or 0
        legacy_codes = _legacy_history_error_codes(report)
        report_codes = _failed_history_codes(report)
        failed_codes = tuple(dict.fromkeys([*missing_codes, *report_codes, *legacy_codes]))
        missing_or_invalid = max(0, candidate_total - valid_history_files)

        if missing_or_invalid or report_history_failed or legacy_codes:
            problems.append(
                ProblemSnapshot(
                    data_date=source_date,
                    candidate_total=candidate_total,
                    valid_history_files=valid_history_files,
                    missing_or_invalid_history=missing_or_invalid,
                    report_history_failed=report_history_failed,
                    legacy_history_errors=len(legacy_codes),
                    failed_codes=failed_codes,
                )
            )
    return problems


def problem_snapshot_summary(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    problems = scan_problem_snapshots(root)
    return {
        "snapshot_count": len(problems),
        "problem_count": int(sum(problem.problem_count for problem in problems)),
        "dates": [problem.data_date for problem in problems],
    }


def read_refresh_status(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = _status_path(root)
    if not path.exists():
        return {"status": REFRESH_IDLE}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": REFRESH_IDLE, "error": f"无法读取刷新状态: {exc}"}
    if not isinstance(payload, dict):
        return {"status": REFRESH_IDLE}
    return payload


def refresh_task_is_active(root: Path = PROJECT_ROOT) -> bool:
    path = _lock_path(root)
    return path.exists() and not _lock_is_recoverable(path)


def _previous_weekday(value: date) -> date:
    current = value
    while current.weekday() >= 5:
        current = current - timedelta(days=1)
    return current


def _refresh_date_sequence(root: Path, target_date: str, force: bool) -> list[str]:
    dates = _missing_weekday_radar_dates(root, target_date)
    target_radar = root / "data" / "radar" / f"{target_date}.csv"
    if force or not target_radar.exists():
        dates.append(target_date)
    return list(dict.fromkeys(dates))


def _missing_weekday_radar_dates(root: Path, target_date: str) -> list[str]:
    target = date.fromisoformat(target_date)
    radar_dir = root / "data" / "radar"
    if not radar_dir.exists():
        return []

    existing_dates: set[date] = set()
    for path in radar_dir.glob("*.csv"):
        try:
            data_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if data_date <= target:
            existing_dates.add(data_date)

    previous_dates = [data_date for data_date in existing_dates if data_date < target]
    if not previous_dates:
        return []

    missing: list[str] = []
    current = max(previous_dates) + timedelta(days=1)
    while current <= target:
        if current.weekday() < 5 and current not in existing_dates:
            missing.append(current.isoformat())
        current = current + timedelta(days=1)
    return missing


def _detect_complete_history_date(
    root: Path,
    source_date: str,
    min_coverage_ratio: float = HISTORY_COMPLETE_COVERAGE_RATIO,
) -> str | None:
    history_dir = root / "data" / "normalized" / source_date / "history"
    if not history_dir.exists():
        return None

    target = pd.Timestamp(source_date)
    candidate_codes = _candidate_codes(root, source_date)
    history_codes: set[str] = set()
    date_counts: dict[str, int] = {}
    for path in sorted(history_dir.glob("*.csv")):
        code = path.stem.zfill(6)
        if candidate_codes and code not in candidate_codes:
            continue
        try:
            frame = pd.read_csv(path, usecols=["日期"])
        except (OSError, ValueError, pd.errors.EmptyDataError):
            continue
        dates = pd.to_datetime(frame["日期"], errors="coerce").dropna()
        dates = dates[dates <= target]
        if dates.empty:
            continue
        history_codes.add(code)
        for item in dates.dt.date.astype(str).unique():
            date_counts[str(item)] = date_counts.get(str(item), 0) + 1

    denominator = len(candidate_codes) if candidate_codes else len(history_codes)
    if not denominator or not date_counts:
        return None
    min_count = max(1, int(denominator * min_coverage_ratio + 0.999999))
    complete_dates = sorted(date_key for date_key, count in date_counts.items() if count >= min_count)
    if complete_dates:
        return complete_dates[-1]
    best_date = max(date_counts, key=lambda date_key: (date_counts[date_key], date_key))
    return best_date


def _detect_latest_history_date(root: Path, source_date: str) -> str | None:
    return _detect_complete_history_date(root, source_date)


def _candidate_codes(root: Path, source_date: str) -> set[str]:
    path = root / "data" / "normalized" / source_date / "spot_candidates.csv"
    if not path.exists():
        return set()
    try:
        frame = pd.read_csv(path, usecols=["code"], dtype={"code": str})
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return set()
    if frame.empty:
        return set()
    return set(frame["code"].astype(str).str.zfill(6))


def _read_radar_generated_at(root: Path, data_date: str) -> str | None:
    path = root / "data" / "radar" / f"{data_date}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("generated_at")
    return str(value) if value else None


def _remove_incomplete_target_outputs(root: Path, target_date: str, actual_date: str) -> None:
    if actual_date == target_date:
        return
    radar_dir = root / "data" / "radar"
    for suffix in (".csv", ".json"):
        try:
            (radar_dir / f"{target_date}{suffix}").unlink()
        except FileNotFoundError:
            pass


def _promote_latest_from_radar(root: Path, data_date: str) -> None:
    radar_json = root / "data" / "radar" / f"{data_date}.json"
    latest_json = root / "data" / "state" / "latest.json"
    try:
        payload = json.loads(radar_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    latest_json.parent.mkdir(parents=True, exist_ok=True)
    temp_path = latest_json.with_name(f"{latest_json.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(latest_json)


def _score_targets_for_sources(root: Path, source_dates: list[str]) -> list[tuple[str, str]]:
    source_set = set(source_dates)
    by_source: dict[str, set[str]] = {source_date: set() for source_date in source_dates}
    radar_dir = root / "data" / "radar"
    if radar_dir.exists():
        for path in sorted(radar_dir.glob("*.json")):
            payload = _read_json(path)
            source_date = str(payload.get("source_date") or path.stem)
            if source_date in source_set:
                data_date = str(payload.get("data_date") or path.stem)
                by_source[source_date].add(data_date)

    targets: list[tuple[str, str]] = []
    for source_date in source_dates:
        dates = sorted(by_source.get(source_date) or {source_date})
        targets.extend((data_date, source_date) for data_date in dates)
    return list(dict.fromkeys(targets))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _failed_history_codes(report: dict[str, Any]) -> list[str]:
    failed_history = report.get("failed_history")
    codes: list[str] = []
    if isinstance(failed_history, list):
        for item in failed_history:
            if isinstance(item, dict):
                raw_code = str(item.get("code") or "").strip()
                code = raw_code.zfill(6)
                if raw_code and len(code) == 6 and code.isdigit() and code not in codes:
                    codes.append(code)
    return codes


def _legacy_history_error_codes(report: dict[str, Any]) -> list[str]:
    errors = report.get("errors")
    if not isinstance(errors, list):
        return []
    codes: list[str] = []
    for item in errors:
        code = str(item).split(":", 1)[0].strip().zfill(6)
        if len(code) == 6 and code.isdigit() and code not in codes:
            codes.append(code)
    return codes


def _retry_fetch_progress(
    progress: dict[str, Any],
    snapshot_completed: int,
    snapshot_total: int,
    current_source_date: str,
) -> dict[str, Any]:
    payload = _progress_from_callback(progress)
    payload.update(
        {
            "snapshot_completed": int(snapshot_completed),
            "snapshot_total": int(snapshot_total),
            "current_source_date": current_source_date,
        }
    )
    return payload


def _retry_score_progress(score_completed: int, score_total: int, current_score_date: str, current_source_date: str) -> dict[str, Any]:
    payload = _progress_payload(score_completed, score_total)
    payload.update(
        {
            "score_completed": int(score_completed),
            "score_total": int(score_total),
            "current_score_date": current_score_date,
            "current_source_date": current_source_date,
        }
    )
    return payload


def _problem_retry_success_message(snapshot_count: int, score_count: int, remaining: dict[str, Any]) -> str:
    remaining_snapshots = _safe_int(remaining.get("snapshot_count"))
    remaining_problems = _safe_int(remaining.get("problem_count"))
    suffix = f"；剩余问题 {remaining_snapshots} 个日期，失败/缺失 {remaining_problems} 个历史文件" if remaining_snapshots else ""
    return f"已补拉 {snapshot_count} 个存量快照并重算 {score_count} 个排行榜{suffix}"


def _write_status(root: Path, result: RefreshResult, traceback_text: str | None = None) -> None:
    path = _status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_payload()
    if traceback_text:
        payload["traceback"] = traceback_text
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _result_from_payload(payload: dict[str, Any], fallback_target_date: str) -> RefreshResult:
    return RefreshResult(
        status=str(payload.get("status") or REFRESH_IDLE),
        target_date=str(payload.get("target_date") or fallback_target_date),
        actual_date=_optional_str(payload.get("actual_date")),
        started_at=_optional_str(payload.get("started_at")),
        finished_at=_optional_str(payload.get("finished_at")),
        generated_at=_optional_str(payload.get("generated_at")),
        radar_csv=_optional_path(payload.get("radar_csv")),
        radar_json=_optional_path(payload.get("radar_json")),
        error=_optional_str(payload.get("error")),
        message=_optional_str(payload.get("message")),
        updated_at=_optional_str(payload.get("updated_at")),
        phase=_optional_str(payload.get("phase")),
        progress=_optional_progress(payload.get("progress")),
    )


def _refresh_success_message(target_date: str, actual_date: str, refreshed_dates: list[str]) -> str:
    target_refreshed = target_date in refreshed_dates
    base = _success_message(target_date, actual_date) if target_refreshed else f"当前已是最新 {target_date} 数据"
    gap_dates = [data_date for data_date in refreshed_dates if data_date != target_date]
    if not gap_dates:
        return base
    return f"已补齐 {len(gap_dates)} 个缺失日期：{', '.join(gap_dates)}；{base}"


def _success_message(target_date: str, actual_date: str) -> str:
    if actual_date == target_date:
        return f"已生成 {actual_date} 数据"
    return f"数据源最新为 {actual_date}，已生成对应数据"


def _retry_success_message(root: Path, data_date: str) -> str:
    summary = _fetch_report_history_summary(root, data_date)
    if summary is None:
        return f"已补拉并生成 {data_date} 数据"
    return (
        f"已补拉并生成 {data_date} 数据：历史行情成功 "
        f"{summary['success']}/{summary['total']}，失败 {summary['failed']}"
    )


def _fetch_report_history_summary(root: Path, data_date: str) -> dict[str, int] | None:
    path = root / "data" / "normalized" / data_date / "fetch_report.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    total = _optional_int(payload, "history_total")
    if total is None:
        total = _optional_int(payload, "candidate_rows")
    success = _optional_int(payload, "history_success")
    if success is None:
        success = (_optional_int(payload, "skipped_cached") or 0) + (_optional_int(payload, "fetched_history_files") or 0)
    failed = _optional_int(payload, "history_failed")
    if failed is None:
        failed = _optional_int(payload, "history_errors") or 0
    if total is None:
        return None
    return {"total": int(total), "success": int(success), "failed": int(failed)}


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    try:
        return int(payload[key])
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_path(value: object) -> Path | None:
    if not value:
        return None
    return Path(str(value))


def _optional_progress(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return dict(value)


def _progress_from_callback(progress: dict[str, Any]) -> dict[str, Any]:
    return _progress_payload(
        progress.get("completed"),
        progress.get("total"),
        current_code=progress.get("current_code"),
        current_name=progress.get("current_name"),
        skipped_cached=progress.get("skipped_cached"),
        fetched=progress.get("fetched"),
        errors=progress.get("errors"),
        max_workers=progress.get("max_workers"),
    )


def _progress_payload(
    completed: object,
    total: object,
    current_code: object | None = None,
    current_name: object | None = None,
    skipped_cached: object | None = None,
    fetched: object | None = None,
    errors: object | None = None,
    max_workers: object | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "completed": _safe_int(completed),
        "total": _safe_int(total),
        "current_code": _optional_str(current_code),
        "current_name": _optional_str(current_name),
        "skipped_cached": _safe_int(skipped_cached),
        "fetched": _safe_int(fetched),
        "errors": _safe_int(errors),
        "max_workers": _safe_int(max_workers),
    }
    return payload


def _safe_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _timestamp() -> str:
    return now_shanghai().strftime("%Y-%m-%d %H:%M:%S")


def _status_path(root: Path) -> Path:
    return root / "data" / "state" / REFRESH_STATUS_FILE


def _lock_path(root: Path) -> Path:
    return root / "data" / "state" / REFRESH_LOCK_FILE


class _RefreshLock:
    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd = fd

    @classmethod
    def try_acquire(cls, path: Path) -> "_RefreshLockContext":
        return _RefreshLockContext(path)

    def release(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class _RefreshLockContext:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock: _RefreshLock | None = None

    def __enter__(self) -> _RefreshLock | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self._open_lock()
        return self.lock

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.lock:
            self.lock.release()

    def _open_lock(self) -> _RefreshLock | None:
        try:
            return self._create_lock()
        except FileExistsError:
            if _lock_is_recoverable(self.path):
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    return self._create_lock()
                except FileExistsError:
                    return None
            return None

    def _create_lock(self) -> _RefreshLock:
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        payload = json.dumps({"pid": os.getpid(), "created_at": _timestamp()}, ensure_ascii=False)
        os.write(fd, payload.encode("utf-8"))
        return _RefreshLock(self.path, fd)


def _lock_is_recoverable(path: Path) -> bool:
    lock_payload = _read_lock_payload(path)
    pid = lock_payload.get("pid")
    if not isinstance(pid, int):
        return True
    return not _pid_is_running(pid)


def _read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = [
    "REFRESH_FAILED",
    "REFRESH_IDLE",
    "REFRESH_RUNNING",
    "REFRESH_SKIPPED",
    "REFRESH_SUCCEEDED",
    "ProblemSnapshot",
    "RefreshResult",
    "problem_snapshot_summary",
    "read_refresh_status",
    "refresh_task_is_active",
    "refresh_latest_data",
    "retry_problem_snapshots",
    "retry_selected_date",
    "scan_problem_snapshots",
    "resolve_refresh_target_date",
]
