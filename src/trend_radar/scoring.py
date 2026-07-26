"""Build the radar ranking from normalized ETF data."""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from .config import (
    CROSS_VALIDATE_CONCURRENCY,
    MIN_AVG_20D_AMOUNT_YI,
    MIN_LATEST_AMOUNT_YI,
    MIN_LISTING_DAYS,
    MIN_MARKET_CAP_YI,
    PROJECT_ROOT,
    now_shanghai,
)
from .filters import classify_etf


SCORE_COLUMNS = [
    "排名",
    "ETF名称",
    "代码",
    "主题",
    "主线得分",
    "趋势状态",
    "成交额Top30次数",
    "成交额排名",
    "成交额(亿)",
    "20日均额(亿)",
    "20日涨幅",
    "60日涨幅",
    "120日涨幅",
    "5日份额变化",
    "20日份额变化",
    "均线结构",
    "排名变化",
    "数据质量",
    "数据置信度",
    "行情来源",
    "份额来源",
    "数据问题",
    "观察理由",
    "热度分",
    "资金分",
    "趋势分",
    "结构分",
    "数据日期",
    "source_flags",
    "generated_at",
]
EMA_WINDOWS = (13, 26, 144, 169)
QUALITY_COLUMNS = ["数据质量", "数据置信度", "行情来源", "份额来源", "数据问题"]
LOW_CONFIDENCE_FLAGS = {
    "LOW_CONF_HISTORY_SPOT_FALLBACK",
    "LOW_CONF_HISTORY_UNKNOWN",
    "LOW_CONF_HISTORY_CROSS_MISMATCH",
    "LOW_CONF_SHARE",
}
MEDIUM_CONFIDENCE_FLAGS = {
    "MEDIUM_CONF_HISTORY_SINA",
    "MEDIUM_CONF_HISTORY_CACHE_RAW",
    "MEDIUM_CONF_HISTORY_PRIMARY_FAILED",
    "LOW_CONF_SHARE_SPOT_FALLBACK",
    "LOW_CONF_OHLC",
    "WARN_STALE_HIST",
    "SHARE_LT_20",
    "MARKET_CAP_SPOT_FALLBACK",
    "MISSING_MARKET_CAP",
}
TRUSTED_HISTORY_PROVIDERS = {
    "eastmoney_urllib",
    "eastmoney_curl",
    "eastmoney_curl_proxy",
    "fixture",
}
CROSS_VALIDATE_TOP_N = 10
CROSS_VALIDATE_FALLBACK_RANK_LIMIT = 30
CROSS_VALIDATE_RANK_CHANGE_THRESHOLD = 10
CROSS_VALIDATE_MAX_TARGETS = 12
CROSS_VALIDATE_CLOSE_TOLERANCE_PCT = 0.2
CROSS_VALIDATE_AMOUNT_TOLERANCE_PCT = 5.0
OHLC_COLUMNS = ["开盘价", "最高价", "最低价", "收盘价"]
DETAIL_HISTORY_COLUMNS = [
    "日期",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "成交额(亿)",
    "成交量",
    "ETF份额(亿份)",
    "EMA13",
    "EMA26",
    "EMA144",
    "EMA169",
]


@dataclass(frozen=True)
class ScoreResult:
    data_date: str
    rows: int
    radar_csv: Path
    radar_json: Path
    latest_json: Path
    quality_status: str


def generate_scores(
    data_date: str,
    root: Path = PROJECT_ROOT,
    source_date: str | None = None,
    *,
    cross_validate_top_pool: bool = True,
) -> ScoreResult:
    source_data_date = source_date or data_date
    source_normalized_dir = root / "data" / "normalized" / source_data_date
    output_normalized_dir = root / "data" / "normalized" / data_date
    history_dir = source_normalized_dir / "history"
    shares_path = source_normalized_dir / "shares.csv"
    spot_path = source_normalized_dir / "spot_candidates.csv"

    if not spot_path.exists():
        raise FileNotFoundError(f"Missing normalized spot candidates: {spot_path}")
    if not history_dir.exists():
        raise FileNotFoundError(f"Missing normalized history directory: {history_dir}")

    spot = pd.read_csv(spot_path, dtype={"code": str})
    spot["code"] = spot["code"].astype(str).str.zfill(6)
    spot = spot[
        spot.apply(lambda row: classify_etf(str(row.get("name", "")), str(row.get("code", "")))[0], axis=1)
    ].reset_index(drop=True)
    candidate_codes = set(spot["code"].astype(str).str.zfill(6))
    shares = _read_shares(shares_path)
    histories = _read_histories(history_dir, data_date, candidate_codes)
    history_sources = _history_source_map(source_normalized_dir)
    cross_validation = _cross_validation_map(source_normalized_dir, data_date)
    latest_amount_ranks = _latest_amount_ranks(histories, data_date)
    top30_counts = _top30_residency(histories, data_date)
    previous_rank = _previous_rank_map(root, data_date)

    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    generated_at = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")

    for _, spot_row in spot.iterrows():
        code = str(spot_row["code"]).zfill(6)
        source_flags: list[str] = []
        history = histories.get(code)
        if history is None or history.empty:
            _count(rejected, "MISSING_HISTORY")
            continue
        history_source = _history_source_for_code(history_sources, history, code)
        cross_status = cross_validation.get(code, {})

        history = history[history["日期"] <= pd.Timestamp(data_date)].copy()
        if len(history) < MIN_LISTING_DAYS:
            _count(rejected, "HISTORY_LT_120")
            continue

        latest = history.iloc[-1]
        amount_20d = history.tail(20)["成交额(亿)"]
        latest_amount = float(latest["成交额(亿)"])
        avg_amount_20d = float(amount_20d.mean())
        if latest_amount < MIN_LATEST_AMOUNT_YI:
            _count(rejected, "LOW_LATEST_AMOUNT")
            continue
        if avg_amount_20d < MIN_AVG_20D_AMOUNT_YI:
            _count(rejected, "LOW_AVG_AMOUNT")
            continue

        share_history = _share_history_for_code(shares, code, data_date)
        current_share = _latest_share(share_history)
        market_cap_yi, market_cap_source = _market_cap_yi(spot_row, latest, current_share)
        if market_cap_source == "spot":
            source_flags.append("MARKET_CAP_SPOT_FALLBACK")
        if pd.notna(market_cap_yi) and market_cap_yi < MIN_MARKET_CAP_YI:
            _count(rejected, "LOW_MARKET_CAP")
            continue
        if pd.isna(market_cap_yi):
            source_flags.append("MISSING_MARKET_CAP")

        if latest["日期"].date() != date.fromisoformat(data_date):
            source_flags.append("WARN_STALE_HIST")
        if "_ohlc_fallback" in history and bool(history["_ohlc_fallback"].any()):
            source_flags.append("LOW_CONF_OHLC")
        if _history_uses_spot_fallback(history_source, history):
            source_flags.append("LOW_CONF_HISTORY_SPOT_FALLBACK")
        elif history_source == "sina":
            source_flags.append("MEDIUM_CONF_HISTORY_SINA")
        elif history_source == "cache_raw":
            source_flags.append("MEDIUM_CONF_HISTORY_CACHE_RAW")
        elif not _history_source_is_trusted(history_source):
            source_flags.append("LOW_CONF_HISTORY_UNKNOWN")
        if cross_status.get("status") == "primary_mismatch":
            source_flags.append("LOW_CONF_HISTORY_CROSS_MISMATCH")
        elif cross_status.get("status") == "primary_failed":
            source_flags.append("MEDIUM_CONF_HISTORY_PRIMARY_FAILED")

        if share_history.empty:
            source_flags.append("LOW_CONF_SHARE")
            share_5d = 0.0
            share_20d = 0.0
            share_source = "missing"
        else:
            share_source = _latest_share_source(share_history)
            share_5d = _pct_change_from_tail(share_history["ETF份额(亿份)"], 5)
            share_20d = _pct_change_from_tail(share_history["ETF份额(亿份)"], 20)
            if share_source == "spot_fallback":
                source_flags.append("LOW_CONF_SHARE_SPOT_FALLBACK")
            if len(share_history) < 20:
                source_flags.append("SHARE_LT_20")

        ret_20d = _return_pct(history["收盘价"], 20)
        ret_60d = _return_pct(history["收盘价"], 60)
        ret_120d = _return_pct(history["收盘价"], 120)
        ema_history = _with_emas(history, share_history)
        latest_ema = ema_history.iloc[-1]
        structure_score, ma_structure = _structure_score(latest_ema)
        amount_rank = latest_amount_ranks.get(code, len(latest_amount_ranks) + 1)
        top30_days = top30_counts.get(code, 0)
        heat_score = _heat_score(top30_days, amount_rank, history)
        flow_score = _flow_score(share_5d, share_20d, ret_20d, "LOW_CONF_SHARE" in source_flags)
        trend_score = _trend_score(ret_20d, ret_60d, ret_120d)
        total_score = round(heat_score * 0.45 + flow_score * 0.20 + trend_score * 0.25 + structure_score * 0.10, 1)
        confidence = _confidence_label(source_flags)
        issues = _data_issues(source_flags, history_source, share_source, cross_status)

        rows.append(
            {
                "ETF名称": str(spot_row["name"]),
                "代码": code,
                "主题": _infer_theme(str(spot_row["name"])),
                "主线得分": total_score,
                "趋势状态": _trend_status(total_score),
                "成交额Top30次数": int(top30_days),
                "成交额排名": int(amount_rank),
                "成交额(亿)": round(latest_amount, 3),
                "20日均额(亿)": round(avg_amount_20d, 3),
                "20日涨幅": round(ret_20d, 2),
                "60日涨幅": round(ret_60d, 2),
                "120日涨幅": round(ret_120d, 2),
                "5日份额变化": round(share_5d, 2),
                "20日份额变化": round(share_20d, 2),
                "均线结构": ma_structure,
                "排名变化": None,
                "数据质量": _quality_label(source_flags),
                "数据置信度": confidence,
                "行情来源": _history_source_label(history_source),
                "份额来源": _share_source_label(share_source),
                "数据问题": "；".join(issues) if issues else "无",
                "观察理由": _reason(total_score, top30_days, share_20d, ma_structure),
                "热度分": round(heat_score, 1),
                "资金分": round(flow_score, 1),
                "趋势分": round(trend_score, 1),
                "结构分": round(structure_score, 1),
                "数据日期": data_date,
                "source_flags": ",".join(source_flags) if source_flags else "OK",
                "generated_at": generated_at,
                "_history": ema_history,
            }
        )

    ranking = pd.DataFrame(rows)
    if ranking.empty:
        raise RuntimeError("No ETFs passed the normalized data and liquidity filters")

    ranking = ranking.sort_values(["主线得分", "成交额(亿)"], ascending=False).reset_index(drop=True)
    ranking["排名"] = range(1, len(ranking) + 1)
    ranking["排名变化"] = ranking.apply(
        lambda row: _rank_change(previous_rank, str(row["代码"]), int(row["排名"])), axis=1
    )

    if cross_validate_top_pool:
        cross_report = _cross_validate_top_pool(
            ranking=ranking,
            histories=histories,
            history_dir=history_dir,
            data_date=data_date,
            source_normalized_dir=source_normalized_dir,
        )
        if cross_report:
            _write_cross_validation_report(source_normalized_dir, data_date, cross_report)
            return generate_scores(
                data_date=data_date,
                root=root,
                source_date=source_data_date,
                cross_validate_top_pool=False,
            )

    output_history_dir = output_normalized_dir / "radar_history"
    if output_history_dir.exists():
        shutil.rmtree(output_history_dir)
    output_history_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        history = row["_history"].copy()
        history = history[DETAIL_HISTORY_COLUMNS]
        history.to_csv(output_history_dir / f"{row['代码']}.csv", index=False)

    radar_dir = root / "data" / "radar"
    state_dir = root / "data" / "state"
    radar_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    output = ranking[SCORE_COLUMNS].copy()
    radar_csv = radar_dir / f"{data_date}.csv"
    radar_json = radar_dir / f"{data_date}.json"
    latest_json = state_dir / "latest.json"
    output.to_csv(radar_csv, index=False)

    changes = build_changes_from_frames(output, _previous_ranking(root, data_date))
    quality = _quality_summary(output, rejected)
    payload = {
        "data_date": data_date,
        "generated_at": generated_at,
        "source_date": source_data_date,
        "source_mode": "real",
        "rows": int(len(output)),
        "top10": output.head(10)[["排名", "ETF名称", "代码", "主线得分"]].to_dict(orient="records"),
        "changes": changes,
        "data_quality": quality,
        "rejected": rejected,
    }
    radar_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if _should_update_latest(latest_json, data_date):
        latest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return ScoreResult(
        data_date=data_date,
        rows=len(output),
        radar_csv=radar_csv,
        radar_json=radar_json,
        latest_json=latest_json,
        quality_status=str(quality["status"]),
    )


def build_changes_from_frames(current: pd.DataFrame, previous: pd.DataFrame | None) -> dict[str, list[str]]:
    current_top = current.head(10)
    if previous is None or previous.empty:
        return {
            "新进Top10": current_top["ETF名称"].head(3).tolist(),
            "连续Top10": [],
            "跌出Top10": [],
        }

    previous_top = previous.head(10)
    current_codes = current_top["代码"].astype(str).str.zfill(6).tolist()
    previous_codes = previous_top["代码"].astype(str).str.zfill(6).tolist()
    current_name = dict(zip(current_codes, current_top["ETF名称"], strict=True))
    previous_name = dict(zip(previous_codes, previous_top["ETF名称"], strict=True))
    return {
        "新进Top10": [current_name[code] for code in current_codes if code not in previous_codes],
        "连续Top10": [current_name[code] for code in current_codes if code in previous_codes],
        "跌出Top10": [previous_name[code] for code in previous_codes if code not in current_codes],
    }


def _read_shares(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["code", "日期", "ETF份额(亿份)", "share_source"])
    shares = pd.read_csv(path, dtype={"code": str})
    if shares.empty:
        return shares
    shares["code"] = shares["code"].astype(str).str.zfill(6)
    shares["日期"] = pd.to_datetime(shares["日期"], errors="coerce")
    shares["ETF份额(亿份)"] = pd.to_numeric(shares["ETF份额(亿份)"], errors="coerce")
    if "share_source" not in shares:
        shares["share_source"] = "unknown"
    shares["share_source"] = shares["share_source"].fillna("unknown").astype(str)
    return shares.dropna(subset=["日期", "ETF份额(亿份)"])


def _read_histories(history_dir: Path, data_date: str, candidate_codes: set[str] | None = None) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    for path in sorted(history_dir.glob("*.csv")):
        code = path.stem.zfill(6)
        if candidate_codes is not None and code not in candidate_codes:
            continue
        try:
            frame = pd.read_csv(path, dtype={"code": str})
        except EmptyDataError:
            continue
        if frame.empty:
            continue
        original_columns = set(frame.columns)
        frame["code"] = code
        frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
        frame["收盘价"] = pd.to_numeric(frame["收盘价"], errors="coerce")
        for column in ["开盘价", "最高价", "最低价"]:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            else:
                frame[column] = frame["收盘价"]
        frame["成交额(亿)"] = pd.to_numeric(frame["成交额(亿)"], errors="coerce")
        frame["成交量"] = pd.to_numeric(frame["成交量"], errors="coerce") if "成交量" in frame else pd.NA
        frame["成交量"] = _volume_with_amount_fallback(frame)
        if "history_source" not in frame:
            frame["history_source"] = ""
        frame["history_source"] = frame["history_source"].fillna("").astype(str)
        frame["开盘价"] = frame["开盘价"].fillna(frame["收盘价"])
        frame["最高价"] = frame["最高价"].fillna(frame[["开盘价", "收盘价"]].max(axis=1))
        frame["最低价"] = frame["最低价"].fillna(frame[["开盘价", "收盘价"]].min(axis=1))
        frame["最高价"] = frame[["最高价", "开盘价", "收盘价"]].max(axis=1)
        frame["最低价"] = frame[["最低价", "开盘价", "收盘价"]].min(axis=1)
        frame["_ohlc_fallback"] = not {"开盘价", "最高价", "最低价"}.issubset(original_columns)
        frame = frame.dropna(subset=["日期", "收盘价", "成交额(亿)"])
        histories[code] = frame[frame["日期"] <= pd.Timestamp(data_date)].sort_values("日期").reset_index(drop=True)
    return histories


def _latest_amount_ranks(histories: dict[str, pd.DataFrame], data_date: str) -> dict[str, int]:
    rows = []
    for code, history in histories.items():
        current = history[history["日期"] <= pd.Timestamp(data_date)]
        if not current.empty:
            latest = current.iloc[-1]
            rows.append({"code": code, "amount": float(latest["成交额(亿)"])})
    if not rows:
        return {}
    ranked = pd.DataFrame(rows).sort_values("amount", ascending=False).reset_index(drop=True)
    return {str(row["code"]).zfill(6): index for index, row in enumerate(ranked.to_dict("records"), start=1)}


def _top30_residency(histories: dict[str, pd.DataFrame], data_date: str) -> dict[str, int]:
    records = []
    for code, history in histories.items():
        latest = history[history["日期"] <= pd.Timestamp(data_date)].tail(20)
        records.extend(
            {"code": code, "date": item["日期"], "amount": item["成交额(亿)"]} for item in latest.to_dict("records")
        )
    if not records:
        return {}
    frame = pd.DataFrame(records)
    frame["rank"] = frame.groupby("date")["amount"].rank(method="first", ascending=False)
    top = frame[frame["rank"] <= 30]
    return top.groupby("code").size().astype(int).to_dict()


def _share_history_for_code(shares: pd.DataFrame, code: str, data_date: str) -> pd.DataFrame:
    if shares.empty:
        return shares
    frame = shares[(shares["code"] == code) & (shares["日期"] <= pd.Timestamp(data_date))].copy()
    return frame.sort_values("日期").tail(60).reset_index(drop=True)


def _latest_share(share_history: pd.DataFrame) -> float:
    if share_history.empty:
        return float("nan")
    return float(share_history.iloc[-1]["ETF份额(亿份)"])


def _latest_share_source(share_history: pd.DataFrame) -> str:
    if share_history.empty or "share_source" not in share_history:
        return "unknown"
    values = share_history["share_source"].fillna("unknown").astype(str).str.strip()
    return str(values.iloc[-1] or "unknown")


def _market_cap_yi(spot_row: pd.Series, latest: pd.Series, share_yi: float) -> tuple[float, str]:
    if pd.notna(share_yi):
        return float(latest["收盘价"]) * share_yi, "share"
    for column in ("total_market_cap_yi", "float_market_cap_yi"):
        value = pd.to_numeric(pd.Series([spot_row.get(column)]), errors="coerce").iloc[0]
        if pd.notna(value):
            return float(value), "spot"
    return float("nan"), "missing"


def _return_pct(series: pd.Series, periods: int) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) <= periods:
        return 0.0
    base = float(values.iloc[-periods - 1])
    if base == 0:
        return 0.0
    return (float(values.iloc[-1]) / base - 1.0) * 100


def _pct_change_from_tail(series: pd.Series, periods: int) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) <= periods:
        return 0.0
    base = float(values.iloc[-periods - 1])
    if base == 0:
        return 0.0
    return (float(values.iloc[-1]) / base - 1.0) * 100


def _with_emas(history: pd.DataFrame, shares: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy().sort_values("日期")
    for window in EMA_WINDOWS:
        frame[f"EMA{window}"] = frame["收盘价"].ewm(span=window, adjust=False, min_periods=1).mean()
    if shares.empty:
        frame["ETF份额(亿份)"] = pd.NA
    else:
        share_frame = shares[["日期", "ETF份额(亿份)"]].sort_values("日期")
        frame = pd.merge_asof(frame, share_frame, on="日期", direction="backward")
    return frame


def _volume_with_amount_fallback(frame: pd.DataFrame) -> pd.Series:
    volume = pd.to_numeric(frame.get("成交量", pd.Series(pd.NA, index=frame.index)), errors="coerce")
    if not {"成交额(亿)", "收盘价"}.issubset(frame.columns):
        return volume

    amount = pd.to_numeric(frame["成交额(亿)"], errors="coerce")
    close = pd.to_numeric(frame["收盘价"], errors="coerce")
    derived = (amount * 100_000_000 / close / 100).where(close > 0)
    return volume.fillna(derived)


def _structure_score(latest: pd.Series) -> tuple[float, str]:
    close = float(latest["收盘价"])
    ema13 = float(latest["EMA13"])
    ema26 = float(latest["EMA26"])
    ema144 = float(latest["EMA144"])
    ema169 = float(latest["EMA169"])
    if ema13 > ema26 > ema144 > ema169:
        return 100.0, "EMA13 > EMA26 > EMA144 > EMA169"
    if close > ema13 > ema26:
        return 75.0, "收盘价 > EMA13 > EMA26"
    if close > ema13:
        return 60.0, "收盘价 > EMA13"
    if close < ema26:
        return 35.0, "主要EMA下方"
    return 50.0, "EMA13 与 EMA26 缠绕"


def _heat_score(top30_days: int, amount_rank: int, history: pd.DataFrame) -> float:
    top30_score = min(100.0, max(0.0, top30_days / 20 * 100))
    if amount_rank <= 30:
        rank_score = (31 - amount_rank) / 30 * 100
    else:
        rank_score = max(0.0, (60 - amount_rank) / 30 * 45)
    amount_5d = float(history.tail(5)["成交额(亿)"].mean())
    amount_20d = float(history.tail(20)["成交额(亿)"].mean())
    ratio = amount_5d / amount_20d if amount_20d else 1.0
    expansion_score = min(100.0, max(0.0, (ratio - 0.5) * 100))
    return min(100.0, top30_score * 0.55 + rank_score * 0.25 + expansion_score * 0.20)


def _flow_score(share_5d: float, share_20d: float, ret_20d: float, low_confidence: bool) -> float:
    if low_confidence:
        return 45.0
    score = 50 + share_5d * 5 + share_20d * 2
    if ret_20d > 0 and share_20d > 0:
        score += 10
    if ret_20d > 0 and share_20d < 0:
        score -= 12
    return min(100.0, max(0.0, score))


def _trend_score(ret_20d: float, ret_60d: float, ret_120d: float) -> float:
    weighted = ret_20d * 0.45 + ret_60d * 0.35 + ret_120d * 0.20
    return min(100.0, max(0.0, 50 + weighted * 2.0))


def _trend_status(score: float) -> str:
    if score >= 88:
        return "强趋势"
    if score >= 74:
        return "趋势向上"
    if score >= 64:
        return "转强观察"
    if score >= 52:
        return "震荡观察"
    return "弱观察"


def _infer_theme(name: str) -> str:
    for keyword, theme in (
        ("芯片", "科技成长"),
        ("半导体", "科技成长"),
        ("人工智能", "科技成长"),
        ("机器人", "高端制造"),
        ("军工", "高端制造"),
        ("新能源", "新能源"),
        ("光伏", "新能源"),
        ("医药", "医药消费"),
        ("消费", "医药消费"),
        ("证券", "金融地产"),
        ("银行", "金融地产"),
        ("地产", "金融地产"),
        ("创业", "宽基成长"),
        ("科创", "宽基成长"),
        ("沪深300", "宽基核心"),
        ("中证500", "宽基核心"),
        ("上证50", "宽基核心"),
    ):
        if keyword in name:
            return theme
    return "A股权益"


def _reason(score: float, top30_days: int, share_20d: float, ma_structure: str) -> str:
    if score >= 74 and share_20d > 0:
        return "成交额热度、份额变化和多周期趋势共振，主线特征清晰"
    if top30_days >= 12:
        return "成交额驻留较稳定，趋势结构保持向上，适合继续观察"
    if "收盘价" in ma_structure:
        return "短期热度改善，趋势结构开始修复，需要继续确认持续性"
    return "资金关注度和趋势结构仍需继续观察"


def _quality_label(flags: list[str]) -> str:
    if not flags:
        return "完整"
    if "LOW_CONF_HISTORY_CROSS_MISMATCH" in flags:
        return "主源冲突"
    if "LOW_CONF_HISTORY_SPOT_FALLBACK" in flags or "LOW_CONF_HISTORY_UNKNOWN" in flags:
        return "需复核"
    if "MEDIUM_CONF_HISTORY_SINA" in flags:
        return "兜底需复核"
    if "MEDIUM_CONF_HISTORY_CACHE_RAW" in flags:
        return "缓存需复核"
    if "LOW_CONF_SHARE_SPOT_FALLBACK" in flags:
        return "份额需复核"
    if "LOW_CONF_SHARE" in flags:
        return "份额低置信度"
    if "LOW_CONF_OHLC" in flags:
        return "OHLC低置信度"
    if "WARN_STALE_HIST" in flags:
        return "日期滞后"
    return "需复核"


def _confidence_label(flags: list[str]) -> str:
    flag_set = set(flags)
    if flag_set & LOW_CONFIDENCE_FLAGS:
        return "低"
    if flag_set & MEDIUM_CONFIDENCE_FLAGS:
        return "中"
    return "高"


def _history_source_map(normalized_dir: Path) -> dict[str, str]:
    report = _read_fetch_report(normalized_dir / "fetch_report.json")
    outcomes = report.get("history_outcomes")
    if not isinstance(outcomes, list):
        return {}
    sources: dict[str, str] = {}
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        raw_code = str(item.get("code") or "").strip()
        code = raw_code.zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        provider = str(item.get("provider") or "unknown").strip() or "unknown"
        sources[code] = provider
    return sources


def _cross_validation_map(normalized_dir: Path, data_date: str) -> dict[str, dict[str, Any]]:
    report = _read_fetch_report(normalized_dir / "fetch_report.json")
    payload = report.get("top_pool_cross_validation")
    if not isinstance(payload, dict):
        return {}
    records = payload.get(data_date)
    if not isinstance(records, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        raw_code = str(item.get("code") or "").strip()
        code = raw_code.zfill(6)
        if len(code) == 6 and code.isdigit():
            result[code] = item
    return result


def _cross_validate_one_target(
    code: str,
    histories: dict[str, pd.DataFrame],
    history_dir: Path,
    data_date: str,
    source_label_by_code: dict[str, str],
) -> dict[str, Any] | None:
    current = histories.get(code)
    if current is None or current.empty:
        return None
    target = pd.Timestamp(data_date)
    current = current[current["日期"] <= target].copy()
    if current.empty:
        return None
    start_date = current["日期"].min().strftime("%Y%m%d")
    end_date = target.strftime("%Y%m%d")
    current_source = source_label_by_code.get(code) or _history_source_label(_history_source_for_code({}, current, code))
    item: dict[str, Any] = {
        "code": code,
        "current_provider": current_source,
        "status": "not_checked",
    }
    try:
        from .fetcher import _fetch_history_eastmoney_with_provider
        from .normalization import normalize_history

        raw, provider = _fetch_history_eastmoney_with_provider(code, start_date, end_date)
        primary = normalize_history(raw, code)
        primary["history_source"] = provider
        primary = primary[primary["日期"] <= target].copy()
        if primary.empty:
            raise RuntimeError("eastmoney returned no usable rows")
        mismatch = _history_mismatch(current, primary)
        item.update({"primary_provider": provider, **mismatch})
        if mismatch["status"] == "primary_match":
            _write_history_csv(primary, history_dir / f"{code}.csv")
            histories[code] = primary
            item["status"] = "primary_replaced"
        else:
            item["status"] = "primary_mismatch"
    except Exception as exc:  # noqa: BLE001 - report user-visible source failures
        item.update(
            {
                "status": "primary_failed",
                "primary_provider": "eastmoney",
                "primary_error": _short_text(str(exc), 180),
            }
        )
    return item


def _cross_validate_top_pool(
    ranking: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    history_dir: Path,
    data_date: str,
    source_normalized_dir: Path,
) -> list[dict[str, Any]]:
    targets = _cross_validation_targets(ranking)
    if not targets:
        return []

    source_label_by_code = {
        str(row["代码"]).zfill(6): str(row.get("行情来源") or "")
        for row in ranking.to_dict("records")
    }

    report: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=CROSS_VALIDATE_CONCURRENCY) as executor:
        futures = {
            executor.submit(
                _cross_validate_one_target,
                code,
                histories,
                history_dir,
                data_date,
                source_label_by_code,
            ): code
            for code in targets
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                report.append(result)

    return report


def _cross_validation_targets(ranking: pd.DataFrame) -> list[str]:
    if ranking.empty:
        return []
    targets: list[str] = []
    for _, row in ranking.iterrows():
        code = str(row["代码"]).zfill(6)
        source = str(row.get("行情来源") or "")
        confidence = str(row.get("数据置信度") or "")
        rank = int(row["排名"])
        rank_change = pd.to_numeric(pd.Series([row.get("排名变化")]), errors="coerce").iloc[0]
        is_top = rank <= CROSS_VALIDATE_TOP_N
        is_large_rank_change = (
            rank <= CROSS_VALIDATE_FALLBACK_RANK_LIMIT
            and pd.notna(rank_change)
            and abs(int(rank_change)) >= CROSS_VALIDATE_RANK_CHANGE_THRESHOLD
        )
        is_fallback = source in {"新浪历史行情", "新浪兜底行情", "实时行情补点", "来源未记录", "原始缓存重建"}
        is_problem = confidence in {"低", "中"}
        if (is_top or is_large_rank_change or (is_problem and rank <= CROSS_VALIDATE_TOP_N)) and is_fallback:
            targets.append(code)
    return list(dict.fromkeys(targets))[:CROSS_VALIDATE_MAX_TARGETS]


def _history_mismatch(current: pd.DataFrame, primary: pd.DataFrame) -> dict[str, Any]:
    current_frame = current[["日期", "收盘价", "成交额(亿)"]].copy()
    primary_frame = primary[["日期", "收盘价", "成交额(亿)"]].copy()
    merged = pd.merge(
        current_frame,
        primary_frame,
        on="日期",
        suffixes=("_current", "_primary"),
        how="inner",
    ).tail(20)
    if merged.empty:
        return {
            "status": "primary_mismatch",
            "reason": "主源与当前数据没有可比日期",
            "max_close_diff_pct": None,
            "max_amount_diff_pct": None,
        }

    close_diff = _relative_diff_pct(merged["收盘价_current"], merged["收盘价_primary"])
    amount_diff = _relative_diff_pct(merged["成交额(亿)_current"], merged["成交额(亿)_primary"])
    max_close = float(close_diff.max(skipna=True) or 0.0)
    max_amount = float(amount_diff.max(skipna=True) or 0.0)
    mismatch = max_close > CROSS_VALIDATE_CLOSE_TOLERANCE_PCT or max_amount > CROSS_VALIDATE_AMOUNT_TOLERANCE_PCT
    return {
        "status": "primary_mismatch" if mismatch else "primary_match",
        "reason": "主源与兜底源关键字段差异超过阈值" if mismatch else "",
        "max_close_diff_pct": round(max_close, 4),
        "max_amount_diff_pct": round(max_amount, 4),
    }


def _relative_diff_pct(left: pd.Series, right: pd.Series) -> pd.Series:
    left_num = pd.to_numeric(left, errors="coerce")
    right_num = pd.to_numeric(right, errors="coerce")
    denominator = right_num.abs().where(right_num.abs() > 0)
    return ((left_num - right_num).abs() / denominator * 100).fillna(0.0)


def _write_history_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _write_cross_validation_report(normalized_dir: Path, data_date: str, records: list[dict[str, Any]]) -> None:
    path = normalized_dir / "fetch_report.json"
    report = _read_fetch_report(path)
    payload = report.get("top_pool_cross_validation")
    if not isinstance(payload, dict):
        payload = {}
    payload[data_date] = records
    report["top_pool_cross_validation"] = payload
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_fetch_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _short_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


def _history_source_for_code(sources: dict[str, str], history: pd.DataFrame, code: str) -> str:
    if "history_source" in history:
        values = history["history_source"].dropna().astype(str).str.strip()
        if values.eq("spot_fallback").any():
            return "spot_fallback"
        non_empty = values[values != ""]
        if not non_empty.empty:
            return str(non_empty.iloc[-1])
    mapped = str(sources.get(code) or "").strip()
    if mapped:
        if mapped == "cache_normalized":
            return "unknown"
        return mapped
    return "unknown"


def _history_uses_spot_fallback(source: str, history: pd.DataFrame) -> bool:
    if source == "spot_fallback":
        return True
    if "history_source" not in history:
        return False
    values = history["history_source"].fillna("").astype(str).str.strip()
    return bool(values.eq("spot_fallback").any())


def _history_source_is_trusted(source: str) -> bool:
    return source in TRUSTED_HISTORY_PROVIDERS


def _history_source_label(source: str) -> str:
    return {
        "cache_normalized": "来源未记录",
        "cache_raw": "原始缓存重建",
        "eastmoney_urllib": "东方财富历史行情",
        "eastmoney_curl": "东方财富历史行情",
        "eastmoney_curl_proxy": "东方财富历史行情",
        "sina": "新浪兜底行情",
        "spot_fallback": "实时行情补点",
        "fixture": "测试夹具",
        "unknown": "来源未记录",
        "missing": "缺失",
    }.get(source, source or "来源未记录")


def _share_source_label(source: str) -> str:
    return {
        "sse": "上交所份额",
        "szse": "深交所份额",
        "spot_fallback": "实时行情份额",
        "fixture": "测试夹具",
        "missing": "缺失",
        "unknown": "来源未记录",
    }.get(source, source or "来源未记录")


def _data_issues(
    flags: list[str],
    history_source: str,
    share_source: str,
    cross_status: dict[str, Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    if "LOW_CONF_HISTORY_SPOT_FALLBACK" in flags:
        issues.append("历史行情补点来自实时行情快照，建议重新拉取历史行情")
    elif "LOW_CONF_HISTORY_CROSS_MISMATCH" in flags:
        issues.append(_cross_mismatch_issue(cross_status or {}))
    elif "LOW_CONF_HISTORY_UNKNOWN" in flags:
        issues.append("历史行情来源未记录，建议重新拉取确认")
    if "MEDIUM_CONF_HISTORY_SINA" in flags:
        primary_error = str((cross_status or {}).get("primary_error") or "").strip()
        suffix = f"：{primary_error}" if primary_error else ""
        issues.append(f"东方财富历史行情暂不可用，使用新浪兜底{suffix}")
    if "MEDIUM_CONF_HISTORY_CACHE_RAW" in flags:
        issues.append("历史行情由原始缓存重建，建议重拉以补全主源来源")
    if "MEDIUM_CONF_HISTORY_PRIMARY_FAILED" in flags and "MEDIUM_CONF_HISTORY_SINA" not in flags:
        primary_error = str((cross_status or {}).get("primary_error") or "").strip()
        issues.append(f"东方财富主源复核失败{f'：{primary_error}' if primary_error else ''}")
    if "LOW_CONF_SHARE_SPOT_FALLBACK" in flags:
        issues.append("官方份额接口缺失，使用实时行情份额兜底")
    if "LOW_CONF_SHARE" in flags:
        issues.append("份额历史缺失，资金流入评分使用低置信度兜底")
    if "SHARE_LT_20" in flags:
        issues.append("份额历史不足 20 个交易日")
    if "LOW_CONF_OHLC" in flags:
        issues.append("开高低价格由收盘价兜底，K线结构需复核")
    if "WARN_STALE_HIST" in flags:
        issues.append("历史行情日期滞后")
    if "MARKET_CAP_SPOT_FALLBACK" in flags:
        issues.append("市值由行情缓存兜底")
    if "MISSING_MARKET_CAP" in flags:
        issues.append("市值数据缺失")
    return list(dict.fromkeys(issues))


def _cross_mismatch_issue(cross_status: dict[str, Any]) -> str:
    close_diff = cross_status.get("max_close_diff_pct")
    amount_diff = cross_status.get("max_amount_diff_pct")
    detail = []
    if close_diff is not None:
        detail.append(f"收盘价最大差异 {close_diff}%")
    if amount_diff is not None:
        detail.append(f"成交额最大差异 {amount_diff}%")
    suffix = f"（{'，'.join(detail)}）" if detail else ""
    return f"主源与兜底源关键字段不一致{suffix}"


def _quality_summary(output: pd.DataFrame, rejected: dict[str, int]) -> dict[str, Any]:
    warnings = output[output["source_flags"] != "OK"]["source_flags"].value_counts().to_dict()
    confidence_counts = output["数据置信度"].value_counts().to_dict() if "数据置信度" in output else {}
    history_source_counts = output["行情来源"].value_counts().to_dict() if "行情来源" in output else {}
    share_source_counts = output["份额来源"].value_counts().to_dict() if "份额来源" in output else {}
    low_confidence_rows = int((output.get("数据置信度") == "低").sum()) if "数据置信度" in output else 0
    top10_low_confidence_rows = int((output.head(10).get("数据置信度") == "低").sum()) if "数据置信度" in output else 0
    issue_counts = output[output["数据问题"] != "无"]["数据问题"].value_counts().to_dict() if "数据问题" in output else {}
    status = "OK" if not warnings and not rejected and low_confidence_rows == 0 else "WARN"
    return {
        "status": status,
        "included_rows": int(len(output)),
        "low_confidence_rows": low_confidence_rows,
        "top10_low_confidence_rows": top10_low_confidence_rows,
        "confidence_counts": {str(key): int(value) for key, value in confidence_counts.items()},
        "history_source_counts": {str(key): int(value) for key, value in history_source_counts.items()},
        "share_source_counts": {str(key): int(value) for key, value in share_source_counts.items()},
        "issue_counts": {str(key): int(value) for key, value in issue_counts.items()},
        "warnings": {str(key): int(value) for key, value in warnings.items()},
        "rejected": {str(key): int(value) for key, value in rejected.items()},
    }


def _previous_ranking(root: Path, data_date: str) -> pd.DataFrame | None:
    radar_dir = root / "data" / "radar"
    paths = sorted(path for path in radar_dir.glob("*.csv") if path.stem < data_date)
    if not paths:
        return None
    frame = pd.read_csv(paths[-1], dtype={"代码": str})
    frame["代码"] = frame["代码"].astype(str).str.zfill(6)
    return frame.sort_values("排名")


def _previous_rank_map(root: Path, data_date: str) -> dict[str, int]:
    previous = _previous_ranking(root, data_date)
    if previous is None or previous.empty:
        return {}
    return dict(zip(previous["代码"].astype(str).str.zfill(6), previous["排名"].astype(int), strict=True))


def _rank_change(previous_rank: dict[str, int], code: str, current_rank: int) -> int | None:
    previous = previous_rank.get(code)
    if previous is None:
        return None
    return previous - current_rank


def _count(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _should_update_latest(latest_json: Path, data_date: str) -> bool:
    if not latest_json.exists():
        return True
    try:
        payload = json.loads(latest_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    current_date = str(payload.get("data_date", ""))
    return not current_date or data_date >= current_date


__all__ = [
    "DETAIL_HISTORY_COLUMNS",
    "EMA_WINDOWS",
    "QUALITY_COLUMNS",
    "ScoreResult",
    "build_changes_from_frames",
    "generate_scores",
]
