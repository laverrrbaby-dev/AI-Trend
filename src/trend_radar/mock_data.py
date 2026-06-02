"""Mock data and UI-facing calculations for the ETF radar prototype."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")

AVAILABLE_DATES = ("2026-05-27", "2026-05-28", "2026-05-29")


@dataclass(frozen=True)
class EtfProfile:
    code: str
    name: str
    theme: str
    base_price: float


ETF_PROFILES: tuple[EtfProfile, ...] = (
    EtfProfile("562500", "机器人ETF", "高端制造", 1.23),
    EtfProfile("159995", "芯片ETF", "科技成长", 1.06),
    EtfProfile("159819", "人工智能ETF", "科技成长", 0.92),
    EtfProfile("588000", "科创50ETF", "宽基成长", 0.83),
    EtfProfile("512760", "半导体ETF", "科技成长", 0.98),
    EtfProfile("512660", "军工ETF", "高端制造", 1.12),
    EtfProfile("515030", "新能源车ETF", "新能源", 0.77),
    EtfProfile("510300", "沪深300ETF", "宽基核心", 4.16),
    EtfProfile("512010", "医药ETF", "医药消费", 0.45),
    EtfProfile("159928", "消费ETF", "医药消费", 0.81),
    EtfProfile("512880", "证券ETF", "金融地产", 1.03),
    EtfProfile("159949", "创业板50ETF", "宽基成长", 0.94),
    EtfProfile("515790", "光伏ETF", "新能源", 0.62),
    EtfProfile("512800", "银行ETF", "金融地产", 1.21),
    EtfProfile("159915", "创业板ETF", "宽基成长", 1.88),
    EtfProfile("512480", "半导体龙头ETF", "科技成长", 0.89),
    EtfProfile("512690", "酒ETF", "医药消费", 0.74),
    EtfProfile("515050", "5GETF", "科技成长", 0.71),
    EtfProfile("512400", "有色金属ETF", "周期资源", 1.08),
    EtfProfile("159825", "农业ETF", "消费农业", 0.68),
    EtfProfile("516160", "新能源ETF", "新能源", 0.59),
    EtfProfile("159611", "电力ETF", "公用事业", 1.14),
    EtfProfile("512200", "房地产ETF", "金融地产", 0.64),
    EtfProfile("512070", "非银ETF", "金融地产", 0.86),
)

PROFILE_BY_CODE = {profile.code: profile for profile in ETF_PROFILES}

RANKINGS: dict[str, tuple[str, ...]] = {
    "2026-05-27": (
        "159995",
        "512880",
        "588000",
        "510300",
        "512760",
        "159819",
        "512800",
        "159915",
        "512010",
        "159928",
        "512690",
        "159949",
        "562500",
        "515030",
        "512660",
        "515790",
        "512480",
        "515050",
        "512400",
        "159825",
        "516160",
        "159611",
        "512070",
        "512200",
    ),
    "2026-05-28": (
        "159995",
        "588000",
        "159819",
        "512760",
        "512880",
        "510300",
        "159949",
        "512800",
        "512010",
        "515790",
        "562500",
        "159928",
        "515030",
        "512660",
        "159915",
        "512480",
        "512690",
        "515050",
        "516160",
        "512400",
        "159825",
        "159611",
        "512070",
        "512200",
    ),
    "2026-05-29": (
        "562500",
        "159995",
        "159819",
        "588000",
        "512760",
        "512660",
        "515030",
        "510300",
        "512010",
        "159928",
        "159949",
        "512880",
        "515790",
        "512480",
        "159915",
        "516160",
        "515050",
        "512690",
        "512400",
        "512800",
        "159825",
        "159611",
        "512070",
        "512200",
    ),
}

SCORES: dict[str, dict[str, int]] = {
    "2026-05-27": {
        "159995": 91,
        "512880": 86,
        "588000": 84,
        "510300": 80,
        "512760": 79,
        "159819": 77,
        "512800": 74,
        "159915": 72,
        "512010": 70,
        "159928": 69,
        "512690": 66,
        "159949": 65,
        "562500": 63,
        "515030": 62,
        "512660": 60,
        "515790": 58,
        "512480": 57,
        "515050": 55,
        "512400": 52,
        "159825": 50,
        "516160": 48,
        "159611": 45,
        "512070": 43,
        "512200": 39,
    },
    "2026-05-28": {
        "159995": 92,
        "588000": 86,
        "159819": 84,
        "512760": 81,
        "512880": 78,
        "510300": 76,
        "159949": 73,
        "512800": 71,
        "512010": 70,
        "515790": 69,
        "562500": 68,
        "159928": 67,
        "515030": 65,
        "512660": 64,
        "159915": 62,
        "512480": 60,
        "512690": 57,
        "515050": 55,
        "516160": 54,
        "512400": 52,
        "159825": 48,
        "159611": 46,
        "512070": 44,
        "512200": 40,
    },
    "2026-05-29": {
        "562500": 94,
        "159995": 91,
        "159819": 87,
        "588000": 84,
        "512760": 82,
        "512660": 78,
        "515030": 76,
        "510300": 74,
        "512010": 72,
        "159928": 70,
        "159949": 68,
        "512880": 66,
        "515790": 65,
        "512480": 63,
        "159915": 61,
        "516160": 58,
        "515050": 56,
        "512690": 54,
        "512400": 51,
        "512800": 49,
        "159825": 47,
        "159611": 45,
        "512070": 42,
        "512200": 38,
    },
}

CUSTOM_REASONS = {
    "562500": "成交额快速进入核心区间，近5日份额扩张，均线结构转为多头排列",
    "159995": "成交额连续驻留Top30，20日和60日趋势保持领先，份额继续温和增加",
    "159819": "热度排名稳定抬升，短中期涨幅同步走强，资金关注保持连续",
    "588000": "宽基成长方向维持高成交额，趋势结构健康，适合作为风格锚点观察",
    "512760": "半导体方向维持高热度，成交额驻留充分，价格仍在主要均线上方",
    "512660": "军工方向新进观察区，成交额放大明显，趋势结构进入转强阶段",
    "515030": "新能源车方向重新抬升，份额变化转正，短期动量明显改善",
    "512880": "证券方向热度回落，仍有成交额基础，但趋势强度低于前排主线",
    "512200": "历史热度和趋势结构都偏弱，当前仅保留为低置信度样例",
}


def resolve_default_date(now: datetime | None = None) -> str:
    """Pick the dashboard default date from local Shanghai time and cached dates."""
    current = now.astimezone(ASIA_SHANGHAI) if now else datetime.now(ASIA_SHANGHAI)
    available = [date.fromisoformat(item) for item in AVAILABLE_DATES]
    latest = max(available)
    today = current.date()

    if today in available and current.time() >= time(15, 30):
        return today.isoformat()

    complete_dates = [item for item in available if item < today or (item == today and current.time() >= time(15, 30))]
    if complete_dates:
        return max(complete_dates).isoformat()
    return latest.isoformat()


def get_available_dates() -> list[str]:
    return list(AVAILABLE_DATES)


def load_snapshot(data_date: str) -> pd.DataFrame:
    """Return the full mock ETF ranking for a data date."""
    if data_date not in RANKINGS:
        raise ValueError(f"Unsupported mock date: {data_date}")

    rows: list[dict[str, object]] = []
    score_map = SCORES[data_date]
    previous_rank = _previous_rank_map(data_date)

    for index, code in enumerate(RANKINGS[data_date], start=1):
        profile = PROFILE_BY_CODE[code]
        score = score_map[code]
        prev_rank = previous_rank.get(code)
        change = None if prev_rank is None else prev_rank - index
        row = _build_row(profile, score, index, data_date, change)
        rows.append(row)

    return pd.DataFrame(rows).sort_values("主线得分", ascending=False).reset_index(drop=True)


def build_changes(data_date: str) -> dict[str, list[str]]:
    """Compare current Top10 with the previous available date."""
    previous_date = _previous_date(data_date)
    current = load_snapshot(data_date).head(10)
    if not previous_date:
        return {
            "新进Top10": current["ETF名称"].head(3).tolist(),
            "连续Top10": [],
            "跌出Top10": [],
        }

    previous = load_snapshot(previous_date).head(10)
    current_codes = list(current["代码"])
    previous_codes = list(previous["代码"])
    current_name = dict(zip(current["代码"], current["ETF名称"], strict=True))
    previous_name = dict(zip(previous["代码"], previous["ETF名称"], strict=True))

    new_codes = [code for code in current_codes if code not in previous_codes]
    continuing_codes = [code for code in current_codes if code in previous_codes]
    dropped_codes = [code for code in previous_codes if code not in current_codes]

    return {
        "新进Top10": [current_name[code] for code in new_codes],
        "连续Top10": [current_name[code] for code in continuing_codes],
        "跌出Top10": [previous_name[code] for code in dropped_codes],
    }


def build_price_history(row: pd.Series, data_date: str) -> pd.DataFrame:
    """Build deterministic OHLC, EMA, amount and share mock series for one ETF."""
    code = str(row["代码"])
    profile = PROFILE_BY_CODE[code]
    periods = 150
    dates = pd.bdate_range(end=pd.Timestamp(data_date), periods=periods)

    ret_120 = float(row["120日涨幅"]) / 100.0
    final_price = profile.base_price * (1.0 + _code_seed(code) / 600.0)
    start_price = max(0.2, final_price / max(0.35, 1.0 + ret_120))
    trend_gain = final_price - start_price
    wave_scale = 0.012 if row["趋势状态"] in {"强趋势", "趋势向上"} else 0.024

    close_values: list[float] = []
    amount_values: list[float] = []
    volume_values: list[float] = []
    share_values: list[float] = []
    base_amount = float(row["成交额(亿)"])
    base_share = 35.0 + _code_seed(code) * 1.7
    share_20d = float(row["20日份额变化"])

    for index in range(periods):
        progress = index / (periods - 1)
        wave = math.sin(index / 6.0 + _code_seed(code)) * wave_scale
        pullback = math.sin(index / 17.0 + len(code)) * 0.008
        close = start_price + trend_gain * progress
        close *= 1.0 + wave + pullback
        close_values.append(round(max(close, 0.05), 4))

        amount_wave = 1.0 + math.sin(index / 5.5 + _code_seed(code) / 3.0) * 0.18
        amount_ramp = 0.72 + progress * (0.42 if float(row["主线得分"]) >= 70 else 0.18)
        amount = round(max(0.05, base_amount * amount_wave * amount_ramp), 2)
        amount_values.append(amount)
        volume_values.append(round(amount * 100_000_000 / close_values[-1] / 100, 0))

        recent_boost = max(0.0, (progress - 0.86) / 0.14) * share_20d / 100.0
        share_wave = math.sin(index / 13.0 + _code_seed(code)) * 0.003
        share_values.append(round(base_share * (1.0 + recent_boost + share_wave), 2))

    open_values: list[float] = []
    high_values: list[float] = []
    low_values: list[float] = []
    previous_close = close_values[0]
    for index, close in enumerate(close_values):
        gap = math.sin(index / 9.0 + _code_seed(code) / 5.0) * 0.006
        open_price = previous_close * (1.0 + gap) if index else close * (1.0 - gap / 2.0)
        high = max(open_price, close) * (1.0 + 0.002 + abs(math.sin(index / 7.0)) * 0.008)
        low = min(open_price, close) * (1.0 - 0.002 - abs(math.cos(index / 8.0)) * 0.008)
        open_values.append(round(max(open_price, 0.05), 4))
        high_values.append(round(max(high, open_price, close), 4))
        low_values.append(round(max(min(low, open_price, close), 0.01), 4))
        previous_close = close

    history = pd.DataFrame(
        {
            "日期": dates,
            "开盘价": open_values,
            "最高价": high_values,
            "最低价": low_values,
            "收盘价": close_values,
            "成交额(亿)": amount_values,
            "成交量": volume_values,
            "ETF份额(亿份)": share_values,
        }
    )
    for window in (13, 26, 144, 169):
        history[f"EMA{window}"] = history["收盘价"].ewm(span=window, adjust=False, min_periods=1).mean()
    return history


def build_score_breakdown(row: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"维度": "成交额热度", "得分": float(row["热度分"]), "权重": "45%"},
            {"维度": "份额流入", "得分": float(row["资金分"]), "权重": "20%"},
            {"维度": "趋势强度", "得分": float(row["趋势分"]), "权重": "25%"},
            {"维度": "趋势结构", "得分": float(row["结构分"]), "权重": "10%"},
        ]
    )


def _build_row(profile: EtfProfile, score: int, rank: int, data_date: str, rank_change: int | None) -> dict[str, object]:
    seed = _code_seed(profile.code)
    status = _trend_status(score, profile.code)
    heat_score = min(100, score + 4 + (10 - min(rank, 10)) * 0.7)
    flow_score = max(20, min(100, score - 5 + (seed % 6)))
    momentum_score = max(20, min(100, score + (4 if status in {"强趋势", "趋势向上"} else -3)))
    structure_score = _structure_score(status)

    return_20d = round((score - 55) * 0.42 + (seed % 7) * 0.35, 2)
    return_60d = round((score - 50) * 0.72 + (seed % 5) * 0.8, 2)
    return_120d = round((score - 48) * 0.95 + (seed % 6) * 1.1, 2)
    if status == "数据不足":
        return_120d = 0.0
        return_60d = round(return_60d * 0.35, 2)
        return_20d = round(return_20d * 0.25, 2)

    share_5d = round((score - 62) * 0.18 + (seed % 5) * 0.22, 2)
    share_20d = round((score - 58) * 0.34 + (seed % 7) * 0.35, 2)
    if score < 55:
        share_5d = round(-1.2 + (seed % 3) * 0.25, 2)
        share_20d = round(-3.0 + (seed % 5) * 0.3, 2)

    top30_days = max(1, min(20, 22 - rank + (seed % 4)))
    amount_rank = min(35, max(1, rank + (seed % 5) - 2))
    amount_100m = round(max(0.35, 18.5 - rank * 0.62 + score * 0.06 + (seed % 6) * 0.28), 2)
    avg_amount_20d = round(max(0.25, amount_100m * (0.72 + min(top30_days, 20) / 80.0)), 2)

    return {
        "排名": rank,
        "ETF名称": profile.name,
        "代码": profile.code,
        "主题": profile.theme,
        "主线得分": score,
        "趋势状态": status,
        "成交额Top30次数": top30_days,
        "成交额排名": amount_rank,
        "成交额(亿)": amount_100m,
        "20日均额(亿)": avg_amount_20d,
        "20日涨幅": return_20d,
        "60日涨幅": return_60d,
        "120日涨幅": return_120d,
        "5日份额变化": share_5d,
        "20日份额变化": share_20d,
        "均线结构": _ma_structure(status),
        "排名变化": rank_change,
        "数据质量": "历史不足" if status == "数据不足" else "完整",
        "观察理由": CUSTOM_REASONS.get(profile.code, _default_reason(status)),
        "热度分": round(heat_score, 1),
        "资金分": round(flow_score, 1),
        "趋势分": round(momentum_score, 1),
        "结构分": round(structure_score, 1),
        "数据日期": data_date,
    }


def _trend_status(score: int, code: str) -> str:
    if code == "512200":
        return "数据不足"
    if score >= 88:
        return "强趋势"
    if score >= 74:
        return "趋势向上"
    if score >= 64:
        return "转强观察"
    if score >= 52:
        return "震荡观察"
    return "弱观察"


def _structure_score(status: str) -> float:
    return {
        "强趋势": 96.0,
        "趋势向上": 86.0,
        "转强观察": 72.0,
        "震荡观察": 50.0,
        "弱观察": 35.0,
        "数据不足": 20.0,
    }[status]


def _ma_structure(status: str) -> str:
    return {
        "强趋势": "EMA13 > EMA26 > EMA144 > EMA169",
        "趋势向上": "EMA13 > EMA26 > EMA144 > EMA169",
        "转强观察": "收盘价 > EMA13 > EMA26",
        "震荡观察": "EMA13 与 EMA26 缠绕",
        "弱观察": "主要EMA下方",
        "数据不足": "历史不足",
    }[status]


def _default_reason(status: str) -> str:
    return {
        "强趋势": "成交额热度、份额变化和多周期趋势共振，主线特征清晰",
        "趋势向上": "成交额驻留较稳定，趋势结构保持向上，适合继续观察",
        "转强观察": "短期热度改善，趋势结构开始修复，需要继续确认持续性",
        "震荡观察": "资金关注度一般，趋势结构尚未形成清晰向上排列",
        "弱观察": "成交额和趋势强度不足，当前观察优先级偏低",
        "数据不足": "历史样本不足，当前仅用于界面状态覆盖",
    }[status]


def _previous_date(data_date: str) -> str | None:
    dates = get_available_dates()
    index = dates.index(data_date)
    if index == 0:
        return None
    return dates[index - 1]


def _previous_rank_map(data_date: str) -> dict[str, int]:
    previous = _previous_date(data_date)
    if not previous:
        return {}
    return {code: index for index, code in enumerate(RANKINGS[previous], start=1)}


def _code_seed(code: str) -> int:
    return sum(int(char) for char in code if char.isdigit())
