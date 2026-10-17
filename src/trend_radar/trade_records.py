"""Manual trade lifecycle with independent, locked, atomic persistence."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .trade_checklist import evaluate_checklist


def decimal_value(raw: object, label: str, *, signed: bool = False) -> Decimal:
    try:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 64:
            raise ValueError
        value = Decimal(raw.strip())
        if not value.is_finite() or value.copy_abs() > Decimal("1e18"):
            raise ValueError
        if not signed and value < Decimal("1e-18"):
            raise ValueError
        return value
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"{label}须为有效{'数值' if signed else '正数'}。") from exc


def trade_time(raw: str, label: str) -> datetime:
    try:
        if not isinstance(raw, str) or len(raw) > 64:
            raise ValueError
        value = datetime.fromisoformat(raw.strip())
        if value.utcoffset() is None:
            raise ValueError
        return value
    except ValueError as exc:
        raise ValueError(f"{label}须包含日期、时间和时区，例如 2026-10-17T10:30:00+08:00。") from exc


def validate_execution(values: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key in ("instrument_id", "entry_price", "quantity", "opened_at"):
        value = values.get(key)
        if not isinstance(value, str):
            raise ValueError("请填写完整的实际成交信息。")
        cleaned[key] = value.strip()
    if not re.fullmatch(r"[A-Z0-9]+-(?:USDT|USDC|USD)(?:-SWAP|-\d{6})?", cleaned["instrument_id"]):
        raise ValueError("请填写 OKX 完整交易代码，例如 BTC-USDT-SWAP；现货与合约不能混用。")
    decimal_value(cleaned["entry_price"], "实际成交价")
    decimal_value(cleaned["quantity"], "实际数量")
    trade_time(cleaned["opened_at"], "开仓时间")
    return cleaned


def _outcome(values: dict[str, str], execution: dict[str, str]) -> dict[str, str]:
    cleaned = {}
    for key in ("closed_at", "pnl", "reason", "notes"):
        value = values.get(key, "" if key == "notes" else None)
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("请填写完整的平仓结果。")
        cleaned[key] = value.strip()
    if trade_time(cleaned["closed_at"], "平仓时间") < trade_time(execution["opened_at"], "开仓时间"):
        raise ValueError("平仓时间不能早于开仓时间。")
    decimal_value(cleaned["pnl"], "实际盈亏 U", signed=True)
    if not cleaned["reason"]:
        raise ValueError("请填写退出原因。")
    return cleaned


@dataclass(frozen=True)
class TradeRecord:
    id: str
    revision: int
    status: str
    snapshot: dict[str, Any]
    execution: dict[str, str]
    stop_price: str | None = None
    result: dict[str, str] | None = None

    @property
    def exit_baseline(self) -> Decimal:
        return decimal_value(self.stop_price or self.snapshot["liquidation_price"], "当前退出基准")


class TradeRecordStore:
    def __init__(self, root: Path):
        self.path = root / "data" / "crypto" / "trade_records" / "records.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def list_records(self) -> list[TradeRecord]:
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
                raise ValueError
            if not isinstance(payload.get("records"), list):
                raise ValueError
            records = []
            for item in payload["records"]:
                record = TradeRecord(**item)
                if not isinstance(record.id, str) or not re.fullmatch(r"[0-9a-f]{32}", record.id):
                    raise ValueError
                if type(record.revision) is not int or record.revision < 1 or record.status not in ("active", "closed"):
                    raise ValueError
                if not isinstance(record.snapshot, dict) or not evaluate_checklist(record.snapshot).passed:
                    raise ValueError
                if not isinstance(record.execution, dict) or validate_execution(record.execution) != record.execution:
                    raise ValueError
                if record.stop_price is not None:
                    stop = decimal_value(record.stop_price, "止损价")
                    initial = decimal_value(record.snapshot["liquidation_price"], "初始退出价")
                    if not (stop > initial if record.snapshot["direction"] == "做多" else stop < initial):
                        raise ValueError
                if record.status == "closed":
                    if not isinstance(record.result, dict) or _outcome(record.result, record.execution) != record.result:
                        raise ValueError
                elif record.result is not None:
                    raise ValueError
                records.append(record)
            if len({record.id for record in records}) != len(records) or sum(record.status == "active" for record in records) > 1:
                raise ValueError
            return records
        except (ValueError, TypeError, KeyError, UnicodeError, AttributeError) as exc:
            raise ValueError("交易记录损坏或格式不兼容，原文件未被覆盖，请检查记录文件。") from exc

    def _write(self, records: list[TradeRecord]) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"version": 1, "records": [asdict(record) for record in records]}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def create(self, snapshot: dict[str, Any], execution: dict[str, str], *, confirmed: bool) -> TradeRecord:
        if confirmed is not True or not evaluate_checklist(snapshot).passed:
            raise ValueError("须通过交易前检查，并确认已经实际成交。")
        actual = validate_execution(execution)
        # A JSON copy freezes the saved plan independently of caller-owned dictionaries.
        record = TradeRecord(uuid4().hex, 1, "active", json.loads(json.dumps(snapshot)), actual)
        with self._lock():
            records = self.list_records()
            if any(item.status == "active" for item in records):
                raise ValueError("已有一笔进行中交易，请先结束或删除后再保存新交易。")
            self._write([record, *records])
        return record

    def _replace(self, record_id: str, revision: int, *, status: str | None = None,
                 execution: dict[str, str] | None = None, stop_price: str | None = None,
                 result: dict[str, str] | None = None, delete: bool = False) -> TradeRecord:
        with self._lock():
            records = self.list_records()
            record = next((item for item in records if item.id == record_id), None)
            if record is None or record.revision != revision:
                raise ValueError("记录已被修改或删除，请重新加载后核对。")
            if status is not None and record.status != status:
                raise ValueError("交易状态已变化，请重新加载。")
            actual = validate_execution(execution) if execution is not None else record.execution
            updated = replace(record, revision=record.revision + 1, execution=actual)
            if stop_price is not None:
                stop = decimal_value(stop_price, "新止损价")
                if not (stop > record.exit_baseline if record.snapshot["direction"] == "做多" else stop < record.exit_baseline):
                    raise ValueError("止损只允许收紧；相同价格视为未调整。")
                updated = replace(updated, stop_price=stop_price.strip())
            if result is not None:
                updated = replace(updated, result=_outcome(result, actual), status="closed")
            self._write([updated if item.id == record_id else item for item in records] if not delete
                        else [item for item in records if item.id != record_id])
            return updated

    def update_execution(self, record_id: str, revision: int, execution: dict[str, str]) -> TradeRecord:
        return self._replace(record_id, revision, status="active", execution=execution)

    def update_stop(self, record_id: str, revision: int, price: str) -> TradeRecord:
        return self._replace(record_id, revision, status="active", stop_price=price)

    def close(self, record_id: str, revision: int, result: dict[str, str], *, confirmed: bool) -> TradeRecord:
        if confirmed is not True:
            raise ValueError("请先核对并确认平仓信息。")
        return self._replace(record_id, revision, status="active", result=result)

    def update_closed(self, record_id: str, revision: int, execution: dict[str, str], result: dict[str, str], *, confirmed: bool) -> TradeRecord:
        if confirmed is not True:
            raise ValueError("历史修改需要二次确认。")
        return self._replace(record_id, revision, status="closed", execution=execution, result=result)

    def delete(self, record_id: str, revision: int) -> None:
        self._replace(record_id, revision, delete=True)
