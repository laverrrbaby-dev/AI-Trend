from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import MutableMapping
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from trend_radar.ui_data import (  # noqa: E402
    ASIA_SHANGHAI,
    build_changes,
    build_price_history,
    build_score_breakdown,
    get_available_dates,
    load_metadata,
    load_snapshot,
    resolve_default_date,
)
from trend_radar.refresh import (  # noqa: E402
    REFRESH_FAILED,
    REFRESH_IDLE,
    REFRESH_RUNNING,
    REFRESH_SKIPPED,
    REFRESH_SUCCEEDED,
    problem_snapshot_summary,
    read_refresh_status,
    refresh_latest_data,
    refresh_task_is_active,
    retry_problem_snapshots,
    retry_selected_date,
    resolve_refresh_target_date,
)
from trend_radar.crypto import (  # noqa: E402
    CRYPTO_REFRESH_FAILED,
    CRYPTO_REFRESH_IDLE,
    CRYPTO_REFRESH_RUNNING,
    CRYPTO_REFRESH_SKIPPED,
    CRYPTO_REFRESH_SUCCEEDED,
    CRYPTO_TOP_TURNOVER_LIMIT,
    CRYPTO_WINDOW_DAYS,
    get_available_crypto_dates,
    latest_crypto_cache_date,
    load_crypto_metadata,
    load_crypto_snapshot,
    read_crypto_refresh_status,
    refresh_crypto_turnover,
    resolve_crypto_target_date,
)
from trend_radar.us_stocks import (  # noqa: E402
    US_REFRESH_FAILED,
    US_REFRESH_IDLE,
    US_REFRESH_RUNNING,
    US_REFRESH_SKIPPED,
    US_REFRESH_SUCCEEDED,
    US_TOP_TURNOVER_LIMIT,
    get_available_us_stock_dates,
    latest_us_stock_cache_date,
    load_us_stock_metadata,
    load_us_stock_snapshot,
    read_us_stock_refresh_status,
    refresh_us_stock_turnover,
    resolve_us_stock_target_date,
)
from trend_radar.us_stock_anomalies import serialize_anomaly_details  # noqa: E402
from trend_radar.us_stock_news import load_cached_news, load_or_fetch_news  # noqa: E402
from trend_radar.crypto_alert_refresh import (  # noqa: E402
    read_crypto_alert_refresh_status,
    start_crypto_alert_refresh_once,
)
from trend_radar.crypto_alert_store import (  # noqa: E402
    load_crypto_dashboard,
    mark_crypto_symbol_read,
)
from trend_radar.crypto_alert_config import (  # noqa: E402
    activate_crypto_alert_preset,
    list_crypto_alert_presets,
    load_crypto_alert_config,
    save_crypto_alert_preset,
)
from trend_radar.crypto_indicators import calculate_indicators  # noqa: E402
from trend_radar.crypto_market_data import load_crypto_history  # noqa: E402


STATUS_ORDER = ["强趋势", "趋势向上", "转强观察", "震荡观察", "弱观察", "数据不足"]
MARKET_OPTIONS = ["美股", "A股ETF", "币圈现货"]
DATE_SELECT_KEY = "selected_data_date"
PENDING_DATE_SELECT_KEY = "pending_selected_data_date"
MARKET_SELECT_KEY = "selected_market_tag"
US_STOCK_DATE_SELECT_KEY = "selected_us_stock_date"
PENDING_US_STOCK_DATE_SELECT_KEY = "pending_selected_us_stock_date"
CRYPTO_DATE_SELECT_KEY = "selected_crypto_date"
PENDING_CRYPTO_DATE_SELECT_KEY = "pending_selected_crypto_date"
DETAIL_CODE_STATE_KEY = "detail_selected_code"
TOP10_SELECTION_STATE_KEY = "top10_selection_signature"
RANKING_SELECTION_STATE_KEY = "ranking_selection_signature"
SORT_FIELDS = {
    "主线得分": ("主线得分", False),
    "成交额Top30次数": ("成交额Top30次数", False),
    "成交额": ("成交额(亿)", False),
    "成交额排名": ("成交额排名", True),
    "20日涨幅": ("20日涨幅", False),
    "60日涨幅": ("60日涨幅", False),
    "120日涨幅": ("120日涨幅", False),
    "20日份额变化": ("20日份额变化", False),
}
TERM_HELP = {
    "数据状态": "当前页面使用的数据来源和质量状态。真实缓存表示读取本地真实数据；Mock原型表示使用静态样例数据。",
    "头部主线": "当前排行榜第 1 名 ETF，用来快速观察当日主线得分最高的方向。",
    "Top10均分": "当前 Top10 观察池的主线得分平均值，用来衡量头部方向整体强度。",
    "强趋势数量": "完整榜单中趋势状态为强趋势或趋势向上的 ETF 数量。",
    "新进Top10": "相对上一交易日，本交易日新进入 Top10 观察池的 ETF 数量。",
    "搜索": "按 ETF 名称或 6 位代码过滤完整排行榜。",
    "趋势状态": "按主线得分分档：>=88 强趋势，>=74 趋势向上，>=64 转强观察，>=52 震荡观察，低于 52 为弱观察；数据不足仅用于历史样本不足或 mock 状态展示。",
    "排序": "选择完整排行榜的排序字段；成交额排名按名次从小到大，其余字段默认从高到低。",
    "明细来源": "选择单 ETF 明细展示的对象。点击 Top10 或完整排行榜行会同步切换这里的选项。",
    "ETF明细": "选择要查看明细图表和评分拆解的 ETF。点击上方表格行也会同步切换。",
    "主线排名": "按主线得分和成交额排序后的榜单名次，数值越小排名越靠前。",
    "主线得分": "0-100 分，总分 = 成交额热度 45% + 份额流入 20% + 趋势强度 25% + 趋势结构 10%。",
    "成交额Top30次数": "近 20 个交易日内，该 ETF 当日成交额在全候选 A股权益 ETF 中排前 30 的天数；20/20 表示近 20 日每天都进入成交额 Top30。",
    "成交额Top30": "近 20 个交易日内进入成交额 Top30 的天数；右侧显示最新交易日成交额排名。",
    "成交额排名": "最新交易日成交额在全候选 A股权益 ETF 中的横向排名，数值越小成交额越靠前。",
    "20日涨幅": "最新收盘价相对 20 个交易日前收盘价的涨跌幅。",
    "60日涨幅": "最新收盘价相对 60 个交易日前收盘价的涨跌幅。",
    "120日涨幅": "最新收盘价相对 120 个交易日前收盘价的涨跌幅。",
    "5日份额变化": "ETF 最新份额相对 5 个交易日前的变化比例，用来观察短期份额流入或流出。",
    "20日份额变化": "ETF 最新份额相对 20 个交易日前的变化比例，用来观察中期份额流入或流出。",
    "成交额(亿)": "最新交易日成交额，单位为亿元。",
    "数据质量": "当前 ETF 行的数据完整性提示。完整仅表示历史行情、份额和关键字段来源可信；fallback 或来源未记录必须复核。",
    "数据置信度": "高表示关键数据来源可信；中表示存在份额、市值或K线兜底；低表示历史行情来自实时快照补点或来源无法确认。",
    "行情来源": "当前 ETF 历史行情的来源。实时行情补点和来源未记录都需要重新拉取确认。",
    "份额来源": "当前 ETF 份额历史来源。实时行情份额表示官方份额接口缺失时的兜底。",
    "数据问题": "当前 ETF 的具体数据风险和建议动作；无表示未发现低置信来源或关键字段兜底。",
    "份额变化": "展示 5 日和 20 日 ETF 份额变化比例，正值表示份额增加，负值表示份额减少。",
    "成交额热度": "由近 20 日 Top30 驻留、最新成交额排名、近 5 日相对近 20 日成交额放大共同组成。",
    "份额流入": "由近 5 日、近 20 日份额变化构成；价格上涨且份额增加会加分。",
    "趋势强度": "按 20/60/120 日涨幅加权，权重分别为 45%/35%/20%。",
    "趋势结构": "基于均线排列，MA20 > MA60 > MA120 得分最高，价格重新站上短中期均线视为修复。",
}

if hasattr(components, "declare_component"):
    US_STOCK_TABLE_COMPONENT = components.declare_component(
        "us_stock_interactive_table",
        path=str(ROOT / "src" / "trend_radar" / "components" / "us_stock_table"),
    )
else:
    def US_STOCK_TABLE_COMPONENT(**kwargs: object) -> None:
        return kwargs.get("default")  # type: ignore[return-value]

if not os.environ.get("OPENROUTER_API_KEY"):
    try:
        configured_openrouter_key = str(st.secrets.get("OPENROUTER_API_KEY") or "").strip()
    except Exception:  # noqa: BLE001 - local secrets are optional outside Streamlit runtime
        configured_openrouter_key = ""
    if configured_openrouter_key:
        os.environ["OPENROUTER_API_KEY"] = configured_openrouter_key


def main() -> None:
    st.set_page_config(
        page_title="A股ETF主线趋势雷达",
        page_icon=None,
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _apply_style()
    _install_code_copy_handler()
    _install_lifecycle_heartbeat()
    _start_crypto_background_work()

    market = _market_selector()
    if market == "美股":
        _render_us_stock_turnover_page()
    elif market == "币圈现货":
        _render_crypto_turnover_page()
    else:
        _render_a_share_etf_page()


def _start_crypto_background_work() -> bool:
    """Start the process-wide crypto check before the market selector renders."""
    try:
        if not load_crypto_alert_config(ROOT).startup_refresh:
            return False
        return start_crypto_alert_refresh_once(ROOT)
    except Exception:
        return False


def _crypto_market_label(status: dict[str, Any]) -> str:
    unread = int(status.get("unread_count", 0) or 0)
    return f"币圈现货 · {unread}" if unread else "币圈现货"


def _crypto_global_notice(status: dict[str, Any], session_state: MutableMapping[str, Any]) -> str | None:
    revision = str(status.get("finished_at") or status.get("started_at") or "")
    if not revision or session_state.get("crypto_notice_revision") == revision:
        return None
    session_state["crypto_notice_revision"] = revision
    if status.get("status") in {"partial", "failed"}:
        return "币圈现货数据部分需要复核"
    if int(status.get("unread_count", 0) or 0):
        return f"币圈现货有 {status['unread_count']} 项新的策略变化"
    return None


def _market_selector() -> str:
    crypto_status = read_crypto_alert_refresh_status(ROOT)
    crypto_status["unread_count"] = load_crypto_dashboard(ROOT).get("unread_count", 0)
    crypto_label = _crypto_market_label(crypto_status)
    options = ["美股", "A股ETF", crypto_label]
    if hasattr(st, "segmented_control"):
        selected = st.segmented_control("市场", options, default=options[0], key=MARKET_SELECT_KEY)
    else:
        selected = st.radio("市场", options, index=0, horizontal=True, key=MARKET_SELECT_KEY)
    return "币圈现货" if selected == crypto_label else str(selected or options[0])


def _render_a_share_etf_page() -> None:
    dates = get_available_dates()
    _apply_pending_selected_date(dates)
    refresh_target_date = resolve_refresh_target_date()
    refresh_started = _start_auto_refresh_once(refresh_target_date)
    default_date = resolve_default_date()
    selected_default = _ensure_selected_date(dates, default_date)
    default_index = dates.index(selected_default) if selected_default in dates else len(dates) - 1

    header_left, header_date, header_status = st.columns([1.7, 0.72, 0.82], vertical_alignment="center")
    with header_left:
        st.title("A股ETF主线趋势雷达")
        st.caption("主线发现系统 | 真实缓存优先 | 不含交易建议")
    with header_date:
        selected_date = st.selectbox("数据日期", dates, index=default_index, key=DATE_SELECT_KEY)
    with header_status:
        meta = load_metadata(selected_date)
        generated_at = meta.get("generated_at") or datetime.now(ASIA_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        source_label = "真实缓存" if meta.get("source_mode") == "real" else "Mock原型"
        quality_status = meta.get("data_quality", {}).get("status", "MOCK")
        st.metric("数据状态", f"{source_label} {quality_status}", f"生成 {generated_at}", help=TERM_HELP["数据状态"])

    _render_refresh_status(selected_date, force_poll=refresh_started)

    snapshot = load_snapshot(selected_date)
    changes = build_changes(selected_date)
    top10 = snapshot.head(10).copy()

    _render_summary_metrics(snapshot, changes)
    _render_data_quality(meta, snapshot)

    filter_cols = st.columns([0.86, 1.1, 0.78], vertical_alignment="bottom")
    with filter_cols[0]:
        query = st.text_input("搜索", placeholder="ETF名称或代码", label_visibility="visible", help=TERM_HELP["搜索"])
    with filter_cols[1]:
        status_filter = st.multiselect(
            "趋势状态",
            STATUS_ORDER,
            default=STATUS_ORDER[:4],
            help=TERM_HELP["趋势状态"],
        )
    with filter_cols[2]:
        sort_label = st.selectbox("排序", list(SORT_FIELDS.keys()), index=0, help=TERM_HELP["排序"])

    filtered = _filter_snapshot(snapshot, query, status_filter)
    filtered = _sort_snapshot_for_display(filtered, sort_label)

    left, right = st.columns([2.05, 1.0], gap="medium")
    with left:
        st.subheader("Top10观察池")
        top10_event = st.dataframe(
            _display_table(top10),
            width="stretch",
            hide_index=True,
            height=386,
            column_config=_table_columns(),
            on_select="rerun",
            selection_mode="single-row",
            key=f"top10-{selected_date}",
        )
    with right:
        _render_changes(changes)

    st.subheader("完整排行榜")
    ranking_event = st.dataframe(
        _display_table(filtered),
        width="stretch",
        hide_index=True,
        height=380,
        column_config=_table_columns(),
        on_select="rerun",
        selection_mode="single-row",
        key=f"ranking-{selected_date}",
    )

    detail_options = _detail_options(snapshot)
    selectbox_key = f"detail-select-{selected_date}"
    _resolve_selected_code(
        snapshot,
        top10,
        filtered,
        top10_event,
        ranking_event,
        selected_date,
        st.session_state,
        detail_options,
        selectbox_key,
    )
    selected_label = st.selectbox("ETF明细", list(detail_options.keys()), key=selectbox_key, help=TERM_HELP["ETF明细"])
    selected_code = detail_options[selected_label]
    st.session_state[DETAIL_CODE_STATE_KEY] = selected_code
    selected_row = snapshot[snapshot["代码"] == selected_code].iloc[0]
    _render_detail(selected_row, selected_date)


def _ensure_selected_date(dates: list[str], default_date: str) -> str:
    if not dates:
        return default_date
    selected = st.session_state.get(DATE_SELECT_KEY)
    if selected not in dates:
        selected = default_date if default_date in dates else dates[-1]
        st.session_state[DATE_SELECT_KEY] = selected
    return str(selected)


def _start_auto_refresh_once(target_date: str) -> bool:
    key = f"auto_refresh_started_{target_date}"
    status = read_refresh_status()
    needs_recovery = status.get("status") == REFRESH_RUNNING and not refresh_task_is_active()
    if st.session_state.get(key) and not needs_recovery:
        return False
    st.session_state[key] = True
    _start_refresh_thread(force=False)
    return True


def _start_refresh_thread(force: bool) -> None:
    def _safe_refresh() -> None:
        try:
            refresh_latest_data(force=force)
        except Exception:
            # Exception already recorded in refresh_status.json by refresh_latest_data
            pass

    # Check for existing refresh thread to avoid duplicates
    for thread in threading.enumerate():
        if thread.name == "trend-radar-refresh" and thread.is_alive():
            return

    thread = threading.Thread(
        target=_safe_refresh,
        name="trend-radar-refresh",
        daemon=True,
    )
    thread.start()


def _start_retry_thread() -> None:
    def _safe_retry() -> None:
        try:
            retry_problem_snapshots()
        except Exception:
            pass

    for thread in threading.enumerate():
        if thread.name == "trend-radar-retry" and thread.is_alive():
            return

    thread = threading.Thread(
        target=_safe_retry,
        name="trend-radar-retry",
        daemon=True,
    )
    thread.start()


def _start_retry_selected_thread(data_date: str) -> None:
    def _safe_retry_selected() -> None:
        try:
            retry_selected_date(data_date=data_date)
        except Exception:
            pass

    name = f"trend-radar-retry-{data_date}"
    for thread in threading.enumerate():
        if thread.name == name and thread.is_alive():
            return

    thread = threading.Thread(
        target=_safe_retry_selected,
        name=name,
        daemon=True,
    )
    thread.start()


def _start_us_stock_auto_refresh_once(target_date: str) -> bool:
    if target_date in get_available_us_stock_dates():
        return False
    key = f"us_stock_auto_refresh_started_{target_date}"
    if st.session_state.get(key):
        return read_us_stock_refresh_status().get("status") == US_REFRESH_RUNNING
    st.session_state[key] = True
    _start_us_stock_refresh_thread(force=False)
    return True


def _start_us_stock_refresh_thread(force: bool) -> None:
    thread = threading.Thread(
        target=refresh_us_stock_turnover,
        kwargs={"force": force},
        name="trend-radar-us-stock-refresh",
        daemon=True,
    )
    thread.start()


def _render_us_stock_turnover_page() -> None:
    target_date = resolve_us_stock_target_date()
    refresh_started = _start_us_stock_auto_refresh_once(target_date)
    available_dates = get_available_us_stock_dates()
    _apply_pending_us_stock_selected_date(available_dates)
    status = read_us_stock_refresh_status()
    default_display_date = _resolve_us_stock_display_date(target_date, available_dates, status)
    selected_default = _ensure_us_stock_selected_date(available_dates, default_display_date)
    date_options = available_dates if available_dates else [selected_default]
    default_index = date_options.index(selected_default) if selected_default in date_options else len(date_options) - 1
    display_date = selected_default
    snapshot = load_us_stock_snapshot(display_date) if display_date in available_dates else pd.DataFrame()
    metadata = load_us_stock_metadata(display_date) if display_date in available_dates else {}

    header_left, header_date, header_status = st.columns([1.5, 0.58, 0.9], vertical_alignment="center")
    with header_left:
        st.title(f"美股成交额Top{US_TOP_TURNOVER_LIMIT}")
        st.caption("TradingView 直接成交额 | 本地历史快照 | 不含交易建议")
    with header_date:
        display_date = st.selectbox("数据日期", date_options, index=default_index, key=US_STOCK_DATE_SELECT_KEY)
        snapshot = load_us_stock_snapshot(display_date) if display_date in available_dates else pd.DataFrame()
        metadata = load_us_stock_metadata(display_date) if display_date in available_dates else {}
    with header_status:
        source = str(metadata.get("source") or status.get("source") or "tradingview")
        source_label = "TradingView" if source == "tradingview" else source
        generated_at = str(metadata.get("generated_at") or status.get("generated_at") or "-")
        st.metric("数据状态", f"{source_label} {len(snapshot) or '-'}只", f"日期 {display_date} | 生成 {generated_at}")

    _render_us_stock_refresh_status(display_date, target_date, force_poll=refresh_started)

    if snapshot.empty:
        st.warning("暂无可展示的美股成交额缓存，正在尝试拉取最新数据。")
        return

    display_table = _display_us_stock_table(snapshot)
    expanded_key = f"us-stock-expanded-{display_date}"
    expanded_code = str(st.session_state.get(expanded_key) or "")
    details_html = ""
    news_loading = False
    if expanded_code:
        selected_snapshot = snapshot[snapshot["代码"].astype(str) == expanded_code]
        if not selected_snapshot.empty and str(selected_snapshot.iloc[0].get("异动等级") or "") == "强异动":
            selected_row = selected_snapshot.iloc[0]
            cached_news = load_cached_news(expanded_code, display_date, root=ROOT)
            if cached_news is None:
                news_loading = True
                _start_us_stock_news_fetch(selected_row, display_date)
            details_html = _build_us_stock_detail_html(selected_row, display_date, metadata, news=cached_news)
        else:
            expanded_code = ""
    interaction = US_STOCK_TABLE_COMPONENT(
        rows=_build_us_stock_component_rows(display_table),
        strong_codes=snapshot.loc[snapshot["异动等级"].astype(str) == "强异动", "代码"].astype(str).tolist()
        if "异动等级" in snapshot.columns
        else [],
        expanded_code=expanded_code,
        details_html=details_html,
        news_loading=news_loading,
        key=f"us-stock-table-{display_date}",
        default=None,
    )
    event_key = f"us-stock-event-{display_date}"
    event_id = interaction.get("eventId") if isinstance(interaction, dict) else None
    if event_id is not None and st.session_state.get(event_key) != event_id:
        st.session_state[event_key] = event_id
        next_code = _resolve_us_stock_interaction(interaction, expanded_code, snapshot)
    else:
        next_code = expanded_code
    if next_code != expanded_code:
        st.session_state[expanded_key] = next_code
        st.rerun()
    if isinstance(interaction, dict) and interaction.get("type") == "poll" and news_loading:
        poll_key = f"us-stock-news-poll-{display_date}"
        if event_id is not None and st.session_state.get(poll_key) != event_id:
            st.session_state[poll_key] = event_id
            st.rerun()


def _build_us_stock_component_rows(table: pd.DataFrame) -> list[dict[str, str]]:
    return [
        {column: "" if pd.isna(value) else str(value) for column, value in row.items()}
        for row in table.to_dict(orient="records")
    ]


def _resolve_us_stock_interaction(
    interaction: object,
    expanded_code: str,
    snapshot: pd.DataFrame,
) -> str:
    if not isinstance(interaction, dict) or interaction.get("type") != "toggle":
        return expanded_code
    code = str(interaction.get("code") or "")
    matching = snapshot[(snapshot["代码"].astype(str) == code) & (snapshot["异动等级"].astype(str) == "强异动")]
    if matching.empty:
        return expanded_code
    return "" if expanded_code == code else code


def _start_us_stock_news_fetch(row: pd.Series, data_date: str) -> None:
    code = str(row.get("代码") or "")
    if not code:
        return
    thread_name = f"trend-radar-us-news-{data_date}-{code}"
    if any(thread.name == thread_name and thread.is_alive() for thread in threading.enumerate()):
        return

    def fetch() -> None:
        load_or_fetch_news(code, str(row.get("名称") or code), data_date, root=ROOT)

    threading.Thread(target=fetch, name=thread_name, daemon=True).start()


def _resolve_us_stock_display_date(target_date: str, available_dates: list[str], status: dict[str, Any]) -> str:
    if target_date in available_dates:
        return target_date
    actual_date = str(status.get("actual_date") or "")
    if actual_date in available_dates:
        return actual_date
    latest_date = latest_us_stock_cache_date()
    return latest_date or target_date


def _ensure_us_stock_selected_date(dates: list[str], default_date: str) -> str:
    if not dates:
        return default_date
    selected = st.session_state.get(US_STOCK_DATE_SELECT_KEY)
    if selected not in dates:
        selected = default_date if default_date in dates else dates[-1]
        st.session_state[US_STOCK_DATE_SELECT_KEY] = selected
    return str(selected)


def _apply_pending_us_stock_selected_date(dates: list[str]) -> None:
    pending = st.session_state.pop(PENDING_US_STOCK_DATE_SELECT_KEY, None)
    if pending in dates:
        st.session_state[US_STOCK_DATE_SELECT_KEY] = pending


def _render_us_stock_refresh_status(display_date: str, target_date: str, force_poll: bool = False) -> None:
    should_poll = force_poll or read_us_stock_refresh_status().get("status") == US_REFRESH_RUNNING
    if hasattr(st, "fragment") and should_poll:

        @st.fragment(run_every="5s")
        def _us_stock_refresh_fragment() -> None:
            _render_us_stock_refresh_status_panel(display_date, target_date)

        _us_stock_refresh_fragment()
        return

    _render_us_stock_refresh_status_panel(display_date, target_date)


def _render_us_stock_refresh_status_panel(display_date: str, target_date: str) -> None:
    status = read_us_stock_refresh_status()
    status_name = str(status.get("status") or US_REFRESH_IDLE)
    message = _us_stock_status_message(status, display_date, target_date)
    notice_col, latest_col, check_col = st.columns([1.48, 0.34, 0.34], vertical_alignment="center")
    with notice_col:
        if status_name == US_REFRESH_SUCCEEDED:
            st.success(message)
        elif status_name == US_REFRESH_FAILED:
            if display_date and display_date != target_date:
                st.warning(message)
            else:
                st.error(message)
        elif status_name in {US_REFRESH_RUNNING, US_REFRESH_SKIPPED}:
            st.info(message)
        else:
            st.info(message)
        updated_at = str(status.get("updated_at") or "").strip()
        if updated_at:
            st.caption(f"最近更新 {updated_at}")
    with latest_col:
        if target_date in get_available_us_stock_dates() and display_date != target_date:
            if st.button("刷新到最新数据", key="refresh-us-stock-to-latest", use_container_width=True):
                st.session_state[PENDING_US_STOCK_DATE_SELECT_KEY] = target_date
                st.rerun()
    with check_col:
        if st.button(
            "检查最新数据",
            key="check-us-stock-latest-data",
            disabled=status_name == US_REFRESH_RUNNING,
            use_container_width=True,
        ):
            _start_us_stock_refresh_thread(force=True)
            st.rerun()


def _us_stock_status_message(status: dict[str, Any], display_date: str, target_date: str) -> str:
    status_name = str(status.get("status") or US_REFRESH_IDLE)
    if status_name == US_REFRESH_RUNNING:
        return f"正在更新美股成交额 {target_date} 数据..."
    if status_name == US_REFRESH_SUCCEEDED:
        return str(status.get("message") or f"已生成 {target_date} 美股成交额 Top{US_TOP_TURNOVER_LIMIT}")
    if status_name == US_REFRESH_FAILED:
        base = str(status.get("message") or f"{target_date} 美股成交额更新失败")
        error = str(status.get("error") or "").strip()
        suffix = f"：{_short_error(error)}" if error else ""
        return f"{base}{suffix}"
    if status_name == US_REFRESH_SKIPPED:
        return str(status.get("message") or f"当前已是最新美股成交额 {target_date} 数据")
    if display_date and display_date != target_date:
        return f"当前展示 {display_date} 美股成交额缓存，目标日期 {target_date} 尚未生成"
    return "等待检查美股最新数据"


def _start_crypto_auto_refresh_once(target_date: str) -> bool:
    if target_date in get_available_crypto_dates():
        return False
    key = f"crypto_auto_refresh_started_{target_date}"
    if st.session_state.get(key):
        return read_crypto_refresh_status().get("status") == CRYPTO_REFRESH_RUNNING
    st.session_state[key] = True
    _start_crypto_refresh_thread(force=False)
    return True


def _start_crypto_refresh_thread(force: bool) -> None:
    thread = threading.Thread(
        target=refresh_crypto_turnover,
        kwargs={"force": force},
        name="trend-radar-crypto-refresh",
        daemon=True,
    )
    thread.start()


def _render_crypto_turnover_page() -> None:
    _render_crypto_alert_dashboard()
    _render_legacy_crypto_turnover()


def _render_crypto_alert_dashboard() -> None:
    status = read_crypto_alert_refresh_status(ROOT)
    notice = _crypto_global_notice(status, st.session_state)
    if notice:
        st.info(notice)
    st.title("币圈现货关注提醒")
    st.caption("日线 · UTC+8 · 仅提示策略变化，不构成交易指令")
    _render_crypto_alert_settings()
    if status.get("status") == "running":
        st.caption(f"后台检查中：{status.get('completed', 0)}/{status.get('total', 5)}")
    dashboard = load_crypto_dashboard(ROOT)
    changed_only = st.checkbox("仅看有变化", key="crypto-alert-changed-only")
    table = _crypto_dashboard_table(dashboard, changed_only=changed_only)
    st.dataframe(table, use_container_width=True, hide_index=True)
    if st.button("重新检查", key="crypto-alert-refresh"):
        start_crypto_alert_refresh_once(ROOT, force=True)
    for state in dashboard.get("symbols", []):
        if changed_only and state.get("status") != "有变化":
            continue
        symbol = str(state.get("symbol", ""))
        with st.expander(f"{symbol} · {state.get('trend', '震荡/未确认')}"):
            st.write(state.get("latest_change", "暂无新的策略变化"))
            st.caption(f"数据截至 {state.get('data_date', '-')} · {state.get('quality', '正常')}")
            unread_event_ids = crypto_unread_event_ids(dashboard, symbol)
            if unread_event_ids and st.button("标记该币种已读", key=f"crypto-alert-read-{symbol}"):
                mark_crypto_symbol_read(ROOT, symbol, unread_event_ids)
                st.success("该币种的新提醒已标记为已读。")
            history = load_crypto_history(ROOT, symbol)
            if not history.empty:
                overlays = {
                    "sma120": st.checkbox("显示 MA120", value=True, key=f"crypto-alert-ma120-{symbol}"),
                    "vegas": st.checkbox("显示 Vegas 通道", value=True, key=f"crypto-alert-vegas-{symbol}"),
                }
                st.plotly_chart(_crypto_candlestick_chart(calculate_indicators(history, load_crypto_alert_config(ROOT)), state, overlays), use_container_width=True)


def _render_crypto_alert_settings() -> None:
    labels = {
        "dow": "道氏结构",
        "price_action": "价格行为",
        "divergence": "背离",
        "td": "TD9 / TD13",
        "ma120": "MA120",
        "vegas": "Vegas 通道",
    }
    config = load_crypto_alert_config(ROOT)
    with st.expander("提醒设置", expanded=False):
        presets = list_crypto_alert_presets(ROOT)
        selected = st.selectbox("策略预设", presets, index=presets.index(config.preset_id))
        if selected != config.preset_id and st.button("切换预设", key="crypto-alert-activate-preset"):
            activate_crypto_alert_preset(ROOT, selected)
            st.success(f"已切换为预设：{selected}；下一根完整日线起生效。")
        enabled = st.multiselect(
            "启用的提醒类型",
            options=list(labels),
            default=list(config.enabled_alerts),
            format_func=lambda key: labels[key],
            key=f"crypto-alert-enabled-{config.preset_id}",
        )
        name = st.text_input("新预设名称", key="crypto-alert-preset-name")
        if st.button("保存为新预设并启用", key="crypto-alert-save-preset"):
            if not name.strip():
                st.warning("请填写新预设名称。")
            else:
                save_crypto_alert_preset(ROOT, name.strip(), {"enabled_alerts": enabled})
                activate_crypto_alert_preset(ROOT, name.strip())
                st.success(f"已保存并启用预设：{name.strip()}；下一根完整日线起生效。")


def _crypto_dashboard_table(dashboard: dict[str, Any], changed_only: bool = False) -> pd.DataFrame:
    fixed = ["BTC", "ETH", "SOL", "XAUT", "BNB"]
    states = {item.get("symbol"): item for item in dashboard.get("symbols", [])}
    rows = []
    for symbol in fixed:
        state = states.get(symbol, {})
        rows.append({
            "币种": symbol,
            "当前状态": state.get("status", "待检查"),
            "最新变化": state.get("latest_change", "暂无新的策略变化"),
            "变化日期": state.get("latest_change_date", "-"),
            "数据截至": state.get("data_date", "-"),
            "数据质量": state.get("quality", "待检查"),
        })
    table = pd.DataFrame(rows)
    return table[table["当前状态"] == "有变化"].reset_index(drop=True) if changed_only else table


def crypto_unread_event_ids(dashboard: dict[str, Any], symbol: str) -> list[str]:
    return [
        str(event["event_id"])
        for event in dashboard.get("events", [])
        if event.get("symbol") == symbol
        and not event.get("historical_backfill", False)
        and event.get("event_id")
    ]


def _crypto_candlestick_chart(history: pd.DataFrame, state: dict[str, Any] | None = None, overlays: dict[str, bool] | None = None) -> go.Figure:
    del state
    overlays = overlays or {"sma120": True, "vegas": True}
    figure = go.Figure()
    if history.empty or not hasattr(figure, "add_trace"):
        return figure
    figure.add_trace(go.Candlestick(x=history["日期"], open=history["开盘价"], high=history["最高价"], low=history["最低价"], close=history["收盘价"], name="日线"))
    if overlays.get("sma120") and "sma120" in history:
        figure.add_trace(go.Scatter(x=history["日期"], y=history["sma120"], name="SMA120", mode="lines"))
    if overlays.get("vegas"):
        for column, name in (("ema144", "EMA144"), ("ema169", "EMA169")):
            if column in history:
                figure.add_trace(go.Scatter(x=history["日期"], y=history[column], name=name, mode="lines"))
    figure.update_layout(height=520, xaxis_rangeslider_visible=False, margin={"l": 20, "r": 20, "t": 30, "b": 20})
    return figure


def _render_legacy_crypto_turnover() -> None:
    target_date = resolve_crypto_target_date()
    refresh_started = _start_crypto_auto_refresh_once(target_date)
    available_dates = get_available_crypto_dates()
    _apply_pending_crypto_selected_date(available_dates)
    status = read_crypto_refresh_status()
    default_display_date = _resolve_crypto_display_date(target_date, available_dates, status)
    selected_default = _ensure_crypto_selected_date(available_dates, default_display_date)
    date_options = available_dates if available_dates else [selected_default]
    default_index = date_options.index(selected_default) if selected_default in date_options else len(date_options) - 1
    display_date = selected_default
    snapshot = load_crypto_snapshot(display_date) if display_date in available_dates else pd.DataFrame()
    metadata = load_crypto_metadata(display_date) if display_date in available_dates else {}

    header_left, header_date, header_status = st.columns([1.45, 0.58, 0.92], vertical_alignment="center")
    with header_left:
        st.title(f"加密货币{CRYPTO_WINDOW_DAYS}日成交额Top{CRYPTO_TOP_TURNOVER_LIMIT}")
        st.caption(_crypto_source_caption(metadata))
    with header_date:
        display_date = st.selectbox("数据日期", date_options, index=default_index, key=CRYPTO_DATE_SELECT_KEY)
        snapshot = load_crypto_snapshot(display_date) if display_date in available_dates else pd.DataFrame()
        metadata = load_crypto_metadata(display_date) if display_date in available_dates else {}
    with header_status:
        success_count = len(metadata.get("successful_exchanges") or [])
        failed_count = len(metadata.get("failed_exchanges") or [])
        source_count = success_count + failed_count or 5
        generated_at = str(metadata.get("generated_at") or status.get("generated_at") or "-")
        window_start = str(metadata.get("window_start") or "-")
        window_end = str(metadata.get("window_end") or display_date)
        st.metric(
            "数据状态",
            f"{success_count or '-'} / {source_count}源 {len(snapshot) or '-'}币",
            f"窗口 {window_start} 至 {window_end} | 生成 {generated_at}",
        )

    _render_crypto_refresh_status(display_date, target_date, force_poll=refresh_started)

    if snapshot.empty:
        st.warning("暂无可展示的币圈现货成交额缓存，正在尝试拉取最新数据。")
        return

    if metadata.get("partial"):
        failed = metadata.get("failed_exchanges") or []
        failed_names = "、".join(str(item.get("exchange")) for item in failed if isinstance(item, dict))
        skipped = metadata.get("skipped_exchanges") or []
        skipped_names = "、".join(str(item) for item in skipped)
        detail = failed_names or skipped_names or "未记录"
        st.warning(f"本次为部分数据源结果，未纳入数据源：{detail}。")

    st.dataframe(
        _display_crypto_table(snapshot),
        width="stretch",
        hide_index=True,
        height=35 * (len(snapshot) + 1) + 20,
        column_config=_crypto_table_columns(),
    )


def _crypto_source_caption(metadata: dict[str, Any]) -> str:
    source_mode = str(metadata.get("source_mode") or "")
    successful = {str(item) for item in metadata.get("successful_exchanges") or []}
    if source_mode in {"binance_plugin", "binance_public_fallback"} or (
        successful and successful.issubset({"binance", "binance_vision"})
    ):
        return "Binance 现货滚动30日成交额 | USD系报价近似等值 | 不含交易建议"
    return "多交易所现货成交额聚合 | USD系报价近似等值 | 不含交易建议"


def _resolve_crypto_display_date(target_date: str, available_dates: list[str], status: dict[str, Any]) -> str:
    if target_date in available_dates:
        return target_date
    actual_date = str(status.get("actual_date") or "")
    if actual_date in available_dates:
        return actual_date
    latest_date = latest_crypto_cache_date()
    return latest_date or target_date


def _ensure_crypto_selected_date(dates: list[str], default_date: str) -> str:
    if not dates:
        return default_date
    selected = st.session_state.get(CRYPTO_DATE_SELECT_KEY)
    if selected not in dates:
        selected = default_date if default_date in dates else dates[-1]
        st.session_state[CRYPTO_DATE_SELECT_KEY] = selected
    return str(selected)


def _apply_pending_crypto_selected_date(dates: list[str]) -> None:
    pending = st.session_state.pop(PENDING_CRYPTO_DATE_SELECT_KEY, None)
    if pending in dates:
        st.session_state[CRYPTO_DATE_SELECT_KEY] = pending


def _render_crypto_refresh_status(display_date: str, target_date: str, force_poll: bool = False) -> None:
    should_poll = force_poll or read_crypto_refresh_status().get("status") == CRYPTO_REFRESH_RUNNING
    if hasattr(st, "fragment") and should_poll:

        @st.fragment(run_every="5s")
        def _crypto_refresh_fragment() -> None:
            _render_crypto_refresh_status_panel(display_date, target_date)

        _crypto_refresh_fragment()
        return

    _render_crypto_refresh_status_panel(display_date, target_date)


def _render_crypto_refresh_status_panel(display_date: str, target_date: str) -> None:
    status = read_crypto_refresh_status()
    status_name = str(status.get("status") or CRYPTO_REFRESH_IDLE)
    message = _crypto_status_message(status, display_date, target_date)
    notice_col, latest_col, check_col = st.columns([1.48, 0.34, 0.34], vertical_alignment="center")
    with notice_col:
        if status_name == CRYPTO_REFRESH_SUCCEEDED:
            st.success(message)
        elif status_name == CRYPTO_REFRESH_FAILED:
            if display_date and display_date != target_date:
                st.warning(message)
            else:
                st.error(message)
        elif status_name in {CRYPTO_REFRESH_RUNNING, CRYPTO_REFRESH_SKIPPED}:
            st.info(message)
        else:
            st.info(message)
        updated_at = str(status.get("updated_at") or "").strip()
        if updated_at:
            st.caption(f"最近更新 {updated_at}")
    with latest_col:
        if target_date in get_available_crypto_dates() and display_date != target_date:
            if st.button("刷新到最新数据", key="refresh-crypto-to-latest", use_container_width=True):
                st.session_state[PENDING_CRYPTO_DATE_SELECT_KEY] = target_date
                st.rerun()
    with check_col:
        if st.button(
            "检查最新数据",
            key="check-crypto-latest-data",
            disabled=status_name == CRYPTO_REFRESH_RUNNING,
            use_container_width=True,
        ):
            _start_crypto_refresh_thread(force=True)
            st.rerun()


def _crypto_status_message(status: dict[str, Any], display_date: str, target_date: str) -> str:
    status_name = str(status.get("status") or CRYPTO_REFRESH_IDLE)
    if status_name == CRYPTO_REFRESH_RUNNING:
        return f"正在更新币圈现货 {target_date} 30日成交额数据..."
    if status_name == CRYPTO_REFRESH_SUCCEEDED:
        return str(status.get("message") or f"已生成 {target_date} 币圈现货30日成交额 Top{CRYPTO_TOP_TURNOVER_LIMIT}")
    if status_name == CRYPTO_REFRESH_FAILED:
        base = str(status.get("message") or f"{target_date} 币圈现货30日成交额更新失败")
        error = str(status.get("error") or "").strip()
        suffix = f"：{_short_error(error)}" if error else ""
        return f"{base}{suffix}"
    if status_name == CRYPTO_REFRESH_SKIPPED:
        return str(status.get("message") or f"当前已是最新币圈现货 {target_date} 30日成交额数据")
    if display_date and display_date != target_date:
        return f"当前展示 {display_date} 币圈现货缓存，目标日期 {target_date} 尚未生成"
    return "等待检查币圈现货最新数据"


def _render_refresh_status(selected_date: str, force_poll: bool = False) -> None:
    should_poll = force_poll or read_refresh_status().get("status") == REFRESH_RUNNING
    if hasattr(st, "fragment") and should_poll:

        @st.fragment(run_every="5s")
        def _refresh_fragment() -> None:
            _render_refresh_status_panel(selected_date)

        _refresh_fragment()
        return

    _render_refresh_status_panel(selected_date)


def _render_refresh_status_panel(selected_date: str) -> None:
    available_dates = get_available_dates()
    status = read_refresh_status()
    view = _refresh_status_view(
        status,
        selected_date,
        available_dates,
        fetch_report=_read_fetch_report(selected_date),
        problem_summary=problem_snapshot_summary(),
    )
    active_refresh = refresh_task_is_active()

    notice_col, latest_col, retry_date_col, retry_col, check_col = st.columns(
        [1.18, 0.3, 0.34, 0.34, 0.34], vertical_alignment="center"
    )
    with notice_col:
        level = view["level"]
        if level == "success":
            st.success(view["message"])
        elif level == "warning":
            st.warning(view["message"])
        elif level == "error":
            st.error(view["message"])
        else:
            st.info(view["message"])
        if view["progress_total"]:
            st.progress(int(view["progress_percent"]))
        if view["updated_at"]:
            st.caption(f"最近更新 {view['updated_at']}")
        if view["history_stats_message"]:
            if view["history_failed"]:
                st.warning(view["history_stats_message"])
            else:
                st.caption(view["history_stats_message"])
        if view["problem_summary_message"]:
            st.warning(view["problem_summary_message"])
        if view["is_stale"]:
            st.warning("进度暂未变化，可能在等待数据源响应")
    with latest_col:
        if view["show_refresh_button"]:
            if st.button("刷新到最新数据", key="refresh-to-latest", use_container_width=True):
                latest_date = str(view["latest_date"])
                if latest_date in get_available_dates():
                    st.session_state[PENDING_DATE_SELECT_KEY] = latest_date
                st.rerun()
    with retry_date_col:
        if st.button(
            "重拉当前日期",
            key=f"retry-selected-date-{selected_date}",
            disabled=view["status"] == REFRESH_RUNNING and active_refresh,
            use_container_width=True,
        ):
            _start_retry_selected_thread(selected_date)
            st.rerun()
    with retry_col:
        if view["show_retry_button"]:
            if st.button(
                "补拉存量问题",
                key="retry-problem-snapshots",
                disabled=view["status"] == REFRESH_RUNNING and active_refresh,
                use_container_width=True,
            ):
                _start_retry_thread()
                st.rerun()
    with check_col:
        if st.button(
            "检查最新数据",
            key="check-latest-data",
            disabled=view["status"] == REFRESH_RUNNING and active_refresh,
            use_container_width=True,
        ):
            _start_refresh_thread(force=False)
            st.rerun()


def _refresh_status_view(
    status: dict[str, Any],
    selected_date: str,
    available_dates: list[str],
    now: datetime | None = None,
    fetch_report: dict[str, Any] | None = None,
    problem_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_name = str(status.get("status") or REFRESH_IDLE)
    target_date = str(status.get("target_date") or resolve_refresh_target_date())
    actual_date = str(status.get("actual_date") or target_date)
    latest_date = actual_date if actual_date in available_dates else target_date
    error = str(status.get("error") or "").strip()
    progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
    progress_completed = _safe_int(progress.get("completed"))
    progress_total = _safe_int(progress.get("total"))
    progress_percent = int(progress_completed / progress_total * 100) if progress_total else 0
    updated_at = str(status.get("updated_at") or "").strip()

    view = {
        "status": status_name,
        "level": "info",
        "message": "等待检查最新数据",
        "latest_date": latest_date,
        "show_refresh_button": False,
        "progress_completed": progress_completed,
        "progress_total": progress_total,
        "progress_percent": max(0, min(100, progress_percent)),
        "updated_at": updated_at,
        "history_stats_message": "",
        "history_failed": 0,
        "failed_history_codes": [],
        "problem_summary_message": "",
        "problem_snapshot_count": 0,
        "problem_count": 0,
        "show_retry_button": False,
        "is_stale": False,
    }

    history_stats = _history_report_view(fetch_report)
    if history_stats is not None:
        view.update(history_stats)
    problem_stats = _problem_summary_view(problem_summary)
    if problem_stats is not None:
        view.update(problem_stats)

    if status_name == REFRESH_RUNNING:
        view["message"] = _running_refresh_message(target_date, str(status.get("phase") or ""), progress)
        view["is_stale"] = _refresh_progress_is_stale(updated_at, now)
    elif status_name == REFRESH_SUCCEEDED:
        view["level"] = "success" if actual_date == target_date else "warning"
        view["message"] = str(
            status.get("message")
            or (f"已生成 {actual_date} 数据" if actual_date == target_date else f"数据源最新为 {actual_date}，已生成对应数据")
        )
        view["show_refresh_button"] = actual_date in available_dates and actual_date != selected_date
        view["latest_date"] = actual_date
    elif status_name == REFRESH_FAILED:
        view["level"] = "error"
        base_message = str(status.get("message") or f"{target_date} 更新失败")
        suffix = f"：{_short_error(error)}" if error else ""
        view["message"] = f"{base_message}{suffix}"
    elif status_name == REFRESH_SKIPPED:
        generated_at = str(status.get("generated_at") or status.get("finished_at") or "").strip()
        view["message"] = str(status.get("message") or f"当前已是最新 {target_date} 数据" + (f"，生成 {generated_at}" if generated_at else ""))
    elif target_date in available_dates:
        view["message"] = f"当前已是最新 {target_date} 数据"

    return view


# R5: fetch_report memoize 缓存
_fetch_report_cache: dict[tuple[str, int], dict] = {}


def _read_fetch_report(data_date: str) -> dict[str, Any] | None:
    """R5: 按 (data_date, mtime_ns) memoize。"""
    path = ROOT / "data" / "normalized" / data_date / "fetch_report.json"
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    cache_key = (data_date, mtime)
    cached = _fetch_report_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    _fetch_report_cache[cache_key] = payload
    # 简单 LRU：限制缓存大小
    if len(_fetch_report_cache) > 50:
        _fetch_report_cache.pop(next(iter(_fetch_report_cache)), None)
    return payload


def _history_report_view(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    total = _report_int(report, "history_total")
    if total is None:
        total = _report_int(report, "candidate_rows")
    if total is None:
        return None
    success = _report_int(report, "history_success")
    if success is None:
        success = (_report_int(report, "skipped_cached") or 0) + (_report_int(report, "fetched_history_files") or 0)
    failed = _report_int(report, "history_failed")
    if failed is None:
        failed = _report_int(report, "history_errors") or 0
    failed_codes = _failed_history_codes(report)
    failed_text = f"；失败代码 {'、'.join(failed_codes[:6])}" if failed_codes else ""
    if len(failed_codes) > 6:
        failed_text += "等"
    return {
        "history_stats_message": f"历史行情：成功 {success} / 总数 {total}，失败 {failed}{failed_text}",
        "history_failed": int(failed),
        "failed_history_codes": failed_codes,
    }


def _problem_summary_view(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not summary:
        return None
    snapshot_count = _safe_int(summary.get("snapshot_count"))
    problem_count = _safe_int(summary.get("problem_count"))
    if snapshot_count <= 0:
        return {
            "problem_summary_message": "",
            "problem_snapshot_count": 0,
            "problem_count": 0,
            "show_retry_button": False,
        }
    dates = summary.get("dates") if isinstance(summary.get("dates"), list) else []
    date_text = f"：{'、'.join(str(item) for item in dates[:4])}" if dates else ""
    if len(dates) > 4:
        date_text += "等"
    return {
        "problem_summary_message": f"存量待补拉：{snapshot_count} 个日期，失败/缺失 {problem_count} 个历史文件{date_text}",
        "problem_snapshot_count": snapshot_count,
        "problem_count": problem_count,
        "show_retry_button": True,
    }


def _failed_history_codes(report: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    failed_history = report.get("failed_history")
    if isinstance(failed_history, list):
        for item in failed_history:
            if isinstance(item, dict):
                raw_code = str(item.get("code") or "").strip()
                code = raw_code.zfill(6)
                if raw_code and len(code) == 6 and code.isdigit() and code not in codes:
                    codes.append(code)
    if codes:
        return codes

    errors = report.get("errors")
    if not isinstance(errors, list):
        return []
    for item in errors:
        code = str(item).split(":", 1)[0].strip()
        if len(code) == 6 and code.isdigit() and code not in codes:
            codes.append(code)
    return codes


def _report_int(report: dict[str, Any], key: str) -> int | None:
    if key not in report:
        return None
    try:
        return int(report[key])
    except (TypeError, ValueError):
        return None


def _running_refresh_message(target_date: str, phase: str, progress: dict[str, Any]) -> str:
    completed = _safe_int(progress.get("completed"))
    total = _safe_int(progress.get("total"))
    skipped_cached = _safe_int(progress.get("skipped_cached"))
    fetched = _safe_int(progress.get("fetched"))
    errors = _safe_int(progress.get("errors"))
    max_workers = _safe_int(progress.get("max_workers"))
    current_code = str(progress.get("current_code") or "").strip()
    speed = _safe_float(progress.get("speed"))
    eta_seconds = _safe_float(progress.get("eta_seconds"))

    # R6: speed/ETA 后缀（仅当有意义时显示）
    speed_eta_suffix = ""
    if speed > 0 and eta_seconds > 0:
        speed_eta_suffix = f"，{speed:.1f} 码/秒，预计 {int(eta_seconds)}s"
    if phase == "retry_scan":
        return "正在扫描存量问题..."
    if phase == "retry_fetch_history" and total:
        snapshot_total = _safe_int(progress.get("snapshot_total"))
        snapshot_completed = _safe_int(progress.get("snapshot_completed"))
        source_date = str(progress.get("current_source_date") or "").strip()
        date_prefix = f"日期 {min(snapshot_completed + 1, snapshot_total)}/{snapshot_total} {source_date}" if snapshot_total else source_date
        workers = f"，并发 {max_workers}" if max_workers else ""
        current = f"，当前 {current_code}" if current_code else ""
        return (
            f"正在补拉存量问题：{date_prefix}，历史行情 {completed}/{total}，"
            f"缓存 {skipped_cached}，新拉取 {fetched}，失败 {errors}{workers}{current}{speed_eta_suffix}"
        )
    if phase == "retry_fetch_scale":
        source_date = str(progress.get("current_source_date") or "").strip()
        return f"正在补拉存量问题：{source_date} 份额数据...{speed_eta_suffix}"
    if phase == "retry_score":
        score_completed = _safe_int(progress.get("score_completed", completed))
        score_total = _safe_int(progress.get("score_total", total))
        score_date = str(progress.get("current_score_date") or "").strip()
        current = f"，当前 {score_date}" if score_date else ""
        return f"正在重算关联排行榜：{score_completed}/{score_total}{current}{speed_eta_suffix}"
    if phase == "fetch_spot":
        return f"正在更新 {target_date} 数据：拉取候选池...{speed_eta_suffix}"
    if phase == "fetch_history" and total:
        if skipped_cached or fetched or errors or max_workers > 1:
            workers = f"，并发 {max_workers}" if max_workers else ""
            current = f"，当前 {current_code}" if current_code else ""
            return (
                f"正在更新 {target_date} 数据：历史行情 {completed}/{total}，"
                f"缓存 {skipped_cached}，新拉取 {fetched}，失败 {errors}{workers}{current}{speed_eta_suffix}"
            )
        current = f"，当前 {current_code}" if current_code else ""
        return f"正在更新 {target_date} 数据：历史行情 {completed}/{total}{current}{speed_eta_suffix}"
    if phase == "fetch_scale":
        return f"正在更新 {target_date} 数据：拉取份额数据...{speed_eta_suffix}"
    if phase == "score":
        return f"正在更新 {target_date} 数据：生成评分...{speed_eta_suffix}"
    return f"正在更新 {target_date} 数据...{speed_eta_suffix}"


def _refresh_progress_is_stale(updated_at: str, now: datetime | None = None) -> bool:
    if not updated_at:
        return False
    try:
        updated = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASIA_SHANGHAI)
    except ValueError:
        return False
    current = now.astimezone(ASIA_SHANGHAI) if now else datetime.now(ASIA_SHANGHAI)
    return (current - updated).total_seconds() > 180


def _safe_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _apply_pending_selected_date(dates: list[str]) -> None:
    pending = st.session_state.pop(PENDING_DATE_SELECT_KEY, None)
    if pending in dates:
        st.session_state[DATE_SELECT_KEY] = pending


def _short_error(error: str, limit: int = 96) -> str:
    if len(error) <= limit:
        return error
    return f"{error[:limit].rstrip()}..."


def _embed_hidden_html(html: str) -> None:
    if hasattr(st, "iframe"):
        st.iframe(
            f"""
            <script>
            try {{
              window.frameElement.style.display = "none";
            }} catch (error) {{}}
            </script>
            {html}
            """,
            height=1,
            width=1,
        )
        return
    components.html(html, height=0, width=0)


def _install_lifecycle_heartbeat() -> None:
    heartbeat_url = os.environ.get("TREND_RADAR_HEARTBEAT_URL")
    if not heartbeat_url:
        return

    # The local launcher watches this ping and shuts Streamlit down after the
    # browser page has been closed for a short idle window.
    _embed_hidden_html(
        f"""
        <script>
        const heartbeatUrl = {json.dumps(heartbeat_url)};
        function pingLauncher() {{
          const img = new Image();
          img.src = heartbeatUrl + "?t=" + Date.now();
        }}
        pingLauncher();
        window.setInterval(pingLauncher, 2500);
        </script>
        """
    )


def _install_code_copy_handler() -> None:
    _embed_hidden_html(
        """
        <script>
        (() => {
          const parentWindow = window.parent;
          const parentDocument = parentWindow.document;
          if (parentDocument.__trendRadarCodeCopyAbort) {
            parentDocument.__trendRadarCodeCopyAbort.abort();
          }
          const abortController = new parentWindow.AbortController();
          parentDocument.__trendRadarCodeCopyAbort = abortController;
          parentDocument.__trendRadarCodeCopyInstalled = true;

          const codePattern = /^(?:\\d{6}|[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z0-9]{1,2})?)$/;
          const editorSelector = "textarea.gdg-input, input.gdg-input";
          const editorOverlaySelector = ".gdg-style.gdg-pad, .gdg-clip-region";
          const toastId = "trend-code-copy-toast";
          let lastClick = null;
          let lastCopied = null;

          function codeColumnIndex(frame) {
            const headers = Array.from(frame.querySelectorAll('th[role="columnheader"]'));
            const codeHeader = headers.find((header) => header.textContent.trim() === "代码");
            return codeHeader ? codeHeader.getAttribute("aria-colindex") : null;
          }

          function dataFrameIndexFromTarget(target) {
            if (!target || target.nodeType !== Node.ELEMENT_NODE) {
              return -1;
            }
            const frame = target.closest('[data-testid="stDataFrame"]');
            if (!frame) {
              return -1;
            }
            return Array.from(parentDocument.querySelectorAll('[data-testid="stDataFrame"]')).indexOf(frame);
          }

          function selectedCodeFromDataFrame(frame) {
            if (!frame) {
              return null;
            }

            const codeColumn = codeColumnIndex(frame);
            if (!codeColumn) {
              return null;
            }

            const selectedCells = Array.from(frame.querySelectorAll('td[aria-selected="true"]'));
            const selectedCodeCell = selectedCells.find((cell) => {
              const text = cell.textContent.trim();
              return cell.getAttribute("aria-colindex") === codeColumn && codePattern.test(text);
            });
            return selectedCodeCell ? selectedCodeCell.textContent.trim() : null;
          }

          function selectedCodeFromFrameIndex(index) {
            if (index < 0) {
              return null;
            }
            const frame = parentDocument.querySelectorAll('[data-testid="stDataFrame"]')[index];
            return selectedCodeFromDataFrame(frame);
          }

          function selectedCodeFromText() {
            const editorCode = codeFromEditor(parentDocument.activeElement);
            if (editorCode) {
              return editorCode;
            }
            const selection = parentDocument.getSelection();
            const text = selection ? selection.toString().trim() : "";
            return codePattern.test(text) ? text : null;
          }

          function codeFromEditor(target) {
            if (!target?.matches?.(editorSelector)) {
              return null;
            }
            const text = String(target.value || target.textContent || "").trim();
            return codePattern.test(text) ? text : null;
          }

          function legacyCopy(text) {
            const textarea = parentDocument.createElement("textarea");
            textarea.value = text;
            textarea.setAttribute("readonly", "");
            textarea.style.position = "fixed";
            textarea.style.left = "-9999px";
            textarea.style.top = "0";
            parentDocument.body.appendChild(textarea);
            textarea.select();

            let copied = false;
            try {
              copied = parentDocument.execCommand("copy");
            } finally {
              parentDocument.body.removeChild(textarea);
            }
            return copied;
          }

          function showToast(code) {
            let toast = parentDocument.getElementById(toastId);
            if (!toast) {
              toast = parentDocument.createElement("div");
              toast.id = toastId;
              toast.style.position = "fixed";
              toast.style.right = "24px";
              toast.style.bottom = "24px";
              toast.style.zIndex = "2147483647";
              toast.style.background = "#111827";
              toast.style.color = "#ffffff";
              toast.style.border = "1px solid rgba(255,255,255,0.16)";
              toast.style.borderRadius = "6px";
              toast.style.boxShadow = "0 10px 28px rgba(17,24,39,0.22)";
              toast.style.font = "14px/1.35 sans-serif";
              toast.style.padding = "9px 12px";
              toast.style.pointerEvents = "none";
              toast.style.opacity = "0";
              toast.style.transform = "translateY(6px)";
              toast.style.transition = "opacity 150ms ease, transform 150ms ease";
              parentDocument.body.appendChild(toast);
            }

            toast.textContent = `已复制 ${code}`;
            toast.style.opacity = "1";
            toast.style.transform = "translateY(0)";
            parentWindow.clearTimeout(toast.__trendRadarTimer);
            toast.__trendRadarTimer = parentWindow.setTimeout(() => {
              toast.style.opacity = "0";
              toast.style.transform = "translateY(6px)";
            }, 1200);
          }

          function closeFloatingEditor() {
            const activeElement = parentDocument.activeElement;
            const escapeEvent = new parentWindow.KeyboardEvent("keydown", {
              key: "Escape",
              code: "Escape",
              keyCode: 27,
              which: 27,
              bubbles: true,
              cancelable: true,
            });

            if (activeElement?.matches?.(editorSelector)) {
              activeElement.blur();
              activeElement.dispatchEvent(escapeEvent);
            }

            parentDocument.querySelectorAll(editorSelector).forEach((editor) => {
              editor.blur();
              editor.style.display = "none";
            });
            parentDocument.querySelectorAll(editorOverlaySelector).forEach((overlay) => {
              overlay.style.display = "none";
              overlay.style.opacity = "0";
              overlay.style.pointerEvents = "none";
            });
            parentDocument.dispatchEvent(escapeEvent);
          }

          function copyCode(code) {
            const now = Date.now();
            if (lastCopied && lastCopied.code === code && now - lastCopied.time < 700) {
              return;
            }
            lastCopied = { code, time: now };
            closeFloatingEditor();
            legacyCopy(code);
            if (parentWindow.navigator.clipboard?.writeText) {
              parentWindow.navigator.clipboard.writeText(code).catch(() => {});
            }
            showToast(code);
            parentWindow.setTimeout(closeFloatingEditor, 80);
            parentWindow.setTimeout(closeFloatingEditor, 250);
          }

          function maybeCopyFromRepeatedClick(event) {
            const frameIndex = dataFrameIndexFromTarget(event.target);
            const editorCode = codeFromEditor(event.target);
            parentWindow.setTimeout(() => {
              const code = editorCode || selectedCodeFromFrameIndex(frameIndex) || selectedCodeFromText();
              if (!code) {
                lastClick = null;
                return;
              }

              const now = Date.now();
              if (lastClick && lastClick.code === code && now - lastClick.time <= 650) {
                copyCode(code);
                lastClick = null;
                return;
              }
              lastClick = { code, time: now };
            }, 400);
          }

          parentDocument.addEventListener("click", maybeCopyFromRepeatedClick, {
            capture: true,
            signal: abortController.signal,
          });
          parentDocument.addEventListener(
            "dblclick",
            (event) => {
              const frameIndex = dataFrameIndexFromTarget(event.target);
              const editorCode = codeFromEditor(event.target);
              parentWindow.setTimeout(() => {
                const code = editorCode || selectedCodeFromFrameIndex(frameIndex) || selectedCodeFromText();
                if (code) {
                  copyCode(code);
                }
              }, 80);
            },
            {
              capture: true,
              signal: abortController.signal,
            }
          );
        })();
        </script>
        """
    )


def _render_summary_metrics(snapshot: pd.DataFrame, changes: dict[str, list[str]]) -> None:
    best = snapshot.iloc[0]
    strong_count = int(snapshot["趋势状态"].isin(["强趋势", "趋势向上"]).sum())
    new_count = len(changes["新进Top10"])
    avg_score = snapshot.head(10)["主线得分"].mean()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("头部主线", str(best["ETF名称"]), f"{int(best['主线得分'])} 分", help=TERM_HELP["头部主线"])
    col2.metric("Top10均分", f"{avg_score:.1f}", "观察池强度", help=TERM_HELP["Top10均分"])
    col3.metric("强趋势数量", strong_count, "全榜", help=TERM_HELP["强趋势数量"])
    col4.metric("新进Top10", new_count, "相对上一交易日", help=TERM_HELP["新进Top10"])


def _render_changes(changes: dict[str, list[str]]) -> None:
    st.subheader("主线变化提醒")
    for label, values in changes.items():
        names = "、".join(values[:6]) if values else "无"
        css_class = {
            "新进Top10": "change-new",
            "连续Top10": "change-keep",
            "跌出Top10": "change-drop",
        }[label]
        st.markdown(
            f"""
            <div class="change-card {css_class}">
              <div class="change-label">{label}</div>
              <div class="change-names">{names}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_data_quality(meta: dict[str, object], snapshot: pd.DataFrame) -> None:
    health = _data_health_summary(meta, snapshot)
    if health["rows"] and (health["low_confidence_rows"] or health["medium_confidence_rows"]):
        cols = st.columns(5)
        cols[0].metric("数据行数", health["rows"])
        cols[1].metric("东方财富主源", health["eastmoney_rows"])
        cols[2].metric("新浪兜底", health["sina_rows"])
        cols[3].metric("低置信行", health["low_confidence_rows"], f"Top10 {health['top10_low_confidence_rows']}")
        cols[4].metric("fallback/未知", health["fallback_rows"])

    if health["top10_low_confidence_rows"]:
        st.warning(
            f"当前 Top10 观察池含 {health['top10_low_confidence_rows']} 条低置信行情，"
            "不能按完整数据解读；请点击“重拉当前日期”重新拉取正确历史行情。"
        )
    elif health["low_confidence_rows"]:
        st.warning(
            f"完整榜单含 {health['low_confidence_rows']} 条低置信数据；相关行已标记具体原因，建议重拉当前日期。"
        )

    if health["source_summary"]:
        st.caption(f"行情来源分布：{health['source_summary']}")


def _data_health_summary(meta: dict[str, object], snapshot: pd.DataFrame) -> dict[str, object]:
    rows = int(len(snapshot))
    if rows == 0:
        return {
            "rows": 0,
            "low_confidence_rows": 0,
            "medium_confidence_rows": 0,
            "top10_low_confidence_rows": 0,
            "fallback_rows": 0,
            "eastmoney_rows": 0,
            "sina_rows": 0,
            "source_summary": "",
        }

    confidence = (
        snapshot["数据置信度"].fillna("未记录").astype(str)
        if "数据置信度" in snapshot
        else pd.Series(["未记录"] * rows, index=snapshot.index)
    )
    history_source = (
        snapshot["行情来源"].fillna("来源未记录").astype(str)
        if "行情来源" in snapshot
        else pd.Series(["来源未记录"] * rows, index=snapshot.index)
    )
    share_source = (
        snapshot["份额来源"].fillna("来源未记录").astype(str)
        if "份额来源" in snapshot
        else pd.Series(["来源未记录"] * rows, index=snapshot.index)
    )
    low_confidence_rows = int(confidence.eq("低").sum())
    medium_confidence_rows = int(confidence.eq("中").sum())
    top10_low_confidence_rows = int(confidence.head(10).eq("低").sum())
    fallback_pattern = "fallback|补点|兜底|实时行情|来源未记录"
    fallback_rows = int(
        (
            history_source.str.contains(fallback_pattern, case=False, regex=True)
            | share_source.str.contains(fallback_pattern, case=False, regex=True)
        ).sum()
    )
    eastmoney_rows = int(history_source.str.contains("东方财富", case=False, regex=False).sum())
    sina_rows = int(history_source.str.contains("新浪", case=False, regex=False).sum())

    quality = meta.get("data_quality") if isinstance(meta, dict) else None
    if isinstance(quality, dict):
        low_confidence_rows = int(quality.get("low_confidence_rows") or low_confidence_rows)
        top10_low_confidence_rows = int(quality.get("top10_low_confidence_rows") or top10_low_confidence_rows)

    source_counts = history_source.value_counts().to_dict()
    source_summary = "、".join(f"{key} {value}" for key, value in list(source_counts.items())[:5])
    return {
        "rows": rows,
        "low_confidence_rows": low_confidence_rows,
        "medium_confidence_rows": medium_confidence_rows,
        "top10_low_confidence_rows": top10_low_confidence_rows,
        "fallback_rows": fallback_rows,
        "eastmoney_rows": eastmoney_rows,
        "sina_rows": sina_rows,
        "source_summary": source_summary,
    }


def _render_detail(row: pd.Series, selected_date: str) -> None:
    st.subheader(f"{row['ETF名称']} {row['代码']} 明细")
    st.markdown(f"<div class='reason-line'>{row['观察理由']}</div>", unsafe_allow_html=True)
    confidence = str(row.get("数据置信度", "未记录"))
    issue = str(row.get("数据问题", "") or "")
    if confidence in {"低", "中"} or (issue and issue != "无"):
        st.warning(
            f"数据置信度：{confidence}；行情来源：{row.get('行情来源', '未记录')}；"
            f"份额来源：{row.get('份额来源', '未记录')}；问题：{issue or '未记录'}。"
            "如需按完整数据参考，请重拉当前日期。"
        )

    detail_cols = st.columns([0.95, 0.95, 0.95, 1.25], vertical_alignment="center")
    detail_cols[0].metric("主线得分", int(row["主线得分"]), row["趋势状态"], help=TERM_HELP["主线得分"])
    detail_cols[1].metric(
        "成交额Top30",
        f"{int(row['成交额Top30次数'])}/20",
        f"当前第{int(row['成交额排名'])}",
        help=TERM_HELP["成交额Top30"],
    )
    detail_cols[2].metric("20日涨幅", _pct(row["20日涨幅"]), f"60日 {_pct(row['60日涨幅'])}", help=TERM_HELP["20日涨幅"])
    detail_cols[3].metric(
        "份额变化",
        f"5日 {_pct(row['5日份额变化'])}",
        f"20日 {_pct(row['20日份额变化'])}",
        help=TERM_HELP["份额变化"],
    )

    history = build_price_history(row, selected_date)
    score_breakdown = build_score_breakdown(row)

    chart_cols = st.columns([1.62, 1.0], gap="medium")
    with chart_cols[0]:
        st.plotly_chart(_price_chart(history, str(row["ETF名称"])), width="stretch")
    with chart_cols[1]:
        st.plotly_chart(_score_chart(score_breakdown), width="stretch")

    flow_cols = st.columns(2, gap="medium")
    with flow_cols[0]:
        st.plotly_chart(_amount_chart(history), width="stretch")
    with flow_cols[1]:
        st.plotly_chart(_share_chart(history), width="stretch")


def _filter_snapshot(snapshot: pd.DataFrame, query: str, statuses: list[str]) -> pd.DataFrame:
    filtered = snapshot.copy()
    if query.strip():
        keyword = query.strip().lower()
        filtered = filtered[
            filtered["ETF名称"].str.lower().str.contains(keyword)
            | filtered["代码"].astype(str).str.lower().str.contains(keyword)
        ]
    if statuses:
        filtered = filtered[filtered["趋势状态"].isin(statuses)]
    return filtered.reset_index(drop=True)


def _sort_snapshot_for_display(df: pd.DataFrame, sort_label: str) -> pd.DataFrame:
    sort_column, ascending = SORT_FIELDS[sort_label]
    return df.sort_values(sort_column, ascending=ascending).reset_index(drop=True)


def _display_crypto_table(df: pd.DataFrame) -> pd.DataFrame:
    value_columns = ["币种", "30日成交额", "日均成交额", "交易所数", "交易对数", "最大来源", "最大来源占比", "来源明细"]
    columns = ["排名", *value_columns]
    if df.empty:
        return pd.DataFrame(columns=columns)
    source = df.copy()
    for column in ["30日成交额", "日均成交额", "最大来源占比", "交易所数", "交易对数"]:
        source[column] = pd.to_numeric(source.get(column), errors="coerce")
    source = source.sort_values("30日成交额", ascending=False).reset_index(drop=True)
    table = source[value_columns].copy()
    table.insert(0, "排名", range(1, len(table) + 1))
    table["30日成交额"] = table["30日成交额"].map(_format_usd_equivalent_zh)
    table["日均成交额"] = table["日均成交额"].map(_format_usd_equivalent_zh)
    return table


def _crypto_table_columns() -> dict[str, object]:
    return {
        "排名": st.column_config.NumberColumn("排名", format="%d", width="small"),
        "币种": st.column_config.TextColumn("币种", width="small"),
        "30日成交额": st.column_config.TextColumn("30日成交额", width="medium"),
        "日均成交额": st.column_config.TextColumn("日均成交额", width="medium"),
        "交易所数": st.column_config.NumberColumn("交易所数", format="%d", width="small"),
        "交易对数": st.column_config.NumberColumn("交易对数", format="%d", width="small"),
        "最大来源": st.column_config.TextColumn("最大来源", width="small"),
        "最大来源占比": st.column_config.NumberColumn("最大来源占比", format="%.2f%%", width="small"),
        "来源明细": st.column_config.TextColumn("来源明细", width="large"),
    }


def _display_us_stock_table(df: pd.DataFrame) -> pd.DataFrame:
    value_columns = ["代码", "名称", "涨跌幅", "最新价", "涨跌额", "成交额", "市值"]
    columns = ["序号", *value_columns]
    if df.empty:
        return pd.DataFrame(columns=columns)
    source = df.copy()
    for column in ["最新价", "涨跌幅", "涨跌额", "成交额", "市值"]:
        source[column] = pd.to_numeric(source.get(column), errors="coerce")
    source = source.sort_values("成交额", ascending=False).reset_index(drop=True)
    table = source[value_columns].copy()
    table.insert(0, "序号", range(1, len(table) + 1))
    table["涨跌幅"] = table["涨跌幅"].map(_format_us_stock_change_percent)
    table["成交额"] = table["成交额"].map(_format_usd_amount_zh)
    table["市值"] = table["市值"].map(_format_usd_amount_zh)
    return table


def _style_us_stock_table(table: pd.DataFrame) -> pd.io.formats.style.Styler:
    def row_style(row: pd.Series) -> list[str]:
        marker = str(row.get("关注") or "")
        if "强异动" in marker:
            return ["background-color: #ffe0e0"] * len(row)
        return [""] * len(row)

    return table.style.apply(row_style, axis=1)


def _build_us_stock_detail_html(
    row: pd.Series,
    data_date: str,
    metadata: dict[str, Any] | None = None,
    news: dict[str, Any] | None = None,
) -> str:
    details = serialize_anomaly_details(row)
    metadata = metadata or {}
    details["行情来源"] = str(row.get("source") or metadata.get("source") or "未记录")
    details["数据生成时间"] = str(row.get("数据生成时间") or metadata.get("generated_at") or "未记录")
    code = details["代码"] or "美股"
    multiple = details["成交额放大倍数"]
    metrics = [
        f"异动等级：{details['异动等级']}",
        f"命中标签：{details['异动标签'] or '-'}",
        f"涨跌幅：{details['涨跌幅'] if details['涨跌幅'] is not None else '-'}%",
        f"成交额放大：{multiple:.2f} 倍" if multiple is not None else "成交额放大：历史数据不足，未评估",
        f"排名变化：{details['排名变化'] if details['排名变化'] is not None else '-'}",
        f"异动阈值：{details['异动阈值'] or '-'}",
        f"行情来源：{details['行情来源']}；生成时间：{details['数据生成时间']}",
    ]
    metric_html = "".join(f"<div>{escape(str(metric))}</div>" for metric in metrics)
    news_html = "<div>新闻加载中…</div>"
    if news is None:
        pass
    elif news.get("status") == "missing_key":
        news_html = "<div>未配置 OPENROUTER_API_KEY，暂不加载新闻概览。</div>"
    elif news.get("status") != "ok":
        news_html = f"<div>{escape(str(news.get('message') or '新闻检索失败'))}</div>"
    elif news.get("articles"):
        articles: list[str] = []
        for article in news["articles"]:
            title = escape(str(article.get("title_zh") or "未命名新闻"))
            summary = escape(str(article.get("summary_zh") or ""))
            source = escape(str(article.get("source") or "未知来源"))
            published_at = escape(str(article.get("published_at") or ""))
            url = str(article.get("url") or "")
            link = f"<a href='{escape(url, quote=True)}' target='_blank'>查看原文</a>" if url.startswith(("http://", "https://")) else ""
            articles.append(f"<div class='news-item'><strong>{title}</strong><div>{summary}</div><small>{source} {published_at} {link}</small></div>")
        news_html = "".join(articles)
    return f"<div class='detail-card'><h4>{escape(code)} 异动详情</h4><div>{metric_html}</div><div><strong>当天新闻概览</strong>{news_html}</div></div>"


def _render_us_stock_detail(row: pd.Series, data_date: str, metadata: dict[str, Any] | None = None) -> None:
    details = serialize_anomaly_details(row)
    metadata = metadata or {}
    details["行情来源"] = str(row.get("source") or metadata.get("source") or "未记录")
    details["数据生成时间"] = str(row.get("数据生成时间") or metadata.get("generated_at") or "未记录")
    code = details["代码"] or "美股"
    with st.expander(f"{code} 当日异动详情", expanded=True):
        st.write(f"异动等级：{details['异动等级']}")
        st.write(f"命中标签：{details['异动标签']}")
        st.write(f"涨跌幅：{details['涨跌幅'] if details['涨跌幅'] is not None else '-'}%")
        multiple = details["成交额放大倍数"]
        st.write(f"成交额放大：{multiple:.2f} 倍" if multiple is not None else "成交额放大：历史数据不足，未评估")
        st.write(f"排名变化：{details['排名变化'] if details['排名变化'] is not None else '-'}")
        st.write(f"异动阈值：{details['异动阈值'] or '-'}")
        st.write(f"行情来源：{details['行情来源']}；生成时间：{details['数据生成时间']}")
        st.caption(str(details["异动数据状态"] or ""))
        if details["异动等级"] == "无异动":
            st.info("该股票当天未触发已配置的异动规则，暂不加载新闻概览。")
        else:
            st.divider()
            st.subheader("当天新闻概览")
            news = load_or_fetch_news(details["代码"], str(row.get("名称") or details["代码"]), data_date, root=ROOT)
            if news.get("status") == "missing_key":
                st.info("未配置 OPENROUTER_API_KEY，暂不加载新闻概览。")
            elif news.get("status") != "ok":
                st.warning(str(news.get("message") or "新闻检索失败"))
            elif not news.get("articles"):
                st.info("未检索到当天相关公开报道。")
            else:
                for article in news["articles"]:
                    title = str(article.get("title_zh") or "未命名新闻")
                    st.markdown(f"**{title}**")
                    st.write(str(article.get("summary_zh") or ""))
                    source = str(article.get("source") or "未知来源")
                    published_at = str(article.get("published_at") or "")
                    url = str(article.get("url") or "")
                    suffix = f" · {published_at}" if published_at else ""
                    st.caption(f"{source}{suffix}")
                    if url:
                        st.markdown(f"[查看原文]({url})")


def _us_stock_table_columns() -> dict[str, object]:
    return {
        "序号": st.column_config.NumberColumn("序号", format="%d", width="small"),
        "代码": st.column_config.TextColumn("代码", width="small"),
        "名称": st.column_config.TextColumn("名称", width="medium"),
        "最新价": st.column_config.NumberColumn("最新价", format="%.3f", width="small"),
        "涨跌幅": st.column_config.NumberColumn("涨跌幅", format="%+.2f%%", width="small"),
        "涨跌额": st.column_config.NumberColumn("涨跌额", format="%+.3f", width="small"),
        "成交额": st.column_config.TextColumn("成交额", width="small"),
        "市值": st.column_config.TextColumn("市值", width="small"),
    }


def _format_us_stock_change_percent(value: object) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "-"
    return f"{float(numeric):+.2f}%"


def _format_usd_amount_zh(value: object) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "-"
    amount = float(numeric)
    sign = "-" if amount < 0 else ""
    absolute = abs(amount)
    if absolute >= 1_000_000_000_000:
        return f"{sign}{_trim_decimal(absolute / 1_000_000_000_000)}万亿"
    if absolute >= 100_000_000:
        return f"{sign}{_trim_decimal(absolute / 100_000_000)}亿"
    if absolute >= 10_000:
        return f"{sign}{_trim_decimal(absolute / 10_000)}万"
    return f"{sign}{_trim_decimal(absolute)}"


def _format_usd_equivalent_zh(value: object) -> str:
    formatted = _format_usd_amount_zh(value)
    return "-" if formatted == "-" else f"{formatted} USD等值"


def _trim_decimal(value: float) -> str:
    text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".")


def _display_table(df: pd.DataFrame) -> pd.DataFrame:
    display_columns = [
        "排名",
        "ETF名称",
        "代码",
        "主线得分",
        "趋势状态",
        "成交额Top30次数",
        "成交额排名",
        "成交额(亿)",
        "20日涨幅",
        "60日涨幅",
        "120日涨幅",
        "5日份额变化",
        "20日份额变化",
        "数据质量",
        "数据置信度",
        "行情来源",
        "数据问题",
    ]
    source = df.copy()
    for column, default in {
        "数据质量": "未记录",
        "数据置信度": "未记录",
        "行情来源": "来源未记录",
        "数据问题": "历史缓存未包含数据质量字段，建议重拉当前日期",
    }.items():
        if column not in source:
            source[column] = default
    table = source[display_columns].rename(columns={"排名": "主线排名"}).copy()
    numeric_columns = [
        "主线排名",
        "主线得分",
        "成交额Top30次数",
        "成交额排名",
        "成交额(亿)",
        "20日涨幅",
        "60日涨幅",
        "120日涨幅",
        "5日份额变化",
        "20日份额变化",
    ]
    for column in numeric_columns:
        table[column] = pd.to_numeric(table[column], errors="coerce")
    return table


def _table_columns() -> dict[str, object]:
    return {
        "主线排名": st.column_config.NumberColumn("主线排名", format="%d", width="small", help=TERM_HELP["主线排名"]),
        "主线得分": st.column_config.ProgressColumn(
            "主线得分", min_value=0, max_value=100, format="%.1f", width="small", help=TERM_HELP["主线得分"]
        ),
        "ETF名称": st.column_config.TextColumn("ETF名称", width="medium"),
        "代码": st.column_config.TextColumn("代码", width="small"),
        "趋势状态": st.column_config.TextColumn("趋势状态", width="small", help=TERM_HELP["趋势状态"]),
        "成交额Top30次数": st.column_config.NumberColumn("Top30次数", width="small", help=TERM_HELP["成交额Top30次数"]),
        "成交额排名": st.column_config.NumberColumn("成交额排名", format="%d", width="small", help=TERM_HELP["成交额排名"]),
        "20日涨幅": st.column_config.NumberColumn("20日涨幅", format="%+.2f%%", width="small", help=TERM_HELP["20日涨幅"]),
        "60日涨幅": st.column_config.NumberColumn("60日涨幅", format="%+.2f%%", width="small", help=TERM_HELP["60日涨幅"]),
        "120日涨幅": st.column_config.NumberColumn("120日涨幅", format="%+.2f%%", width="small", help=TERM_HELP["120日涨幅"]),
        "5日份额变化": st.column_config.NumberColumn("5日份额变化", format="%+.2f%%", width="small", help=TERM_HELP["5日份额变化"]),
        "20日份额变化": st.column_config.NumberColumn("20日份额变化", format="%+.2f%%", width="small", help=TERM_HELP["20日份额变化"]),
        "成交额(亿)": st.column_config.NumberColumn("成交额(亿)", format="%.2f", width="small", help=TERM_HELP["成交额(亿)"]),
        "数据质量": st.column_config.TextColumn("数据质量", width="small", help=TERM_HELP["数据质量"]),
        "数据置信度": st.column_config.TextColumn("数据置信度", width="small", help=TERM_HELP["数据置信度"]),
        "行情来源": st.column_config.TextColumn("行情来源", width="medium", help=TERM_HELP["行情来源"]),
        "数据问题": st.column_config.TextColumn("数据问题", width="large", help=TERM_HELP["数据问题"]),
    }


def _resolve_selected_code(
    snapshot: pd.DataFrame,
    top10: pd.DataFrame,
    filtered: pd.DataFrame,
    top10_event: object,
    ranking_event: object,
    selected_date: str,
    state: MutableMapping[str, object],
    detail_options: dict[str, str],
    selectbox_key: str,
) -> str:
    if not detail_options:
        return ""

    clicked_code = _clicked_table_code(top10, filtered, top10_event, ranking_event, selected_date, state)
    if clicked_code:
        _set_detail_state(state, detail_options, selectbox_key, clicked_code)
        return clicked_code

    selected_label = state.get(selectbox_key)
    if isinstance(selected_label, str) and selected_label in detail_options:
        selected_code = detail_options[selected_label]
        state[DETAIL_CODE_STATE_KEY] = selected_code
        return selected_code

    selected_code = str(state.get(DETAIL_CODE_STATE_KEY, ""))
    if selected_code in set(detail_options.values()):
        _set_detail_state(state, detail_options, selectbox_key, selected_code)
        return selected_code

    fallback_code = str(snapshot.iloc[0]["代码"]) if not snapshot.empty else next(iter(detail_options.values()))
    _set_detail_state(state, detail_options, selectbox_key, fallback_code)
    return fallback_code


def _clicked_table_code(
    top10: pd.DataFrame,
    filtered: pd.DataFrame,
    top10_event: object,
    ranking_event: object,
    selected_date: str,
    state: MutableMapping[str, object],
) -> str | None:
    clicked_code: str | None = None

    top10_rows = _selection_rows(top10_event)
    top10_signature = (selected_date, top10_rows)
    if top10_rows and top10_signature != state.get(TOP10_SELECTION_STATE_KEY) and top10_rows[0] < len(top10):
        clicked_code = str(top10.iloc[top10_rows[0]]["代码"])

    ranking_rows = _selection_rows(ranking_event)
    ranking_signature = (selected_date, ranking_rows)
    if (
        ranking_rows
        and ranking_signature != state.get(RANKING_SELECTION_STATE_KEY)
        and not filtered.empty
        and ranking_rows[0] < len(filtered)
    ):
        clicked_code = str(filtered.iloc[ranking_rows[0]]["代码"])

    state[TOP10_SELECTION_STATE_KEY] = top10_signature
    state[RANKING_SELECTION_STATE_KEY] = ranking_signature
    return clicked_code


def _selection_rows(event: object) -> tuple[int, ...]:
    rows = getattr(getattr(event, "selection", None), "rows", [])
    return tuple(int(row) for row in rows)


def _set_detail_state(
    state: MutableMapping[str, object],
    detail_options: dict[str, str],
    selectbox_key: str,
    selected_code: str,
) -> None:
    state[DETAIL_CODE_STATE_KEY] = selected_code
    selected_label = _detail_label_for_code(detail_options, selected_code)
    if selected_label:
        state[selectbox_key] = selected_label


def _detail_options(df: pd.DataFrame) -> dict[str, str]:
    if df.empty:
        return {}
    return {f"{row['排名']:02d} {row['ETF名称']} {row['代码']}": str(row["代码"]) for _, row in df.iterrows()}


def _detail_label_for_code(detail_options: dict[str, str], selected_code: str) -> str | None:
    for label, code in detail_options.items():
        if code == selected_code:
            return label
    return None


def _price_chart(history: pd.DataFrame, name: str) -> go.Figure:
    fig = go.Figure()
    dates = history["日期"]
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=history["开盘价"],
            high=history["最高价"],
            low=history["最低价"],
            close=history["收盘价"],
            name=name,
            increasing_line_color="#b13f2d",
            increasing_fillcolor="#b13f2d",
            decreasing_line_color="#1f6f78",
            decreasing_fillcolor="#1f6f78",
        )
    )
    ema_colors = {
        "EMA13": "#d14b2f",
        "EMA26": "#2b7a78",
        "EMA144": "#5b5f97",
        "EMA169": "#7a6f3f",
    }
    for column, color in ema_colors.items():
        fig.add_trace(
            go.Scatter(x=dates, y=history[column], name=column, line=dict(color=color, width=1.35))
        )
    fig.add_trace(
        go.Bar(
            x=dates,
            y=_volume_for_chart(history),
            name="成交量",
            marker_color="#8a7b5c",
            opacity=0.58,
            yaxis="y2",
            hovertemplate="成交量 %{y:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        title="价格走势与EMA结构",
        height=405,
        margin=dict(l=20, r=16, t=48, b=24),
        legend=dict(orientation="h", y=1.08, x=0),
        xaxis=dict(anchor="y2", rangeslider=dict(visible=False), rangebreaks=_trading_date_rangebreaks(dates)),
        yaxis=dict(domain=[0.27, 1.0], title="价格"),
        yaxis2=dict(domain=[0.0, 0.19], title="成交量", anchor="x", showgrid=False, zeroline=False),
        bargap=0,
    )
    return fig


def _volume_for_chart(history: pd.DataFrame) -> pd.Series:
    volume = pd.to_numeric(history.get("成交量", pd.Series(pd.NA, index=history.index)), errors="coerce")
    if not {"成交额(亿)", "收盘价"}.issubset(history.columns):
        return volume

    amount = pd.to_numeric(history["成交额(亿)"], errors="coerce")
    close = pd.to_numeric(history["收盘价"], errors="coerce")
    derived = (amount * 100_000_000 / close / 100).where(close > 0)
    return volume.fillna(derived)


def _trading_date_rangebreaks(dates: pd.Series) -> list[dict[str, object]]:
    parsed = pd.Series(pd.to_datetime(dates, errors="coerce")).dropna()
    if parsed.empty:
        return []

    actual_days = pd.DatetimeIndex(parsed.dt.normalize().drop_duplicates()).sort_values()
    business_days = pd.bdate_range(actual_days.min(), actual_days.max())
    missing_weekdays = business_days.difference(actual_days)
    rangebreaks: list[dict[str, object]] = [{"bounds": ["sat", "mon"]}]
    if not missing_weekdays.empty:
        rangebreaks.append({"values": [day.strftime("%Y-%m-%d") for day in missing_weekdays]})
    return rangebreaks


def _score_chart(score_breakdown: pd.DataFrame) -> go.Figure:
    help_text = score_breakdown["维度"].map(lambda dimension: TERM_HELP.get(str(dimension), ""))
    fig = go.Figure(
        go.Bar(
            x=score_breakdown["得分"],
            y=score_breakdown["维度"],
            orientation="h",
            marker=dict(color=["#b13f2d", "#2b7a78", "#5b5f97", "#7a6f3f"]),
            text=score_breakdown["权重"],
            textposition="outside",
            customdata=help_text,
            hovertemplate="%{y}<br>得分 %{x:.1f}<br>权重 %{text}<br>%{customdata}<extra></extra>",
        )
    )
    fig.update_layout(
        title="评分拆解",
        height=330,
        xaxis=dict(range=[0, 105]),
        margin=dict(l=20, r=32, t=48, b=20),
    )
    return fig


def _amount_chart(history: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Bar(x=history["日期"], y=history["成交额(亿)"], marker_color="#8f3f2a", name="成交额"))
    fig.update_layout(title="成交额历史", height=285, margin=dict(l=20, r=16, t=44, b=20))
    return fig


def _share_chart(history: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=history["日期"],
            y=history["ETF份额(亿份)"],
            mode="lines",
            line=dict(color="#1f6f78", width=2.2),
            name="ETF份额",
        )
    )
    fig.update_layout(title="份额变化历史", height=285, margin=dict(l=20, r=16, t=44, b=20))
    return fig


def _pct(value: object) -> str:
    number = float(value)
    return f"{number:+.2f}%"


def _apply_style() -> None:
    st.markdown(
        """
        <style>
        .block-container {
          padding-top: 1.05rem;
          padding-bottom: 2.5rem;
          max-width: 1500px;
        }
        h1 {
          font-size: 1.82rem !important;
          line-height: 1.15 !important;
          margin-bottom: 0.2rem !important;
          letter-spacing: 0 !important;
        }
        h2, h3 {
          letter-spacing: 0 !important;
        }
        div[data-testid="stMetric"] {
          background: #ffffff;
          border: 1px solid #d8dde6;
          border-radius: 6px;
          padding: 0.66rem 0.74rem;
        }
        div[data-testid="stMetric"] label {
          color: #4b5563;
        }
        .change-card {
          background: #ffffff;
          border: 1px solid #d8dde6;
          border-left-width: 5px;
          border-radius: 6px;
          margin-bottom: 0.58rem;
          padding: 0.68rem 0.76rem;
        }
        .change-new {
          border-left-color: #b13f2d;
        }
        .change-keep {
          border-left-color: #1f6f78;
        }
        .change-drop {
          border-left-color: #7a6f3f;
        }
        .change-label {
          font-size: 0.82rem;
          color: #4b5563;
          margin-bottom: 0.24rem;
        }
        .change-names {
          font-size: 1rem;
          color: #111827;
          line-height: 1.35;
          word-break: break-word;
        }
        .reason-line {
          background: #fbfbf8;
          border: 1px solid #d8dde6;
          border-radius: 6px;
          padding: 0.7rem 0.8rem;
          margin-bottom: 0.75rem;
          color: #1f2937;
        }
        [data-testid="stDataFrame"] {
          border: 1px solid #d8dde6;
          border-radius: 6px;
        }
        textarea.gdg-input,
        input.gdg-input,
        .gdg-style.gdg-pad,
        .gdg-clip-region {
          display: none !important;
          opacity: 0 !important;
          pointer-events: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
