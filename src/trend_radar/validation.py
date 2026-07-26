"""Validate radar outputs against UI and backend expectations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .config import PROJECT_ROOT, now_shanghai
from .filters import EXCLUDE_CODE_PREFIXES
from .scoring import DETAIL_HISTORY_COLUMNS, SCORE_COLUMNS, build_changes_from_frames
from .scoring import _history_source_label, _share_source_label  # noqa: PLC2701 - validation must mirror scoring labels
from .ui_data import build_price_history, get_available_dates, load_snapshot


OPTIONAL_DETAIL_COLUMNS = {"成交量"}
DETAIL_COLUMNS = set(DETAIL_HISTORY_COLUMNS) - OPTIONAL_DETAIL_COLUMNS


@dataclass(frozen=True)
class ValidationResult:
    start: str
    end: str
    checked_dates: list[str]
    ok: bool
    errors: list[str]
    warnings: list[str]
    report_path: Path


def validate_outputs(start: str, end: str, root: Path = PROJECT_ROOT) -> ValidationResult:
    radar_dates = _radar_dates_between(root, start, end)
    errors: list[str] = []
    warnings: list[str] = []

    if not radar_dates:
        errors.append(f"No radar outputs found between {start} and {end}")

    available_dates = set(get_available_dates(root=root))
    for data_date in radar_dates:
        csv_path = root / "data" / "radar" / f"{data_date}.csv"
        json_path = root / "data" / "radar" / f"{data_date}.json"
        if data_date not in available_dates:
            errors.append(f"{data_date}: missing from get_available_dates()")
        if not json_path.exists():
            errors.append(f"{data_date}: missing metadata JSON")
            continue

        frame = _read_radar_csv(csv_path, data_date, errors)
        if frame is None:
            continue
        metadata = _read_json(json_path, data_date, errors)
        if metadata is None:
            continue

        _validate_required_columns(frame, data_date, errors)
        _validate_basic_frame(frame, data_date, errors)
        _validate_metadata(frame, metadata, root, data_date, errors)
        _validate_quality_sources(frame, metadata, root, data_date, errors, warnings)
        _validate_ui_backend(root, frame, data_date, errors, warnings)

    report = {
        "start": start,
        "end": end,
        "checked_dates": radar_dates,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "validated_at": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
    }
    report_path = root / "data" / "state" / f"validation_{start}_{end}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return ValidationResult(start, end, radar_dates, not errors, errors, warnings, report_path)


def _radar_dates_between(root: Path, start: str, end: str) -> list[str]:
    radar_dir = root / "data" / "radar"
    if not radar_dir.exists():
        return []
    return sorted(path.stem for path in radar_dir.glob("*.csv") if start <= path.stem <= end)


def _read_radar_csv(path: Path, data_date: str, errors: list[str]) -> pd.DataFrame | None:
    if not path.exists():
        errors.append(f"{data_date}: missing radar CSV")
        return None
    try:
        frame = pd.read_csv(path, dtype={"代码": str})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{data_date}: failed to read CSV: {exc}")
        return None
    frame["代码"] = frame.get("代码", pd.Series(dtype=str)).astype(str).str.zfill(6)
    return frame


def _read_json(path: Path, data_date: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{data_date}: failed to read JSON: {exc}")
        return None


def _validate_required_columns(frame: pd.DataFrame, data_date: str, errors: list[str]) -> None:
    missing = [column for column in SCORE_COLUMNS if column not in frame.columns]
    if missing:
        errors.append(f"{data_date}: missing required columns {missing}")


def _validate_basic_frame(frame: pd.DataFrame, data_date: str, errors: list[str]) -> None:
    if frame.empty:
        errors.append(f"{data_date}: radar CSV is empty")
        return

    expected_ranks = list(range(1, len(frame) + 1))
    ranks = pd.to_numeric(frame["排名"], errors="coerce").astype("Int64").tolist() if "排名" in frame else []
    if ranks != expected_ranks:
        errors.append(f"{data_date}: ranks are not consecutive from 1")

    if "代码" in frame and frame["代码"].duplicated().any():
        errors.append(f"{data_date}: duplicate ETF codes found")

    if "主线得分" in frame:
        scores = pd.to_numeric(frame["主线得分"], errors="coerce")
        if scores.isna().any() or not scores.between(0, 100).all():
            errors.append(f"{data_date}: scores must be numeric and within 0-100")

    if "数据日期" in frame and not (frame["数据日期"].astype(str) == data_date).all():
        errors.append(f"{data_date}: 数据日期 column does not match file date")

    bad_prefix = frame["代码"].astype(str).str.startswith(EXCLUDE_CODE_PREFIXES) if "代码" in frame else pd.Series(dtype=bool)
    if bad_prefix.any():
        codes = frame.loc[bad_prefix, "代码"].head(10).tolist()
        errors.append(f"{data_date}: non A-share equity ETF prefixes found {codes}")


def _validate_metadata(frame: pd.DataFrame, metadata: dict[str, Any], root: Path, data_date: str, errors: list[str]) -> None:
    top10 = metadata.get("top10")
    if not isinstance(top10, list):
        errors.append(f"{data_date}: JSON top10 is missing or invalid")
    else:
        expected = frame.head(10)[["排名", "ETF名称", "代码", "主线得分"]].to_dict(orient="records")
        normalized_expected = [_normalize_top10_item(item) for item in expected]
        normalized_actual = [_normalize_top10_item(item) for item in top10]
        if normalized_actual != normalized_expected:
            errors.append(f"{data_date}: JSON top10 does not match CSV head(10)")

    previous = _previous_ranking(root, data_date)
    expected_changes = build_changes_from_frames(frame, previous)
    if metadata.get("changes") != expected_changes:
        errors.append(f"{data_date}: JSON changes do not match previous radar output")


def _validate_quality_sources(
    frame: pd.DataFrame,
    metadata: dict[str, Any],
    root: Path,
    data_date: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    required = {"数据置信度", "行情来源", "份额来源", "数据问题", "数据质量"}
    if not required.issubset(frame.columns):
        return

    confidence = frame["数据置信度"].astype(str)
    bad_confidence = sorted(set(confidence) - {"高", "中", "低"})
    if bad_confidence:
        errors.append(f"{data_date}: invalid 数据置信度 values {bad_confidence}")

    source_date = str(metadata.get("source_date") or data_date)
    history_sources = _history_sources_from_report(root, source_date)
    row_history_sources = _history_sources_from_history_files(root, source_date, data_date)
    combined_history_sources = {**history_sources, **row_history_sources}
    if combined_history_sources:
        for _, row in frame.iterrows():
            code = str(row["代码"]).zfill(6)
            provider = combined_history_sources.get(code)
            if provider is None:
                continue
            expected_source = _history_source_label(provider)
            if str(row["行情来源"]) != expected_source:
                errors.append(f"{data_date}: {code} 行情来源 {row['行情来源']} does not match fetch_report {expected_source}")
            if provider == "spot_fallback":
                if str(row["数据置信度"]) != "低":
                    errors.append(f"{data_date}: {code} spot_fallback history must be low confidence")
                if str(row["数据质量"]) == "完整":
                    errors.append(f"{data_date}: {code} spot_fallback history must not be marked 完整")
                if "历史行情补点" not in str(row["数据问题"]):
                    errors.append(f"{data_date}: {code} spot_fallback history missing explicit data issue")
            if provider == "sina":
                if str(row["数据置信度"]) == "高":
                    errors.append(f"{data_date}: {code} Sina fallback history must not be high confidence")
                if str(row["数据质量"]) == "完整":
                    errors.append(f"{data_date}: {code} Sina fallback history must not be marked 完整")
                if "新浪" not in str(row["数据问题"]):
                    errors.append(f"{data_date}: {code} Sina fallback history missing explicit data issue")
    else:
        warnings.append(f"{data_date}: fetch_report history_outcomes missing; cannot verify 行情来源")

    share_sources = _latest_share_sources(root, source_date, data_date)
    for _, row in frame.iterrows():
        code = str(row["代码"]).zfill(6)
        share_source = share_sources.get(code)
        if not share_source:
            continue
        expected_share_source = _share_source_label(share_source)
        if str(row["份额来源"]) != expected_share_source:
            errors.append(f"{data_date}: {code} 份额来源 {row['份额来源']} does not match shares.csv {expected_share_source}")
        if share_source == "spot_fallback" and str(row["数据质量"]) == "完整":
            errors.append(f"{data_date}: {code} spot_fallback shares must not be marked 完整")


def _history_sources_from_report(root: Path, source_date: str) -> dict[str, str]:
    report = _read_json(root / "data" / "normalized" / source_date / "fetch_report.json", source_date, [])
    outcomes = report.get("history_outcomes") if report else None
    if not isinstance(outcomes, list):
        return {}
    sources: dict[str, str] = {}
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        raw_code = str(item.get("code") or "").strip()
        code = raw_code.zfill(6)
        if len(code) == 6 and code.isdigit():
            sources[code] = str(item.get("provider") or "unknown").strip() or "unknown"
    return sources


def _history_sources_from_history_files(root: Path, source_date: str, data_date: str) -> dict[str, str]:
    history_dir = root / "data" / "normalized" / source_date / "history"
    if not history_dir.exists():
        return {}
    sources: dict[str, str] = {}
    target = pd.Timestamp(data_date)
    for path in sorted(history_dir.glob("*.csv")):
        code = path.stem.zfill(6)
        try:
            frame = pd.read_csv(path, usecols=lambda column: column in {"日期", "history_source"})
        except (OSError, ValueError, pd.errors.EmptyDataError):
            continue
        if frame.empty or "history_source" not in frame:
            continue
        frame["日期"] = pd.to_datetime(frame.get("日期"), errors="coerce")
        frame = frame[frame["日期"] <= target].dropna(subset=["日期"])
        if frame.empty:
            continue
        values = frame["history_source"].fillna("").astype(str).str.strip()
        if values.eq("spot_fallback").any():
            sources[code] = "spot_fallback"
            continue
        non_empty = values[values != ""]
        if not non_empty.empty:
            sources[code] = str(non_empty.iloc[-1])
    return sources


def _latest_share_sources(root: Path, source_date: str, data_date: str) -> dict[str, str]:
    path = root / "data" / "normalized" / source_date / "shares.csv"
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path, dtype={"code": str})
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return {}
    required = {"code", "日期", "share_source"}
    if frame.empty or not required.issubset(frame.columns):
        return {}
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
    frame = frame[frame["日期"] <= pd.Timestamp(data_date)].dropna(subset=["日期"])
    if frame.empty:
        return {}
    latest = frame.sort_values("日期").groupby("code", as_index=False).tail(1)
    return dict(zip(latest["code"], latest["share_source"].fillna("unknown").astype(str), strict=True))


def _validate_ui_backend(root: Path, frame: pd.DataFrame, data_date: str, errors: list[str], warnings: list[str]) -> None:
    try:
        snapshot = load_snapshot(data_date, root=root)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{data_date}: load_snapshot failed: {exc}")
        return
    if len(snapshot) != len(frame):
        errors.append(f"{data_date}: load_snapshot row count mismatch")

    for _, row in frame.head(10).iterrows():
        code = str(row["代码"]).zfill(6)
        detail_path = root / "data" / "normalized" / data_date / "radar_history" / f"{code}.csv"
        if not detail_path.exists():
            errors.append(f"{data_date}: missing Top10 detail history for {code}")
            continue
        detail = pd.read_csv(detail_path)
        missing_detail = sorted(DETAIL_COLUMNS - set(detail.columns))
        if missing_detail:
            errors.append(f"{data_date}: detail history for {code} missing {missing_detail}")
            continue
        loaded_detail = build_price_history(row, data_date, root=root)
        if loaded_detail.empty:
            errors.append(f"{data_date}: build_price_history returned empty detail for {code}")
        if len(loaded_detail) != len(detail):
            warnings.append(f"{data_date}: build_price_history row count differs for {code}")


def _previous_ranking(root: Path, data_date: str) -> pd.DataFrame | None:
    paths = sorted(path for path in (root / "data" / "radar").glob("*.csv") if path.stem < data_date)
    if not paths:
        return None
    frame = pd.read_csv(paths[-1], dtype={"代码": str})
    frame["代码"] = frame["代码"].astype(str).str.zfill(6)
    return frame.sort_values("排名").reset_index(drop=True)


def _normalize_top10_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "排名": int(item["排名"]),
        "ETF名称": str(item["ETF名称"]),
        "代码": str(item["代码"]).zfill(6),
        "主线得分": round(float(item["主线得分"]), 4),
    }


__all__ = ["ValidationResult", "validate_outputs"]
