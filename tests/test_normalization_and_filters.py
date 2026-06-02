from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.filters import filter_equity_etfs
from trend_radar.normalization import money_to_yi, normalize_history, normalize_spot, share_to_yi, to_numeric_series


class NormalizationAndFilterTests(unittest.TestCase):
    def test_numeric_parsing_preserves_negative_values(self) -> None:
        values = to_numeric_series(pd.Series(["-1.25%", "--", "3,000"]))
        self.assertEqual(values.iloc[0], -1.25)
        self.assertTrue(pd.isna(values.iloc[1]))
        self.assertEqual(values.iloc[2], 3000)

    def test_unit_conversion_to_yi(self) -> None:
        self.assertEqual(money_to_yi(pd.Series([300_000_000]), "成交额").iloc[0], 3)
        self.assertEqual(money_to_yi(pd.Series([30_000]), "成交额(万元)").iloc[0], 3)
        self.assertEqual(share_to_yi(pd.Series([50_000]), "基金份额(万份)").iloc[0], 5)
        self.assertEqual(share_to_yi(pd.Series([600_000_000]), "基金份额").iloc[0], 6)

    def test_equity_etf_filter_excludes_non_a_share_products(self) -> None:
        raw = pd.DataFrame(
            {
                "代码": ["159995", "511990", "511880", "513100", "518880", "508000", "159577", "159747", "510300"],
                "名称": ["芯片ETF", "货币ETF", "华宝添益ETF", "纳指ETF", "黄金ETF", "REIT ETF", "美国50ETF", "香港科技ETF", "沪深300ETF"],
                "最新价": [1, 1, 1, 1, 1, 1, 1, 1, 1],
                "成交额": [100_000_000] * 9,
            }
        )
        spot = normalize_spot(raw)
        filtered = filter_equity_etfs(spot)
        self.assertEqual(filtered["code"].tolist(), ["159995", "510300"])

    def test_normalize_history_outputs_ohlc_contract(self) -> None:
        raw = pd.DataFrame(
            {
                "日期": ["2026-05-27", "2026-05-28"],
                "开盘": [1.0, 1.1],
                "收盘": [1.08, 1.05],
                "最高": [1.1, 1.12],
                "最低": [0.99, 1.03],
                "成交额": [100_000_000, 120_000_000],
                "成交量": [10000, 12000],
                "涨跌幅": [0.5, -0.2],
            }
        )

        history = normalize_history(raw, "159995")

        self.assertEqual(
            history.columns.tolist(),
            ["code", "日期", "开盘价", "最高价", "最低价", "收盘价", "成交额(亿)", "成交量", "涨跌幅"],
        )
        self.assertEqual(history["开盘价"].tolist(), [1.0, 1.1])
        self.assertEqual(history["最高价"].tolist(), [1.1, 1.12])
        self.assertEqual(history["最低价"].tolist(), [0.99, 1.03])
        self.assertEqual(history["成交量"].tolist(), [10000, 12000])


if __name__ == "__main__":
    unittest.main()
