from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_trade_checklist import valid_draft
from trend_radar.trade_records import TradeRecordStore


def execution(**changes: str) -> dict[str, str]:
    return {"instrument_id": "BTC-USDT-SWAP", "entry_price": "101", "quantity": "0.01",
            "opened_at": "2026-10-16T12:00:00+08:00", **changes}


def outcome(**changes: str) -> dict[str, str]:
    return {"closed_at": "2026-10-17T12:00:00+08:00", "pnl": "12.5",
            "reason": "移动止损触发", "notes": "", **changes}


class TradeRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = TradeRecordStore(self.root)

    def test_real_trade_lifecycle_preserves_original_plan(self) -> None:
        draft = valid_draft()
        record = self.store.create(draft, execution(), confirmed=True)
        self.assertEqual(record.status, "active")
        self.assertEqual(self.store.list_records()[0].execution["entry_price"], "101")
        record = self.store.update_execution(record.id, record.revision, execution(entry_price="102"))
        record = self.store.update_stop(record.id, record.revision, "95")
        record = self.store.close(record.id, record.revision, outcome(), confirmed=True)
        self.assertEqual(record.status, "closed")
        record = self.store.update_closed(record.id, record.revision, execution(), outcome(pnl="-3"), confirmed=True)
        restored = TradeRecordStore(self.root).list_records()[0]
        self.assertEqual(restored.snapshot, draft)
        self.assertEqual(restored.result["pnl"], "-3")
        self.assertEqual(restored.stop_price, "95")
        self.store.delete(record.id, record.revision)
        self.assertEqual(self.store.list_records(), [])

    def test_invalid_execution_never_creates_a_record(self) -> None:
        for values in [execution(entry_price="NaN"), execution(entry_price="1e9999999"),
                       execution(quantity="0"), execution(quantity="-1"),
                       execution(opened_at="2026-10-16T12:00:00"), execution(instrument_id="BTC")]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.store.create(valid_draft(), values, confirmed=True)
        self.assertEqual(self.store.list_records(), [])

    def test_unpassed_and_unconfirmed_plans_are_not_trades(self) -> None:
        for draft, confirmed in [(valid_draft(account_balance="700"), True), (valid_draft(), False)]:
            with self.assertRaises(ValueError):
                self.store.create(draft, execution(), confirmed=confirmed)
        self.assertEqual(self.store.list_records(), [])

    def test_one_active_trade_even_for_concurrent_submissions(self) -> None:
        def create() -> bool:
            try:
                TradeRecordStore(self.root).create(valid_draft(), execution(), confirmed=True)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: create(), range(2))), [False, True])
        self.assertEqual(len(self.store.list_records()), 1)

    def test_stop_updates_only_tighten_for_both_directions(self) -> None:
        for draft, first, tighter, looser in [
            (valid_draft(), "95", "97", "94"),
            (valid_draft(direction="做空", planned_exit_price="110", liquidation_price="112", take_profit_price="80"), "105", "103", "106"),
        ]:
            record = self.store.create(draft, execution(), confirmed=True)
            with self.assertRaises(ValueError):
                self.store.update_stop(record.id, record.revision, draft["liquidation_price"])
            record = self.store.update_stop(record.id, record.revision, first)
            for invalid in (first, looser):
                with self.assertRaisesRegex(ValueError, "只允许收紧"):
                    self.store.update_stop(record.id, record.revision, invalid)
            record = self.store.update_stop(record.id, record.revision, tighter)
            self.assertEqual(self.store.list_records()[0].stop_price, tighter)
            self.store.delete(record.id, record.revision)

    def test_stale_page_cannot_overwrite_or_delete_newer_record(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        self.store.update_stop(record.id, record.revision, "95")
        for operation in [lambda: self.store.delete(record.id, record.revision),
                          lambda: self.store.update_execution(record.id, record.revision, execution(entry_price="102")),
                          lambda: self.store.close(record.id, record.revision, outcome(), confirmed=True)]:
            with self.assertRaisesRegex(ValueError, "重新加载"):
                operation()
        self.assertEqual(self.store.list_records()[0].stop_price, "95")

    def test_close_and_history_corrections_require_confirmation_and_valid_results(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        for result in [outcome(closed_at="2026-10-15T12:00:00+08:00"), outcome(reason=""), outcome(pnl="NaN")]:
            with self.assertRaises(ValueError):
                self.store.close(record.id, record.revision, result, confirmed=True)
        with self.assertRaises(ValueError):
            self.store.close(record.id, record.revision, outcome(), confirmed=False)
        record = self.store.close(record.id, record.revision, outcome(pnl="0"), confirmed=True)
        with self.assertRaisesRegex(ValueError, "二次确认"):
            self.store.update_closed(record.id, record.revision, execution(), outcome(), confirmed=False)
        with self.assertRaises(ValueError):
            self.store.update_stop(record.id, record.revision, "95")
        self.store.create(valid_draft(), execution(), confirmed=True)
        self.assertEqual([item.status for item in self.store.list_records()], ["active", "closed"])

    def test_corruption_is_reported_and_never_overwritten(self) -> None:
        self.store.path.parent.mkdir(parents=True)
        for content in ["broken-json", '{"version":2,"records":[]}', '{"version":1,"records":[{}]}']:
            self.store.path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "损坏"):
                self.store.list_records()
            with self.assertRaises(ValueError):
                self.store.create(valid_draft(), execution(), confirmed=True)
            self.assertEqual(self.store.path.read_text(encoding="utf-8"), content)

    def test_failed_atomic_write_preserves_previous_record(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        with patch("os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.update_stop(record.id, record.revision, "95")
        self.assertIsNone(self.store.list_records()[0].stop_price)
        self.assertEqual(sorted(path.name for path in self.store.path.parent.iterdir()), ["records.json", "records.lock"])


if __name__ == "__main__":
    unittest.main()
