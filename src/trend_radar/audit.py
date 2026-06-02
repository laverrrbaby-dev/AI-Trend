"""End-to-end audit and repair for radar Top30 data."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from .backfill import recent_trading_dates
from .config import MIN_LISTING_DAYS, PROJECT_ROOT, now_shanghai
from .fetcher import (
    fetch_data,
    _fetch_history_eastmoney_with_provider,
    _fetch_history_sina,
    _load_akshare,
)
from .filters import classify_etf
from .normalization import normalize_history
from .scoring import generate_scores
from .validation import validate_outputs


ExternalHistoryFetcher = Callable[[str, str, str], tuple[pd.DataFrame, str]]


@dataclass(frozen=True)
class AuditResult:
    start: str
    end: str
    checked_dates: list[str]
    ok: bool
    repaired: bool
    errors: list[str]
    warnings: list[str]
    report_path: Path


@dataclass(frozen=True)
class _SourceUniverse:
    source_date: str
    candidate_codes: set[str]
    histories: dict[str, pd.DataFrame]
    history_order: list[str]
    spot_amounts: dict[str, float]
    spot_dates: dict[str, str]
    spot_names: dict[str, str]


def audit_outputs(
    start: str,
    end: str,
    external: bool = False,
    repair: bool = False,
    root: Path = PROJECT_ROOT,
    external_fetcher: ExternalHistoryFetcher | None = None,
    fetcher: Callable[..., Any] = fetch_data,
    scorer: Callable[..., Any] = generate_scores,
) -> AuditResult:
    """Audit cached radar outputs and optionally repair/regenerate them."""

    report_path = root / "data" / "state" / f"audit_{start}_{end}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    initial = _run_audit(start, end, root, external, external_fetcher)
    repairs: dict[str, Any] = {"attempted": False}
    final = initial

    if repair and initial["errors"]:
        try:
            repairs = _repair_outputs(start, end, root, initial["checked_dates"], initial["source_dates"], fetcher, scorer)
            final = _run_audit(start, end, root, external, external_fetcher)
            final["pre_repair_errors"] = initial["errors"]
            final["pre_repair_warnings"] = initial["warnings"]
        except Exception as exc:  # noqa: BLE001 - report must preserve repair failures
            repairs["error"] = str(exc)
            final = dict(initial)
            final["errors"] = [*initial["errors"], f"repair failed: {exc}"]

    final["repairs"] = repairs
    final["ok"] = not final["errors"]
    final["validated_at"] = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    report_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")

    return AuditResult(
        start=start,
        end=end,
        checked_dates=list(final["checked_dates"]),
        ok=bool(final["ok"]),
        repaired=bool(repairs.get("attempted") and not repairs.get("error")),
        errors=list(final["errors"]),
        warnings=list(final["warnings"]),
        report_path=report_path,
    )


def _run_audit(
    start: str,
    end: str,
    root: Path,
    external: bool,
    external_fetcher: ExternalHistoryFetcher | None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    coverage: list[dict[str, Any]] = []
    top30_diffs: list[dict[str, Any]] = []
    spot_history_diffs: list[dict[str, Any]] = []
    external_diffs: list[dict[str, Any]] = []
    provider_counts: Counter[str] = Counter()
    source_cache: dict[str, _SourceUniverse] = {}
    source_dates: set[str] = set()
    source_compare_dates: dict[str, str] = {}

    radar_dates = _radar_dates_between(root, start, end)
    if not radar_dates:
        errors.append(f"No radar outputs found between {start} and {end}")

    for data_date in radar_dates:
        source_date = _source_date_for(root, data_date, errors)
        if source_date is None:
            continue
        source_dates.add(source_date)
        source_compare_dates[source_date] = max(source_compare_dates.get(source_date, data_date), data_date)
        if source_date not in source_cache:
            source_cache[source_date] = _load_source_universe(root, source_date, errors, warnings, coverage)
        _audit_top30_for_date(root, data_date, source_cache[source_date], errors, top30_diffs)
        _audit_spot_history_for_date(data_date, source_cache[source_date], spot_history_diffs)

    validation = validate_outputs(start=start, end=end, root=root)
    if validation.errors:
        errors.extend(f"validation: {item}" for item in validation.errors)
    if validation.warnings:
        warnings.extend(f"validation: {item}" for item in validation.warnings)

    if external:
        fetch = external_fetcher or _default_external_fetcher
        for source in source_cache.values():
            compare_end_date = source_compare_dates.get(source.source_date, source.source_date)
            _audit_external_source(source, compare_end_date, fetch, errors, external_diffs, provider_counts)

    if top30_diffs:
        errors.append(f"Top30/rank recompute mismatches: {len(top30_diffs)}")
    if spot_history_diffs:
        errors.append(f"Spot/history amount mismatches or stale histories: {len(spot_history_diffs)}")
    if external_diffs:
        errors.append(f"External source mismatches or failures: {len(external_diffs)}")

    return {
        "start": start,
        "end": end,
        "checked_dates": radar_dates,
        "source_dates": sorted(source_dates),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "coverage": coverage,
        "top30_diffs": top30_diffs,
        "spot_history_diffs": spot_history_diffs,
        "external_diffs": external_diffs,
        "external_provider_counts": dict(provider_counts),
        "external": bool(external),
    }


def _radar_dates_between(root: Path, start: str, end: str) -> list[str]:
    radar_dir = root / "data" / "radar"
    if not radar_dir.exists():
        return []
    return sorted(path.stem for path in radar_dir.glob("*.csv") if start <= path.stem <= end)


def _source_date_for(root: Path, data_date: str, errors: list[str]) -> str | None:
    path = root / "data" / "radar" / f"{data_date}.json"
    if not path.exists():
        errors.append(f"{data_date}: missing metadata JSON")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{data_date}: failed to read metadata JSON: {exc}")
        return None
    return str(payload.get("source_date") or data_date)


def _load_source_universe(
    root: Path,
    source_date: str,
    errors: list[str],
    warnings: list[str],
    coverage: list[dict[str, Any]],
) -> _SourceUniverse:
    source_dir = root / "data" / "normalized" / source_date
    spot_path = source_dir / "spot_candidates.csv"
    history_dir = source_dir / "history"
    if not spot_path.exists():
        errors.append(f"{source_date}: missing spot_candidates.csv")
        coverage.append({"source_date": source_date, "status": "missing_spot"})
        return _SourceUniverse(source_date, set(), {}, [], {}, {}, {})
    if not history_dir.exists():
        errors.append(f"{source_date}: missing history directory")
        coverage.append({"source_date": source_date, "status": "missing_history_dir"})
        return _SourceUniverse(source_date, set(), {}, [], {}, {}, {})

    spot = pd.read_csv(spot_path, dtype={"code": str})
    spot["code"] = spot.get("code", pd.Series(dtype=str)).astype(str).str.zfill(6)
    spot["amount_yi"] = pd.to_numeric(spot.get("amount_yi", pd.Series(dtype=float)), errors="coerce")
    spot["data_date"] = spot.get("data_date", pd.Series("", index=spot.index)).astype(str)
    decisions = [classify_etf(str(row.get("name", "")), str(row.get("code", ""))) for _, row in spot.iterrows()]
    spot["audit_is_candidate"] = [item[0] for item in decisions]
    spot["audit_reason"] = [item[1] for item in decisions]

    excluded = spot[~spot["audit_is_candidate"]]
    if not excluded.empty:
        sample = excluded[["code", "name", "audit_reason"]].head(20).to_dict(orient="records")
        errors.append(f"{source_date}: spot_candidates contains excluded ETFs {sample}")

    candidate_codes = set(spot.loc[spot["audit_is_candidate"], "code"].astype(str).str.zfill(6))
    candidate_spot = spot[spot["audit_is_candidate"]].copy()
    spot_amounts = {
        str(row["code"]).zfill(6): float(row["amount_yi"])
        for _, row in candidate_spot.dropna(subset=["amount_yi"]).iterrows()
    }
    spot_dates = {
        str(row["code"]).zfill(6): str(row.get("data_date") or "")
        for _, row in candidate_spot.iterrows()
    }
    spot_names = {
        str(row["code"]).zfill(6): str(row.get("name") or "")
        for _, row in candidate_spot.iterrows()
    }
    history_paths = {path.stem.zfill(6): path for path in sorted(history_dir.glob("*.csv"))}
    missing = sorted(candidate_codes - set(history_paths))
    histories: dict[str, pd.DataFrame] = {}
    history_order: list[str] = []
    invalid: list[str] = []
    short: list[dict[str, Any]] = []
    latest_dates: Counter[str] = Counter()

    for code in sorted(candidate_codes & set(history_paths)):
        frame, problem = _read_history(history_paths[code], source_date)
        if frame is None:
            invalid.append(f"{code}:{problem}")
            continue
        histories[code] = frame
        history_order.append(code)
        latest_dates[frame.iloc[-1]["日期"].date().isoformat()] += 1
        if len(frame) < MIN_LISTING_DAYS:
            short.append({"code": code, "rows": int(len(frame)), "latest": frame.iloc[-1]["日期"].date().isoformat()})

    if missing or invalid:
        warnings.append(f"{source_date}: missing/invalid history for {len(missing) + len(invalid)} candidate ETFs")
    if short:
        warnings.append(f"{source_date}: {len(short)} candidate histories have fewer than {MIN_LISTING_DAYS} rows")

    coverage.append(
        {
            "source_date": source_date,
            "spot_candidate_rows": int(len(spot)),
            "audit_candidate_rows": int(len(candidate_codes)),
            "excluded_candidate_rows": int(len(excluded)),
            "history_files": int(len(history_paths)),
            "valid_history_files": int(len(histories)),
            "missing_history_count": int(len(missing)),
            "invalid_history_count": int(len(invalid)),
            "short_history_count": int(len(short)),
            "missing_history_sample": missing[:20],
            "invalid_history_sample": invalid[:20],
            "short_history_sample": short[:20],
            "latest_history_dates": dict(latest_dates),
        }
    )
    return _SourceUniverse(source_date, candidate_codes, histories, history_order, spot_amounts, spot_dates, spot_names)


def _read_history(path: Path, source_date: str) -> tuple[pd.DataFrame | None, str | None]:
    try:
        frame = pd.read_csv(path, dtype={"code": str})
    except EmptyDataError:
        return None, "empty"
    except (OSError, ValueError) as exc:
        return None, str(exc)
    required = {"日期", "收盘价", "成交额(亿)"}
    if frame.empty:
        return None, "empty"
    if not required.issubset(frame.columns):
        return None, f"missing_columns:{sorted(required - set(frame.columns))}"
    frame = frame.copy()
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
    frame["收盘价"] = pd.to_numeric(frame["收盘价"], errors="coerce")
    frame["成交额(亿)"] = pd.to_numeric(frame["成交额(亿)"], errors="coerce")
    frame = frame.dropna(subset=["日期", "收盘价", "成交额(亿)"])
    frame = frame[frame["日期"] <= pd.Timestamp(source_date)].sort_values("日期").reset_index(drop=True)
    if frame.empty:
        return None, "no_valid_rows"
    return frame, None


def _audit_top30_for_date(
    root: Path,
    data_date: str,
    source: _SourceUniverse,
    errors: list[str],
    top30_diffs: list[dict[str, Any]],
) -> None:
    csv_path = root / "data" / "radar" / f"{data_date}.csv"
    if not csv_path.exists():
        errors.append(f"{data_date}: missing radar CSV")
        return
    radar = pd.read_csv(csv_path, dtype={"代码": str})
    if radar.empty:
        errors.append(f"{data_date}: radar CSV is empty")
        return
    radar["代码"] = radar["代码"].astype(str).str.zfill(6)

    invalid_output = sorted(set(radar["代码"]) - source.candidate_codes)
    if invalid_output:
        errors.append(f"{data_date}: radar contains non-candidate ETF codes {invalid_output[:20]}")

    amount_ranks, top30_counts = _recompute_amount_metrics(source, data_date)
    for _, row in radar.iterrows():
        code = str(row["代码"]).zfill(6)
        if code not in source.candidate_codes:
            continue
        expected_rank = amount_ranks.get(code)
        expected_top30 = int(top30_counts.get(code, 0))
        actual_rank = _int_or_none(row.get("成交额排名"))
        actual_top30 = _int_or_none(row.get("成交额Top30次数"))
        if expected_rank is None:
            top30_diffs.append(
                {
                    "date": data_date,
                    "code": code,
                    "name": str(row.get("ETF名称", "")),
                    "problem": "missing_rank_source_history",
                }
            )
            continue
        if actual_rank != expected_rank or actual_top30 != expected_top30:
            top30_diffs.append(
                {
                    "date": data_date,
                    "code": code,
                    "name": str(row.get("ETF名称", "")),
                    "actual_rank": actual_rank,
                    "expected_rank": int(expected_rank),
                    "actual_top30": actual_top30,
                    "expected_top30": expected_top30,
                }
            )


def _audit_spot_history_for_date(
    data_date: str,
    source: _SourceUniverse,
    spot_history_diffs: list[dict[str, Any]],
) -> None:
    if data_date != source.source_date:
        return
    target = pd.Timestamp(data_date)
    for code in sorted(source.candidate_codes):
        spot_amount = source.spot_amounts.get(code)
        history = source.histories.get(code)
        if spot_amount is None or history is None or history.empty:
            continue
        current = history[history["日期"] <= target].sort_values("日期")
        if current.empty:
            continue
        latest = current.iloc[-1]
        history_date = latest["日期"].date().isoformat()
        history_amount = float(latest["成交额(亿)"])
        spot_date = source.spot_dates.get(code, "")
        tolerance = max(0.02, abs(spot_amount) * 0.02)
        stale = history_date != data_date
        mismatched = abs(spot_amount - history_amount) > tolerance
        spot_date_mismatch = bool(spot_date and spot_date != data_date)
        if not (stale or mismatched or spot_date_mismatch):
            continue
        ratio = _amount_ratio(spot_amount, history_amount)
        spot_history_diffs.append(
            {
                "source_date": source.source_date,
                "code": code,
                "name": source.spot_names.get(code, ""),
                "spot_date": spot_date,
                "history_date": history_date,
                "spot_amount_yi": round(float(spot_amount), 6),
                "history_amount_yi": round(history_amount, 6),
                "ratio": round(ratio, 4) if ratio is not None else None,
                "problem": _spot_history_problem(stale, spot_date_mismatch, ratio),
                "unit_mismatch_suspected": _is_unit_ratio(ratio),
            }
        )


def _recompute_amount_metrics(source: _SourceUniverse, data_date: str) -> tuple[dict[str, int], dict[str, int]]:
    latest_rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    target = pd.Timestamp(data_date)
    for code in source.history_order:
        history = source.histories[code]
        current = history[history["日期"] <= target].sort_values("日期")
        if current.empty:
            continue
        latest = current.iloc[-1]
        latest_rows.append({"code": code, "amount": float(latest["成交额(亿)"])})
        records.extend(
            {"code": code, "date": item["日期"], "amount": float(item["成交额(亿)"])}
            for item in current.tail(20).to_dict("records")
        )

    if not latest_rows:
        return {}, {}
    ranked = pd.DataFrame(latest_rows).sort_values("amount", ascending=False).reset_index(drop=True)
    ranks = {str(row["code"]).zfill(6): index for index, row in enumerate(ranked.to_dict("records"), start=1)}
    if not records:
        return ranks, {}
    frame = pd.DataFrame(records)
    frame["rank"] = frame.groupby("date")["amount"].rank(method="first", ascending=False)
    counts = frame[frame["rank"] <= 30].groupby("code").size().astype(int).to_dict()
    return ranks, counts


def _int_or_none(value: object) -> int | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return int(numeric)


def _amount_ratio(left: float, right: float) -> float | None:
    if right == 0:
        return None
    return abs(left / right)


def _is_unit_ratio(ratio: float | None) -> bool:
    if ratio is None:
        return False
    return 80 <= ratio <= 125 or 0.008 <= ratio <= 0.0125


def _spot_history_problem(stale: bool, spot_date_mismatch: bool, ratio: float | None) -> str:
    if _is_unit_ratio(ratio):
        return "suspected_100x_amount_unit_mismatch"
    if stale:
        return "stale_history_for_spot_date"
    if spot_date_mismatch:
        return "spot_date_mismatch"
    return "spot_history_amount_mismatch"


def _audit_external_source(
    source: _SourceUniverse,
    compare_end_date: str,
    external_fetcher: ExternalHistoryFetcher,
    errors: list[str],
    external_diffs: list[dict[str, Any]],
    provider_counts: Counter[str],
) -> None:
    initial_diff_count = len(external_diffs)
    for code in sorted(source.candidate_codes):
        local = source.histories.get(code)
        if local is None or local.empty:
            continue
        start_ymd = local.iloc[0]["日期"].strftime("%Y%m%d")
        compare_end = pd.Timestamp(compare_end_date)
        end_ymd = compare_end.strftime("%Y%m%d")
        try:
            raw, provider = external_fetcher(code, start_ymd, end_ymd)
            provider_counts[str(provider)] += 1
            remote = normalize_history(raw, code)
        except Exception as exc:  # noqa: BLE001 - report per-code source failures
            external_diffs.append(
                {
                    "source_date": source.source_date,
                    "code": code,
                    "provider": "unavailable",
                    "problem": str(exc),
                }
            )
            continue

        remote["日期"] = pd.to_datetime(remote["日期"], errors="coerce")
        remote = remote[remote["日期"] <= compare_end].sort_values("日期")
        local_tail = local[local["日期"] <= compare_end].sort_values("日期").tail(20)
        _compare_external_history(source.source_date, code, local_tail, remote, provider, external_diffs)

    source_diff_count = len(external_diffs) - initial_diff_count
    if source_diff_count:
        errors.append(f"{source.source_date}: external source audit found {source_diff_count} differences/failures")


def _compare_external_history(
    source_date: str,
    code: str,
    local_tail: pd.DataFrame,
    remote: pd.DataFrame,
    provider: str,
    external_diffs: list[dict[str, Any]],
) -> None:
    remote_by_date = {item["日期"].date().isoformat(): item for item in remote.to_dict("records")}
    for item in local_tail.to_dict("records"):
        date_key = item["日期"].date().isoformat()
        remote_item = remote_by_date.get(date_key)
        if remote_item is None:
            external_diffs.append(
                {
                    "source_date": source_date,
                    "code": code,
                    "provider": provider,
                    "date": date_key,
                    "problem": "missing_remote_date",
                }
            )
            continue
        local_close = float(item["收盘价"])
        remote_close = float(remote_item["收盘价"])
        local_amount = float(item["成交额(亿)"])
        remote_amount = float(remote_item["成交额(亿)"])
        amount_tolerance = max(0.02, abs(local_amount) * 0.01)
        if abs(local_close - remote_close) > 0.0001 or abs(local_amount - remote_amount) > amount_tolerance:
            ratio = _amount_ratio(local_amount, remote_amount)
            external_diffs.append(
                {
                    "source_date": source_date,
                    "code": code,
                    "provider": provider,
                    "date": date_key,
                    "problem": "suspected_100x_amount_unit_mismatch"
                    if _is_unit_ratio(ratio)
                    else "external_history_mismatch",
                    "local_close": round(local_close, 6),
                    "remote_close": round(remote_close, 6),
                    "local_amount_yi": round(local_amount, 6),
                    "remote_amount_yi": round(remote_amount, 6),
                    "ratio": round(ratio, 4) if ratio is not None else None,
                    "unit_mismatch_suspected": _is_unit_ratio(ratio),
                }
            )


def _default_external_fetcher(code: str, start_ymd: str, end_ymd: str) -> tuple[pd.DataFrame, str]:
    ak = _load_akshare()
    try:
        return _fetch_history_sina(ak, code, start_ymd, end_ymd), "sina"
    except Exception:
        return _fetch_history_eastmoney_with_provider(code, start_ymd, end_ymd)


def _repair_outputs(
    start: str,
    end: str,
    root: Path,
    radar_dates: list[str],
    source_dates: list[str],
    fetcher: Callable[..., Any],
    scorer: Callable[..., Any],
) -> dict[str, Any]:
    backup_dir = _backup_for_repair(root, start, end, radar_dates, source_dates)
    result = fetcher(data_date=end, root=root, resume=False)
    fetch_errors = list(getattr(result, "errors", []) or [])
    if fetch_errors:
        raise RuntimeError(f"repair fetch completed with {len(fetch_errors)} errors")

    source_dir = root / "data" / "normalized" / end
    dates = [item for item in recent_trading_dates(source_dir, end, 10_000) if start <= item <= end]
    if not dates:
        raise RuntimeError(f"repair found no trading dates in {source_dir / 'history'}")

    score_rows: dict[str, int] = {}
    for data_date in dates:
        score_result = scorer(data_date=data_date, root=root, source_date=end)
        score_rows[data_date] = int(getattr(score_result, "rows", 0))

    return {
        "attempted": True,
        "backup_dir": str(backup_dir),
        "source_date": end,
        "fetch_errors": len(fetch_errors),
        "regenerated_dates": dates,
        "score_rows": score_rows,
    }


def _backup_for_repair(root: Path, start: str, end: str, radar_dates: list[str], source_dates: list[str]) -> Path:
    timestamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
    backup_dir = root / "data" / "state" / "audit_backups" / f"{timestamp}_{start}_{end}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    for raw_date in sorted(set(source_dates + [end])):
        _copy_if_exists(root / "data" / "raw" / raw_date, backup_dir / "raw" / raw_date)
    for normalized_date in sorted(set(radar_dates + source_dates + [end])):
        _copy_if_exists(root / "data" / "normalized" / normalized_date, backup_dir / "normalized" / normalized_date)
    for data_date in radar_dates:
        _copy_if_exists(root / "data" / "radar" / f"{data_date}.csv", backup_dir / "radar" / f"{data_date}.csv")
        _copy_if_exists(root / "data" / "radar" / f"{data_date}.json", backup_dir / "radar" / f"{data_date}.json")
    _copy_if_exists(root / "data" / "state" / "latest.json", backup_dir / "state" / "latest.json")
    return backup_dir


def _copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


__all__ = ["AuditResult", "audit_outputs"]
