"""Instrument-specific OKX UTC+8 daily references for manual stop updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .crypto_market_data import JsonFetcher, _fetch_json
from .trade_records import TradeRecord, decimal_value

BEIJING = ZoneInfo("Asia/Shanghai")
OKX_MARKET = "https://www.okx.com/api/v5/market/"


@dataclass(frozen=True)
class StopMarket:
    instrument_id: str
    current_price: Decimal
    high: Decimal
    low: Decimal
    candle_open: datetime
    ticker_at: datetime


@dataclass(frozen=True)
class StopSuggestion:
    action: str
    price: Decimal


def suggest_stop(record: TradeRecord, market: StopMarket) -> StopSuggestion:
    if market.instrument_id != record.execution["instrument_id"]:
        raise ValueError("行情品种与实际持仓不一致。")
    long = record.snapshot["direction"] == "做多"
    reference = market.low if long else market.high
    if market.current_price <= reference if long else market.current_price >= reference:
        return StopSuggestion("crossed", reference)
    tighter = reference > record.exit_baseline if long else reference < record.exit_baseline
    return StopSuggestion("tighten" if tighter else "keep", reference)


def _timestamp(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 16:
        raise ValueError("行情时间无效。")
    return datetime.fromtimestamp(int(raw) / 1000, UTC).astimezone(BEIJING)


def fetch_stop_market(instrument_id: str, *, now: datetime | None = None,
                      fetch_json: JsonFetcher | None = None) -> StopMarket:
    """Read only public data; 1D is OKX's UTC+8 bar, unlike 1Dutc.

    Source: https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks
    """
    fetcher = fetch_json or _fetch_json

    def data(endpoint: str, **params: str) -> list[Any]:
        payload = fetcher(OKX_MARKET + endpoint + "?" + urlencode({"instId": instrument_id, **params}),
                          {"User-Agent": "trend-radar/trade-records", "Accept": "application/json"})
        if not isinstance(payload, dict) or payload.get("code") != "0" or not isinstance(payload.get("data"), list):
            raise ValueError("OKX 行情接口返回异常。")
        return payload["data"]

    try:
        rows = data("candles", bar="1D", limit="3")
        tickers = data("ticker")
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise ValueError("当前时间必须包含时区。")
        current = current.astimezone(BEIJING)
        yesterday = datetime.combine(current.date() - timedelta(days=1), time(), BEIJING)
        candidates = [row for row in rows if isinstance(row, list) and len(row) >= 9
                      and row[8] == "1" and _timestamp(row[0]) == yesterday]
        if len(candidates) != 1:
            raise ValueError("缺少最近一根已收盘的东八区日 K，暂时无法给出建议。")
        candle = candidates[0]
        opened, high, low, closed = (decimal_value(value, "日 K 价格") for value in candle[1:5])
        if not low <= min(opened, closed) <= max(opened, closed) <= high:
            raise ValueError("日 K 高低点无效。")
        if len(tickers) != 1 or not isinstance(tickers[0], dict) or tickers[0].get("instId") != instrument_id:
            raise ValueError("行情品种与实际持仓不一致。")
        ticker = tickers[0]
        ticker_at = _timestamp(ticker.get("ts"))
        age = (current - ticker_at).total_seconds()
        if age > 120 or age < -5:
            raise ValueError("最新成交价的时间过旧或异常，暂时无法给出建议。")
        return StopMarket(instrument_id, decimal_value(ticker.get("last"), "最新成交价"), high, low, yesterday, ticker_at)
    except (TypeError, KeyError, OverflowError, OSError) as exc:
        raise ValueError("行情格式或时间异常，暂时无法给出建议。") from exc
