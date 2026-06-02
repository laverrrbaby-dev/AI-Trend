"""ETF universe filters."""

from __future__ import annotations

import pandas as pd


EXCLUDE_KEYWORDS = (
    "货币",
    "现金",
    "保证金",
    "债",
    "可转债",
    "转债",
    "城投",
    "政金债",
    "国债",
    "地方债",
    "信用债",
    "公司债",
    "REIT",
    "REITS",
    "黄金",
    "白银",
    "有色期货",
    "豆粕",
    "能源化工",
    "商品",
    "纳指",
    "纳斯达克",
    "标普",
    "恒生",
    "恒指",
    "港股",
    "香港",
    "美国",
    "H股",
    "跨境",
    "中概",
    "日经",
    "德国",
    "法国",
    "韩国",
    "亚太",
    "东南亚",
    "沙特",
    "海外",
    "QDII",
    "互联互通",
)

INCLUDE_OVERRIDES: set[str] = set()
EXCLUDE_OVERRIDES: set[str] = set()
EXCLUDE_CODE_PREFIXES = ("508", "511", "513", "518", "520")


def classify_etf(name: str, code: str = "") -> tuple[bool, str]:
    normalized_name = str(name).upper()
    normalized_code = str(code).zfill(6)
    if normalized_code in INCLUDE_OVERRIDES:
        return True, "include_override"
    if normalized_code in EXCLUDE_OVERRIDES:
        return False, "exclude_override"
    if normalized_code.startswith(EXCLUDE_CODE_PREFIXES):
        return False, f"exclude_code_prefix:{normalized_code[:3]}"
    for keyword in EXCLUDE_KEYWORDS:
        if keyword.upper() in normalized_name:
            return False, f"exclude_keyword:{keyword}"
    if "ETF" not in normalized_name:
        return False, "exclude_not_etf"
    return True, "a_share_equity_candidate"


def filter_equity_etfs(spot: pd.DataFrame) -> pd.DataFrame:
    if spot.empty:
        return spot.copy()
    filtered = spot.copy()
    decisions = [classify_etf(row["name"], row["code"]) for _, row in filtered.iterrows()]
    filtered["is_equity_etf"] = [decision[0] for decision in decisions]
    filtered["universe_reason"] = [decision[1] for decision in decisions]
    return filtered[filtered["is_equity_etf"]].reset_index(drop=True)
