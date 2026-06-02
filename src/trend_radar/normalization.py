"""Normalize public ETF data into stable internal columns."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


CODE_COLUMNS = ("代码", "基金代码", "证券代码", "基金管理人代码")
NAME_COLUMNS = ("名称", "基金简称", "基金名称", "证券简称", "简称")


def pick_column(columns: Iterable[object], candidates: Iterable[str]) -> str | None:
    normalized = {str(column).strip(): str(column) for column in columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for column in normalized:
        for candidate in candidates:
            if candidate in column:
                return normalized[column]
    return None


def normalize_code(value: object) -> str:
    text = str(value).strip()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(char for char in text if char.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def to_numeric_series(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace("亿元", "", regex=False)
        .str.replace("亿份", "", regex=False)
        .str.replace("万元", "", regex=False)
        .str.replace("万份", "", regex=False)
        .str.replace("元", "", regex=False)
        .str.replace("份", "", regex=False)
        .str.replace("--", "", regex=False)
        .str.strip()
    )
    cleaned = cleaned.mask(cleaned == "-", "")
    return pd.to_numeric(cleaned, errors="coerce")


def money_to_yi(series: pd.Series, column_name: str = "") -> pd.Series:
    values = to_numeric_series(series)
    name = column_name.lower()
    if "亿元" in column_name or "(亿" in column_name or "（亿" in column_name:
        return values
    if "万元" in column_name or "(万" in column_name or "（万" in column_name:
        return values / 10_000
    median = values.abs().dropna().median()
    if pd.isna(median):
        return values
    return values / 100_000_000 if median > 10_000 else values


def share_to_yi(series: pd.Series, column_name: str = "") -> pd.Series:
    values = to_numeric_series(series)
    if "亿份" in column_name or "(亿" in column_name or "（亿" in column_name:
        return values
    if "万份" in column_name or "(万" in column_name or "（万" in column_name:
        return values / 10_000
    median = values.abs().dropna().median()
    if pd.isna(median):
        return values
    if median > 100_000_000:
        return values / 100_000_000
    if median > 10_000:
        return values / 10_000
    return values


def normalize_spot(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()

    code_col = pick_column(raw.columns, CODE_COLUMNS)
    name_col = pick_column(raw.columns, NAME_COLUMNS)
    if not code_col or not name_col:
        raise ValueError("ETF spot data is missing code or name columns")

    price_col = pick_column(raw.columns, ("最新价", "最新", "现价", "收盘价"))
    pct_col = pick_column(raw.columns, ("涨跌幅",))
    amount_col = pick_column(raw.columns, ("成交额",))
    volume_col = pick_column(raw.columns, ("成交量",))
    date_col = pick_column(raw.columns, ("数据日期", "日期"))
    update_col = pick_column(raw.columns, ("更新时间", "时间"))
    share_col = pick_column(raw.columns, ("最新份额", "基金份额", "份额"))
    total_cap_col = pick_column(raw.columns, ("总市值", "基金规模"))
    float_cap_col = pick_column(raw.columns, ("流通市值",))

    normalized = pd.DataFrame(
        {
            "code": raw[code_col].map(normalize_code),
            "name": raw[name_col].astype(str).str.strip(),
        }
    )
    normalized["latest_price"] = to_numeric_series(raw[price_col]) if price_col else pd.NA
    normalized["pct_change"] = to_numeric_series(raw[pct_col]) if pct_col else pd.NA
    normalized["amount_yi"] = money_to_yi(raw[amount_col], amount_col) if amount_col else pd.NA
    normalized["volume"] = to_numeric_series(raw[volume_col]) if volume_col else pd.NA
    normalized["data_date"] = raw[date_col].astype(str).str.strip() if date_col else ""
    normalized["update_time"] = raw[update_col].astype(str).str.strip() if update_col else ""
    normalized["latest_share_yi"] = share_to_yi(raw[share_col], share_col) if share_col else pd.NA
    normalized["total_market_cap_yi"] = money_to_yi(raw[total_cap_col], total_cap_col) if total_cap_col else pd.NA
    normalized["float_market_cap_yi"] = money_to_yi(raw[float_cap_col], float_cap_col) if float_cap_col else pd.NA
    return normalized[normalized["code"].str.len() == 6].drop_duplicates("code").reset_index(drop=True)


def normalize_history(raw: pd.DataFrame, code: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()

    date_col = pick_column(raw.columns, ("日期", "交易日期", "数据日期"))
    open_col = pick_column(raw.columns, ("开盘", "开盘价"))
    close_col = pick_column(raw.columns, ("收盘", "收盘价", "最新价"))
    high_col = pick_column(raw.columns, ("最高", "最高价"))
    low_col = pick_column(raw.columns, ("最低", "最低价"))
    amount_col = pick_column(raw.columns, ("成交额",))
    volume_col = pick_column(raw.columns, ("成交量", "volume"))
    pct_col = pick_column(raw.columns, ("涨跌幅",))
    if not date_col or not close_col or not amount_col:
        raise ValueError(f"ETF history for {code} is missing date, close, or amount columns")

    close = to_numeric_series(raw[close_col])
    open_price = to_numeric_series(raw[open_col]) if open_col else close.copy()
    high = to_numeric_series(raw[high_col]) if high_col else close.copy()
    low = to_numeric_series(raw[low_col]) if low_col else close.copy()
    normalized = pd.DataFrame(
        {
            "code": normalize_code(code),
            "日期": pd.to_datetime(raw[date_col], errors="coerce"),
            "开盘价": open_price,
            "最高价": high,
            "最低价": low,
            "收盘价": close,
            "成交额(亿)": money_to_yi(raw[amount_col], amount_col),
            "成交量": to_numeric_series(raw[volume_col]) if volume_col else pd.NA,
            "涨跌幅": to_numeric_series(raw[pct_col]) if pct_col else pd.NA,
        }
    )
    normalized["开盘价"] = normalized["开盘价"].fillna(normalized["收盘价"])
    normalized["最高价"] = normalized["最高价"].fillna(normalized[["开盘价", "收盘价"]].max(axis=1))
    normalized["最低价"] = normalized["最低价"].fillna(normalized[["开盘价", "收盘价"]].min(axis=1))
    normalized["最高价"] = normalized[["最高价", "开盘价", "收盘价"]].max(axis=1)
    normalized["最低价"] = normalized[["最低价", "开盘价", "收盘价"]].min(axis=1)
    normalized = normalized.dropna(subset=["日期", "收盘价"]).sort_values("日期")
    return normalized.drop_duplicates(["code", "日期"], keep="last").reset_index(drop=True)


def normalize_scale(raw: pd.DataFrame, source: str, fallback_date: str | None = None) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=["code", "name", "日期", "ETF份额(亿份)", "share_source"])

    code_col = pick_column(raw.columns, CODE_COLUMNS)
    name_col = pick_column(raw.columns, NAME_COLUMNS)
    share_col = pick_column(
        raw.columns,
        (
            "基金份额",
            "基金总份额",
            "总份额",
            "份额",
            "基金规模",
            "流通份额",
            "最新份额",
        ),
    )
    date_col = pick_column(raw.columns, ("日期", "规模日期", "数据日期", "截止日期", "统计日期"))
    if not code_col or not share_col:
        return pd.DataFrame(columns=["code", "name", "日期", "ETF份额(亿份)", "share_source"])

    dates = pd.to_datetime(raw[date_col], errors="coerce") if date_col else pd.to_datetime(fallback_date)
    normalized = pd.DataFrame(
        {
            "code": raw[code_col].map(normalize_code),
            "name": raw[name_col].astype(str).str.strip() if name_col else "",
            "日期": dates,
            "ETF份额(亿份)": share_to_yi(raw[share_col], share_col),
            "share_source": source,
        }
    )
    normalized = normalized.dropna(subset=["日期", "ETF份额(亿份)"])
    return normalized[normalized["code"].str.len() == 6].drop_duplicates(["code", "日期"], keep="last").reset_index(drop=True)
