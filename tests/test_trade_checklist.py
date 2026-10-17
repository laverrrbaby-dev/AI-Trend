from __future__ import annotations

import sys
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trend_radar.trade_checklist import evaluate_checklist
from trend_radar import trade_checklist


def valid_draft(**changes: object) -> dict:
    return {
        "account_balance": "500", "symbol": "BTC-USDT-SWAP",
        "direction": "做多", "trade_type": "价格行为", "timeframe": "日线 UTC",
        "exit_reason": "参考前一天最低点 90", "entry_price": "100",
        "planned_exit_price": "90", "take_profit_price": "120",
        "leverage": "10", "quantity": "0.01", "liquidation_price": "88",
        "previous_position_closed": True, "orders_cleared": True,
        "funds_checked": True, "okx_cross_checked": True,
        "order_parameters_checked": True, **changes,
    }


class ChecklistRulesTests(unittest.TestCase):
    def test_valid_long_reports_adjusted_exit_and_distances(self) -> None:
        result = evaluate_checklist(valid_draft())
        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.reward_distance, Decimal("20"))
        self.assertEqual(result.risk_distance, Decimal("12"))
        self.assertAlmostEqual(float(result.reward_risk_ratio), 1.6666666667)
        self.assertEqual(result.exit_deviation, Decimal("-2"))

    def test_balance_mismatch_and_unconfirmed_checks_cannot_pass(self) -> None:
        cases = [{"account_balance": "700"}, {"previous_position_closed": False},
                 {"orders_cleared": False}, {"funds_checked": False},
                 {"okx_cross_checked": False}, {"order_parameters_checked": False},
                 {"funds_checked": "true"}]
        for changes in cases:
            with self.subTest(changes=changes):
                result = evaluate_checklist(valid_draft(**changes))
                self.assertFalse(result.passed)
                self.assertTrue(result.errors)

    def test_exit_cannot_precede_strategy_in_either_direction(self) -> None:
        cases = [({}, True), ({"liquidation_price": "90"}, True),
                 ({"liquidation_price": "91"}, False),
                 ({"direction": "做空", "planned_exit_price": "110", "liquidation_price": "112", "take_profit_price": "80"}, True),
                 ({"direction": "做空", "planned_exit_price": "110", "liquidation_price": "109", "take_profit_price": "80"}, False),
                 ({"take_profit_price": "80"}, False),
                 ({"planned_exit_price": "110"}, False),
                 ({"liquidation_price": "100"}, False)]
        for changes, passed in cases:
            with self.subTest(changes=changes):
                self.assertEqual(evaluate_checklist(valid_draft(**changes)).passed, passed)

    def test_reward_risk_threshold_is_strict_and_uses_adjusted_exit(self) -> None:
        for target, expected in [("115", False), ("115.01", True), ("114.99", False),
                                 ("115.00000001", True)]:
            with self.subTest(target=target):
                self.assertEqual(evaluate_checklist(valid_draft(liquidation_price="90", take_profit_price=target)).passed, expected)
        # Original 90 exit has ratio 2, but moving to 85 reduces it below 1.5.
        self.assertFalse(evaluate_checklist(valid_draft(liquidation_price="85")).passed)

    def test_missing_malformed_and_nonpositive_fields_fail_closed(self) -> None:
        self.assertFalse(evaluate_checklist({}).passed)
        for field in ["account_balance", "entry_price", "planned_exit_price", "take_profit_price",
                      "liquidation_price", "leverage", "quantity"]:
            for value in ["", "oops", "NaN", "Infinity", "-Infinity", "0", "-1", True, None, [], "1e999999"]:
                with self.subTest(field=field, value=value):
                    self.assertFalse(evaluate_checklist(valid_draft(**{field: value})).passed)
        for field in ["symbol", "timeframe", "exit_reason", "direction", "trade_type"]:
            for value in ["", "   ", None, []]:
                with self.subTest(field=field, value=value):
                    self.assertFalse(evaluate_checklist(valid_draft(**{field: value})).passed)
        self.assertFalse(evaluate_checklist(valid_draft(direction="sideways")).passed)
        self.assertFalse(evaluate_checklist(valid_draft(trade_type="unknown")).passed)


class ChecklistDraftTests(unittest.TestCase):
    def test_current_draft_round_trips_and_new_check_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(trade_checklist.load_checklist_draft(root), {})
            draft = valid_draft(quantity="0.00010000000001")
            trade_checklist.save_checklist_draft(root, draft)
            self.assertEqual(trade_checklist.load_checklist_draft(root), draft)
            trade_checklist.save_checklist_draft(root, valid_draft(account_balance="700"))
            self.assertFalse(evaluate_checklist(trade_checklist.load_checklist_draft(root)).passed)
            trade_checklist.save_checklist_draft(root, {})
            self.assertEqual(trade_checklist.load_checklist_draft(root), {})

    def test_corrupt_or_wrong_schema_drafts_are_rejected(self) -> None:
        payloads = ["not-json", "[]", '{"version": 0, "draft": {}}',
                    '{"version": 1, "draft": []}',
                    json.dumps({"version": 1, "draft": {"funds_checked": "true"}}),
                    json.dumps({"version": 1, "draft": {"entry_price": 100}})]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data/crypto/trade_checklist/draft.json"
            path.parent.mkdir(parents=True)
            for payload in payloads:
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        trade_checklist.load_checklist_draft(root)

    def test_saved_unknown_result_fields_cannot_restore_passed_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trade_checklist.save_checklist_draft(root, {"passed": True, "account_balance": "700"})
            restored = trade_checklist.load_checklist_draft(root)
            self.assertNotIn("passed", restored)
            self.assertFalse(evaluate_checklist(restored).passed)


if __name__ == "__main__":
    unittest.main()
