from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from trend_radar.scoring import ScoreResult, generate_scores


DEFAULT_CODES = ["159995", "510300", "512880", "515000", "560580"]
DEFAULT_NAMES = ["芯片ETF", "沪深300ETF", "证券ETF", "科技ETF", "机器人ETF"]
DEFAULT_AMOUNTS = {
    "159995": 5.0,
    "510300": 4.0,
    "512880": 3.0,
    "515000": 2.5,
    "560580": 2.0,
}


@dataclass(frozen=True)
class SourceFixture:
    root: Path
    data_date: str
    normalized_dir: Path
    history_dir: Path
    codes: list[str]
    dates: pd.DatetimeIndex


def write_source_fixture(
    root: Path,
    data_date: str = "2026-05-28",
    codes: list[str] | None = None,
    trading_days: int = 150,
    include_excluded: bool = False,
    future_rows: int = 0,
    missing_share_codes: set[str] | None = None,
) -> SourceFixture:
    """Create a deterministic normalized source snapshot under a temp root."""

    selected_codes = list(codes or DEFAULT_CODES)
    names = _names_for_codes(selected_codes)
    normalized_dir = root / "data" / "normalized" / data_date
    history_dir = normalized_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    spot_rows = []
    for index, (code, name) in enumerate(zip(selected_codes, names, strict=True), start=1):
        latest_price = 1.0 + index * 0.2 + trading_days * (0.0015 * index)
        latest_share = 30.0 + index * 5
        spot_rows.append(
            {
                "code": code,
                "name": name,
                "latest_price": latest_price,
                "pct_change": 0.5,
                "amount_yi": DEFAULT_AMOUNTS.get(code, max(0.5, 5.0 - index * 0.5)),
                "volume": 10000 * index,
                "data_date": data_date,
                "update_time": "15:00:00",
                "latest_share_yi": latest_share,
                "total_market_cap_yi": latest_price * latest_share,
                "float_market_cap_yi": latest_price * latest_share,
                "is_equity_etf": True,
                "universe_reason": "fixture",
            }
        )
    if include_excluded:
        spot_rows.append(
            {
                "code": "511990",
                "name": "华宝添益ETF",
                "latest_price": 1.0,
                "pct_change": 0.0,
                "amount_yi": 100.0,
                "volume": 1,
                "data_date": data_date,
                "update_time": "15:00:00",
                "latest_share_yi": 1000.0,
                "total_market_cap_yi": 1000.0,
                "float_market_cap_yi": 1000.0,
                "is_equity_etf": False,
                "universe_reason": "fixture_excluded",
            }
        )
    pd.DataFrame(spot_rows).to_csv(normalized_dir / "spot_candidates.csv", index=False)

    dates = pd.bdate_range(end=pd.Timestamp(data_date), periods=trading_days)
    all_history_dates = dates
    if future_rows:
        future_dates = pd.bdate_range(start=pd.Timestamp(data_date) + pd.Timedelta(days=1), periods=future_rows)
        all_history_dates = dates.append(future_dates)

    for index, code in enumerate(selected_codes, start=1):
        amount = DEFAULT_AMOUNTS.get(code, max(0.5, 5.0 - index * 0.5))
        history = normalized_history_frame(code, all_history_dates, amount, index=index)
        if future_rows:
            future_mask = history["日期"] > pd.Timestamp(data_date)
            history.loc[future_mask, "收盘价"] = history.loc[future_mask, "收盘价"] * 5
            history.loc[future_mask, "成交额(亿)"] = amount * 100
        history.to_csv(history_dir / f"{code}.csv", index=False)

    missing_share_codes = missing_share_codes or set()
    share_rows = []
    share_dates = dates[-40:]
    if future_rows:
        share_dates = share_dates.append(pd.bdate_range(start=pd.Timestamp(data_date) + pd.Timedelta(days=1), periods=future_rows))
    for index, code in enumerate(selected_codes, start=1):
        if code in missing_share_codes:
            continue
        base = 30 + index * 5
        for offset, current in enumerate(share_dates):
            value = base + offset * 0.05
            if current > pd.Timestamp(data_date):
                value = base * 100
            share_rows.append(
                {
                    "code": code,
                    "name": code,
                    "日期": current,
                    "ETF份额(亿份)": value,
                    "share_source": "fixture",
                }
            )
    pd.DataFrame(share_rows).to_csv(normalized_dir / "shares.csv", index=False)

    return SourceFixture(root, data_date, normalized_dir, history_dir, selected_codes, dates)


def generate_radar_fixture(root: Path, data_date: str = "2026-05-28") -> ScoreResult:
    write_source_fixture(root, data_date=data_date)
    return generate_scores(data_date, root=root)


def normalized_history_frame(code: str, dates: pd.DatetimeIndex, amount_yi: float, index: int = 1) -> pd.DataFrame:
    close = pd.Series(range(len(dates)), dtype=float) * (0.002 * index) + (1.0 + index * 0.2)
    return pd.DataFrame(
        {
            "code": code,
            "日期": dates,
            "开盘价": close - 0.001,
            "最高价": close + 0.004,
            "最低价": close - 0.004,
            "收盘价": close,
            "成交额(亿)": [amount_yi] * len(dates),
            "成交量": [10000 * index] * len(dates),
            "涨跌幅": [0.1] * len(dates),
        }
    )


def raw_history_frame(data_date: str, code: str, amount_yi: float = 5.0, trading_days: int = 150) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(data_date), periods=trading_days)
    index = DEFAULT_CODES.index(code) + 1 if code in DEFAULT_CODES else 1
    history = normalized_history_frame(code, dates, amount_yi, index=index)
    return pd.DataFrame(
        {
            "日期": history["日期"].dt.strftime("%Y-%m-%d"),
            "开盘": history["开盘价"],
            "最高": history["最高价"],
            "最低": history["最低价"],
            "收盘": history["收盘价"],
            "成交额": history["成交额(亿)"] * 100_000_000,
            "涨跌幅": history["涨跌幅"],
        }
    )


def _names_for_codes(codes: list[str]) -> list[str]:
    by_code = dict(zip(DEFAULT_CODES, DEFAULT_NAMES, strict=True))
    return [by_code.get(code, f"测试ETF{code}") for code in codes]
