from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import _crypto_dashboard_table  # noqa: E402


class CryptoDashboardTableTests(unittest.TestCase):
    def test_table_always_contains_fixed_five_in_order(self) -> None:
        table = _crypto_dashboard_table({"symbols": [{"symbol": "ETH", "status": "有变化", "latest_change": "TD9：上涨计数"}]})
        self.assertEqual(table["币种"].tolist(), ["BTC", "ETH", "SOL", "XAUT", "BNB"])
        self.assertEqual(list(table.columns), ["币种", "当前状态", "最新变化", "变化日期", "数据截至", "数据质量"])
        self.assertNotIn("买入", " ".join(table["最新变化"].tolist()))


if __name__ == "__main__":
    unittest.main()
