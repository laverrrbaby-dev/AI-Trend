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
