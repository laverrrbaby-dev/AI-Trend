"""Manual trade entry and record management; never sends orders to OKX."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests
import streamlit as st

from .crypto_market_data import JsonFetcher
from .trade_checklist import CHECK_LABELS, NUMBER_LABELS, TEXT_LABELS
from .trade_record_market import BEIJING, StopMarket, fetch_stop_market, suggest_stop
from .trade_records import TradeRecord, TradeRecordStore


def _execution_fields(prefix: str, values: dict[str, str]) -> dict[str, str]:
    labels = {"instrument_id": "实际 OKX 交易代码", "entry_price": "实际成交价",
              "quantity": "实际数量（按 OKX 显示单位）", "opened_at": "开仓时间（含时区）"}
    result = {}
    for field, label in labels.items():
        result[field] = st.text_input(label, value=values.get(field, ""), key=prefix + field, max_chars=64)
    st.caption("交易代码须与实际持仓一致，例如 BTC-USDT-SWAP。时间使用东八区，例如 2026-10-17T10:30:00+08:00。")
    return result


def render_trade_save(
    root: Path,
    draft: dict[str, Any],
    *,
    on_success: Callable[[], None] | None = None,
) -> None:
    st.subheader("5. 实际成交后保存交易")
    store = TradeRecordStore(root)
    try:
        active = any(record.status == "active" for record in store.list_records())
    except (OSError, ValueError) as exc:
        st.error(f"交易记录不可用，暂不能保存新交易：{exc}")
        return
    if active:
        st.info("已有一笔进行中交易，请在“交易记录”中管理；结束或删除后才能保存新交易。")
    with st.form("trade-create"):
        actual = _execution_fields("trade-create-", {
            "instrument_id": draft["symbol"].strip().upper(), "entry_price": draft["entry_price"],
            "quantity": draft["quantity"], "opened_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        })
        confirmed = st.checkbox("我已实际成交，并已核对成交价、数量和开仓时间", key="trade-create-confirmed")
        submitted = st.form_submit_button("保存为进行中交易", disabled=active)
    if submitted:
        try:
            store.create(draft, actual, confirmed=confirmed)
        except (OSError, ValueError) as exc:
            st.error(f"交易未保存，输入已保留：{exc}")
        else:
            st.session_state["trade-record-notice"] = "交易已保存，请到“交易记录”查看。"
            if on_success is not None:
                on_success()
            st.rerun()


def _commit(action: Callable[[], object], *, pending_key: str | None = None, callback: bool = False) -> None:
    try:
        action()
    except (OSError, ValueError) as exc:
        message = f"未能保存，输入已保留：{exc}"
        if callback:
            st.session_state["trade-record-error"] = message
        else:
            st.error(message)
    else:
        if pending_key:
            st.session_state.pop(pending_key, None)
        st.session_state["trade-record-notice"] = "交易记录已更新。"
        st.session_state["trade-records-dirty"] = True
        if not callback:
            st.rerun()


def _outcome_fields(prefix: str, values: dict[str, str]) -> dict[str, str]:
    result = {}
    for field, label in {"closed_at": "平仓时间（含时区）", "pnl": "实际盈亏（U，可为负数）",
                         "reason": "退出原因"}.items():
        result[field] = st.text_input(label, value=values.get(field, ""), key=prefix + field, max_chars=64 if field != "reason" else 2000)
    result["notes"] = st.text_area("备注（选填）", value=values.get("notes", ""), key=prefix + "notes", max_chars=2000)
    st.caption("实际盈亏以交易所结果为准，平仓时间须包含时区。")
    return result


def _show_execution(actual: dict[str, str]) -> None:
    for field, label in {"instrument_id": "OKX 交易代码", "entry_price": "实际成交价",
                         "quantity": "实际数量", "opened_at": "开仓时间"}.items():
        st.write(f"{label}：{actual[field]}")


def _show_outcome(result: dict[str, str]) -> None:
    for field, label in {"closed_at": "平仓时间", "pnl": "实际盈亏（U）", "reason": "退出原因", "notes": "备注"}.items():
        st.write(f"{label}：{result[field] or '—'}")


def _show_suggestion(record: TradeRecord, root: Path, fetch_json: JsonFetcher | None, refresh: bool) -> None:
    cache_key = f"trade-record-market-{root}-{record.id}-{record.execution['instrument_id']}"
    if refresh:
        st.session_state.pop(cache_key, None)
    if cache_key not in st.session_state:
        try:
            with st.spinner("获取东八区日 K 与最新成交价…"):
                market = fetch_stop_market(record.execution["instrument_id"], fetch_json=fetch_json)
            st.session_state[cache_key] = (market, "")
        except (OSError, ValueError, requests.RequestException) as exc:
            st.session_state[cache_key] = (None, str(exc))
    market, error = st.session_state[cache_key]
    if error:
        st.warning(f"移动止损建议不可用：{error}")
        st.caption("可继续手动管理记录；重新进入页面或点击“重新加载记录与行情”再获取。")
        return
    assert isinstance(market, StopMarket)
    st.caption(f"OKX {market.instrument_id} · 最新成交价 {market.current_price} · 行情时间 {market.ticker_at.isoformat(timespec='seconds')}")
    close = market.candle_open + timedelta(days=1)
    st.caption(f"参考日 K：{market.candle_open.date()} · 收盘 {close.isoformat(timespec='minutes')} · 东八区")
    if (datetime.now(BEIJING) - market.ticker_at).total_seconds() > 120 or close.date() != datetime.now(BEIJING).date():
        st.warning("移动止损建议不可用：页面中的行情已过期，请重新加载。")
        return
    suggestion = suggest_stop(record, market)
    if suggestion.action == "crossed":
        st.warning(f"当前价格已触及或越过参考位置 {suggestion.price}，请核对持仓。")
    elif suggestion.action == "keep":
        st.info("保持当前止损：前一日高低点没有提供更紧的位置。")
    else:
        st.info(f"建议将止损收紧至 {suggestion.price}。请在 OKX 手动执行后，再更新当前止损记录。")
    st.caption("建议不会自动执行，也不会自动结束交易；最新成交价仅用于核对参考位置。")


def _preview_result(record: TradeRecord, prefix: str, historical: bool) -> None:
    actual = ({field: st.session_state[prefix + field] for field in record.execution}
              if historical else record.execution)
    result = {field: st.session_state[prefix + field] for field in ("closed_at", "pnl", "reason", "notes")}
    st.session_state[f"trade-record-pending-{record.id}"] = {"revision": record.revision, "execution": actual, "result": result}
    st.session_state[prefix + "result-draft"] = {"execution": actual, "result": result}


def _back_to_edit(pending_key: str) -> None:
    st.session_state.pop(pending_key, None)


def _edit_result(store: TradeRecordStore, record: TradeRecord, prefix: str, *, historical: bool) -> None:
    pending_key = f"trade-record-pending-{record.id}"
    draft_key = prefix + "result-draft"
    pending = st.session_state.get(pending_key)
    if pending and pending["revision"] != record.revision:
        st.session_state.pop(pending_key, None)
        st.warning("记录已有新修改，旧确认已失效，请重新核对。")
        pending = None
    if pending:
        st.subheader("请再次核对历史修改" if historical else "请核对平仓结果")
        _show_execution(pending["execution"])
        _show_outcome(pending["result"])
        confirm = "确认保存历史修改" if historical else "确认结束交易"
        if historical:
            commit = lambda: store.update_closed(record.id, pending["revision"], pending["execution"], pending["result"], confirmed=True)
        else:
            commit = lambda: store.close(record.id, pending["revision"], pending["result"], confirmed=True)
        st.button(confirm, key=prefix + "confirm", on_click=_commit, args=(commit,),
                  kwargs={"pending_key": pending_key, "callback": True})
        st.button("返回修改", key=prefix + "back", on_click=_back_to_edit, args=(pending_key,))
        return
    editing = st.session_state.get(draft_key, {})
    with st.form(prefix + "result-form"):
        if historical:
            _execution_fields(prefix, editing.get("execution", record.execution))
        _outcome_fields(prefix, editing.get("result", record.result or {"closed_at": datetime.now(BEIJING).isoformat(timespec="seconds")}))
        st.form_submit_button("预览历史修改" if historical else "核对平仓信息",
                              on_click=_preview_result, args=(record, prefix, historical))


def _record_actions(store: TradeRecordStore, record: TradeRecord) -> None:
    prefix = f"trade-record-{record.id}-{record.revision}-"
    active = record.status == "active"
    options = ["查看", "修改成交信息", "更新止损", "结束交易", "删除交易"] if active else ["查看", "修改历史", "删除交易"]
    action = st.selectbox("操作", options, key=prefix + "action")
    if action == "修改成交信息":
        with st.form(prefix + "execution-form"):
            actual = _execution_fields(prefix, record.execution)
            submitted = st.form_submit_button("保存成交信息")
        if submitted:
            _commit(lambda: store.update_execution(record.id, record.revision, actual))
    elif action == "更新止损":
        with st.form(prefix + "stop-form"):
            stop = st.text_input("已在 OKX 设置的新止损价", key=prefix + "stop", max_chars=64)
            confirmed = st.checkbox("我已在 OKX 手动设置此止损价", key=prefix + "stop-confirmed")
            submitted = st.form_submit_button("保存当前止损")
        if submitted:
            if confirmed:
                _commit(lambda: store.update_stop(record.id, record.revision, stop))
            else:
                st.error("请先确认已经在 OKX 设置此止损价。")
    elif action in ("结束交易", "修改历史"):
        _edit_result(store, record, prefix, historical=not active)
    elif action == "删除交易":
        st.warning("删除后这笔交易记录无法从页面恢复。")
        confirmed = st.checkbox("我确认删除这笔交易", key=prefix + "delete-confirmed")
        st.button("永久删除记录", key=prefix + "delete", disabled=not confirmed,
                  on_click=_commit, args=(lambda: store.delete(record.id, record.revision),), kwargs={"callback": True})


def render_trade_records(root: Path, *, fetch_json: JsonFetcher | None = None, refresh: bool = False) -> None:
    st.title("交易记录")
    st.caption("OKX · 手动记录 · 同时最多一笔进行中交易 · 历史暂不做统计分析")
    notice = st.session_state.pop("trade-record-notice", "")
    if notice:
        st.success(notice)
    error = st.session_state.pop("trade-record-error", "")
    if error:
        st.error(error)
    refresh = st.button("重新加载记录与行情", key="trade-records-reload") or refresh
    section = st.radio("交易状态", ["进行中", "已结束"], horizontal=True, key="trade-records-status")
    refresh = st.session_state.get("trade-records-last-status") != section or refresh
    st.session_state["trade-records-last-status"] = section
    reload_records = st.session_state.pop("trade-records-dirty", False) or refresh
    store = TradeRecordStore(root)
    records_key = f"trade-records-view-{root}"
    try:
        if reload_records or records_key not in st.session_state:
            st.session_state[records_key] = store.list_records()
        records = st.session_state[records_key]
    except (OSError, ValueError) as exc:
        st.error(f"无法加载交易记录，原文件未被覆盖：{exc}")
        return
    visible = [record for record in records if record.status == ("active" if section == "进行中" else "closed")]
    if not visible:
        st.info("暂无进行中交易。实际成交后，可从“交易前检查”保存。" if section == "进行中" else "暂无已结束交易。")
    for record in visible:
        st.subheader(f"{record.execution['instrument_id']} · {record.snapshot['direction']}")
        _show_execution(record.execution)
        st.write(f"{'当前已设置止损价' if record.stop_price else '初始退出基准（预估强平价）'}：{record.exit_baseline}")
        if record.result:
            _show_outcome(record.result)
        with st.expander("原始检查快照（保存后不覆盖）"):
            labels = {**NUMBER_LABELS, **TEXT_LABELS, **CHECK_LABELS, "direction": "交易方向", "trade_type": "交易类型"}
            for field, label in labels.items():
                value = record.snapshot.get(field, "—")
                if type(value) is bool:
                    value = "已确认" if value else "未确认"
                st.write(f"{label}：{value}")
        if record.status == "active":
            _show_suggestion(record, root, fetch_json, refresh)
        _record_actions(store, record)
        st.divider()
