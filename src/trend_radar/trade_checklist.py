"""Pre-entry trading rules and the current manual checklist draft."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

CHECK_LABELS = {
    "previous_position_closed": "上一笔持仓已结束",
    "orders_cleared": "剩余挂单已处理",
    "funds_checked": "上一笔剩余资金或盈利已清理，当前资金为 500U",
    "okx_cross_checked": "本次选择 OKX 全仓模式",
    "order_parameters_checked": "杠杆、数量、预估强平价与 OKX 当前下单页面一致",
}
NUMBER_LABELS = {
    "account_balance": "实际交易账户资金（U）",
    "entry_price": "计划开仓价",
    "planned_exit_price": "原计划退出价",
    "take_profit_price": "计划止盈价",
    "leverage": "杠杆倍数",
    "quantity": "下单数量（按 OKX 显示单位）",
    "liquidation_price": "OKX 预估强平价（执行退出价）",
}
TEXT_LABELS = {"symbol": "交易品种", "timeframe": "参考周期", "exit_reason": "退出依据"}
DIRECTIONS = ("做多", "做空")
TRADE_TYPES = ("价格行为", "趋势交易")


@dataclass(frozen=True)
class ChecklistResult:
    passed: bool
    errors: tuple[str, ...]
    reward_distance: Decimal | None = None
    risk_distance: Decimal | None = None
    reward_risk_ratio: Decimal | None = None
    exit_deviation: Decimal | None = None


def evaluate_checklist(draft: dict[str, Any]) -> ChecklistResult:
    errors: list[str] = []
    numbers: dict[str, Decimal] = {}
    for key, label in NUMBER_LABELS.items():
        raw = draft.get(key)
        try:
            if not isinstance(raw, str) or len(raw) > 64:
                raise ValueError
            value = Decimal(raw.strip())
            if not value.is_finite() or not Decimal("1e-18") <= value <= Decimal("1e18"):
                raise ValueError
            numbers[key] = value
        except (InvalidOperation, ValueError):
            errors.append(f"请填写有效的{label}（大于 0，范围 1e-18 至 1e18）。")
    for key, label in TEXT_LABELS.items():
        raw = draft.get(key)
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"请填写{label}。")
    if draft.get("direction") not in DIRECTIONS:
        errors.append("请选择做多或做空。")
    if draft.get("trade_type") not in TRADE_TYPES:
        errors.append("请选择价格行为或趋势交易。")
    balance = numbers.get("account_balance")
    if balance is not None and balance != Decimal("500"):
        errors.append(f"账户资金为 {draft['account_balance']}U，与固定投入 500U 不符。")
    errors.extend(f"尚未确认：{label}。" for key, label in CHECK_LABELS.items() if draft.get(key) is not True)
    prices = ("entry_price", "liquidation_price", "planned_exit_price", "take_profit_price")
    if not all(key in numbers for key in prices):
        return ChecklistResult(False, tuple(errors))
    entry, exit_price, planned_exit, target = (numbers[key] for key in prices)
    if draft.get("direction") == "做多":
        if not exit_price < entry < target or not planned_exit < entry:
            errors.append("做多价格顺序错误：退出价必须低于开仓价，止盈价必须高于开仓价。")
        if exit_price > planned_exit:
            errors.append("做多的预估强平价高于原计划退出价，会提前退出。")
    elif draft.get("direction") == "做空":
        if not target < entry < exit_price or not entry < planned_exit:
            errors.append("做空价格顺序错误：止盈价必须低于开仓价，退出价必须高于开仓价。")
        if exit_price < planned_exit:
            errors.append("做空的预估强平价低于原计划退出价，会提前退出。")
    # More than enough precision for the bounded, 64-character decimal inputs.
    with localcontext() as context:
        context.prec = 128
        reward = abs(target - entry)
        risk = abs(exit_price - entry)
        ratio = reward / risk if risk else None
        if ratio is None or reward <= Decimal("1.5") * risk:
            errors.append("计划盈亏比未满足严格大于 1.5。")
        return ChecklistResult(not errors, tuple(errors), reward, risk, ratio, exit_price - planned_exit)


def load_checklist_draft(root: Path) -> dict[str, Any]:
    path = root / "data" / "crypto" / "trade_checklist" / "draft.json"
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError("草稿文件过大。")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("草稿版本无效。")
        return _draft_fields(payload.get("draft"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("检查草稿损坏或格式不兼容，请检查文件或开始新一笔检查。") from exc


def _draft_fields(draft: Any) -> dict[str, Any]:
    if not isinstance(draft, dict):
        raise ValueError("草稿必须为对象。")
    cleaned: dict[str, Any] = {}
    for key in (*NUMBER_LABELS, *TEXT_LABELS, "direction", "trade_type", *CHECK_LABELS):
        if key not in draft:
            continue
        value = draft[key]
        if key in CHECK_LABELS:
            if type(value) is not bool:
                raise ValueError("确认项格式无效。")
        elif not isinstance(value, str) or len(value) > 2000:
            raise ValueError("输入字段格式无效。")
        cleaned[key] = value
    return cleaned


def save_checklist_draft(root: Path, draft: dict[str, Any]) -> None:
    cleaned = _draft_fields(draft)
    path = root / "data" / "crypto" / "trade_checklist" / "draft.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "draft": cleaned}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
