from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from trend_radar.config import DEFAULT_HISTORY_FETCH_MAX_WORKERS  # noqa: E402


def _install_app_import_stubs() -> None:
    try:
        import streamlit  # noqa: F401
        import plotly.graph_objects  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    class _ColumnConfig:
        @staticmethod
        def NumberColumn(label: str, **kwargs: object) -> dict[str, object]:
            return {"label": label, "help": kwargs.get("help"), "type_config": {"type": "number", **kwargs}}

        @staticmethod
        def ProgressColumn(label: str, **kwargs: object) -> dict[str, object]:
            return {"label": label, "help": kwargs.get("help"), "type_config": {"type": "progress", **kwargs}}

        @staticmethod
        def TextColumn(label: str, **kwargs: object) -> dict[str, object]:
            return {"label": label, "help": kwargs.get("help"), "type_config": {"type": "text", **kwargs}}

    streamlit_stub = types.ModuleType("streamlit")
    streamlit_stub.__path__ = []
    streamlit_stub.column_config = _ColumnConfig()

    components_pkg = types.ModuleType("streamlit.components")
    components_pkg.__path__ = []
    components_v1 = types.ModuleType("streamlit.components.v1")
    components_v1.html = lambda *args, **kwargs: None

    plotly_pkg = types.ModuleType("plotly")
    plotly_pkg.__path__ = []
    graph_objects = types.ModuleType("plotly.graph_objects")
    graph_objects.Figure = object
    graph_objects.Scatter = object
    graph_objects.Candlestick = object
    graph_objects.Bar = object

    sys.modules.setdefault("streamlit", streamlit_stub)
    sys.modules.setdefault("streamlit.components", components_pkg)
    sys.modules.setdefault("streamlit.components.v1", components_v1)
    sys.modules.setdefault("plotly", plotly_pkg)
    sys.modules.setdefault("plotly.graph_objects", graph_objects)


_install_app_import_stubs()

import app  # noqa: E402
from helpers.data_factory import generate_radar_fixture  # noqa: E402
from trend_radar.ui_data import load_snapshot  # noqa: E402


DATA_DATE = "2026-05-28"
NUMERIC_CONTRACT_COLUMNS = [
    "主线得分",
    "成交额(亿)",
    "成交额排名",
    "20日涨幅",
    "60日涨幅",
    "120日涨幅",
    "5日份额变化",
    "20日份额变化",
]
DISPLAY_NUMERIC_COLUMNS = [
    "主线排名",
    "主线得分",
    "成交额Top30次数",
    "成交额排名",
    "20日涨幅",
    "60日涨幅",
    "120日涨幅",
    "5日份额变化",
    "20日份额变化",
    "成交额(亿)",
]
DISPLAY_TEXT_COLUMNS = ["数据质量", "数据置信度", "行情来源", "数据问题"]
REQUIRED_TERM_HELP_KEYS = [
    "数据状态",
    "头部主线",
    "Top10均分",
    "强趋势数量",
    "新进Top10",
    "趋势状态",
    "排序",
    "明细来源",
    "ETF明细",
    "主线排名",
    "主线得分",
    "成交额Top30次数",
    "成交额Top30",
    "成交额排名",
    "20日涨幅",
    "60日涨幅",
    "120日涨幅",
    "5日份额变化",
    "20日份额变化",
    "成交额(亿)",
    "数据质量",
    "数据置信度",
    "行情来源",
    "份额来源",
    "数据问题",
    "份额变化",
    "成交额热度",
    "份额流入",
    "趋势强度",
    "趋势结构",
]
TABLE_HELP_COLUMNS = [
    "主线排名",
    "主线得分",
    "趋势状态",
    "成交额Top30次数",
    "成交额排名",
    "20日涨幅",
    "60日涨幅",
    "120日涨幅",
    "5日份额变化",
    "20日份额变化",
    "成交额(亿)",
    "数据质量",
    "数据置信度",
    "行情来源",
    "数据问题",
]


class FunctionalContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        generate_radar_fixture(self.root, DATA_DATE)
        self.radar_csv = self.root / "data" / "radar" / f"{DATA_DATE}.csv"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_real_cache_keeps_numeric_contract_columns(self) -> None:
        frame = pd.read_csv(self.radar_csv, dtype={"代码": str})

        for column in NUMERIC_CONTRACT_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, frame.columns)
                self.assertTrue(pd.api.types.is_numeric_dtype(frame[column]), f"{column} is {frame[column].dtype}")

    def test_load_snapshot_defaults_to_mainline_rank_order(self) -> None:
        snapshot = load_snapshot(DATA_DATE, root=self.root)
        expected = pd.read_csv(self.radar_csv, dtype={"代码": str}).sort_values("排名").reset_index(drop=True)

        self.assertEqual(snapshot["排名"].tolist(), sorted(snapshot["排名"].tolist()))
        self.assertEqual(snapshot["代码"].head(20).tolist(), expected["代码"].head(20).tolist())

    def test_amount_sort_matches_csv_numeric_descending_order(self) -> None:
        snapshot = load_snapshot(DATA_DATE, root=self.root)
        actual = app._sort_snapshot_for_display(snapshot, "成交额").head(20)["代码"].tolist()
        expected = (
            pd.read_csv(self.radar_csv, dtype={"代码": str})
            .sort_values("成交额(亿)", ascending=False)
            .head(20)["代码"]
            .tolist()
        )

        self.assertEqual(actual, expected)

    def test_amount_rank_sort_is_monotonic_and_matches_amount_direction(self) -> None:
        snapshot = load_snapshot(DATA_DATE, root=self.root)
        sorted_frame = app._sort_snapshot_for_display(snapshot, "成交额排名")

        self.assertTrue(sorted_frame["成交额排名"].is_monotonic_increasing)
        self.assertTrue(sorted_frame["成交额(亿)"].is_monotonic_decreasing)

    def test_display_table_labels_mainline_rank_and_keeps_sort_columns_numeric(self) -> None:
        snapshot = load_snapshot(DATA_DATE, root=self.root)
        table = app._display_table(snapshot.head(10))

        self.assertIn("主线排名", table.columns)
        self.assertNotIn("排名", table.columns)
        self.assertEqual(
            list(table.columns)[list(table.columns).index("成交额排名") + 1],
            "成交额(亿)",
        )
        for column in DISPLAY_NUMERIC_COLUMNS:
            with self.subTest(column=column):
                self.assertTrue(pd.api.types.is_numeric_dtype(table[column]), f"{column} is {table[column].dtype}")
        for column in DISPLAY_TEXT_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, table.columns)
                self.assertFalse(pd.api.types.is_numeric_dtype(table[column]), f"{column} is {table[column].dtype}")

    def test_table_columns_use_numeric_display_formatting(self) -> None:
        columns = app._table_columns()

        self.assertEqual(columns["主线得分"]["type_config"]["type"], "progress")
        self.assertEqual(columns["主线得分"]["type_config"]["format"], "%.1f")
        self.assertEqual(columns["主线排名"]["type_config"]["format"], "%d")
        self.assertEqual(columns["成交额排名"]["type_config"]["format"], "%d")
        self.assertEqual(columns["成交额(亿)"]["type_config"]["format"], "%.2f")
        for column in DISPLAY_TEXT_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(columns[column]["type_config"]["type"], "text")
        for column in ["20日涨幅", "60日涨幅", "120日涨幅", "5日份额变化", "20日份额变化"]:
            with self.subTest(column=column):
                self.assertEqual(columns[column]["type_config"]["type"], "number")
                self.assertEqual(columns[column]["type_config"]["format"], "%+.2f%%")

    def test_term_help_contains_required_core_copy(self) -> None:
        for key in REQUIRED_TERM_HELP_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, app.TERM_HELP)
                self.assertTrue(app.TERM_HELP[key].strip())

        self.assertIn(">=88 强趋势", app.TERM_HELP["趋势状态"])
        self.assertIn("成交额热度 45%", app.TERM_HELP["主线得分"])
        self.assertIn("全候选 A股权益 ETF", app.TERM_HELP["成交额Top30次数"])
        self.assertIn("45%/35%/20%", app.TERM_HELP["趋势强度"])
        self.assertIn("MA20 > MA60 > MA120", app.TERM_HELP["趋势结构"])

    def test_table_columns_include_core_help_text(self) -> None:
        columns = app._table_columns()

        for column in TABLE_HELP_COLUMNS:
            with self.subTest(column=column):
                self.assertIn("help", columns[column])
                self.assertEqual(columns[column]["help"], app.TERM_HELP[column])

    def test_us_stock_display_table_matches_requested_fields_and_order(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "代码": "NVDA",
                    "名称": "NVIDIA Corporation",
                    "最新价": 222.82,
                    "涨跌幅": -0.686,
                    "涨跌额": -1.54,
                    "成交额": 42_824_000_000.0,
                    "市值": 5_392_244_000_000.0,
                },
                {
                    "代码": "MU",
                    "名称": "Micron Technology",
                    "最新价": 1064.10,
                    "涨跌幅": 2.762,
                    "涨跌额": 28.60,
                    "成交额": 50_107_000_000.0,
                    "市值": 1_200_021_803_669.0,
                },
            ]
        )

        table = app._display_us_stock_table(frame)

        self.assertEqual(table.columns.tolist(), ["序号", "代码", "名称", "涨跌幅", "最新价", "涨跌额", "成交额", "市值"])
        self.assertEqual(table["序号"].tolist(), [1, 2])
        self.assertEqual(table["代码"].tolist(), ["MU", "NVDA"])
        self.assertEqual(table.iloc[0]["成交额"], "501.07亿")
        self.assertEqual(table.iloc[0]["市值"], "1.2万亿")
        self.assertEqual(table["涨跌幅"].tolist(), ["+2.76%", "-0.69%"])

    def test_us_stock_display_hides_attention_column_even_with_anomalies(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "代码": "LITE",
                    "名称": "Lumentum",
                    "最新价": 978.535,
                    "涨跌幅": 11.04,
                    "涨跌额": 97.23,
                    "成交额": 12_300_000_000.0,
                    "市值": 50_000_000_000.0,
                    "异动等级": "强异动",
                }
            ]
        )

        table = app._display_us_stock_table(frame)

        self.assertNotIn("关注", table.columns)
        self.assertEqual(table.loc[0, "涨跌幅"], "+11.04%")

    def test_us_stock_table_columns_use_requested_formatting(self) -> None:
        columns = app._us_stock_table_columns()

        self.assertEqual(columns["序号"]["type_config"]["format"], "%d")
        self.assertEqual(columns["最新价"]["type_config"]["format"], "%.3f")
        self.assertEqual(columns["涨跌幅"]["type_config"]["format"], "%+.2f%%")
        self.assertEqual(columns["涨跌额"]["type_config"]["format"], "%+.3f")
        self.assertEqual(columns["成交额"]["type_config"]["type"], "text")
        self.assertEqual(columns["市值"]["type_config"]["type"], "text")
        self.assertNotIn("关注", columns)

    def test_us_stock_component_formats_prices_and_changes_without_float_tails(self) -> None:
        frame = pd.DataFrame([
            {"代码": code, "名称": code, "最新价": price, "涨跌额": change,
             "涨跌幅": 0, "成交额": 100, "市值": 1000}
            for code, price, change in [
                ("NVDA", 221.3, 221.3 - 228.52),
                ("AAPL", 342.96, 342.96 - 341.27),
                ("ZERO", 10, 0),
                ("MISSING", None, None),
                ("LITE", 978.535, 0.125),
            ]
        ])

        rows = app._build_us_stock_component_rows(app._display_us_stock_table(frame))
        by_code = {row["代码"]: row for row in rows}

        self.assertEqual(by_code["NVDA"]["涨跌额"], "-7.220")
        self.assertEqual(by_code["AAPL"]["涨跌额"], "+1.690")
        self.assertEqual(by_code["ZERO"]["涨跌额"], "+0.000")
        self.assertEqual(by_code["MISSING"]["涨跌额"], "")
        self.assertEqual(by_code["MISSING"]["最新价"], "")
        self.assertEqual(by_code["NVDA"]["最新价"], "221.300")
        self.assertEqual(by_code["LITE"]["最新价"], "978.535")
        self.assertEqual(by_code["LITE"]["涨跌额"], "+0.125")

    def test_us_stock_interaction_only_toggles_strong_anomalies(self) -> None:
        table = pd.DataFrame(
            [
                {"序号": 1, "代码": "LITE", "名称": "鲁门特姆", "最新价": "978.535", "涨跌幅": "+11.04%", "涨跌额": "+97.23", "成交额": "123.00亿", "市值": "500.00亿"},
                {"序号": 2, "代码": "NVDA", "名称": "英伟达", "最新价": "225.730", "涨跌幅": "-2.01%", "涨跌额": "-4.63", "成交额": "276.66亿", "市值": "5.44万亿"},
            ]
        )

        snapshot = pd.DataFrame([{"代码": "LITE", "异动等级": "强异动"}, {"代码": "NVDA", "异动等级": "无异动"}])

        rows = app._build_us_stock_component_rows(table)
        self.assertEqual(rows[0]["代码"], "LITE")
        self.assertEqual(app._resolve_us_stock_interaction({"type": "toggle", "code": "LITE"}, "", snapshot), "LITE")
        self.assertEqual(app._resolve_us_stock_interaction({"type": "toggle", "code": "LITE"}, "LITE", snapshot), "")
        self.assertEqual(app._resolve_us_stock_interaction({"type": "toggle", "code": "NVDA"}, "", snapshot), "")

    def test_us_stock_component_rows_expose_only_enabled_anomaly_decoration(self) -> None:
        table = pd.DataFrame(
            [
                {"序号": 1, "代码": "STRONG", "名称": "强异动", "涨跌幅": "+8.00%"},
                {"序号": 2, "代码": "WATCH", "名称": "关注级", "涨跌幅": "+4.00%"},
                {"序号": 3, "代码": "NONE", "名称": "无异动", "涨跌幅": "+1.00%"},
            ]
        )
        levels = {"STRONG": "强异动", "WATCH": "关注", "NONE": "无异动"}

        defaults = app._build_us_stock_component_rows(table, levels)
        self.assertEqual([row["_anomaly_display"] for row in defaults], ["强异动", "", ""])

        both = app._build_us_stock_component_rows(table, levels, show_strong=True, show_attention=True)
        self.assertEqual([row["_anomaly_display"] for row in both], ["强异动", "关注", ""])

        attention_only = app._build_us_stock_component_rows(table, levels, show_strong=False, show_attention=True)
        self.assertEqual([row["_anomaly_display"] for row in attention_only], ["", "关注", ""])

    def test_us_stock_component_renders_anomaly_badges_and_row_classes(self) -> None:
        component = (PROJECT_ROOT / "src/trend_radar/components/us_stock_table/index.html").read_text(encoding="utf-8")

        self.assertIn('tr.className = `anomaly-${displayLevel}`', component)
        self.assertIn('badge.textContent = displayLevel', component)
        self.assertIn('cell.appendChild(badge)', component)
        self.assertIn('row["_anomaly_display"]', component)

    def test_us_stock_component_exposes_copy_and_expand_events(self) -> None:
        component = (PROJECT_ROOT / "src/trend_radar/components/us_stock_table/index.html").read_text(encoding="utf-8")

        self.assertIn('addEventListener("dblclick", () => copyCode(code))', component)
        self.assertIn('addEventListener("dblclick", () => toggle(code))', component)
        self.assertIn('streamlit:setComponentValue', component)
        self.assertNotIn('detail-button', component)
        self.assertNotIn('"关注"', component)
        self.assertIn('new Set(currentArgs.strong_codes || [])', component)
        self.assertIn('const columns = ["序号", "代码", "名称", "涨跌幅", "最新价", "涨跌额", "成交额", "市值"]', component)

    def test_us_stock_detail_renders_before_news_fetch_completes(self) -> None:
        row = pd.Series({"代码": "LITE", "名称": "鲁门特姆", "异动等级": "强异动", "异动标签": "价格强异动", "涨跌幅": 11.0})

        detail_html = app._build_us_stock_detail_html(row, "2026-09-08", news=None)

        self.assertIn("异动等级：强异动", detail_html)
        self.assertIn("新闻加载中…", detail_html)

    def test_us_stock_display_date_prefers_latest_target_then_status_actual(self) -> None:
        self.assertEqual(app._resolve_us_stock_display_date("2026-06-03", ["2026-06-02", "2026-06-03"], {}), "2026-06-03")
        self.assertEqual(
            app._resolve_us_stock_display_date("2026-06-03", ["2026-06-02"], {"actual_date": "2026-06-02"}),
            "2026-06-02",
        )

    def test_market_options_hide_a_share_etf(self) -> None:
        self.assertEqual(app.MARKET_OPTIONS, ["美股", "币圈现货"])

    def test_crypto_display_table_matches_requested_fields_and_order(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "币种": "ETH",
                    "30日成交额": 8_000_000_000.0,
                    "日均成交额": 266_666_666.67,
                    "交易所数": 4,
                    "交易对数": 5,
                    "最大来源": "Coinbase",
                    "最大来源占比": 40.5,
                    "来源明细": "Coinbase 40.5% / Binance 30.0%",
                },
                {
                    "币种": "BTC",
                    "30日成交额": 12_340_000_000.0,
                    "日均成交额": 411_333_333.33,
                    "交易所数": 5,
                    "交易对数": 6,
                    "最大来源": "Binance",
                    "最大来源占比": 45.2,
                    "来源明细": "Binance 45.2% / Coinbase 20.0%",
                },
            ]
        )

        table = app._display_crypto_table(frame)

        self.assertEqual(
            table.columns.tolist(),
            ["排名", "币种", "30日成交额", "日均成交额", "交易所数", "交易对数", "最大来源", "最大来源占比", "来源明细"],
        )
        self.assertEqual(table["排名"].tolist(), [1, 2])
        self.assertEqual(table["币种"].tolist(), ["BTC", "ETH"])
        self.assertEqual(table.iloc[0]["30日成交额"], "123.4亿 USD等值")
        self.assertEqual(table.iloc[0]["日均成交额"], "4.11亿 USD等值")

    def test_crypto_display_table_excludes_symbols_outside_fixed_alert_universe(self) -> None:
        frame = pd.DataFrame([
            {"币种": "XRP", "30日成交额": 30.0, "日均成交额": 1.0, "交易所数": 1, "交易对数": 1, "最大来源": "Binance", "最大来源占比": 100.0, "来源明细": "Binance 100%"},
            {"币种": "BTC", "30日成交额": 20.0, "日均成交额": 1.0, "交易所数": 1, "交易对数": 1, "最大来源": "Binance", "最大来源占比": 100.0, "来源明细": "Binance 100%"},
        ])

        table = app._display_crypto_table(frame)

        self.assertEqual(table["币种"].tolist(), ["BTC"])

    def test_crypto_table_columns_use_requested_formatting(self) -> None:
        columns = app._crypto_table_columns()

        self.assertEqual(columns["排名"]["type_config"]["format"], "%d")
        self.assertEqual(columns["30日成交额"]["type_config"]["type"], "text")
        self.assertEqual(columns["日均成交额"]["type_config"]["type"], "text")
        self.assertEqual(columns["交易所数"]["type_config"]["format"], "%d")
        self.assertEqual(columns["交易对数"]["type_config"]["format"], "%d")
        self.assertEqual(columns["最大来源占比"]["type_config"]["format"], "%.2f%%")

    def test_crypto_source_caption_is_honest_for_binance_only_source(self) -> None:
        self.assertEqual(
            app._crypto_source_caption({"source_mode": "binance_public_fallback", "successful_exchanges": ["binance_vision"]}),
            "Binance 现货滚动30日成交额 | USD系报价近似等值 | 不含交易建议",
        )
        self.assertEqual(
            app._crypto_source_caption({"source_mode": "multi_exchange_public", "successful_exchanges": ["binance", "coinbase"]}),
            "多交易所现货成交额聚合 | USD系报价近似等值 | 不含交易建议",
        )

    def test_table_clicks_update_shared_detail_selection(self) -> None:
        snapshot = load_snapshot(DATA_DATE, root=self.root)
        top10 = snapshot.head(10).copy()
        filtered = app._sort_snapshot_for_display(snapshot, "成交额")
        detail_options = app._detail_options(snapshot)
        selectbox_key = f"detail-select-{DATA_DATE}"
        state: dict[str, object] = {}

        top10_code = app._resolve_selected_code(
            snapshot,
            top10,
            filtered,
            _selection_event([1]),
            _selection_event([]),
            DATA_DATE,
            state,
            detail_options,
            selectbox_key,
        )
        self.assertEqual(top10_code, str(top10.iloc[1]["代码"]))

        ranking_code = app._resolve_selected_code(
            snapshot,
            top10,
            filtered,
            _selection_event([1]),
            _selection_event([2]),
            DATA_DATE,
            state,
            detail_options,
            selectbox_key,
        )
        self.assertEqual(ranking_code, str(filtered.iloc[2]["代码"]))
        self.assertEqual(state[app.DETAIL_CODE_STATE_KEY], ranking_code)

    def test_detail_selectbox_state_can_drive_selection_without_table_click(self) -> None:
        snapshot = load_snapshot(DATA_DATE, root=self.root)
        detail_options = app._detail_options(snapshot)
        selectbox_key = f"detail-select-{DATA_DATE}"
        labels = list(detail_options)
        state: dict[str, object] = {selectbox_key: labels[3]}

        selected_code = app._resolve_selected_code(
            snapshot,
            snapshot.head(10),
            snapshot,
            _selection_event([]),
            _selection_event([]),
            DATA_DATE,
            state,
            detail_options,
            selectbox_key,
        )

        self.assertEqual(selected_code, detail_options[labels[3]])
        self.assertEqual(state[app.DETAIL_CODE_STATE_KEY], selected_code)

    def test_price_chart_uses_candlestick_four_emas_volume_and_rangebreaks(self) -> None:
        if app.go.Figure is object:
            self.skipTest("plotly is not installed; import stub cannot inspect traces")
        history = pd.DataFrame(
            {
                "日期": pd.to_datetime(["2026-05-22", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]),
                "开盘价": [1.0, 1.1, 1.2, 1.15, 1.18],
                "最高价": [1.12, 1.22, 1.25, 1.2, 1.24],
                "最低价": [0.98, 1.05, 1.12, 1.1, 1.14],
                "收盘价": [1.1, 1.2, 1.15, 1.18, 1.22],
                "成交量": [10000, 12000, 11000, 13000, 12500],
                "EMA13": [1.1, 1.11, 1.12, 1.13, 1.14],
                "EMA26": [1.1, 1.105, 1.11, 1.12, 1.13],
                "EMA144": [1.1, 1.101, 1.103, 1.105, 1.107],
                "EMA169": [1.1, 1.101, 1.102, 1.104, 1.106],
            }
        )

        fig = app._price_chart(history, "fixture")

        self.assertEqual([trace.type for trace in fig.data], ["candlestick", "scatter", "scatter", "scatter", "scatter", "bar"])
        self.assertEqual(fig.data[-1].name, "成交量")
        self.assertFalse(fig.layout.xaxis.rangeslider.visible)
        self.assertEqual(app._trading_date_rangebreaks(history["日期"])[1]["values"], ["2026-05-25"])

    def test_refresh_status_view_contract(self) -> None:
        available = ["2026-05-28", "2026-05-29"]

        running = app._refresh_status_view(
            {"status": "running", "target_date": "2026-05-29"},
            "2026-05-28",
            available,
        )
        self.assertEqual(running["message"], "正在更新 2026-05-29 数据...")
        self.assertFalse(running["show_refresh_button"])

        running_progress = app._refresh_status_view(
            {
                "status": "running",
                "target_date": "2026-05-29",
                "phase": "fetch_history",
                "updated_at": "2026-05-30 09:00:00",
                "progress": {
                    "completed": 12,
                    "total": 455,
                    "current_code": "560580",
                    "current_name": "测试ETF",
                },
            },
            "2026-05-28",
            available,
            now=datetime(2026, 5, 30, 9, 1, tzinfo=app.ASIA_SHANGHAI),
        )
        self.assertEqual(running_progress["message"], "正在更新 2026-05-29 数据：历史行情 12/455，当前 560580")
        self.assertEqual(running_progress["progress_completed"], 12)
        self.assertEqual(running_progress["progress_total"], 455)
        self.assertFalse(running_progress["is_stale"])

        parallel_progress = app._refresh_status_view(
            {
                "status": "running",
                "target_date": "2026-05-29",
                "phase": "fetch_history",
                "updated_at": "2026-05-30 09:00:00",
                "progress": {
                    "completed": 36,
                    "total": 455,
                    "current_code": "512290",
                    "skipped_cached": 24,
                    "fetched": 12,
                    "errors": 0,
                    "max_workers": DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                },
            },
            "2026-05-28",
            available,
            now=datetime(2026, 5, 30, 9, 1, tzinfo=app.ASIA_SHANGHAI),
        )
        self.assertEqual(
            parallel_progress["message"],
            (
                "正在更新 2026-05-29 数据：历史行情 36/455，缓存 24，新拉取 12，"
                f"失败 0，并发 {DEFAULT_HISTORY_FETCH_MAX_WORKERS}，当前 512290"
            ),
        )

        stale_progress = app._refresh_status_view(
            {
                "status": "running",
                "target_date": "2026-05-29",
                "phase": "fetch_history",
                "updated_at": "2026-05-30 09:00:00",
                "progress": {"completed": 12, "total": 455, "current_code": "560580"},
            },
            "2026-05-28",
            available,
            now=datetime(2026, 5, 30, 9, 4, 1, tzinfo=app.ASIA_SHANGHAI),
        )
        self.assertTrue(stale_progress["is_stale"])

        succeeded = app._refresh_status_view(
            {"status": "succeeded", "target_date": "2026-05-29", "actual_date": "2026-05-29"},
            "2026-05-28",
            available,
        )
        self.assertEqual(succeeded["level"], "success")
        self.assertEqual(succeeded["message"], "已生成 2026-05-29 数据")
        self.assertTrue(succeeded["show_refresh_button"])

        stale_source = app._refresh_status_view(
            {"status": "succeeded", "target_date": "2026-05-29", "actual_date": "2026-05-28"},
            "2026-05-28",
            available,
        )
        self.assertEqual(stale_source["level"], "warning")
        self.assertEqual(stale_source["message"], "数据源最新为 2026-05-28，已生成对应数据")
        self.assertFalse(stale_source["show_refresh_button"])

        failed = app._refresh_status_view(
            {"status": "failed", "target_date": "2026-05-29", "error": "network down"},
            "2026-05-28",
            available,
        )
        self.assertEqual(failed["level"], "error")
        self.assertIn("network down", failed["message"])

        skipped = app._refresh_status_view(
            {"status": "skipped", "target_date": "2026-05-29", "generated_at": "2026-05-29 16:00:00"},
            "2026-05-29",
            available,
        )
        self.assertEqual(skipped["message"], "当前已是最新 2026-05-29 数据，生成 2026-05-29 16:00:00")

        success_stats = app._refresh_status_view(
            {"status": "skipped", "target_date": "2026-05-29"},
            "2026-05-29",
            available,
            fetch_report={"history_total": 455, "history_success": 455, "history_failed": 0},
        )
        self.assertEqual(success_stats["history_stats_message"], "历史行情：成功 455 / 总数 455，失败 0")
        self.assertFalse(success_stats["show_retry_button"])

        stock_problem_stats = app._refresh_status_view(
            {"status": "skipped", "target_date": "2026-05-29"},
            "2026-05-29",
            available,
            fetch_report={"history_total": 455, "history_success": 455, "history_failed": 0},
            problem_summary={"snapshot_count": 2, "problem_count": 162, "dates": ["2026-05-28", "2026-06-01"]},
        )
        self.assertEqual(stock_problem_stats["problem_summary_message"], "存量待补拉：2 个日期，失败/缺失 162 个历史文件：2026-05-28、2026-06-01")
        self.assertTrue(stock_problem_stats["show_retry_button"])

        failed_stats = app._refresh_status_view(
            {"status": "skipped", "target_date": "2026-05-29"},
            "2026-05-29",
            available,
            fetch_report={
                "history_total": 433,
                "history_success": 431,
                "history_failed": 2,
                "failed_history": [
                    {"code": "159031", "name": "粮食ETF博时", "error": "empty history"},
                    {"code": "517130", "name": "N云计算ETF嘉实", "error": "empty history"},
                ],
            },
        )
        self.assertEqual(failed_stats["history_failed"], 2)
        self.assertEqual(failed_stats["failed_history_codes"], ["159031", "517130"])
        self.assertIn("失败代码 159031、517130", failed_stats["history_stats_message"])
        self.assertFalse(failed_stats["show_retry_button"])

        retry_progress = app._refresh_status_view(
            {
                "status": "running",
                "target_date": "存量问题",
                "phase": "retry_fetch_history",
                "updated_at": "2026-05-30 09:00:00",
                "progress": {
                    "completed": 36,
                    "total": 414,
                    "current_code": "512290",
                    "skipped_cached": 24,
                    "fetched": 12,
                    "errors": 0,
                    "max_workers": DEFAULT_HISTORY_FETCH_MAX_WORKERS,
                    "snapshot_completed": 0,
                    "snapshot_total": 2,
                    "current_source_date": "2026-05-28",
                },
            },
            "2026-05-29",
            available,
        )
        self.assertEqual(
            retry_progress["message"],
            (
                "正在补拉存量问题：日期 1/2 2026-05-28，历史行情 36/414，缓存 24，"
                f"新拉取 12，失败 0，并发 {DEFAULT_HISTORY_FETCH_MAX_WORKERS}，当前 512290"
            ),
        )


def _selection_event(rows: list[int]) -> object:
    return types.SimpleNamespace(selection=types.SimpleNamespace(rows=rows))


if __name__ == "__main__":
    unittest.main()
