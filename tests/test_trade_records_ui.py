from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from test_trade_checklist import valid_draft
from test_trade_records import execution
from test_trade_records import outcome
from trend_radar.trade_checklist import load_checklist_draft, save_checklist_draft
from trend_radar.trade_records import TradeRecordStore


def button(page: AppTest, label: str):
    return next(widget for widget in page.button if widget.label == label)


class TradeRecordsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = TradeRecordStore(self.root)

    def checklist(self) -> AppTest:
        save_checklist_draft(self.root, valid_draft())
        return AppTest.from_string(
            "from pathlib import Path\n"
            "from trend_radar.trade_checklist_ui import render_trade_checklist\n"
            f"render_trade_checklist(Path({str(self.root)!r}))\n"
        ).run()

    def test_passed_checklist_saves_actual_trade_only_after_confirmation(self) -> None:
        page = self.checklist()
        self.assertFalse(page.exception)
        button(page, "保存为进行中交易").click().run()
        self.assertEqual(self.store.list_records(), [])
        self.assertTrue(page.error)
        page.checkbox(key="trade-create-confirmed").check()
        page.text_input(key="trade-create-entry_price").set_value("101")
        button(page, "保存为进行中交易").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(self.store.list_records()[0].execution["entry_price"], "101")
        self.assertEqual(self.store.list_records()[0].snapshot["entry_price"], "100")
        self.assertFalse(any(item.label == "保存为进行中交易" for item in page.button))
        self.assertFalse(any(load_checklist_draft(self.root).values()))
        self.assertEqual(page.text_input(key="trade-checklist-account_balance").value, "")
        self.assertEqual(page.text_input(key="trade-checklist-entry_price").value, "")
        self.assertFalse(page.checkbox(key="trade-checklist-previous_position_closed").value)

    def records(self, *, unavailable: bool = False) -> AppTest:
        return AppTest.from_string(
            "from pathlib import Path\n"
            "from datetime import datetime, time, timedelta\n"
            "from trend_radar.trade_record_market import BEIJING\n"
            "from trend_radar.trade_records_ui import render_trade_records\n"
            "def http(url, headers):\n"
            + ("    raise OSError('network unavailable')\n" if unavailable else
               "    now = datetime.now(BEIJING)\n"
               "    if '/ticker?' in url:\n"
               "        return {'code':'0','data':[{'instId':'BTC-USDT-SWAP','last':'102','ts':str(int(now.timestamp()*1000))}]}\n"
               "    opened=datetime.combine(now.date()-timedelta(days=1),time(),BEIJING)\n"
               f"    fixture=Path({str(self.root / 'market-low.txt')!r})\n"
               "    low=fixture.read_text() if fixture.exists() else '95'\n"
               "    return {'code':'0','data':[[str(int(opened.timestamp()*1000)),'100','110',low,'102','1','1','1','1']]}\n")
            + f"render_trade_records(Path({str(self.root)!r}), fetch_json=http)\n"
        ).run()

    def set_text(self, page: AppTest, label: str, value: str) -> None:
        next(item for item in page.text_input if item.label == label).set_value(value)

    def action(self, page: AppTest, label: str) -> AppTest:
        return next(item for item in page.selectbox if item.label == "操作").select(label).run()

    def test_active_stop_manual_close_and_confirmed_history_edit(self) -> None:
        self.store.create(valid_draft(), execution(), confirmed=True)
        page = self.records()
        self.assertFalse(page.exception)
        self.assertTrue(any("95" in item.value for item in page.info))
        self.assertIsNone(self.store.list_records()[0].stop_price)
        page = self.action(page, "更新止损")
        self.set_text(page, "已在 OKX 设置的新止损价", "95")
        next(item for item in page.checkbox if item.label == "我已在 OKX 手动设置此止损价").check()
        button(page, "保存当前止损").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(self.store.list_records()[0].stop_price, "95")
        page = self.action(page, "结束交易")
        self.set_text(page, "平仓时间（含时区）", "2026-10-17T12:00:00+08:00")
        self.set_text(page, "实际盈亏（U，可为负数）", "12.5")
        self.set_text(page, "退出原因", "移动止损")
        button(page, "核对平仓信息").click().run()
        self.assertEqual(self.store.list_records()[0].status, "active")
        button(page, "确认结束交易").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(self.store.list_records()[0].status, "closed")
        page.radio(key="trade-records-status").set_value("已结束").run()
        page = self.action(page, "修改历史")
        self.set_text(page, "实际盈亏（U，可为负数）", "-3")
        button(page, "预览历史修改").click().run()
        self.assertEqual(self.store.list_records()[0].result["pnl"], "12.5")
        button(page, "确认保存历史修改").click().run()
        self.assertFalse(page.exception)
        record = self.store.list_records()[0]
        self.assertEqual(record.result["pnl"], "-3")
        self.assertEqual(record.snapshot, valid_draft())

    def test_unavailable_market_does_not_block_manual_record_management(self) -> None:
        self.store.create(valid_draft(), execution(), confirmed=True)
        page = self.records(unavailable=True)
        self.assertFalse(page.exception)
        self.assertTrue(any("建议不可用" in item.value for item in page.warning))
        page = self.action(page, "修改成交信息")
        self.set_text(page, "实际成交价", "103")
        button(page, "保存成交信息").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(self.store.list_records()[0].execution["entry_price"], "103")

    def test_looser_stop_rejected_and_input_preserved(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        self.store.update_stop(record.id, record.revision, "95")
        page = self.action(self.records(), "更新止损")
        self.set_text(page, "已在 OKX 设置的新止损价", "94")
        next(item for item in page.checkbox if item.label == "我已在 OKX 手动设置此止损价").check()
        button(page, "保存当前止损").click().run()
        self.assertFalse(page.exception)
        self.assertTrue(any("只允许收紧" in item.value for item in page.error))
        self.assertEqual(next(item.value for item in page.text_input if item.label == "已在 OKX 设置的新止损价"), "94")
        self.assertEqual(self.store.list_records()[0].stop_price, "95")

    def test_failed_save_preserves_form_and_can_retry(self) -> None:
        self.store.create(valid_draft(), execution(), confirmed=True)
        page = self.action(self.records(), "修改成交信息")
        self.set_text(page, "实际成交价", "103")
        lock = self.store.path.with_suffix(".lock")
        lock.unlink()
        lock.mkdir()
        button(page, "保存成交信息").click().run()
        self.assertFalse(page.exception)
        self.assertTrue(page.error)
        self.assertEqual(next(item.value for item in page.text_input if item.label == "实际成交价"), "103")
        lock.rmdir()
        button(page, "保存成交信息").click().run()
        self.assertEqual(self.store.list_records()[0].execution["entry_price"], "103")

    def test_stale_form_preserves_input_and_rejects_overwrite(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        page = self.action(self.records(), "修改成交信息")
        self.set_text(page, "实际成交价", "103")
        self.store.update_execution(record.id, record.revision, execution(entry_price="104"))
        button(page, "保存成交信息").click().run()
        self.assertFalse(page.exception)
        self.assertTrue(any("重新加载" in item.value for item in page.error))
        self.assertEqual(next(item.value for item in page.text_input if item.label == "实际成交价"), "103")
        self.assertEqual(self.store.list_records()[0].execution["entry_price"], "104")

    def test_history_delete_and_refresh_do_not_restore_removed_record(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        self.store.close(record.id, record.revision, outcome(), confirmed=True)
        page = self.records()
        page.radio(key="trade-records-status").set_value("已结束").run()
        page = self.action(page, "删除交易")
        self.assertTrue(button(page, "永久删除记录").disabled)
        next(item for item in page.checkbox if item.label == "我确认删除这笔交易").check().run()
        button(page, "永久删除记录").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(self.store.list_records(), [])
        button(page, "重新加载记录与行情").click().run()
        self.assertFalse(page.exception)
        self.assertFalse(page.selectbox)

    def test_corrupt_records_block_creation_and_record_page_without_overwrite(self) -> None:
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text("bad-records", encoding="utf-8")
        page = self.checklist()
        self.assertFalse(page.exception)
        self.assertTrue(any("交易记录不可用" in item.value for item in page.error))
        self.assertFalse(any(item.label == "保存为进行中交易" for item in page.button))
        page = self.records()
        self.assertFalse(page.exception)
        self.assertTrue(page.error)
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "bad-records")

    def test_history_preview_can_be_changed_and_stale_confirmation_cannot_overwrite(self) -> None:
        record = self.store.create(valid_draft(), execution(), confirmed=True)
        record = self.store.close(record.id, record.revision, outcome(), confirmed=True)
        page = self.records()
        page.radio(key="trade-records-status").set_value("已结束").run()
        page = self.action(page, "修改历史")
        self.set_text(page, "实际盈亏（U，可为负数）", "-3")
        button(page, "预览历史修改").click().run()
        button(page, "返回修改").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(next(item.value for item in page.text_input if item.label == "实际盈亏（U，可为负数）"), "-3")
        self.set_text(page, "实际盈亏（U，可为负数）", "-4")
        button(page, "预览历史修改").click().run()
        self.store.update_closed(record.id, record.revision, execution(), outcome(pnl="99"), confirmed=True)
        button(page, "确认保存历史修改").click().run()
        self.assertFalse(page.exception)
        self.assertTrue(any("重新加载" in item.value for item in page.error))
        self.assertEqual(self.store.list_records()[0].result["pnl"], "99")
        button(page, "重新加载记录与行情").click().run()
        self.assertFalse(page.exception)
        self.assertFalse(any(item.label == "确认保存历史修改" for item in page.button))

    def test_draft_reset_leaves_saved_position_and_unpassed_plan_has_no_save_button(self) -> None:
        page = self.checklist()
        page.checkbox(key="trade-create-confirmed").check()
        button(page, "保存为进行中交易").click().run()
        button(page, "重置当前填写").click().run()
        self.assertFalse(page.exception)
        self.assertEqual(len(self.store.list_records()), 1)
        self.assertFalse(any(item.label == "保存为进行中交易" for item in page.button))

    def test_checklist_save_failure_keeps_actual_execution_for_retry(self) -> None:
        page = self.checklist()
        self.store.path.parent.mkdir(parents=True)
        lock = self.store.path.with_suffix(".lock")
        lock.mkdir()
        page.checkbox(key="trade-create-confirmed").check()
        page.text_input(key="trade-create-entry_price").set_value("103")
        button(page, "保存为进行中交易").click().run()
        self.assertFalse(page.exception)
        self.assertTrue(page.error)
        self.assertEqual(page.text_input(key="trade-create-entry_price").value, "103")
        lock.rmdir()
        button(page, "保存为进行中交易").click().run()
        self.assertEqual(self.store.list_records()[0].execution["entry_price"], "103")

    def test_returning_from_history_gets_new_market_reference(self) -> None:
        self.store.create(valid_draft(), execution(), confirmed=True)
        page = self.records()
        self.assertTrue(any("收紧至 95" in item.value for item in page.info))
        page.radio(key="trade-records-status").set_value("已结束").run()
        (self.root / "market-low.txt").write_text("96")
        page.radio(key="trade-records-status").set_value("进行中").run()
        self.assertTrue(any("收紧至 96" in item.value for item in page.info))


if __name__ == "__main__":
    unittest.main()
