"""Build the radar ranking from normalized ETF data."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from .config import (
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


def generate_scores(data_date: str, root: Path = PROJECT_ROOT, source_date: str | None = None) -> ScoreResult:
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

        if share_history.empty:
            source_flags.append("LOW_CONF_SHARE")
            share_5d = 0.0
            share_20d = 0.0
        else:
            share_5d = _pct_change_from_tail(share_history["ETF份额(亿份)"], 5)
            share_20d = _pct_change_from_tail(share_history["ETF份额(亿份)"], 20)
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
    if "LOW_CONF_SHARE" in flags:
        return "份额低置信度"
    if "LOW_CONF_OHLC" in flags:
        return "OHLC低置信度"
    if "WARN_STALE_HIST" in flags:
        return "日期滞后"
    return "需复核"


def _quality_summary(output: pd.DataFrame, rejected: dict[str, int]) -> dict[str, Any]:
    warnings = output[output["source_flags"] != "OK"]["source_flags"].value_counts().to_dict()
    status = "OK" if not warnings and not rejected else "WARN"
    return {
        "status": status,
        "included_rows": int(len(output)),
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
    "ScoreResult",
    "build_changes_from_frames",
    "generate_scores",
]
