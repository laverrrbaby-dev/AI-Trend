"""Manual pre-entry checklist UI, separate from market-alert rendering."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import streamlit as st

from .trade_checklist import (
    CHECK_LABELS,
    DIRECTIONS,
    NUMBER_LABELS,
    TEXT_LABELS,
    TRADE_TYPES,
    evaluate_checklist,
    load_checklist_draft,
    save_checklist_draft,
)

PREFIX = "trade-checklist-"
FIELDS = (*NUMBER_LABELS, *TEXT_LABELS, "direction", "trade_type", *CHECK_LABELS)


def _invalidate_checks() -> None:
    for field in CHECK_LABELS:
        st.session_state[PREFIX + field] = False


def _retry_save() -> None:
    # Retry the write even when a failed reset preserved an unchanged draft.
    st.session_state[PREFIX + "saved"] = None


def _reset(root: Path) -> None:
    try:
        save_checklist_draft(root, {})
    except (OSError, ValueError) as exc:
        st.session_state[PREFIX + "save-error"] = f"新一笔检查未能保存，原输入已保留：{exc}"
        return
    for field in FIELDS:
        st.session_state[PREFIX + field] = _initial_value(field, {})
    st.session_state[PREFIX + "draft"] = {}
    st.session_state[PREFIX + "saved"] = {}
    st.session_state[PREFIX + "load-error"] = ""
    st.session_state[PREFIX + "save-error"] = ""


def _initial_value(field: str, draft: dict) -> str | bool | None:
    if field in CHECK_LABELS:
        return draft.get(field) is True
    value = draft.get(field, "")
    if field == "direction":
        return value if value in DIRECTIONS else None
    if field == "trade_type":
        return value if value in TRADE_TYPES else None
    return value


def _text_field(field: str) -> None:
    label = {**NUMBER_LABELS, **TEXT_LABELS}[field]
    st.text_input(label, key=PREFIX + field, max_chars=64 if field in NUMBER_LABELS else 120,
                  on_change=_invalidate_checks)


def _number_display(value: Decimal | None) -> str:
    return format(value, "f") if value is not None else "—"


def render_trade_checklist(root: Path) -> None:
    st.title("交易前检查")
    st.caption("OKX · 全仓 · 每笔固定 500U · 初始退出采用强平机制")
    st.info("先完成检查，再到 OKX 下单。预估强平价是检查时的预估值，不代表保证成交价格。")
    if PREFIX + "draft" not in st.session_state:
        try:
            restored = load_checklist_draft(root)
            st.session_state[PREFIX + "saved"] = restored if restored else None
            st.session_state[PREFIX + "load-error"] = ""
        except (OSError, ValueError) as exc:
            restored = {}
            st.session_state[PREFIX + "saved"] = None
            st.session_state[PREFIX + "load-error"] = str(exc)
        st.session_state[PREFIX + "draft"] = restored
        st.session_state[PREFIX + "save-error"] = ""
    for field in FIELDS:
        key = PREFIX + field
        if key not in st.session_state:
            st.session_state[key] = _initial_value(field, st.session_state[PREFIX + "draft"])
    st.button("新一笔检查", key=PREFIX + "reset", on_click=_reset, args=(root,))
    if st.session_state[PREFIX + "load-error"]:
        st.error(st.session_state[PREFIX + "load-error"])
        st.caption("原草稿未被覆盖；处理文件后刷新页面，或点击“新一笔检查”重置。")

    st.subheader("1. 核对账户")
    _text_field("account_balance")
    for field, label in CHECK_LABELS.items():
        if field != "order_parameters_checked":
            st.checkbox(label, key=PREFIX + field)

    st.subheader("2. 写下交易计划")
    left, right = st.columns(2)
    with left:
        _text_field("symbol")
        st.selectbox("交易类型", TRADE_TYPES, index=None, placeholder="请选择交易类型", key=PREFIX + "trade_type", on_change=_invalidate_checks)
        _text_field("timeframe")
    with right:
        st.selectbox("交易方向", DIRECTIONS, index=None, placeholder="请选择做多或做空", key=PREFIX + "direction", on_change=_invalidate_checks)
        _text_field("entry_price")
        _text_field("planned_exit_price")
        _text_field("take_profit_price")
    st.text_area("退出依据", key=PREFIX + "exit_reason", max_chars=2000,
                 on_change=_invalidate_checks,
                 placeholder="价格行为：注明参考日期、时区和高低点；趋势交易：注明周期、结构和失效价格。")

    st.subheader("3. 核对实际下单参数")
    left, right = st.columns(2)
    with left:
        _text_field("leverage")
        _text_field("quantity")
    with right:
        _text_field("liquidation_price")
        st.caption("做多：执行退出价 ≤ 原计划退出价；做空：执行退出价 ≥ 原计划退出价。")
    st.checkbox(CHECK_LABELS["order_parameters_checked"], key=PREFIX + "order_parameters_checked")

    draft = {field: st.session_state[PREFIX + field] for field in FIELDS}
    draft = {key: "" if value is None else value for key, value in draft.items()}
    st.session_state[PREFIX + "draft"] = draft
    if not st.session_state[PREFIX + "load-error"] and draft != st.session_state[PREFIX + "saved"]:
        try:
            save_checklist_draft(root, draft)
            st.session_state[PREFIX + "saved"] = draft
            st.session_state[PREFIX + "save-error"] = ""
        except (OSError, ValueError) as exc:
            st.session_state[PREFIX + "save-error"] = f"草稿保存失败，当前输入仍保留在页面：{exc}"
    save_error = st.session_state[PREFIX + "save-error"]
    if save_error:
        st.error(save_error)
        st.button("重试保存", key=PREFIX + "retry", on_click=_retry_save)
    elif not st.session_state[PREFIX + "load-error"]:
        st.caption("当前草稿已自动保存到本机；修改输入后须重新确认。")
    result = evaluate_checklist(draft)
    st.subheader("4. 最终检查结果")
    original, actual, deviation = st.columns(3)
    original.metric("原计划退出价", draft["planned_exit_price"] or "—")
    actual.metric("执行退出价（OKX 预估）", draft["liquidation_price"] or "—")
    deviation.metric("退出价偏差（执行 − 原计划）", _number_display(result.exit_deviation))
    reward, risk, ratio = st.columns(3)
    reward.metric("盈利距离", _number_display(result.reward_distance))
    risk.metric("亏损距离", _number_display(result.risk_distance))
    ratio.metric("计划盈亏比（须 > 1.5）", f"{result.reward_risk_ratio:.4f}" if result.reward_risk_ratio is not None else "—")
    st.caption("按价格距离计算，未扣手续费及资金费。门槛按未舍入值判断。")
    if result.passed and not save_error and not st.session_state[PREFIX + "load-error"]:
        st.success("检查通过")
    else:
        st.warning("检查未通过")
        for error in result.errors:
            st.write(f"• {error}")
    st.caption("检查结果仅基于手动填写内容，不代表系统已核实 OKX 账户，也不代表交易一定盈利。")
